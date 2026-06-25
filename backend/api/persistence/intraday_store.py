"""
Three-tier intraday-bar read path:
  Tier 1 — in-memory LRU dict keyed (symbol, exchange, date, interval) → list[IntradayBar]
            TTL: 5 minutes from last fetch (intraday bars are growing — short TTL keeps
            the right edge of the sparkline current during the session).
  Tier 2 — PostgreSQL SELECT from intraday_bars WHERE matches.
  Tier 3 — broker.historical_data() via asyncio.to_thread, round-robin across
            get_historical_brokers().

After a broker fetch the result is immediately written to Tier 1 and enqueued
to both write workers (disk + DB) without blocking the caller.

Per-(symbol, exchange, date, interval) asyncio.Lock deduplicates concurrent
in-flight fetches: the second coroutine that acquires the lock re-checks tiers
1 and 2 before calling the broker, so the broker is called at most once per
cold key within the TTL window.

Completeness rule:
  - Today's bars: always serve what we have (intraday is still growing — partial
    is OK). A fresh fetch is triggered only when the TTL (5 min) has elapsed.
  - Historical (yesterday and earlier): bar_ts span must reach at least:
      NSE/NFO/BSE/BFO: 09:30 → 15:00 IST
      MCX:             09:30 → 23:00 IST
      Other/CDS:       09:30 → 15:30 IST
    If the span is too short, treat as a miss and re-fetch from broker.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import TypedDict


class IntradayBar(TypedDict):
    bar_ts: str   # ISO-8601 UTC, the timestamp of the bar's close
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int


# ── Tier 1: in-memory LRU cache ──────────────────────────────────────────────
# OrderedDict acts as LRU: read/write moves key to most-recent end; when size
# exceeds _MEM_CACHE_MAX_KEYS we evict from the oldest end. 500 keys × ~14
# 30-min bars/day × ~60 bytes/bar ≈ ~420 kB ceiling.
_MEM_CACHE_MAX_KEYS = 500
# Value shape: (cached_at_epoch, list[IntradayBar])
_MEM_CACHE: "OrderedDict[tuple[str, str, str, str], tuple[float, list[IntradayBar]]]" = OrderedDict()

_TODAY_TTL_S = 300   # 5 minutes — bars are growing during the session

# Per-(symbol, exchange, date, interval) lock to deduplicate concurrent fetches.
_FETCH_LOCKS: dict[tuple[str, str, str, str], asyncio.Lock] = {}
_LOCK_MAP_LOCK = asyncio.Lock()


async def _get_fetch_lock(key: tuple[str, str, str, str]) -> asyncio.Lock:
    async with _LOCK_MAP_LOCK:
        if key not in _FETCH_LOCKS:
            _FETCH_LOCKS[key] = asyncio.Lock()
        return _FETCH_LOCKS[key]


def _ist_today() -> str:
    """Return today's date in IST as YYYY-MM-DD."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _mem_get(key: tuple[str, str, str, str]) -> tuple[float, list[IntradayBar]] | None:
    """Return (cached_at, bars) if present in Tier 1, else None. Moves to end (LRU)."""
    if key in _MEM_CACHE:
        _MEM_CACHE.move_to_end(key)
        return _MEM_CACHE[key]
    return None


def _mem_set(key: tuple[str, str, str, str], bars: list[IntradayBar]) -> None:
    """Populate Tier 1. Evicts oldest key when capacity exceeded."""
    _MEM_CACHE[key] = (time.time(), bars)
    _MEM_CACHE.move_to_end(key)
    while len(_MEM_CACHE) > _MEM_CACHE_MAX_KEYS:
        _MEM_CACHE.popitem(last=False)


def _is_fresh(cached_at: float, on_date: date) -> bool:
    """Decide whether a Tier 1 entry is usable without a re-fetch.

    For today's bars (still growing): apply the 5-minute TTL.
    For historical bars (yesterday and earlier): consider them permanently
    fresh once cached — immutable once the session closes.
    """
    today = _ist_today()
    if on_date.isoformat() == today:
        return (time.time() - cached_at) < _TODAY_TTL_S
    return True   # historical bars are immutable


