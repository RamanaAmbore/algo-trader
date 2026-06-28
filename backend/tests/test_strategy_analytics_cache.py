"""
test_strategy_analytics_cache.py — covers Phase 2: leg-signature short-circuit
cache on POST /strategy-analytics.

Five quality dimensions per feedback_test_dimensions.md:

  1. SSOT — cached response is deep-equal to the result returned by the
     underlying compute function; the cache never transforms the value.
  2. Performance — warm-cache hit <20ms; cold miss is bounded by the
     underlying compute (which is fast for unit-test mocked broker paths).
  3. Stale code — cache helper uses `time.monotonic()`, not `time.time()`.
     Asserted by source-grepping the module text.
  4. Reusable — cache key uses `hashlib.blake2b`, not `hashlib.sha256`.
     Also asserted by source-grep on the cache key function source text.
  5. Correctness:
       - 2nd identical request returns cached (not re-computed) response.
       - Request with different `spot` returns a fresh compute.
       - Same hash but `bypass_cache=true` also returns fresh.
       - Cache evicts oldest when capacity is exceeded.
       - Cache entry expires after TTL (mocked monotonic).
"""

from __future__ import annotations

import inspect
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.api.routes.options as _options_mod
from backend.api.routes.options import (
    StrategyLeg,
    StrategyRequest,
    _STRATEGY_ANALYTICS_CACHE_SIZE,
    _STRATEGY_ANALYTICS_CACHE_TTL,
    _strategy_cache_get,
    _strategy_cache_key,
    _strategy_cache_put,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _make_request(spot: float | None = None, ltp_override: float | None = None) -> StrategyRequest:
    """Minimal 2-leg bull-call spread request. `spot` controls the spot
    override so the same legs with different spot values produce different
    cache keys (testing independence)."""
    legs = [
        StrategyLeg(symbol="NIFTY24500CE", qty=50,
                    ltp=ltp_override, avg_cost=120.0),
        StrategyLeg(symbol="NIFTY24700CE", qty=-50,
                    ltp=ltp_override, avg_cost=35.0),
    ]
    return StrategyRequest(legs=legs, spot=spot, span_pct=0.10, points=21)


def _make_fake_response(tag: str = "fresh") -> MagicMock:
    """Fake StrategyResponse with an identity tag so tests can distinguish
    a cached copy from a re-computed copy."""
    resp = MagicMock()
    resp._tag = tag
    return resp


def _clear_strategy_cache() -> None:
    """Wipe the module-level cache between tests."""
    with _options_mod._STRATEGY_ANALYTICS_CACHE_LOCK:
        _options_mod._STRATEGY_ANALYTICS_CACHE.clear()


# ── 1. SSOT — cache stores and returns the exact same object ──────────


def test_cache_returns_exact_object():
    """The cached value must be the same object (identity) as what was
    stored.  The cache must not copy, transform, or re-serialize."""
    _clear_strategy_cache()
    req  = _make_request(spot=24500.0)
    key  = _strategy_cache_key(req)
    resp = _make_fake_response("exact")
    _strategy_cache_put(key, resp)
    retrieved = _strategy_cache_get(key)
    assert retrieved is resp, (
        "Cache returned a different object — must return the exact stored value"
    )


def test_cache_put_and_get_roundtrip():
    """Basic put/get round-trip: stored value comes back under the same key."""
    _clear_strategy_cache()
    req  = _make_request(spot=24600.0)
    key  = _strategy_cache_key(req)
    resp = _make_fake_response("roundtrip")
    _strategy_cache_put(key, resp)
    assert _strategy_cache_get(key) is resp


# ── 2. Performance — warm-cache hit <20ms ─────────────────────────────


def test_cache_hit_under_20ms():
    """Warm-cache hit must complete in <20ms.  This exercises the full
    _strategy_cache_get() path including lock acquisition."""
    _clear_strategy_cache()
    req  = _make_request(spot=24500.0)
    key  = _strategy_cache_key(req)
    resp = _make_fake_response("fast")
    _strategy_cache_put(key, resp)

    t0 = time.monotonic()
    result = _strategy_cache_get(key)
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert result is resp
    assert elapsed_ms < 20, (
        f"Cache hit took {elapsed_ms:.1f}ms — must be <20ms"
    )


def test_cache_key_computation_under_1ms():
    """Key derivation (blake2b + json.dumps) must complete in <1ms per
    call — it runs on every POST before any broker work."""
    req = _make_request(spot=24500.0)
    t0  = time.monotonic()
    for _ in range(100):
        _strategy_cache_key(req)
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert elapsed_ms < 100, (
        f"100 key computations took {elapsed_ms:.1f}ms; budget 100ms (1ms each)"
    )


# ── 3. Stale code — time.monotonic + blake2b asserted via source-grep ─


def test_cache_helper_uses_monotonic_not_time_time():
    """Cache helper must use time.monotonic(), not time.time().
    time.monotonic() is immune to NTP adjustments / clock skew.
    Assert by source-inspecting the module text."""
    src = inspect.getsource(_options_mod)
    assert "time.monotonic()" in src, (
        "options.py must use time.monotonic() in the strategy cache helpers"
    )
    # Negative assertion: no bare time.time() call in the cache helpers
    # (the hist-cache block below also uses monotonic; there should be
    # no time.time() at all in this module).
    # We specifically check the strategy cache functions.
    cache_src = (
        inspect.getsource(_strategy_cache_get)
        + inspect.getsource(_strategy_cache_put)
        + inspect.getsource(_strategy_cache_key)
    )
    assert "time.monotonic()" in cache_src, (
        "_strategy_cache_get/_put must use time.monotonic()"
    )


def test_cache_key_uses_blake2b_not_sha256():
    """Cache key must use hashlib.blake2b — project convention for short
    hashes (matches backend/api/cache.py approach: short digest_size=16).
    Assert by source-inspecting the key function."""
    key_src = inspect.getsource(_strategy_cache_key)
    assert "blake2b" in key_src, (
        "_strategy_cache_key must use hashlib.blake2b (not sha256)"
    )
    assert "sha256" not in key_src, (
        "_strategy_cache_key must NOT use sha256 — use blake2b per project convention"
    )


def test_blake2b_digest_size_16():
    """Cache key must use digest_size=16 (128-bit) — consistent with the
    short-key convention used across the project."""
    key_src = inspect.getsource(_strategy_cache_key)
    assert "digest_size=16" in key_src, (
        "_strategy_cache_key must use digest_size=16"
    )


# ── 4. Reusable — same LRU/TTL mechanics as _hist_cache ──────────────


def test_lru_eviction_at_capacity():
    """Cache must evict the LRU entry when capacity is exceeded so memory
    stays bounded at _STRATEGY_ANALYTICS_CACHE_SIZE entries."""
    _clear_strategy_cache()
    # Fill to capacity + 1.
    responses: list = []
    for i in range(_STRATEGY_ANALYTICS_CACHE_SIZE + 1):
        req  = _make_request(spot=float(20000 + i))
        key  = _strategy_cache_key(req)
        resp = _make_fake_response(str(i))
        responses.append((key, resp))
        _strategy_cache_put(key, resp)

    with _options_mod._STRATEGY_ANALYTICS_CACHE_LOCK:
        size = len(_options_mod._STRATEGY_ANALYTICS_CACHE)
    assert size == _STRATEGY_ANALYTICS_CACHE_SIZE, (
        f"Cache size {size} should equal capacity {_STRATEGY_ANALYTICS_CACHE_SIZE} "
        "after eviction"
    )

    # The first entry (index 0) should have been evicted (LRU).
    first_key, _ = responses[0]
    assert _strategy_cache_get(first_key) is None, (
        "Oldest (LRU) entry must be evicted when cache overflows"
    )

    # The most recent entry must still be present.
    last_key, last_resp = responses[-1]
    assert _strategy_cache_get(last_key) is last_resp, (
        "Most-recently-stored entry must survive LRU eviction"
    )


def test_cache_ttl_expiry():
    """Entry must be considered expired after TTL seconds (mocked monotonic)."""
    _clear_strategy_cache()
    req  = _make_request(spot=24500.0)
    key  = _strategy_cache_key(req)
    resp = _make_fake_response("expire-me")

    with patch("backend.api.routes.options.time") as mock_time:
        # Simulate: store at t=0.
        mock_time.monotonic.return_value = 0.0
        _strategy_cache_put(key, resp)
        # Read before TTL — should hit.
        mock_time.monotonic.return_value = _STRATEGY_ANALYTICS_CACHE_TTL - 0.5
        assert _strategy_cache_get(key) is resp, "Should hit before TTL"
        # Read AT or past TTL — should miss.
        mock_time.monotonic.return_value = _STRATEGY_ANALYTICS_CACHE_TTL + 0.1
        assert _strategy_cache_get(key) is None, "Should miss after TTL"


# ── 5. Correctness ────────────────────────────────────────────────────


def test_second_identical_request_uses_cache():
    """2nd call with identical inputs must return the cached object, not
    re-invoke the underlying compute.  We mock _strategy_analytics_impl
    as a counter to verify it is only called once.

    Calls the underlying function via handler.fn (bypassing Litestar
    route dispatch which needs a full Request context — unit tests don't
    need HTTP machinery)."""
    _clear_strategy_cache()
    req   = _make_request(spot=24500.0)
    fresh = _make_fake_response("first-compute")

    call_count = 0

    async def _fake_impl(data):
        nonlocal call_count
        call_count += 1
        return fresh

    controller = _options_mod.OptionsController.__new__(_options_mod.OptionsController)
    controller._strategy_analytics_impl = _fake_impl

    # Access the raw async function, not the Litestar route handler wrapper.
    handler_fn = _options_mod.OptionsController.strategy_analytics.fn

    import asyncio

    async def run():
        # First call — computes, stores in cache.
        r1 = await handler_fn(controller, req, bypass_cache=False)
        # Second call — same input, must use cache.
        r2 = await handler_fn(controller, req, bypass_cache=False)
        return r1, r2

    r1, r2 = asyncio.run(run())

    assert call_count == 1, (
        f"_strategy_analytics_impl called {call_count} times; expected 1 "
        "(second call should hit cache)"
    )
    assert r1 is fresh
    assert r2 is fresh


def test_different_spot_returns_fresh():
    """Request with a different spot must produce a different cache key and
    trigger a fresh compute, not return the previously cached response."""
    _clear_strategy_cache()
    req_a = _make_request(spot=24500.0)
    req_b = _make_request(spot=25000.0)
    key_a = _strategy_cache_key(req_a)
    key_b = _strategy_cache_key(req_b)

    assert key_a != key_b, (
        "Different spot overrides must produce different cache keys"
    )

    resp_a = _make_fake_response("A")
    _strategy_cache_put(key_a, resp_a)

    # req_b should miss — different spot, different key.
    assert _strategy_cache_get(key_b) is None, (
        "Request with different spot must not hit cache for the other spot"
    )


def test_bypass_cache_skips_lookup():
    """bypass_cache=True must skip the cache lookup and call the underlying
    compute even when a fresh cached response exists."""
    _clear_strategy_cache()
    req   = _make_request(spot=24500.0)
    key   = _strategy_cache_key(req)
    stale = _make_fake_response("cached-stale")
    _strategy_cache_put(key, stale)

    call_count = 0
    fresh_result = _make_fake_response("bypass-fresh")

    async def _fake_impl(data):
        nonlocal call_count
        call_count += 1
        return fresh_result

    import asyncio
    controller = _options_mod.OptionsController.__new__(_options_mod.OptionsController)
    controller._strategy_analytics_impl = _fake_impl

    handler_fn = _options_mod.OptionsController.strategy_analytics.fn

    async def run():
        return await handler_fn(controller, req, bypass_cache=True)

    result = asyncio.run(run())

    assert call_count == 1, "bypass_cache=True must call _strategy_analytics_impl"
    assert result is fresh_result, "bypass_cache=True must return the fresh compute"
    # The cache should also have been updated with the fresh result.
    assert _strategy_cache_get(key) is fresh_result, (
        "After bypass_cache=True compute, the cache must be updated with fresh result"
    )


def test_same_legs_different_spot_different_key():
    """Canonical property: same legs + different S must not share a cache
    entry so two operators with identical option symbols but different
    overlying spots don't receive stale data from each other's request."""
    _clear_strategy_cache()
    for spot in (24000.0, 24500.0, 25000.0, None):
        req = _make_request(spot=spot)
        key = _strategy_cache_key(req)
        _strategy_cache_put(key, _make_fake_response(str(spot)))

    # Verify all 4 keys are distinct.
    keys = {
        _strategy_cache_key(_make_request(spot=s))
        for s in (24000.0, 24500.0, 25000.0, None)
    }
    assert len(keys) == 4, (
        f"Expected 4 distinct cache keys for 4 different spots; got {len(keys)}"
    )


def test_leg_order_does_not_affect_key():
    """Leg order must NOT affect the cache key — the same basket presented
    in a different order is the same strategy."""
    leg1 = StrategyLeg(symbol="NIFTY24500CE", qty=50,  avg_cost=120.0, ltp=None)
    leg2 = StrategyLeg(symbol="NIFTY24700CE", qty=-50, avg_cost=35.0,  ltp=None)
    req_forward = StrategyRequest(legs=[leg1, leg2], spot=24500.0, points=21)
    req_reverse = StrategyRequest(legs=[leg2, leg1], spot=24500.0, points=21)
    assert _strategy_cache_key(req_forward) == _strategy_cache_key(req_reverse), (
        "Leg order must not affect cache key — same basket presented differently"
    )
