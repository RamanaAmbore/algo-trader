"""
PersistentStoreBase — shared three-tier (memory → DB → broker API) read pattern.

Subclasses provide data-class-specific behaviour via abstract methods.
The base handles:
  - LRU-bounded in-memory cache (mem_key → value)
  - Per-key asyncio.Lock for fetch dedup
  - Bypass-mode check via runtime_state.is_bypass_on()
  - Tier 1 → Tier 2 → Tier 3 routing with re-check after lock
  - Write-back enqueue on Tier 3 hit
  - LRU eviction when mem_cache exceeds max_keys

Override hooks (abstract):
  _db_fetch(key)           — Tier 2 read; return value or None
  _broker_fetch(key)       — Tier 3 read; return value (raises on failure)
  _is_complete(value, key) — coverage check; True = serve from cache as-is
  _enqueue_persist(key, value) — push to write_queue on Tier 3 hit

Optional hooks (default no-op / always-valid):
  _mem_key(key)            — normalise lookup key → storage key (default: identity)
                             used by ohlcv, which stores under (sym, exch) but looks
                             up under (sym, exch, from_d, to_d)
  _on_mem_evict(key, value) — called when LRU evicts; default: no-op
  _mem_get_validator(value, key) — extra freshness check on Tier 1; default: True
                             intraday_store overrides to enforce 5-min TTL on today

Class attributes (override in subclass):
  _name     (str)  — used in log lines
  _max_keys (int)  — LRU cap (default 500)
  _lru      (bool) — True → use OrderedDict with LRU eviction
                     False → plain dict with no eviction (instruments/holidays)
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class PersistentStoreBase(ABC):
    _name:     str  = "store"
    _max_keys: int  = 500
    _lru:      bool = True   # set False for stores that manage eviction themselves

    def __init__(self) -> None:
        if self._lru:
            self._mem_cache: dict[Any, Any] = OrderedDict()
        else:
            self._mem_cache = {}
        self._fetch_locks:   dict[Any, asyncio.Lock] = {}
        self._lock_map_lock: asyncio.Lock             = asyncio.Lock()
        # Tier-hit counters — reset on process restart.  Surfaced via
        # get_metrics() on /admin/health so the operator can see the
        # actual cache-vs-broker pressure per store.  Tier-1 hits are
        # the cheapest; tier-3 fetches are the expensive ones.
        self._tier1_hits:    int = 0
        self._tier2_hits:    int = 0
        self._tier3_fetches: int = 0
        self._tier3_errors:  int = 0
        self._bypass_reads:  int = 0

    # ── Abstract hooks ───────────────────────────────────────────────────────────

    @abstractmethod
    async def _db_fetch(self, key: Any) -> Any:
        """Tier 2 read.  Return the stored value or None on miss/error."""

    @abstractmethod
    async def _broker_fetch(self, key: Any) -> Any:
        """Tier 3 read.  Return the fetched value.  May raise on failure."""

    @abstractmethod
    def _is_complete(self, value: Any, key: Any) -> bool:
        """True if value covers the request described by key; serves from cache."""

    @abstractmethod
    def _enqueue_persist(self, key: Any, value: Any) -> None:
        """Push a Tier 3 result to the write_queue (disk and/or DB)."""

    # ── Optional hooks ───────────────────────────────────────────────────────────

    def _mem_key(self, key: Any) -> Any:
        """Normalise the lookup key to the storage key.

        Default: identity (storage key == lookup key).
        Override in stores where the lookup key carries extra context that
        should NOT be part of the cache key (e.g. ohlcv date range).
        """
        return key

    def _on_mem_evict(self, key: Any, value: Any) -> None:
        """Called when LRU evicts an entry. Default: drop the per-key
        fetch lock alongside the mem entry. Without this, _fetch_locks
        grows monotonically over a long session as movers rotate
        through (the mem entry gets LRU-evicted but the lock stayed
        forever) — slice AQ caught this as a slow memory leak."""
        self._fetch_locks.pop(key, None)

    def _mem_get_validator(self, value: Any, key: Any) -> bool:
        """Extra freshness check on Tier 1 hits.

        Default: always valid.
        intraday_store overrides this to enforce a 5-min TTL on today's bars.
        instruments_store handles staleness via a pre-call _purge_stale() instead.
        """
        return True

    # ── Public API ───────────────────────────────────────────────────────────────

    async def get(self, key: Any, *, bypass_cache: bool | None = None) -> Any:
        """Three-tier read.  Returns None (or empty sentinel) on full miss.

        When bypass_cache is None it reads runtime_state.is_bypass_on().
        """
        from backend.api.persistence import runtime_state
        if bypass_cache is None:
            bypass_cache = runtime_state.is_bypass_on()

        if bypass_cache:
            self._bypass_reads += 1

        if not bypass_cache:
            # Tier 1
            cached = self._mem_get(key)
            if cached is not None and self._is_complete(cached, key):
                self._tier1_hits += 1
                return cached

            # Tier 2
            db_value = await self._db_fetch(key)
            if db_value is not None and self._is_complete(db_value, key):
                self._mem_set(key, db_value)
                self._tier2_hits += 1
                return db_value

        # Tier 3 — deduplicated per mem_key
        lock = await self._get_lock(key)
        async with lock:
            if not bypass_cache:
                # Re-check after acquiring — another coroutine may have filled it.
                cached = self._mem_get(key)
                if cached is not None and self._is_complete(cached, key):
                    self._tier1_hits += 1
                    return cached
                db_value2 = await self._db_fetch(key)
                if db_value2 is not None and self._is_complete(db_value2, key):
                    self._mem_set(key, db_value2)
                    self._tier2_hits += 1
                    return db_value2

            try:
                value = await self._broker_fetch(key)
                self._tier3_fetches += 1
            except Exception as exc:
                self._tier3_errors += 1
                logger.warning(f"{self._name}: broker fetch failed for {key}: {exc}")
                return None

            if value is not None:
                self._mem_set(key, value)
                self._enqueue_persist(key, value)
            return value

    # ── Metrics ──────────────────────────────────────────────────────────────────

    def get_metrics(self) -> dict[str, Any]:
        """Snapshot of tier hit counters.  Reset on process restart.

        hit_rate is the share of non-bypass reads served from cache (tier
        1 OR 2). A high hit_rate means most reads avoid the broker
        round-trip — exactly what the persistence pipeline buys us.
        """
        total = self._tier1_hits + self._tier2_hits + self._tier3_fetches
        cache_hits = self._tier1_hits + self._tier2_hits
        hit_rate = (cache_hits / total) if total else None
        return {
            "name":          self._name,
            "mem_keys":      len(self._mem_cache),
            "tier1_hits":    self._tier1_hits,
            "tier2_hits":    self._tier2_hits,
            "tier3_fetches": self._tier3_fetches,
            "tier3_errors":  self._tier3_errors,
            "bypass_reads":  self._bypass_reads,
            "hit_rate":      round(hit_rate, 3) if hit_rate is not None else None,
        }

    # ── Internal helpers ─────────────────────────────────────────────────────────

    def _mem_get(self, key: Any) -> Any:
        """Tier 1 read with LRU promotion and freshness validation."""
        mk = self._mem_key(key)
        v = self._mem_cache.get(mk)
        if v is None:
            return None
        if self._lru:
            self._mem_cache.move_to_end(mk)  # type: ignore[attr-defined]
        if not self._mem_get_validator(v, key):
            del self._mem_cache[mk]
            self._on_mem_evict(mk, v)
            return None
        return v

    def _mem_set(self, key: Any, value: Any) -> None:
        """Tier 1 write with LRU eviction when capacity is exceeded."""
        mk = self._mem_key(key)
        self._mem_cache[mk] = value
        if self._lru:
            self._mem_cache.move_to_end(mk)  # type: ignore[attr-defined]
            while len(self._mem_cache) > self._max_keys:
                ek, ev = self._mem_cache.popitem(last=False)  # type: ignore[attr-defined]
                self._on_mem_evict(ek, ev)

    async def _get_lock(self, key: Any) -> asyncio.Lock:
        mk = self._mem_key(key)
        async with self._lock_map_lock:
            if mk not in self._fetch_locks:
                self._fetch_locks[mk] = asyncio.Lock()
            return self._fetch_locks[mk]
