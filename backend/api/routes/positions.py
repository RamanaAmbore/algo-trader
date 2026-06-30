"""Positions endpoint — returns per-account rows and summary."""

import pandas as pd
import polars as pl
from litestar import Controller, Request, get
from litestar.exceptions import HTTPException
from typing import Optional

from backend.api.auth_guard import is_admin_request, is_authenticated_request
from backend.api.rbac import (
    resolve_role_from_connection, user_scope_for_connection,
    normalise_role,
)
from backend.api.algo.pnl_math import decomposed_intraday_pnl, naive_day_pnl, recompute_row_percentages
from backend.api.cache import get_or_fetch, invalidate
from backend.api.helpers.ltp_patch import apply_ltp_patch, positions_policy
from backend.api.schemas import PositionsResponse, PositionRow, PositionsSummaryRow
from backend.brokers import broker_apis
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account, mask_column

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Closed-hours snapshot helpers
# ---------------------------------------------------------------------------

async def _is_all_markets_closed() -> bool:
    """Return True when every configured market segment is currently closed.

    Used as the fast-path guard at the top of the route: if the market is
    not open for any exchange, we serve the persisted daily_book snapshot
    instead of hitting the broker.
    """
    try:
        from backend.shared.helpers.date_time_utils import is_any_segment_open, timestamp_indian
        return not is_any_segment_open(timestamp_indian())
    except Exception:
        return False  # fail-open: assume market is open so live path runs


async def _positions_snapshot() -> Optional[PositionsResponse]:
    """Read the most-recent pre-today daily_book[kind='positions'] snapshot
    and reconstruct a PositionsResponse from it.

    Returns None when:
      - no snapshot exists yet (first ever deploy)
      - the DB query fails

    The response's `as_of` field carries the UTC ISO-8601 string of the
    most-recent captured_at so the frontend can surface "as of <time>".
    """
    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text
    from backend.shared.helpers.date_time_utils import timestamp_indian

    today_ist_midnight = timestamp_indian().replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    try:
        async with async_session() as session:
            result = await session.execute(_sql_text("""
                SELECT account, symbol, exchange, qty, avg_cost, ltp,
                       day_pnl, total_pnl, payload_json, captured_at
                FROM daily_book
                WHERE kind = 'positions' AND ltp IS NOT NULL
                  AND captured_at < :today_open
                ORDER BY captured_at DESC
                LIMIT 5000
            """), {"today_open": today_ist_midnight})
            raw_rows = result.all()
    except Exception as exc:
        logger.warning(f"positions snapshot query failed: {exc}")
        return None

    if not raw_rows:
        return None

    # captured_at of the most-recent snapshot row (first row after ORDER DESC)
    snap_captured_at: str = raw_rows[0][9].isoformat() if raw_rows[0][9] else ""

    # Build per-account sums for the summary
    pnl_by_account: dict[str, float] = {}
    dcv_by_account: dict[str, float] = {}
    prev_by_account: dict[str, float] = {}

    rows: list[PositionRow] = []
    import json as _json
    for (account, symbol, exchange, qty, avg_cost, ltp,
         day_pnl, total_pnl, payload_json, captured_at) in raw_rows:
        # Reconstruct a minimal PositionRow from the snapshot columns.
        # The payload_json holds the original broker row for forensics but
        # we rebuild from the stored aggregates to keep the struct correct.
        avg_cost_f = float(avg_cost) if avg_cost is not None else 0.0
        ltp_f      = float(ltp) if ltp is not None else 0.0
        total_pnl_f = float(total_pnl) if total_pnl is not None else 0.0
        day_pnl_f   = float(day_pnl) if day_pnl is not None else 0.0

        # close_price not stored separately; use avg_cost as a proxy so the
        # row is well-formed (the snapshot's pnl figures are authoritative).
        qty_i = int(qty) if qty is not None else 0
        # pnl_percentage: pnl / |avg × qty| × 100
        inv_val = abs(avg_cost_f * qty_i)
        pnl_pct = (total_pnl_f / inv_val * 100.0) if inv_val else 0.0
        # day_change_percentage: day_change_val / |close × qty| × 100
        # EOD LTP is the closest proxy for prior-session close in the snapshot.
        # Fall back to |avg × qty| when ltp is zero (opened-today rows).
        close_notional = abs(ltp_f * qty_i)
        day_pct = (day_pnl_f / close_notional * 100.0) if close_notional else (
            day_pnl_f / inv_val * 100.0 if inv_val else 0.0
        )
        row = PositionRow(
            account=str(account),
            tradingsymbol=str(symbol),
            exchange=str(exchange or ""),
            product="NRML",
            quantity=qty_i,
            average_price=avg_cost_f,
            close_price=ltp_f,  # EOD LTP serves as prior-session close
            last_price=ltp_f,
            pnl=total_pnl_f,
            pnl_percentage=pnl_pct,
            day_change_val=day_pnl_f,
            day_change_percentage=day_pct,
            last_price_stale=True,
        )
        rows.append(row)

        acct = str(account)
        pnl_by_account[acct] = pnl_by_account.get(acct, 0.0) + total_pnl_f
        dcv_by_account[acct]  = dcv_by_account.get(acct, 0.0) + day_pnl_f
        prev_by_account[acct] = prev_by_account.get(acct, 0.0) + abs(ltp_f * (int(qty) if qty else 0))

    from backend.api.schemas import PositionsSummaryRow
    summary: list[PositionsSummaryRow] = []
    total_pnl_sum = 0.0
    total_dcv_sum = 0.0
    for acct, pnl_sum in pnl_by_account.items():
        dcv_sum  = dcv_by_account.get(acct, 0.0)
        prev_sum = prev_by_account.get(acct, 0.0)
        pct = dcv_sum / prev_sum * 100.0 if prev_sum else 0.0
        summary.append(PositionsSummaryRow(
            account=acct,
            pnl=pnl_sum,
            day_change_val=dcv_sum,
            day_change_percentage=pct,
            day_prev_val=prev_sum,
        ))
        total_pnl_sum += pnl_sum
        total_dcv_sum += dcv_sum
    # TOTAL row
    total_prev = sum(prev_by_account.values())
    summary.append(PositionsSummaryRow(
        account="TOTAL",
        pnl=total_pnl_sum,
        day_change_val=total_dcv_sum,
        day_change_percentage=total_dcv_sum / total_prev * 100.0 if total_prev else 0.0,
        day_prev_val=total_prev,
    ))

    return PositionsResponse(
        rows=rows,
        summary=summary,
        refreshed_at=timestamp_display(),
        as_of=snap_captured_at,
    )

