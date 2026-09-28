"""
Tests for get_or_fetch timeout and lock-release behaviour (cache.py).
Also verifies the _CHAIN_SYM_TTL constant in options.py.
"""

import asyncio
import time

import pytest

from backend.api.cache import get_or_fetch, invalidate_all, invalidate, put as cache_put


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


def test_options_chain_instruments_uses_peek_not_get_or_fetch():
    """Guard: chain_quotes in options.py must use cache.peek('instruments'), NOT
    get_or_fetch('instruments', ...).

    chain_quotes must NEVER trigger an instruments download from inside the route
    handler.  Triggering get_or_fetch("instruments") concurrently with the startup
    sparkline-warm's _qt_broker_token_map causes a double-NFO-peak OOM (two 300-500 MB
    instrument dumps in memory simultaneously — 2026-08-11 prod OOM kill loop).

    The correct pattern: peek() returns None on cold cache → chain_quotes returns empty
    immediately → 5s frontend poll retries → background _task_instruments warms cache
    at T+120s → subsequent requests see data.
    """
    import re

    src_options = open("backend/api/routes/options.py").read()

    # Extract chain_quotes function body only (ends at next top-level async def or EOF)
    m_fn = re.search(r'async def chain_quotes\b.*?(?=\nasync def |\Z)', src_options, re.DOTALL)
    assert m_fn is not None, "chain_quotes function not found in options.py"
    chain_quotes_body = m_fn.group(0)

    # chain_quotes must use peek("instruments") — non-blocking
    assert 'peek("instruments")' in chain_quotes_body or "peek('instruments')" in chain_quotes_body, (
        "chain_quotes in options.py must use cache.peek('instruments') — a non-blocking "
        "read that returns None on cold cache. Never use get_or_fetch('instruments') from "
        "inside a route handler: it triggers a blocking download that races with the "
        "sparkline-warm token-map download and causes double-NFO-peak OOM."
    )

    # chain_quotes must NOT use get_or_fetch for instruments (strip comments first)
    body_no_comments = re.sub(r'#[^\n]*', '', chain_quotes_body)
    m_blocking = re.search(
        r'get_or_fetch\s*\(\s*["\']instruments["\'].*?\)',
        body_no_comments, re.DOTALL,
    )
    assert m_blocking is None, (
        "chain_quotes in options.py must NOT use get_or_fetch('instruments', ...) — "
        "this triggers a blocking download that races with sparkline-warm and causes OOM. "
        "Use cache.peek('instruments') instead."
    )

    # options_helpers.py: if it uses get_or_fetch('instruments'), it must not add timeout
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


# ---------------------------------------------------------------------------
# cache.put() — priming (2026-09 market/news scheduling redesign)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_put_primes_cache_get_or_fetch_never_calls_fetcher():
    """put() must make the NEXT get_or_fetch() call for that key return
    the primed value directly — this is the "warm the cache with a
    keep-warm job" contract the news/market schedulers rely on."""
    calls = {"n": 0}

    def _fetcher():
        calls["n"] += 1
        return "live-fetched"

    cache_put("k1", "primed-value", ttl_seconds=60)
    result = await get_or_fetch("k1", _fetcher, ttl_seconds=30)

    assert result == "primed-value"
    assert calls["n"] == 0, "get_or_fetch must not call the fetcher after put() primed the key"


@pytest.mark.asyncio
async def test_put_expires_after_ttl_and_falls_through_to_fetcher():
    cache_put("k2", "primed-value", ttl_seconds=0.01)
    await asyncio.sleep(0.05)

    result = await get_or_fetch("k2", lambda: "live-fetched", ttl_seconds=30)
    assert result == "live-fetched"


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


# ---------------------------------------------------------------------------
# 2026-09-27 audit fix — invalidate() racing an in-flight get_or_fetch()
# ---------------------------------------------------------------------------
# A fetch that started BEFORE an invalidate() call (e.g. a postback/fill
# firing mid-fetch) but finishes AFTER used to still write its pre-fill,
# now-stale value into the cache with a fresh TTL — silently undoing the
# invalidation for the rest of that fetch's own duration. Generation
# counters (_generation/_global_gen) close this: a fetch only caches its
# result if nothing invalidated its key while it was in flight.

@pytest.mark.asyncio
async def test_invalidate_racing_in_flight_fetch_prevents_stale_recache():
    """The core reproduction: invalidate() fires WHILE a fetch for the
    same key is still running. That fetch's result must reach its own
    caller, but must NOT be written back into the cache afterward."""
    call_log = []

    async def slow_fetch():
        call_log.append(1)
        await asyncio.sleep(0.05)
        return "pre_fill_stale_value"

    task = asyncio.create_task(get_or_fetch("race_k", slow_fetch, ttl_seconds=30))
    await asyncio.sleep(0.01)  # let it start and acquire the per-key lock

    # Simulate a fill/postback invalidating this key mid-fetch.
    invalidate("race_k")

    result = await task
    assert result == "pre_fill_stale_value"  # in-flight caller still gets its value

    # The cache must NOT have been re-populated with the stale value.
    from backend.api.cache import peek
    assert peek("race_k") is None, (
        "a fetch racing invalidate() must not re-cache its stale result"
    )

    # A subsequent call must genuinely re-fetch.
    calls_before = len(call_log)
    result2 = await get_or_fetch("race_k", slow_fetch, ttl_seconds=30)
    assert len(call_log) == calls_before + 1


@pytest.mark.asyncio
async def test_invalidate_all_racing_in_flight_fetch_prevents_stale_recache():
    """Same race, via invalidate_all() (the global generation counter)."""
    call_log = []

    async def slow_fetch():
        call_log.append(1)
        await asyncio.sleep(0.05)
        return "stale"

    task = asyncio.create_task(get_or_fetch("race_all_k", slow_fetch, ttl_seconds=30))
    await asyncio.sleep(0.01)

    invalidate_all()

    result = await task
    assert result == "stale"

    from backend.api.cache import peek
    assert peek("race_all_k") is None
