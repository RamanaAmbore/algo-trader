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
                snapshot-fallback for all code paths.
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
