"""
Three-tier OHLCV read path:
  Tier 1 — in-memory dict keyed (symbol, exchange) → {date: bar}
  Tier 2 — PostgreSQL SELECT from ohlcv_daily
  Tier 3 — broker.historical_data() via asyncio.to_thread

After a broker fetch the result is immediately written to Tier 1 and
enqueued to both write workers (disk + DB) without blocking the caller.

Per-(symbol, exchange) asyncio.Lock deduplicates concurrent in-flight
fetches: the second coroutine that acquires the lock re-checks tiers 1
and 2 before calling the broker, so the broker is called at most once
per cold symbol.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import TypedDict


class OHLCVBar(TypedDict):
    date:   str    # YYYY-MM-DD
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int


# ── Tier 1: in-memory cache (LRU-bounded) ────────────────────────────────────
# OrderedDict acts as LRU: read/write moves a key to the most-recent end;
# when size exceeds _MEM_CACHE_MAX_KEYS we evict from the oldest end. Keeps
# steady-state memory bounded for a long-running process. 500 keys × ~250
# trading-day-years of bars × ~50 bytes/bar ≈ ~6 MB ceiling.
_MEM_CACHE_MAX_KEYS = 500
_MEM_CACHE: "OrderedDict[tuple[str, str], dict[str, OHLCVBar]]" = OrderedDict()

# Per-(symbol, exchange) lock to deduplicate concurrent broker fetches.
_FETCH_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_LOCK_MAP_LOCK = asyncio.Lock()


async def _get_fetch_lock(key: tuple[str, str]) -> asyncio.Lock:
    async with _LOCK_MAP_LOCK:
        if key not in _FETCH_LOCKS:
            _FETCH_LOCKS[key] = asyncio.Lock()
        return _FETCH_LOCKS[key]


def _is_complete_range(bars: list[OHLCVBar], from_d: date, to_d: date) -> bool:
    """Coverage check used by BOTH tier 1 (memory) and tier 2 (DB) reads.

    A daily-bar series has legitimate gaps (weekends + exchange holidays)
    so we cannot require every calendar day to be present. Instead:

    1. Boundary dates must be present (handles a fresh partial fetch
       that errored mid-stream and only persisted the head of the range).
    2. No consecutive bars may be > 4 days apart. A Fri→Mon weekend is
       3 calendar days; a Fri→Tue weekend with a holiday Monday is 4.
       Anything larger almost certainly means missing data, not a
       legitimate calendar gap.

    Returns False on either failure → caller falls back to broker. This
    is the "recognize missing data and fall back" guarantee.
    """
    if not bars:
        return False
    dates_sorted = sorted({b["date"] for b in bars})
    if dates_sorted[0] != from_d.isoformat() or dates_sorted[-1] != to_d.isoformat():
        return False
    prev = date.fromisoformat(dates_sorted[0])
    for s in dates_sorted[1:]:
        cur = date.fromisoformat(s)
        if (cur - prev).days > 4:
            return False
        prev = cur
    return True


def _mem_slice(key: tuple[str, str], from_d: date, to_d: date) -> list[OHLCVBar]:
    cached = _MEM_CACHE.get(key, {})
    if cached:
        _MEM_CACHE.move_to_end(key)
    bars = [v for k, v in cached.items() if from_d.isoformat() <= k <= to_d.isoformat()]
    bars.sort(key=lambda b: b["date"])
    return bars


def _mem_covers(key: tuple[str, str], from_d: date, to_d: date) -> bool:
    return _is_complete_range(_mem_slice(key, from_d, to_d), from_d, to_d)


def _mem_populate(key: tuple[str, str], bars: list[OHLCVBar]) -> None:
    if key not in _MEM_CACHE:
        _MEM_CACHE[key] = {}
    for bar in bars:
        _MEM_CACHE[key][bar["date"]] = bar
    _MEM_CACHE.move_to_end(key)
    while len(_MEM_CACHE) > _MEM_CACHE_MAX_KEYS:
        _MEM_CACHE.popitem(last=False)


# ── Tier 2: DB SELECT ─────────────────────────────────────────────────────────

async def _db_fetch(symbol: str, exchange: str, from_d: date, to_d: date) -> list[OHLCVBar]:
    from sqlalchemy import text
    from backend.api.database import async_session

    stmt = text("""
        SELECT date, open, high, low, close, volume
        FROM   ohlcv_daily
        WHERE  symbol   = :sym
          AND  exchange = :exch
          AND  date     BETWEEN :from_d AND :to_d
        ORDER BY date
    """)
    try:
        async with async_session() as session:
            result = await session.execute(stmt, {
                "sym": symbol, "exch": exchange,
                "from_d": from_d.isoformat(), "to_d": to_d.isoformat(),
            })
            rows = result.fetchall()
        return [
            OHLCVBar(
                date=str(r[0]),
                open=float(r[1]),  high=float(r[2]),
                low=float(r[3]),   close=float(r[4]),
                volume=int(r[5]),
            )
            for r in rows
        ]
    except Exception:
        return []


# Coverage is now centralised in _is_complete_range (above); both
# tiers reuse it so partial DB or partial memory is always re-fetched.


# ── Tier 3: broker fetch ──────────────────────────────────────────────────────

def _broker_fetch_sync(
    symbol: str, exchange: str, from_d: date, to_d: date,
) -> list[OHLCVBar]:
    from backend.shared.brokers.registry import get_price_broker
    from backend.api.routes.quote import _get_today_token_map

    broker = get_price_broker()
    token_map = _get_today_token_map(broker)

    exch_order = [exchange] + [e for e in ("NSE", "NFO", "BSE", "BFO", "MCX", "CDS") if e != exchange]
    token: int | None = None
    for ex in exch_order:
        token = token_map.get((symbol, ex))
        if token is not None:
            break
    if token is None:
        return []

    from_dt = datetime(from_d.year, from_d.month, from_d.day)
    to_dt   = datetime(to_d.year, to_d.month, to_d.day, 23, 59, 59)
    raw = broker.historical_data(token, from_dt, to_dt, "day") or []

    bars: list[OHLCVBar] = []
    for r in raw:
        raw_date = r.get("date")
        if raw_date is None:
            continue
        if hasattr(raw_date, "strftime"):
            date_str = raw_date.strftime("%Y-%m-%d")
        else:
            date_str = str(raw_date)[:10]
        try:
            bars.append(OHLCVBar(
                date=date_str,
                open=float(r.get("open", 0)),
                high=float(r.get("high", 0)),
                low=float(r.get("low", 0)),
                close=float(r.get("close", 0)),
                volume=int(r.get("volume", 0)),
            ))
        except (TypeError, ValueError):
            continue
    return bars


# ── Public API ────────────────────────────────────────────────────────────────

async def get_or_fetch_daily(
    symbol: str, exchange: str, from_d: date, to_d: date,
    bypass_cache: bool | None = None,
) -> list[OHLCVBar]:
    """Return daily OHLCV bars for symbol/exchange in [from_d, to_d].

    Read path: Tier 1 (memory) → Tier 2 (DB) → Tier 3 (broker).
    Write-back to Tier 1 + write queues happens only on Tier 3 hit.

    When `bypass_cache=True`, skips Tier 1 + Tier 2 entirely and goes
    straight to the broker. Used by `?fresh=1` query param and the
    global `persistence.bypass_db` settings flag. Defect-recovery
    tool: forces a re-fetch from broker truth and heals the persistent
    tiers on the write-back pass (the queue writer overwrites with the
    fresh data). Operator: "switch to use api with no db ... will help
    refresh cache and db if they are not accurate because code defects".
    """
    from backend.api.persistence import runtime_state
    if bypass_cache is None:
        bypass_cache = runtime_state.is_bypass_on()

    sym  = symbol.upper().strip()
    exch = exchange.upper().strip()
    key  = (sym, exch)

    if not bypass_cache:
        # Tier 1 — in-memory
        if _mem_covers(key, from_d, to_d):
            return _mem_slice(key, from_d, to_d)

        # Tier 2 — DB
        db_bars = await _db_fetch(sym, exch, from_d, to_d)
        if _is_complete_range(db_bars, from_d, to_d):
            _mem_populate(key, db_bars)
            return _mem_slice(key, from_d, to_d)

    # Tier 3 — broker (deduplicated per key). Always hit when bypass
    # is on, regardless of cache state.
    lock = await _get_fetch_lock(key)
    async with lock:
        if not bypass_cache:
            # Re-check after acquiring — another coroutine may have populated.
            if _mem_covers(key, from_d, to_d):
                return _mem_slice(key, from_d, to_d)
            db_bars2 = await _db_fetch(sym, exch, from_d, to_d)
            if _is_complete_range(db_bars2, from_d, to_d):
                _mem_populate(key, db_bars2)
                return _mem_slice(key, from_d, to_d)

        try:
            broker_bars = await asyncio.to_thread(_broker_fetch_sync, sym, exch, from_d, to_d)
        except Exception as exc:
            from backend.shared.helpers.ramboq_logger import get_logger as _gl
            _gl(__name__).warning(f"ohlcv_store: broker fetch failed {sym}/{exch}: {exc}")
            return []

        if broker_bars:
            _mem_populate(key, broker_bars)
            _enqueue_persist(sym, exch, broker_bars)

        return _mem_slice(key, from_d, to_d)


def _enqueue_persist(symbol: str, exchange: str, bars: list[OHLCVBar]) -> None:
    from backend.api.persistence import write_queue
    payload = {
        "kind":     "ohlcv_daily",
        "symbol":   symbol,
        "exchange": exchange,
        "bars":     list(bars),
    }
    write_queue.enqueue_disk(payload)
    write_queue.enqueue_db(payload)