_ROW_COLS = [
    'account', 'tradingsymbol', 'exchange', 'product',
    'quantity', 'average_price', 'close_price', 'last_price',
    'pnl', 'pnl_percentage', 'unrealised', 'realised',
    'day_change', 'day_change_val', 'day_change_percentage',
    # Intraday split — used by Candidates grid to detect closed-then-
    # reopened activity and render the leg as two separate rows.
    'overnight_quantity', 'day_buy_quantity', 'day_sell_quantity',
    'day_buy_value', 'day_sell_value',
    # Staleness flag — True when last_price came from the last-known-good
    # cache rather than a live broker or ticker source.
    'last_price_stale',
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
    # Three sync broker_apis calls below — each holds the event loop
    # (~50ms each typical, up to 500-1000ms on cold UDS hits). Wrap
    # in asyncio.to_thread so concurrent SSE heartbeats + other
    # routes keep responding while the cache misses are in flight.
    # cache.py awaits this coroutine directly (not via to_thread)
    # since it's already async — we do the off-loop hop here.
    import asyncio as _asyncio
    per_acct = await _asyncio.to_thread(broker_apis.fetch_positions)
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
    await _asyncio.to_thread(broker_apis.backfill_market_data, raw)

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
    await _asyncio.to_thread(_enrich_position_greeks, rows)
    summary = [
        PositionsSummaryRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in summary_df.to_dicts()
    ]
    return PositionsResponse(rows=rows, summary=summary, refreshed_at=timestamp_display())


# Required columns for the decomposed (intraday-aware) day_change_val
# formula. When all five are present the formula uses overnight_qty ×
# (LTP − close) + buy/sell decomposition; otherwise falls back to
# (LTP − close) × qty (naive overnight-only path).
_INTRADAY_FIELDS = {
    'overnight_quantity', 'day_buy_quantity', 'day_sell_quantity',
    'day_buy_value', 'day_sell_value',
}


def _compute_day_change_val(raw: pd.DataFrame, sel: pd.Index) -> pd.Series:
    """Decomposed intraday day_change_val for the rows indexed by `sel`.

    Vectorised pandas wrapper over `pnl_math.decomposed_intraday_pnl`
    (the scalar canonical formula). Both the polars expression in
    `broker_apis._enrich_positions` and this pandas path call into the
    same module so the formula can never drift between routes.

    See `backend/api/algo/pnl_math.py` for the formula definition +
    rationale. Naive fallback `(LTP − close) × quantity` is used when
    the intraday columns aren't all present (Dhan / Groww adapters).
    """
    _ltp = pd.to_numeric(raw.loc[sel, 'last_price'], errors='coerce').fillna(0)
    _cls = pd.to_numeric(raw.loc[sel, 'close_price'], errors='coerce').fillna(0)
    if _INTRADAY_FIELDS.issubset(raw.columns):
        _oq = pd.to_numeric(raw.loc[sel, 'overnight_quantity'], errors='coerce').fillna(0)
        _bq = pd.to_numeric(raw.loc[sel, 'day_buy_quantity'],   errors='coerce').fillna(0)
        _sq = pd.to_numeric(raw.loc[sel, 'day_sell_quantity'],  errors='coerce').fillna(0)
        _bv = pd.to_numeric(raw.loc[sel, 'day_buy_value'],      errors='coerce').fillna(0)
        _sv = pd.to_numeric(raw.loc[sel, 'day_sell_value'],     errors='coerce').fillna(0)
        # decomposed_intraday_pnl(oq, ltp, cls, bq, bv, sv, sq) on Series — pandas
        # broadcasts each scalar op across the index, yielding the same Series shape.
        return decomposed_intraday_pnl(_oq, _ltp, _cls, _bq, _bv, _sv, _sq)
    _qty = pd.to_numeric(raw.loc[sel, 'quantity'], errors='coerce').fillna(0)
    return naive_day_pnl(_ltp, _cls, _qty)


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
    canonical decomposed formula so the value stays in sync with
    the new LTP.

    Bookkeeping (ticker pull + LKG fallback + stale flag) is owned
    by `helpers/ltp_patch.apply_ltp_patch`. This route only owns the
    decomposed pnl recompute (positions-specific).
    """
    res = apply_ltp_patch(raw, positions_policy)
    if res is None or not res.any_patched:
        return

    # Recompute day_change_val on patched rows — same decomposed
    # formula `broker_apis._enrich_positions` uses. Without this
    # the row's day_change_val would still hold Kite's stale value
    # (computed against the pre-patch LTP === close_price, i.e. zero).
    _sel = pd.Index(res.patched_idx)
    _ltp = pd.to_numeric(raw.loc[_sel, 'last_price'], errors='coerce').fillna(0)
    _cls = pd.to_numeric(raw.loc[_sel, 'close_price'], errors='coerce').fillna(0)
    _dcv_calc = _compute_day_change_val(raw, _sel)
    raw.loc[_sel, 'day_change_val'] = _dcv_calc.where(_ltp > 0, raw.loc[_sel, 'day_change_val'])
    raw.loc[_sel, 'day_change'] = _ltp - _cls
    # Additive pnl patch — preserves broker-side adjustments (fees,
    # corporate-action P&L, intraday tax) that the simple `(LTP − avg)
    # × qty + realised` reconstruction would silently drop. Math:
    #
    #   pnl_broker = (old_LTP − avg) × qty + realised + adjustments
    #   pnl_new    = (new_LTP − avg) × qty + realised + adjustments
    #              = pnl_broker + (new_LTP − old_LTP) × qty
    #
    # Without this patch, frontend's `_livePositionsPnl = Σ p.pnl + delta`
    # double-misses: pnl uses stale-REST-LTP and the live delta is ~0
    # (because patched_LTP ≈ SSE live_ltp). Operator: P showed ₹4.6L
    # vs broker's ₹6.27L on a day when illiquid MCX options were
    # stuck on yesterday's close in Kite's REST.
    if 'pnl' in raw.columns:
        _old_ltp_s = pd.Series(
            [res.patched_old_ltp[i] for i in res.patched_idx],
            index=_sel, dtype='float64',
        )
        _qty = pd.to_numeric(raw.loc[_sel, 'quantity'], errors='coerce').fillna(0)
        _pnl_delta = (_ltp - _old_ltp_s) * _qty
        _pnl_current = pd.to_numeric(raw.loc[_sel, 'pnl'], errors='coerce').fillna(0)
        raw.loc[_sel, 'pnl'] = (_pnl_current + _pnl_delta).where(
            _ltp > 0, raw.loc[_sel, 'pnl']
        )
    # Recompute day_change_percentage + pnl_percentage on patched rows.
    # day_change_val and pnl were updated above; without this step the
    # percentage columns still carry the pre-override broker values and
    # will disagree with the absolute columns by a visible margin.
    recompute_row_percentages(raw, _sel)
    n_stale = len(res.stale_idx)
    logger.info(
        f"positions: ltp-override patched {len(res.patched_idx)}/{len(raw)} rows "
        f"from KiteTicker"
        + (f" ({n_stale} via last-known-good cache)" if n_stale else "")
    )


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
    # (Uses module-level _INTRADAY_FIELDS via _compute_day_change_val.)
    _sel = pd.Index(patched_idx)
    _ltp = pd.to_numeric(raw.loc[_sel, 'last_price'], errors='coerce').fillna(0)
    _cls = pd.to_numeric(raw.loc[_sel, 'close_price'], errors='coerce').fillna(0)
    _dcv_calc = _compute_day_change_val(raw, _sel)
    raw.loc[_sel, 'day_change_val'] = _dcv_calc.where(_ltp > 0, raw.loc[_sel, 'day_change_val'])
    raw.loc[_sel, 'day_change'] = _ltp - _cls
    # Recompute day_change_percentage + pnl_percentage on patched rows.
    # close_price was replaced above and day_change_val just recomputed;
    # without this step the percentage columns lag the absolute columns
    # (same fix applied to _override_stale_ltp_from_ticker above).
    recompute_row_percentages(raw, _sel)
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
    from backend.brokers.registry import get_price_broker

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
            # ── Closed-hours fast-path ──────────────────────────────────────
            # When every configured market segment is closed, serve the most
            # recent daily_book[kind='positions'] snapshot instead of hitting
            # the broker.  This prevents spurious broker calls when the
            # frontend polls during overnight / weekend hours.  The `as_of`
            # field in the response lets the frontend show a staleness hint.
            # `?fresh=1` bypasses this guard so the operator can still force
            # a live fetch (e.g. after an AMO fill).
            if not fresh and await _is_all_markets_closed():
                snap = await _positions_snapshot()
                if snap is not None:
                    logger.info("positions: market closed — serving daily_book snapshot")
                    # Apply masking to the snapshot response for non-admin callers
                    # using the same copy-not-mutate pattern as the live path.
                    role = normalise_role(resolve_role_from_connection(request))
                    if role == "trader":
                        allowed, _ = await user_scope_for_connection(request)
                        allowed_set = {str(a).upper() for a in (allowed or [])}
                        import msgspec
                        snap = msgspec.structs.replace(
                            snap,
                            rows=[r for r in snap.rows
                                  if str(getattr(r, "account", "")).upper() in allowed_set],
                            summary=[s for s in snap.summary
                                     if str(getattr(s, "account", "")).upper() in allowed_set
                                     or str(getattr(s, "account", "")).upper() == "TOTAL"],
                        )
                    if not is_admin_request(request):
                        import msgspec
                        def _mask_snap(row):
                            return msgspec.structs.replace(row, account=mask_account(row.account))
                        snap = msgspec.structs.replace(
                            snap,
                            rows=[_mask_snap(r) for r in snap.rows],
                            summary=[_mask_snap(s) for s in snap.summary],
                        )
                    return snap

            # Demo + public flow share one path: real broker data via
            # the cached fetch, with accounts masked for non-admin
            # callers (existing behaviour from the public /performance
            # page). No synthetic data — demo visitors see real
            # positions with `ZG####` style masks.
            if fresh:
                invalidate("positions")
                # Also drop the raw-DataFrame cache so the refetch
                # below sees fresh broker state rather than the
                # cached list[pd.DataFrame].
                try:
                    from backend.brokers.broker_apis import _raw_cache_invalidate
                    _raw_cache_invalidate("positions")
                except Exception:
                    pass
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
