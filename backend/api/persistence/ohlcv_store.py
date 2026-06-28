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

Partial-range fetch (get_or_fetch_daily):
  When Tier 2 holds bars for part of the requested range, only the
  missing slice(s) are fetched from the broker — not the full range.
  Gaps ≤ 6 days are treated as legitimate market closures (weekends +
  holidays) and do NOT trigger a broker fetch.  The four gap cases
  handled by _compute_missing_ranges():
    a. DB range entirely inside requested range → two slices (head + tail)
    b. DB overlaps tail of requested → fetch missing head
    c. DB overlaps head of requested → fetch missing tail
    d. DB disjoint from requested      → fetch full requested range
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import date, datetime, timedelta
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


# ── Missing-range computation ─────────────────────────────────────────────────

_MARKET_GAP_DAYS = 6   # gaps ≤ this many calendar days are legitimate (weekends + holidays)


def _compute_missing_ranges(
    existing_bars: list[OHLCVBar],
    from_d: date,
    to_d: date,
) -> list[tuple[date, date]]:
    """Return the date ranges that are NOT covered by `existing_bars` within
    the interval [from_d, to_d].

    Rules:
    - Gaps ≤ _MARKET_GAP_DAYS (6) calendar days between consecutive existing
      bars are treated as legitimate market closures and do NOT split the
      covered range into two separate holes.
    - The function returns the minimum set of (start, end) pairs that the
      caller must fetch from the broker to produce a complete [from_d, to_d]
      series when merged with existing_bars.

    Four structural cases (returned slices):
      a. existing entirely inside [from_d, to_d] → [(from_d, existing_min-1),
                                                      (existing_max+1, to_d)]
      b. existing overlaps tail of requested → [(from_d, existing_min-1)]
      c. existing overlaps head of requested → [(existing_max+1, to_d)]
      d. existing disjoint / empty            → [(from_d, to_d)]

    Gaps ≤ _MARKET_GAP_DAYS between the boundary of existing_bars and the
    requested boundary are absorbed (treated as holidays), so a request for
    Mon 2026-01-05 where the existing bars end on Fri 2025-12-26 (gap=10 days)
    would still produce a fetch, but a gap of 4 days (e.g. Easter + Easter
    Monday) would not.
    """
    if not existing_bars:
        return [(from_d, to_d)]

    existing_dates = sorted({b["date"] for b in existing_bars})
    db_min = date.fromisoformat(existing_dates[0])
    db_max = date.fromisoformat(existing_dates[-1])

    missing: list[tuple[date, date]] = []

    # Head gap: from_d .. db_min-1 (if more than _MARKET_GAP_DAYS away)
    if (db_min - from_d).days > _MARKET_GAP_DAYS:
        head_to = db_min - timedelta(days=1)
        if head_to >= from_d:
            missing.append((from_d, head_to))

    # Tail gap: db_max+1 .. to_d (if more than _MARKET_GAP_DAYS away)
    if (to_d - db_max).days > _MARKET_GAP_DAYS:
        tail_from = db_max + timedelta(days=1)
        if tail_from <= to_d:
            missing.append((tail_from, to_d))

    return missing


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

    # ── Partial-range fetch (used by get_or_fetch_daily) ─────────────────────

    async def _db_fetch_existing(self, sym: str, exch: str,
                                  from_d: date, to_d: date) -> list[OHLCVBar]:
        """Return whatever bars exist in DB for (sym, exch) in [from_d, to_d].
        Unlike _db_fetch, this does NOT check completeness — it is used only to
        learn what the DB already has so the caller can compute missing ranges."""
        key: _FullKey = (sym, exch, from_d.isoformat(), to_d.isoformat())
        result = await self._db_fetch(key)
        return result if result is not None else []

    async def _fetch_slice(self, sym: str, exch: str,
                            from_d: date, to_d: date,
                            today: date) -> list[OHLCVBar]:
        """Fetch [from_d, to_d] from broker (Tier 3) for a single slice.
        Persists bars up to (but not including) today — today's bar is
        unsettled while the session is open, so it is excluded from the
        durable store but still returned to the caller.

        Uses the per-(sym, exch) asyncio.Lock to deduplicate concurrent
        in-flight slices for the same symbol.
        """
        full_key: _FullKey = (sym, exch, from_d.isoformat(), to_d.isoformat())
        lock = await self._get_lock(full_key)
        async with lock:
            # Re-check Tier 1 after acquiring (another coroutine may have
            # already populated it for this slice).
            cached = self._mem_get(full_key)
            if cached is not None and self._is_complete(cached, full_key):
                self._tier1_hits += 1
                if isinstance(cached, dict):
                    return [v for k, v in cached.items()
                            if from_d.isoformat() <= k <= to_d.isoformat()]
                return list(cached)

            try:
                bars = await self._broker_fetch(full_key)
                self._tier3_fetches += 1
            except Exception as exc:
                self._tier3_errors += 1
                logger.warning(
                    f"{self._name}: slice broker fetch failed "
                    f"{sym}/{exch} [{from_d}..{to_d}]: {exc}"
                )
                return []

            if bars:
                self._mem_set(full_key, bars)
                # Persist only bars strictly before today (immutable-day rule).
                persist_bars = [b for b in bars if b["date"] < today.isoformat()]
                if persist_bars:
                    _enqueue_persist_impl(sym, exch, persist_bars)

            return bars


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

    Read path (normal): Tier 1 (memory) → Tier 2 (DB) → Tier 3 (broker).
    Write-back to Tier 1 + write queues happens only on Tier 3 hit.

    Partial-range optimisation: when Tier 2 has bars for part of the
    requested range, only the missing slice(s) are fetched from the
    broker.  The existing DB bars and the freshly-fetched slices are
    merged and returned as a single chronologically sorted list.  This
    avoids the prior behaviour where a 1Y request with 6 months in DB
    triggered a full 365-day broker fetch.

    Gap rule: gaps ≤ _MARKET_GAP_DAYS (6) calendar days between the
    boundary of existing DB coverage and the requested boundary are
    treated as legitimate market closures (weekends + holiday clusters)
    and do NOT trigger an extra broker slice.

    Today-edge: the bar for today is unsettled while the session is
    open (the close price is live LTP).  Bars fetched from the broker
    for today are returned to the caller but NOT persisted to the durable
    store (immutable-day semantics per CLAUDE.md).

    When `bypass_cache=True`, skips Tier 1 + Tier 2 entirely and goes
    straight to the broker for the full range. Used by `?fresh=1` query
    param and the global `persistence.bypass_db` settings flag.
    Defect-recovery tool: forces a re-fetch from broker truth and heals
    the persistent tiers on the write-back pass.
    """
    from backend.api.persistence import runtime_state

    sym  = symbol.upper().strip()
    exch = exchange.upper().strip()
    full_key: _FullKey = (sym, exch, from_d.isoformat(), to_d.isoformat())

    if bypass_cache is None:
        bypass_cache = runtime_state.is_bypass_on()

    # ── Bypass: skip all cache tiers, full broker fetch ───────────────────────
    if bypass_cache:
        # Delegate to the base get() with bypass_cache=True.  That path
        # increments _bypass_reads and calls _broker_fetch for the full range.
        result = await _ohlcv_store.get(full_key, bypass_cache=True)
        if result is None:
            return []
        if isinstance(result, dict):
            bars = [v for k, v in result.items()
                    if from_d.isoformat() <= k <= to_d.isoformat()]
            bars.sort(key=lambda b: b["date"])
            return bars
        return list(result)

    # ── Tier 1: in-memory cache ───────────────────────────────────────────────
    cached = _ohlcv_store._mem_get(full_key)
    if cached is not None and _ohlcv_store._is_complete(cached, full_key):
        _ohlcv_store._tier1_hits += 1
        if isinstance(cached, dict):
            bars = [v for k, v in cached.items()
                    if from_d.isoformat() <= k <= to_d.isoformat()]
            bars.sort(key=lambda b: b["date"])
            return bars
        return list(cached)

    # ── Tier 2: DB fetch (unconditional — we need what's there) ───────────────
    today = date.today()
    db_bars = await _ohlcv_store._db_fetch_existing(sym, exch, from_d, to_d)

    if db_bars and _is_complete_range(db_bars, from_d, to_d):
        # DB covers the full range — populate Tier 1 and return.
        _ohlcv_store._mem_set(full_key, db_bars)
        _ohlcv_store._tier2_hits += 1
        return sorted(db_bars, key=lambda b: b["date"])

    # ── Partial-range fetch: compute missing slices ────────────────────────────
    missing = _compute_missing_ranges(db_bars, from_d, to_d)

    if not missing:
        # Gaps are all within the holiday/weekend tolerance — DB bars suffice.
        _ohlcv_store._mem_set(full_key, db_bars)
        _ohlcv_store._tier2_hits += 1
        return sorted(db_bars, key=lambda b: b["date"])

    # Fetch each missing slice from the broker (Tier 3).
    # Slices are independent so we can fire them concurrently.
    slice_results = await asyncio.gather(
        *[
            _ohlcv_store._fetch_slice(sym, exch, s_from, s_to, today)
            for s_from, s_to in missing
        ],
        return_exceptions=True,
    )

    # Merge: existing DB bars + newly fetched slices.
    merged: dict[str, OHLCVBar] = {b["date"]: b for b in db_bars}
    for res in slice_results:
        if isinstance(res, Exception):
            logger.warning(
                f"ohlcv_store: slice fetch raised {res} for {sym}/{exch} — "
                "returning partial coverage"
            )
            continue
        for bar in res:  # type: ignore[union-attr]
            merged[bar["date"]] = bar

    if not merged:
        return []

    all_bars = sorted(merged.values(), key=lambda b: b["date"])

    # Populate Tier 1 with the full merged set for future range queries.
    _ohlcv_store._mem_set(full_key, all_bars)

    return all_bars
