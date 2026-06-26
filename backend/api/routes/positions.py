"""Positions endpoint — returns per-account rows and summary."""

import pandas as pd
import polars as pl
from litestar import Controller, Request, get
from litestar.exceptions import HTTPException

from backend.api.auth_guard import is_admin_request, is_authenticated_request
from backend.api.rbac import (
    resolve_role_from_connection, user_scope_for_connection,
    normalise_role,
)
from backend.api.cache import get_or_fetch, invalidate
from backend.api.schemas import PositionsResponse, PositionRow, PositionsSummaryRow
from backend.shared.helpers import broker_apis
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account, mask_column

logger = get_logger(__name__)

_ROW_COLS = [
    'account', 'tradingsymbol', 'exchange', 'product',
    'quantity', 'average_price', 'close_price', 'last_price',
    'pnl', 'pnl_percentage', 'unrealised', 'realised',
    'day_change', 'day_change_val', 'day_change_percentage',
    # Intraday split — used by Candidates grid to detect closed-then-
    # reopened activity and render the leg as two separate rows.
    'overnight_quantity', 'day_buy_quantity', 'day_sell_quantity',
    'day_buy_value', 'day_sell_value',
]

_TTL = 30


def _is_broker_outage(err: Exception) -> bool:
    """Detect Kite (Zerodha) upstream HTTP gateway errors. See
    funds.py for the rationale — same helper, same patterns."""
    s = str(err).lower()
    return any(needle in s for needle in (
        'bad gateway', '502', '503', '504',
        'service unavailable', 'gateway timeout',
    ))


