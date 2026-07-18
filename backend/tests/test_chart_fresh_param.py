"""
test_chart_fresh_param.py

Tests for the options.historical `fresh` parameter cache-bypass behavior.
Ensures the `fresh` query param controls whether the in-process _HIST_CACHE
is consulted (fresh=False) or bypassed (fresh=True).

Two quality dimensions (feedback_test_dimensions.md):

  1. Cache hit (fresh=False) — seeded cache returned unchanged
  2. Cache bypass (fresh=True) — cached result NOT returned; fresh
     broker path triggered instead

Note on calling the handler under test:
  Litestar wraps every @get/@post decorated method as an HTTPRouteHandler
  whose .fn attribute is the original coroutine. Tests call
  `handler.fn(controller_instance, ...)` directly to bypass Litestar's
  request-lifecycle machinery (dependency injection, guards, etc.) and
  exercise only the handler logic.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest


def _make_fake_bars(n: int, base: date | None = None) -> list[dict]:
    """Build a list of OHLCVBar-like dicts for testing."""
    d = base or date(2025, 1, 2)
    bars = []
    for i in range(n):
        bars.append({
            "date":   (d + timedelta(days=i)).isoformat(),
            "open":   100.0, "high": 105.0,
            "low":    95.0,  "close": 102.0,
            "volume": 1000,
        })
    return bars


def _make_fake_response(bars: list[dict]) -> "HistoricalResponse":
    """Construct a minimal HistoricalResponse with given bars."""
    from backend.api.routes.options import HistoricalResponse, HistoricalBar
    return HistoricalResponse(
        symbol="TESTOPT",
        instrument_token=12345,
        interval="day",
        bars=[
            HistoricalBar(
                ts=bar["date"],
                open=bar["open"],
                high=bar["high"],
                low=bar["low"],
                close=bar["close"],
                volume=bar["volume"],
            )
            for bar in bars
        ],
        partial=False,
    )


# ── Test 1: Cache hit behavior (fresh=False) ───────────────────────────

@pytest.mark.asyncio
async def test_historical_cache_hit_when_fresh_false() -> None:
    """
    When fresh=False and a result is in the cache, the cached result
    must be returned without calling the broker store layers.
    """
    from backend.api.routes.options import OptionsController

    cached_bars = _make_fake_bars(50)
    cached_response = _make_fake_response(cached_bars)

    call_log: list[str] = []

    async def fake_historical_ohlcv_store(*args, **kw):
        call_log.append("ohlcv_store_called")
        return None  # Should never reach here

    async def fake_historical_broker_loop(*args, **kw):
        call_log.append("broker_loop_called")
        return _make_fake_response([])  # Should never reach here

    with (
        patch("backend.api.routes.options._hist_cache_get") as mock_cache_get,
        patch("backend.api.routes.options._hist_cache_put"),
        patch(
            "backend.api.routes.options_helpers._historical_ohlcv_store",
            side_effect=fake_historical_ohlcv_store,
        ),
        patch(
            "backend.api.routes.options_helpers._historical_broker_loop",
            side_effect=fake_historical_broker_loop,
        ),
    ):
        # Seed the cache mock to return our fake response
        mock_cache_get.return_value = cached_response

        controller = OptionsController.__new__(OptionsController)
        result = await OptionsController.historical.fn(
            controller,
            symbol="TESTOPT",
            days=30,
            interval="day",
            exchange="NFO",
            fresh=False,  # <-- Cache should be used
        )

    # Verify: cache was consulted
    assert mock_cache_get.called, "Cache _hist_cache_get should have been called"

    # Verify: broker stores were NOT called (because cache hit)
    assert len(call_log) == 0, (
        f"Expected no broker calls (cache hit), but got: {call_log}"
    )

    # Verify: returned response matches the cached one
    assert result == cached_response, (
        "Returned response should match the cached response"
    )
    assert len(result.bars) == len(cached_bars), (
        f"Expected {len(cached_bars)} bars, got {len(result.bars)}"
    )


# ── Test 2: Cache bypass behavior (fresh=True) ─────────────────────────

@pytest.mark.asyncio
async def test_historical_cache_bypass_when_fresh_true() -> None:
    """
    When fresh=True, the handler must NOT consult _hist_cache_get,
    even if a cached result would be available. The broker path must
    be triggered and the returned result must NOT be the cached one.
    """
    from backend.api.routes.options import OptionsController

    cached_bars = _make_fake_bars(50)
    cached_response = _make_fake_response(cached_bars)

    fresh_bars = _make_fake_bars(100)
    fresh_response = _make_fake_response(fresh_bars)

    call_log: list[dict] = []

    async def fake_historical_ohlcv_store(*args, **kw):
        call_log.append({"fn": "ohlcv_store"})
        return fresh_response

    with (
        patch("backend.api.routes.options._hist_cache_get") as mock_cache_get,
        patch("backend.api.routes.options._hist_cache_put"),
        patch("backend.api.routes.options._ohlcv_trace_enabled", return_value=False),
        patch(
            "backend.api.routes.options_helpers._historical_ohlcv_store",
            side_effect=fake_historical_ohlcv_store,
        ),
    ):
        # Set up the cache mock to return our cached response
        # (it should NOT be called when fresh=True)
        mock_cache_get.return_value = cached_response

        controller = OptionsController.__new__(OptionsController)
        result = await OptionsController.historical.fn(
            controller,
            symbol="TESTOPT",
            days=30,
            interval="day",
            exchange="NFO",
            fresh=True,  # <-- Cache should be BYPASSED
        )

    # Verify: cache was NOT consulted
    assert not mock_cache_get.called, (
        "Cache _hist_cache_get should NOT have been called when fresh=True"
    )

    # Verify: broker store was called (because cache was bypassed)
    assert len(call_log) > 0, (
        "Expected broker store to be called when fresh=True"
    )

    # Verify: returned response is the FRESH one, not the cached one
    assert result == fresh_response, (
        "Returned response should be the fresh result, not the cached one"
    )
    assert len(result.bars) == len(fresh_bars), (
        f"Expected {len(fresh_bars)} bars (fresh), got {len(result.bars)}"
    )
    # Double-check: fresh response has more bars than cached response
    assert len(result.bars) != len(cached_bars), (
        "Result must differ from the cached response"
    )
