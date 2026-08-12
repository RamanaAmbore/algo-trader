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

def test_bg_instruments_not_in_on_startup():
    """Guard: bg-instruments must NOT be started in on_startup.

    Starting _task_instruments at startup causes a double-NFO-peak OOM:
    at T+120s, _fetch_instruments (5 exchanges) runs concurrently with
    sparkline warm_backfill (which keeps the 6-exchange token_map in RAM).
    Combined peak exceeds 7.8GB server RAM → OOM kill loop.
    Instruments load on-demand via options.py get_or_fetch (coalesced, no timeout).
    """
    import re
    src = open("backend/api/background.py").read()
    # Extract on_startup body (from def on_startup up to next top-level def)
    m = re.search(r'async def on_startup\(.*?\n(?=async def |\Z)', src, re.DOTALL)
    assert m is not None, "on_startup function not found in background.py"
    body = m.group(0)
    assert "bg-instruments" not in body, (
        "bg-instruments must NOT be in on_startup — starting _task_instruments at "
        "startup causes double-NFO-peak OOM (token_map + _fetch_instruments both "
        "downloading simultaneously at T+120s). Instruments load on-demand via "
        "options.py get_or_fetch instead. See 2026-08-11 OOM kill loop fix."
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


def test_sparkline_warm_has_startup_delay():
    """Guard: sparkline startup warm must NOT fire immediately (OOM risk on prod).

    Without a startup delay, _do_warm_with_retry fires at T=0 and downloads 6 exchanges
    (NFO = ~70k instruments). If count==0 it retries at T+60s (second full download).
    Combined RSS reaches 5-6GB before port 8000 binds → OOM kill loop.
    600s delay lets the process stabilise before sparkline warm fires.
    Root cause of 2026-08-12 prod OOM.
    """
    import re
    src = open("backend/api/background.py").read()
    m = re.search(r'async def _task_sparkline_warm\b.*?(?=\nasync def |\Z)', src, re.DOTALL)
    assert m is not None, "_task_sparkline_warm not found in background.py"
    body = m.group(0)
    assert '_spark_delayed_startup' in body or 'asyncio.sleep(600)' in body, (
        "sparkline startup warm must include a 600s delay — "
        "immediate startup warm causes concurrent NFO download OOM with other tasks "
        "(see fix 2026-08-12). Remove this guard only if instruments store is warm at boot."
    )
