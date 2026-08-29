"""exchange_clock — DB-backed single source of truth for exchange segment timing.

Public API
----------
All async functions are safe to call from Litestar route handlers and background
tasks. The module-level in-process cache eliminates per-request DB round-trips
while keeping timing changes effective within one refresh cycle (default 60 s).

    await refresh()                          — reload cache from DB
    is_exchange_open(exchange)               — sync; True when any matching gate
                                               is currently open
    is_exchange_closed(exchange)             — sync; inverse of is_exchange_open
    await settlement_cutoff_for(gate)        — last 08:00 IST boundary (as datetime)
    sessions_with_snapshot_time_now()        — sync; list of rows whose
                                               snapshot_time matches now ± 1 min
    await seed_and_warm()                    — on_startup callable: idempotent seed
                                               of 2 default rows, then warm cache

Design
------
The cache is a module-level list of ORM rows loaded via ``async_session``.
Each public accessor reads from the cache; no asyncio.Lock is needed for reads
because Python's GIL makes list reassignment atomic and the cache is only
replaced as a whole (never mutated in place).

The ``seed_and_warm`` callable is registered as a Litestar on_startup handler
and uses the app engine (``AsyncSession(engine)`` directly) since the
dependency-injection session is not available at startup.

Gate → exchange mapping
-----------------------
Each row carries ``exchanges: list[str]`` — the Kite exchange codes that belong
to that gate.  ``is_exchange_open`` resolves the exchange to a gate by iterating
rows and checking membership in ``row.exchanges``.

Seed rows (2 defaults, date IS NULL)
-------------------------------------
+---------+----------------------------------+---------+-----------+----------+----------+
| gate    | exchanges                        | open    | close     | snapshot | reset    |
+---------+----------------------------------+---------+-----------+----------+----------+
| NON-MCX | NSE, BSE, NFO, BFO, CDS         | 08:00   | 15:30     | 15:45    | 08:00    |
| MCX     | MCX                              | 08:00   | 23:30     | 23:45    | 08:00    |
+---------+----------------------------------+---------+-----------+----------+----------+

Pre-open, post-close, and night-settlement sessions are absorbed into the two
main gate rows.  PRE/POST/NIGHT rows from older schema versions are removed by
the migration step in seed_and_warm().

settlement_cutoff_for
---------------------
Returns the last 08:00 IST boundary that has passed for the given gate.
This is the canonical cutoff used when querying ``daily_book.ltp`` for
the prior-session settlement price. Both NON-MCX and MCX use 08:00 IST as
``snapshot_reset_time``; callers should pass the gate derived from the
position/holding's exchange field via EXCHANGE_TO_GATE.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from backend.shared.helpers.ramboq_logger import get_logger

if TYPE_CHECKING:
    from backend.api.models import ExchangeSchedule

logger = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# In-process cache
# ---------------------------------------------------------------------------
_CACHE: list["ExchangeSchedule"] = []
_CACHE_LOCK = asyncio.Lock()
_CACHE_TTL_S: float = 60.0
_cache_loaded_at: float = 0.0  # unix epoch


def _now_ist() -> datetime:
    return datetime.now(_IST)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_matches_now(row: "ExchangeSchedule") -> bool:
    """Return True if the row is in effect right now (date override or default)."""
    now = _now_ist()
    today = now.date()

    # Date-specific override takes precedence.
    if row.date is not None:
        return row.date == today

    # Default row — check weekday filter (Mon=0 … Sun=6).
    if row.weekdays:
        return now.weekday() in row.weekdays
    # No weekday filter → applies every day.
    return True


def _is_within_session(row: "ExchangeSchedule") -> bool:
    """Return True when the current IST time falls within the row's session window."""
    if not row.is_open:
        return False
    if row.open_time is None or row.close_time is None:
        return False
    now_t = _now_ist().time().replace(second=0, microsecond=0)
    return row.open_time <= now_t < row.close_time


def _exchange_to_gate(exchange: str) -> str | None:
    """Return the gate name whose ``exchanges`` list contains *exchange*, or None."""
    upper = exchange.upper()
    for row in _CACHE:
        if upper in [e.upper() for e in (row.exchanges or [])]:
            return row.gate
    return None


# ---------------------------------------------------------------------------
# Cache refresh
# ---------------------------------------------------------------------------

async def refresh() -> None:
    """Reload the exchange_schedule cache from DB.

    Skips the DB call if the cache is younger than ``_CACHE_TTL_S``.
    Thread-safe: uses an asyncio.Lock so concurrent callers during the same
    refresh window each wait for one shared DB fetch rather than racing.
    """
    import time as _time
    global _CACHE, _cache_loaded_at

    async with _CACHE_LOCK:
        age = _time.monotonic() - _cache_loaded_at
        if age < _CACHE_TTL_S and _CACHE:
            return  # Cache is fresh — skip DB round-trip.
        await _force_refresh()


