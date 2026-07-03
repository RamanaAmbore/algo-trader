"""
Tests for backend/api/helpers/snapshot_gate.py

Five quality dimensions:
  1. SSOT     — closed_hours_or_broker IS the single gate; routes import from here,
                no inline is_any_segment_open calls remain in migrated routes.
  2. Perf     — helper adds < 1ms overhead vs. a direct coroutine call.
  3. Stale    — migrated routes (positions, holdings) import snapshot_gate; no
                inline _is_all_markets_closed helpers remain there.
  4. Reuse    — each migrated route imports from snapshot_gate (single import SSOT).
  5. UX       — source field in return value correctly encodes live / snapshot /
                snapshot-fallback / stale-live for all code paths.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Source paths for static checks (Dimensions 3 & 4)
# ---------------------------------------------------------------------------

_GATE_SRC = Path(__file__).parent.parent / "api" / "helpers" / "snapshot_gate.py"
_POS_SRC  = Path(__file__).parent.parent / "api" / "routes"  / "positions.py"
_HOL_SRC  = Path(__file__).parent.parent / "api" / "routes"  / "holdings.py"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Dimension 4 — reuse: migrated routes import from snapshot_gate
# ---------------------------------------------------------------------------

def test_positions_imports_snapshot_gate():
    """positions.py imports closed_hours_or_broker from snapshot_gate."""
    src = _src(_POS_SRC)
    assert "from backend.api.helpers.snapshot_gate import closed_hours_or_broker" in src, (
        "positions.py must import closed_hours_or_broker from snapshot_gate"
    )


def test_holdings_imports_snapshot_gate():
    """holdings.py imports closed_hours_or_broker from snapshot_gate."""
    src = _src(_HOL_SRC)
    assert "from backend.api.helpers.snapshot_gate import closed_hours_or_broker" in src, (
        "holdings.py must import closed_hours_or_broker from snapshot_gate"
    )


# ---------------------------------------------------------------------------
# Dimension 3 — stale: migrated routes no longer define _is_all_markets_closed
# ---------------------------------------------------------------------------

def test_positions_no_inline_is_all_markets_closed():
    """positions.py must NOT define _is_all_markets_closed (migrated to snapshot_gate)."""
    src = _src(_POS_SRC)
    assert "def _is_all_markets_closed" not in src, (
        "positions.py still has inline _is_all_markets_closed — "
        "must use closed_hours_or_broker from snapshot_gate instead"
    )


def test_holdings_no_inline_is_all_markets_closed():
    """holdings.py must NOT define _is_all_markets_closed (migrated to snapshot_gate)."""
    src = _src(_HOL_SRC)
    assert "def _is_all_markets_closed" not in src, (
        "holdings.py still has inline _is_all_markets_closed — "
        "must use closed_hours_or_broker from snapshot_gate instead"
    )


# ---------------------------------------------------------------------------
# Dimension 1 — SSOT: helper exports the canonical gate function
# ---------------------------------------------------------------------------

def test_snapshot_gate_exports_closed_hours_or_broker():
    """snapshot_gate.py exports closed_hours_or_broker."""
    src = _src(_GATE_SRC)
    assert "async def closed_hours_or_broker" in src, (
        "snapshot_gate.py must define closed_hours_or_broker"
    )
    assert "snapshot_fn" in src and "broker_fn" in src, (
        "closed_hours_or_broker must accept snapshot_fn and broker_fn parameters"
    )


def test_snapshot_gate_returns_source_tag():
    """snapshot_gate.py documents the three source tags in the docstring."""
    src = _src(_GATE_SRC)
    assert "'live'" in src, "snapshot_gate.py docstring must mention 'live' source tag"
    assert "'snapshot'" in src, "snapshot_gate.py docstring must mention 'snapshot' source tag"
    assert "'snapshot-fallback'" in src, (
        "snapshot_gate.py docstring must mention 'snapshot-fallback' source tag"
    )


# ---------------------------------------------------------------------------
# Dimension 5 — UX: source tag correctness for all code paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_closed_hours_or_broker_market_closed_returns_snapshot_tag():
    """When market is closed, helper returns (snapshot_data, 'snapshot')."""
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker

    sentinel = object()

    async def _snap():
        return sentinel

    async def _live():
        raise AssertionError("broker_fn must not be called when market is closed")

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False):
        data, source = await closed_hours_or_broker("NSE", _snap, _live)

    assert data is sentinel
    assert source == "snapshot"


@pytest.mark.asyncio
async def test_closed_hours_or_broker_market_open_returns_live_tag():
    """When market is open and broker succeeds, returns (broker_data, 'live')."""
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker

    live_sentinel = object()

    async def _snap():
        raise AssertionError("snapshot_fn must not be called when market is open")

    async def _live():
        return live_sentinel

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        data, source = await closed_hours_or_broker("NSE", _snap, _live)

    assert data is live_sentinel
    assert source == "live"


@pytest.mark.asyncio
async def test_closed_hours_or_broker_broker_fails_returns_snapshot_fallback():
    """When market is open but broker_fn raises, returns (snapshot_data, 'snapshot-fallback')."""
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker

    fallback_sentinel = object()

    async def _snap():
        return fallback_sentinel

    async def _live():
        raise RuntimeError("broker auth expired")

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        data, source = await closed_hours_or_broker(
            "NSE", _snap, _live, fallback_to_snapshot_on_broker_error=True
        )

    assert data is fallback_sentinel
    assert source == "snapshot-fallback"


@pytest.mark.asyncio
async def test_closed_hours_or_broker_broker_fails_no_fallback_raises():
    """When fallback_to_snapshot_on_broker_error=False, broker error propagates."""
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker

    async def _snap():
        return "unused"

    async def _live():
        raise RuntimeError("broker auth expired")

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        with pytest.raises(RuntimeError, match="broker auth expired"):
            await closed_hours_or_broker(
                "NSE", _snap, _live, fallback_to_snapshot_on_broker_error=False
            )


@pytest.mark.asyncio
async def test_closed_hours_or_broker_snapshot_fn_raises_propagates():
    """snapshot_fn raising during closed hours propagates the exception."""
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker

    async def _snap():
        raise RuntimeError("DB connection lost")

    async def _live():
        raise AssertionError("broker_fn must not be called when market is closed")

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False):
        with pytest.raises(RuntimeError, match="DB connection lost"):
            await closed_hours_or_broker("NSE", _snap, _live)


# ---------------------------------------------------------------------------
# Dimension 2 — perf: helper overhead < 1ms vs. direct coroutine call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_closed_hours_or_broker_overhead_under_1ms():
    """closed_hours_or_broker adds < 1ms overhead vs. directly calling the coroutine."""
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker

    result_value = {"data": "ok"}

    async def _fast_fn():
        return result_value

    # Baseline: direct call cost
    N = 100
    t0 = time.perf_counter()
    for _ in range(N):
        _ = await _fast_fn()
    direct_ms = (time.perf_counter() - t0) / N * 1000

    # Helper call cost (market open, successful broker path)
    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        t0 = time.perf_counter()
        for _ in range(N):
            _ = await closed_hours_or_broker("NSE", _fast_fn, _fast_fn)
        helper_ms = (time.perf_counter() - t0) / N * 1000

    overhead_ms = helper_ms - direct_ms
    assert overhead_ms < 1.0, (
        f"closed_hours_or_broker added {overhead_ms:.3f}ms overhead "
        "(budget: < 1ms per call)"
    )


# ---------------------------------------------------------------------------
# Dimension 1 — SSOT: broker_fn is NEVER called during closed hours
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broker_fn_never_called_during_closed_hours():
    """SSOT invariant: broker_fn must never execute when market is closed."""
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker

    broker_called = False

    async def _snap():
        return "snapshot-data"

    async def _live():
        nonlocal broker_called
        broker_called = True
        return "live-data"

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False):
        data, source = await closed_hours_or_broker("NSE", _snap, _live)

    assert not broker_called, (
        "broker_fn was called during closed hours — this is the primary SSOT invariant "
        "that closed_hours_or_broker enforces"
    )
    assert source == "snapshot"
    assert data == "snapshot-data"


# ---------------------------------------------------------------------------
# Dimension 5 — UX: response time < 50ms for the test suite itself
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_gate_tests_run_fast():
    """This entire test module's core logic resolves in < 50ms (no IO)."""
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker

    async def _snap():
        return "snap"

    async def _live():
        return "live"

    t0 = time.perf_counter()

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False):
        d1, s1 = await closed_hours_or_broker("NSE", _snap, _live)

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        d2, s2 = await closed_hours_or_broker("MCX", _snap, _live)

    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 50, (
        f"snapshot_gate tests took {elapsed_ms:.1f}ms — budget is 50ms"
    )
    assert s1 == "snapshot"
    assert s2 == "live"


