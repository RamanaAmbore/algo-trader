"""Holdings endpoint — returns per-account rows and summary."""

import asyncio
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import msgspec
import pandas as pd
import polars as pl
from litestar import Controller, Request, get
from litestar.exceptions import HTTPException

from backend.api.auth_guard import is_admin_request
from backend.api.rbac import (
    resolve_role_from_connection, user_scope_for_connection, normalise_role,
)
from backend.api.algo.pnl_math import recompute_row_percentages
from backend.api.cache import get_or_fetch, invalidate
from backend.api.helpers.ltp_patch import apply_ltp_patch, holdings_policy
from backend.api.helpers.price_resolver import resolve_current_price
from backend.api.helpers.snapshot_gate import (
    closed_hours_or_broker, is_exchange_closed_now, latest_snapshot_ltp_map,
)
from backend.api.routes.positions_helpers import _is_broker_outage
from backend.api.schemas import HoldingsResponse, HoldingRow, HoldingsSummaryRow
from backend.brokers import broker_apis
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account

logger = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Closed-hours snapshot helpers
# ---------------------------------------------------------------------------

_HOLDINGS_SNAPSHOT_SQL = """
    WITH latest_batch AS (
        SELECT account, MAX(captured_at) AS max_at
        FROM daily_book
        WHERE kind = 'holdings' AND ltp IS NOT NULL
        GROUP BY account
    ),
    prev_batch AS (
        SELECT DISTINCT ON (db.account, db.symbol)
               db.account, db.symbol, db.ltp AS prev_ltp
        FROM daily_book db
        JOIN latest_batch lb
          ON db.account = lb.account
        WHERE db.kind = 'holdings'
          AND db.ltp IS NOT NULL AND db.ltp > 0
          AND db.captured_at < lb.max_at
          AND db.captured_at >= lb.max_at - INTERVAL '7 days'
        ORDER BY db.account, db.symbol, db.captured_at DESC
    )
    SELECT db.account, db.symbol, db.exchange, db.qty, db.avg_cost,
           db.ltp, db.previous_close, db.day_pnl, db.total_pnl, db.captured_at,
           pb.prev_ltp
    FROM daily_book db
    JOIN latest_batch lb
      ON db.account = lb.account AND db.captured_at = lb.max_at
    LEFT JOIN prev_batch pb
      ON db.account = pb.account AND db.symbol = pb.symbol
    WHERE db.kind = 'holdings'
      AND db.ltp IS NOT NULL
      AND NOT (db.ltp = 0 AND (db.total_pnl = 0 OR db.total_pnl IS NULL)
               AND db.avg_cost IS NOT NULL AND db.avg_cost > 0)
    ORDER BY db.account, db.symbol
"""


async def _query_holdings_snapshot_rows():
    """Latest snapshot BATCH per account — pull every (account, symbol)
    row written in the most-recent captured_at for that account.
    The prior `DISTINCT ON (account, symbol) ORDER BY captured_at DESC`
    pattern picked the newest non-zero row per symbol regardless of
    date. For symbols closed weeks ago, that's a months-old row
    whose day_pnl was real on its capture date but is summed today
    as nonsense (NavStrip showed ₹14k vs the real ₹30k holdings P∆).
    Batch-anchoring guarantees we ONLY surface the broker's current
    book, never carry-over from prior sessions. Zero-payload guard
    still applies inside the batch in case the writer slipped one
    through.
    """
    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text

    try:
        async with async_session() as session:
            result = await session.execute(_sql_text(_HOLDINGS_SNAPSHOT_SQL))
            return result.all()
    except Exception as exc:
        logger.warning(f"holdings snapshot query failed: {exc}")
        return None


