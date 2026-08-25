"""
Tests for the asyncio.wait_for timeout guard in _chain_snapshot_batch_quote.

Covers:
  1. Timeout path — broker.quote() stalls > 10s → TimeoutError caught, returns ({}, key_meta)
  2. Generic exception path — broker.quote() raises → returns ({}, key_meta)
  3. Happy path — broker.quote() returns data → returns (data, key_meta)
  4. Empty keys — skips quote call entirely, returns ({}, {})
  5. Timeout value — asyncio.wait_for called with timeout=10.0
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sym_by_strike(strikes: list[float]) -> dict:
    """Build a minimal sym_by_strike dict with CE/PE for each strike.

    Use realistic Kite tradingsymbol format so that option_quote_key()
    returns a valid string (NIFTY + 2-digit year + 3-char month + strike + CE/PE).
    """
    return {
        strike: {
            "CE": f"NIFTY26AUG{int(strike)}CE",
            "PE": f"NIFTY26AUG{int(strike)}PE",
        }
        for strike in strikes
    }


# ---------------------------------------------------------------------------
# 1. Timeout path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_snapshot_timeout_returns_empty():
    """
    When broker.quote() takes longer than 10 s, _chain_snapshot_batch_quote
    must catch TimeoutError and return ({}, key_meta) — never raise.
    """
    from backend.api.routes.options_helpers import _chain_snapshot_batch_quote

    sym_by_strike = _make_sym_by_strike([24000.0, 24050.0])
    window_strikes = [24000.0, 24050.0]

    mock_broker = MagicMock()

    async def _slow_thread(*_args, **_kwargs):
        await asyncio.sleep(30)  # far beyond the 10s timeout
        return {"some": "data"}

    with patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=mock_broker,
    ), patch(
        "asyncio.to_thread",
        new=AsyncMock(side_effect=_slow_thread),
    ):
        quote_resp, key_meta = await _chain_snapshot_batch_quote(
            "NIFTY", "2026-08-28", sym_by_strike, window_strikes
        )

    assert quote_resp == {}, "timeout must yield empty quote_resp"
    # key_meta should still contain the expected entries (4 keys: 2 strikes × 2 sides)
    assert len(key_meta) == 4


# ---------------------------------------------------------------------------
# 2. Generic exception path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_snapshot_broker_exception_returns_empty():
    """When broker.quote() raises a generic exception, return ({}, key_meta)."""
    from backend.api.routes.options_helpers import _chain_snapshot_batch_quote

    sym_by_strike = _make_sym_by_strike([24000.0])
    window_strikes = [24000.0]

    mock_broker = MagicMock()

    with patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=mock_broker,
    ), patch(
        "asyncio.to_thread",
        new=AsyncMock(side_effect=RuntimeError("broker down")),
    ):
        quote_resp, key_meta = await _chain_snapshot_batch_quote(
            "NIFTY", "2026-08-28", sym_by_strike, window_strikes
        )

    assert quote_resp == {}
    assert len(key_meta) == 2  # 1 strike × 2 sides


# ---------------------------------------------------------------------------
# 3. Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_snapshot_happy_path():
    """When broker.quote() returns data promptly, that data is returned."""
    from backend.api.routes.options_helpers import _chain_snapshot_batch_quote

    sym_by_strike = _make_sym_by_strike([24000.0])
    window_strikes = [24000.0]

    fake_data = {
        "NFO:NIFTY24000CE": {"last_price": 100.0},
        "NFO:NIFTY24000PE": {"last_price": 90.0},
    }

    mock_broker = MagicMock()

    with patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=mock_broker,
    ), patch(
        "asyncio.to_thread",
        new=AsyncMock(return_value=fake_data),
    ):
        quote_resp, key_meta = await _chain_snapshot_batch_quote(
            "NIFTY", "2026-08-28", sym_by_strike, window_strikes
        )

    assert quote_resp == fake_data
    assert len(key_meta) == 2


# ---------------------------------------------------------------------------
# 4. Empty keys — no broker call at all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_snapshot_empty_keys_skips_quote():
    """When all symbols are None/empty, no broker call is made."""
    from backend.api.routes.options_helpers import _chain_snapshot_batch_quote

    sym_by_strike = {24000.0: {"CE": None, "PE": None}}
    window_strikes = [24000.0]

    mock_broker = MagicMock()

    with patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=mock_broker,
    ):
        quote_resp, key_meta = await _chain_snapshot_batch_quote(
            "NIFTY", "2026-08-28", sym_by_strike, window_strikes
        )

    assert quote_resp == {}
    assert key_meta == {}
    mock_broker.quote.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Timeout boundary — asyncio.wait_for uses exactly 10.0 s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_snapshot_wait_for_has_10s_timeout():
    """
    Verify the timeout is wired to 10.0 s by recording what timeout value
    asyncio.wait_for receives during the broker call.
    """
    from backend.api.routes.options_helpers import _chain_snapshot_batch_quote

    sym_by_strike = _make_sym_by_strike([24000.0])
    window_strikes = [24000.0]

    captured_timeout: list[float] = []
    original_wait_for = asyncio.wait_for

    async def _recording_wait_for(coro, timeout=None, **kw):
        captured_timeout.append(timeout)
        # Complete normally with empty result so the function doesn't hang
        return await original_wait_for(
            asyncio.coroutine(lambda: {})() if False else _instant_coro(),
            timeout=60.0,
            **kw,
        )

    async def _instant_coro():
        return {}

    fake_data: dict = {}
    mock_broker = MagicMock()

    with patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=mock_broker,
    ), patch(
        "asyncio.to_thread",
        new=AsyncMock(return_value=fake_data),
    ), patch(
        "backend.api.routes.options_helpers.asyncio.wait_for",
        side_effect=_recording_wait_for,
    ):
        await _chain_snapshot_batch_quote(
            "NIFTY", "2026-08-28", sym_by_strike, window_strikes
        )

    assert captured_timeout == [10.0], (
        f"Expected timeout=10.0 passed to asyncio.wait_for, got {captured_timeout}"
    )
