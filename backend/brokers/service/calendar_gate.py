"""Market-calendar gate for conn_service's own KiteTicker connect attempt.

Resource-efficiency only — see this commit's message for the feature
summary. conn_service connects to Kite's WebSocket exactly once at process boot
(`app._start_kite_ticker`) and never tears it down on its own; the 30s
watchdog (`app._ticker_watchdog`) only ever repeats the same idempotent
connect attempt while the ticker hasn't started yet. On a day nothing
will trade (weekend / holiday with no special session), that repeated
attempt is pure waste — open WS, CPU, exposure to Kite-side anomalies —
but never unsafe, since `TickBufferWriter` has no consumer-awareness
either way. This module answers one question for `app.py`: "is it worth
even trying to connect today?" — it never disconnects an already-live
connection, and it never touches the failover/health machinery.

DB-access convention, deliberately NOT `shared_async_session()`
--------------------------------------------------------------
The originating plan called for `shared_async_session()` (the engine
`Connections().rebuild_from_db()` uses for `broker_accounts`), reasoning
that conn_service's one existing calendar check — `is_any_segment_open()`
via `run_on_main_loop()` — is silently broken here because
`run_on_main_loop()` depends on a module-level loop handle only the main
API process's startup ever captures.

That diagnosis is correct, but `shared_async_session()` is the wrong
fix for it: `market_holidays` / `market_special_sessions` (unlike
`broker_accounts`) are ordinary branch-local tables — see
`backend.api.database.init_db`'s own docstring and
`_build_shared_url()`'s docstring ("Currently only `broker_accounts`
uses this session... All other tables... stay on the branch-local DB").
They are created on the branch-local `engine`, not `_shared_engine`.

The actual constraint from the investigation is narrower than "use
`shared_async_session()`" — it's "don't go through `run_on_main_loop()`
or any sync wrapper that depends on it". `broker_apis._read_market_
holidays_async()` and `broker_apis._read_special_sessions_async()`
already satisfy that: they are plain `async with async_session() as s`
coroutines with zero `run_on_main_loop` involvement (only their *_sync
wrapper siblings use it). This module awaits them directly — the exact
same query shape the API-side `_ticker_calendar_verdict()` consults,
just without its broken sync detour. This is a deliberate deviation
from the originating plan's literal instruction, not an oversight.

A second, independently-discovered gap: `_parse_extra_trading_days()`
(the operator's `market.extra_trading_days` override — Muhurat Saturdays
etc.) reads `settings.py`'s in-process `_CACHE`, populated only by
`seed_settings()` — which conn_service's `on_startup` list never calls.
`market.extra_trading_days` also has no YAML fallback (it's a DB-only
seeded setting, absent from `backend_config.yaml` entirely), so inside
conn_service that helper would ALWAYS resolve to the empty set,
regardless of what an operator has configured live. For most settings
that's an existing, lower-stakes gap; for this one specifically it would
mean a genuine Muhurat trading day reads CLOSED here while the API
process's own verdict reads OPEN and expects ticks that never arrive —
exactly the failure direction this feature must never introduce.
`_conn_extra_trading_days()` below reads the `settings` row directly
instead of trusting that cache.

Audit persistence — always recompute, always upsert
-----------------------------------------------------
`conn_should_attempt_connect()` persists one row per calendar day into
`ticker_calendar_verdicts` (see `backend.api.models.TickerCalendarVerdict`
for the full column contract) so an operator or future consumer has a
durable record of what conn_service decided on any given day.

This is deliberately NOT a read-through cache: the connect-decision
loop (`conn_calendar_verdict`/`_conn_calendar_verdict_detail`) ALWAYS
recomputes fresh and NEVER reads this table back to short-circuit that
computation. The reason is a genuine same-day correctness requirement,
not caution for its own sake: `market_special_sessions` (the one input
that can legitimately change mid-day — an operator adding a late
special session) carries no reliable "has this changed since I last
looked" signal — its reconciliation path DELETEs stale rows rather than
updating them in place, so even `MAX(created_at)` can't detect a
same-day change without re-querying the same table the fresh
computation already reads. Caching the verdict for the rest of the day
would silently break the "a same-day special session is picked up on
the next 30s watchdog tick" guarantee this whole feature exists to
preserve. The persisted row is written AFTER the connect/skip decision
is already finalised, and a persistence failure (including the table
not existing yet — conn_service never runs migrations itself; see
`TickerCalendarVerdict`'s own docstring on this) is swallowed and
logged, never allowed to change that decision.

Audit coverage once the ticker is already connected (2026-10-24)
------------------------------------------------------------------
`conn_should_attempt_connect()` above is only ever called from two
places in `app.py`: `_start_kite_ticker()` at process boot, and
`_ticker_watchdog()`'s Phase 1, which itself only runs while
`ticker.status().get("started")` is False. Once the ticker is up,
neither call site runs again until a restart — Twisted's own reconnect
logic keeps the connection alive for weeks without one. Left alone,
`ticker_calendar_verdicts` would get a row only for the day
conn_service last (re)started, with zero rows for every day in between
on a restart-free multi-day stretch (e.g. a weekend sitting on top of
an already-live Friday connection) — the opposite of "one row per
calendar day".

`record_daily_verdict_if_needed(ticker_started)` closes this gap. It is
wired into `_ticker_watchdog()` unconditionally (every 30s tick,
regardless of Phase 1's own "not started" gate), and is the ONLY other
writer of `ticker_calendar_verdicts` besides `conn_should_attempt_
connect()`. It no-ops instantly while the ticker hasn't started
(Phase 1 already covers that case) and no-ops once today (IST) has
already been recorded this process run — by either function, via the
shared `_last_recorded_date` sentinel below — so a continuously-connected
ticker still gets exactly one row per day, not one per 30s tick. Rows
it writes carry `detail["ticker_already_connected"] = True` so a reader
can tell "closed but not acted on because a prior connection was
already live" apart from "closed and genuinely never connected".
"""