def _build_holding_row_from_snapshot(raw_row) -> tuple[HoldingRow, float, float, float, float]:
    """Convert one raw snapshot tuple into a HoldingRow + the four
    per-account sums (inv, cur, total_pnl, day_pnl) that the caller
    aggregates into HoldingsSummaryRow.
    """
    (account, symbol, exchange, qty, avg_cost, ltp, previous_close,
     day_pnl, total_pnl, _captured_at, prev_ltp) = raw_row

    avg_cost_f       = float(avg_cost)       if avg_cost       is not None else 0.0
    ltp_f            = float(ltp)            if ltp             is not None else 0.0
    previous_close_f = float(previous_close) if previous_close is not None else 0.0
    total_pnl_f      = float(total_pnl)      if total_pnl       is not None else 0.0
    day_pnl_f        = float(day_pnl)        if day_pnl         is not None else 0.0
    qty_i            = int(qty)              if qty             is not None else 0
    inv_val          = avg_cost_f * qty_i
    cur_val          = ltp_f      * qty_i

    # pnl_percentage: pnl / |avg × qty| × 100
    # (inv_val = avg_cost_f × qty_i, so use that directly)
    pnl_pct = (total_pnl_f / inv_val * 100.0) if inv_val else 0.0
    # `previous_close` is the rolling-shift of the prior daily_book.ltp set at
    # each UPSERT — it holds the prior-session settlement price (pre-08:00).
    # `prev_ltp` is the most-recent batch LTP and converges toward the current
    # LTP during a session, which would make day_change ≈ 0. Use
    # `previous_close` as the primary reference and fall back to `prev_ltp`
    # only when `previous_close` is absent or zero. Mirrors positions_helpers.py.
    prev_ltp_f = float(prev_ltp) if prev_ltp is not None and float(prev_ltp) > 0 else None
    if previous_close_f > 0:
        day_change_val = (ltp_f - previous_close_f) * qty_i
    elif prev_ltp_f is not None:
        day_change_val = (ltp_f - prev_ltp_f) * qty_i
    else:
        day_change_val = day_pnl_f
    # day_change_percentage: day_change_val / |previous_close × qty| × 100
    # Use yesterday's close price as the denominator (NOT LTP, which would
    # understate the move). Fallback to avg_cost when previous_close is
    # missing/zero (same-day buys / cold-boot).
    day_change_percentage = (
        (day_change_val / (previous_close_f * abs(qty_i)) * 100)
        if previous_close_f > 0 and qty_i != 0
        else 0.0
    )
    # Use yesterday's close as close_price (same pattern as
    # build_snapshot_position_row in positions_helpers.py lines 232-236).
    # Fallback to ltp_f when previous_close is zero/missing (same-day
    # buys / cold-boot where no prior-session close exists).
    close_price_f = previous_close_f if previous_close_f > 0 else ltp_f
    row = HoldingRow(
        account=str(account),
        tradingsymbol=str(symbol),
        exchange=str(exchange or ""),
        quantity=qty_i,
        opening_quantity=qty_i,
        average_price=avg_cost_f,
        close_price=close_price_f,
        last_price=ltp_f,
        inv_val=inv_val,
        cur_val=cur_val,
        pnl=total_pnl_f,
        pnl_percentage=pnl_pct,
        day_change_val=day_change_val,
        day_change_percentage=day_change_percentage,
        last_price_stale=True,
        price_source="snapshot_settled",
        current_price=ltp_f,
        is_animating=False,
        previous_close=previous_close_f,
        pnl_per_share=total_pnl_f / qty_i if qty_i != 0 else 0.0,
    )
    return row, inv_val, cur_val, total_pnl_f, day_change_val


def _build_holdings_summary(
    inv_by_account: dict[str, float],
    cur_by_account: dict[str, float],
    pnl_by_account: dict[str, float],
    dcv_by_account: dict[str, float],
) -> list[HoldingsSummaryRow]:
    """Per-account HoldingsSummaryRow list + a TOTAL row."""
    summary: list[HoldingsSummaryRow] = []
    total_inv = total_cur = total_pnl_s = total_dcv = 0.0
    for acct in pnl_by_account:
        inv  = inv_by_account.get(acct, 0.0)
        cur  = cur_by_account.get(acct, 0.0)
        pnl  = pnl_by_account.get(acct, 0.0)
        dcv  = dcv_by_account.get(acct, 0.0)
        prev = cur - dcv
        summary.append(HoldingsSummaryRow(
            account=acct,
            inv_val=inv,
            cur_val=cur,
            pnl=pnl,
            pnl_percentage=pnl / inv * 100.0 if inv else 0.0,
            day_change_val=dcv,
            day_change_percentage=dcv / prev * 100.0 if prev else 0.0,
        ))
        total_inv += inv; total_cur += cur
        total_pnl_s += pnl; total_dcv += dcv
    total_prev = total_cur - total_dcv
    summary.append(HoldingsSummaryRow(
        account="TOTAL",
        inv_val=total_inv,
        cur_val=total_cur,
        pnl=total_pnl_s,
        pnl_percentage=total_pnl_s / total_inv * 100.0 if total_inv else 0.0,
        day_change_val=total_dcv,
        day_change_percentage=total_dcv / total_prev * 100.0 if total_prev else 0.0,
    ))
    return summary


