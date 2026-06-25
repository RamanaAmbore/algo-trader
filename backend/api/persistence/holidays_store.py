"""
Three-tier holiday-calendar read path:
  Tier 1 — in-memory dict keyed (exchange, year) → set[date]
            No TTL: calendar years are immutable once the year is over;
            the current year's set only grows (new holidays may be announced
            mid-year), so Tier 1 is populated eagerly and never evicted.
  Tier 2 — PostgreSQL SELECT from holidays_snapshot WHERE exchange=$1 AND year=$2
  Tier 3 — existing fetch_holidays() from broker_apis.py, which calls NSE API

After a fetch the result is written to Tier 1 sync and enqueued to the DB worker.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone, timedelta

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# ── Tier 1: in-memory cache ───────────────────────────────────────────────────
# Key: (exchange, year)  Value: set[date]
# No eviction — years are read-only once closed.
_MEM_CACHE: dict[tuple[str, int], set[date]] = {}

# Per-(exchange, year) lock to deduplicate concurrent fetches.
_FETCH_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}
_LOCK_MAP_LOCK = asyncio.Lock()


def _ist_year() -> int:
    """Return current year in IST."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).year
    except Exception:
        return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).year


async def _get_fetch_lock(key: tuple[str, int]) -> asyncio.Lock:
    async with _LOCK_MAP_LOCK:
        if key not in _FETCH_LOCKS:
            _FETCH_LOCKS[key] = asyncio.Lock()
        return _FETCH_LOCKS[key]


# ── Tier 2: DB SELECT ─────────────────────────────────────────────────────────

async def _db_fetch(exchange: str, year: int) -> set[date] | None:
    """Return holidays set from PostgreSQL, or None on miss/error.

    Completeness check: at least 1 holiday found (defensive against an
    empty response persisted on nseindia.com outage).
    """
    from sqlalchemy import text
    from backend.api.database import async_session

    stmt = text("""
        SELECT dates_json
        FROM   holidays_snapshot
        WHERE  exchange = :exch
          AND  year     = :year
    """)
    try:
        async with async_session() as session:
            result = await session.execute(stmt, {"exch": exchange, "year": year})
            row = result.fetchone()
        if row is None:
            return None
        dates_json: list[str] = row[0] or []
        if not dates_json:
            return None
        holidays: set[date] = set()
        for s in dates_json:
            try:
                holidays.add(date.fromisoformat(str(s)))
            except ValueError:
                continue
        return holidays if holidays else None
    except Exception as exc:
        logger.warning(f"holidays_store: DB fetch failed for {exchange}/{year}: {exc}")
        return None


# ── Tier 3: broker/NSE fetch (sync, run via to_thread) ───────────────────────

def _nse_fetch_sync(exchange: str) -> set[date]:
    """Call the existing fetch_holidays() from broker_apis — keeps NSE logic in one place."""
    from backend.shared.helpers.broker_apis import fetch_holidays
    return fetch_holidays(exchange)


# ── Enqueue persistence ───────────────────────────────────────────────────────

def _enqueue_db(exchange: str, year: int, holidays: set[date]) -> None:
    from backend.api.persistence import write_queue
    write_queue.enqueue_db({
        "kind":     "holidays_snapshot",
        "exchange": exchange,
        "year":     year,
        "dates":    sorted(d.isoformat() for d in holidays),
    })


# ── Public API ────────────────────────────────────────────────────────────────

async def get_or_fetch_holidays(
    exchange: str, year: int | None = None,
    bypass_cache: bool | None = None,
) -> set[date]:
    """Return the set of holiday dates for exchange/year.

    Read path: Tier 1 (memory) → Tier 2 (DB) → Tier 3 (NSE API).
    Write-back to Tier 1 + DB write queue on Tier 3 hit.

    bypass_cache=True (or runtime_state.is_bypass_on()) skips Tier 1 + 2.
    """
    from backend.api.persistence import runtime_state
    if bypass_cache is None:
        bypass_cache = runtime_state.is_bypass_on()

    exch = exchange.upper().strip()
    yr   = year if year is not None else _ist_year()
    key  = (exch, yr)

    if not bypass_cache:
        # Tier 1 — in-memory
        cached = _MEM_CACHE.get(key)
        if cached is not None:
            return cached

        # Tier 2 — DB (no lock needed for read)
        db_set = await _db_fetch(exch, yr)
        if db_set is not None:
            _MEM_CACHE[key] = db_set
            return db_set

    # Tier 3 — NSE API (deduplicated per exchange/year)
    lock = await _get_fetch_lock(key)
    async with lock:
        if not bypass_cache:
            # Re-check after acquiring.
            cached = _MEM_CACHE.get(key)
            if cached is not None:
                return cached
            db_set2 = await _db_fetch(exch, yr)
            if db_set2 is not None:
                _MEM_CACHE[key] = db_set2
                return db_set2

        try:
            holidays = await asyncio.to_thread(_nse_fetch_sync, exch)
        except Exception as exc:
            logger.warning(f"holidays_store: NSE fetch failed for {exch}/{yr}: {exc}")
            return set()

        if holidays:
            _MEM_CACHE[key] = holidays
            _enqueue_db(exch, yr, holidays)
        else:
            logger.warning(
                f"holidays_store: empty holiday list for {exch}/{yr} — not caching "
                "(will retry on next call)"
            )

        return holidays
