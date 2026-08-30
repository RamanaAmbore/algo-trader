"""Positions endpoint — returns per-account rows and summary."""

import re
import pandas as pd
import polars as pl
from litestar import Controller, Request, get
from litestar.exceptions import HTTPException
from typing import Optional

from backend.api.algo.pnl_math import (
    apply_day_change_backstop,
    decomposed_intraday_pnl,
    naive_day_pnl,
    recompute_row_percentages,
)
from backend.api.cache import get_or_fetch, invalidate
from backend.api.helpers.ltp_patch import apply_ltp_patch, positions_policy
from backend.api.helpers.price_resolver import resolve_current_price
from backend.api.helpers.snapshot_gate import (
    _any_segment_open, closed_hours_or_broker, is_exchange_closed_now,
    latest_snapshot_ltp_map,
)
from backend.api.routes.positions_helpers import (
    _is_broker_outage,
    apply_scope_and_mask,
    build_row_from_snapshot_raw,
    build_summary_from_rows,
    merge_paper_into_live,
)
from backend.api.schemas import PositionsResponse, PositionRow, PositionsSummaryRow
from backend.brokers import broker_apis
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Lot-waterfall auto-pairing
# ---------------------------------------------------------------------------

# Matches the expiry/strike/type block at the end of a trading symbol.
# Two alternatives (applied left-to-right):
#   1. Month-code expiry: 24AUG + optional suffix → BANKNIFTY24AUGFUT, CRUDEOIL24AUGFUT
#   2. Numeric strike + type suffix → NIFTY24800CE
_EXPIRY_RE = re.compile(
    r'\d{2}[A-Z]{3,}\d*(CE|PE|FUT|OPT|BE|BEF)?$'
    r'|\d+(CE|PE|FUT|OPT|BE|BEF)$',
    re.IGNORECASE,
)


def _root_symbol(tradingsymbol: str) -> str:
    """Strip expiry / strike / type suffixes to get the underlying root name.

    Examples:
        NIFTY24800CE    → NIFTY
        BANKNIFTY24AUGFUT → BANKNIFTY
        CRUDEOIL24AUGFUT  → CRUDEOIL
        GOLDM24AUGFUT     → GOLDM
        INFY              → INFY
    """
    s = tradingsymbol.upper()
    # Step 1 — strip month-code expiry blocks (BANKNIFTY24AUGFUT → BANKNIFTY)
    #           and numeric-strike+type blocks (NIFTY24800CE → NIFTY).
    s = _EXPIRY_RE.sub('', s)
    # Step 2 — mop up any remaining trailing digits.
    s = re.sub(r'\d+$', '', s)
    return s or tradingsymbol.upper()


def _auto_pair_positions(rows: "list[PositionRow]") -> "list[PositionRow]":
    """Lot-waterfall auto-pairing.

    Groups rows by (account, root_symbol). Within each group, waterfall-matches
    longs vs shorts by quantity (largest first). Each matched pair gets a
    sequential key "P1", "P2" etc. Unmatched remainder → is_orphan=True.

    paired_qty = lots matched into the pair
    orphan_qty = abs(quantity) - paired_qty (unmatched lots on this row)
    """
    import msgspec as _msc

    if not rows:
        return rows

    # Group rows by (account, root_symbol). We work on indices so we can
    # accumulate replacements without mutating the original list.
    from collections import defaultdict
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        key = (r.account, _root_symbol(r.tradingsymbol))
        groups[key].append(i)

    # Build the result list pre-populated with the originals; we will
    # replace individual entries as we go.
    result: list[PositionRow] = list(rows)

    for (_account, _root), indices in groups.items():
        # Partition into longs, shorts, and flat (quantity == 0).
        longs: list[tuple[int, int]] = []   # (original_index, abs_qty)
        shorts: list[tuple[int, int]] = []
        for i in indices:
            q = rows[i].quantity
            if q > 0:
                longs.append((i, q))
            elif q < 0:
                shorts.append((i, abs(q)))
            # q == 0: no action — defaults (is_orphan=False, pair_group_key=None,
            # paired_qty=0, orphan_qty=0) are already correct.

        # Sort each side largest-first.
        longs.sort(key=lambda t: t[1], reverse=True)
        shorts.sort(key=lambda t: t[1], reverse=True)

        # Waterfall matching.
        pair_n = 1
        # Use mutable lists of [index, remaining_qty] for in-place reduction.
        longs_q: list[list] = [[i, q] for i, q in longs]
        shorts_q: list[list] = [[i, q] for i, q in shorts]

        while longs_q and shorts_q:
            li, lq = longs_q[0]
            si, sq = shorts_q[0]
            match_qty = min(lq, sq)
            key_label = f"P{pair_n}"

            result[li] = _msc.structs.replace(
                rows[li],
                is_orphan=False,
                pair_group_key=key_label,
                paired_qty=match_qty,
                orphan_qty=lq - match_qty,
            )
            result[si] = _msc.structs.replace(
                rows[si],
                is_orphan=False,
                pair_group_key=key_label,
                paired_qty=match_qty,
                orphan_qty=sq - match_qty,
            )

            longs_q[0][1] -= match_qty
            shorts_q[0][1] -= match_qty
            if longs_q[0][1] == 0:
                longs_q.pop(0)
            if shorts_q[0][1] == 0:
                shorts_q.pop(0)
            pair_n += 1

        # Remaining entries in longs_q / shorts_q after the waterfall
        # are either:
        #   a) rows that were NEVER matched (pair_group_key still None on
        #      result[i]) → mark is_orphan=True.
        #   b) rows that were PARTIALLY matched and have leftover qty
        #      (pair_group_key already set on result[i]) → already written
        #      correctly with the right orphan_qty; only need to touch
        #      is_orphan which is already False — leave them alone.
        for entry in longs_q + shorts_q:
            i, rem_qty = entry
            if result[i].pair_group_key is None:
                # Never matched — full orphan.
                result[i] = _msc.structs.replace(
                    rows[i],
                    is_orphan=True,
                    pair_group_key=None,
                    paired_qty=0,
                    orphan_qty=rem_qty,
                )
            # else: already written in the waterfall loop with the correct
            # paired_qty / orphan_qty; is_orphan is False (correct).

    return result