async def _holdings_snapshot() -> Optional[HoldingsResponse]:
    """Read the most-recent pre-today daily_book[kind='holdings'] snapshot
    and reconstruct a HoldingsResponse from it.

    Returns None when no snapshot exists or the DB query fails.

    After building the initial HoldingRow list, `_override_stale_close_for_holdings`
    is applied via a minimal DataFrame so that `previous_close` (and `close_price`)
    reflect the actual settlement LTP from daily_book rather than the potentially
    stale value stored in the snapshot row's `previous_close` column.  This ensures
    the snapshot path and the broker path use the same canonical prior-session
    reference price.
    """
    raw_rows = await _query_holdings_snapshot_rows()
    if not raw_rows:
        return None

    snap_captured_at: str = raw_rows[0][9].isoformat() if raw_rows[0][9] else ""

    rows: list[HoldingRow] = []
    for raw_row in raw_rows:
        row, _, _, _, _ = _build_holding_row_from_snapshot(raw_row)
        rows.append(row)

    # Apply the same close-price override that runs on the broker path so
    # previous_close and day_change_val in the snapshot path reflect the
    # real prior-session settlement LTP, not the potentially drifted value
    # stored in daily_book.previous_close (Kite BHAV-copy).
    raw_df = pd.DataFrame([
        {
            "account":        r.account,
            "tradingsymbol":  r.tradingsymbol,
            "close_price":    r.close_price,
            "last_price":     r.last_price,
            "quantity":       r.quantity,
            "day_change_val": r.day_change_val,
            "day_change":     r.day_change_val / r.quantity if r.quantity else 0.0,
            "previous_close": r.previous_close,
        }
        for r in rows
    ])
    if not raw_df.empty:
        await _override_stale_close_for_holdings(raw_df)
        # Rebuild rows with the patched previous_close + day_change_val values.
        import msgspec as _msc
        patched_rows: list[HoldingRow] = []
        for idx, row in enumerate(rows):
            new_prev_close = float(raw_df.at[idx, "previous_close"])
            new_dcv        = float(raw_df.at[idx, "day_change_val"])
            new_close      = float(raw_df.at[idx, "close_price"])
            # Recompute day_change_percentage from the patched values.
            qty = row.quantity
            prev_val = abs(new_prev_close * qty) if new_prev_close > 0 and qty else 0.0
            new_dcp = (new_dcv / prev_val * 100.0) if prev_val else row.day_change_percentage
            patched_rows.append(_msc.structs.replace(
                row,
                previous_close=new_prev_close,
                close_price=new_close,
                day_change_val=new_dcv,
                day_change_percentage=new_dcp,
            ))
        rows = patched_rows

    inv_by_account: dict[str, float] = {}
    cur_by_account: dict[str, float] = {}
    pnl_by_account: dict[str, float] = {}
    dcv_by_account: dict[str, float] = {}
    for row in rows:
        acct = row.account
        inv_by_account[acct] = inv_by_account.get(acct, 0.0) + row.inv_val
        cur_by_account[acct] = cur_by_account.get(acct, 0.0) + row.cur_val
        pnl_by_account[acct] = pnl_by_account.get(acct, 0.0) + row.pnl
        dcv_by_account[acct] = dcv_by_account.get(acct, 0.0) + row.day_change_val

    summary = _build_holdings_summary(
        inv_by_account, cur_by_account, pnl_by_account, dcv_by_account
    )

    return HoldingsResponse(
        rows=rows,
        summary=summary,
        refreshed_at=timestamp_display(),
        as_of=snap_captured_at,
    )


_ROW_COLS = [
    'account', 'tradingsymbol', 'exchange', 'quantity', 'opening_quantity',
    'average_price', 'close_price', 'last_price', 'inv_val', 'cur_val',
    'pnl', 'pnl_percentage', 'day_change', 'day_change_val', 'day_change_percentage',
    # Staleness flag — True when last_price came from the last-known-good
    # cache rather than a live broker or ticker source.
    'last_price_stale',
    # Account-level staleness — True when the entire row was substituted
    # from broker_apis' LKG frame cache because the account's circuit
    # breaker was OPEN. Preserves DH6847 rows across breaker-open cycles.
    'account_stale',
    # Prior-session settlement LTP from daily_book (direct, not COALESCE).
    # Exposed to frontend so it can compute `(ltp − previous_close) × qty`
    # independently of whether Kite's `close_price` has drifted.
    'previous_close',
    # P&L per share = total pnl / quantity. Zero when quantity is 0.
    'pnl_per_share',
]

_TTL = 30  # seconds — background task invalidates on each refresh