from __future__ import annotations

from datetime import date, time as _time

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

CAL_OPEN = "open"
CAL_CLOSED = "closed"
CAL_UNKNOWN = "unknown"

# Fallback MCX gate bounds — byte-identical to this module's own prior
# hardcoded quiet-window constants (23:31 = 23:30 + 1 min, 08:00), AND to
# `exchange_schedule`'s own seeded default row for gate='MCX' (see
# `exchange_clock.py`'s module docstring seed table). Used ONLY when a
# DB read fails or returns no usable row — never cached as if it were a
# real resolution, so a transient failure can't poison the rest of the
# day (same "never cache a failure" discipline as `_SPECIAL_SESSION_CACHE`
# elsewhere in this codebase).
_MCX_GATE_DEFAULT_OPEN = _time(8, 0)
_MCX_GATE_DEFAULT_CLOSE = _time(23, 30)

# Per-IST-calendar-day cache for the resolved MCX gate bounds — keeps the
# `exchange_schedule` read to at most once per day rather than once per
# 30s tick forever (both `_task_daily_ticker_cycle()` and, while the
# ticker isn't started, `_ticker_watchdog`'s Phase 1 consult this via
# `ticker_quiet_window_now()`). (date, open_time, close_time) | None.
_mcx_gate_bounds_cache: "tuple[date, _time, _time] | None" = None

# Shared "already recorded today" sentinel — set by both
# `conn_should_attempt_connect()` and `record_daily_verdict_if_needed()`
# on a successful persist, read only by the latter (the former always
# recomputes+upserts unconditionally — see this module's "Audit
# persistence" docstring section on why that must never change).
_last_recorded_date: "date | None" = None

# Log-dedup sentinel for the closed-day INFO line in
# `conn_should_attempt_connect()` — avoids one INFO line per 30s tick
# (~2,880/day) across a multi-day closed stretch. Persistence itself is
# unaffected; only the log line is deduped.
_last_logged_closed: "tuple | None" = None

_DEFAULT_SEGMENTS = {
    "equity":    {"holiday_exchange": "NSE", "evening_open_on_holidays": False},
    "commodity": {"holiday_exchange": "MCX", "evening_open_on_holidays": True},
}


def _segments_config() -> dict:
    from backend.shared.helpers.utils import config
    segments = config.get("market_segments", {}) or {}
    return segments or _DEFAULT_SEGMENTS


