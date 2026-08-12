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
# Test 5 — bg-instruments must not be auto-started in on_startup (OOM guard)
# ---------------------------------------------------------------------------

def test_bg_instruments_not_in_on_startup():
    """Guard: bg-instruments must not be auto-started at startup (causes OOM on prod)."""
    import re
    src = open("backend/api/background.py").read()
    # Extract on_startup function body
    m = re.search(r'async def on_startup\(.*?\).*?(?=\nasync def |\Z)', src, re.DOTALL)
    assert m is not None, "on_startup function not found in background.py"
    body = m.group(0)
    assert "bg-instruments" not in body, (
        "bg-instruments task must not be created in on_startup — "
        "it causes concurrent NFO download OOM on prod (see fix 2026-08-12). "
        "Use on-demand loading via get_or_fetch instead."
    )


# ---------------------------------------------------------------------------
# Test 6 — sparkline warm must NOT fire immediately at startup (OOM guard)
# ---------------------------------------------------------------------------

def test_sparkline_warm_has_startup_delay():
    """Guard: sparkline startup warm must be delayed, not immediate (OOM risk on prod)."""
    import re
    src = open("backend/api/background.py").read()
    # Find the _task_sparkline_warm function body
    m = re.search(r'async def _task_sparkline_warm\b.*?(?=\nasync def |\Z)', src, re.DOTALL)
    assert m is not None, "_task_sparkline_warm not found in background.py"
    body = m.group(0)
    # The fire-immediately pattern must be gone: no bare create_task(_do_warm_with_retry("startup"))
    # Instead, there must be an asyncio.sleep before the startup warm.
    assert '_spark_delayed_startup' in body or 'asyncio.sleep(600)' in body, (
        "sparkline startup warm must include a delay (asyncio.sleep) — "
        "immediate startup warm causes concurrent NFO download OOM with other tasks "
        "(see fix 2026-08-12). Remove this guard only if instruments store is warm at boot."
    )