def _override_stale_ltp_from_ticker(raw: pd.DataFrame) -> None:
    """Patch `last_price` from the live KiteTicker tick_map for any
    holdings row whose last_price is still zero or missing after
    `backfill_market_data`. Holdings brokers (Dhan, Groww) sometimes
    return zero LTP for equity symbols; the ticker always has the
    freshest streamed value for subscribed holdings.

    Only patches rows where the broker / backfill delivered a zero or
    missing LTP — never overwrites a valid non-zero broker value (same
    guard as positions route). Recomputes `day_change_val` + `day_change`
    on patched rows using (LTP - close) × opening_qty so the Day P&L
    column reflects the fresh tick immediately.

    Bookkeeping (ticker pull + LKG fallback + stale flag) is owned by
    `helpers/ltp_patch.apply_ltp_patch`. This route only owns the
    naive (LTP - close) × opening_qty recompute (no decomposed
    intraday — holdings don't have buy/sell decomposition columns).
    """
    res = apply_ltp_patch(raw, holdings_policy)
    if res is None or not res.any_patched:
        return

    _sel = pd.Index(res.patched_idx)
    # Use `quantity` (remaining shares) for day P&L recompute — partial-sold
    # holdings must not include the already-sold portion.
    _qty_col = 'quantity' if 'quantity' in raw.columns else 'opening_quantity'
    _ltp_p = pd.to_numeric(raw.loc[_sel, 'last_price'], errors='coerce').fillna(0)
    _cls_p = pd.to_numeric(raw.loc[_sel, 'close_price'], errors='coerce').fillna(0) \
             if 'close_price' in raw.columns else pd.Series(0.0, index=_sel)
    _qty_p = pd.to_numeric(raw.loc[_sel, _qty_col], errors='coerce').fillna(0)
    _dcv = (_ltp_p - _cls_p) * _qty_p
    if 'day_change_val' in raw.columns:
        raw.loc[_sel, 'day_change_val'] = _dcv.where(_ltp_p > 0,
                                                      raw.loc[_sel, 'day_change_val'])
    if 'day_change' in raw.columns:
        raw.loc[_sel, 'day_change'] = _ltp_p - _cls_p
    # Recompute pnl + cur_val on patched rows so the API response is internally
    # consistent: last_price, pnl, and cur_val all reflect the same LTP.
    if 'average_price' in raw.columns and 'pnl' in raw.columns:
        _avg_p = pd.to_numeric(raw.loc[_sel, 'average_price'], errors='coerce').fillna(0)
        _pnl_p = (_ltp_p - _avg_p) * _qty_p
        raw.loc[_sel, 'pnl'] = _pnl_p.where(_ltp_p > 0, raw.loc[_sel, 'pnl'])
        if 'pnl_per_share' in raw.columns:
            raw.loc[_sel, 'pnl_per_share'] = (
                _pnl_p / _qty_p.replace(0, float('nan'))
            ).fillna(0).where(_ltp_p > 0, raw.loc[_sel, 'pnl_per_share'])
        if 'inv_val' in raw.columns and 'cur_val' in raw.columns:
            _inv_p2 = pd.to_numeric(raw.loc[_sel, 'inv_val'], errors='coerce').fillna(0)
            raw.loc[_sel, 'cur_val'] = (_inv_p2 + raw.loc[_sel, 'pnl']).where(
                _ltp_p > 0, raw.loc[_sel, 'cur_val']
            )
    # Recompute day_change_percentage + pnl_percentage on patched rows.
    # day_change_val and pnl were updated by backfill_market_data for
    # holdings rows that had last_price patched — but pnl and cur_val
    # may also have been patched (see backfill_market_data inv_val/cur_val
    # chain). The percentage columns lag without this step.
    recompute_row_percentages(raw, _sel)
    n_stale = len(res.stale_idx)
    logger.info(
        f"holdings: ltp-override patched {len(res.patched_idx)}/{len(raw)} "
        f"zero-LTP rows from KiteTicker"
        + (f" ({n_stale} via last-known-good cache)" if n_stale else "")
    )