async def _force_refresh() -> None:
    """Unconditionally reload from DB (caller must hold _CACHE_LOCK)."""
    import time as _time
    global _CACHE, _cache_loaded_at

    try:
        from backend.api.database import async_session
        from backend.api.models import ExchangeSchedule
        from sqlalchemy import select

        async with async_session() as session:
            result = await session.execute(select(ExchangeSchedule))
            rows = result.scalars().all()
        _CACHE = list(rows)
        _cache_loaded_at = _time.monotonic()
        logger.debug("exchange_clock: cache refreshed — %d rows", len(_CACHE))
    except Exception as exc:
        logger.warning("exchange_clock: cache refresh failed — %s", exc)
        # Keep stale cache on error so callers do not lose timing data.


# ---------------------------------------------------------------------------
# Public sync API (reads from cache — call refresh() to warm first)
# ---------------------------------------------------------------------------

def is_exchange_open(exchange: str) -> bool:
    """Return True when *exchange* is currently within an open session.

    Checks all cache rows whose ``exchanges`` list contains *exchange*,
    filtered to rows in-effect today (date override or default), and
    returns True when at least one such row's session window contains
    the current IST time.

    Fail-open: returns True if the cache is empty or the exchange is
    not found — so callers default to calling the live broker.
    """
    if not _CACHE:
        return True  # Fail-open: assume market open if cache not warmed.

    upper = exchange.upper()
    for row in _CACHE:
        if upper not in [e.upper() for e in (row.exchanges or [])]:
            continue
        if not _row_matches_now(row):
            continue
        if _is_within_session(row):
            return True
    return False


def is_exchange_closed(exchange: str) -> bool:
    """Return True when *exchange* is NOT currently within any open session.

    Inverse of :func:`is_exchange_open`.  Fail-open: returns False (i.e.
    treat as open) when the cache is empty so callers default to live broker.
    """
    if not _CACHE:
        return False  # Fail-open.
    return not is_exchange_open(exchange)


def is_any_segment_open(exchanges: list[str] | None = None) -> bool:
    """Return True when at least one configured segment is currently open.

    Parameters
    ----------
    exchanges:
        Optional list of exchange codes to restrict the check to a subset of
        segments (e.g. ``["NSE"]`` for NSE-only routes).  ``None`` checks all
        cached rows.
    """
    if not _CACHE:
        return True  # Fail-open.

    if exchanges is None:
        # Check every row.
        for row in _CACHE:
            if _row_matches_now(row) and _is_within_session(row):
                return True
        return False

    upper_set = {e.upper() for e in exchanges}
    for row in _CACHE:
        row_exchs = {e.upper() for e in (row.exchanges or [])}
        if not row_exchs.intersection(upper_set):
            continue
        if _row_matches_now(row) and _is_within_session(row):
            return True
    return False


def sessions_with_snapshot_time_now(tolerance_minutes: int = 1) -> list["ExchangeSchedule"]:
    """Return rows whose ``snapshot_time`` is within ± *tolerance_minutes* of now.

    Used by background.py to fire snapshot tasks at the correct moment
    without relying on hardcoded trigger times.  Background polls every 30s
    so a 1-minute window guarantees exactly one hit per snapshot event.
    """
    now_t = _now_ist().time().replace(second=0, microsecond=0)
    delta = timedelta(minutes=tolerance_minutes)
    matched: list["ExchangeSchedule"] = []
    for row in _CACHE:
        if row.snapshot_time is None:
            continue
        snap_dt = datetime.combine(datetime.today(), row.snapshot_time)
        now_dt = datetime.combine(datetime.today(), now_t)
        if abs((snap_dt - now_dt).total_seconds()) <= delta.total_seconds():
            matched.append(row)
    return matched


# ---------------------------------------------------------------------------
# Cutoff helper (async — may trigger a cache refresh)
# ---------------------------------------------------------------------------