# ---------------------------------------------------------------------------
# GTT annotation helpers
# ---------------------------------------------------------------------------

async def _fetch_gtt_set(session) -> "set[tuple[str, str]]":
    """Return {(account, symbol)} for OPEN AlgoOrders that have a gtt_order_id."""
    from sqlalchemy import text as _sql_text
    rows = await session.execute(_sql_text(
        "SELECT account, symbol FROM algo_orders "
        "WHERE status = 'OPEN' AND gtt_order_id IS NOT NULL"
    ))
    return {(r.account, r.symbol) for r in rows}


def _annotate_gtt(rows: "list[PositionRow]", gtt_set: "set[tuple[str, str]]") -> "list[PositionRow]":
    import msgspec as _msc
    return [_msc.structs.replace(r, has_gtt=(r.account, r.tradingsymbol) in gtt_set) for r in rows]


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
    from backend.shared.helpers.date_time_utils import timestamp_indian as _ts_indian

    _now_ist = _ts_indian()
    _today_ist = _now_ist.date()
    _today_ist_midnight = _now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    _today_ist_8am = _today_ist_midnight + timedelta(hours=8)
    # prev_batch_cutoff: used by prev_batch CTE to exclude same-session rows.
    # 08:00 IST boundary is correct here — it prevents today's intraday rows
    # from appearing in the "prior session" reference batch.
    _prev_batch_cutoff = _today_ist_8am if _now_ist >= _today_ist_8am else _today_ist_8am - timedelta(days=1)
    # snapshot_cutoff: used by latest_batch CTE to select the most-recent EOD
    # snapshot.  Weekday-aware so Friday's 15:45 EOD snapshot is included when
    # the query runs on a Friday afternoon (old today-08:00 cutoff would exclude
    # it because 15:45 > 08:00, causing Thursday's data to be served instead).
    #   Mon–Fri : tomorrow midnight — includes any EOD snapshot written today
    #   Saturday: today midnight   — excludes any Sat market-special-session
    #   Sunday  : Saturday 00:00   — same intent as Saturday path
    _weekday = _now_ist.weekday()  # Mon=0 … Sun=6
    if _weekday == 5:   # Saturday
        _snapshot_cutoff = _today_ist_midnight
    elif _weekday == 6:  # Sunday
        _snapshot_cutoff = _today_ist_midnight - timedelta(days=1)
    else:               # Mon–Fri
        _snapshot_cutoff = _today_ist_midnight + timedelta(days=1)

    try:
        async with async_session() as session:
            # Single combined query — latest_batch anchors the current
            # snapshot, prev_batch finds the most-recent prior row per
            # (account, symbol) using captured_at < max_at (not date < today)
            # so UTC/IST date-column edge cases can't drop yesterday's rows.
            # prev_batch lookback window is 7 days to cover MCX's 23:30 IST
            # close and multi-day holiday gaps.
            # qty=0 rows (positions closed intraday) are included only when
            # db.date matches today IST so they show with 'closed' decoration
            # in the derivatives legs grid.  On the next trading day (before
            # market opens), yesterday's closed legs are excluded (date !=
            # today) leaving only the carried-overnight open positions.
            result = await session.execute(_sql_text("""
                WITH latest_batch AS (
                    SELECT account, MAX(captured_at) AS max_at
                    FROM daily_book
                    WHERE kind = 'positions' AND ltp IS NOT NULL AND ltp > 0
                      AND captured_at < :snapshot_cutoff
                    GROUP BY account
                ),
                prev_batch AS (
                    SELECT DISTINCT ON (db.account, db.symbol)
                        db.account,
                        db.symbol,
                        db.ltp       AS prev_ltp,
                        db.total_pnl AS prev_settlement_pnl
                    FROM daily_book db
                    JOIN latest_batch lb ON db.account = lb.account
                    WHERE db.kind = 'positions'
                      AND db.total_pnl IS NOT NULL
                      AND db.captured_at < lb.max_at
                      AND db.captured_at >= lb.max_at - INTERVAL '7 days'
                      AND db.ltp IS NOT NULL AND db.ltp > 0
                      AND db.captured_at < :prev_batch_cutoff
                    ORDER BY db.account, db.symbol, db.captured_at DESC
                )
                SELECT db.account, db.symbol, db.exchange, db.qty, db.avg_cost,
                       db.ltp, db.day_pnl, db.total_pnl, db.payload_json,
                       db.captured_at, db.previous_close,
                       pb.prev_ltp, pb.prev_settlement_pnl, db.previous_close_backup
                FROM daily_book db
                JOIN latest_batch lb
                  ON db.account = lb.account AND db.captured_at = lb.max_at
                LEFT JOIN prev_batch pb
                  ON pb.account = db.account AND pb.symbol = db.symbol
                WHERE db.kind = 'positions'
                  AND (db.qty != 0 OR db.date = :today_ist)
                  AND (db.ltp IS NULL OR db.ltp > 0)
                ORDER BY db.account, db.symbol
            """).bindparams(today_ist=_today_ist, prev_batch_cutoff=_prev_batch_cutoff, snapshot_cutoff=_snapshot_cutoff))
            raw_rows = result.all()
    except Exception as exc:
        logger.warning(f"positions snapshot query failed: {exc}")
        return None

    if not raw_rows:
        return None

    snap_captured_at_dt = raw_rows[0][9]  # index 9 = captured_at (previous_close=10, prev_ltp=11, prev_settlement_pnl=12)
    snap_captured_at: str = snap_captured_at_dt.isoformat() if snap_captured_at_dt else ""

    # Log when the snapshot is from a prior session (no today rows yet —
    # normal during the window between market close and scheduled snapshot run).
    if snap_captured_at_dt and snap_captured_at_dt.date() != _today_ist:
        logger.info(
            f"positions snapshot: no rows for today, serving prior snapshot "
            f"from {snap_captured_at_dt.date()}"
        )

    rows: list[PositionRow] = [build_row_from_snapshot_raw(r) for r in raw_rows]
    rows = _auto_pair_positions(rows)
    try:
        async with async_session() as _gtt_session:
            _gtt_set = await _fetch_gtt_set(_gtt_session)
        rows = _annotate_gtt(rows, _gtt_set)
    except Exception as _gtt_exc:
        logger.warning(f"positions snapshot: gtt_set fetch failed: {_gtt_exc}")

    summary = build_summary_from_rows(rows)

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
    # Yesterday's total_pnl from daily_book — None for positions opened today.
    'prev_settlement_pnl',
    # Frozen prior-session settlement price — set by _override_stale_close_from_snapshot
    # for every matched row (independent of the epsilon close_price patch).
    'previous_close',
]

