"""Positions endpoint — returns per-account rows and summary."""

import pandas as pd
import polars as pl
from litestar import Controller, Request, get
from litestar.exceptions import HTTPException
from typing import Optional, Literal

from backend.api.auth_guard import is_admin_request
from backend.api.rbac import (
    resolve_role_from_connection, user_scope_for_connection,
    normalise_role,
)
from backend.api.algo.pnl_math import decomposed_intraday_pnl, naive_day_pnl, recompute_row_percentages
from backend.api.cache import get_or_fetch, invalidate
from backend.api.helpers.ltp_patch import apply_ltp_patch, positions_policy
from backend.api.helpers.price_resolver import resolve_current_price
from backend.api.helpers.snapshot_gate import (
    closed_hours_or_broker, is_exchange_closed_now, latest_snapshot_ltp_map,
)
from backend.api.schemas import PositionsResponse, PositionRow, PositionsSummaryRow
from backend.brokers import broker_apis
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Closed-hours snapshot helpers
# ---------------------------------------------------------------------------

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

    try:
        async with async_session() as session:
            # Latest snapshot BATCH per account — pull every (account, symbol)
            # row written in the most-recent captured_at for that account.
            # Previous `DISTINCT ON (account, symbol) ORDER BY captured_at DESC`
            # carried over months-old rows for symbols closed long ago (e.g.
            # May CRUDEOIL positions resurfacing in today's NavStrip sum).
            # Batch-anchoring guarantees we only surface the broker's CURRENT
            # book. Zero-payload guard still applies inside the batch.
            result = await session.execute(_sql_text("""
                WITH latest_batch AS (
                    SELECT account, MAX(captured_at) AS max_at
                    FROM daily_book
                    WHERE kind = 'positions' AND ltp IS NOT NULL
                    GROUP BY account
                )
                SELECT db.account, db.symbol, db.exchange, db.qty, db.avg_cost,
                       db.ltp, db.day_pnl, db.total_pnl, db.payload_json,
                       db.captured_at
                FROM daily_book db
                JOIN latest_batch lb
                  ON db.account = lb.account AND db.captured_at = lb.max_at
                WHERE db.kind = 'positions'
                  AND db.ltp IS NOT NULL
                  AND NOT (db.ltp = 0 AND (db.total_pnl = 0 OR db.total_pnl IS NULL)
                           AND db.avg_cost IS NOT NULL AND db.avg_cost > 0)
                ORDER BY db.account, db.symbol
            """))
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
            # Snapshot represents session-end holdings — every held qty IS
            # overnight qty for the next session. Without this, the frontend
            # `baseDayPnlForPosition` new-position override (`oq === 0 &&
            # pnl !== 0 → day = pnl`) fires on every closed-hours row and
            # inflates Day P&L from the settled `day_change_val` (e.g.
            # ₹0.6) to the LIFETIME `pnl` (e.g. ₹14670) — cascading through
            # NavStrip P slot 1, /admin/derivatives Snapshot per-row + TOTAL,
            # Legs grid, and MarketPulse Positions grid.
            overnight_quantity=qty_i,
            last_price_stale=True,
            price_source="snapshot_settled",
            current_price=ltp_f,
            is_animating=False,
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
    # Account-level staleness — True when the entire row was substituted
    # from broker_apis' LKG frame cache because the account's circuit
    # breaker was OPEN. Preserves DH6847 rows across breaker-open cycles.
    'account_stale',
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