async def resolve_mcx_gate_bounds(today: date) -> "tuple[_time, _time]":
    """Resolve TODAY's effective MCX gate (open_time, close_time) from
    ``exchange_schedule``, with a date-specific override row taking
    absolute priority over the permanent default — mirrors
    ``exchange_clock._effective_gate_rows()``'s own precedence (see that
    module's docstring), reading directly via
    ``broker_apis._read_exchange_schedule_rows_async`` rather than that
    module's in-process ``_CACHE`` (main-API-process-only state).

    2026-10-28 — replaces the hardcoded 23:31/08:00 KiteTicker
    quiet-window constants. ``background.py`` has had comments since
    before this change anticipating "a future DST-adjusted MCX close"
    modeled as an ``exchange_schedule`` date-override row (the SAME
    table ``_ds_tick_mcx_stop()`` already reads for its own close-time
    decision) — this makes conn_service's quiet window agree with that
    mechanism instead of hardcoding a value that can silently drift out
    of sync with it.

    Both the open and close bounds are resolved for the SAME calendar
    date (``today``), never a separate "tomorrow" lookup — this is
    deliberate, not a simplification that loses the cross-midnight case:
    at any instant, ``today`` (the current IST calendar date) already
    carries the relevant boundary for whichever side of the quiet window
    the caller is evaluating. Checked at 23:40 on 2026-10-30, ``today``
    is 2026-10-30 and its own ``close_time`` is what just passed (or is
    about to). Checked at 02:00 on 2026-10-31 (now inside the window),
    ``today`` is 2026-10-31 and its own ``open_time`` is what the window
    is waiting for. No row for "the next trading day" is ever needed.

    Treats a holiday/no-session override row (``open_time`` or
    ``close_time`` is ``None`` — ``exchange_clock.py``'s own
    ``_is_within_session()`` documents this convention) the SAME as "no
    override" — falls through to the default bounds. This is a
    deliberate simplification: this function answers "what time does
    the shared ticker's connectivity window open/close today", not
    "does MCX have a session at all today" (that is the DIFFERENT,
    pre-existing day-level question ``conn_should_attempt_connect()``
    already answers, consulted independently by every call site that
    needs it). Narrowing the quiet window further on a pure-MCX holiday
    when NSE might still be open is out of scope here and would risk a
    regression on the NSE side; this function only ever widens or
    shifts the MCX-close boundary when a REAL alternate-hours override
    exists, never invents a shorter/no-connectivity day on its own.

    DB failures and "no usable row" both fall back to
    ``(_MCX_GATE_DEFAULT_OPEN, _MCX_GATE_DEFAULT_CLOSE)`` — identical to
    this module's historical hardcoded values — and are never cached, so
    a transient failure doesn't poison the rest of the day (same
    discipline as ``_SPECIAL_SESSION_CACHE`` elsewhere in this codebase).
    A successful resolution IS cached, keyed on ``today``, so this reads
    the DB at most once per IST calendar day rather than once per 30s
    tick forever.
    """
    global _mcx_gate_bounds_cache

    cached = _mcx_gate_bounds_cache
    if cached is not None and cached[0] == today:
        return cached[1], cached[2]

    try:
        from backend.brokers.broker_apis import _read_exchange_schedule_rows_async
        rows = await _read_exchange_schedule_rows_async("MCX", today)
    except Exception as e:
        logger.warning(
            "[CONN-TICKER-CALENDAR] exchange_schedule(MCX) read failed — "
            "falling back to default gate bounds: %s", e,
        )
        return _MCX_GATE_DEFAULT_OPEN, _MCX_GATE_DEFAULT_CLOSE

    override = [r for r in rows if r.get("date") == today]
    if override:
        row = override[0]
    else:
        defaults = [
            r for r in rows
            if r.get("date") is None and (
                not r.get("weekdays") or today.weekday() in r["weekdays"]
            )
        ]
        row = defaults[0] if defaults else None

    if row and row.get("open_time") is not None and row.get("close_time") is not None:
        open_t, close_t = row["open_time"], row["close_time"]
    else:
        open_t, close_t = _MCX_GATE_DEFAULT_OPEN, _MCX_GATE_DEFAULT_CLOSE

    _mcx_gate_bounds_cache = (today, open_t, close_t)
    return open_t, close_t


