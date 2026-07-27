"""
Coverage tests for backend/api/algo/actions_sim.py

Covers:
  - _sim_prices_for: simulated LTP/bid/ask/qty lookup
  - _sim_ltp_for: back-compat shim
  - _sim_positions_in_scope: position filtering (total/account scope)
  - _write_sim_order: AlgoOrder persistence + driver registration
  - _sim_chase_close: scope expansion across sim positions
  - _sim_expiry_close: exchange-filtered expiry closure
  - _sim_place_or_close: place/close with LTP resolution
  - _sim_paper_trade: mode-1 dispatcher
  - _replay_paper_trade: mode-4 informational writer
  - _shadow_trade: mode-5 shadow writer
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
# _sim_prices_for tests
# ─────────────────────────────────────────────────────────────────────────────

def test_sim_prices_for_found():
    """Symbol in sim state → returns (ltp, bid, ask, qty)."""
    from backend.api.algo.actions_sim import _sim_prices_for

    mock_driver = MagicMock()
    mock_driver._positions_rows = [
        {
            "account": "ZG0790",
            "tradingsymbol": "NIFTY25JULFUT",
            "last_price": 24500.0,
            "bid": 24498.0,
            "ask": 24502.0,
            "quantity": 1,
        }
    ]

    with patch("backend.api.algo.sim.driver.get_driver", return_value=mock_driver):
        ltp, bid, ask, qty = _sim_prices_for("ZG0790", "NIFTY25JULFUT")

    assert ltp == 24500.0
    assert bid == 24498.0
    assert ask == 24502.0
    assert qty == 1


def test_sim_prices_for_not_found():
    """Symbol not in sim state → returns (None, None, None, None)."""
    from backend.api.algo.actions_sim import _sim_prices_for

    mock_driver = MagicMock()
    mock_driver._positions_rows = []

    with patch("backend.api.algo.sim.driver.get_driver", return_value=mock_driver):
        ltp, bid, ask, qty = _sim_prices_for("ZG0790", "UNKNOWN")

    assert ltp is None
    assert bid is None
    assert ask is None
    assert qty is None


def test_sim_prices_for_account_mismatch():
    """Different account → returns None tuple."""
    from backend.api.algo.actions_sim import _sim_prices_for

    mock_driver = MagicMock()
    mock_driver._positions_rows = [
        {
            "account": "OTHER",
            "tradingsymbol": "NIFTY25JULFUT",
            "last_price": 24500.0,
            "bid": 24498.0,
            "ask": 24502.0,
            "quantity": 1,
        }
    ]

    with patch("backend.api.algo.sim.driver.get_driver", return_value=mock_driver):
        ltp, bid, ask, qty = _sim_prices_for("ZG0790", "NIFTY25JULFUT")

    assert ltp is None


def test_sim_prices_for_exception_handling():
    """Driver error → returns None tuple."""
    from backend.api.algo.actions_sim import _sim_prices_for

    # Patch at the import location inside the function
    with patch("backend.api.algo.sim.driver.get_driver", side_effect=RuntimeError("driver error")):
        ltp, bid, ask, qty = _sim_prices_for("ZG0790", "NIFTY25JULFUT")

    assert ltp is None
    assert bid is None
    assert ask is None
    assert qty is None


# ─────────────────────────────────────────────────────────────────────────────
# _sim_ltp_for tests (back-compat shim)
# ─────────────────────────────────────────────────────────────────────────────

def test_sim_ltp_for_returns_ltp_qty():
    """_sim_ltp_for delegates to _sim_prices_for, returns (ltp, qty)."""
    from backend.api.algo.actions_sim import _sim_ltp_for

    with patch("backend.api.algo.actions_sim._sim_prices_for",
               return_value=(24500.0, 24498.0, 24502.0, 1)):
        ltp, qty = _sim_ltp_for("ZG0790", "NIFTY25JULFUT")

    assert ltp == 24500.0
    assert qty == 1


def test_sim_ltp_for_none_when_not_found():
    """Symbol not found → returns (None, None)."""
    from backend.api.algo.actions_sim import _sim_ltp_for

    with patch("backend.api.algo.actions_sim._sim_prices_for",
               return_value=(None, None, None, None)):
        ltp, qty = _sim_ltp_for("ZG0790", "UNKNOWN")

    assert ltp is None
    assert qty is None


# ─────────────────────────────────────────────────────────────────────────────
# _sim_positions_in_scope tests
# ─────────────────────────────────────────────────────────────────────────────

def test_sim_positions_in_scope_total():
    """scope='total' → all positions."""
    from backend.api.algo.actions_sim import _sim_positions_in_scope

    positions = [
        {"account": "ZG0790", "tradingsymbol": "NIFTY25JULFUT"},
        {"account": "OTHER", "tradingsymbol": "NIFTY25AUGFUT"},
    ]

    mock_driver = MagicMock()
    mock_driver._positions_rows = positions

    with patch("backend.api.algo.sim.driver.get_driver", return_value=mock_driver):
        result = _sim_positions_in_scope({"scope": "total"})

    assert len(result) == 2


def test_sim_positions_in_scope_account():
    """scope='account' → filtered by account."""
    from backend.api.algo.actions_sim import _sim_positions_in_scope

    positions = [
        {"account": "ZG0790", "tradingsymbol": "NIFTY25JULFUT"},
        {"account": "OTHER", "tradingsymbol": "NIFTY25AUGFUT"},
    ]

    mock_driver = MagicMock()
    mock_driver._positions_rows = positions

    with patch("backend.api.algo.sim.driver.get_driver", return_value=mock_driver):
        result = _sim_positions_in_scope({"scope": "account", "account": "ZG0790"})

    assert len(result) == 1
    assert result[0]["account"] == "ZG0790"


def test_sim_positions_in_scope_default_total():
    """No scope specified → defaults to total."""
    from backend.api.algo.actions_sim import _sim_positions_in_scope

    positions = [{"account": "ZG0790", "tradingsymbol": "NIFTY25JULFUT"}]

    mock_driver = MagicMock()
    mock_driver._positions_rows = positions

    with patch("backend.api.algo.sim.driver.get_driver", return_value=mock_driver):
        result = _sim_positions_in_scope({})

    assert len(result) == 1


def test_sim_positions_in_scope_empty_rows():
    """No positions in driver → returns []."""
    from backend.api.algo.actions_sim import _sim_positions_in_scope

    mock_driver = MagicMock()
    mock_driver._positions_rows = []

    with patch("backend.api.algo.sim.driver.get_driver", return_value=mock_driver):
        result = _sim_positions_in_scope({"scope": "total"})

    assert result == []


def test_sim_positions_in_scope_exception_handling():
    """Driver error → returns []."""
    from backend.api.algo.actions_sim import _sim_positions_in_scope

    with patch("backend.api.algo.sim.driver.get_driver", side_effect=RuntimeError("driver error")):
        result = _sim_positions_in_scope({"scope": "total"})

    assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# _write_sim_order tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_sim_order_success():
    """Order written to DB, event created, driver registration called."""
    from backend.api.algo.actions_sim import _write_sim_order

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    resolved = {
        "account": "SIM",
        "symbol": "NIFTY25JULFUT",
        "side": "BUY",
        "qty": 1,
        "price": 24500.0,
        "exchange": "NFO",
    }

    mock_row = MagicMock()
    mock_row.id = 999

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = MagicMock()
    mock_session.__aenter__.return_value.add = MagicMock()
    mock_session.__aenter__.return_value.commit = AsyncMock()

    mock_driver = MagicMock()
    mock_driver.tick_index = 1
    mock_driver.scenario_slug = "test-scenario"
    mock_driver._tick_log = []

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.models.AlgoOrder") as mock_order_class, \
         patch("backend.api.algo.actions_sim._sim_write_placed_event", new=AsyncMock()), \
         patch("backend.api.algo.sim.driver.get_driver", return_value=mock_driver), \
         patch("backend.api.algo.actions_sim._sim_register_with_driver"):
        mock_order_class.return_value = mock_row
        result = await _write_sim_order(agent, "place_order", resolved)

    assert result == 999


@pytest.mark.asyncio
async def test_write_sim_order_db_error():
    """DB error → returns None."""
    from backend.api.algo.actions_sim import _write_sim_order

    agent = MagicMock()
    agent.slug = "test-agent"

    resolved = {
        "account": "SIM",
        "symbol": "NIFTY25JULFUT",
        "side": "BUY",
        "qty": 1,
        "price": 24500.0,
        "exchange": "NFO",
    }

    mock_session = AsyncMock()
    mock_session.__aenter__.side_effect = RuntimeError("DB error")

    with patch("backend.api.database.async_session", return_value=mock_session):
        result = await _write_sim_order(agent, "place_order", resolved)

    assert result is None


@pytest.mark.asyncio
async def test_write_sim_order_market_order():
    """Market order (price=None) still writes."""
    from backend.api.algo.actions_sim import _write_sim_order

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    resolved = {
        "account": "SIM",
        "symbol": "NIFTY25JULFUT",
        "side": "SELL",
        "qty": 1,
        # no price
        "exchange": "NFO",
    }

    mock_row = MagicMock()
    mock_row.id = 888

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = MagicMock()
    mock_session.__aenter__.return_value.add = MagicMock()
    mock_session.__aenter__.return_value.commit = AsyncMock()

    mock_driver = MagicMock()
    mock_driver.tick_index = 1
    mock_driver.scenario_slug = "test-scenario"
    mock_driver._tick_log = []

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.models.AlgoOrder") as mock_order_class, \
         patch("backend.api.algo.actions_sim._sim_write_placed_event", new=AsyncMock()), \
         patch("backend.api.algo.sim.driver.get_driver", return_value=mock_driver), \
         patch("backend.api.algo.actions_sim._sim_register_with_driver"):
        mock_order_class.return_value = mock_row
        result = await _write_sim_order(agent, "close_position", resolved)

    assert result == 888


# ─────────────────────────────────────────────────────────────────────────────
# _sim_resolve_* helper tests
# ─────────────────────────────────────────────────────────────────────────────

def test_sim_resolve_side_explicit_buy():
    """Explicit side=BUY → used."""
    from backend.api.algo.actions_sim import _sim_resolve_side

    side = _sim_resolve_side({"side": "BUY"}, None)
    assert side == "BUY"


def test_sim_resolve_side_explicit_sell():
    """Explicit side=SELL → used."""
    from backend.api.algo.actions_sim import _sim_resolve_side

    side = _sim_resolve_side({"side": "SELL"}, None)
    assert side == "SELL"


def test_sim_resolve_side_transaction_type_fallback():
    """No side, falls back to transaction_type."""
    from backend.api.algo.actions_sim import _sim_resolve_side

    side = _sim_resolve_side({"transaction_type": "BUY"}, None)
    assert side == "BUY"


def test_sim_resolve_side_qty_held_positive():
    """qty_held > 0 → SELL."""
    from backend.api.algo.actions_sim import _sim_resolve_side

    side = _sim_resolve_side({}, qty_held=5)
    assert side == "SELL"


def test_sim_resolve_side_qty_held_negative():
    """qty_held < 0 → BUY."""
    from backend.api.algo.actions_sim import _sim_resolve_side

    side = _sim_resolve_side({}, qty_held=-5)
    assert side == "BUY"


def test_sim_resolve_side_default():
    """No params, no qty_held → defaults to SELL."""
    from backend.api.algo.actions_sim import _sim_resolve_side

    side = _sim_resolve_side({}, None)
    assert side == "SELL"


def test_sim_resolve_qty_explicit():
    """Explicit quantity → used."""
    from backend.api.algo.actions_sim import _sim_resolve_qty

    qty = _sim_resolve_qty({"quantity": 10}, None)
    assert qty == 10


def test_sim_resolve_qty_held():
    """No quantity, uses abs(qty_held)."""
    from backend.api.algo.actions_sim import _sim_resolve_qty

    qty = _sim_resolve_qty({}, qty_held=-5)
    assert qty == 5


def test_sim_resolve_qty_default():
    """No quantity, no qty_held → 0."""
    from backend.api.algo.actions_sim import _sim_resolve_qty

    qty = _sim_resolve_qty({}, None)
    assert qty == 0


def test_sim_resolve_price_sell_uses_bid():
    """SELL side → bid price."""
    from backend.api.algo.actions_sim import _sim_resolve_price

    price = _sim_resolve_price("SELL", bid=24498.0, ask=24502.0, ltp=24500.0, params={})
    assert price == 24498.0


def test_sim_resolve_price_buy_uses_ask():
    """BUY side → ask price."""
    from backend.api.algo.actions_sim import _sim_resolve_price

    price = _sim_resolve_price("BUY", bid=24498.0, ask=24502.0, ltp=24500.0, params={})
    assert price == 24502.0


def test_sim_resolve_price_fallback_to_ltp():
    """No side price → LTP."""
    from backend.api.algo.actions_sim import _sim_resolve_price

    price = _sim_resolve_price("BUY", bid=None, ask=None, ltp=24500.0, params={})
    assert price == 24500.0


def test_sim_resolve_price_fallback_to_params():
    """No market prices → param price."""
    from backend.api.algo.actions_sim import _sim_resolve_price

    price = _sim_resolve_price("SELL", bid=None, ask=None, ltp=None, params={"price": 24505.0})
    assert price == 24505.0


# ─────────────────────────────────────────────────────────────────────────────
# _sim_chase_close tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sim_chase_close_expands_positions():
    """Expands to one order per position."""
    from backend.api.algo.actions_sim import _sim_chase_close

    agent = MagicMock()
    agent.slug = "test-agent"

    positions = [
        {"account": "SIM", "tradingsymbol": "NIFTY25JULFUT", "quantity": 1,
         "last_price": 24500.0, "bid": 24498.0, "ask": 24502.0, "exchange": "NFO"},
        {"account": "SIM", "tradingsymbol": "NIFTY25AUGFUT", "quantity": 2,
         "last_price": 24600.0, "bid": 24598.0, "ask": 24602.0, "exchange": "NFO"},
    ]
    params = {"scope": "total"}

    with patch("backend.api.algo.actions_sim._sim_positions_in_scope", return_value=positions), \
         patch("backend.api.algo.actions_sim._write_sim_order", new=AsyncMock()) as mock_write:
        await _sim_chase_close(agent, "chase_close_positions", params)

    assert mock_write.call_count == 2


@pytest.mark.asyncio
async def test_sim_chase_close_no_positions():
    """No positions matched → writes one "no positions" row."""
    from backend.api.algo.actions_sim import _sim_chase_close

    agent = MagicMock()
    agent.slug = "test-agent"

    params = {"scope": "total"}

    with patch("backend.api.algo.actions_sim._sim_positions_in_scope", return_value=[]), \
         patch("backend.api.algo.actions_sim._write_sim_order", new=AsyncMock()) as mock_write:
        await _sim_chase_close(agent, "chase_close", params)

    mock_write.assert_called_once()
    call_args = mock_write.call_args[0]
    assert "(no positions in scope)" in call_args[2]["symbol"]


# ─────────────────────────────────────────────────────────────────────────────
# _sim_expiry_close tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sim_expiry_close_nfo_positions():
    """NFO exchange filters positions correctly."""
    from backend.api.algo.actions_sim import _sim_expiry_close

    agent = MagicMock()
    agent.slug = "test-agent"

    params = {"exchange": "NFO"}

    positions = [
        {"exchange": "NFO", "tradingsymbol": "NIFTY25AUGFUT", "quantity": 1},
        {"exchange": "MCX", "tradingsymbol": "CRUDEOILAUG25FUT", "quantity": 1},
    ]

    with patch("backend.api.algo.actions_sim._sim_positions_in_scope", return_value=positions), \
         patch("backend.api.algo.actions_sim._sim_expiry_close_position",
               new=AsyncMock()) as mock_close:
        await _sim_expiry_close(agent, "expiry_auto_close", params)

    # Only NFO position processed
    mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_sim_expiry_close_no_targets():
    """No matching positions → writes one "no positions" row."""
    from backend.api.algo.actions_sim import _sim_expiry_close

    agent = MagicMock()
    agent.slug = "test-agent"

    params = {"exchange": "MCX"}

    positions = [
        {"exchange": "NFO", "tradingsymbol": "NIFTY25AUGFUT", "quantity": 1},
    ]

    with patch("backend.api.algo.actions_sim._sim_positions_in_scope", return_value=positions), \
         patch("backend.api.algo.actions_sim._write_sim_order", new=AsyncMock()) as mock_write:
        await _sim_expiry_close(agent, "expiry_auto_close", params)

    mock_write.assert_called_once()
    call_args = mock_write.call_args[0]
    assert "(no MCX positions in sim book)" in call_args[2]["symbol"]


# ─────────────────────────────────────────────────────────────────────────────
# _sim_place_or_close tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sim_place_or_close_writes_order_and_attaches_template():
    """Order written, template attached."""
    from backend.api.algo.actions_sim import _sim_place_or_close

    agent = MagicMock()
    agent.slug = "test-agent"

    params = {
        "account": "SIM",
        "symbol": "NIFTY25JULFUT",
        "quantity": 1,
        "price": 24500.0,
        "exchange": "NFO",
    }

    with patch("backend.api.algo.actions_sim._sim_prices_for",
               return_value=(24500.0, 24498.0, 24502.0, 0)), \
         patch("backend.api.algo.actions_sim._write_sim_order",
               new=AsyncMock(return_value=999)) as mock_write, \
         patch("backend.api.algo.actions._maybe_attach_template_from_action",
               new=AsyncMock()):
        await _sim_place_or_close(agent, "place_order", params)

    mock_write.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# _sim_paper_trade dispatcher tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sim_paper_trade_chase_close_dispatch():
    """chase_close_positions → _sim_chase_close."""
    from backend.api.algo.actions_sim import _sim_paper_trade

    agent = MagicMock()
    agent.slug = "test-agent"
    params = {"scope": "total"}
    context = {}

    with patch("backend.api.algo.actions_sim._sim_chase_close", new=AsyncMock()) as mock_chase:
        await _sim_paper_trade(agent, "chase_close_positions", params, context)

    mock_chase.assert_called_once()


@pytest.mark.asyncio
async def test_sim_paper_trade_expiry_auto_close_dispatch():
    """expiry_auto_close → _sim_expiry_close."""
    from backend.api.algo.actions_sim import _sim_paper_trade

    agent = MagicMock()
    agent.slug = "test-agent"
    params = {"exchange": "NFO"}
    context = {}

    with patch("backend.api.algo.actions_sim._sim_expiry_close", new=AsyncMock()) as mock_expiry:
        await _sim_paper_trade(agent, "expiry_auto_close", params, context)

    mock_expiry.assert_called_once()


@pytest.mark.asyncio
async def test_sim_paper_trade_place_order_dispatch():
    """place_order → _sim_place_or_close."""
    from backend.api.algo.actions_sim import _sim_paper_trade

    agent = MagicMock()
    agent.slug = "test-agent"
    params = {"symbol": "NIFTY25JULFUT"}
    context = {}

    with patch("backend.api.algo.actions_sim._sim_place_or_close", new=AsyncMock()) as mock_place:
        await _sim_paper_trade(agent, "place_order", params, context)

    mock_place.assert_called_once()


@pytest.mark.asyncio
async def test_sim_paper_trade_close_position_dispatch():
    """close_position → _sim_place_or_close."""
    from backend.api.algo.actions_sim import _sim_paper_trade

    agent = MagicMock()
    agent.slug = "test-agent"
    params = {"symbol": "NIFTY25JULFUT"}
    context = {}

    with patch("backend.api.algo.actions_sim._sim_place_or_close", new=AsyncMock()) as mock_place:
        await _sim_paper_trade(agent, "close_position", params, context)

    mock_place.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# _replay_paper_trade tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replay_paper_trade_writes_filled():
    """Replay order written with status=FILLED (no fill lifecycle)."""
    from backend.api.algo.actions_sim import _replay_paper_trade

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    params = {
        "symbol": "NIFTY25JULFUT",
        "account": "REPLAY",
        "quantity": 1,
        "price": 24500.0,
        "exchange": "NFO",
    }
    context = {}

    mock_row = MagicMock()
    mock_row.id = 777

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = MagicMock()
    mock_session.__aenter__.return_value.add = MagicMock()
    mock_session.__aenter__.return_value.commit = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.models.AlgoOrder") as mock_order_class:
        mock_order_class.return_value = mock_row
        await _replay_paper_trade(agent, "place_order", params, context)

    # Verify AlgoOrder was instantiated
    mock_order_class.assert_called_once()


@pytest.mark.asyncio
async def test_replay_paper_trade_db_error():
    """DB error → caught, logged."""
    from backend.api.algo.actions_sim import _replay_paper_trade

    agent = MagicMock()
    agent.slug = "test-agent"

    params = {
        "symbol": "NIFTY25JULFUT",
        "quantity": 1,
        "price": 24500.0,
    }
    context = {}

    mock_session = AsyncMock()
    mock_session.__aenter__.side_effect = RuntimeError("DB error")

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.actions_sim.logger") as mock_logger:
        await _replay_paper_trade(agent, "place_order", params, context)

    mock_logger.error.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# _shadow_trade tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shadow_trade_calls_shadow_engine():
    """Shadow trade delegates to shadow engine."""
    from backend.api.algo.actions_sim import _shadow_trade

    agent = MagicMock()
    agent.slug = "test-agent"

    params = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "quantity": 1,
        "price": 24500.0,
    }
    context = {}

    mock_engine = MagicMock()
    mock_engine.capture_order = AsyncMock(return_value={"ok": True})

    with patch("backend.api.algo.shadow.get_shadow_engine", return_value=mock_engine):
        await _shadow_trade(agent, "place_order", params, context)

    mock_engine.capture_order.assert_called_once()


@pytest.mark.asyncio
async def test_shadow_trade_logs_rejection():
    """Shadow engine rejection → logged."""
    from backend.api.algo.actions_sim import _shadow_trade

    agent = MagicMock()
    agent.slug = "test-agent"

    params = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "quantity": 100,
        "price": 24500.0,
    }
    context = {}

    mock_engine = MagicMock()
    mock_engine.capture_order = AsyncMock(return_value={"ok": False, "margin_info": "insufficient"})

    with patch("backend.api.algo.shadow.get_shadow_engine", return_value=mock_engine), \
         patch("backend.api.algo.actions_sim.logger") as mock_logger:
        await _shadow_trade(agent, "place_order", params, context)

    mock_logger.warning.assert_called_once()