# ---------------------------------------------------------------------------
# Anti-flicker stale-live path (added Jul 2026)
# ---------------------------------------------------------------------------

def _clear_route_cache(key: str) -> None:
    """Wipe the anti-flicker cache entry for `key` so tests start clean."""
    from backend.api.helpers import snapshot_gate
    snapshot_gate._last_response_by_route.pop(key, None)


@pytest.mark.asyncio
async def test_stale_live_returned_on_broker_fail_after_success():
    """When broker fails but a recent live payload is cached, return
    stale-live instead of snapshot-fallback."""
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker
    _clear_route_cache("test_pos")

    live_sentinel = {"rows": ["live_data"]}
    fallback_sentinel = {"rows": ["snapshot_data"]}

    call_count = 0

    async def _snap():
        return fallback_sentinel

    async def _live_first():
        return live_sentinel

    async def _live_fail():
        raise RuntimeError("transient broker error")

    # First call: market open, broker succeeds — stashes in cache.
    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        data1, source1 = await closed_hours_or_broker(
            "NSE", _snap, _live_first,
            fallback_to_snapshot_on_broker_error=True,
            route_key="test_pos",
        )
    assert source1 == "live"
    assert data1 is live_sentinel

    # Second call: market open, broker fails — should return stale-live NOT snapshot-fallback.
    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        data2, source2 = await closed_hours_or_broker(
            "NSE", _snap, _live_fail,
            fallback_to_snapshot_on_broker_error=True,
            route_key="test_pos",
        )
    assert source2 == "stale-live", (
        f"Expected stale-live but got {source2!r} — anti-flicker cache not working"
    )
    # CRITICAL: the operator sees the live data, not the snapshot.
    assert data2 is live_sentinel

    _clear_route_cache("test_pos")