async def _override_stale_close_for_holdings(raw: pd.DataFrame) -> None:
    """Replace `close_price` with the frozen prior-session reference price
    per (account, tradingsymbol) for holdings rows, and write `previous_close`
    for every row (regardless of whether `close_price` is patched).

    The reference price is `daily_book.ltp` from the most-recent pre-08:00 IST
    snapshot. `daily_book.ltp` is the actual settlement LTP captured at session
    end and is the canonical prior-session reference price.

    `previous_close` (Kite's BHAV-copy field) is deliberately NOT used here.
    Kite populates `previous_close` from the BHAV-copy API which lags until
    ~08:00 IST the next trading day. Using it via COALESCE caused the epsilon
    check to always pass (stale value equals stale close_price), so `close_price`
    was never patched. Using `daily_book.ltp` directly fixes this.

    `previous_close` is written to rows that have a matching daily_book
    snapshot entry.  Rows with no snapshot entry receive 0.0 (initialised
    after the query succeeds).  On DB failure the column is left absent so
    the broker's own `close_price` / `previous_close` field (set by
    `_enrich_holdings`) is used instead of a hard zero.

    `close_price` is synced to ref_close unconditionally for every row that
    has a snapshot entry — no epsilon guard (the old epsilon guard caused the
    denominator and numerator to reference different prices when Kite's
    BHAV-copy value was close to the snapshot).

    Runs AFTER backfill_market_data and AFTER `_enrich_holdings` (which runs
    inside `broker_apis.fetch_holdings` per-account). Because `_enrich_holdings`
    has already computed `day_change_val` against the stale `close_price`, this
    function recomputes `day_change_val` on close_price-patched rows after
    updating `close_price`.
    """
    if raw.empty or 'tradingsymbol' not in raw.columns or 'account' not in raw.columns:
        return

    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text
    from backend.api.helpers.exchange_clock import settlement_cutoff_for

    # Cutoff = last passed 08:00 IST boundary (the prev_close invariant).
    # Use NON-MCX gate; MCX gate has the same reset time (08:00 IST) so one
    # cutoff covers all exchanges.  Both daily_book snapshots (NSE ~15:45 and
    # MCX ~00:15) fall before the 08:00 boundary and are included by this query.
    today_ist_cutoff = await settlement_cutoff_for("NON-MCX")

    # ref_close: ltp directly — canonical prior-session settlement LTP.
    # previous_close (Kite BHAV-copy) is stale during the overnight window
    # and must not be used here (see docstring).
    snapshot_map: dict[tuple[str, str], float] = {}
    try:
        async with async_session() as session:
            result = await session.execute(_sql_text("""
                SELECT DISTINCT ON (account, symbol)
                       account, symbol,
                       ltp AS ref_close
                FROM daily_book
                WHERE kind = 'holdings'
                  AND ltp IS NOT NULL AND ltp > 0
                  AND captured_at < :eod_cutoff
                ORDER BY account, symbol, captured_at DESC
            """), {"eod_cutoff": today_ist_cutoff})
            for account, symbol, ref_close in result.all():
                v = float(ref_close) if ref_close is not None else 0.0
                if v > 0:
                    snapshot_map[(str(account), str(symbol))] = v
    except Exception as e:
        logger.warning(f"holdings daily_book close-override query failed: {e}")
        return

    if not snapshot_map:
        return

    # Ensure previous_close column exists — initialised to 0.0 for rows that
    # have no matching snapshot entry.  Placed here (after the query succeeds)
    # so that a DB failure returns early above, leaving the column absent and
    # letting the broker's own value be used by downstream consumers.
    if 'previous_close' not in raw.columns:
        raw['previous_close'] = 0.0

    # Write previous_close for ALL rows that have a snapshot entry (not just
    # rows where close_price gets patched). Rows with no snapshot entry keep 0.0.
    patched_indices: list = []
    for idx in raw.index:
        key = (str(raw.at[idx, 'account']), str(raw.at[idx, 'tradingsymbol']))
        ref_close = snapshot_map.get(key)
        if ref_close is None:
            continue
        # Always write previous_close unconditionally.
        raw.at[idx, 'previous_close'] = ref_close
        # Always sync close_price to ref_close — unconditional, no epsilon guard.
        # This ensures _recompute_day_change_pct (which uses close_price as the
        # percentage denominator) is always consistent with day_change_val
        # (which uses previous_close = ref_close).  The old epsilon guard
        # (abs(ref_close - current_close) <= 0.005) silently skipped rows where
        # Kite's BHAV-copy value was close to the snapshot — causing the
        # denominator and numerator to reference different prices.
        raw.at[idx, 'close_price'] = ref_close
        patched_indices.append(idx)

    if patched_indices:
        logger.info(
            f"holdings: close-override patched {len(patched_indices)}/{len(raw)} rows from daily_book"
        )

    # Recompute day_change_val for ALL rows where previous_close > 0 — not
    # just the close_price-patched rows.  Rows where close_price already
    # matched the snapshot (epsilon ≤ 0.005) still need a fresh
    # (ltp − previous_close) × qty because _enrich_holdings ran against the
    # stale Kite close_price before this function was called.
    #
    # For Dhan/Groww rows: backfill_market_data sets close_price ≈ ohlc.close
    # (today's settlement ≈ ltp), so (ltp - close_price) × qty ≈ 0 and the
    # row falls through the epsilon guard unchanged. Recomputing against
    # previous_close gives the correct (ltp - prev_close) × qty value.
    #
    # close_price patch log is kept separate (above) — it tracks the number of
    # rows where the broker value diverged, which is the metric that matters for
    # the Kite BHAV-copy lag diagnostic.
    pc_series = pd.to_numeric(raw['previous_close'], errors='coerce').fillna(0)
    all_pc_indices = raw.index[pc_series > 0].tolist()
    if all_pc_indices and 'day_change_val' in raw.columns:
        _ltp = pd.to_numeric(raw.loc[all_pc_indices, 'last_price'], errors='coerce').fillna(0)
        _cls = pc_series.loc[all_pc_indices]
        _qty = pd.to_numeric(raw.loc[all_pc_indices, 'quantity'], errors='coerce').fillna(0)
        raw.loc[all_pc_indices, 'day_change_val'] = (_ltp - _cls) * _qty
        # Also update per-share day_change so percentage columns are consistent.
        _nonzero_qty = _qty.replace(0, float('nan'))
        raw.loc[all_pc_indices, 'day_change'] = (
            raw.loc[all_pc_indices, 'day_change_val'] / _nonzero_qty
        ).fillna(0)
    recompute_row_percentages(raw, pd.Index(all_pc_indices))


def _hold_tag_open_row(r, _msc) -> object:
    """Tag a single holding row when its exchange is open."""
    live_ltp = float(getattr(r, "last_price", 0.0) or 0.0)
    price, source, animating = resolve_current_price(exchange_open=True, live_ltp=live_ltp)
    return _msc.structs.replace(
        r, price_source=source,
        current_price=price if price is not None else live_ltp,
        is_animating=animating,
    )


