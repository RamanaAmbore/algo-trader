"""
Tests for anchor contract subscription in the strategy-analytics endpoint.

Verifies that when `_resolve_spot` returns a non-null `_spot_anchor` (e.g.,
an MCX futures contract used as the spot proxy for the underlying), the
strategy endpoint fires an async task to subscribe to the anchor's LTP via
the KiteTicker WebSocket to ensure real-time updates flow through.

Covers:
  - _subscribe_anchor_nowait resolves tokens and subscribes to ticker
  - Correct anchor symbol and exchange are passed
  - Failures in subscription task are silently caught
  - Null tokens are handled gracefully
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio


# ─────────────────────────────────────────────────────────────────────────────
# Test: _subscribe_anchor_nowait resolves token and subscribes to ticker
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_anchor_nowait_resolves_token_and_subscribes():
    """
    Unit test for _subscribe_anchor_nowait directly.
    Verifies it:
      1. Resolves instrument token for the anchor symbol via _resolve_token_for_sym
      2. Calls get_ticker().subscribe_with_sym() with [(token, anchor_symbol)]
      3. Succeeds without raising
    """
    from backend.api.routes.options import _subscribe_anchor_nowait

    # Mock _resolve_token_for_sym to return a token
    token = 12345
    mock_resolve_token = AsyncMock(return_value=token)

    # Mock the ticker
    ticker_mock = MagicMock()
    ticker_mock.subscribe_with_sym = MagicMock()

    with patch(
        "backend.api.routes.quote._resolve_token_for_sym",
        mock_resolve_token,
    ), patch(
        "backend.brokers.kite_ticker.get_ticker",
        return_value=ticker_mock,
    ):
        # Call the function
        await _subscribe_anchor_nowait("GOLD26OCTFUT", "MCX")

        # Verify token was resolved with correct arguments
        mock_resolve_token.assert_called_once_with("GOLD26OCTFUT", "MCX")

        # Verify subscribe was called with the correct arguments
        ticker_mock.subscribe_with_sym.assert_called_once()
        call_args = ticker_mock.subscribe_with_sym.call_args[0][0]
        assert call_args == [(token, "GOLD26OCTFUT")], f"Expected [(12345, 'GOLD26OCTFUT')], got {call_args}"


# ─────────────────────────────────────────────────────────────────────────────
# Test: _subscribe_anchor_nowait silently catches exceptions
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_anchor_nowait_silently_catches_exceptions():
    """
    Verify that _subscribe_anchor_nowait swallows exceptions and doesn't
    bubble them up (since it's called via asyncio.create_task, exceptions
    must not propagate to the main request handler).
    """
    from backend.api.routes.options import _subscribe_anchor_nowait

    # Mock _resolve_token_for_sym to raise an exception
    with patch(
        "backend.api.routes.quote._resolve_token_for_sym",
        new=AsyncMock(side_effect=RuntimeError("Token resolution failed")),
    ):
        # Call the function — it should NOT raise
        try:
            await _subscribe_anchor_nowait("GOLD26OCTFUT", "MCX")
            # Success — no exception bubbled
        except Exception as e:
            pytest.fail(f"_subscribe_anchor_nowait should catch exceptions, but raised {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Test: _subscribe_anchor_nowait handles null token
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_anchor_nowait_handles_null_token():
    """
    Verify that _subscribe_anchor_nowait handles the case where
    _resolve_token_for_sym returns None (token not found).
    When token is None, subscribe_with_sym should NOT be called.
    """
    from backend.api.routes.options import _subscribe_anchor_nowait

    # Mock _resolve_token_for_sym to return None
    with patch(
        "backend.api.routes.quote._resolve_token_for_sym",
        new=AsyncMock(return_value=None),
    ):
        # Mock the ticker (should NOT be called)
        ticker_mock = MagicMock()
        ticker_mock.subscribe_with_sym = MagicMock()

        with patch(
            "backend.brokers.kite_ticker.get_ticker",
            return_value=ticker_mock,
        ):
            # Call the function
            await _subscribe_anchor_nowait("GOLD26OCTFUT", "MCX")

            # Verify subscribe was NOT called (because token was None)
            ticker_mock.subscribe_with_sym.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Test: _subscribe_anchor_nowait exception in subscribe_with_sym
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_anchor_nowait_catches_subscribe_exception():
    """
    Verify that _subscribe_anchor_nowait also catches exceptions from
    the ticker.subscribe_with_sym() call itself (defensive programming).
    """
    from backend.api.routes.options import _subscribe_anchor_nowait

    token = 12345
    mock_resolve_token = AsyncMock(return_value=token)

    # Mock the ticker to raise on subscribe
    ticker_mock = MagicMock()
    ticker_mock.subscribe_with_sym = MagicMock(side_effect=RuntimeError("Ticker error"))

    with patch(
        "backend.api.routes.quote._resolve_token_for_sym",
        mock_resolve_token,
    ), patch(
        "backend.brokers.kite_ticker.get_ticker",
        return_value=ticker_mock,
    ):
        # Call the function — should not raise despite subscribe_with_sym error
        try:
            await _subscribe_anchor_nowait("GOLD26OCTFUT", "MCX")
            # Success — exception was caught
        except Exception as e:
            pytest.fail(f"_subscribe_anchor_nowait should catch ticker.subscribe errors, but raised {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Test: strategy endpoint fires asyncio.create_task for anchor subscription
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_strategy_analytics_impl_creates_task_for_anchor():
    """
    Integration test: when _strategy_analytics_impl resolves a non-null anchor,
    it should call asyncio.create_task(_subscribe_anchor_nowait(...)).

    We can't easily test asyncio.create_task's actual task creation without
    running a full endpoint test, so this test mocks the whole path and
    verifies the code path that calls create_task exists.
    """
    # This is more of a code inspection test — the actual test would require
    # hitting the endpoint with full broker mocks. The unit tests above cover
    # the _subscribe_anchor_nowait function itself.
    #
    # Key invariant to verify: line 3077 in options.py calls
    #   asyncio.create_task(_subscribe_anchor_nowait(_spot_anchor, "MCX" or "NFO"))
    # when _spot_anchor is truthy.

    # Inspect the source to ensure the pattern exists
    import inspect
    from backend.api.routes.options import OptionsController

    source = inspect.getsource(OptionsController._strategy_analytics_impl)
    assert "asyncio.create_task" in source, "Expected asyncio.create_task in _strategy_analytics_impl"
    assert "_subscribe_anchor_nowait" in source, "Expected _subscribe_anchor_nowait call in _strategy_analytics_impl"
    assert "if _spot_anchor:" in source, "Expected 'if _spot_anchor:' guard before create_task call"