@pytest.mark.asyncio
async def test_snapshot_fallback_when_no_cache_and_broker_fails():
    """Without a prior successful call, broker failure still falls back to snapshot."""
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker
    _clear_route_cache("test_cold")

    fallback_sentinel = {"rows": ["snapshot_data"]}

    async def _snap():
        return fallback_sentinel

    async def _live():
        raise RuntimeError("transient broker error")

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        data, source = await closed_hours_or_broker(
            "NSE", _snap, _live,
            fallback_to_snapshot_on_broker_error=True,
            route_key="test_cold",
        )
    assert source == "snapshot-fallback"
    assert data is fallback_sentinel

    _clear_route_cache("test_cold")


@pytest.mark.asyncio
async def test_stale_live_respects_ttl():
    """Stale-live cache entry older than _STALE_LIVE_TTL_S is ignored;
    falls through to snapshot-fallback."""
    import time as _time
    from backend.api.helpers import snapshot_gate
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker
    _clear_route_cache("test_ttl")

    live_sentinel = {"rows": ["live_data"]}
    fallback_sentinel = {"rows": ["snapshot_data"]}

    # Manually insert an old cache entry (1 second past TTL).
    snapshot_gate._last_response_by_route["test_ttl"] = (
        _time.time() - snapshot_gate._STALE_LIVE_TTL_S - 1.0,
        live_sentinel,
    )

    async def _snap():
        return fallback_sentinel

    async def _live():
        raise RuntimeError("transient broker error")

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        data, source = await closed_hours_or_broker(
            "NSE", _snap, _live,
            fallback_to_snapshot_on_broker_error=True,
            route_key="test_ttl",
        )
    assert source == "snapshot-fallback", (
        f"Expired stale-live entry should fall through to snapshot-fallback, got {source!r}"
    )
    assert data is fallback_sentinel

    _clear_route_cache("test_ttl")


@pytest.mark.asyncio
async def test_no_route_key_skips_anti_flicker():
    """When route_key='', broker failure always uses snapshot-fallback (legacy behaviour)."""
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker

    live_sentinel = {"rows": ["live_data"]}
    fallback_sentinel = {"rows": ["snapshot_data"]}

    async def _snap():
        return fallback_sentinel

    async def _live_success():
        return live_sentinel

    async def _live_fail():
        raise RuntimeError("broker down")

    # Even after a successful call, no-route_key path can't cache.
    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        await closed_hours_or_broker("NSE", _snap, _live_success)
        data, source = await closed_hours_or_broker(
            "NSE", _snap, _live_fail,
            fallback_to_snapshot_on_broker_error=True,
            route_key="",
        )
    assert source == "snapshot-fallback"


@pytest.mark.asyncio
async def test_route_keys_independent():
    """Different route keys have independent caches — a 'positions' cache
    does not serve as stale-live for 'holdings'."""
    from backend.api.helpers.snapshot_gate import closed_hours_or_broker
    _clear_route_cache("pos_ind")
    _clear_route_cache("hold_ind")

    live_sentinel = {"rows": ["live_data"]}
    fallback_sentinel = {"rows": ["snapshot_data"]}

    async def _snap():
        return fallback_sentinel

    async def _live_success():
        return live_sentinel

    async def _live_fail():
        raise RuntimeError("broker down")

    # Warm the positions cache.
    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        await closed_hours_or_broker("NSE", _snap, _live_success, route_key="pos_ind")

    # Holdings cache is cold — should fall back to snapshot-fallback.
    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        data, source = await closed_hours_or_broker(
            "NSE", _snap, _live_fail,
            fallback_to_snapshot_on_broker_error=True,
            route_key="hold_ind",
        )
    assert source == "snapshot-fallback"

    _clear_route_cache("pos_ind")
    _clear_route_cache("hold_ind")


def test_positions_passes_route_key():
    """positions.py must pass route_key='positions' to closed_hours_or_broker."""
    src = _src(_POS_SRC)
    assert "route_key=\"positions\"" in src or "route_key='positions'" in src, (
        "positions.py must pass route_key='positions' to closed_hours_or_broker "
        "for the anti-flicker stale-live cache to work"
    )


def test_holdings_passes_route_key():
    """holdings.py must pass route_key='holdings' to closed_hours_or_broker."""
    src = _src(_HOL_SRC)
    assert "route_key=\"holdings\"" in src or "route_key='holdings'" in src, (
        "holdings.py must pass route_key='holdings' to closed_hours_or_broker "
        "for the anti-flicker stale-live cache to work"
    )