def _hold_tag_closed_row(r, snap_ltp, _msc) -> object:
    """Tag a single holding row when its exchange is closed."""
    broker_ltp = float(getattr(r, "last_price", 0.0) or 0.0)
    has_snapshot = snap_ltp is not None and snap_ltp > 0
    price, source, animating = resolve_current_price(
        exchange_open=False,
        live_ltp=broker_ltp,
        snapshot_close=(float(snap_ltp) if has_snapshot else None),
        snapshot_last_ltp=broker_ltp,
        settled=has_snapshot,
    )
    replace_kwargs: dict = {
        "price_source": source,
        "current_price": price if price is not None else broker_ltp,
        "is_animating": animating,
    }
    # On settled path — overlay last_price + recompute cur_val + day_change_val.
    if has_snapshot and price is not None:
        qty = int(getattr(r, "quantity", 0) or getattr(r, "opening_quantity", 0))
        snap_price = float(price)
        close_px = float(getattr(r, "close_price", 0.0) or 0.0)
        replace_kwargs["last_price"] = snap_price
        replace_kwargs["cur_val"] = snap_price * qty
        if close_px > 0 and qty != 0:
            dcv = (snap_price - close_px) * qty
            replace_kwargs["day_change_val"] = dcv
            replace_kwargs["day_change"] = dcv / qty
            replace_kwargs["day_change_percentage"] = dcv / abs(close_px * qty) * 100
    return _msc.structs.replace(r, **replace_kwargs)


async def _overlay_snapshot_for_closed_exchanges(rows: list) -> list:
    """Per-exchange close-snapshot overlay for holdings rows under the
    unified animation model (Jul 2026 refactor). Delegates the per-row
    triad decision to `price_resolver.resolve_current_price` so
    positions + holdings + movers all share ONE branch matrix.

    Holdings-specific concern: cur_val is derived from ltp × qty
    (unlike positions where broker owns pnl). When the snapshot LTP
    wins, we recompute cur_val to match — otherwise the frontend TOTAL
    row rolls up stale broker cur_val against fresh snapshot LTP.
    """
    if not rows:
        return rows
    exchange_closed: dict[str, bool] = {}
    def _closed(exch: str) -> bool:
        e = (exch or "").upper()
        if e not in exchange_closed:
            exchange_closed[e] = is_exchange_closed_now(e)
        return exchange_closed[e]

    import msgspec as _msc

    # Fast path — every exchange currently open.
    if not any(_closed(getattr(r, "exchange", "")) for r in rows):
        return [_hold_tag_open_row(r, _msc) for r in rows]

    snap_map = await latest_snapshot_ltp_map("holdings")
    out = []
    for r in rows:
        exch = getattr(r, "exchange", "")
        if not _closed(exch):
            out.append(_hold_tag_open_row(r, _msc))
        else:
            key = (getattr(r, "account", ""), getattr(r, "tradingsymbol", ""))
            out.append(_hold_tag_closed_row(r, snap_map.get(key), _msc))
    return out


def _is_full_outage(per_acct: list) -> bool:
    """Every per-account frame carries fetch_failed = a true outage.

    Empty per_acct or ANY successful frame → legitimate 'no holdings' state
    (operator who hasn't taken delivery, or holds only F&O).
    """
    return bool(per_acct) and all(
        df.attrs.get("fetch_failed", False) for df in per_acct
    )


def _stale_since_map(per_acct: list) -> dict[str, str]:
    """Build {account → 'HH:MM IST'} for stale-substituted frames BEFORE
    concat (which drops all DataFrame.attrs)."""
    out: dict[str, str] = {}
    if not per_acct:
        return out
    for _df in per_acct:
        _ss = _df.attrs.get("stale_since")
        if not (_ss and not _df.empty and "account" in _df.columns):
            continue
        _acct = str(_df["account"].iloc[0])
        try:
            out[_acct] = datetime.fromtimestamp(
                float(_ss), tz=_IST
            ).strftime("%H:%M IST")
        except Exception:
            pass
    return out


def _prepare_raw_frame(per_acct: list) -> pd.DataFrame:
    """Concat + backfill + LTP-override + numeric-fillna. Returns the
    fully-hydrated pandas DataFrame ready for polars conversion."""
    raw = pd.concat(per_acct, ignore_index=True) if per_acct else pd.DataFrame()
    if raw.empty:
        return raw
    # Backfill missing market data (close_price + last_price) for adapters
    # that don't populate them (Dhan / Groww). Market data routes through
    # PriceBroker.quote (prefers Kite) so cross-broker rows agree on Day
    # P&L / Day % / Prev Close.
    broker_apis.backfill_market_data(raw)
    # Rows still at last_price=0 (rate-limit cool-off or missing quote) get
    # patched from the live KiteTicker snapshot — same pattern as positions.
    _override_stale_ltp_from_ticker(raw)
    numeric = raw.select_dtypes(include="number").columns
    raw[numeric] = raw[numeric].fillna(0)
    return raw