async def _overlay_snapshot_for_closed_exchanges(rows: list, *, kind: str) -> list:
    """Per-exchange close-snapshot overlay under the unified animation model
    (Jul 2026 refactor).

    Delegates the per-row (current_price, price_source, is_animating)
    decision to `price_resolver.resolve_current_price` so movers /
    watchlist / positions all share ONE branch matrix. The overlay layer
    itself only owns:
      1. per-exchange closed-check caching (avoid N×holidays lookups)
      2. one-shot snapshot-map lookup (via latest_snapshot_ltp_map)
      3. mapping resolver outputs back into the msgspec Struct row
      4. holdings-only recompute of cur_val when the snapshot LTP wins
         (positions' pnl is broker-owned and stays as-is)

    The `settled` flag we pass to the resolver is a presence heuristic:
    when the snapshot map has an LTP for this key we treat it as settled
    (the daily_book close_settled writer null-guards `ltp`, so a value in
    the map came from a close_settled cut). When the map has no key we
    pass settled=False and the resolver returns "snapshot_unsettled".

    Args:
        rows: list of PositionRow / HoldingRow structs.
        kind: 'positions' or 'holdings' — routes the snapshot query.
    Returns:
        new list (rows are msgspec Structs — replaced not mutated).
    """
    if not rows:
        return rows
    # Which exchanges are currently closed? Cache per call so we don't
    # probe holidays N × per-row times.
    exchange_closed: dict[str, bool] = {}
    def _closed(exch: str) -> bool:
        e = (exch or "").upper()
        if e not in exchange_closed:
            exchange_closed[e] = is_exchange_closed_now(e)
        return exchange_closed[e]

    import msgspec as _msc

    # Fast path — every row's exchange is currently open. Route through
    # the resolver for uniform tagging (single decision point).
    if not any(_closed(getattr(r, "exchange", "")) for r in rows):
        out: list = []
        for r in rows:
            live_ltp = float(getattr(r, "last_price", 0.0) or 0.0)
            price, source, animating = resolve_current_price(
                exchange_open=True, live_ltp=live_ltp,
            )
            out.append(_msc.structs.replace(
                r, price_source=source,
                current_price=price if price is not None else live_ltp,
                is_animating=animating,
            ))
        return out

    # Some rows are on closed exchanges — pull the snapshot map ONCE and
    # let the resolver decide per-row.
    snap_map = await latest_snapshot_ltp_map(kind)
    out = []
    for r in rows:
        exch = getattr(r, "exchange", "")
        broker_ltp = float(getattr(r, "last_price", 0.0) or 0.0)
        if not _closed(exch):
            price, source, animating = resolve_current_price(
                exchange_open=True, live_ltp=broker_ltp,
            )
            out.append(_msc.structs.replace(
                r, price_source=source,
                current_price=price if price is not None else broker_ltp,
                is_animating=animating,
            ))
            continue

        key = (getattr(r, "account", ""), getattr(r, "tradingsymbol", ""))
        snap_ltp = snap_map.get(key)
        has_snapshot = snap_ltp is not None and snap_ltp > 0

        price, source, animating = resolve_current_price(
            exchange_open=False,
            live_ltp=broker_ltp,
            snapshot_close=(float(snap_ltp) if has_snapshot else None),
            # broker LTP captured before close serves as the pre-settle
            # fallback — the resolver falls through to it via
            # snapshot_last_ltp when snapshot_close is missing.
            snapshot_last_ltp=broker_ltp,
            settled=has_snapshot,
        )
        replace_kwargs: dict = {
            "price_source": source,
            "current_price": price if price is not None else broker_ltp,
            "is_animating": animating,
        }
        # On settled path, overlay last_price with the snapshot value so
        # legacy consumers reading last_price see the frozen close_price.
        if has_snapshot and price is not None:
            replace_kwargs["last_price"] = float(price)
        out.append(_msc.structs.replace(r, **replace_kwargs))
    return out


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
    # Build account → "HH:MM IST" map for stale-substituted frames BEFORE
    # concat (which drops all DataFrame.attrs). Each LKG-substituted frame
    # carries attrs["stale_since"] (unix epoch of last successful fetch)
    # and at least one row with the account in the "account" column.
    _acct_stale_since: dict[str, str] = {}
    if per_acct:
        for _df in per_acct:
            _ss = _df.attrs.get("stale_since")
            if _ss and not _df.empty and "account" in _df.columns:
                _acct = str(_df["account"].iloc[0])
                try:
                    from zoneinfo import ZoneInfo
                    from datetime import datetime
                    _acct_stale_since[_acct] = datetime.fromtimestamp(
                        float(_ss), tz=ZoneInfo("Asia/Kolkata")
                    ).strftime("%H:%M IST")
                except Exception:
                    pass
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
    # Thread account_stale_since into stale rows so the frontend can
    # render "STALE @ HH:MM" next to the account name without a separate
    # endpoint. _acct_stale_since is built before concat (attrs survive).
    if _acct_stale_since:
        import msgspec as _msc
        rows = [
            _msc.structs.replace(r, account_stale_since=_acct_stale_since[r.account])
            if r.account_stale and r.account in _acct_stale_since
            else r
            for r in rows
        ]
    # Enrich option rows with position-Greeks (Δ × qty, Θ × qty) so the
    # /performance + /dashboard grids can surface them as columns without
    # round-tripping through /api/options/analytics per symbol.
    await _asyncio.to_thread(_enrich_position_greeks, rows)
    # Per-exchange close-snapshot overlay (Jul 2026 unified animation
    # model). If NSE has closed but MCX is still open, rows on
    # NSE/BSE/NFO/BFO exchanges get their LTP frozen at the daily_book
    # close_settled snapshot value and are tagged with `price_source`
    # ∈ {"snapshot_settled","snapshot_unsettled"} + `is_animating=False`.
    # Rows on still-open exchanges get `price_source="live"` +
    # `is_animating=True`. `current_price` alias populated on every row.
    # Runs after Greeks + LKG threading so the tag is authoritative for
    # what the frontend renders.
    rows = await _overlay_snapshot_for_closed_exchanges(rows, kind="positions")
    summary = [
        PositionsSummaryRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in summary_df.to_dicts()
    ]
    # Response-level stale_accounts list — every account whose rows were
    # substituted from broker_apis' LKG frame cache. NavStrip + TOTAL
    # rollups read this to surface "stale @ HH:MM" without walking rows.
    stale_accts = sorted({r.account for r in rows if r.account_stale})
    return PositionsResponse(
        rows=rows,
        summary=summary,
        refreshed_at=timestamp_display(),
        stale_accounts=stale_accts,
    )


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


