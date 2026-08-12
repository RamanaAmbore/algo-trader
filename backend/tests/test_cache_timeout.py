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
    """Guard: instruments get_or_fetch in options.py and options_helpers.py
    must not use timeout_seconds.

    Adding timeout_seconds=N to the instruments fetch causes zombie threads: when the
    download takes > N seconds, asyncio.wait_for releases the lock but the underlying
    asyncio.to_thread worker keeps running and holds GB of instrument data.  The next
    caller acquires the freed lock and starts a second download — concurrent downloads
    accumulate and OOM the process.  Root cause of the 2026-08-11 prod OOM kill loop.
    """
    import re

    # Check options.py
    src_options = open("backend/api/routes/options.py").read()
    m_options = re.search(
        r'get_or_fetch\s*\(\s*["\']instruments["\'].*?\)',
        src_options, re.DOTALL,
    )
    assert m_options is not None, "get_or_fetch('instruments', ...) not found in options.py"
    call_text_options = m_options.group(0)
    assert "timeout_seconds" not in call_text_options, (
        "instruments get_or_fetch in options.py must not have timeout_seconds — "
        "a timeout releases the lock while the thread keeps downloading, causing zombie "
        "threads that accumulate GB of instrument data and OOM prod (see 2026-08-11 fix). "
        "Use coalescing (no timeout) so concurrent callers wait for the same download."
    )

    # Check options_helpers.py
    src_helpers = open("backend/api/routes/options_helpers.py").read()
    m_helpers = re.search(
        r'get_or_fetch\s*\(\s*["\']instruments["\'].*?\)',
        src_helpers, re.DOTALL,
    )
    if m_helpers is not None:
        call_text_helpers = m_helpers.group(0)
        assert "timeout_seconds" not in call_text_helpers, (
            "instruments get_or_fetch in options_helpers.py must not have timeout_seconds — "
            "a timeout releases the lock while the thread keeps downloading, causing zombie "
            "threads that accumulate GB of instrument data and OOM prod (see 2026-08-11 fix). "
            "Use coalescing (no timeout) so concurrent callers wait for the same download."
        )


def test_token_refresh_parks_under_conn_service():
    """Guard: _task_token_refresh must sleep 86400s (park) under conn_service.

    Under RAMBOQ_USE_CONN_SERVICE=1 the function has no work to do. Returning
    immediately causes _supervised to restart it in a tight loop — 23,651+
    invocations per minute logged on 2026-08-12, starving the event loop at 97%
    CPU. Port 8000 never bound; process OOM-killed at 5.56GB.

    Fix: await asyncio.sleep(86400) before return so _supervised sees a long-lived
    coroutine. Same park pattern as commit 80b9c4b6.
    """
    import re
    src = open("backend/api/background.py").read()
    m = re.search(r'async def _task_token_refresh\b.*?(?=\nasync def |\Z)', src, re.DOTALL)
    assert m is not None, "_task_token_refresh not found in background.py"
    body = m.group(0)
    assert 'await asyncio.sleep(86400)' in body, (
        "_task_token_refresh must contain `await asyncio.sleep(86400)` in the "
        "RAMBOQ_USE_CONN_SERVICE branch — returning immediately causes _supervised "
        "to restart it thousands of times per minute, starving the event loop and "
        "causing OOM kill (2026-08-12 prod incident). Park the task instead."
    )


def test_sparkline_startup_warm_is_disabled():
    """Guard: _task_sparkline_warm must NOT fire asyncio.create_task at startup.

    The T+0 startup warm calls _qt_broker_token_map which downloads all 6 sparkline
    exchanges' instruments (NSE, NFO, BSE, BFO, MCX, CDS) sequentially. With NFO
    having 300K+ rows, this peaks at 5-6GB RSS → OOM kill before port 8000 binds.

    _do_warm now guards on instruments_store Tier 1 — it returns 0 immediately if
    Tier 1 is cold, preventing the Tier 3 broker download at any call site.
    Sparklines warm lazily once a user visit populates instruments_store from DB.

    Root cause of 2026-08-12 OOM kill loop. Fix: remove the fire-and-forget
    asyncio.create_task at startup; rely on the _do_warm Tier 1 guard for all
    subsequent warm attempts (segment open, midnight boundary).
    """
    import re
    src = open("backend/api/background.py").read()
    m = re.search(r'async def _task_sparkline_warm\b.*?(?=\nasync def |\Z)', src, re.DOTALL)
    assert m is not None, "_task_sparkline_warm not found in background.py"
    body = m.group(0)
    assert 'asyncio.create_task(_do_warm_with_retry("startup"))' not in body, (
        "_task_sparkline_warm must NOT fire asyncio.create_task(_do_warm_with_retry) "
        "at startup — the T+0 6-exchange instruments download peaks at 5-6GB RSS "
        "and OOM-kills the process before port 8000 binds (2026-08-12 incident). "
        "_do_warm now guards on instruments_store Tier 1; startup warm is disabled."
    )