def _compute_summary_df(df: pl.DataFrame) -> pl.DataFrame:
    """Group by account, add derived %s, append TOTAL row.

    day_change_percentage uses YESTERDAY's value (cur_val - day_change_val)
    as the denominator — Kite's convention for "today moved X% off the
    previous close". Using cur_val (which already includes today's gain)
    would understate on positive days and overstate on negative.
    """
    sum_cols = [c for c in ["inv_val", "cur_val", "pnl", "day_change_val"] if c in df.columns]
    grouped = df.group_by("account").agg([pl.col(c).sum() for c in sum_cols])
    derived = [
        (pl.col("pnl") / pl.col("inv_val") * 100).alias("pnl_percentage"),
        (pl.col("day_change_val") / (pl.col("cur_val") - pl.col("day_change_val")) * 100)
            .alias("day_change_percentage"),
    ]
    grouped = grouped.with_columns(derived)
    totals = grouped.select(sum_cols).sum().with_columns([
        pl.lit("TOTAL").alias("account"), *derived
    ])
    return pl.concat([grouped, totals], how="diagonal").fill_nan(0).fill_null(0)


def _apply_stale_since_map(
    rows: list[HoldingRow], stale_since: dict[str, str],
) -> list[HoldingRow]:
    """Thread account_stale_since into rows where the account was
    LKG-substituted (breaker-open cache path). See positions.py."""
    if not stale_since:
        return rows
    return [
        msgspec.structs.replace(r, account_stale_since=stale_since[r.account])
        if r.account_stale and r.account in stale_since
        else r
        for r in rows
    ]


def _build_holding_rows(df_rows, stale_since_by_acct: dict) -> list:
    """Convert a polars row-select DataFrame to HoldingRow list with stale threading."""
    rows = [
        HoldingRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in df_rows.to_dicts()
    ]
    return _apply_stale_since_map(rows, stale_since_by_acct)


def _build_summary_rows(summary_df) -> list:
    """Convert a polars summary DataFrame to HoldingsSummaryRow list."""
    return [
        HoldingsSummaryRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in summary_df.to_dicts()
    ]


async def _fetch() -> HoldingsResponse:
    # Run sync broker SDK calls in a thread pool — avoids blocking the event
    # loop (100-500ms round-trips). With --workers 1 (Kite constraint) a
    # blocking call here stalls the entire API.
    per_acct = await asyncio.to_thread(broker_apis.fetch_holdings)
    # Outage detection: fetch_failed flag set on every frame. Empty per_acct
    # alone is a legitimate "no holdings" state — not an outage.
    if _is_full_outage(per_acct):
        raise Exception(
            "Broker returned no holdings data — upstream Bad Gateway / outage"
        )
    stale_since_by_acct = _stale_since_map(per_acct)
    raw = await asyncio.to_thread(_prepare_raw_frame, per_acct)
    if raw.empty:
        return HoldingsResponse(rows=[], summary=[], refreshed_at=timestamp_display())

    # Replace broker's drifted close_price with the prior-session EOD LTP
    # from daily_book. Also writes previous_close to all rows and recomputes
    # day_change_val for ALL rows with previous_close > 0 using
    # (ltp - previous_close) × qty — the canonical holdings day P&L formula.
    await _override_stale_close_for_holdings(raw)

    df = pl.from_pandas(raw)
    row_cols = [c for c in _ROW_COLS if c in df.columns]
    df_rows = df.select(row_cols)
    summary_df = _compute_summary_df(df)

    rows = _build_holding_rows(df_rows, stale_since_by_acct)
    summary = _build_summary_rows(summary_df)
    stale_accts = sorted({r.account for r in rows if r.account_stale})
    return HoldingsResponse(
        rows=rows,
        summary=summary,
        refreshed_at=timestamp_display(),
        stale_accounts=stale_accts,
    )


def _hold_mask_account_in_resp(resp: "HoldingsResponse", _msc) -> "HoldingsResponse":
    """Replace raw account codes with masked versions (copy-not-mutate)."""
    def _mask_row(row):
        return _msc.structs.replace(row, account=mask_account(row.account))
    return _msc.structs.replace(
        resp,
        rows=[_mask_row(r) for r in resp.rows],
        summary=[_mask_row(s) for s in resp.summary],
    )


def _filter_holdings_by_account(
    resp: "HoldingsResponse",
    account: Optional[str],
    _msc,
) -> "HoldingsResponse":
    """Narrow *resp* to rows belonging to *account* (raw code, pre-mask).

    - If *account* is None or empty: return *resp* unchanged.
    - Otherwise: keep only rows where ``row.account`` matches *account*
      (case-insensitive) and rebuild the summary so it contains the
      matching account's summary row plus a recalculated TOTAL row whose
      values equal that account's totals (single-account TOTAL = account).
    """
    if not account:
        return resp

    acct_upper = account.strip().upper()
    filtered_rows = [
        r for r in resp.rows
        if str(getattr(r, "account", "")).upper() == acct_upper
    ]
    # Find the matching summary row (non-TOTAL).
    acct_summary = [
        s for s in resp.summary
        if str(getattr(s, "account", "")).upper() == acct_upper
    ]
    if acct_summary:
        src = acct_summary[0]
        total_row = _msc.structs.replace(src, account="TOTAL")
        new_summary = acct_summary + [total_row]
    else:
        new_summary = []

    return _msc.structs.replace(resp, rows=filtered_rows, summary=new_summary)