async def ticker_quiet_window_now() -> bool:
    """True during the nightly KiteTicker-quiet window, with its
    boundaries resolved from ``exchange_schedule`` (via
    ``resolve_mcx_gate_bounds()``) rather than hardcoded 23:31/08:00
    constants.

    2026-10-28 — moved here from ``service/app.py`` (was
    ``_ticker_quiet_window_now()``, a pure, zero-argument clock function)
    because resolving the boundary now needs calendar data, which is
    this module's domain. ``app.py`` imports this under its old name via
    ``from .calendar_gate import ticker_quiet_window_now as
    _ticker_quiet_window_now`` so existing call sites and their tests
    don't need to rename anything.

    Still stateless PER CALL in the sense that mattered for the original
    design decision (no settable/clearable marker tracking "did the
    daily-cycle task already disconnect tonight") — a process restart or
    crash recovery at any point inside the window is still handled
    correctly, because every call re-derives the window fresh from
    ``timestamp_indian()`` and the (cached-per-day, re-read-on-date-
    rollover) resolved bounds. It is NOT a "no DB" function anymore —
    the very first call on a new IST calendar day does one real
    ``exchange_schedule`` read (see ``resolve_mcx_gate_bounds()``'s own
    cache).

    Close threshold is ``close_time + 1 minute`` (matching the historical
    "MCX settles around 23:30; stopping 1 min later captures final
    ticks" rationale in ``background.py:_ds_tick_mcx_stop()``), not the
    raw close time. The open threshold is the raw resolved open time —
    no buffer on that side, matching the original design (08:00 was
    always used as-is, never 08:01).
    """
    from datetime import datetime as _dt_cls, timedelta
    from backend.shared.helpers.date_time_utils import timestamp_indian

    now = timestamp_indian()
    today = now.date()
    open_t, close_t = await resolve_mcx_gate_bounds(today)

    close_threshold = (_dt_cls.combine(today, close_t) + timedelta(minutes=1)).time()
    t = now.time()
    return t >= close_threshold or t < open_t


async def _conn_extra_trading_days() -> "set | None":
    """Direct DB read of the `market.extra_trading_days` operator setting.

    Returns `None` on any DB error (caller must treat this as UNCERTAIN,
    never as "no override configured"). Returns an empty set when the
    row is genuinely absent/blank — a real "nothing configured" answer,
    not uncertainty.
    """
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import Setting

    try:
        async with async_session() as s:
            raw = (await s.execute(
                select(Setting.value).where(
                    Setting.key == "market.extra_trading_days"
                )
            )).scalar_one_or_none()
    except Exception as e:
        logger.warning("[CONN-TICKER-CALENDAR] extra_trading_days read failed: %s", e)
        return None

    out: set = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            y, m, d = part.split("-")
            out.add(date(int(y), int(m), int(d)))
        except Exception:
            continue
    return out


async def _segment_verdict(seg_cfg: dict, today: date, extra: set) -> str:
    """Tri-state verdict for ONE segment. See `conn_calendar_verdict`'s
    docstring for the full precedence chain — this implements it for a
    single `seg_cfg` dict so the caller's loop stays a flat dispatch."""
    from backend.shared.helpers.date_time_utils import is_trading_day
    from backend.brokers.broker_apis import (
        _read_market_holidays_async, _read_special_sessions_async,
    )

    exch = str((seg_cfg or {}).get("holiday_exchange", "NSE")).upper().strip()

    try:
        holidays = await _read_market_holidays_async(exch)
    except Exception as e:
        logger.warning("[CONN-TICKER-CALENDAR] market_holidays(%s) failed: %s", exch, e)
        return CAL_UNKNOWN

    if is_trading_day(today, holidays, extra_trading_days=extra):
        return CAL_OPEN

    try:
        special = await _read_special_sessions_async(exch, today)
    except Exception as e:
        logger.warning("[CONN-TICKER-CALENDAR] special_sessions(%s) failed: %s", exch, e)
        return CAL_UNKNOWN

    if any(s.get("start") != s.get("end") for s in (special or [])):
        return CAL_OPEN
    if special:
        # Zero-width override row for today — blocks the evening-open
        # default below; must never be treated as an open signal.
        return CAL_CLOSED

    evening = bool((seg_cfg or {}).get("evening_open_on_holidays", False))
    if evening and today.weekday() < 5:
        return CAL_OPEN
    return CAL_CLOSED


