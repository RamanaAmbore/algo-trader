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
    build_symbol_summary_from_rows,
    merge_paper_into_live,
)
from backend.api.schemas import (
    PositionsResponse, PositionRow, PositionsSummaryRow, PositionsSymbolSummaryRow,
)
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

# The 08:00 IST boundary of the trading SESSION that a `captured_at` value
# falls into — derived PURELY from `captured_at`, never from the `date`
# column (2026-09 Day P&L audit round 3, item #1). Lifted to a module
# constant (not inlined per call site) so `_positions_snapshot`'s
# `latest_batch` CTE and the regression test that proves its behavior
# against a real Postgres instance both execute the EXACT same SQL text —
# a hand-copied test mirror can silently drift from the real query.
#
# `date_trunc('day', (captured_at AT TIME ZONE 'Asia/Kolkata') -
# INTERVAL '8 hours') + INTERVAL '8 hours'` shifts captured_at back 8h,
# truncates to the calendar day, then shifts forward 8h again — landing on
# the 08:00 IST start of the `[08:00, next 08:00)` trading-day window
# `captured_at` itself falls into, regardless of whichever calendar day
# the `date` column happens to carry.
_SESSION_ANCHOR_CUTOFF_TS_SQL = (
    "(date_trunc('day', (captured_at AT TIME ZONE 'Asia/Kolkata') - INTERVAL '8 hours')"
    " + INTERVAL '8 hours') AT TIME ZONE 'Asia/Kolkata'"
)

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
    #   Saturday: today 02:00 IST  — includes MCX 00:15 settlement, excludes Sat sessions (09:00+)
    #   Sunday  : Saturday 02:00   — same boundary as Saturday path
    _weekday = _now_ist.weekday()  # Mon=0 … Sun=6
    if _weekday == 5:   # Saturday: +2 h to capture MCX 00:15 settlement
        _snapshot_cutoff = _today_ist_midnight + timedelta(hours=2)
    elif _weekday == 6:  # Sunday: same boundary = Saturday 02:00 IST
        _snapshot_cutoff = _today_ist_midnight - timedelta(hours=22)
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
            result = await session.execute(_sql_text(f"""
                WITH latest_batch AS (
                    -- cutoff_ts: the 08:00 IST boundary of the trading
                    -- SESSION that THIS batch's own `captured_at` falls
                    -- into — derived purely from `captured_at`, never from
                    -- the `date` column.
                    --
                    -- Round-2 fix (superseded) anchored cutoff_ts to the
                    -- `date` column (`date + 08:00 IST`). That still broke:
                    -- the *writer* (daily_snapshot.py) stamps `date` from
                    -- the wall-clock calendar day at write time, not the
                    -- trading session captured. A close_settled write that
                    -- fires just after midnight IST (e.g. MCX close 23:30 +
                    -- a 30 min settled-offset = 00:00 next calendar day)
                    -- gets `date` = the NEXT day, so `date`-based cutoff_ts
                    -- (next day's 08:00) sits WELL AFTER this same batch's
                    -- own earlier same-session writes (e.g. an NSE
                    -- close_settled write at 16:00 the PRIOR calendar day)
                    -- — those earlier-same-session rows then satisfy
                    -- `captured_at < cutoff_ts` and get picked as "baseline"
                    -- even though they're the SAME trading session as
                    -- "current", collapsing Day P&L to ~0 (2026-09 Day P&L
                    -- audit round 3, item #1).
                    --
                    -- Fix: compute cutoff_ts as the 08:00 IST boundary of
                    -- the 24h trading-day window [08:00, next 08:00) that
                    -- `captured_at` itself falls into. This is immune to
                    -- whichever calendar day the `date` column happens to
                    -- carry — a post-midnight write's captured_at still
                    -- resolves to ITS trading session's own 08:00 start,
                    -- correctly excluding every row from that same
                    -- session (however late it was written) and landing
                    -- on the strictly prior trading day's close-reset row.
                    SELECT DISTINCT ON (account) account, captured_at AS max_at,
                           {_SESSION_ANCHOR_CUTOFF_TS_SQL} AS cutoff_ts
                    FROM daily_book
                    WHERE kind = 'positions' AND ltp IS NOT NULL AND ltp > 0
                      AND captured_at < :snapshot_cutoff
                    ORDER BY account, captured_at DESC
                ),
                prev_batch AS (
                    -- prev_ltp ONLY (prev_close / prev_ltp sourcing is governed
                    -- by the "close_price / ltp invariant — DO NOT CHANGE" rule
                    -- in CLAUDE.md; kept exactly as-is, 7-day lookback window).
                    -- prev_settlement_pnl (base_pnl) is sourced from the
                    -- batch-anchored pnl_ranked CTE below (_BASELINE_PNL_CTE_SQL —
                    -- same fragment used by _fetch_baseline_pnl_map /
                    -- _fetch_snapshot_close_map), which fixes the same staleness
                    -- risk this loose 7-day window has, plus the
                    -- kind IN ('positions','holdings') CNC-split precedence.
                    SELECT DISTINCT ON (db.account, db.symbol)
                        db.account,
                        db.symbol,
                        db.ltp       AS prev_ltp
                    FROM daily_book db
                    JOIN latest_batch lb ON db.account = lb.account
                    WHERE db.kind = 'positions'
                      AND db.captured_at < lb.max_at
                      AND db.captured_at >= lb.max_at - INTERVAL '7 days'
                      AND db.ltp IS NOT NULL AND db.ltp > 0
                      AND db.captured_at < :prev_batch_cutoff
                    ORDER BY db.account, db.symbol, db.captured_at DESC
                ),
                {_BASELINE_PNL_CTE_SQL}
                SELECT db.account, db.symbol, db.exchange, db.qty, db.avg_cost,
                       db.ltp, db.day_pnl, db.total_pnl, db.payload_json,
                       db.captured_at, db.prev_close AS previous_close,
                       pb.prev_ltp, pf.total_pnl AS prev_settlement_pnl, db.prev_close_backup,
                       pf.kind AS prev_settlement_kind, pf.qty AS prev_settlement_qty
                FROM daily_book db
                JOIN latest_batch lb
                  ON db.account = lb.account AND db.captured_at = lb.max_at
                LEFT JOIN prev_batch pb
                  ON pb.account = db.account AND pb.symbol = db.symbol
                LEFT JOIN pnl_final pf
                  ON pf.account = db.account AND pf.symbol = db.symbol
                WHERE db.kind = 'positions'
                  AND (db.qty != 0 OR db.date = :today_ist)
                  AND (db.ltp IS NULL OR db.ltp > 0)
                ORDER BY db.account, db.symbol
            """).bindparams(
                today_ist=_today_ist, prev_batch_cutoff=_prev_batch_cutoff,
                snapshot_cutoff=_snapshot_cutoff,
            ))
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

    # base_pnl (prev_settlement_pnl) is already the batch-anchored value
    # (pnl_final / pnl_ranked CTE in the query above, positions-over-holdings
    # precedence) — no separate Python-side patch needed.
    rows: list[PositionRow] = [build_row_from_snapshot_raw(r) for r in raw_rows]
    rows = _auto_pair_positions(rows)
    try:
        async with async_session() as _gtt_session:
            _gtt_set = await _fetch_gtt_set(_gtt_session)
        rows = _annotate_gtt(rows, _gtt_set)
    except Exception as _gtt_exc:
        logger.warning(f"positions snapshot: gtt_set fetch failed: {_gtt_exc}")

    summary = build_summary_from_rows(rows)
    symbol_summary = build_symbol_summary_from_rows(rows)

    return PositionsResponse(
        rows=rows,
        summary=summary,
        refreshed_at=timestamp_display(),
        as_of=snap_captured_at,
        symbol_summary=symbol_summary,
    )