async def _scope_and_mask_holdings(
    resp: "HoldingsResponse",
    request: "Request",
    account: Optional[str] = None,
) -> "HoldingsResponse":
    """Apply trader horizontal scoping then account-ID masking.

    Trader role: filter rows + summary to the user's assigned_accounts.
    Non-admin: replace raw account codes with masked versions
    (copy-not-mutate so the shared cache doesn't end up holding masked
    codes — prevents the demo→signin lag bug).

    The two transforms are always applied in order (scope first, then mask)
    so a trader who is also non-admin gets both filters.

    *account*: optional raw account code from the ``?account=`` query param.
    Applied after trader-scope scoping but before masking so comparisons
    use raw codes (the same values in ``row.account`` at that point).
    """
    import msgspec as _msc

    role = normalise_role(resolve_role_from_connection(request))
    if role == "trader":
        allowed, _ = await user_scope_for_connection(request)
        allowed_set = {str(a).upper() for a in (allowed or [])}
        resp = _msc.structs.replace(
            resp,
            rows=[r for r in resp.rows
                  if str(getattr(r, "account", "")).upper() in allowed_set],
            summary=[s for s in resp.summary
                     if str(getattr(s, "account", "")).upper() in allowed_set
                     or str(getattr(s, "account", "")).upper() == "TOTAL"],
        )
    # Account filter runs after trader-scope (uses raw codes, pre-mask).
    if account:
        resp = _filter_holdings_by_account(resp, account, _msc)
    if not is_admin_request(request):
        resp = _hold_mask_account_in_resp(resp, _msc)
    return resp


class HoldingsController(Controller):
    path = "/api/holdings"

    @get("/")
    async def get_holdings(
        self,
        request: Request,
        fresh: bool = False,
        skip_ltp: bool = False,
        account: Optional[str] = None,
    ) -> HoldingsResponse:
        try:
            # ── Inner helpers ───────────────────────────────────────────────
            # Holdings are long-dated (days–weeks) so their LTP doesn't
            # change between sessions.  closed_hours_or_broker decides
            # whether to call the broker or serve the daily_book snapshot.
            # `?fresh=1` bypasses the gate.
            # `?skip_ltp=1` — force snapshot path even when a segment is open.
            async def _snapshot_fn() -> HoldingsResponse:
                snap = await _holdings_snapshot()
                if snap is None:
                    # as_of=None signals "no snapshot yet" so the gate at
                    # line ~604 does NOT short-circuit on first deploy when
                    # the DB is empty.  The gate only short-circuits when
                    # as_of is truthy (a genuine persisted snapshot exists).
                    return HoldingsResponse(rows=[], summary=[],
                                            refreshed_at=timestamp_display(),
                                            as_of=None)
                return snap

            async def _broker_fn() -> HoldingsResponse:
                if fresh:
                    invalidate("holdings")
                    try:
                        from backend.brokers.broker_apis import (
                            _raw_cache_invalidate, dhan_next_poll_clear,
                            _use_conn_service,
                        )
                        _raw_cache_invalidate("holdings")
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
                _resp = await get_or_fetch("holdings", _fetch, ttl_seconds=_TTL)
                # Per-exchange overlay — closed exchanges get snapshot LTP
                # + price_source="snapshot_*" + is_animating=False. Runs
                # on the broker path so NSE-closed / MCX-open windows serve
                # mixed live+snap.
                _new_rows = await _overlay_snapshot_for_closed_exchanges(list(_resp.rows))
                import msgspec as _msc
                return _msc.structs.replace(_resp, rows=_new_rows)

            # ── Route selector ──────────────────────────────────────────────
            # ?skip_ltp=1 — RefreshButton's both-closed click: broker path
            # refreshes metadata; overlay freezes closed-exchange LTPs.
            # ?fresh=1 — bypass closed-hours gate entirely.
            if skip_ltp:
                resp = await _broker_fn()
            elif not fresh:
                resp, source = await closed_hours_or_broker(
                    exchange="NSE",
                    snapshot_fn=_snapshot_fn,
                    broker_fn=_broker_fn,
                    fallback_to_snapshot_on_broker_error=True,
                    route_key="holdings",
                    segment_exchanges=["NSE"],
                )
                if source not in ("live", "stale-live") and getattr(resp, "as_of", None):
                    # Market closed — snapshot path: scope + mask then return.
                    logger.debug(
                        f"holdings: market closed ({source}) — serving daily_book snapshot"
                    )
                    return await _scope_and_mask_holdings(resp, request, account=account)
                # Market is open (or stale-live), or no snapshot exists yet —
                # continue to live path.
                if source not in ("live", "stale-live"):
                    resp = await _broker_fn()
            else:
                resp = await _broker_fn()

            # Live path: scope + mask before returning.
            return await _scope_and_mask_holdings(resp, request, account=account)

        except Exception as e:
            logger.error(f"Holdings API error: {e}")
            if _is_broker_outage(e):
                raise HTTPException(
                    status_code=503,
                    detail="Broker (Kite) is temporarily unavailable. Try again shortly.",
                )
            raise HTTPException(status_code=500, detail=str(e))