async def settlement_cutoff_for(gate: str) -> datetime:
    """Return the last 08:00 IST boundary that has passed for *gate*.

    Looks up ``snapshot_reset_time`` from the matching default cache row.
    If not found (or cache empty), falls back to 08:00 IST which is the
    project-wide default for both NSE and MCX.

    This is the canonical cutoff for ``daily_book.ltp`` queries that need
    the prior-session settlement LTP rather than any mid-session value.
    """
    await refresh()

    reset_time: time = time(8, 0)  # Default: 08:00 IST
    for row in _CACHE:
        if row.gate.upper() == gate.upper() and row.date is None:
            if row.snapshot_reset_time is not None:
                reset_time = row.snapshot_reset_time
            break

    now_ist = _now_ist()
    today_midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today_reset = today_midnight.replace(
        hour=reset_time.hour, minute=reset_time.minute, second=0, microsecond=0
    )
    if now_ist >= today_reset:
        return today_reset
    # Before reset time — use yesterday's reset boundary.
    return today_reset - timedelta(days=1)


# ---------------------------------------------------------------------------
# Seed + warm (on_startup callable)
# ---------------------------------------------------------------------------

_SEED_ROWS: list[dict] = [
    {
        "gate": "NON-MCX",
        "exchanges": ["NSE", "BSE", "NFO", "BFO", "CDS"],
        "session_name": "regular",
        "is_open": True,
        "open_time": time(8, 0),
        "close_time": time(15, 30),
        "snapshot_time": time(15, 45),
        "snapshot_reset_time": time(8, 0),
        "source": "system",
    },
    {
        "gate": "MCX",
        "exchanges": ["MCX"],
        "session_name": "regular",
        "is_open": True,
        "open_time": time(8, 0),
        "close_time": time(23, 30),
        "snapshot_time": time(23, 45),
        "snapshot_reset_time": time(8, 0),
        "source": "system",
    },
]


async def seed_and_warm() -> None:
    """Idempotent seed of default exchange_schedule rows, then warm cache.

    Called from Litestar ``on_startup`` before any request is served.
    Uses the app engine directly (``AsyncSession(engine)``) since the
    DI session is not available at startup.

    Migration steps run first (idempotent):
      1. Delete legacy PRE / POST / NIGHT rows (no-op if already absent).
      2. Rename the old NSE gate row to NON-MCX and update its open/close times.
      3. Update MCX snapshot time to 23:45 and open from 09:00 to 08:00.

    Seed inserts follow with ``ON CONFLICT DO NOTHING`` so repeated restarts
    do not clobber operator-modified rows.
    """
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy import text as _text
    from backend.api.database import engine

    try:
        async with AsyncSession(engine) as session:
            async with session.begin():
                # --- migration: remove old session rows -------------------------
                await session.execute(_text(
                    "DELETE FROM exchange_schedule WHERE gate IN ('PRE', 'POST', 'NIGHT')"
                ))
                # --- migration: rename NSE → NON-MCX, update times -------------
                await session.execute(_text("""
                    UPDATE exchange_schedule
                    SET gate = 'NON-MCX',
                        open_time = '08:00',
                        close_time = '15:30',
                        snapshot_time = '15:45',
                        snapshot_reset_time = '08:00'
                    WHERE gate = 'NSE'
                      AND date IS NULL
                      AND session_name = 'regular'
                      AND source = 'system'
                """))
                # --- migration: fix MCX open + snapshot time -------------------
                await session.execute(_text("""
                    UPDATE exchange_schedule
                    SET open_time = '08:00',
                        snapshot_time = '23:45'
                    WHERE gate = 'MCX'
                      AND date IS NULL
                      AND session_name = 'regular'
                      AND source = 'system'
                """))
                # --- seed missing rows (NON-MCX / MCX if not yet present) ------
                for row in _SEED_ROWS:
                    await session.execute(_text("""
                        INSERT INTO exchange_schedule
                            (gate, exchanges, date, weekdays, session_name,
                             is_open, open_time, close_time,
                             snapshot_time, snapshot_reset_time, reason, source)
                        VALUES
                            (:gate, :exchanges, NULL, NULL, :session_name,
                             :is_open, :open_time, :close_time,
                             :snapshot_time, :snapshot_reset_time, NULL, :source)
                        ON CONFLICT ON CONSTRAINT uq_exchange_schedule_gate_date_session
                        DO NOTHING
                    """), {
                        "gate": row["gate"],
                        "exchanges": row["exchanges"],
                        "session_name": row["session_name"],
                        "is_open": row["is_open"],
                        "open_time": row.get("open_time"),
                        "close_time": row.get("close_time"),
                        "snapshot_time": row.get("snapshot_time"),
                        "snapshot_reset_time": row.get("snapshot_reset_time"),
                        "source": row["source"],
                    })
        logger.info("exchange_clock: migration + seed complete")
    except Exception as exc:
        logger.warning("exchange_clock: seed failed — %s", exc)

    # Warm the in-process cache after seeding.
    async with _CACHE_LOCK:
        await _force_refresh()
    logger.info("exchange_clock: cache warmed — %d rows", len(_CACHE))
