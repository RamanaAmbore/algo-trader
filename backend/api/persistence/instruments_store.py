"""
Three-tier instruments read path:
  Tier 1 — in-memory dict keyed (date_str, exchange) → {(tradingsymbol, exchange): token}
            daily TTL: entries whose date != today IST are purged on each access.
  Tier 2 — PostgreSQL SELECT from instruments_snapshot WHERE exchange=$1 AND date=$2
  Tier 3 — broker.instruments(exchange) via asyncio.to_thread + get_sparkline_broker()

After a broker fetch the result is immediately written to Tier 1 and enqueued to the
DB write worker (no disk worker — payload is too large for JSON cache file).

Per-(exchange, date) asyncio.Lock deduplicates concurrent in-flight fetches so
broker.instruments() is called at most once per cold (exchange, date) pair.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any

from backend.api.persistence.store_base import PersistentStoreBase
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_SPARKLINE_EXCHANGES = ("NSE", "NFO", "BSE", "BFO", "MCX", "CDS")

_MemKey = tuple[str, str]   # (date_str, exchange)


def _ist_today() -> str:
    """Return today's date in IST as YYYY-MM-DD."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


# ── InstrumentsStore subclass ─────────────────────────────────────────────────

class InstrumentsStore(PersistentStoreBase):
    _name     = "instruments_store"
    _max_keys = 500
    _lru      = False   # daily purge replaces LRU; we manage eviction in purge_stale()

    # ── Daily staleness purge ────────────────────────────────────────────────

    def purge_stale(self, today: str) -> None:
        """Drop Tier 1 entries whose date component != today (lazy eviction)."""
        stale = [k for k in self._mem_cache if k[0] != today]
        for k in stale:
            del self._mem_cache[k]

    # ── Completeness check ───────────────────────────────────────────────────

    def _is_complete(self, value: dict[_MemKey, int] | None, key: _MemKey) -> bool:
        """A non-None, non-empty mapping is considered complete."""
        return value is not None and bool(value)

    # ── Tier 2: DB SELECT ────────────────────────────────────────────────────

    async def _db_fetch(self, key: _MemKey) -> dict[tuple[str, str], int] | None:
        """Return the instruments map from PostgreSQL, or None on miss/error.

        None = snapshot absent or payload empty.
        Empty dict is a valid (if useless) result from Kite — caller treats it
        as a miss so we always re-fetch an empty snapshot from the broker.
        """
        from sqlalchemy import text
        from backend.api.database import async_session

        date_str, exchange = key
        stmt = text("""
            SELECT payload, row_count
            FROM   instruments_snapshot
            WHERE  exchange = :exch
              AND  date     = :date
        """)
        try:
            async with async_session() as session:
                result = await session.execute(stmt, {"exch": exchange, "date": date_str})
                row = result.fetchone()
            if row is None:
                return None
            payload: list[dict[str, Any]] = row[0] or []
            row_count: int = int(row[1] or 0)
            if row_count == 0 or not payload:
                return None
            mapping: dict[tuple[str, str], int] = {}
            for item in payload:
                ts   = item.get("tradingsymbol")
                exch = item.get("exchange", exchange)
                tok  = item.get("instrument_token")
                if ts and tok:
                    mapping[(str(ts).upper(), str(exch))] = int(tok)
            return mapping if mapping else None
        except Exception as exc:
            logger.warning(f"instruments_store: DB fetch failed for {key}: {exc}")
            return None

    # ── Tier 3: broker fetch ─────────────────────────────────────────────────

    async def _broker_fetch(self, key: _MemKey) -> dict[tuple[str, str], int] | None:
        _date_str, exchange = key
        rows = await asyncio.to_thread(_broker_fetch_sync, exchange)
        mapping = _rows_to_map(rows, exchange)
        if not mapping:
            logger.warning(
                f"instruments_store: broker returned 0 instruments for {exchange} "
                "(not caching empty response)"
            )
            return None   # base will not call _mem_set or _enqueue_persist
        return mapping

    # ── Write-back ───────────────────────────────────────────────────────────

    def _enqueue_persist(self, key: _MemKey, value: dict[tuple[str, str], int]) -> None:
        # We need the raw rows to persist (the map has lost extra fields).
        # Re-reconstruct slim rows from the map — consistent with original logic.
        date_str, exchange = key
        slim: list[dict[str, Any]] = [
            {"tradingsymbol": ts, "exchange": exch, "instrument_token": tok}
            for (ts, exch), tok in value.items()
        ]
        from backend.api.persistence import write_queue
        write_queue.enqueue_db({
            "kind":      "instruments_snapshot",
            "exchange":  exchange,
            "date":      date_str,
            "payload":   slim,
            "row_count": len(slim),
        })

    # ── Override: broker returns empty mapping — log and don't cache ──────────

    async def get(self, key: _MemKey, *, bypass_cache: bool | None = None) -> dict[tuple[str, str], int]:
        """Same three-tier flow as base, but empty-mapping from broker is not cached."""
        result = await super().get(key, bypass_cache=bypass_cache)
        if result is None:
            return {}
        return result