_TTL = 30

# Fields that must remain None when absent rather than being coerced to 0
# by the general None-guard in the row-building comprehension.
_NULLABLE_COLS: frozenset[str] = frozenset({'prev_settlement_pnl'})


def _replace_row_price(r, live_ltp: float, exchange_open: bool, snap_ltp: "float | None"):
    """Apply resolve_current_price to *r* and return a replaced struct.

    When *exchange_open* is True, *snap_ltp* is ignored.
    When *exchange_open* is False, *snap_ltp* may supply the settled close
    price; None means no snapshot is available (pre-settle state).
    Also overlays last_price on the settled path so legacy consumers that
    read last_price see the frozen close price.
    """
    import msgspec as _msc
    has_snapshot = exchange_open is False and snap_ltp is not None and snap_ltp > 0
    price, source, animating = resolve_current_price(
        exchange_open=exchange_open,
        live_ltp=live_ltp,
        **({} if exchange_open else dict(
            snapshot_close=(float(snap_ltp) if has_snapshot else None),
            snapshot_last_ltp=live_ltp,
            settled=has_snapshot,
        )),
    )
    replace_kwargs: dict = {
        "price_source": source,
        "current_price": price if price is not None else live_ltp,
        "is_animating": animating,
    }
    if has_snapshot and price is not None:
        replace_kwargs["last_price"] = float(price)
    return _msc.structs.replace(r, **replace_kwargs)