async def _fetch() -> PositionsResponse:
    per_acct = broker_apis.fetch_positions()
    # Outage detection: only raise when every per-account call failed
    # (`fetch_failed` flag set in broker_apis.py). An empty result with
    # the flag UNSET is a legitimate "no positions" state — e.g.
    # operator placed a LIMIT order that hasn't filled yet, or simply
    # has no open positions today. Surfacing that as a 503 produced a
    # false "Positions feed unavailable" banner on /admin/derivatives.
    if per_acct and all(df.attrs.get('fetch_failed', False) for df in per_acct):
        raise Exception("Broker (Kite) returned no positions data — upstream Bad Gateway / outage")
    raw = pd.concat(per_acct, ignore_index=True) if per_acct else pd.DataFrame()
    # Legitimate empty book — no positions on any account. Return a
    # well-formed empty response so /admin/derivatives renders zero
    # candidates instead of the false "Positions feed unavailable"
    # banner (which only fires on actual outage 5xx now).
    if raw.empty:
        return PositionsResponse(rows=[], summary=[], refreshed_at=timestamp_display())
    # Account masking removed — admin-only pages show real account IDs

    # Backfill missing market data (close_price + last_price) for
    # adapters that don't populate them (Dhan v2 positions endpoint
    # omits close_price, sometimes last_price too). One batched
    # PriceBroker.quote() across every missing-field row from every
    # account — not N per N accounts. Source brokers keep their
    # account-specific facts (avg_price, qty, realised); market data
    # routes through Kite. Day_change_val + pnl on patched rows are
    # recomputed inside the helper.
    broker_apis.backfill_market_data(raw)

    # Refresh stale last_price from the live KiteTicker tick_map.
    # Kite's /positions REST endpoint sometimes lags behind the WS
    # feed by minutes — observed on 2026-06-22 around 09:30 IST where
    # CRUDEOIL options showed last_price === close_price (stuck on
    # yesterday's EOD) even though MCX had been open 30 min. With
    # last_price = close_price the day_change_val formula collapses
    # to 0, so the Snapshot grid Day column stayed at zero all
    # session. Override using the streamed tick BEFORE close-override
    # so the day_change_val recompute below sees the fresh LTP.
    _override_stale_ltp_from_ticker(raw)

    # Override stale close_price with yesterday's daily_book snapshot.
    # Why: Kite's positions.close_price (and quote.ohlc.close) lag the
    # actual previous-session close — observed on 2026-06-19 at 00:30
    # IST showing close=7089 for GOLDM26JUN145000CE when the true 6/18
    # EOD was 3402 (one full Kite roll behind). Without this override,
    # the decomposed day_pnl formula computes (LTP - stale_close) × qty
    # against a 2-session-old reference, producing the +1.33L phantom
    # gain the operator reported. The snapshot is captured at our
    # daemon's startup + 15:35 IST — the most recent one is the actual
    # EOD of the prior session.
    await _override_stale_close_from_snapshot(raw)

    numeric = raw.select_dtypes(include='number').columns
    raw[numeric] = raw[numeric].fillna(0)
    df = pl.from_pandas(raw)

    row_cols = [c for c in _ROW_COLS if c in df.columns]
    df_rows = df.select(row_cols)

    # Compute prev_val per row so we can sum it per account and derive a
    # meaningful day_change_percentage on the summary (sum Δ / sum prev).
    df = df.with_columns(
        (pl.col('close_price') * pl.col('quantity')).abs().alias('_prev_val')
    )
    sum_cols = [c for c in ('pnl', 'day_change_val', '_prev_val') if c in df.columns]
    if sum_cols:
        grouped = df.group_by('account').agg([pl.col(c).sum() for c in sum_cols])
    else:
        grouped = pl.DataFrame({'account': []})
    # Ensure all sum columns exist even when absent from the broker frame
    for col in ('pnl', 'day_change_val', '_prev_val'):
        if col not in grouped.columns:
            grouped = grouped.with_columns(pl.lit(0.0).alias(col))
    totals = pl.DataFrame([{
        'account': 'TOTAL',
        'pnl': grouped['pnl'].sum(),
        'day_change_val': grouped['day_change_val'].sum(),
        '_prev_val': grouped['_prev_val'].sum(),
    }])
    summary_df = pl.concat([grouped, totals], how='diagonal').fill_nan(0).fill_null(0)
    # day_change_percentage = Σ day_change_val / Σ |close × qty|, per-row's
    # absolute denominator captured above. Rename _prev_val to the
    # public field day_prev_val so the frontend can sum it for a
    # filtered-subset TOTAL row.
    summary_df = summary_df.with_columns(
        (pl.col('day_change_val') / pl.col('_prev_val').replace(0, None) * 100)
        .fill_nan(0).fill_null(0)
        .alias('day_change_percentage')
    ).rename({'_prev_val': 'day_prev_val'})

    rows = [
        PositionRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in df_rows.to_dicts()
    ]
    # Enrich option rows with position-Greeks (Δ × qty, Θ × qty) so the
    # /performance + /dashboard grids can surface them as columns without
    # round-tripping through /api/options/analytics per symbol.
    _enrich_position_greeks(rows)
    summary = [
        PositionsSummaryRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in summary_df.to_dicts()
    ]
    return PositionsResponse(rows=rows, summary=summary, refreshed_at=timestamp_display())


