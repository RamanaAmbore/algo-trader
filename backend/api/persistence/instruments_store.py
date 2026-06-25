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

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_SPARKLINE_EXCHANGES = ("NSE", "NFO", "BSE", "BFO", "MCX", "CDS")

# ── Tier 1: in-memory cache ───────────────────────────────────────────────────
# Key: (date_str, exchange)  Value: {(tradingsymbol, exchange): instrument_token}
_MEM_CACHE: dict[tuple[str, str], dict[tuple[str, str], int]] = {}

# Per-(exchange, date) lock to deduplicate concurrent broker fetches.
_FETCH_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_LOCK_MAP_LOCK = asyncio.Lock()


def _ist_today() -> str:
    """Return today's date in IST as YYYY-MM-DD."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _purge_stale(today: str) -> None:
    """Drop Tier 1 entries whose date component != today (lazy eviction)."""
    stale = [k for k in _MEM_CACHE if k[0] != today]
    for k in stale:
        del _MEM_CACHE[k]


async def _get_fetch_lock(key: tuple[str, str]) -> asyncio.Lock:
    async with _LOCK_MAP_LOCK:
        if key not in _FETCH_LOCKS:
            _FETCH_LOCKS[key] = asyncio.Lock()
        return _FETCH_LOCKS[key]


# ── Tier 2: DB SELECT ─────────────────────────────────────────────────────────

async def _db_fetch(exchange: str, date_str: str) -> dict[tuple[str, str], int] | None:
    """Return the instruments map from PostgreSQL, or None on miss/error.

    None = snapshot absent or payload empty.
    Empty dict is a valid (if useless) result from Kite — caller treats it
    as a miss so we always re-fetch an empty snapshot from the broker.
    """
    from sqlalchemy import text
    from backend.api.database import async_session

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
        # Reconstruct the {(tradingsymbol, exchange): token} map.
        mapping: dict[tuple[str, str], int] = {}
        for item in payload:
            ts  = item.get("tradingsymbol")
            exch = item.get("exchange", exchange)
            tok = item.get("instrument_token")
            if ts and tok:
                mapping[(str(ts).upper(), str(exch))] = int(tok)
        return mapping if mapping else None
    except Exception as exc:
        logger.warning(f"instruments_store: DB fetch failed for {exchange}/{date_str}: {exc}")
        return None


# ── Tier 3: broker fetch ──────────────────────────────────────────────────────

def _broker_fetch_sync(exchange: str) -> list[dict[str, Any]]:
    """Blocking call to broker.instruments(exchange). Run via asyncio.to_thread."""
    from backend.shared.brokers.registry import get_sparkline_broker
    broker = get_sparkline_broker()
    raw = broker.instruments(exchange) or []
    return list(raw)


def _rows_to_map(rows: list[dict[str, Any]], exchange: str) -> dict[tuple[str, str], int]:
    mapping: dict[tuple[str, str], int] = {}
    for row in rows:
        ts  = row.get("tradingsymbol")
        exch = row.get("exchange", exchange)
        tok = row.get("instrument_token")
        if ts and tok:
            mapping[(str(ts).upper(), str(exch))] = int(tok)
    return mapping


# ── Enqueue persistence ───────────────────────────────────────────────────────

def _enqueue_db(exchange: str, date_str: str, rows: list[dict[str, Any]]) -> None:
    from backend.api.persistence import write_queue
    # Slim the rows — only the fields we need for reconstruction to keep
    # the JSONB payload compact (drops strike, tick_size, lot_size, etc.).
    slim: list[dict[str, Any]] = []
    for r in rows:
        ts  = r.get("tradingsymbol")
        exch = r.get("exchange", exchange)
        tok = r.get("instrument_token")
        if ts and tok:
            slim.append({
                "tradingsymbol":    str(ts),
                "exchange":         str(exch),
                "instrument_token": int(tok),
            })
    write_queue.enqueue_db({
        "kind":      "instruments_snapshot",
        "exchange":  exchange,
        "date":      date_str,
        "payload":   slim,
        "row_count": len(slim),
    })


# ── Public API ────────────────────────────────────────────────────────────────

async def get_or_fetch_instruments(exchange: str) -> dict[tuple[str, str], int]:
    """Return {(tradingsymbol, exchange): instrument_token} for exchange.

    Read path: Tier 1 (memory) → Tier 2 (DB) → Tier 3 (broker).
    Write-back to Tier 1 + DB write queue happens only on Tier 3 hit.
    """
    exch    = exchange.upper().strip()
    today   = _ist_today()
    _purge_stale(today)
    mem_key = (today, exch)

    # Tier 1 — in-memory
    cached = _MEM_CACHE.get(mem_key)
    if cached is not None:
        return cached

    # Tier 2 — DB (outside lock — read is safe without serialisation)
    db_map = await _db_fetch(exch, today)
    if db_map is not None:
        _MEM_CACHE[mem_key] = db_map
        return db_map

    # Tier 3 — broker (deduplicated per exchange/date)
    lock = await _get_fetch_lock(mem_key)
    async with lock:
        # Re-check after acquiring — another coroutine may have populated.
        cached = _MEM_CACHE.get(mem_key)
        if cached is not None:
            return cached
        db_map2 = await _db_fetch(exch, today)
        if db_map2 is not None:
            _MEM_CACHE[mem_key] = db_map2
            return db_map2

        try:
            rows = await asyncio.to_thread(_broker_fetch_sync, exch)
        except Exception as exc:
            logger.warning(f"instruments_store: broker fetch failed for {exch}: {exc}")
            return {}

        mapping = _rows_to_map(rows, exch)
        if mapping:
            _MEM_CACHE[mem_key] = mapping
            _enqueue_db(exch, today, rows)
        else:
            logger.warning(
                f"instruments_store: broker returned 0 instruments for {exch} "
                "(not caching empty response)"
            )

        return mapping


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