async def _build_paper_positions_response() -> PositionsResponse:
    """Synthesize paper positions from filled AlgoOrder rows and mark-to-market
    them using the KiteTicker tick map + daily_book close_price snapshot.

    Returns a PositionsResponse whose rows all carry mode='paper'.
    """
    from backend.api.algo.paper import synthesize_paper_positions

    raw_dicts = await synthesize_paper_positions()
    if not raw_dicts:
        return PositionsResponse(rows=[], summary=[], refreshed_at=timestamp_display())

    # Convert to DataFrame for vectorised LTP + close patches.
    raw = pd.DataFrame(raw_dicts)

    # Patch last_price from KiteTicker (same path as live positions).
    # We want the freshest LTP; fall through to LKG cache if ticker
    # has no sample.  The policy matches positions_policy from ltp_patch.
    _override_stale_ltp_from_ticker(raw)

    # Patch close_price from daily_book (prior-session authoritative close).
    # Paper rows carry close_price=0.0 from the synthesis step; this
    # replaces them so day_change_val can be computed correctly.
    await _override_stale_close_from_snapshot(raw)

    # Recompute pnl = (last_price - average_price) × quantity.
    # Paper rows don't have broker-side unrealised; we compute from scratch.
    if 'last_price' in raw.columns and 'average_price' in raw.columns:
        _ltp = pd.to_numeric(raw['last_price'],    errors='coerce').fillna(0)
        _avg = pd.to_numeric(raw['average_price'], errors='coerce').fillna(0)
        _qty = pd.to_numeric(raw['quantity'],       errors='coerce').fillna(0)
        raw['pnl'] = (_ltp - _avg) * _qty
        raw['pnl_percentage'] = (
            raw['pnl'] / ((_avg * _qty).abs().replace(0, float('nan'))) * 100
        ).fillna(0)

    # Compute day_change_val using naive (LTP - close) × qty.
    # Paper positions don't carry overnight/buy/sell decomposition so
    # we always use the naive formula here — this is correct for paper
    # because every fill happened during the current session.
    if 'last_price' in raw.columns and 'close_price' in raw.columns:
        _ltp_s  = pd.to_numeric(raw['last_price'],  errors='coerce').fillna(0)
        _cls_s  = pd.to_numeric(raw['close_price'], errors='coerce').fillna(0)
        _qty_s  = pd.to_numeric(raw['quantity'],     errors='coerce').fillna(0)
        raw['day_change_val'] = naive_day_pnl(_ltp_s, _cls_s, _qty_s)
        raw['day_change'] = _ltp_s - _cls_s
        _prev_val = (_cls_s * _qty_s).abs()
        raw['day_change_percentage'] = (
            raw['day_change_val'] / _prev_val.replace(0, float('nan')) * 100
        ).fillna(0)

    numeric = raw.select_dtypes(include='number').columns
    raw[numeric] = raw[numeric].fillna(0)

    rows: list[PositionRow] = []
    for r in raw.to_dict(orient='records'):
        kwargs = {k: (r[k] if r[k] is not None else 0) for k in r}
        # Rename column-name 'last_price_stale' default
        kwargs.setdefault('last_price_stale', False)
        kwargs['mode'] = 'paper'
        # Only pass fields PositionRow knows about
        valid = {f for f in PositionRow.__struct_fields__}
        kwargs = {k: v for k, v in kwargs.items() if k in valid}
        rows.append(PositionRow(**kwargs))

    # Build summary — per-account aggregates.
    pnl_by_account: dict[str, float] = {}
    dcv_by_account: dict[str, float] = {}
    prev_by_account: dict[str, float] = {}
    for row in rows:
        acct = row.account
        pnl_by_account[acct]  = pnl_by_account.get(acct, 0.0) + row.pnl
        dcv_by_account[acct]  = dcv_by_account.get(acct, 0.0) + row.day_change_val
        prev_by_account[acct] = prev_by_account.get(acct, 0.0) + abs(row.close_price * row.quantity)

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
    total_prev = sum(prev_by_account.values())
    summary.append(PositionsSummaryRow(
        account="TOTAL",
        pnl=total_pnl_sum,
        day_change_val=total_dcv_sum,
        day_change_percentage=total_dcv_sum / total_prev * 100.0 if total_prev else 0.0,
        day_prev_val=total_prev,
    ))

    return PositionsResponse(rows=rows, summary=summary, refreshed_at=timestamp_display())


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
    from backend.brokers.registry import get_market_data_broker

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
        broker = get_market_data_broker()
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
        # SSOT: publish the underlying spot on the row itself. Frontend
        # NavStrip P.expiry, Snapshot Exp P&L, payoff overlay all consume
        # this instead of reconstructing it via multi-source client-side
        # fallbacks. Operator 2026-07-01: "use ssot."
        row.underlying_ltp = S
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
    async def get_positions(
        self,
        request: Request,
        fresh: bool = False,
        mode: Optional[str] = None,
        skip_ltp: bool = False,
    ) -> PositionsResponse:
        """Return positions.

        ?mode=paper — synthesized paper rows only (from filled AlgoOrder rows)
        ?mode=live  — broker-fetched rows only (current default behaviour)
        ?mode=both  — union of live + paper; each row carries a `mode` field
        ?skip_ltp=1 — force daily_book snapshot path even when a segment is
                     open (RefreshButton uses this during both-markets-closed
                     click so cash/margins refresh without a broker LTP fetch).
        (no param)  — same as 'live' for backward compatibility
        """
        # ── Paper-only fast path ─────────────────────────────────────────────
        if mode == "paper":
            try:
                resp = await _build_paper_positions_response()
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
                if not is_admin_request(request):
                    import msgspec
                    def _mask_paper_row(row):
                        return msgspec.structs.replace(row, account=mask_account(row.account))
                    resp = msgspec.structs.replace(
                        resp,
                        rows=[_mask_paper_row(r) for r in resp.rows],
                        summary=[_mask_paper_row(s) for s in resp.summary],
                    )
                return resp
            except Exception as e:
                logger.error(f"Paper positions API error: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        try:
            # ── Closed-hours gate via canonical helper ──────────────────────
            # closed_hours_or_broker is the single gate for "should I call the
            # broker or serve the daily_book snapshot?".  `?fresh=1` bypasses
            # the gate so the operator can force a live fetch (e.g. after an
            # AMO fill).
            #
            # _positions_snapshot() returns None when no DB row exists yet
            # (first-ever deploy). The snapshot_fn wrapper converts that to
            # an empty PositionsResponse so the helper can always return T.
            # The route then checks `as_of` to distinguish "real snapshot" from
            # "no snapshot exists" and falls through to the live path in the
            # latter case.

            async def _snapshot_fn() -> PositionsResponse:
                snap = await _positions_snapshot()
                if snap is None:
                    return PositionsResponse(rows=[], summary=[], refreshed_at=timestamp_display())
                return snap

            async def _broker_fn() -> PositionsResponse:
                if fresh:
                    invalidate("positions")
                    try:
                        from backend.brokers.broker_apis import (
                            _raw_cache_invalidate, dhan_next_poll_clear,
                            _use_conn_service,
                        )
                        _raw_cache_invalidate("positions")
                        # Reset the Dhan interval gate so ?fresh=1 bypasses
                        # cold/warm cadence and always hits the broker.
                        # Under conn-service the _dhan_next_poll dict lives in
                        # conn_service's process — proxy the reset over UDS.
                        if _use_conn_service():
                            from backend.brokers.client.api import dhan_poll_reset_remote
                            await dhan_poll_reset_remote()
                        else:
                            dhan_next_poll_clear()
                    except Exception:
                        pass
                return await get_or_fetch("positions", _fetch, ttl_seconds=_TTL)

            # ?skip_ltp=1 — RefreshButton's both-closed click. Runs the
            # normal broker path so metadata (qty / avg_cost / product /
            # intraday split) refreshes; the row-level overlay in
            # _broker_fn tags every closed-exchange row as
            # price_source='snapshot_*' and freezes its last_price to the
            # daily_book close_settled value. When both markets are
            # closed (the RefreshButton's trigger condition) every row's
            # exchange is closed → every LTP is snapshot-served
            # regardless of what the broker's REST payload said.
            # Funds/margins stay on their own broker paths in parallel.
            if skip_ltp:
                resp = await _broker_fn()
            elif not fresh:
                resp, source = await closed_hours_or_broker(
                    exchange="NSE",
                    snapshot_fn=_snapshot_fn,
                    broker_fn=_broker_fn,
                    fallback_to_snapshot_on_broker_error=True,
                    route_key="positions",
                )
                # When market is closed and the DB has a genuine snapshot
                # (as_of is set), return it with masking applied.
                # stale-live means market is open but broker transient-failed;
                # we serve the last-known live payload — no snapshot branch.
                if source not in ("live", "stale-live") and getattr(resp, "as_of", None):
                    logger.info(
                        f"positions: market closed ({source}) — serving daily_book snapshot"
                    )
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
                    if not is_admin_request(request):
                        import msgspec
                        def _mask_snap(row):
                            return msgspec.structs.replace(row, account=mask_account(row.account))
                        resp = msgspec.structs.replace(
                            resp,
                            rows=[_mask_snap(r) for r in resp.rows],
                            summary=[_mask_snap(s) for s in resp.summary],
                        )
                    return resp
                # Market is open (or stale-live), or no snapshot exists yet —
                # continue to live path (resp already holds the broker response
                # from _broker_fn or the anti-flicker stale-live copy).
                if source in ("live", "stale-live"):
                    pass  # resp is already the broker/stale-live response
                else:
                    # Market closed but no snapshot (first deploy) — fall back live.
                    resp = await _broker_fn()
            else:
                # ?fresh=1 — bypass closed-hours gate entirely
                resp = await _broker_fn()
            # ── mode=both — merge paper rows into the live response ─────────
            # Paper rows tagged mode='paper'; live rows default mode='live'.
            # Summary is recomputed over the combined set so totals are correct.
            if mode == "both":
                paper_resp = await _build_paper_positions_response()
                if paper_resp.rows:
                    import msgspec as _msgspec
                    # Tag live rows explicitly so frontend can distinguish them.
                    live_rows_tagged = [
                        _msgspec.structs.replace(r, mode="live") for r in resp.rows
                    ]
                    merged_rows = live_rows_tagged + list(paper_resp.rows)
                    # Recompute summary over merged set.
                    _pnl_by_acct: dict[str, float] = {}
                    _dcv_by_acct: dict[str, float] = {}
                    _prev_by_acct: dict[str, float] = {}
                    for _r in merged_rows:
                        _a = _r.account
                        _pnl_by_acct[_a]  = _pnl_by_acct.get(_a, 0.0) + _r.pnl
                        _dcv_by_acct[_a]  = _dcv_by_acct.get(_a, 0.0) + _r.day_change_val
                        _prev_by_acct[_a] = _prev_by_acct.get(_a, 0.0) + abs(_r.close_price * _r.quantity)
                    _merged_summary: list[PositionsSummaryRow] = []
                    _t_pnl = 0.0
                    _t_dcv = 0.0
                    for _a, _p in _pnl_by_acct.items():
                        _d = _dcv_by_acct.get(_a, 0.0)
                        _pv = _prev_by_acct.get(_a, 0.0)
                        _pct = _d / _pv * 100.0 if _pv else 0.0
                        _merged_summary.append(PositionsSummaryRow(
                            account=_a, pnl=_p, day_change_val=_d,
                            day_change_percentage=_pct, day_prev_val=_pv,
                        ))
                        _t_pnl += _p
                        _t_dcv += _d
                    _t_pv = sum(_prev_by_acct.values())
                    _merged_summary.append(PositionsSummaryRow(
                        account="TOTAL", pnl=_t_pnl, day_change_val=_t_dcv,
                        day_change_percentage=_t_dcv / _t_pv * 100.0 if _t_pv else 0.0,
                        day_prev_val=_t_pv,
                    ))
                    resp = _msgspec.structs.replace(
                        resp, rows=merged_rows, summary=_merged_summary,
                    )

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
