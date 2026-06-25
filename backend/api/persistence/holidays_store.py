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

from backend.api.persistence.store_base import PersistentStoreBase
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_MemKey = tuple[str, int]   # (exchange, year)


def _ist_year() -> int:
    """Return current year in IST."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).year
    except Exception:
        return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).year


# ── HolidaysStore subclass ────────────────────────────────────────────────────

class HolidaysStore(PersistentStoreBase):
    _name     = "holidays_store"
    _max_keys = 500
    _lru      = False   # years are never evicted (immutable once closed)

    # ── Completeness check ───────────────────────────────────────────────────

    def _is_complete(self, value: set[date] | None, key: _MemKey) -> bool:
        """A non-None, non-empty set is considered complete."""
        return value is not None and bool(value)

    # ── Tier 2: DB SELECT ────────────────────────────────────────────────────

    async def _db_fetch(self, key: _MemKey) -> set[date] | None:
        """Return holidays set from PostgreSQL, or None on miss/error.

        Completeness check: at least 1 holiday found (defensive against an
        empty response persisted on nseindia.com outage).
        """
        from sqlalchemy import text
        from backend.api.database import async_session

        exchange, year = key
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
            logger.warning(f"holidays_store: DB fetch failed for {key}: {exc}")
            return None

    # ── Tier 3: NSE API fetch ────────────────────────────────────────────────

    async def _broker_fetch(self, key: _MemKey) -> set[date] | None:
        exchange, _year = key
        result = await asyncio.to_thread(_nse_fetch_sync, exchange)
        if not result:
            logger.warning(
                f"holidays_store: empty holiday list for {key} — not caching "
                "(will retry on next call)"
            )
            return None   # base will not cache or enqueue
        return result

    # ── Write-back ───────────────────────────────────────────────────────────

    def _enqueue_persist(self, key: _MemKey, value: set[date]) -> None:
        exchange, year = key
        from backend.api.persistence import write_queue
        write_queue.enqueue_db({
            "kind":     "holidays_snapshot",
            "exchange": exchange,
            "year":     year,
            "dates":    sorted(d.isoformat() for d in value),
        })

    # ── Override: return set() instead of None on full miss ──────────────────

    async def get(self, key: _MemKey, *, bypass_cache: bool | None = None) -> set[date]:
        result = await super().get(key, bypass_cache=bypass_cache)
        if result is None:
            return set()
        return result


# ── Module-level singleton + backward-compat alias ───────────────────────────

_holidays_store = HolidaysStore()

# runtime_state.invalidate_holidays() reaches into _MEM_CACHE.
_MEM_CACHE: dict[_MemKey, set[date]] = _holidays_store._mem_cache


# ── Tier 3 sync helper (module-level) ────────────────────────────────────────

def _nse_fetch_sync(exchange: str) -> set[date]:
    """Call the existing fetch_holidays() from broker_apis — keeps NSE logic in one place."""
    from backend.shared.helpers.broker_apis import fetch_holidays
    return fetch_holidays(exchange)


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
    exch = exchange.upper().strip()
    yr   = year if year is not None else _ist_year()
    key: _MemKey = (exch, yr)
    return await _holidays_store.get(key, bypass_cache=bypass_cache)