def _override_stale_ltp_from_ticker(raw: pd.DataFrame) -> None:
    """Patch `last_price` from the live KiteTicker tick_map for any
    row whose tradingsymbol the ticker is currently subscribed to.
    Kite's /positions REST API can lag the WS feed by minutes after
    market open for less-liquid contracts (observed on 2026-06-22 at
    09:30 IST, CRUDEOIL options stuck on yesterday's EOD ~30 min
    after MCX open). Without this override day_change_val collapses
    to 0 because (stale_LTP - close_price) === 0.

    Idempotent — only writes when the ticker LTP differs from the
    current row value by > 0.005. After patching, recomputes
    `day_change_val` + `day_change` on the affected rows using the
    same decomposed formula `broker_apis` uses so the value stays in
    sync with the new LTP.
    """
    if raw.empty or 'tradingsymbol' not in raw.columns:
        return
    try:
        from backend.shared.helpers.kite_ticker import _ticker
    except Exception:
        return
    patched_idx: list = []
    for idx in raw.index:
        sym = raw.at[idx, 'tradingsymbol']
        if not sym:
            continue
        tick_ltp = _ticker.get_ltp_by_sym(str(sym))
        if tick_ltp is None or tick_ltp <= 0:
            continue
        try:
            current = float(raw.at[idx, 'last_price']) if pd.notna(raw.at[idx, 'last_price']) else 0.0
        except (TypeError, ValueError):
            current = 0.0
        if abs(tick_ltp - current) <= 0.005:
            continue
        raw.at[idx, 'last_price'] = float(tick_ltp)
        patched_idx.append(idx)

    if not patched_idx:
        return
    # Recompute day_change_val on patched rows — same decomposed
    # formula `broker_apis._patch_day_change_val` uses. Without this
    # the row's day_change_val would still hold Kite's stale value
    # (computed against the pre-patch LTP === close_price, i.e. zero).
    _intraday_fields = {'overnight_quantity', 'day_buy_quantity',
                        'day_sell_quantity', 'day_buy_value', 'day_sell_value'}
    _sel = pd.Index(patched_idx)
    _ltp = pd.to_numeric(raw.loc[_sel, 'last_price'], errors='coerce').fillna(0)
    _cls = pd.to_numeric(raw.loc[_sel, 'close_price'], errors='coerce').fillna(0)
    _qty = pd.to_numeric(raw.loc[_sel, 'quantity'], errors='coerce').fillna(0)
    if _intraday_fields.issubset(raw.columns):
        _oq = pd.to_numeric(raw.loc[_sel, 'overnight_quantity'], errors='coerce').fillna(0)
        _bq = pd.to_numeric(raw.loc[_sel, 'day_buy_quantity'], errors='coerce').fillna(0)
        _sq = pd.to_numeric(raw.loc[_sel, 'day_sell_quantity'], errors='coerce').fillna(0)
        _bv = pd.to_numeric(raw.loc[_sel, 'day_buy_value'], errors='coerce').fillna(0)
        _sv = pd.to_numeric(raw.loc[_sel, 'day_sell_value'], errors='coerce').fillna(0)
        _dcv_calc = (
            _oq * (_ltp - _cls)
            + (_bq * _ltp - _bv)
            + (_sv - _sq * _ltp)
        )
    else:
        _dcv_calc = (_ltp - _cls) * _qty
    raw.loc[_sel, 'day_change_val'] = _dcv_calc.where(_ltp > 0, raw.loc[_sel, 'day_change_val'])
    raw.loc[_sel, 'day_change'] = _ltp - _cls
    # Recompute pnl from the patched LTP too. Without this, broker's
    # `pnl` (which was computed against the stale REST LTP) stays
    # frozen — the frontend's `_livePositionsPnl = Σ p.pnl + delta`
    # then double-misses: pnl uses stale LTP and delta = (live_ltp -
    # patched_LTP) × qty is ~0 because patched_LTP ≈ live_ltp from the
    # SSE stream. Operator confirmed: P showed ₹4.6L vs broker's
    # ₹6.27L when illiquid MCX options were stuck on yesterday's close.
    # pnl = unrealised + realised = (ltp − avg) × qty + realised.
    if 'average_price' in raw.columns and 'pnl' in raw.columns:
        _avg = pd.to_numeric(raw.loc[_sel, 'average_price'], errors='coerce').fillna(0)
        _real = (pd.to_numeric(raw.loc[_sel, 'realised'], errors='coerce').fillna(0)
                 if 'realised' in raw.columns else 0)
        _pnl_new = (_ltp - _avg) * _qty + _real
        raw.loc[_sel, 'pnl'] = _pnl_new.where(_ltp > 0, raw.loc[_sel, 'pnl'])
    logger.info(f"positions: ltp-override patched {len(patched_idx)}/{len(raw)} rows from KiteTicker")


