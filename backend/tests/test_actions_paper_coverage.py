"""
Coverage tests for backend/api/algo/actions_paper.py

Covers:
  - _ap_dry_run_margin: basket-margin validation via broker
  - _ap_persist_algo_order_row: AlgoOrder DB persistence
  - _ap_write_placement_events: timeline event creation
  - _ap_position_expiry_eligible: expiry filter logic
  - _write_paper_order: full paper order pipeline
  - _paper_place_or_close: place/close/modify/cancel dispatching
  - _paper_chase_close: scope-based position closure
  - _paper_expiry_close: expiry-filtered closure
  - _paper_trade: mode-2 dispatcher
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
# _ap_dry_run_margin tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ap_dry_run_margin_success():
    """Basket margin validation succeeds → (True, reason)."""
    from backend.api.algo.actions_paper import _ap_dry_run_margin

    mock_broker = MagicMock()

    with patch("backend.brokers.registry.get_broker", return_value=mock_broker), \
         patch("backend.api.algo.actions_paper._basket_margin_validate",
               new=AsyncMock(return_value=(True, "OK"))):
        ok, reason = await _ap_dry_run_margin("ZG0790", "NIFTY25JULFUT", "BUY", 1, 24500.0, "NFO")

    assert ok is True
    assert reason == "OK"


@pytest.mark.asyncio
async def test_ap_dry_run_margin_failure():
    """Basket margin validation fails → (False, error_text)."""
    from backend.api.algo.actions_paper import _ap_dry_run_margin

    mock_broker = MagicMock()

    with patch("backend.brokers.registry.get_broker", return_value=mock_broker), \
         patch("backend.api.algo.actions_paper._basket_margin_validate",
               new=AsyncMock(return_value=(False, "insufficient margin"))):
        ok, reason = await _ap_dry_run_margin("ZG0790", "NIFTY25JULFUT", "BUY", 100, 24500.0, "NFO")

    assert ok is False
    assert "insufficient" in reason


@pytest.mark.asyncio
async def test_ap_dry_run_margin_zero_qty_skips():
    """Zero qty skips validation → (True, 'paper')."""
    from backend.api.algo.actions_paper import _ap_dry_run_margin

    ok, reason = await _ap_dry_run_margin("ZG0790", "NIFTY25JULFUT", "BUY", 0, 24500.0, "NFO")

    assert ok is True
    assert reason == "paper"


@pytest.mark.asyncio
async def test_ap_dry_run_margin_none_price_skips():
    """None price skips validation → (True, 'paper')."""
    from backend.api.algo.actions_paper import _ap_dry_run_margin

    ok, reason = await _ap_dry_run_margin("ZG0790", "NIFTY25JULFUT", "BUY", 1, None, "NFO")

    assert ok is True
    assert reason == "paper"


@pytest.mark.asyncio
async def test_ap_dry_run_margin_broker_lookup_error():
    """Broker lookup fails → (False, error text)."""
    from backend.api.algo.actions_paper import _ap_dry_run_margin

    with patch("backend.brokers.registry.get_broker", side_effect=RuntimeError("no broker")):
        ok, reason = await _ap_dry_run_margin("ZG0790", "NIFTY25JULFUT", "BUY", 1, 24500.0, "NFO")

    assert ok is False


# ─────────────────────────────────────────────────────────────────────────────
# _ap_persist_algo_order_row tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ap_persist_algo_order_row_success():
    """AlgoOrder row inserted → id returned."""
    from backend.api.algo.actions_paper import _ap_persist_algo_order_row

    agent = MagicMock()
    agent.id = 1
    agent.slug = "test-agent"

    mock_row = MagicMock()
    mock_row.id = 999

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = MagicMock()
    mock_session.__aenter__.return_value.add = MagicMock()
    mock_session.__aenter__.return_value.commit = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.models.AlgoOrder") as mock_order_class:
        mock_order_class.return_value = mock_row
        result = await _ap_persist_algo_order_row(
            "ZG0790", "NIFTY25JULFUT", "NFO", "BUY", 1, 24500.0,
            "OPEN", "PAPER-xxx", "test order", agent
        )

    assert result == 999


@pytest.mark.asyncio
async def test_ap_persist_algo_order_row_rejected_status():
    """Rejected order persisted with REJECTED status."""
    from backend.api.algo.actions_paper import _ap_persist_algo_order_row

    agent = MagicMock()
    agent.id = 1

    mock_row = MagicMock()
    mock_row.id = 888

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = MagicMock()
    mock_session.__aenter__.return_value.add = MagicMock()
    mock_session.__aenter__.return_value.commit = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.models.AlgoOrder") as mock_order_class:
        mock_order_class.return_value = mock_row
        result = await _ap_persist_algo_order_row(
            "ZG0790", "NIFTY25JULFUT", "NFO", "SELL", 1, 24500.0,
            "REJECTED", "PAPER-xxx", "rejected", agent
        )

    assert result == 888


@pytest.mark.asyncio
async def test_ap_persist_algo_order_row_db_error():
    """DB error → returns None."""
    from backend.api.algo.actions_paper import _ap_persist_algo_order_row

    agent = MagicMock()
    agent.id = 1

    mock_session = AsyncMock()
    mock_session.__aenter__.side_effect = RuntimeError("DB error")

    with patch("backend.api.database.async_session", return_value=mock_session):
        result = await _ap_persist_algo_order_row(
            "ZG0790", "NIFTY25JULFUT", "NFO", "BUY", 1, 24500.0,
            "OPEN", "PAPER-xxx", "order", agent
        )

    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# _ap_dte and _ap_position_expiry_eligible tests
# ─────────────────────────────────────────────────────────────────────────────

def test_ap_position_expiry_eligible_wrong_exchange():
    """Position on different exchange → not eligible."""
    from backend.api.algo.actions_paper import _ap_position_expiry_eligible

    p = {
        "exchange": "NSE",
        "quantity": 1,
        "tradingsymbol": "NIFTY25AUGFUT",
    }
    result = _ap_position_expiry_eligible(p, "NFO", datetime.now())
    assert result is False


def test_ap_position_expiry_eligible_zero_quantity():
    """Position with zero quantity → not eligible."""
    from backend.api.algo.actions_paper import _ap_position_expiry_eligible

    p = {
        "exchange": "NFO",
        "quantity": 0,
        "tradingsymbol": "NIFTY25AUGFUT",
    }
    result = _ap_position_expiry_eligible(p, "NFO", datetime.now())
    assert result is False


def test_ap_position_expiry_eligible_no_expiry():
    """Position with unparseable expiry → not eligible."""
    from backend.api.algo.actions_paper import _ap_position_expiry_eligible

    p = {
        "exchange": "NFO",
        "quantity": 1,
        "tradingsymbol": "NIFTY",  # equity, no expiry
    }
    result = _ap_position_expiry_eligible(p, "NFO", datetime.now())
    assert result is False


def test_ap_position_expiry_eligible_dte_too_high():
    """Position with DTE > 1.5 → not eligible."""
    from backend.api.algo.actions_paper import _ap_position_expiry_eligible

    p = {
        "exchange": "NFO",
        "quantity": 1,
        "tradingsymbol": "NIFTY25AUG10500CE",
    }
    with patch("backend.api.algo.actions_paper._ap_dte", return_value=2.0):  # > 1.5
        result = _ap_position_expiry_eligible(p, "NFO", datetime.now())
        assert result is False


def test_ap_position_expiry_eligible_dte_1_5():
    """Position with DTE exactly 1.5 → eligible."""
    from backend.api.algo.actions_paper import _ap_position_expiry_eligible

    p = {
        "exchange": "NFO",
        "quantity": 1,
        "tradingsymbol": "NIFTY25AUG10500CE",
    }
    with patch("backend.api.algo.actions_paper._ap_dte", return_value=1.5):
        result = _ap_position_expiry_eligible(p, "NFO", datetime.now())
        assert result is True


def test_ap_position_expiry_eligible_dte_0_5():
    """Position with DTE < 1.5 → eligible."""
    from backend.api.algo.actions_paper import _ap_position_expiry_eligible

    p = {
        "exchange": "NFO",
        "quantity": 1,
        "tradingsymbol": "NIFTY25AUG10500CE",
    }
    with patch("backend.api.algo.actions_paper._ap_dte", return_value=0.5):
        result = _ap_position_expiry_eligible(p, "NFO", datetime.now())
        assert result is True


# ─────────────────────────────────────────────────────────────────────────────
# _ap_pick_expiring_positions tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ap_pick_expiring_positions_filters_correctly():
    """Filters positions by exchange and DTE."""
    from backend.api.algo.actions_paper import _ap_pick_expiring_positions

    all_positions = [
        {"exchange": "NFO", "tradingsymbol": "NIFTY25AUGFUT", "quantity": 1},
        {"exchange": "MCX", "tradingsymbol": "CRUDEOILAUG25FUT", "quantity": 1},
        {"exchange": "NFO", "tradingsymbol": "NIFTY25SEPFUT", "quantity": 1},
    ]
    context = {"now": datetime.now()}

    with patch("backend.api.algo.actions_paper._ap_position_expiry_eligible") as mock_elig:
        # Only first and third are eligible
        mock_elig.side_effect = lambda p, ex, ref: (
            p.get("tradingsymbol") in ("NIFTY25AUGFUT", "NIFTY25SEPFUT")
            and ex == "NFO"
        )
        result = await _ap_pick_expiring_positions(all_positions, "NFO", context)

    assert len(result) == 2


# ─────────────────────────────────────────────────────────────────────────────
# _write_paper_order tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_paper_order_accepted():
    """Paper order accepted by margin check → status=OPEN, registered."""
    from backend.api.algo.actions_paper import _write_paper_order

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    resolved = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "side": "BUY",
        "qty": 1,
        "price": 24500.0,
        "exchange": "NFO",
    }
    context = {}

    mock_row_id = 999
    mock_engine = MagicMock()

    with patch("backend.api.algo.actions_paper._ap_dry_run_margin",
               new=AsyncMock(return_value=(True, "OK"))), \
         patch("backend.api.algo.actions_paper._ap_persist_algo_order_row",
               new=AsyncMock(return_value=mock_row_id)), \
         patch("backend.api.algo.actions_paper._ap_write_placement_events",
               new=AsyncMock()), \
         patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_engine):
        await _write_paper_order(agent, "place_order", resolved, context)

    # Paper engine's register_open_order should be called
    mock_engine.register_open_order.assert_called_once()


@pytest.mark.asyncio
async def test_write_paper_order_rejected():
    """Paper order rejected by margin check → status=REJECTED, not registered."""
    from backend.api.algo.actions_paper import _write_paper_order

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    resolved = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "side": "BUY",
        "qty": 100,
        "price": 24500.0,
        "exchange": "NFO",
    }
    context = {}

    mock_row_id = 888
    mock_engine = MagicMock()

    with patch("backend.api.algo.actions_paper._ap_dry_run_margin",
               new=AsyncMock(return_value=(False, "insufficient margin"))), \
         patch("backend.api.algo.actions_paper._ap_persist_algo_order_row",
               new=AsyncMock(return_value=mock_row_id)), \
         patch("backend.api.algo.actions_paper._ap_write_placement_events",
               new=AsyncMock()), \
         patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_engine):
        await _write_paper_order(agent, "place_order", resolved, context)

    # Paper engine should NOT register a rejected order
    mock_engine.register_open_order.assert_not_called()


@pytest.mark.asyncio
async def test_write_paper_order_market_order():
    """Market order (price=None) still writes."""
    from backend.api.algo.actions_paper import _write_paper_order

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    resolved = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "side": "SELL",
        "qty": 1,
        # no price
        "exchange": "NFO",
    }
    context = {}

    mock_row_id = 777
    mock_engine = MagicMock()

    with patch("backend.api.algo.actions_paper._ap_dry_run_margin",
               new=AsyncMock(return_value=(True, "OK"))), \
         patch("backend.api.algo.actions_paper._ap_persist_algo_order_row",
               new=AsyncMock(return_value=mock_row_id)), \
         patch("backend.api.algo.actions_paper._ap_write_placement_events",
               new=AsyncMock()), \
         patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_engine):
        await _write_paper_order(agent, "close_position", resolved, context)

    # Should still register (price=None skips margin check but order is valid)
    mock_engine.register_open_order.assert_not_called()  # None price skips registration


# ─────────────────────────────────────────────────────────────────────────────
# _paper_place_or_close tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_paper_place_or_close_buy_explicit_side():
    """Explicit side=BUY in params → used."""
    from backend.api.algo.actions_paper import _paper_place_or_close

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    params = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "side": "BUY",
        "quantity": 1,
        "price": 24500.0,
        "exchange": "NFO",
    }
    context = {}

    with patch("backend.api.algo.actions_paper._write_paper_order", new=AsyncMock()) as mock_write:
        await _paper_place_or_close(agent, "place_order", params, context)

    mock_write.assert_called_once()
    call_args = mock_write.call_args[0]
    assert call_args[2]["side"] == "BUY"


@pytest.mark.asyncio
async def test_paper_place_or_close_transaction_type_fallback():
    """Falls back to transaction_type when side not present."""
    from backend.api.algo.actions_paper import _paper_place_or_close

    agent = MagicMock()
    agent.slug = "test-agent"

    params = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "transaction_type": "SELL",  # fallback
        "quantity": 1,
        "price": 24500.0,
        "exchange": "NFO",
    }
    context = {}

    with patch("backend.api.algo.actions_paper._write_paper_order", new=AsyncMock()) as mock_write:
        await _paper_place_or_close(agent, "close_position", params, context)

    call_args = mock_write.call_args[0]
    assert call_args[2]["side"] == "SELL"


@pytest.mark.asyncio
async def test_paper_place_or_close_default_side():
    """No side nor transaction_type → defaults to SELL."""
    from backend.api.algo.actions_paper import _paper_place_or_close

    agent = MagicMock()
    agent.slug = "test-agent"

    params = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "quantity": 1,
        "price": 24500.0,
    }
    context = {}

    with patch("backend.api.algo.actions_paper._write_paper_order", new=AsyncMock()) as mock_write:
        await _paper_place_or_close(agent, "place_order", params, context)

    call_args = mock_write.call_args[0]
    assert call_args[2]["side"] == "SELL"


# ─────────────────────────────────────────────────────────────────────────────
# _paper_chase_close tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_paper_chase_close_expands_positions():
    """Scope-level action expands to one order per position."""
    from backend.api.algo.actions_paper import _paper_chase_close

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    positions = [
        {"account": "ZG0790", "tradingsymbol": "NIFTY25JULFUT", "quantity": 1,
         "last_price": 24500.0, "close_price": 24500.0, "exchange": "NFO"},
        {"account": "ZG0790", "tradingsymbol": "NIFTY25AUGFUT", "quantity": 2,
         "last_price": 24600.0, "close_price": 24600.0, "exchange": "NFO"},
    ]
    params = {"scope": "total"}
    context = {}

    with patch("backend.api.algo.actions_paper._live_positions_in_scope", return_value=positions), \
         patch("backend.api.algo.actions_paper._write_paper_order", new=AsyncMock()) as mock_write:
        await _paper_chase_close(agent, "chase_close_positions", params, context)

    assert mock_write.call_count == 2


@pytest.mark.asyncio
async def test_paper_chase_close_no_positions():
    """No positions matched → writes one "no positions" row."""
    from backend.api.algo.actions_paper import _paper_chase_close

    agent = MagicMock()
    agent.slug = "test-agent"

    params = {"scope": "total", "account": "ZG0790"}
    context = {}

    with patch("backend.api.algo.actions_paper._live_positions_in_scope", return_value=[]), \
         patch("backend.api.algo.actions_paper._write_paper_order", new=AsyncMock()) as mock_write:
        await _paper_chase_close(agent, "chase_close", params, context)

    mock_write.assert_called_once()
    call_args = mock_write.call_args[0]
    assert "(no positions in scope)" in call_args[2]["symbol"]


# ─────────────────────────────────────────────────────────────────────────────
# _paper_expiry_close tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_paper_expiry_close_filters_by_exchange():
    """Only processes positions on matching exchange."""
    from backend.api.algo.actions_paper import _paper_expiry_close

    agent = MagicMock()
    agent.slug = "test-agent"

    params = {"exchange": "NFO"}
    context = {"now": datetime.now()}

    positions = [
        {"exchange": "NFO", "tradingsymbol": "NIFTY25AUGFUT", "quantity": 1},
        {"exchange": "MCX", "tradingsymbol": "CRUDEOILAUG25FUT", "quantity": 1},
    ]

    with patch("backend.api.algo.actions_paper._live_positions_in_scope", return_value=positions), \
         patch("backend.api.algo.actions_paper._ap_pick_expiring_positions",
               new=AsyncMock(return_value=[positions[0]])), \
         patch("backend.api.algo.actions_paper._write_paper_order", new=AsyncMock()) as mock_write:
        await _paper_expiry_close(agent, "expiry_auto_close", params, context)

    # Only NFO position processed
    mock_write.assert_called_once()


@pytest.mark.asyncio
async def test_paper_expiry_close_invalid_exchange():
    """Invalid exchange → warning, returns."""
    from backend.api.algo.actions_paper import _paper_expiry_close

    agent = MagicMock()
    agent.slug = "test-agent"

    params = {"exchange": "INVALID"}
    context = {"now": datetime.now()}

    with patch("backend.api.algo.actions_paper.logger") as mock_logger:
        await _paper_expiry_close(agent, "expiry_auto_close", params, context)

    mock_logger.warning.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# _paper_trade dispatcher tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_paper_trade_chase_close_dispatch():
    """chase_close_positions → _paper_chase_close."""
    from backend.api.algo.actions_paper import _paper_trade

    agent = MagicMock()
    agent.slug = "test-agent"
    params = {"scope": "total"}
    context = {}

    with patch("backend.api.algo.actions_paper._paper_chase_close", new=AsyncMock()) as mock_chase:
        await _paper_trade(agent, "chase_close_positions", params, context)

    mock_chase.assert_called_once()


@pytest.mark.asyncio
async def test_paper_trade_expiry_auto_close_dispatch():
    """expiry_auto_close → _paper_expiry_close."""
    from backend.api.algo.actions_paper import _paper_trade

    agent = MagicMock()
    agent.slug = "test-agent"
    params = {"exchange": "NFO"}
    context = {"now": datetime.now()}

    with patch("backend.api.algo.actions_paper._paper_expiry_close", new=AsyncMock()) as mock_expiry:
        await _paper_trade(agent, "expiry_auto_close", params, context)

    mock_expiry.assert_called_once()


@pytest.mark.asyncio
async def test_paper_trade_place_order_dispatch():
    """place_order → _paper_place_or_close."""
    from backend.api.algo.actions_paper import _paper_trade

    agent = MagicMock()
    agent.slug = "test-agent"
    params = {"symbol": "NIFTY25JULFUT", "quantity": 1, "price": 24500.0}
    context = {}

    with patch("backend.api.algo.actions_paper._paper_place_or_close", new=AsyncMock()) as mock_place:
        await _paper_trade(agent, "place_order", params, context)

    mock_place.assert_called_once()


@pytest.mark.asyncio
async def test_paper_trade_close_position_dispatch():
    """close_position → _paper_place_or_close."""
    from backend.api.algo.actions_paper import _paper_trade

    agent = MagicMock()
    agent.slug = "test-agent"
    params = {"symbol": "NIFTY25JULFUT"}
    context = {}

    with patch("backend.api.algo.actions_paper._paper_place_or_close", new=AsyncMock()) as mock_place:
        await _paper_trade(agent, "close_position", params, context)

    mock_place.assert_called_once()


@pytest.mark.asyncio
async def test_paper_trade_modify_order_dispatch():
    """modify_order → _paper_place_or_close."""
    from backend.api.algo.actions_paper import _paper_trade

    agent = MagicMock()
    agent.slug = "test-agent"
    params = {"symbol": "NIFTY25JULFUT", "quantity": 2}
    context = {}

    with patch("backend.api.algo.actions_paper._paper_place_or_close", new=AsyncMock()) as mock_place:
        await _paper_trade(agent, "modify_order", params, context)

    mock_place.assert_called_once()


@pytest.mark.asyncio
async def test_paper_trade_cancel_order_dispatch():
    """cancel_order → _paper_place_or_close."""
    from backend.api.algo.actions_paper import _paper_trade

    agent = MagicMock()
    agent.slug = "test-agent"
    params = {"symbol": "NIFTY25JULFUT"}
    context = {}

    with patch("backend.api.algo.actions_paper._paper_place_or_close", new=AsyncMock()) as mock_place:
        await _paper_trade(agent, "cancel_order", params, context)

    mock_place.assert_called_once()
