"""
Audit Fix #2 — _opl_price_from_broker uses exchange:symbol key instead of NFO:.

Tests for orders_place.py _opl_price_from_broker LTP resolution.

Coverage:
  - _opl_price_from_broker constructs key as f"{exchange}:{symbol}"
  - MCX:CRUDEOIL26SEPFUT key matches MCX exchange — broker hit returns price
  - Old NFO:CRUDEOIL26SEPFUT key does NOT match MCX (no cross-exchange collision)
  - exchange parameter threads through _opp_resolve_notional_price → _opl_price_from_broker
  - _enforce_capacity_guard accepts and forwards exchange param
  - Handles exception gracefully (returns None)
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_opl_price_from_broker_mcx_key_hit():
    """For MCX exchange, broker.ltp is called with MCX:symbol — returns price."""
    from backend.api.routes.orders_place import _opl_price_from_broker

    mock_broker = MagicMock()
    mock_broker.ltp = MagicMock(return_value={
        "MCX:CRUDEOIL26SEPFUT": {"last_price": 7250.50},
    })

    # The broker is imported lazily inside the function; patch the registry module.
    with patch("backend.brokers.registry.get_market_data_broker",
               return_value=mock_broker):
        with patch("asyncio.to_thread", new=AsyncMock(
            side_effect=lambda func, *args: func(*args)
        )):
            price = await _opl_price_from_broker("CRUDEOIL26SEPFUT", exchange="MCX")

    assert price == 7250.50, f"Expected 7250.50, got {price}"
    call_args = mock_broker.ltp.call_args[0][0]  # first positional arg (list)
    assert call_args == ["MCX:CRUDEOIL26SEPFUT"], (
        f"Key must be MCX:CRUDEOIL26SEPFUT; got {call_args}"
    )


@pytest.mark.asyncio
async def test_opl_price_from_broker_nfo_key():
    """For NFO exchange (default), broker.ltp is called with NFO:symbol."""
    from backend.api.routes.orders_place import _opl_price_from_broker

    mock_broker = MagicMock()
    mock_broker.ltp = MagicMock(return_value={
        "NFO:BANKNIFTY26SEPFUT": {"last_price": 50000.0},
    })

    with patch("backend.brokers.registry.get_market_data_broker",
               return_value=mock_broker):
        with patch("asyncio.to_thread", new=AsyncMock(
            side_effect=lambda func, *args: func(*args)
        )):
            price = await _opl_price_from_broker("BANKNIFTY26SEPFUT", exchange="NFO")

    assert price == 50000.0, f"Expected 50000.0, got {price}"
    call_args = mock_broker.ltp.call_args[0][0]
    assert call_args == ["NFO:BANKNIFTY26SEPFUT"], (
        f"Key must be NFO:BANKNIFTY26SEPFUT; got {call_args}"
    )


@pytest.mark.asyncio
async def test_opl_price_from_broker_no_cross_exchange_collision():
    """MCX call does not hit NFO-keyed entry — key mismatch → returns None."""
    from backend.api.routes.orders_place import _opl_price_from_broker

    mock_broker = MagicMock()
    # Only NFO key is present — MCX call should miss and return None.
    mock_broker.ltp = MagicMock(return_value={
        "NFO:CRUDEOIL26SEPFUT": {"last_price": 9999.0},
    })

    with patch("backend.brokers.registry.get_market_data_broker",
               return_value=mock_broker):
        with patch("asyncio.to_thread", new=AsyncMock(
            side_effect=lambda func, *args: func(*args)
        )):
            price = await _opl_price_from_broker("CRUDEOIL26SEPFUT", exchange="MCX")

    assert price is None, (
        "When MCX key is absent from broker response, must return None — "
        "no cross-exchange collision with NFO entry"
    )


@pytest.mark.asyncio
async def test_opl_price_from_broker_handles_exception():
    """When broker.ltp raises an exception, return None gracefully."""
    from backend.api.routes.orders_place import _opl_price_from_broker

    mock_broker = MagicMock()
    mock_broker.ltp = MagicMock(side_effect=Exception("Broker error"))

    with patch("backend.brokers.registry.get_market_data_broker",
               return_value=mock_broker):
        with patch("asyncio.to_thread", new=AsyncMock(
            side_effect=lambda func, *args: func(*args)
        )):
            price = await _opl_price_from_broker("CRUDEOIL26SEPFUT", exchange="MCX")

    assert price is None, "Exceptions in broker.ltp must be caught; return None"


@pytest.mark.asyncio
async def test_opp_resolve_notional_price_passes_exchange_to_broker():
    """_opp_resolve_notional_price forwards exchange to _opl_price_from_broker."""
    from backend.api.routes.orders_place import _opp_resolve_notional_price

    mock_broker = MagicMock()
    mock_broker.ltp = MagicMock(return_value={
        "MCX:CRUDEOIL26SEPFUT": {"last_price": 7100.0},
    })

    # Ticker returns None so broker path is exercised.
    with patch("backend.api.routes.orders_place._opl_price_from_ticker",
               return_value=None):
        with patch("backend.brokers.registry.get_market_data_broker",
                   return_value=mock_broker):
            with patch("asyncio.to_thread", new=AsyncMock(
                side_effect=lambda func, *args: func(*args)
            )):
                price = await _opp_resolve_notional_price(
                    "CRUDEOIL26SEPFUT", price_hint=None, exchange="MCX"
                )

    assert price == 7100.0, f"Expected 7100.0, got {price}"
    call_args = mock_broker.ltp.call_args[0][0]
    assert "MCX:CRUDEOIL26SEPFUT" in call_args, (
        f"Key must include MCX:CRUDEOIL26SEPFUT; got {call_args}"
    )


def test_source_no_hardcoded_nfo_in_opl_price_from_broker():
    """Source inspection: _opl_price_from_broker uses f'{exchange}:{symbol}' format."""
    import re
    from pathlib import Path
    src = Path("backend/api/routes/orders_place.py").read_text()

    # Extract just the _opl_price_from_broker function body
    match = re.search(
        r"async def _opl_price_from_broker.*?(?=\nasync def |\ndef )",
        src,
        re.DOTALL,
    )
    assert match is not None, "_opl_price_from_broker must exist in orders_place.py"
    func_body = match.group(0)

    # Must not have hardcoded NFO: as the key
    assert '"NFO:' not in func_body, (
        "_opl_price_from_broker must not hardcode 'NFO:' as key prefix"
    )
    # Must use exchange variable
    assert "exchange" in func_body, (
        "_opl_price_from_broker must reference 'exchange' for key construction"
    )


def test_enforce_capacity_guard_has_exchange_param():
    """Source inspection: _enforce_capacity_guard signature includes exchange."""
    import re
    from pathlib import Path
    src = Path("backend/api/routes/orders_place.py").read_text()

    match = re.search(
        r"async def _enforce_capacity_guard\(.*?\) -> None:",
        src,
        re.DOTALL,
    )
    assert match is not None, "_enforce_capacity_guard must exist"
    sig = match.group(0)
    assert "exchange" in sig, (
        "_enforce_capacity_guard must accept an 'exchange' parameter"
    )