async def _override_stale_close_from_snapshot(raw: pd.DataFrame) -> None:
    """Replace `close_price` with the most-recent daily_book snapshot LTP
    per (account, tradingsymbol). When found, recomputes the decomposed
    day_change_val so the row reflects the actual move since the prior
    session's authoritative close.

    Only triggers when the snapshot LTP differs from Kite's reported
    close_price by more than a tiny epsilon — rows where Kite is already
    current pass through unchanged."""
    if raw.empty or 'tradingsymbol' not in raw.columns or 'account' not in raw.columns:
        return

    # Pull the latest snapshot per (account, symbol) — DISTINCT ON keeps
    # only the most recent row, regardless of which date label the
    # snapshot daemon used (00:09 IST captures end up labelled with the
    # NEXT session's date; 23:52 IST captures end up labelled with the
    # CURRENT session's date — both represent the same prior-session EOD).
    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text

    pairs = list({(str(r["account"]), str(r["tradingsymbol"]))
                  for _, r in raw.iterrows()
                  if r.get("account") and r.get("tradingsymbol")})
    if not pairs:
        return

    # Filter to snapshots captured BEFORE today's market open (00:00 IST
    # today). Without this filter, a mid-session deploy's startup
    # snapshot would land in daily_book labelled as "most recent" and
    # the close-override would patch close_price to TODAY's mid-session
    # LTP — collapsing day_change_val to zero. Observed on 2026-06-22
    # ~09:38 IST: today's 09:38 IST snapshot returned LTP=264.5 for
    # CRUDEOIL26JUL6900PE, but yesterday's 23:59 IST snapshot (the true
    # MCX EOD) had LTP=220.
    from backend.shared.helpers.date_time_utils import timestamp_indian
    today_ist_midnight = timestamp_indian().replace(
        hour=0, minute=0, second=0, microsecond=0,
    )

    snapshot_map: dict[tuple[str, str], float] = {}
    try:
        async with async_session() as session:
            result = await session.execute(_sql_text("""
                SELECT DISTINCT ON (account, symbol) account, symbol, ltp
                FROM daily_book
                WHERE kind = 'positions' AND ltp IS NOT NULL AND ltp > 0
                  AND captured_at < :today_open
                ORDER BY account, symbol, captured_at DESC
            """), {"today_open": today_ist_midnight})
            for account, symbol, ltp in result.all():
                snapshot_map[(str(account), str(symbol))] = float(ltp)
    except Exception as e:
        logger.warning(f"daily_book close-override query failed: {e}")
        return

    if not snapshot_map:
        return

    # Apply override row-by-row. Use a small epsilon (0.005) so we only
    # patch when the values meaningfully diverge — protects against
    # rounding noise between Kite's float repr and snapshot storage.
    patched_idx: list = []
    for idx in raw.index:
        key = (str(raw.at[idx, 'account']), str(raw.at[idx, 'tradingsymbol']))
        snap_ltp = snapshot_map.get(key)
        if snap_ltp is None:
            continue
        try:
            current_close = float(raw.at[idx, 'close_price']) if pd.notna(raw.at[idx, 'close_price']) else 0.0
        except (TypeError, ValueError):
            current_close = 0.0
        if abs(snap_ltp - current_close) <= 0.005:
            continue
        raw.at[idx, 'close_price'] = snap_ltp
        patched_idx.append(idx)

    if not patched_idx:
        return

    # Recompute day_change_val on patched rows only — same decomposed
    # formula broker_apis uses, kept in sync. Non-patched rows keep
    # broker_apis' value untouched so backfilled Dhan rows (where the
    # backfill computes day_chg = (LTP - close) × qty as a fallback for
    # missing intraday fields) stay correct.
    _intraday_fields = {'overnight_quantity', 'day_buy_quantity',
                        'day_sell_quantity', 'day_buy_value', 'day_sell_value'}
    _sel = pd.Index(patched_idx)
    _ltp = pd.to_numeric(raw.loc[_sel, 'last_price'], errors='coerce').fillna(0)
    _cls = pd.to_numeric(raw.loc[_sel, 'close_price'], errors='coerce').fillna(0)
    _qty = pd.to_numeric(raw.loc[_sel, 'quantity'], errors='coerce').fillna(0)
    if _intraday_fields.issubset(raw.columns):
        _oq = pd.to_numeric(raw.loc[_sel, 'overnight_quantity'], errors='coerce').fillna(0)
        _bq = pd.to_numeric(raw.loc[_sel, 'day_buy_quantity'], errors='coerce').fillna(0)
        _sq = pd.to_numeric(raw.loc[_sel, 'day_sell_quantity'], errors='coerce').fillna(0)
        _bv = pd.to_numeric(raw.loc[_sel, 'day_buy_value'], errors='coerce').fillna(0)
        _sv = pd.to_numeric(raw.loc[_sel, 'day_sell_value'], errors='coerce').fillna(0)
        _dcv_calc = (
            _oq * (_ltp - _cls)
            + (_bq * _ltp - _bv)
            + (_sv - _sq * _ltp)
        )
    else:
        _dcv_calc = (_ltp - _cls) * _qty
    _dcv_valid = (_ltp > 0)
    raw.loc[_sel, 'day_change_val'] = _dcv_calc.where(_dcv_valid, raw.loc[_sel, 'day_change_val'])
    raw.loc[_sel, 'day_change'] = _ltp - _cls
    logger.info(f"positions: close-override patched {len(patched_idx)}/{len(raw)} rows from daily_book")


