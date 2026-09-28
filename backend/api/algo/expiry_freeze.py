"""Expiry-day-final snapshot freeze — SSOT for a single operator rule
(verbatim, 2026-09):

    "gold and goldm should be present until the next market open day, as
    they are snapshot of at the end of expiry day, they should be
    refreshed only at 8:00 AM on the next market open day. It is like
    snapshot taken on the last minute of expiry day."

IMPORTANT — this is NOT a new freeze/refresh mechanism
--------------------------------------------------------
Positions / holdings / NavStrip freezing at market close and refreshing at
the next 08:00 IST open is ALREADY the existing, working, general design
(CLAUDE.md: "Market daily window — 08:00–23:31 IST", the "Staleness
indicator freeze rule", `positions.py`'s `_SESSION_ANCHOR_CUTOFF_TS_SQL`).
The mechanism that already does this for the SERVING side is
`backend.api.helpers.snapshot_gate.closed_hours_or_broker()`:  it calls the
LIVE broker fetch whenever ANY segment is open, and only falls back to the
`daily_book` snapshot reader (`positions.py:_positions_snapshot`) once
EVERY segment is closed. The moment the market reopens, the route switches
back to the live broker fetch — which naturally stops returning an expired
contract — so "refresh at next market open" already happens for free, with
zero code in this module, for the common case.

What THIS module actually fixes is two narrower, genuinely separate bugs
that sit "underneath" that existing mechanism and can corrupt what it has
available to serve:

  (a) A completely UNRELATED data-hygiene job — `daily_snapshot.py`'s
      generic 7-day orphan sweep (`_delete_prior_orphan_positions` /
      `_delete_orphan_positions`) — deletes ANY `daily_book` position row
      whose symbol the broker stops returning, using whatever OTHER
      symbol's fresh write happens to exist that day as its prune anchor.
      That is correct for an ORDINARY closed position (squared off while
      the contract still had time left) but WRONG for an expired contract:
      it physically destroys the row `closed_hours_or_broker`'s snapshot
      reader would otherwise still have available to serve throughout the
      closed window. This module supplies the missing "is this row still
      inside its freeze window" predicate so the sweep can defer to it
      instead of deleting on its own unrelated per-account anchor.

      WHY THIS IS A HARD STRUCTURAL CONSTRAINT, NOT A TUNABLE PREFERENCE
      (operator, 2026-09): "why freeze works — the API and websocket gets
      closed. the feed comes only from db snapshot." During the closed
      window (23:31→08:00 IST, CLAUDE.md's "Market daily window"),
      KiteTicker's WebSocket has unsubscribed and there is no active
      broker polling AT ALL — `daily_book` is not "the preferred source
      for now, live is still available as a fallback"; it is the ONLY
      data that exists, full stop, until the next session's broker
      connection actually comes back at 08:00 IST. This is precisely why
      the orphan sweep must DEFER to the freeze window instead of
      opportunistically cleaning up whenever it happens to run: during
      that window, deleting a row doesn't just make the data "less
      fresh" — it destroys the ONLY copy, with nothing live to
      reconcile against or fall back to until the market reopens. "Just
      refresh more often" is not an available option during this window;
      there is nothing to refresh FROM.

  (b) `positions.py:_positions_snapshot`'s `latest_batch` CTE selects only
      ONE `captured_at` timestamp per ACCOUNT (`JOIN latest_batch lb ON
      db.account = lb.account AND db.captured_at = lb.max_at`). An account
      holding BOTH a still-actively-traded symbol (fresh batch every day)
      AND an expired-but-still-in-its-freeze-window symbol (older batch,
      frozen at expiry) will only ever have the fresher batch selected —
      the expired symbol's row is silently masked EVERY night, even though
      it still physically exists in the DB and is still inside its freeze
      window. This is a genuine, everyday bug independent of any
      market-open/closed toggle. See "Callers" below for the fix.

Two confirmed production failure modes (2026-09 investigation):
  1. GOLD/GOLDM legs get swept the moment ANY other symbol in the same
     account (e.g. CRUDEOIL) writes a fresh row, because the generic prune
     has no concept of "this particular symbol's own contract has expired
     vs. simply stopped trading."
  2. An account that goes fully flat writes NOTHING for 'positions' that
     day (broker returns an empty list ⇒ zero rows to upsert), so the
     prune's own anchor (`MAX(captured_at)` for today) never advances,
     and a stale row is *never* pruned — frozen forever instead of frozen
     until the correct 8AM-next-market-open boundary. See
     `daily_snapshot.py:_write_confirmed_empty_marker` for the fix (a
     'positions_empty' sentinel kind that gives the prune a real anchor
     on a confirmed-flat day, without a schema migration).

Why 08:00 IST, not midnight (design precedent)
-----------------------------------------------
This mirrors two existing conventions in this exact codebase, applied to
a new surface — the "trading day" here is never the bare calendar day:
  - `frontend/src/lib/dateFormat.js:tradingSessionDateIST()` (commit
    8b47be7b, same session) — the FRONTEND'S theoretical Exp P&L valuation
    concern for an expired-but-still-held leg.
  - `backend/api/routes/positions.py:_SESSION_ANCHOR_CUTOFF_TS_SQL` — the
    BACKEND'S existing precedent for deriving a session boundary from
    08:00 IST (not the wall-clock `date` column, not midnight), because a
    settlement write can land just after midnight IST (MCX close 23:30 +
    ~15 min settled offset ≈ 00:00 next calendar day) and must still count
    as the PRIOR trading day's own session.
This module is the third, backend-side, DATA-PERSISTENCE application of
the same 08:00 IST convention: which `daily_book` position rows exist and
get served as "current" at all (as opposed to the frontend file's
theoretical-valuation concern, or the read-time session-anchor concern in
positions.py's Day-P&L baseline query). All three independently converge
on the same boundary by design, not by coincidence.

Expired vs. closed — the distinction this module exists to draw
------------------------------------------------------------------
  - EXPIRED  : the contract's OWN expiry date has passed AND the row's
               last capture happened during that contract's own expiry-day
               trading session. Gets the freeze treatment below.
  - CLOSED   : the operator/algo squared off the position while the
               contract still had time left before its own expiry (e.g.
               ZG0790's GOLD/GOLDM legs in the 2026-09 investigation,
               which stopped appearing in the daily fetch roughly a week
               before that contract's real expiry — confirmed via
               `parse_tradingsymbol`). This is an ORDINARY closed position;
               it keeps using the EXISTING 7-day sweep, unmodified, and
               must NEVER be swept up into the freeze path by accident —
               conflating the two would silently resurrect every position
               a trader has ever closed early, which is a much larger
               (and wrong) blast radius than the bug being fixed here.

Callers
-------
  `daily_snapshot.py` (fix (a) above) — `expiry_status(...) == "frozen"`
      protects a candidate row from both `_delete_orphan_positions`
      (same-day sweep) and `_delete_prior_orphan_positions` (7-day sweep).
      This is the PRIMARY fix — it stops the row from being destroyed so
      `closed_hours_or_broker`'s existing snapshot reader still has it to
      serve throughout the closed window, exactly like every other frozen
      position/holding already does.

  `positions.py` (fix (b) above) — `expiry_status(...)` additionally
      gates `_positions_snapshot`'s per-account single-batch join:
      "frozen" rows are unioned into the response even when a different,
      fresher batch for the SAME account (a different, still-live symbol)
      would otherwise mask them via the `latest_batch` CTE. This is
      necessary every night for a mixed account, independent of the
      market-open/closed toggle.

      The complementary "refresh_eligible → exclude" check is a
      DEFENSIVE BACKSTOP, not the primary refresh mechanism — the primary
      mechanism is `closed_hours_or_broker` switching back to the live
      broker fetch the moment any segment reopens (which naturally stops
      returning an expired contract, no code here required). The backstop
      only matters for two edge cases the primary mechanism doesn't cover
      on its own: (1) a broker outage DURING market-open hours falls back
      to this same snapshot reader (`source='snapshot-fallback'` in
      `closed_hours_or_broker`) — without the exclusion, an already-past-
      its-boundary row could resurface during that fallback; (2) if the
      daily_snapshot prune job is delayed or fails for any reason, this
      stops the row from being served indefinitely instead of just until
      the next prune run. Per CLAUDE.md's "Staleness indicator freeze
      rule", the row is excluded outright rather than shown with a
      staleness marker — past its own refresh boundary the correct state
      is "this position no longer exists", not "degraded data for an
      existing position" (which is what `stale_accounts` denotes
      elsewhere in this route); marking it stale instead of removing it
      would incorrectly imply the ACCOUNT is degraded when only this one
      already-obsolete row is affected.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# Symbol used for the confirmed-empty sentinel row written by
# daily_snapshot.py — never a real tradingsymbol, so parse_tradingsymbol()
# always returns None for it (excluded from every check in this module by
# construction, no special-case needed).
EMPTY_MARKER_SYMBOL = "__EMPTY__"
EMPTY_MARKER_KIND = "positions_empty"


def session_date_of(captured_at: datetime) -> date:
    """Return the trading-SESSION calendar date (the `[08:00 IST, next
    08:00 IST)` window) that *captured_at* falls into.

    Mirrors `positions.py:_SESSION_ANCHOR_CUTOFF_TS_SQL`'s date arithmetic
    exactly (shift back 8h, truncate to day) so a row written just after
    midnight IST during MCX settlement is attributed to the PRIOR trading
    day's session — the same session as its own earlier same-day 23:4x
    write, not the next calendar day. Implemented in Python (not by
    calling out to SQL) because this function operates on `datetime`
    objects already loaded from a query result, not against a live table.
    """
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    ist_dt = captured_at.astimezone(_IST)
    shifted = ist_dt - timedelta(hours=8)
    return shifted.date()


def expiry_if_closed_on_own_expiry_day(symbol: str, captured_at: datetime) -> Optional[date]:
    """Return the contract's expiry `date` when *symbol* is a parseable
    F&O contract AND this row's own capture SESSION date (see
    `session_date_of`) equals that expiry date — i.e. this is genuinely
    the LAST snapshot taken during the contract's own expiry-day session,
    matching the operator's "snapshot taken on the last minute of expiry
    day" framing exactly.

    Returns None (not a frozen candidate) when:
      - *symbol* doesn't parse as an F&O contract (equity, the
        `EMPTY_MARKER_SYMBOL` sentinel, or an unrecognised shape) — no
        expiry concept applies; ordinary sweep rules govern unchanged.
      - the row's session date is BEFORE the contract's real expiry — the
        position was squared off by normal trading while time was still
        left on the contract. This is the "closed, not expired" case
        (see module docstring) — explicitly NOT frozen; the existing
        7-day sweep prunes it exactly as before, no behaviour change.
      - the row's session date is AFTER the expiry date — shouldn't occur
        in practice (a broker wouldn't keep reporting an expired contract
        past its own expiry day) but guarded defensively as non-frozen
        rather than raising, since a parse mismatch here must never block
        the surrounding prune/serve logic.

    Expiry-date accuracy note: delegates to the codebase's existing
    symbol-expiry SSOT, `backend.api.algo.derivatives.parse_tradingsymbol`.
    Its MCX/CDS OPTIONS branch (CE/PE) resolves the correct last-Friday
    commodity-options convention via `_monthly_expiry`/`is_mcx_underlying`
    — GOLD/GOLDM contracts in the 2026-09 incident are options, so this is
    the accurate branch. Its FUTURES branch is a known, already-documented
    approximation (`_last_thursday`) since Kite's per-commodity futures
    expiry day varies and isn't derivable from the symbol text alone; a
    futures row that happens to hit this approximate boundary incorrectly
    fails safe toward "not frozen" (ordinary sweep), never toward
    "frozen forever" — under-protecting a handful of futures rows is a far
    smaller risk than over-protecting and permanently masking a real
    closed position.
    """
    if not symbol or symbol == EMPTY_MARKER_SYMBOL:
        return None
    from backend.api.algo.derivatives import parse_tradingsymbol
    parsed = parse_tradingsymbol(symbol)
    if not parsed or not parsed.get("expiry"):
        return None
    expiry: date = parsed["expiry"]
    if session_date_of(captured_at) == expiry:
        return expiry
    return None


async def next_market_open_ist(after_date: date, exchange: str = "MCX") -> datetime:
    """Return the tz-aware 08:00 IST instant of the next trading day for
    *exchange* STRICTLY after *after_date*.

    This is the operator's own refresh boundary, verbatim: "refreshed
    only at 8:00 AM on the next market open day" — deliberately the next
    MARKET-OPEN day, not the next calendar day, so a Friday expiry (e.g.
    GOLDM26SEP, real incident: expired Friday 2026-09-25) stays frozen
    through Saturday, Sunday, and any holiday Monday, only refreshing at
    the following genuine trading session's 08:00 IST open (in that real
    case: Monday 2026-09-28 08:00 IST) — never at Saturday 08:00, which
    would be a bare "next calendar day" boundary and is explicitly wrong
    per the operator's own wording.

    Reuses the existing DB-backed holiday calendar
    (`backend.api.persistence.holidays_store.get_or_fetch_holidays`) —
    the canonical per-exchange holiday set — rather than hand-rolling a
    parallel calendar. Weekend skip is trivial calendar arithmetic, not
    duplicated business logic. Also consults `market_special_sessions`
    (CLAUDE.md: "highest precedence, by date/time") so an operator-defined
    special trading day (e.g. a Diwali Muhurat weekend session) is
    correctly treated as a valid market-open day rather than skipped.

    Bounded to a 10-day forward scan as a defensive guard against a
    corrupted/empty holiday cache looping forever; on ANY lookup failure
    this fails open to the first non-weekend calendar day found so far —
    mirroring `_exchange_clock._is_market_day_today()`'s own fail-open
    convention elsewhere in this codebase (never block a serving/prune
    path on a holiday-calendar hiccup).
    """
    from backend.api.persistence.holidays_store import get_or_fetch_holidays
    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text

    d = after_date + timedelta(days=1)
    for _ in range(10):
        try:
            is_weekend = d.weekday() >= 5  # Sat=5, Sun=6
            special_open = False
            if is_weekend:
                async with async_session() as session:
                    row = await session.execute(_sql_text(
                        "SELECT 1 FROM market_special_sessions "
                        "WHERE exchange = :exch AND date = :d LIMIT 1"
                    ), {"exch": exchange, "d": d})
                    special_open = row.first() is not None
            if is_weekend and not special_open:
                d += timedelta(days=1)
                continue
            holidays = await get_or_fetch_holidays(exchange, year=d.year)
            if d in holidays and not special_open:
                d += timedelta(days=1)
                continue
            break
        except Exception as exc:
            logger.warning(
                "next_market_open_ist: holiday/special-session lookup failed "
                "for %s (exchange=%s) — failing open: %s", d, exchange, exc,
            )
            break
    return datetime(d.year, d.month, d.day, 8, 0, tzinfo=_IST)


async def is_live_row_past_freeze_window(
    symbol: str, exchange: "str | None", now_ist: datetime,
) -> bool:
    """Live-broker-fetch-path counterpart to `expiry_status` — fixes the
    2026-09-29 incident where account ZJ6294's LIVE `/api/positions` fetch
    kept returning expired MCX GOLDM SEP option contracts (e.g.
    `GOLDM26SEP148000PE`, real expiry Friday 2026-09-25) as non-zero-qty
    positions with real (and wrong) attached P&L, well past every freeze
    boundary — even though `daily_book` had no GOLDM row newer than that
    Friday's settlement snapshot, and the row's own `price_source:
    "snapshot_settled"` / `last_price_stale: true` fields already showed
    the system knew the pricing was stale.

    This module's own docstring states the design's original assumption:
    `closed_hours_or_broker()`'s live-fetch path "naturally stops
    returning an expired contract" once the market reopens, so the
    expiry-freeze machinery above (`expiry_if_closed_on_own_expiry_day`,
    `expiry_status`) was only ever wired into the DB-SNAPSHOT serving path
    (`positions.py:_positions_snapshot`), never into the LIVE broker-fetch
    path (`positions.py:_fetch()`). That assumption is FALSE — Kite is
    still reporting these contracts live. This function answers a
    narrower question than `expiry_status`: "should this (symbol,
    exchange) still be appearing as a live position AT ALL, right now" —
    for a row with NO `daily_book` capture-session history at all (a
    live-fetched row is a genuine "right now" broker read, not a
    persisted snapshot being reclassified). Deliberately a SEPARATE
    function rather than a reuse of `expiry_if_closed_on_own_expiry_day`
    for that reason — that function's `captured_at`/"own expiry-day
    session" requirement has no live-fetch equivalent, there is no prior
    capture session to compare against here.

    Fast path (no I/O): a symbol that doesn't parse as F&O, or whose
    parsed expiry hasn't passed yet (including the expiry day itself —
    the contract is still legitimately trading today), returns False
    immediately with zero DB calls. This covers the overwhelming majority
    of rows (every currently-valid contract, and equity/cash rows) on
    every positions poll. Only once `expiry < now_ist.date()` does this
    consult the EXISTING `next_market_open_ist(expiry, exchange)` (not
    reimplemented) to determine whether the freeze window has actually
    ended yet.

    Fail-safe posture matches the rest of this module (see its docstring,
    "under-protecting is far safer than over-protecting" re: the futures-
    expiry approximation): on any parse ambiguity, returns False — never
    filter out a row that might still be legitimately live.
    """
    if not symbol or symbol == EMPTY_MARKER_SYMBOL:
        return False
    from backend.api.algo.derivatives import parse_tradingsymbol
    parsed = parse_tradingsymbol(symbol)
    if not parsed or not parsed.get("expiry"):
        return False
    expiry: date = parsed["expiry"]
    if expiry >= now_ist.date():
        return False
    boundary = await next_market_open_ist(expiry, exchange=exchange or "MCX")
    return now_ist >= boundary


async def expiry_status(
    symbol: str, captured_at: datetime, exchange: "str | None", now_ist: datetime,
) -> str:
    """Classify one `daily_book` positions row for expiry-freeze purposes.

    Returns one of:
      "not_expiry"       — not a frozen candidate at all (see
                            `expiry_if_closed_on_own_expiry_day`); ordinary
                            rules apply unchanged (prune: sweep normally
                            if orphaned; serve: pass through unchanged).
      "frozen"            — genuinely an expiry-day-final row AND the next
                            market-open 08:00 IST boundary after that
                            expiry has NOT yet passed. Prune: protect from
                            deletion. Serve: include verbatim even if a
                            fresher batch for the same account exists.
      "refresh_eligible"  — an expiry-day-final row whose freeze window
                            HAS ended (now >= boundary). Prune: no longer
                            protected — falls back into the ordinary sweep
                            (which will physically remove it on its next
                            run). Serve: EXCLUDE outright — this is the
                            case a fully-flat account can hit forever
                            without this explicit cutoff, since with no
                            fresher `ltp > 0` batch ever written there is
                            nothing to naturally supersede the frozen
                            batch (see module docstring, "Callers").

    *now_ist* is passed in (not read internally via `timestamp_indian()`)
    so callers — and this function's tests — can pin an exact instant
    rather than depending on wall-clock time at call time.
    """
    expiry = expiry_if_closed_on_own_expiry_day(symbol, captured_at)
    if expiry is None:
        return "not_expiry"
    boundary = await next_market_open_ist(expiry, exchange=exchange or "MCX")
    return "frozen" if now_ist < boundary else "refresh_eligible"