async def _conn_calendar_verdict_detail(today: date) -> "tuple[str, dict]":
    """Shared body for `conn_calendar_verdict()` (verdict only) and
    `conn_should_attempt_connect()` (also persists the detail as an
    audit row) — computes the tri-state "will any configured market
    segment trade at all today" verdict AND a small, JSON-serialisable
    `detail` dict describing how it was reached (per-segment verdict
    strings plus a couple of booleans — never raw DB rows).

    Mirrors `backend.api.background._ticker_calendar_verdict()`
    conceptually (same per-segment precedence: operator override →
    calendar → same-day special session → evening-open-on-holiday
    default → closed), reimplemented with direct, working async DB
    reads instead of that function's `asyncio.to_thread(fetch_holidays,
    ...)` (whose Tier-3 DB path is `run_on_main_loop`-dependent and
    silently no-ops inside this process) — see this module's own
    docstring for the full reasoning.

    Returns `CAL_OPEN` the moment any segment resolves open
    (short-circuits — `detail['segments']` is therefore partial, not
    every configured segment). Returns `CAL_UNKNOWN` if any lookup
    raised. Returns `CAL_CLOSED` only when every segment cleanly
    resolved closed. Callers treat `CAL_UNKNOWN` exactly like
    `CAL_OPEN` (fail-safe-to-connect) — a wasted connection attempt is
    cheap, a missed real trading day is not.
    """
    detail: dict = {}
    try:
        extra = await _conn_extra_trading_days()
        any_uncertain = extra is None
        extra = extra or set()
        detail["extra_trading_days_read_failed"] = any_uncertain

        if today in extra:
            detail["resolved_via"] = "extra_trading_days"
            return CAL_OPEN, detail

        segment_detail: dict = {}
        for name, seg_cfg in _segments_config().items():
            verdict = await _segment_verdict(seg_cfg, today, extra)
            segment_detail[name] = verdict
            if verdict == CAL_OPEN:
                detail["segments"] = segment_detail
                detail["resolved_via"] = name
                return CAL_OPEN, detail
            if verdict == CAL_UNKNOWN:
                any_uncertain = True

        detail["segments"] = segment_detail
        final = CAL_UNKNOWN if any_uncertain else CAL_CLOSED
        return final, detail
    except Exception as e:
        logger.warning(
            "[CONN-TICKER-CALENDAR] verdict computation failed — fail-safe "
            "to CONNECT: %s", e,
        )
        detail["error"] = str(e)
        return CAL_UNKNOWN, detail


async def conn_calendar_verdict(today: date) -> str:
    """Tri-state verdict only. See `_conn_calendar_verdict_detail`'s
    docstring for the full precedence chain — kept there once rather
    than duplicated across two functions."""
    verdict, _detail = await _conn_calendar_verdict_detail(today)
    return verdict


async def _persist_verdict_row(today: date, verdict: str, detail: dict) -> None:
    """UPSERT today's row into `ticker_calendar_verdicts` (audit trail
    only — see this module's own docstring; never read back by the
    connect-decision loop). Raises on failure; `conn_should_attempt_
    connect()` wraps this call in try/except so a persistence failure
    (including the table not existing yet on a fresh deploy — conn_
    service never runs migrations itself) can never affect the
    already-decided connect/skip return value.

    `detail` must already be JSON-serialisable (strings/bools/ints
    only, built by `_conn_calendar_verdict_detail` — never raw
    `market_special_sessions` rows, whose `start_time`/`end_time` are
    `datetime.time` and are not JSON-serialisable).
    """
    import json
    from datetime import datetime, timezone
    from sqlalchemy import text as _text
    from backend.api.database import async_session

    detail_json = json.dumps(detail or {})

    upsert_sql = _text("""
        INSERT INTO ticker_calendar_verdicts (date, verdict, computed_at, detail)
        VALUES (:date, :verdict, :computed_at, CAST(:detail AS JSONB))
        ON CONFLICT (date) DO UPDATE SET
            verdict     = EXCLUDED.verdict,
            computed_at = EXCLUDED.computed_at,
            detail      = EXCLUDED.detail
    """)
    async with async_session() as s:
        await s.execute(upsert_sql, {
            "date": today, "verdict": verdict,
            "computed_at": datetime.now(timezone.utc),
            "detail": detail_json,
        })
        await s.commit()


