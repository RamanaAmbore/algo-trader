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
from datetime import date, datetime
from typing import Any, TypedDict

from backend.api.persistence.store_base import PersistentStoreBase
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class OHLCVBar(TypedDict):
    date:   str    # YYYY-MM-DD
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int


# ── Completeness check ────────────────────────────────────────────────────────

def _is_complete_range(bars: list[OHLCVBar], from_d: date, to_d: date) -> bool:
    """Coverage check used by BOTH tier 1 (memory) and tier 2 (DB) reads.

    A daily-bar series has legitimate gaps (weekends + exchange holidays)
    so we cannot require every calendar day to be present. Instead:

    1. Boundary dates must be present (handles a fresh partial fetch
       that errored mid-stream and only persisted the head of the range).
    2. No consecutive bars may be > 6 days apart. A Fri→Mon weekend is
       3 calendar days; a Fri→Tue with a holiday Monday is 4. Diwali
       week / election week / Holi clusters can produce legitimate
       5-6 day gaps (Sat-Sun + 2-3 holiday weekdays), so 6 is the
       conservative ceiling that still catches truly missing data
       (e.g. a fetch that errored mid-stream) but doesn't false-
       positive on Indian-market holiday clusters. Slice AQ caught
       the prior 4-day ceiling as too tight.

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
        if (cur - prev).days > 6:
            return False
        prev = cur
    return True


# ── OHLCVStore subclass ───────────────────────────────────────────────────────

# Full lookup key type: (sym, exch, from_d_iso, to_d_iso)
# Storage key type:     (sym, exch)

_FullKey = tuple[str, str, str, str]    # (sym, exch, from_iso, to_iso)
_MemKey  = tuple[str, str]              # (sym, exch)


class OHLCVStore(PersistentStoreBase):
    _name     = "ohlcv_store"
    _max_keys = 500
    _lru      = True

    # ── Key normalisation ────────────────────────────────────────────────────

    def _mem_key(self, key: _FullKey) -> _MemKey:
        # Strip the date range — storage is by (sym, exch) only.
        return (key[0], key[1])

    # ── Merge-on-write (bars accumulate in the existing dict) ────────────────

    def _mem_set(self, key: _FullKey, value: list[OHLCVBar]) -> None:
        """Merge new bars into the existing date-keyed dict for this symbol,
        rather than replacing the whole entry.  Preserves bars from other
        date ranges already in cache (e.g. a previous shorter fetch).
        Then applies LRU eviction as usual."""
        mk: _MemKey = self._mem_key(key)
        if mk not in self._mem_cache:
            self._mem_cache[mk] = {}
        for bar in value:
            self._mem_cache[mk][bar["date"]] = bar
        self._mem_cache.move_to_end(mk)  # type: ignore[attr-defined]
        while len(self._mem_cache) > self._max_keys:
            ek, ev = self._mem_cache.popitem(last=False)  # type: ignore[attr-defined]
            self._on_mem_evict(ek, ev)

    # ── Tier 1 completeness ──────────────────────────────────────────────────

    def _is_complete(self, value: dict[str, OHLCVBar] | list[OHLCVBar], key: _FullKey) -> bool:
        """value is the raw stored dict or the list returned from _db_fetch.
        key carries the requested [from_d, to_d] range."""
        from_d = date.fromisoformat(key[2])
        to_d   = date.fromisoformat(key[3])
        if isinstance(value, dict):
            bars = [v for k, v in value.items() if key[2] <= k <= key[3]]
            bars.sort(key=lambda b: b["date"])
        else:
            bars = list(value)
        return _is_complete_range(bars, from_d, to_d)

    # ── Tier 2: DB SELECT ────────────────────────────────────────────────────

    async def _db_fetch(self, key: _FullKey) -> list[OHLCVBar] | None:
        from sqlalchemy import text
        from backend.api.database import async_session

        sym, exch, from_iso, to_iso = key
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
                    "sym": sym, "exch": exch,
                    "from_d": from_iso, "to_d": to_iso,
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

    # ── Tier 3: broker fetch ─────────────────────────────────────────────────

    async def _broker_fetch(self, key: _FullKey) -> list[OHLCVBar]:
        sym, exch, from_iso, to_iso = key
        from_d = date.fromisoformat(from_iso)
        to_d   = date.fromisoformat(to_iso)
        return await asyncio.to_thread(_broker_fetch_sync, sym, exch, from_d, to_d)

    # ── Write-back ───────────────────────────────────────────────────────────

    def _enqueue_persist(self, key: _FullKey, value: list[OHLCVBar]) -> None:
        sym, exch = key[0], key[1]
        _enqueue_persist_impl(sym, exch, value)


# ── Module-level singleton ────────────────────────────────────────────────────

_ohlcv_store = OHLCVStore()

# Backward-compat alias: runtime_state.invalidate_ohlcv() reaches into _MEM_CACHE.
_MEM_CACHE: "OrderedDict[_MemKey, dict[str, OHLCVBar]]" = _ohlcv_store._mem_cache  # type: ignore[assignment]


# ── Tier 3 sync helper (module-level, called via asyncio.to_thread) ───────────

def _broker_fetch_sync(
    symbol: str, exchange: str, from_d: date, to_d: date,
) -> list[OHLCVBar]:
    from backend.brokers.registry import get_price_broker
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


def _enqueue_persist_impl(symbol: str, exchange: str, bars: list[OHLCVBar]) -> None:
    from backend.api.persistence import write_queue
    payload: dict[str, Any] = {
        "kind":     "ohlcv_daily",
        "symbol":   symbol,
        "exchange": exchange,
        "bars":     list(bars),
    }
    write_queue.enqueue_disk(payload)
    write_queue.enqueue_db(payload)


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
    sym  = symbol.upper().strip()
    exch = exchange.upper().strip()
    full_key: _FullKey = (sym, exch, from_d.isoformat(), to_d.isoformat())

    # get() returns the list of OHLCVBar (Tier 3 result) or None.
    # After a Tier 1/2 hit it returns the raw dict; after Tier 3 it returns
    # the list.  We need bars sliced to [from_d, to_d] in all paths.
    result = await _ohlcv_store.get(full_key, bypass_cache=bypass_cache)

    if result is None:
        return []

    # result may be list[OHLCVBar] (from Tier 3 / Tier 2 path) or the full
    # date-keyed dict (from Tier 1 path via _mem_get).  In both cases, slice.
    if isinstance(result, dict):
        bars = [v for k, v in result.items() if from_d.isoformat() <= k <= to_d.isoformat()]
        bars.sort(key=lambda b: b["date"])
        return bars

    # list from Tier 2 or Tier 3 — already filtered to requested range.
    return list(result)
