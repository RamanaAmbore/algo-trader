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
"""

from __future__ import annotations

from datetime import date

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

CAL_OPEN = "open"
CAL_CLOSED = "closed"
CAL_UNKNOWN = "unknown"

_DEFAULT_SEGMENTS = {
    "equity":    {"holiday_exchange": "NSE", "evening_open_on_holidays": False},
    "commodity": {"holiday_exchange": "MCX", "evening_open_on_holidays": True},
}


def _segments_config() -> dict:
    from backend.shared.helpers.utils import config
    segments = config.get("market_segments", {}) or {}
    return segments or _DEFAULT_SEGMENTS


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

    today = timestamp_indian().date()
    verdict, detail = await _conn_calendar_verdict_detail(today)
    should_connect = verdict != CAL_CLOSED
    if not should_connect:
        logger.info(
            "conn_service: market calendar confirms closed today (%s) — "
            "skipping KiteTicker connect attempt", today,
        )

    try:
        await _persist_verdict_row(today, verdict, detail)
    except Exception as e:
        logger.warning(
            "[CONN-TICKER-CALENDAR] verdict persistence failed for %s: %s",
            today, e,
        )

    return should_connect