async def conn_should_attempt_connect() -> bool:
    """True unless today's calendar verdict cleanly resolves CLOSED.

    Fail-safe to True (attempt the connect) on any error or
    uncertainty. The one and only question this answers is "is a
    connect attempt worth making right now" — it never tears down an
    already-live connection and carries no disconnect side effect.

    Also persists today's verdict as an audit row (see this module's
    "Audit persistence" docstring section) — best-effort, AFTER the
    return value below is already finalised, so a persistence failure
    never changes the connect/skip decision."""
    from backend.shared.helpers.date_time_utils import timestamp_indian

    global _last_recorded_date, _last_logged_closed

    today = timestamp_indian().date()
    verdict, detail = await _conn_calendar_verdict_detail(today)
    should_connect = verdict != CAL_CLOSED
    if not should_connect:
        # Dedup on (date, verdict) — same spirit as `_last_recorded_date`
        # below, but log-only: without this, a multi-day closed stretch
        # logs this INFO line every 30s tick (~2,880 lines/day).
        # Persistence itself is unaffected — still upserts every tick.
        log_key = (today, verdict)
        if _last_logged_closed != log_key:
            logger.info(
                "conn_service: market calendar confirms closed today (%s) — "
                "skipping KiteTicker connect attempt", today,
            )
            _last_logged_closed = log_key

    try:
        await _persist_verdict_row(today, verdict, detail)
        _last_recorded_date = today
    except Exception as e:
        logger.warning(
            "[CONN-TICKER-CALENDAR] verdict persistence failed for %s: %s",
            today, e,
        )

    return should_connect


async def record_daily_verdict_if_needed(ticker_started: bool) -> None:
    """Covers the complementary case to `conn_should_attempt_connect()`'s
    own audit write — see this module's "Audit coverage once the ticker
    is already connected" docstring section for the full "why". Call
    once per watchdog tick, unconditionally (regardless of Phase 1's own
    "not started" gate); `ticker_started` is the SAME
    `ticker.status().get("started")` value Phase 1 itself reads, so this
    introduces no new ticker/watchdog state.

    No-ops immediately when `ticker_started` is False — that case is
    Phase 1's job via `conn_should_attempt_connect()`. No-ops when today
    (IST) has already been recorded this process run by either this
    function or `conn_should_attempt_connect()` (shared
    `_last_recorded_date` sentinel) — the common case on every tick but
    the first-after-midnight. Never connects, disconnects, or raises;
    the compute+persist is wrapped in a 5s timeout and any failure
    (including a timeout) is swallowed and logged, so a hung or slow DB
    call here can never stall the same 30s tick that also drives the
    watchdog's Phase 2b/3/4 health-check and failover decisions.
    """
    global _last_recorded_date

    if not ticker_started:
        return

    try:
        from backend.shared.helpers.date_time_utils import timestamp_indian
        import asyncio

        today = timestamp_indian().date()
        if _last_recorded_date == today:
            return

        async def _compute_and_persist() -> None:
            verdict, detail = await _conn_calendar_verdict_detail(today)
            # Copy, never mutate the dict `_conn_calendar_verdict_detail`
            # returned — callers (including tests) may hold/reuse that
            # same dict object elsewhere.
            merged_detail = {**detail, "ticker_already_connected": True}
            await _persist_verdict_row(today, verdict, merged_detail)

        await asyncio.wait_for(_compute_and_persist(), timeout=5.0)
        _last_recorded_date = today
    except Exception as e:
        logger.warning(
            "[CONN-TICKER-CALENDAR] daily verdict recording failed for "
            "already-connected ticker: %s", e,
        )
