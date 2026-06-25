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

from backend.api.persistence.store_base import PersistentStoreBase
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class IntradayBar(TypedDict):
    bar_ts: str   # ISO-8601 UTC, the timestamp of the bar's close
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int


_MemKey = tuple[str, str, str, str]   # (symbol, exchange, date_iso, interval)

_TODAY_TTL_S = 300   # 5 minutes — bars are growing during the session


def _ist_today() -> str:
    """Return today's date in IST as YYYY-MM-DD."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _is_complete_historical(bars: list[IntradayBar], exchange: str) -> bool:
    """Check whether a historical (non-today) bar set spans the session.

    NSE/NFO/BSE/BFO: must have at least one bar with bar_ts >= 15:00 IST.
    MCX:             must have at least one bar with bar_ts >= 23:00 IST.
    Other/CDS:       must have at least one bar with bar_ts >= 15:30 IST.

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


# ── IntradayStore subclass ────────────────────────────────────────────────────

# Stored value: (cached_at_epoch: float, bars: list[IntradayBar])
_Entry = tuple[float, list[IntradayBar]]


class IntradayStore(PersistentStoreBase):
    _name     = "intraday_store"
    _max_keys = 500
    _lru      = True


    # ── Tier 1 freshness: 5-min TTL on today's bars ───────────────────────────

    def _mem_get_validator(self, value: _Entry, key: _MemKey) -> bool:
        """Enforce TTL for today's bars; historical bars are permanently valid."""
        cached_at, _bars = value
        _sym, _exch, date_iso, _interval = key
        today = _ist_today()
        if date_iso == today:
            return (time.time() - cached_at) < _TODAY_TTL_S
        return True   # historical bars are immutable once the session closes

    # ── Tier 1 completeness ──────────────────────────────────────────────────

    def _is_complete(self, value: _Entry, key: _MemKey) -> bool:
        """For today: partial is fine (session still growing).
        For historical: must span the session-close hour."""
        _cached_at, bars = value
        _sym, exch, date_iso, _interval = key
        is_today = date_iso == _ist_today()
        if is_today:
            return True   # any non-empty entry is "complete" for today
        return _is_complete_historical(bars, exch)

    # ── Tier 2: DB SELECT ────────────────────────────────────────────────────

    async def _db_fetch(self, key: _MemKey) -> _Entry | None:
        """Return (cached_at=0, bars) from DB, or None on miss.

        cached_at=0 means historical bars (never-expiring from TTL perspective).
        The completeness check uses _is_complete_historical for non-today queries.
        """
        from sqlalchemy import text
        from backend.api.database import async_session

        sym, exch, date_iso, interval = key
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
                    "sym": sym, "exch": exch,
                    "on_date": date_iso, "interval": interval,
                })
                rows = result.fetchall()
            bars = [
                IntradayBar(
                    bar_ts=r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
                    open=float(r[1]),   high=float(r[2]),
                    low=float(r[3]),    close=float(r[4]),
                    volume=int(r[5]),
                )
                for r in rows
            ]
            if not bars:
                return None
            # Use cached_at=0.0 so that for today's bars the TTL check
            # (_mem_get_validator) sees them as expired and forces a re-fetch.
            # Historical bars are always fresh regardless of cached_at.
            is_today = date_iso == _ist_today()
            cached_at = 0.0 if is_today else time.time()
            return (cached_at, bars)
        except Exception:
            return None

    # ── Tier 3: broker fetch ─────────────────────────────────────────────────

    async def _broker_fetch(self, key: _MemKey) -> _Entry | None:
        sym, exch, date_iso, interval = key
        on_date = date.fromisoformat(date_iso)
        bars = await asyncio.to_thread(_broker_fetch_sync, sym, exch, on_date, interval)
        if not bars:
            # Empty on pre-market, holiday, or unknown symbol — not cached.
            return None
        return (time.time(), bars)

    # ── Write-back ───────────────────────────────────────────────────────────

    def _enqueue_persist(self, key: _MemKey, value: _Entry) -> None:
        sym, exch, date_iso, interval = key
        _cached_at, bars = value
        from backend.api.persistence import write_queue
        payload = {
            "kind":     "intraday_bars",
            "symbol":   sym,
            "exchange": exch,
            "date":     date_iso,
            "interval": interval,
            "bars":     list(bars),
        }
        write_queue.enqueue_disk(payload)
        write_queue.enqueue_db(payload)

    # ── Override get() to unwrap the stored (cached_at, bars) tuple ──────────

    async def get(self, key: _MemKey, *, bypass_cache: bool | None = None) -> list[IntradayBar]:
        """Same three-tier flow as base, but unwraps the (cached_at, bars) entry.

        On a Tier 3 miss (broker returned empty / pre-market / holiday), fall
        back to whatever Tier 1 still holds — even if its TTL is expired — so
        callers get the last-known bars rather than an empty list.  This matches
        the original behaviour:
            entry2 = _mem_get(key)
            return entry2[1] if entry2 else []
        """
        result = await super().get(key, bypass_cache=bypass_cache)
        if result is not None:
            # Tier 1 / Tier 2 / Tier 3 hit: unwrap if needed.
            if isinstance(result, tuple):
                return result[1]
            return result  # type: ignore[return-value]

        # Full miss (broker returned None/empty).
        # Serve TTL-expired Tier 1 entry if available (stale-on-miss).
        mk = self._mem_key(key)
        stale = self._mem_cache.get(mk)
        if stale is not None:
            _cached_at, bars = stale
            return bars
        return []


# ── Module-level singleton + backward-compat alias ───────────────────────────

_intraday_store = IntradayStore()

# runtime_state.invalidate_intraday() reaches into _MEM_CACHE.
# Value shape: (cached_at_epoch, list[IntradayBar]) — same as original.
_MEM_CACHE: "OrderedDict[_MemKey, _Entry]" = _intraday_store._mem_cache  # type: ignore[assignment]


# ── Tier 3 sync helper (module-level) ────────────────────────────────────────

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
    sym  = symbol.upper().strip()
    exch = exchange.upper().strip()
    key: _MemKey = (sym, exch, on_date.isoformat(), interval)
    return await _intraday_store.get(key, bypass_cache=bypass_cache)
