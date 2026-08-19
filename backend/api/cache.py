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
        entry = _store.get(key)
        if entry and entry[0] > now:
            return entry[1]

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

        _store[key] = (now + ttl_seconds, value)
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


def invalidate(key: str) -> None:
    """Remove a key from the cache (called by ARQ worker after publish)."""
    _store.pop(key, None)


def invalidate_all() -> None:
    """Clear the entire cache."""
    _store.clear()