async def _fetch_ref_close_map(
    closed_pairs: list[tuple[str, str]],
    kind: str,
) -> dict[tuple[str, str], float]:
    """Query daily_book for the prior-session settlement LTP for the given
    (account, symbol) pairs.  Only called for rows whose exchange is
    currently closed, to avoid DB hits for live MCX rows.

    Uses the same cutoff logic as `_override_stale_close_from_snapshot`
    (captured_at < today_08:00 IST) so the reference price is the true
    prior-session settlement LTP, not any mid-session or same-session value.

    Returns {} on error so callers fall through to the existing broker value.
    """
    if not closed_pairs:
        return {}

    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text
    from backend.api.helpers.exchange_clock import settlement_cutoff_for

    cutoff = await settlement_cutoff_for("NON-MCX")

    out: dict[tuple[str, str], float] = {}
    # Build IN-list filter to avoid scanning all positions rows.
    pair_filter = " AND (account, symbol) IN :pairs" if closed_pairs else ""
    params: dict = {"kind": kind, "cutoff": cutoff}
    if closed_pairs:
        params["pairs"] = tuple(closed_pairs)
    try:
        async with async_session() as session:
            result = await session.execute(_sql_text(f"""
                SELECT DISTINCT ON (account, symbol)
                       account, symbol, ltp AS ref_close
                FROM daily_book
                WHERE kind = :kind
                  AND ltp IS NOT NULL AND ltp > 0
                  AND captured_at < :cutoff
                  {pair_filter}
                ORDER BY account, symbol, captured_at DESC
            """), params)
            for account, symbol, ref_close in result.all():
                v = float(ref_close) if ref_close is not None else 0.0
                if v > 0:
                    out[(str(account), str(symbol))] = v
    except Exception as exc:
        logger.warning(f"_fetch_ref_close_map({kind}) failed: {exc}")
    return out


def _row_is_settled_flat(row) -> bool:
    """Case 3: flat intraday row (qty==0) — settled regardless of exchange state."""
    try:
        return int(getattr(row, "quantity", 0) or 0) == 0
    except (TypeError, ValueError):
        return False


def _exchange_closed_cached(exchange: str, cache: dict[str, bool]) -> bool:
    """Per-call exchange-closed probe with memoisation to avoid N×holiday lookups."""
    e = (exchange or "").upper()
    if e not in cache:
        cache[e] = is_exchange_closed_now(e)
    return cache[e]


async def _process_overlay_row(r, kind: str, snap_map: dict, ref_close_map: dict,
                               exchange_closed: dict) -> object:
    """Resolve price/source/animation for one row under the closed-exchange overlay.

    Handles the three cases: flat (settled), open exchange (live), closed exchange
    (snapshot path + optional day-change overlay for positions).
    """
    import msgspec as _msc
    broker_ltp = float(getattr(r, "last_price", 0.0) or 0.0)
    if _row_is_settled_flat(r):
        return _msc.structs.replace(
            r, price_source="snapshot_settled",
            current_price=broker_ltp, is_animating=False,
        )
    if not _exchange_closed_cached(getattr(r, "exchange", ""), exchange_closed):
        return _replace_row_price(r, broker_ltp, exchange_open=True, snap_ltp=None)

    key = (getattr(r, "account", ""), getattr(r, "tradingsymbol", ""))
    snap_val     = snap_map.get(key)
    snap_ltp     = snap_val[0] if isinstance(snap_val, tuple) else snap_val
    snap_day_pnl = snap_val[1] if isinstance(snap_val, tuple) else None
    replaced = _replace_row_price(r, broker_ltp, exchange_open=False, snap_ltp=snap_ltp)
    if kind == "positions":
        ref_close = ref_close_map.get(key, 0.0)
        if snap_ltp is not None:
            snap_ltp_f = float(snap_ltp)
            qty = int(getattr(r, "quantity", 0) or 0)
            if snap_day_pnl is not None and snap_day_pnl != 0.0:
                dcv = snap_day_pnl
            elif ref_close > 0:
                dcv = (snap_ltp_f - ref_close) * qty
            else:
                dcv = None
            if dcv is not None:
                prev_val = abs(ref_close * qty) if (ref_close > 0 and qty) else 0.0
                dcp = (dcv / prev_val * 100.0) if prev_val else 0.0
                replaced = _msc.structs.replace(
                    replaced, day_change_val=dcv, day_change_percentage=dcp,
                    close_price=ref_close,
                )
    return replaced


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
      5. (positions only) day_change_val / day_change_percentage / close_price
         overlay for closed-exchange rows using the prior-session settlement
         LTP from daily_book so the values are consistent with the snapshot path

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

    import msgspec as _msc

    exchange_closed: dict[str, bool] = {}
    snap_map = await latest_snapshot_ltp_map(kind)

    closed_pairs: list[tuple[str, str]] = [
        (str(getattr(r, "account", "")), str(getattr(r, "tradingsymbol", "")))
        for r in rows
        if _exchange_closed_cached(getattr(r, "exchange", ""), exchange_closed)
        and not _row_is_settled_flat(r)
    ]
    ref_close_map: dict[tuple[str, str], float] = {}
    if kind == "positions" and closed_pairs:
        ref_close_map = await _fetch_ref_close_map(closed_pairs, kind)

    out = []
    for r in rows:
        out.append(await _process_overlay_row(
            r, kind, snap_map, ref_close_map, exchange_closed,
        ))
    return out


