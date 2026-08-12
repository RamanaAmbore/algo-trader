"""
Tests for get_or_fetch timeout and lock-release behaviour (cache.py).
Also verifies the _CHAIN_SYM_TTL constant in options.py.
"""

import asyncio
import time

import pytest

from backend.api.cache import get_or_fetch, invalidate_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_cache():
    """Wipe the in-process cache before each test."""
    invalidate_all()
    yield
    invalidate_all()


# ---------------------------------------------------------------------------
# Test 1 — timeout raises and releases lock so next caller can succeed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_raises_and_releases_lock():
    async def slow():
        await asyncio.sleep(5)
        return "slow"

    with pytest.raises(asyncio.TimeoutError):
        await get_or_fetch("t1", slow, ttl_seconds=10, timeout_seconds=1)

    # Lock must have been released — a fast fetcher should succeed immediately.
    result = await get_or_fetch("t1", lambda: "fast", ttl_seconds=10, timeout_seconds=5)
    assert result == "fast"


# ---------------------------------------------------------------------------
# Test 2 — succeeds within timeout; second call is a cache hit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_succeeds_within_timeout_and_caches():
    async def fast():
        return "ok"

    result = await get_or_fetch("t2", fast, ttl_seconds=10, timeout_seconds=5)
    assert result == "ok"

    # Cache hit — a different fetcher is ignored.
    async def should_not_run():
        raise AssertionError("fetcher called on cache hit")

    cached = await get_or_fetch("t2", should_not_run, ttl_seconds=10, timeout_seconds=5)
    assert cached == "ok"


# ---------------------------------------------------------------------------
# Test 3 — sync fetcher timeout (offloaded via asyncio.to_thread)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_fetcher_timeout():
    def slow_sync():
        time.sleep(5)
        return "done"

    with pytest.raises(asyncio.TimeoutError):
        await get_or_fetch("t3", slow_sync, ttl_seconds=10, timeout_seconds=1)


# ---------------------------------------------------------------------------
# Test 4 — _CHAIN_SYM_TTL is 30 seconds (options.py constant check)
# ---------------------------------------------------------------------------

def test_chain_sym_ttl_is_30():
    from backend.api.routes.options import _CHAIN_SYM_TTL
    assert _CHAIN_SYM_TTL == 30.0


# ---------------------------------------------------------------------------
# Test 5 — options chain instruments fetch must NOT use timeout_seconds (OOM guard)
# ---------------------------------------------------------------------------

def test_task_instruments_has_120s_startup_delay():
    """Guard: _task_instruments must sleep 120s before first download.

    The sparkline startup warm (fire-and-forget at T+0) downloads the 6-exchange
    token map; _task_instruments must NOT overlap that download.  A 120s delay
    ensures sparkline warm completes and releases its instrument RAM before
    _fetch_instruments starts its own 5-exchange download.  Removing the delay
    causes a concurrent double-NFO-peak OOM (seen 2026-07-25 on expiry day).
    """
    import re
    src = open("backend/api/background.py").read()
    m = re.search(r'async def _task_instruments\b.*?(?=\nasync def |\Z)', src, re.DOTALL)
    assert m is not None, "_task_instruments not found in background.py"
    body = m.group(0)
    assert 'asyncio.sleep(120)' in body, (
        "_task_instruments must sleep 120s before first download — "
        "ensures sparkline warm completes and releases token-map RAM before "
        "_fetch_instruments starts its 5-exchange download. "
        "Removing the delay causes double-NFO-peak OOM (2026-07-25 incident)."
    )


def test_options_chain_instruments_no_timeout():
    """Guard: instruments get_or_fetch in options.py must not use timeout_seconds.

    Adding timeout_seconds=N to the instruments fetch causes zombie threads: when the
    download takes > N seconds, asyncio.wait_for releases the lock but the underlying
    asyncio.to_thread worker keeps running and holds GB of instrument data.  The next
    caller acquires the freed lock and starts a second download — concurrent downloads
    accumulate and OOM the process.  Root cause of the 2026-08-11 prod OOM kill loop.
    """
    import re
    src = open("backend/api/routes/options.py").read()
    # Find the get_or_fetch("instruments", ...) call
    m = re.search(
        r'get_or_fetch\s*\(\s*["\']instruments["\'].*?\)',
        src, re.DOTALL,
    )
    assert m is not None, "get_or_fetch('instruments', ...) not found in options.py"
    call_text = m.group(0)
    assert "timeout_seconds" not in call_text, (
        "instruments get_or_fetch in options.py must not have timeout_seconds — "
        "a timeout releases the lock while the thread keeps downloading, causing zombie "
        "threads that accumulate GB of instrument data and OOM prod (see 2026-08-11 fix). "
        "Use coalescing (no timeout) so concurrent callers wait for the same download."
    )


def test_sparkline_startup_warm_is_fire_and_forget():
    """Guard: sparkline startup warm must use asyncio.create_task (fire-and-forget).

    The startup warm downloads the 6-exchange token map and warms sparklines for
    ~300 symbols.  It must NOT block on_startup (which would delay port 8000 from
    binding for 50-90s).  asyncio.create_task schedules it as a background
    coroutine so on_startup continues immediately while the warm runs concurrently.
    Also primes the token-map cache so _task_warm_backfill (T+60s) reuses it
    instead of triggering its own instruments download.
    """
    import re
    src = open("backend/api/background.py").read()
    m = re.search(r'async def _task_sparkline_warm\b.*?(?=\nasync def |\Z)', src, re.DOTALL)
    assert m is not None, "_task_sparkline_warm not found in background.py"
    body = m.group(0)
    assert 'asyncio.create_task(_do_warm_with_retry("startup"))' in body, (
        "_task_sparkline_warm must fire the startup warm via asyncio.create_task "
        "(fire-and-forget) — not awaited and not skipped. "
        "Skipping the startup warm leaves the token map uncached; _task_warm_backfill "
        "then triggers its own 6-exchange instruments download at T+60s, causing the "
        "same 6GB OOM that removing the startup warm was meant to prevent."
    )
