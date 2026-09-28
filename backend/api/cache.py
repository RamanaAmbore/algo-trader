"""
In-process TTL cache for broker API responses.

All heavy Kite API calls (holdings, positions, margins, orders) are cached here.
Cache entries expire after `ttl_seconds`. On expiry the next request re-fetches
and repopulates the cache. The background ARQ worker also calls `invalidate()`
after each successful refresh so the API always serves fresh data immediately
after a background update — without waiting for TTL expiry.

Thread-safe via asyncio.Lock (single-process uvicorn worker).
"""

import asyncio
import time
from typing import Any

_store: dict[str, tuple[float, Any]] = {}   # key → (expires_at, value)
_locks: dict[str, asyncio.Lock]      = {}   # key → per-key lock

# 2026-09-27 audit fix — generation counters guard against a fetch that
# started BEFORE an invalidate()/invalidate_all() call landing its
# (stale) result in the cache AFTER the invalidation, silently undoing
# it. `_generation[key]` bumps on a per-key invalidate(); `_global_gen`
# bumps on invalidate_all(). A fetch snapshots both before it starts and
# only writes its result to `_store` if neither has moved since —
# otherwise the fetch it raced against already established that this
# key's cached value must not come from this (now-stale) call.
_generation: dict[str, int] = {}
_global_gen: int = 0


def _gen_snapshot(key: str) -> tuple[int, int]:
    return (_generation.get(key, 0), _global_gen)


def _lock(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


async def get_or_fetch(key: str, fetcher, ttl_seconds: int = 30,
                       timeout_seconds: int | None = None):
    """
    Return cached value for `key` if still fresh, otherwise call `fetcher()`
    (an async or sync callable), cache the result, and return it.

    Uses per-key locking so concurrent requests for the same key only trigger
    one fetch — others wait and then receive the cached result (request coalescing).

    If `timeout_seconds` is set, wraps the fetch with asyncio.wait_for. On timeout,
    asyncio.TimeoutError propagates out of the lock block — releasing the lock so the
    next request can retry immediately.
    """
    now = time.monotonic()
    entry = _store.get(key)
    if entry and entry[0] > now:
        return entry[1]

    async with _lock(key):
        # Re-check inside lock — another coroutine may have fetched while we waited
        now = time.monotonic()
        entry = _store.get(key)
        if entry and entry[0] > now:
            return entry[1]

        # Snapshot BEFORE the fetch runs — see the module-level comment
        # on _generation/_global_gen above.
        gen_before = _gen_snapshot(key)

        if asyncio.iscoroutinefunction(fetcher):
            coro = fetcher()
        else:
            # Sync fetchers (Kite SDK calls, urllib3 fetches) historically
            # ran inline on the event loop. On a cold cache hit that
            # blocked the entire ramboq process for several seconds
            # (instruments dump = ~90k rows × N exchanges; orders =
            # one HTTP round-trip per account). Offload to the default
            # threadpool so other requests in flight keep moving.
            coro = asyncio.to_thread(fetcher)

        if timeout_seconds is not None:
            value = await asyncio.wait_for(coro, timeout=timeout_seconds)
        else:
            value = await coro

        # Only cache if nothing invalidated this key while the fetch was
        # in flight — a fetch that started before an invalidate()/fill
        # but finishes after must not put the pre-fill stale value back
        # with a fresh TTL. TTL is measured from NOW (post-fetch), not
        # the `now` captured before the lock/fetch — using the earlier
        # timestamp under-counts the TTL by however long the fetch (or
        # the wait for a contended lock) actually took.
        if _gen_snapshot(key) == gen_before:
            _store[key] = (time.monotonic() + ttl_seconds, value)
        return value


def peek(key: str) -> "Any | None":
    """Return cached value for key without blocking or fetching.

    Returns None when the key is absent or expired. Use this in route handlers
    that must NOT trigger a heavy download (e.g. chain_quotes must not start
    an instruments fetch that could race with the sparkline-warm OOM window).
    """
    entry = _store.get(key)
    if entry and entry[0] > time.monotonic():
        return entry[1]
    return None


def put(key: str, value: Any, ttl_seconds: int) -> None:
    """Prime the cache with an already-fetched value directly — used by
    keep-warm background jobs so the NEXT real request hits a warm cache
    instead of re-triggering a live fetch. Deliberately distinct from
    invalidate(): invalidating after a background refresh would just
    force the very next visitor's request to miss the cache and re-do
    the live fetch anyway (fine when the live fetch is cheap, e.g.
    market's DB-first _db_or_gemini — wrong when it isn't, e.g. news's
    RSS pull, which has no DB-first branch)."""
    _store[key] = (time.monotonic() + ttl_seconds, value)


def invalidate(key: str) -> None:
    """Remove a key from the cache (called by ARQ worker after publish).

    Bumps this key's generation counter so an in-flight `get_or_fetch`
    call that started before this invalidation can't overwrite it with a
    stale result after the fact — see the module-level comment on
    `_generation`/`_global_gen`.
    """
    _generation[key] = _generation.get(key, 0) + 1
    _store.pop(key, None)


def invalidate_all() -> None:
    """Clear the entire cache. Bumps the global generation counter —
    same race-prevention rationale as `invalidate()`, applied to every key."""
    global _global_gen
    _global_gen += 1
    _store.clear()