def _build_stale_since_map(per_acct: list) -> dict[str, str]:
    """Extract account → "HH:MM IST" map from stale-substituted DataFrames.

    Must be called BEFORE pd.concat (which drops DataFrame.attrs).
    Returns {} when no frames are stale or per_acct is empty.
    """
    from zoneinfo import ZoneInfo
    from datetime import datetime
    result: dict[str, str] = {}
    for _df in (per_acct or []):
        _ss = _df.attrs.get("stale_since")
        if not _ss or _df.empty or "account" not in _df.columns:
            continue
        _acct = str(_df["account"].iloc[0])
        try:
            result[_acct] = datetime.fromtimestamp(
                float(_ss), tz=ZoneInfo("Asia/Kolkata")
            ).strftime("%H:%M IST")
        except Exception:
            pass
    return result


def _build_polars_summary(df: "pl.DataFrame") -> "pl.DataFrame":
    """Build a per-account + TOTAL summary DataFrame from the live-positions polars frame.

    The day_change_percentage denominator is Σ|close × qty| per account —
    the same formula the snapshot path uses via `build_summary_from_rows`.
    Returns a polars DataFrame with columns:
      account, pnl, day_change_val, day_change_percentage, day_prev_val
    """
    df = df.with_columns(
        (pl.col('close_price') * pl.col('quantity')).abs().alias('_prev_val')
    )
    sum_cols = [c for c in ('pnl', 'day_change_val', '_prev_val') if c in df.columns]
    if sum_cols:
        grouped = df.group_by('account').agg([pl.col(c).sum() for c in sum_cols])
    else:
        grouped = pl.DataFrame({'account': []})
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
    return summary_df.with_columns(
        (pl.col('day_change_val') / pl.col('_prev_val').replace(0, None) * 100)
        .fill_nan(0).fill_null(0)
        .alias('day_change_percentage')
    ).rename({'_prev_val': 'day_prev_val'})


def _apply_flat_row_hygiene(raw: "pd.DataFrame") -> None:
    """Zero day_change and day_change_percentage for pure intraday round-trips.

    Rows with quantity==0 AND overnight_quantity==0 are pure intraday
    round-trips where LTP is meaningless and day_change_val should be zero.
    Closed overnight positions (qty==0, oq>0) must retain their backstop
    day_change_val set by apply_day_change_backstop — this function must NOT
    overwrite them.

    No-ops when raw is empty or the relevant columns are absent.
    """
    import pandas as _pd
    if raw.empty or 'quantity' not in raw.columns:
        return
    _qty = _pd.to_numeric(raw['quantity'], errors='coerce').fillna(0)
    _oq  = _pd.to_numeric(
        raw['overnight_quantity'] if 'overnight_quantity' in raw.columns
        else _pd.Series(0.0, index=raw.index),
        errors='coerce',
    ).fillna(0)
    _flat_mask = (_qty == 0) & (_oq == 0)
    if not _flat_mask.any():
        return
    if 'day_change' in raw.columns:
        raw.loc[_flat_mask, 'day_change'] = 0.0
    # day_change_val: the absolute ₹ move for a flat intraday position is
    # zero by definition; leaving it non-zero would cause phantom P&L in
    # the NavStrip performance slot.
    if 'day_change_val' in raw.columns:
        raw.loc[_flat_mask, 'day_change_val'] = 0.0
    # day_change_percentage: denominator collapses to 0 when qty=0;
    # undefined percentage — zero it rather than show a spurious value.
    if 'day_change_percentage' in raw.columns:
        raw.loc[_flat_mask, 'day_change_percentage'] = 0.0


