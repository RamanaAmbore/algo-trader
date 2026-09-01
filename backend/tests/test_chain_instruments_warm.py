"""
Tests for _task_chain_instruments._warm() fast path + fallback,
and _task_instruments._warm() chain-rebuild side-effect.

SSOT: chain expiries index is built from the full instruments cache when warm
      (fast path), falling back to broker only when the cache is cold.
Perf: fast path makes zero broker calls (_run not called).
Stale: chain store is populated whether warm path or broker path runs.
Reuse: _task_instruments._warm() rebuilds chain store as a free filter.
UX: log lines distinguish fast path ("built from instruments cache") from
    broker fallback ("broker fetch").
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

import os
os.environ.setdefault("PYTEST_RUNNING", "1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_instrument(s, e, t, x=None, k=None, ls=50):
    """Create a real Instrument struct."""
    from backend.api.routes.instruments import Instrument
    return Instrument(s=s, e=e, t=t, ls=ls, ts=0.05, x=x, k=k)


def _make_ir(items, cycle_date="2026-08-31"):
    from backend.api.routes.instruments import InstrumentsResponse
    return InstrumentsResponse(cycle_date=cycle_date, count=len(items), items=items)


def _make_cancelling_sleep(cancel_after: int = 2):
    """Return an async mock that raises CancelledError after `cancel_after` calls."""
    call_count = [0]

    async def _sleep(delay):
        call_count[0] += 1
        if call_count[0] >= cancel_after:
            raise asyncio.CancelledError("test escape")

    return _sleep


# ---------------------------------------------------------------------------
# Test 1 — fast path: instruments cache warm, no broker call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_instruments_warm_fast_path():
    """When instruments cache is warm, _warm() builds chain from it without calling _run."""
    nfo_items = [
        _make_instrument("NIFTY25SEP25000CE", "NFO", "CE", x="2026-09-25", k=25000.0),
        _make_instrument("NIFTY25SEP25100CE", "NFO", "CE", x="2026-09-25", k=25100.0),
        _make_instrument("NIFTY25SEP24900PE", "NFO", "PE", x="2026-09-25", k=24900.0),
    ]
    nse_item = _make_instrument("RELIANCE", "NSE", "EQ")
    full_ir = _make_ir(nfo_items + [nse_item])

    from backend.api.cache import _store as cache_store
    cache_store.pop("instruments_chain", None)
    cache_store.pop("instruments_chain_expiries", None)

    mock_run = AsyncMock()

    # _cache_peek_chain("instruments") returns full_ir (fast path),
    # _cache_peek_chain("instruments_chain") returns the chain after warm (breaks retry loop)
    def _peek_side_effect(key):
        if key == "instruments":
            return full_ir
        if key == "instruments_chain":
            # After fast path writes to cache_store, return it
            entry = cache_store.get("instruments_chain")
            return entry[1] if entry else None
        return None

    with patch("backend.api.background._cache_peek_chain", side_effect=_peek_side_effect), \
         patch("backend.api.background._run", mock_run), \
         patch("asyncio.sleep", side_effect=_make_cancelling_sleep(cancel_after=2)):

        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            from backend.api import background as _bg
            await _bg._task_chain_instruments()

    # _run must NOT have been called (fast path)
    mock_run.assert_not_called()

    # Chain store must contain only NFO items (3), not NSE
    assert "instruments_chain" in cache_store, "instruments_chain must be set after fast path"
    _, chain_ir = cache_store["instruments_chain"]
    assert chain_ir.count == 3, f"Expected 3 NFO items, got {chain_ir.count}"
    for inst in chain_ir.items:
        assert inst.e == "NFO", f"Non-NFO item found in chain store: {inst}"

    # Expiries index must be non-empty (CE/PE with future expiry)
    assert "instruments_chain_expiries" in cache_store, "instruments_chain_expiries must be set"
    _, exp_idx = cache_store["instruments_chain_expiries"]
    assert len(exp_idx) > 0, "Expiries index must have at least one underlying"
    assert any("NIFTY" in k for k in exp_idx), f"Expected NIFTY in expiries index, got: {list(exp_idx.keys())}"


# ---------------------------------------------------------------------------
# Test 2 — fallback: instruments cache cold, broker call made
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_instruments_warm_fallback_broker():
    """When instruments cache is cold (_cache_peek_chain returns None), _warm() calls _run."""
    mcx_items = [
        _make_instrument("CRUDEOIL24OCTFUT", "MCX", "FUT", x="2026-10-17", ls=100),
        _make_instrument("GOLD24OCTFUT", "MCX", "FUT", x="2026-10-05", ls=1),
    ]
    broker_ir = _make_ir(mcx_items)

    from backend.api.cache import _store as cache_store
    cache_store.pop("instruments_chain", None)
    cache_store.pop("instruments_chain_expiries", None)

    mock_run = AsyncMock(return_value=broker_ir)

    def _peek_cold(key):
        if key == "instruments":
            return None  # cold — triggers fallback
        if key == "instruments_chain":
            entry = cache_store.get("instruments_chain")
            return entry[1] if entry else None
        return None

    with patch("backend.api.background._cache_peek_chain", side_effect=_peek_cold), \
         patch("backend.api.background._run", mock_run), \
         patch("asyncio.sleep", side_effect=_make_cancelling_sleep(cancel_after=2)):

        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            from backend.api import background as _bg
            await _bg._task_chain_instruments()

    # _run MUST have been called (fallback path)
    assert mock_run.called, "_run must be called when instruments cache is cold"

    # Chain store set from broker result
    assert "instruments_chain" in cache_store, "instruments_chain must be set after broker fallback"
    _, chain_ir = cache_store["instruments_chain"]
    assert chain_ir.count == 2

    # Expiries index set
    assert "instruments_chain_expiries" in cache_store, "instruments_chain_expiries must be set"


# ---------------------------------------------------------------------------
# Test 3 — _task_instruments._warm() rebuilds chain as a side-effect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_task_instruments_warm_rebuilds_chain():
    """_task_instruments._warm() must populate instruments_chain from the full dump."""
    nfo_items = [
        _make_instrument(f"NIFTY25SEP2500{i}CE", "NFO", "CE", x="2026-09-25", k=float(25000 + i * 100))
        for i in range(5)
    ]
    nse_items = [
        _make_instrument("INFY", "NSE", "EQ"),
        _make_instrument("TCS", "NSE", "EQ"),
    ]
    full_ir = _make_ir(nfo_items + nse_items)

    from backend.api.cache import _store as cache_store
    cache_store.pop("instruments", None)
    cache_store.pop("instruments_chain", None)
    cache_store.pop("instruments_chain_expiries", None)

    mock_run = AsyncMock(return_value=full_ir)

    with patch("backend.api.background._run", mock_run), \
         patch("asyncio.sleep", side_effect=_make_cancelling_sleep(cancel_after=2)):

        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            from backend.api import background as _bg
            await _bg._task_instruments()

    # Full instruments store has all 7 items
    assert "instruments" in cache_store, "instruments must be set after _task_instruments._warm()"
    _, full_cached = cache_store["instruments"]
    assert full_cached.count == 7, f"Expected 7 items in instruments store, got {full_cached.count}"

    # Chain store has only the 5 NFO items
    assert "instruments_chain" in cache_store, "instruments_chain must be rebuilt by _task_instruments"
    _, chain_ir = cache_store["instruments_chain"]
    assert chain_ir.count == 5, f"Expected 5 NFO items in chain store, got {chain_ir.count}"
    for inst in chain_ir.items:
        assert inst.e == "NFO", f"Non-NFO item leaked into chain store: {inst}"

    # Expiries index built from chain items
    assert "instruments_chain_expiries" in cache_store
    _, exp_idx = cache_store["instruments_chain_expiries"]
    assert len(exp_idx) >= 1, f"Expected at least 1 underlying in expiries index, got: {exp_idx}"