def _enrich_position_greeks(rows: list) -> None:
    """In-place: compute Δ-exposure (delta × qty) and Θ-per-day (theta × qty)
    for every row whose tradingsymbol parses as an option (CE / PE). Non-
    option rows leave both at 0.0 (PositionRow defaults).

    Underlying spots are fetched once per unique underlying via the price
    broker (~1 round-trip total, not per-row). IV is calibrated from each
    row's last_price using the existing bisection solver. A row's Greeks
    are silently skipped (delta_pos / theta_pos stay 0) when:
      - last_price is non-positive (closed-out row)
      - the underlying spot resolves to 0 (broker quote failed)
      - parse_tradingsymbol returns None (not a recognised F&O sym)
    """
    if not rows:
        return
    from backend.api.algo.derivatives import (
        parse_tradingsymbol, implied_vol, greeks, option_underlying_quote_key,
    )
    from backend.shared.brokers.registry import get_price_broker

    # Pass 1 — parse + collect unique underlying keys we need spots for.
    # option_underlying_quote_key() returns the right key shape for both
    # equity options (NSE:RELIANCE / NSE:NIFTY 50) AND MCX commodity
    # options (MCX:CRUDEOIL26JUNFUT — the matching-month future, which
    # serves as the spot proxy for MCX since the exchange has no separate
    # spot ticker). Falling back to a naked NSE:<name> for MCX would
    # always 404 and silently zero out the Greeks for every commodity row.
    parsed_by_idx: dict[int, tuple[dict, str]] = {}
    underlying_keys: set[str] = set()
    today = pd.Timestamp.now().normalize().date()
    for i, r in enumerate(rows):
        if r.quantity == 0 or r.last_price <= 0:
            continue
        p = parse_tradingsymbol(r.tradingsymbol)
        if not p or p.get("kind") != "opt":
            continue
        u_key = option_underlying_quote_key(r.tradingsymbol)
        if not u_key:
            continue
        parsed_by_idx[i] = (p, u_key)
        underlying_keys.add(u_key)

    if not parsed_by_idx:
        return

    # Pass 2 — single batched broker.quote() for every underlying.
    spot_by_key: dict[str, float] = {}
    try:
        broker = get_price_broker()
        spot_data = broker.quote(list(underlying_keys)) or {}
        for k, v in spot_data.items():
            spot_by_key[k] = float(v.get("last_price") or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Greeks enrich: underlying spot fetch failed: {exc}")
        return

    # Pass 3 — per-option IV calibration + greeks compute.
    r_rate = 0.07  # constant; matches the rate used in /api/options/analytics
    for i, (p, u_key) in parsed_by_idx.items():
        row = rows[i]
        S = spot_by_key.get(u_key, 0.0)
        if S <= 0:
            continue
        K = float(p.get("strike") or 0.0)
        if K <= 0:
            continue
        expiry = p.get("expiry")
        if not expiry:
            continue
        T_days = max((expiry - today).days, 0)
        T_years = max(T_days, 1) / 365.0   # never let T hit zero
        try:
            sigma = implied_vol(row.last_price, S, K, T_years, r_rate, p["opt_type"])
            g = greeks(S, K, T_years, r_rate, sigma, p["opt_type"])
            row.delta_pos = g["delta"] * row.quantity
            row.theta_pos = g["theta"] * row.quantity
        except Exception:
            # Single-row failures must NOT poison the whole positions
            # response — the operator gets 0/0 for this row and keeps
            # going. Log at debug, not error.
            logger.debug(f"Greeks compute failed for {row.tradingsymbol}", exc_info=True)


class PositionsController(Controller):
    path = "/api/positions"

    @get("/")
    async def get_positions(self, request: Request, fresh: bool = False) -> PositionsResponse:
        try:
            # Demo + public flow share one path: real broker data via
            # the cached fetch, with accounts masked for non-admin
            # callers (existing behaviour from the public /performance
            # page). No synthetic data — demo visitors see real
            # positions with `ZG####` style masks.
            if fresh:
                invalidate("positions")
            resp = await get_or_fetch("positions", _fetch, ttl_seconds=_TTL)
            # Horizontal scoping. Trader-role callers see only
            # positions on their `assigned_accounts`; firm-wide roles
            # (designated / risk / admin / partner / demo) see every
            # account. Empty assigned-list for a trader = empty
            # result (fail-safe — a freshly-onboarded trader sees
            # nothing until designated grants accounts).
            #
            # MUST run BEFORE masking — once accounts get masked to
            # `ZG####` the trader's assigned-account match can't run.
            role = normalise_role(resolve_role_from_connection(request))
            if role == "trader":
                allowed, _ = await user_scope_for_connection(request)
                allowed_set = {str(a).upper() for a in (allowed or [])}
                import msgspec
                resp = msgspec.structs.replace(
                    resp,
                    rows=[r for r in resp.rows
                          if str(getattr(r, "account", "")).upper() in allowed_set],
                    summary=[s for s in resp.summary
                             if str(getattr(s, "account", "")).upper() in allowed_set
                             or str(getattr(s, "account", "")).upper() == "TOTAL"],
                )
            # Mask account IDs for everyone who is NOT admin/designated.
            # CRITICAL — copy resp.rows / resp.summary BEFORE mutating;
            # the cache returns the same object reference across every
            # request, so an in-place mutation by a demo caller poisons
            # the cached payload and subsequent admin requests see
            # masked codes until the TTL expires (operator hit this
            # when transitioning from demo to signed-in).
            if not is_admin_request(request):
                import msgspec
                def _mask(row):
                    return msgspec.structs.replace(
                        row, account=mask_account(row.account)
                    )
                return msgspec.structs.replace(
                    resp,
                    rows=[_mask(r) for r in resp.rows],
                    summary=[_mask(s) for s in resp.summary],
                )
            return resp
        except Exception as e:
            logger.error(f"Positions API error: {e}")
            if _is_broker_outage(e):
                raise HTTPException(
                    status_code=503,
                    detail="Broker (Kite) is temporarily unavailable. Try again shortly.",
                )
            raise HTTPException(status_code=500, detail=str(e))