async def _patch_raw_positions(raw: "pd.DataFrame") -> "pd.DataFrame":
    """Apply the close-price override and day P&L backstop to the raw
    positions DataFrame, in that order.

    Ordering invariant (tested):
      1. _override_stale_close_from_snapshot — patches close_price so
         day_change_val is computed against yesterday's real close rather
         than Kite's stale overnight price.
      2. apply_day_change_backstop — rescues Case 1 (new position,
         overnight_quantity=0) and Case 3 (flat intraday, quantity=0)
         where Kite omits day_change_val.

    Extracted from _fetch() as part of the CC-reduction refactor so the
    sequence can be tested independently without running a full broker call.
    """
    # Override stale close_price with yesterday's daily_book snapshot.
    # See CLAUDE.md §"Kite close_price stale overnight" and the
    # 2026-06-19 +1.33L phantom gain incident.
    await _override_stale_close_from_snapshot(raw)

    # Case 1 + Case 2 + Case 3 Day P&L backstop — restores day_change_val
    # for new intraday positions (oq=0, ltp=0 pre-first-tick), overnight
    # positions where LTP gate zeroed dcv (Case 2), and fully-closed
    # intraday round-trips (qty=0, oq=0; Case 3 — overnight closures use
    # Case 2 so only today's session gain is returned, not total-from-entry).
    # SSOT: backend.api.algo.pnl_math.apply_day_change_backstop. The
    # background performance task calls the same helper so NavStrip P
    # "today" slot agrees with the /api/positions route.
    raw = apply_day_change_backstop(raw)
    return raw


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

    # Build stale-since map BEFORE concat (attrs dropped after concat).
    _acct_stale_since = _build_stale_since_map(per_acct)

    raw = pd.concat(per_acct, ignore_index=True) if per_acct else pd.DataFrame()
    # Legitimate empty book — no positions on any account. Return a
    # well-formed empty response so /admin/derivatives renders zero
    # candidates instead of the false "Positions feed unavailable"
    # banner (which only fires on actual outage 5xx now).
    if raw.empty:
        return PositionsResponse(rows=[], summary=[], refreshed_at=timestamp_display())

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
    # yesterday's EOD) even though MCX had been open 30 min.
    _override_stale_ltp_from_ticker(raw)

    raw = await _patch_raw_positions(raw)

    # Flat-row hygiene (route-only): rows with quantity == 0 should not
    # report a per-share day_change delta (LTP is meaningless for a closed
    # position). Separate from the day_change_val backstop above.
    _apply_flat_row_hygiene(raw)

    numeric = raw.select_dtypes(include='number').columns
    raw[numeric] = raw[numeric].fillna(0)
    df = pl.from_pandas(raw)

    row_cols = [c for c in _ROW_COLS if c in df.columns]
    df_rows = df.select(row_cols)
    summary_df = _build_polars_summary(df)

    rows = [_dict_to_position_row(r) for r in df_rows.to_dicts()]

    # Auto-pair positions by lot-waterfall within (account, root_symbol) groups.
    rows = _auto_pair_positions(rows)
    try:
        from backend.api.database import async_session as _async_session
        async with _async_session() as _gtt_session:
            _gtt_set = await _fetch_gtt_set(_gtt_session)
        rows = _annotate_gtt(rows, _gtt_set)
    except Exception as _gtt_exc:
        logger.warning(f"positions live: gtt_set fetch failed: {_gtt_exc}")

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
    # Per-exchange close-snapshot overlay (Jul 2026 unified animation model).
    rows = await _overlay_snapshot_for_closed_exchanges(rows, kind="positions")
    summary = [
        PositionsSummaryRow(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in summary_df.to_dicts()
    ]
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


def _backfill_prev_settlement_pnl(
    raw: pd.DataFrame,
    prev_pnl_map: dict[tuple[str, str], float],
) -> None:
    """Set `prev_settlement_pnl` on each row from yesterday's daily_book total_pnl.

    No-ops when `prev_pnl_map` is empty or `raw` is empty.
    Rows with no matching key in `prev_pnl_map` (positions opened today)
    keep None — the PositionRow default for that optional field.
    """
    if not prev_pnl_map or raw.empty:
        return
    if 'prev_settlement_pnl' not in raw.columns:
        raw['prev_settlement_pnl'] = None
    for idx in raw.index:
        key = (str(raw.at[idx, 'account']), str(raw.at[idx, 'tradingsymbol']))
        if key in prev_pnl_map:
            raw.at[idx, 'prev_settlement_pnl'] = prev_pnl_map[key]


def _dict_to_position_row(r: dict) -> "PositionRow":
    """Build a PositionRow from a polars `to_dicts()` record.

    Fields in _NULLABLE_COLS are allowed to stay None; all other None
    values are coerced to 0 to satisfy the non-optional Struct fields.
    """
    return PositionRow(**{k: (v if v is not None or k in _NULLABLE_COLS else 0) for k, v in r.items()})


async def _override_stale_close_from_snapshot(raw: pd.DataFrame) -> None:
    """Replace `close_price` with the most-recent daily_book snapshot LTP
    per (account, tradingsymbol). When found, recomputes the decomposed
    day_change_val so the row reflects the actual move since the prior
    session's authoritative close.

    Uses `daily_book.ltp` directly (not COALESCE with `previous_close`).
    `previous_close` is populated from Kite's stale BHAV-copy API and is
    unreliable during the overnight window — it always passes the epsilon
    check, meaning `close_price` would never be patched. `daily_book.ltp`
    is the actual settlement LTP captured at session end and is the
    canonical prior-session reference price.

    Only triggers when the snapshot LTP differs from Kite's reported
    close_price by more than a tiny epsilon — rows where Kite is already
    current pass through unchanged."""
    if raw.empty or 'tradingsymbol' not in raw.columns or 'account' not in raw.columns:
        return

    # Initialise previous_close column unconditionally so the column always
    # exists even when no DB rows match or the query raises.
    raw['previous_close'] = 0.0

    # Pull the latest snapshot per (account, symbol) — DISTINCT ON keeps
    # only the most recent row, regardless of which date label the
    # snapshot daemon used (00:09 IST captures end up labelled with the
    # NEXT session's date; 23:52 IST captures end up labelled with the
    # CURRENT session's date — both represent the same prior-session EOD).
    from datetime import timedelta
    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text

    if not (raw["account"].notna() & raw["tradingsymbol"].notna()).any():
        return

    # Cutoff = last passed 08:00 IST boundary (the prev_close invariant).
    # Use NON-MCX gate; MCX gate has the same reset time (08:00 IST) so one
    # cutoff covers all exchanges.  Both daily_book snapshots (NSE ~15:45 and
    # MCX ~00:15) fall before the 08:00 boundary and are included by this query.
    from backend.api.helpers.exchange_clock import settlement_cutoff_for
    today_ist_cutoff = await settlement_cutoff_for("NON-MCX")

    snapshot_map: dict[tuple[str, str], float] = {}
    prev_pnl_map: dict[tuple[str, str], float] = {}
    try:
        async with async_session() as session:
            result = await session.execute(_sql_text("""
                SELECT DISTINCT ON (account, symbol) account, symbol,
                       daily_book.ltp AS ref_close,
                       total_pnl
                FROM daily_book
                WHERE kind = 'positions' AND ltp IS NOT NULL AND ltp > 0
                  AND captured_at >= :lower_cutoff
                  AND captured_at < :today_open
                ORDER BY account, symbol, captured_at DESC
            """), {
                "lower_cutoff": today_ist_cutoff - timedelta(days=7),
                "today_open": today_ist_cutoff,
            })
            for account, symbol, ref_close, total_pnl in result.all():
                key = (str(account), str(symbol))
                snapshot_map[key] = float(ref_close)
                if total_pnl is not None:
                    prev_pnl_map[key] = float(total_pnl)
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
        # Set previous_close for ALL matched rows regardless of epsilon check —
        # this is the frozen prior-session settlement price consumed by the
        # frontend's (ltp − previous_close) × qty formula.
        raw.at[idx, 'previous_close'] = snap_ltp
        try:
            current_close = float(raw.at[idx, 'close_price']) if pd.notna(raw.at[idx, 'close_price']) else 0.0
        except (TypeError, ValueError):
            current_close = 0.0
        if abs(snap_ltp - current_close) <= 0.005:
            continue
        raw.at[idx, 'close_price'] = snap_ltp
        patched_idx.append(idx)

    # Backfill prev_settlement_pnl — yesterday's total_pnl for each position
    # that exists in the daily_book snapshot.  Rows opened today have no entry
    # and remain None (the PositionRow default).  Must run before the
    # `if not patched_idx: return` guard so it fires even on days when Kite's
    # close_price already matches the snapshot (no close-override needed).
    _backfill_prev_settlement_pnl(raw, prev_pnl_map)

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
    valid = set(PositionRow.__struct_fields__)
    for r in raw.to_dict(orient='records'):
        kwargs = {k: (r[k] if r[k] is not None else 0) for k in r}
        kwargs.setdefault('last_price_stale', False)
        kwargs['mode'] = 'paper'
        kwargs = {k: v for k, v in kwargs.items() if k in valid}
        rows.append(PositionRow(**kwargs))

    summary = build_summary_from_rows(rows)
    return PositionsResponse(rows=rows, summary=summary, refreshed_at=timestamp_display())


def _batch_fetch_spots(underlying_keys: set[str]) -> dict[str, float]:
    """Fetch last_price for each key in *underlying_keys* via one broker.quote() call.

    Returns a dict mapping each key to its spot price. Returns {} on broker
    failure so the caller can skip Greek computation gracefully.
    """
    from backend.brokers.registry import get_market_data_broker
    try:
        broker = get_market_data_broker()
        spot_data = broker.quote(list(underlying_keys)) or {}
        return {k: float(v.get("last_price") or 0.0) for k, v in spot_data.items()}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Greeks enrich: underlying spot fetch failed: {exc}")
        return {}


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
    spot_by_key = _batch_fetch_spots(underlying_keys)

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


async def _resolve_positions_source(
    request: Request,
    fresh: bool,
    skip_ltp: bool,
) -> PositionsResponse:
    """Resolve whether to serve a DB snapshot or a live broker fetch.

    Encapsulates the closed_hours_or_broker gate, ?fresh=1 cache invalidation,
    ?skip_ltp=1 bypass, and the first-deploy fallback (snapshot returns None).
    Returns a PositionsResponse — caller applies scope/mask afterwards.
    """
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
    # intraday split) refreshes; the row-level overlay tags every
    # closed-exchange row as price_source='snapshot_*' and freezes
    # its last_price to the daily_book close_settled value.
    #
    # Guard: skip_ltp only bypasses the snapshot gate when the market
    # is actually open. Off-market, the broker may return an empty
    # positions frame (post-settlement clearing) which would blank
    # pulsePositionsStore. ?fresh=1 always bypasses (operator-explicit
    # refresh); ?skip_ltp=1 requires market to be open.
    if fresh:
        return await _broker_fn()
    import asyncio as _asyncio
    mkt_open = await _asyncio.to_thread(_any_segment_open)
    if skip_ltp and mkt_open:
        return await _broker_fn()

    resp, source = await closed_hours_or_broker(
        exchange="NSE",
        snapshot_fn=_snapshot_fn,
        broker_fn=_broker_fn,
        fallback_to_snapshot_on_broker_error=True,
        route_key="positions",
    )
    # When market is closed and the DB has a genuine snapshot (as_of
    # is set), return it directly — scope/mask applied by caller.
    if source not in ("live", "stale-live") and getattr(resp, "as_of", None):
        logger.debug(
            f"positions: market closed ({source}) — serving daily_book snapshot"
        )
        return resp
    # Market is open or stale-live — resp is already the broker response.
    if source in ("live", "stale-live"):
        return resp
    # Market closed but no snapshot yet (first deploy) — fall back live.
    return await _broker_fn()


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
                return await apply_scope_and_mask(resp, request)
            except Exception as e:
                logger.error(f"Paper positions API error: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        try:
            resp = await _resolve_positions_source(request, fresh, skip_ltp)

            # ── mode=both — merge paper rows into the live response ─────────
            # Paper rows tagged mode='paper'; live rows default mode='live'.
            # Summary is recomputed over the combined set so totals are correct.
            if mode == "both":
                paper_resp = await _build_paper_positions_response()
                resp = merge_paper_into_live(resp, paper_resp)

            # Horizontal scoping + masking.
            # MUST run BEFORE masking — once accounts are masked to `ZG####`
            # the trader's assigned-account match can't run.
            # CRITICAL: apply_scope_and_mask uses msgspec.structs.replace so
            # the cached object reference is never mutated in place.
            return await apply_scope_and_mask(resp, request)
        except Exception as e:
            logger.error(f"Positions API error: {e}")
            if _is_broker_outage(e):
                raise HTTPException(
                    status_code=503,
                    detail="Broker (Kite) is temporarily unavailable. Try again shortly.",
                )
            raise HTTPException(status_code=500, detail=str(e))