def _is_complete_historical(bars: list[IntradayBar], exchange: str) -> bool:
    """Check whether a historical (non-today) bar set spans the session.

    NSE/NFO/BSE/BFO: must have at least one bar with bar_ts ≥ 15:00 IST.
    MCX:             must have at least one bar with bar_ts ≥ 23:00 IST.
    Other/CDS:       must have at least one bar with bar_ts ≥ 15:30 IST.

    Returns True also when bars is empty AND on_date is a weekend (we don't
    fetch on non-trading days); the caller never calls this for today.
    """
    if not bars:
        return False
    exch = exchange.upper()
    if exch in ("MCX",):
        # IST 23:00 = UTC 17:30
        cutoff_hour_utc = 17
        cutoff_min_utc  = 30
    elif exch in ("NSE", "NFO", "BSE", "BFO"):
        # IST 15:00 = UTC 09:30
        cutoff_hour_utc = 9
        cutoff_min_utc  = 30
    else:
        # CDS and others — IST 15:30 = UTC 10:00
        cutoff_hour_utc = 10
        cutoff_min_utc  = 0

    for bar in bars:
        try:
            ts = datetime.fromisoformat(bar["bar_ts"].replace("Z", "+00:00"))
            if ts.hour > cutoff_hour_utc or (ts.hour == cutoff_hour_utc and ts.minute >= cutoff_min_utc):
                return True
        except Exception:
            continue
    return False


# ── Tier 2: DB SELECT ─────────────────────────────────────────────────────────

async def _db_fetch(
    symbol: str, exchange: str, on_date: date, interval: str,
) -> list[IntradayBar]:
    from sqlalchemy import text
    from backend.api.database import async_session

    stmt = text("""
        SELECT bar_ts, open, high, low, close, volume
        FROM   intraday_bars
        WHERE  symbol   = :sym
          AND  exchange = :exch
          AND  date     = :on_date
          AND  interval = :interval
        ORDER BY bar_ts
    """)
    try:
        async with async_session() as session:
            result = await session.execute(stmt, {
                "sym": symbol, "exch": exchange,
                "on_date": on_date.isoformat(), "interval": interval,
            })
            rows = result.fetchall()
        return [
            IntradayBar(
                bar_ts=r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
                open=float(r[1]),   high=float(r[2]),
                low=float(r[3]),    close=float(r[4]),
                volume=int(r[5]),
            )
            for r in rows
        ]
    except Exception:
        return []


# ── Tier 3: broker fetch ──────────────────────────────────────────────────────