_ROW_COLS = [
    'account', 'tradingsymbol', 'exchange', 'product',
    'quantity', 'average_price', 'last_price',
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
    # for every matched row.
    'prev_close',
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
    # Split pairs into two parallel arrays for asyncpg-compatible UNNEST binding.
    # (account, symbol) IN :pairs fails asyncpg with tuple-of-tuples — UNNEST avoids it.
    accts = [p[0] for p in closed_pairs]
    syms  = [p[1] for p in closed_pairs]
    params: dict = {"kind": kind, "cutoff": cutoff, "accts": accts, "syms": syms}
    try:
        async with async_session() as session:
            result = await session.execute(_sql_text("""
                SELECT DISTINCT ON (account, symbol)
                       account, symbol, ltp AS ref_close
                FROM daily_book
                WHERE kind = :kind
                  AND ltp IS NOT NULL AND ltp > 0
                  AND captured_at < :cutoff
                  AND (account, symbol) IN (
                      SELECT a, s FROM UNNEST(CAST(:accts AS text[]), CAST(:syms AS text[])) AS t(a, s)
                  )
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
                    prev_close=ref_close,
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


def _with_baseline_diff_day_change(df: "pl.DataFrame") -> "pl.DataFrame":
    """Return `df` with `day_change_val` overridden to the baseline-diff Day
    P&L SSOT when `realised` + `unrealised` + `prev_settlement_pnl` are
    present. No-op (returns `df` unchanged) when those columns are absent —
    preserves the legacy per-row `day_change_val` for callers/tests that
    don't carry the full baseline columns (e.g. paper-trading synthetic
    rows).

    Uses `pnl_math.baseline_diff_day_pnl_expr_with_fallback` when a `pnl`
    column is also present, so rows where `realised`/`unrealised` are both
    exactly 0 (not populated) fall back to `pnl` as the realised leg —
    the SAME trigger `_row_baseline_diff_day_pnl` (positions_helpers.py)
    uses row-by-row, so the live-fetch summary and the mode='both' merge
    path (which rebuilds via `build_summary_from_rows`) can never disagree
    on the same underlying data. Falls back to the plain (no-pnl-fallback)
    expression when `pnl` is absent.
    """
    if not {'realised', 'unrealised'}.issubset(df.columns):
        return df
    from backend.api.algo.pnl_math import (
        baseline_diff_day_pnl_expr, baseline_diff_day_pnl_expr_with_fallback,
    )
    _base_col = 'prev_settlement_pnl' if 'prev_settlement_pnl' in df.columns else None
    if _base_col is None:
        df = df.with_columns(pl.lit(0.0).alias('prev_settlement_pnl'))
        _base_col = 'prev_settlement_pnl'
    if 'pnl' in df.columns:
        _expr = baseline_diff_day_pnl_expr_with_fallback(
            'realised', 'unrealised', 'pnl', _base_col
        )
    else:
        _expr = baseline_diff_day_pnl_expr('realised', 'unrealised', _base_col)
    return df.with_columns(_expr.alias('day_change_val'))


def _build_polars_summary(df: "pl.DataFrame") -> "pl.DataFrame":
    """Build a per-account + TOTAL summary DataFrame from the live-positions polars frame.

    `day_change_val` is the baseline-diff Day P&L SSOT, summed per account
    (see `_with_baseline_diff_day_change`) — NOT the legacy per-row
    apply_day_change_backstop value.

    The day_change_percentage denominator is Σ|close × qty| per account —
    the same formula the snapshot path uses via `build_summary_from_rows`.
    Returns a polars DataFrame with columns:
      account, pnl, day_change_val, day_change_percentage, day_prev_val
    """
    df = _with_baseline_diff_day_change(df)
    df = df.with_columns(
        (pl.col('prev_close') * pl.col('quantity')).abs().alias('_prev_val')
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


def _polars_df_to_structs(struct_cls, df: "pl.DataFrame") -> list:
    """Convert a polars summary DataFrame's rows into a list of msgspec
    Structs, coercing null → 0 (Polars fill_null upstream already handles
    most cases; this is a final defensive pass). Shared by the account-level
    (`PositionsSummaryRow`) and symbol-level (`PositionsSymbolSummaryRow`)
    rollup conversions in `_fetch()` — extracted to keep `_fetch()`'s own
    cyclomatic complexity under the project's D-grade gate.
    """
    return [
        struct_cls(**{k: (v if v is not None else 0) for k, v in r.items()})
        for r in df.to_dicts()
    ]


def _build_polars_symbol_summary(df: "pl.DataFrame") -> "pl.DataFrame":
    """Build a per-symbol (across accounts) + TOTAL summary DataFrame from the
    live-positions polars frame. Parallel rollup to `_build_polars_summary`,
    grouped by `tradingsymbol` instead of `account`. Returns a polars
    DataFrame with columns: tradingsymbol, pnl, day_change_val,
    day_change_percentage, day_prev_val.
    """
    df = _with_baseline_diff_day_change(df)
    df = df.with_columns(
        (pl.col('prev_close') * pl.col('quantity')).abs().alias('_prev_val')
    )
    sum_cols = [c for c in ('pnl', 'day_change_val', '_prev_val') if c in df.columns]
    if sum_cols and 'tradingsymbol' in df.columns:
        grouped = df.group_by('tradingsymbol').agg([pl.col(c).sum() for c in sum_cols])
    else:
        grouped = pl.DataFrame({'tradingsymbol': []})
    for col in ('pnl', 'day_change_val', '_prev_val'):
        if col not in grouped.columns:
            grouped = grouped.with_columns(pl.lit(0.0).alias(col))
    totals = pl.DataFrame([{
        'tradingsymbol': 'TOTAL',
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
    # day_change_val: only zero when pnl is also zero (break-even round-trip).
    # When abs(pnl) > 0.005 the backstop (apply_day_change_backstop Case 3)
    # has already restored dcv = pnl for this closed intraday row — preserve it
    # so the NavStrip P slot shows the realised gain/loss, not phantom zero.
    # Half-paisa threshold (0.005) matches _override_stale_close_from_snapshot.
    if 'day_change_val' in raw.columns:
        _pnl_raw = (
            raw['pnl'] if 'pnl' in raw.columns
            else _pd.Series(0.0, index=raw.index)
        )
        _pnl = _pd.to_numeric(_pnl_raw, errors='coerce').fillna(0)
        raw.loc[_flat_mask & (_pnl.abs() < 0.005), 'day_change_val'] = 0.0
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
    #
    # NOTE (2026-09-24, positions/holdings poll-only redesign): this
    # call is NOT the "between-poll tick patch" the redesign removed
    # from the frontend (`livePositionDayPnl` in nav.js) — it runs
    # INSIDE this poll (_fetch()) itself, once per poll, correcting
    # stale/wrong data that THIS poll's broker REST call returned,
    # using the ticker only as the correction source for that single
    # poll. Removing it reintroduces the 2026-06-22 incident and drops
    # the last-known-good fallback (positions_policy's consider_cache)
    # for Dhan/Groww rows still at last_price=0 after
    # backfill_market_data. Do not remove without an explicit, separate
    # operator decision — see holdings.py's equivalent call for the
    # symmetric holdings-side correction.
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
    symbol_summary_df = _build_polars_symbol_summary(df)

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
    summary = _polars_df_to_structs(PositionsSummaryRow, summary_df)
    symbol_summary = _polars_df_to_structs(PositionsSymbolSummaryRow, symbol_summary_df)
    stale_accts = sorted({r.account for r in rows if r.account_stale})
    return PositionsResponse(
        rows=rows,
        summary=summary,
        refreshed_at=timestamp_display(),
        stale_accounts=stale_accts,
        symbol_summary=symbol_summary,
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
    _cls = pd.to_numeric(raw.loc[sel, 'prev_close'], errors='coerce').fillna(0)
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

    Scope note (2026-09-24, positions/holdings poll-only redesign):
    an earlier pass of this redesign mistakenly removed this
    function's call sites in `_fetch()` and
    `background._fetch_positions_direct`, on the premise that it was
    the same "between-poll tick patch" mechanism the redesign
    intentionally removed from the frontend. That premise was wrong —
    this function runs INSIDE each poll, correcting that poll's own
    stale broker REST data using the ticker as the correction source;
    it is unrelated to the frontend's between-poll live-delta removal
    (`nav.js`'s `livePositionDayPnl`). The call sites were restored.
    This function is called from three places: `_fetch()` (live
    positions), `background._fetch_positions_direct` (NavStrip poll),
    and `_build_paper_positions_response` (paper positions, which
    have no broker book to poll at all — the ticker is their only
    live mark-to-market source).
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
    _cls = pd.to_numeric(raw.loc[_sel, 'prev_close'], errors='coerce').fillna(0)
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
        # Mirror the same additive delta onto `unrealised` — since Day P&L
        # now sources from `realised + unrealised` (baseline-diff SSOT),
        # not `pnl`, leaving `unrealised` stale here silently drops this
        # LTP correction from Day P&L even though `pnl` was fixed (regresses
        # the 2026-06-22 illiquid-MCX-options fix — see module history).
        # `realised` is untouched: the ticker only corrects LTP/mark-to-
        # market, never a realised trade.
        if 'unrealised' in raw.columns:
            _unrealised_current = pd.to_numeric(
                raw.loc[_sel, 'unrealised'], errors='coerce'
            ).fillna(0)
            raw.loc[_sel, 'unrealised'] = (_unrealised_current + _pnl_delta).where(
                _ltp > 0, raw.loc[_sel, 'unrealised']
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
    prev_pnl_kind_map: "dict[tuple[str, str], str] | None" = None,
    prev_pnl_qty_map: "dict[tuple[str, str], float] | None" = None,
) -> None:
    """Set `prev_settlement_pnl` on each row from yesterday's daily_book total_pnl.

    No-ops when `prev_pnl_map` is empty or `raw` is empty.
    Rows with no matching key in `prev_pnl_map` (positions opened today)
    keep None — the PositionRow default for that optional field.

    When `prev_pnl_kind_map` / `prev_pnl_qty_map` are supplied, routes the
    raw baseline through `pnl_math`-adjacent
    `positions_helpers._resolve_prev_settlement_pnl` — a holdings-kind
    baseline (the "holding sold into a CNC position" case) is only used
    when today's row is a genuine CNC sale from that holding, pro-rated by
    the actual sold quantity rather than the holding's full lifetime P&L
    (audit item #4). Omitting the two maps preserves the old unconditional
    behaviour (used by callers that haven't threaded the new maps through).
    """
    if not prev_pnl_map or raw.empty:
        return
    if 'prev_settlement_pnl' not in raw.columns:
        raw['prev_settlement_pnl'] = None
    from backend.api.routes.positions_helpers import _resolve_prev_settlement_pnl
    _kind_map = prev_pnl_kind_map or {}
    _qty_map = prev_pnl_qty_map or {}
    _has_product = 'product' in raw.columns
    _has_dsq = 'day_sell_quantity' in raw.columns
    for idx in raw.index:
        key = (str(raw.at[idx, 'account']), str(raw.at[idx, 'tradingsymbol']))
        if key not in prev_pnl_map:
            continue
        resolved = _resolve_prev_settlement_pnl(
            prev_pnl_map[key],
            _kind_map.get(key),
            _qty_map.get(key),
            product=raw.at[idx, 'product'] if _has_product else None,
            day_sell_qty=raw.at[idx, 'day_sell_quantity'] if _has_dsq else None,
        )
        raw.at[idx, 'prev_settlement_pnl'] = resolved


def _dict_to_position_row(r: dict) -> "PositionRow":
    """Build a PositionRow from a polars `to_dicts()` record.

    Fields in _NULLABLE_COLS are allowed to stay None; all other None
    values are coerced to 0 to satisfy the non-optional Struct fields.
    """
    return PositionRow(**{k: (v if v is not None or k in _NULLABLE_COLS else 0) for k, v in r.items()})


# Shared CTE fragment — the batch-anchored base_pnl (yesterday's total_pnl)
# lookup. Embedded (via string formatting, no f-string interpolation of
# untrusted data) into the two hot-path combined queries below
# (`_fetch_snapshot_close_map`, `_positions_snapshot`) so each stays a
# SINGLE round trip, and reused verbatim by the standalone
# `_fetch_baseline_pnl_map` helper. See that helper's docstring for the
# full staleness-fix + kind-precedence rationale.
#
# CONTRACT: every caller must define a `latest_batch(account, cutoff_ts,
# max_at)` CTE before including this fragment.
#
#   `cutoff_ts` — the per-account upper bound (exclusive) the baseline
#     batch must be strictly older than.
#   `max_at`    — the "display" batch's own `captured_at` (exclusive upper
#     bound too). Needed IN ADDITION to `cutoff_ts`, not instead of it —
#     belt-and-suspenders against any future caller whose `cutoff_ts` isn't
#     strictly derived from `max_at`'s own session boundary.
#
#   - `_fetch_baseline_pnl_map` / `_fetch_snapshot_close_map` (live-fetch
#     callers): "current" isn't sourced from daily_book at all (it's a
#     live broker fetch), so every account shares one fixed `cutoff_ts`
#     (today's 08:00 IST boundary) with `max_at` set equal to it (a no-op
#     bound). This is NOT immune to self-collision in general — it is
#     safe ONLY while a market segment is genuinely live (a live-fetch
#     "current" during a real session is never equal to the most recent
#     daily_book close-reset row, so no collision). Calling these off a
#     fixed `today_08` cutoff while the market is FULLY CLOSED (weekend/
#     holiday/overnight) DOES self-collide — "today" isn't a live trading
#     session, so the most recent close-reset row's `captured_at` is
#     always < `today_08`, making it both the live-fetch "current" state
#     (broker returns the same frozen prior-session data) AND the
#     resolved "baseline" — verified empirically against real prod
#     `daily_book` data (2026-09 Day P&L audit round 3, item #3
#     follow-up). Callers off market hours must gate on
#     `_any_segment_open()` and use a closed-hours snapshot reader
#     instead — see `backend/api/routes/auth.py:_auth_nav_closed_hours_fallback`.
#   - `_positions_snapshot` (closed-hours reader): BOTH "current" (the
#     displayed batch) and "baseline" are daily_book batches, so
#     `cutoff_ts` must be anchored to the DISPLAYED batch's own trading
#     SESSION — derived purely from `latest_batch.max_at` (its own
#     `captured_at`), NEVER from the `date` column. `date` is stamped by
#     the writer from the wall-clock calendar day at write time, not the
#     trading session captured — a close_settled write that fires just
#     after midnight IST (e.g. MCX close 23:30 + a settled-offset that
#     crosses midnight) gets `date` = the NEXT calendar day, so a
#     `date`-based cutoff_ts sits a full day later than it should and lets
#     an EARLIER SAME-SESSION row (e.g. that day's own 16:00 IST NSE
#     close_settled write) slip through the `captured_at < cutoff_ts`
#     test and get picked as "baseline" — base_pnl ≈ current total_pnl,
#     collapsing Day P&L to ~0 (2026-09 Day P&L audit round 3, item #1;
#     reproduced on real prod data with a fake +307,200 on GOLD and an
#     exact 0 on a real weekend). The captured_at-derived boundary
#     (`date_trunc('day', (captured_at AT TIME ZONE 'Asia/Kolkata') -
#     INTERVAL '8 hours') + INTERVAL '8 hours'`) is immune to whatever the
#     `date` column says — it always resolves to the 08:00 IST start of
#     the [08:00, next 08:00) trading-day window `captured_at` itself
#     falls into, correctly excluding every row from that same window
#     (however late it was written) and landing on the strictly prior
#     trading day's close-reset row instead.
#
# `pnl_ranked` additionally excludes flat (`qty = 0`) rows — a fully
# closed historical row is not a "genuine continuation" of a position and
# must not serve as tomorrow's baseline for an unrelated fresh re-entry
# (audit item #5). `pnl_final` carries `kind` + `qty` alongside `total_pnl`
# so callers can detect a holdings-sourced baseline (see
# `positions_helpers._resolve_prev_settlement_pnl`) and gate/pro-rate it
# by the actual sold quantity instead of blindly promoting a holding's
# full lifetime P&L onto an unrelated same-symbol positions row (audit
# item #4).
_BASELINE_PNL_CTE_SQL = """
    latest_pnl_batch AS (
        SELECT daily_book.account, daily_book.kind,
               MAX(daily_book.captured_at) AS max_at
        FROM daily_book
        JOIN latest_batch ON latest_batch.account = daily_book.account
        WHERE daily_book.kind IN ('positions', 'holdings')
          AND daily_book.ltp IS NOT NULL AND daily_book.ltp > 0
          AND daily_book.captured_at < latest_batch.cutoff_ts
          AND daily_book.captured_at < latest_batch.max_at
        GROUP BY daily_book.account, daily_book.kind
    ),
    pnl_ranked AS (
        SELECT daily_book.account, daily_book.symbol, daily_book.kind,
               daily_book.total_pnl, daily_book.qty,
               ROW_NUMBER() OVER (
                   PARTITION BY daily_book.account, daily_book.symbol
                   ORDER BY CASE daily_book.kind WHEN 'positions' THEN 0 ELSE 1 END
               ) AS rn
        FROM daily_book
        JOIN latest_pnl_batch lpb
          ON daily_book.account = lpb.account AND daily_book.kind = lpb.kind
          AND daily_book.captured_at = lpb.max_at
        WHERE daily_book.kind IN ('positions', 'holdings')
          AND daily_book.total_pnl IS NOT NULL
          AND daily_book.qty IS NOT NULL AND daily_book.qty != 0
    ),
    pnl_final AS (
        SELECT account, symbol, total_pnl, kind, qty
        FROM pnl_ranked
        WHERE rn = 1
    )
"""


async def _fetch_baseline_pnl_map(cutoff) -> dict[tuple[str, str], float]:
    """Return base_pnl (yesterday's total_pnl) per (account, tradingsymbol),
    bound to the exact most-recent trading-day batch PER ACCOUNT PER KIND
    before `cutoff` — never an arbitrarily old stale row.

    Replaces the old unbounded `captured_at < today_08 ORDER BY DESC LIMIT 1`
    per-symbol lookup (which could walk back through days/weeks of history
    for a symbol that dropped out of recent batches) and the old 7-day
    `prev_batch` window in `_positions_snapshot` — both shared this same
    staleness risk. Symbols absent from the most-recent batch correctly
    default to base_pnl = 0 (no baseline row → new position) rather than
    reaching back further.

    kind IN ('positions', 'holdings') with a PER-KIND latest-batch anchor
    (not a single cross-kind MAX(captured_at)) — verified against
    production data that positions/holdings snapshots for the same account
    land milliseconds apart within one daily_snapshot run but are never
    exactly equal, so a single shared anchor would silently drop one kind's
    rows. When both kinds have a matching (account, symbol) row (the
    "holding sold into a CNC position" case — see CLAUDE.md "Holdings sold
    → P&L splits"), the 'positions' row wins: today's CNC row already
    contains the correct incremental base, whereas the stale 'holdings' row
    reflects yesterday's full holding.

    Returns {} on any DB error (safe to call unconditionally).
    """
    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text

    out: dict[tuple[str, str], float] = {}
    try:
        async with async_session() as session:
            result = await session.execute(_sql_text(f"""
                WITH latest_batch AS (
                    SELECT DISTINCT account,
                           CAST(:baseline_cutoff AS timestamptz) AS cutoff_ts,
                           CAST(:baseline_cutoff AS timestamptz) AS max_at
                    FROM daily_book
                    WHERE kind IN ('positions', 'holdings')
                ),
                {_BASELINE_PNL_CTE_SQL}
                SELECT account, symbol, total_pnl
                FROM pnl_ranked
                WHERE rn = 1
            """).bindparams(baseline_cutoff=cutoff))
            for account, symbol, total_pnl in result.all():
                key = (str(account), str(symbol))
                out[key] = float(total_pnl)
    except Exception as exc:
        logger.warning(f"_fetch_baseline_pnl_map failed: {exc}")
        return {}
    return out


async def _fetch_snapshot_close_map(
    raw: pd.DataFrame,
    cutoff,
) -> tuple[dict, dict, dict, dict]:
    """Query daily_book for the most-recent settlement LTP per (account, symbol).

    Returns the latest entry before today 08:00 IST — the prior-session
    settlement LTP.  Weekend/holiday startup snapshot writes are skipped by
    ``_task_daily_snapshot``; trading-day restart snapshots (00:30–08:00 IST)
    capture the correct MCX settlement LTP from the frozen tick buffer, so
    the single-query path is always correct.

    Returns ``(snapshot_map, prev_pnl_map, prev_pnl_kind_map, prev_pnl_qty_map)``.
    ``snapshot_map`` / ``prev_pnl_map`` are ``dict[tuple[str, str], float]``;
    ``prev_pnl_kind_map`` is ``dict[tuple[str, str], str]`` ('positions' /
    'holdings' — which kind the baseline came from); ``prev_pnl_qty_map`` is
    ``dict[tuple[str, str], float]`` (that winning row's `qty`). All keyed by
    ``(account, tradingsymbol)``. On any DB error logs a warning and returns
    four empty dicts.

    ``snapshot_map`` (ref_close) sourcing is UNCHANGED — governed by the
    documented "close_price / ltp invariant — DO NOT CHANGE" rule in
    CLAUDE.md (unbounded per-symbol DISTINCT ON ... ORDER BY captured_at
    DESC). ``prev_pnl_map`` (base_pnl) is sourced from the batch-anchored
    ``_BASELINE_PNL_CTE_SQL`` fragment (same logic as the standalone
    ``_fetch_baseline_pnl_map`` helper — see its docstring for the full
    staleness-fix + kind='positions'/'holdings' precedence rationale) so a
    symbol absent from the most-recent per-account trading-day batch
    correctly defaults to base_pnl = 0 instead of reaching back to an
    arbitrarily old row. ``prev_pnl_kind_map`` / ``prev_pnl_qty_map`` let
    the caller (`_backfill_prev_settlement_pnl`) detect a holdings-sourced
    baseline and gate/pro-rate it via
    `positions_helpers._resolve_prev_settlement_pnl` (audit item #4) rather
    than blindly promoting a holding's full lifetime P&L onto an unrelated
    same-symbol positions row. Both halves are combined into ONE query (a
    FULL OUTER JOIN of the two independently-keyed result sets) so this
    remains a single round trip, same as before this fix.
    """
    from datetime import datetime as _dt
    from sqlalchemy import text as _sql_text
    from zoneinfo import ZoneInfo

    now_ist = _dt.now(ZoneInfo("Asia/Kolkata"))
    today_08 = now_ist.replace(hour=8, minute=0, second=0, microsecond=0)

    snapshot_map: dict[tuple[str, str], float] = {}
    prev_pnl_map: dict[tuple[str, str], float] = {}
    prev_pnl_kind_map: dict[tuple[str, str], str] = {}
    prev_pnl_qty_map: dict[tuple[str, str], float] = {}
    try:
        from backend.api.database import async_session
        async with async_session() as session:
            result = await session.execute(_sql_text(f"""
                    WITH latest_batch AS (
                        SELECT DISTINCT account,
                               CAST(:today_08 AS timestamptz) AS cutoff_ts,
                               CAST(:today_08 AS timestamptz) AS max_at
                        FROM daily_book
                        WHERE kind IN ('positions', 'holdings')
                    ),
                    {_BASELINE_PNL_CTE_SQL},
                    snapshot_close AS (
                        SELECT DISTINCT ON (account, symbol)
                               account, symbol, ltp AS ref_close
                        FROM daily_book
                        WHERE kind = 'positions'
                          AND ltp IS NOT NULL AND ltp > 0
                          AND captured_at < :today_08
                        ORDER BY account, symbol, captured_at DESC
                    )
                    SELECT
                        COALESCE(sc.account, pf.account) AS account,
                        COALESCE(sc.symbol, pf.symbol)   AS symbol,
                        sc.ref_close,
                        pf.total_pnl,
                        pf.kind,
                        pf.qty
                    FROM snapshot_close sc
                    FULL OUTER JOIN pnl_final pf
                      ON pf.account = sc.account AND pf.symbol = sc.symbol
                """).bindparams(today_08=today_08))
            for account, symbol, ref_close, total_pnl, pnl_kind, pnl_qty in result.all():
                key = (str(account), str(symbol))
                if ref_close is not None:
                    snapshot_map[key] = float(ref_close)
                if total_pnl is not None:
                    prev_pnl_map[key] = float(total_pnl)
                if pnl_kind is not None:
                    prev_pnl_kind_map[key] = str(pnl_kind)
                if pnl_qty is not None:
                    prev_pnl_qty_map[key] = float(pnl_qty)
    except Exception as e:
        logger.warning(f"daily_book close-override query failed: {e}")
        return {}, {}, {}, {}

    return snapshot_map, prev_pnl_map, prev_pnl_kind_map, prev_pnl_qty_map


def _patch_close_from_snapshot_map(
    raw: pd.DataFrame,
    snapshot_map: dict,
) -> list:
    """Apply snapshot LTP values to ``raw`` row-by-row.

    For every row matched in *snapshot_map*:
    - Sets ``prev_close`` unconditionally (the frozen prior-session
      settlement price consumed by the frontend formula).
    - Replaces ``prev_close`` only when the snapshot LTP diverges from
      the current value by more than a tiny epsilon (0.005) — protects
      against rounding noise.

    Returns the list of indices where ``prev_close`` was actually patched.
    """
    patched_idx: list = []
    for idx in raw.index:
        key = (str(raw.at[idx, 'account']), str(raw.at[idx, 'tradingsymbol']))
        snap_ltp = snapshot_map.get(key)
        if snap_ltp is None:
            continue
        # Set prev_close for ALL matched rows regardless of epsilon check —
        # this is the frozen prior-session settlement price consumed by the
        # frontend's (ltp − prev_close) × qty formula.
        try:
            current_close = float(raw.at[idx, 'prev_close']) if pd.notna(raw.at[idx, 'prev_close']) else 0.0
        except (TypeError, ValueError):
            current_close = 0.0
        raw.at[idx, 'prev_close'] = snap_ltp
        if abs(snap_ltp - current_close) <= 0.005:
            continue
        patched_idx.append(idx)
    return patched_idx


async def _apply_second_pass_fallback(raw: pd.DataFrame) -> list:
    """Fallback for rows whose ``prev_close`` is still 0 after the first pass.

    Reads ``daily_book.ltp`` directly because the snapshot at 23:45 IST is
    captured before the MCX BHAV publishes at 00:15 IST, so the stored
    ``prev_close`` at that time equals last-traded ≈ ltp (stale).
    ``daily_book.ltp`` in the settlement snapshot IS the settlement price.

    Only fires when at least one row still has ``prev_close == 0.0``.
    Returns the list of indices patched.  On any DB error logs a warning and
    returns ``[]``.
    """
    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text

    patched_idx2: list = []
    zero_mask = raw['prev_close'] == 0.0
    if not zero_mask.any():
        return patched_idx2

    zero_indices = raw.index[zero_mask].tolist()
    syms_needing_fallback = list({str(raw.at[i, 'tradingsymbol']) for i in zero_indices})
    try:
        async with async_session() as session:
            result2 = await session.execute(_sql_text("""
                SELECT DISTINCT ON (account, symbol) account, symbol, ltp AS prev_close
                FROM daily_book
                WHERE kind = 'positions'
                  AND ltp IS NOT NULL AND ltp > 0
                  AND symbol = ANY(:syms)
                ORDER BY account, symbol, captured_at DESC
            """), {"syms": syms_needing_fallback})
            fallback_map: dict[tuple[str, str], float] = {
                (str(acc), str(sym)): float(pc)
                for acc, sym, pc in result2.all()
            }
        for idx in zero_indices:
            key2 = (str(raw.at[idx, 'account']), str(raw.at[idx, 'tradingsymbol']))
            fallback_close = fallback_map.get(key2)
            if fallback_close is None:
                continue
            raw.at[idx, 'prev_close'] = fallback_close
            patched_idx2.append(idx)
    except Exception as e:
        logger.warning(f"positions: close-override second-pass query failed: {e}")
    return patched_idx2


async def _override_stale_close_from_snapshot(raw: pd.DataFrame) -> None:
    """Set ``prev_close`` from the most-recent daily_book snapshot LTP per
    (account, tradingsymbol). When found, recomputes the decomposed
    day_change_val so the row reflects the actual move since the prior
    session's authoritative close.

    Uses ``daily_book.ltp`` directly — the actual settlement LTP captured
    at session end and the canonical prior-session reference price.

    All matched rows have ``prev_close`` set unconditionally."""
    if raw.empty or 'tradingsymbol' not in raw.columns or 'account' not in raw.columns:
        return

    # Ensure prev_close column exists; do NOT clobber broker-supplied values.
    # Old code wrote to a separate 'previous_close' scratch column; after the
    # rename to 'prev_close', zeroing unconditionally would wipe the
    # broker-supplied prior-close value for rows that get no snapshot match.
    if 'prev_close' not in raw.columns:
        raw['prev_close'] = 0.0

    if not (raw["account"].notna() & raw["tradingsymbol"].notna()).any():
        return

    # Cutoff = last passed 08:00 IST boundary (the prev_close invariant).
    # Use NON-MCX gate; MCX gate has the same reset time (08:00 IST) so one
    # cutoff covers all exchanges.  Both daily_book snapshots (NSE ~15:45 and
    # MCX ~00:15) fall before the 08:00 boundary and are included by this query.
    from backend.api.helpers.exchange_clock import settlement_cutoff_for
    today_ist_cutoff = await settlement_cutoff_for("NON-MCX")

    (snapshot_map, prev_pnl_map, prev_pnl_kind_map,
     prev_pnl_qty_map) = await _fetch_snapshot_close_map(raw, today_ist_cutoff)
    patched_idx = _patch_close_from_snapshot_map(raw, snapshot_map)
    patched_idx2 = await _apply_second_pass_fallback(raw)

    # Backfill prev_settlement_pnl — yesterday's total_pnl for each position
    # that exists in the daily_book snapshot.  Rows opened today have no entry
    # and remain None (the PositionRow default). Holdings-sourced baselines
    # are gated/pro-rated by kind+qty — see _backfill_prev_settlement_pnl.
    _backfill_prev_settlement_pnl(raw, prev_pnl_map, prev_pnl_kind_map, prev_pnl_qty_map)

    all_patched = patched_idx + patched_idx2
    if not all_patched:
        return

    # Recompute day_change_val on patched rows only — same decomposed
    # formula broker_apis uses, kept in sync. Non-patched rows keep
    # broker_apis' value untouched so backfilled Dhan rows (where the
    # backfill computes day_chg = (LTP - close) × qty as a fallback for
    # missing intraday fields) stay correct.
    # (Uses module-level _INTRADAY_FIELDS via _compute_day_change_val.)
    _sel = pd.Index(all_patched)
    _ltp = pd.to_numeric(raw.loc[_sel, 'last_price'], errors='coerce').fillna(0)
    _cls = pd.to_numeric(raw.loc[_sel, 'prev_close'], errors='coerce').fillna(0)
    _dcv_calc = _compute_day_change_val(raw, _sel)
    raw.loc[_sel, 'day_change_val'] = _dcv_calc.where(_ltp > 0, raw.loc[_sel, 'day_change_val'])
    raw.loc[_sel, 'day_change'] = _ltp - _cls
    # Recompute day_change_percentage + pnl_percentage on patched rows.
    # prev_close was set above and day_change_val just recomputed;
    # without this step the percentage columns lag the absolute columns
    # (same fix applied to _override_stale_ltp_from_ticker above).
    recompute_row_percentages(raw, _sel)
    if patched_idx:
        logger.info(f"positions: close-override patched {len(patched_idx)}/{len(raw)} rows from daily_book")
    if patched_idx2:
        logger.info(
            f"positions: close-override second-pass (MCX option fallback) patched "
            f"{len(patched_idx2)}/{len(raw)} rows from daily_book.prev_close"
        )


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
    if 'last_price' in raw.columns and 'prev_close' in raw.columns:
        _ltp_s  = pd.to_numeric(raw['last_price'],  errors='coerce').fillna(0)
        _cls_s  = pd.to_numeric(raw['prev_close'], errors='coerce').fillna(0)
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
    symbol_summary = build_symbol_summary_from_rows(rows)
    return PositionsResponse(
        rows=rows, summary=summary, refreshed_at=timestamp_display(),
        symbol_summary=symbol_summary,
    )


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
        DEFAULT_RISK_FREE,
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
            sigma = implied_vol(row.last_price, S, K, T_years, DEFAULT_RISK_FREE, p["opt_type"])
            g = greeks(S, K, T_years, DEFAULT_RISK_FREE, sigma, p["opt_type"])
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
        await _asyncio.to_thread(_enrich_position_greeks, resp.rows)   # stamp underlying_ltp on snapshot rows
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