# ── Module-level singleton + backward-compat alias ───────────────────────────

_instruments_store = InstrumentsStore()

# runtime_state.invalidate_instruments() reaches into _MEM_CACHE.
_MEM_CACHE: dict[_MemKey, dict[tuple[str, str], int]] = _instruments_store._mem_cache


# ── Tier 3 sync helper (module-level) ────────────────────────────────────────

def _broker_fetch_sync(exchange: str) -> list[dict[str, Any]]:
    """Blocking call to broker.instruments(exchange). Run via asyncio.to_thread."""
    from backend.shared.brokers.registry import get_sparkline_broker
    broker = get_sparkline_broker()
    raw = broker.instruments(exchange) or []
    return list(raw)


def _rows_to_map(rows: list[dict[str, Any]], exchange: str) -> dict[tuple[str, str], int]:
    mapping: dict[tuple[str, str], int] = {}
    for row in rows:
        ts   = row.get("tradingsymbol")
        exch = row.get("exchange", exchange)
        tok  = row.get("instrument_token")
        if ts and tok:
            mapping[(str(ts).upper(), str(exch))] = int(tok)
    return mapping


# ── Public API ────────────────────────────────────────────────────────────────

async def get_or_fetch_instruments(
    exchange: str, bypass_cache: bool | None = None,
) -> dict[tuple[str, str], int]:
    """Return {(tradingsymbol, exchange): instrument_token} for exchange.

    Read path: Tier 1 (memory) → Tier 2 (DB) → Tier 3 (broker).
    Write-back to Tier 1 + DB write queue happens only on Tier 3 hit.

    bypass_cache=True (or runtime_state.is_bypass_on()) skips Tier 1 + 2
    and goes straight to broker — defect-recovery escape hatch.
    """
    exch  = exchange.upper().strip()
    today = _ist_today()
    _instruments_store.purge_stale(today)
    key: _MemKey = (today, exch)
    return await _instruments_store.get(key, bypass_cache=bypass_cache)


async def get_or_fetch_all_today() -> dict[tuple[str, str], int]:
    """Fetch all 6 sparkline exchanges in parallel and return the union map.

    This is what _get_today_token_map in quote.py should delegate to.
    """
    results = await asyncio.gather(
        *[get_or_fetch_instruments(exch) for exch in _SPARKLINE_EXCHANGES],
        return_exceptions=True,
    )
    union: dict[tuple[str, str], int] = {}
    for exch, result in zip(_SPARKLINE_EXCHANGES, results):
        if isinstance(result, Exception):
            logger.warning(f"instruments_store: get_or_fetch_instruments({exch}) failed: {result}")
            continue
        union.update(result)
    return union


# Module-level alias so quote.py's sync-path Tier-1 check can import a
# plain function (instead of binding to the instance method on the
# singleton). Slice AQ caught the import as silently failing: the prior
# `from ... import _purge_stale` raised ImportError, was swallowed by
# the `except Exception: pass` around the Tier-1 lookup, and the entire
# token-map fast path was disabled — every call fell through to the
# legacy 6-exchange broker fetch.
_purge_stale = _instruments_store.purge_stale