def _broker_fetch_sync(
    symbol: str, exchange: str, on_date: date, interval: str,
) -> list[IntradayBar]:
    from backend.shared.brokers.registry import get_historical_brokers
    from backend.api.routes.quote import _get_today_token_map

    try:
        brokers = get_historical_brokers() or []
    except Exception:
        brokers = []

    broker = brokers[0] if brokers else None
    if broker is None:
        return []

    token_map = _get_today_token_map(broker)

    exch_order = [exchange] + [e for e in ("NSE", "NFO", "BSE", "BFO", "MCX", "CDS") if e != exchange]
    token: int | None = None
    for ex in exch_order:
        token = token_map.get((symbol, ex))
        if token is not None:
            break
    if token is None:
        return []

    # Fetch the full session for on_date. Use from midnight to end-of-day
    # so we capture the complete session without timezone gymnastics.
    from_dt = datetime(on_date.year, on_date.month, on_date.day, 0, 0, 0)
    to_dt   = datetime(on_date.year, on_date.month, on_date.day, 23, 59, 59)
    try:
        raw = broker.historical_data(token, from_dt, to_dt, interval) or []
    except Exception:
        return []

    bars: list[IntradayBar] = []
    for r in raw:
        raw_ts = r.get("date")
        if raw_ts is None:
            continue
        if hasattr(raw_ts, "isoformat"):
            bar_ts = raw_ts.astimezone(timezone.utc).isoformat(timespec="seconds")
        else:
            bar_ts = str(raw_ts)
        try:
            bars.append(IntradayBar(
                bar_ts=bar_ts,
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

async def get_or_fetch_intraday(
    symbol: str, exchange: str, on_date: date,
    interval: str = "30minute",
    bypass_cache: bool | None = None,
) -> list[IntradayBar]:
    """Return intraday OHLCV bars for symbol/exchange/date at the given interval.

    Read path: Tier 1 (memory) → Tier 2 (DB) → Tier 3 (broker).
    Write-back to Tier 1 + write queues happens only on Tier 3 hit.

    For today: partial results are OK (session still in progress); Tier 1
    is served when within the 5-minute TTL, re-fetched when stale.
    For historical: results must span the session-close hour; short results
    fall through to broker.

    When `bypass_cache=True`, skips Tier 1 + Tier 2 entirely (defect-recovery
    path — fresh broker data heals the persistent tiers on write-back).
    """
    from backend.api.persistence import runtime_state

    if bypass_cache is None:
        bypass_cache = runtime_state.is_bypass_on()

    sym  = symbol.upper().strip()
    exch = exchange.upper().strip()
    key  = (sym, exch, on_date.isoformat(), interval)
    today_str = _ist_today()
    is_today  = on_date.isoformat() == today_str

    if not bypass_cache:
        # Tier 1 — in-memory
        entry = _mem_get(key)
        if entry is not None:
            cached_at, cached_bars = entry
            if _is_fresh(cached_at, on_date):
                if is_today or _is_complete_historical(cached_bars, exch):
                    return cached_bars

        # Tier 2 — DB
        db_bars = await _db_fetch(sym, exch, on_date, interval)
        if db_bars:
            if is_today or _is_complete_historical(db_bars, exch):
                _mem_set(key, db_bars)
                return db_bars

    # Tier 3 — broker (deduplicated per key).
    lock = await _get_fetch_lock(key)
    async with lock:
        if not bypass_cache:
            # Re-check after acquiring — another coroutine may have populated.
            entry = _mem_get(key)
            if entry is not None:
                cached_at, cached_bars = entry
                if _is_fresh(cached_at, on_date):
                    if is_today or _is_complete_historical(cached_bars, exch):
                        return cached_bars
            db_bars2 = await _db_fetch(sym, exch, on_date, interval)
            if db_bars2:
                if is_today or _is_complete_historical(db_bars2, exch):
                    _mem_set(key, db_bars2)
                    return db_bars2

        try:
            broker_bars = await asyncio.to_thread(
                _broker_fetch_sync, sym, exch, on_date, interval,
            )
        except Exception as exc:
            from backend.shared.helpers.ramboq_logger import get_logger as _gl
            _gl(__name__).warning(
                f"intraday_store: broker fetch failed {sym}/{exch}/{on_date}: {exc}"
            )
            return []

        if broker_bars:
            _mem_set(key, broker_bars)
            _enqueue_persist(sym, exch, on_date, interval, broker_bars)

        # Return whatever we got (may be empty for pre-market / holiday)
        entry2 = _mem_get(key)
        return entry2[1] if entry2 else []


def _enqueue_persist(
    symbol: str, exchange: str, on_date: date, interval: str,
    bars: list[IntradayBar],
) -> None:
    from backend.api.persistence import write_queue
    payload = {
        "kind":     "intraday_bars",
        "symbol":   symbol,
        "exchange": exchange,
        "date":     on_date.isoformat(),
        "interval": interval,
        "bars":     list(bars),
    }
    write_queue.enqueue_disk(payload)
    write_queue.enqueue_db(payload)
