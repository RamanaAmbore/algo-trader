"""Tests for KiteBroker adapter and Kite qty translation functions."""

import pytest
from unittest.mock import MagicMock, patch

from backend.brokers.adapters.kite import (
    KiteBroker,
    _kite_exc,
    to_kite_qty,
    from_kite_qty,
)
from backend.brokers.errors import (
    BrokerError,
    BrokerAuthError,
    BrokerNetworkError,
    BrokerOrderError,
    BrokerInputError,
)


class TestKiteExceptionMapping:
    """Test _kite_exc exception mapping."""

    def test_token_exception_maps_to_auth_error(self):
        """TokenException → BrokerAuthError."""
        mock_exc = MagicMock()
        mock_exc.__class__.__name__ = "TokenException"
        mock_exc.__str__ = MagicMock(return_value="token invalid")

        result = _kite_exc(mock_exc)
        assert isinstance(result, BrokerAuthError)
        assert result.broker == "zerodha_kite"
        assert result.code == "TokenException"

    def test_network_exception_maps_to_network_error(self):
        """NetworkException → BrokerNetworkError."""
        mock_exc = MagicMock()
        mock_exc.__class__.__name__ = "NetworkException"
        mock_exc.__str__ = MagicMock(return_value="connection timeout")

        result = _kite_exc(mock_exc)
        assert isinstance(result, BrokerNetworkError)
        assert result.code == "NetworkException"

    def test_order_exception_maps_to_order_error(self):
        """OrderException → BrokerOrderError."""
        mock_exc = MagicMock()
        mock_exc.__class__.__name__ = "OrderException"
        mock_exc.__str__ = MagicMock(return_value="order rejected")

        result = _kite_exc(mock_exc)
        assert isinstance(result, BrokerOrderError)
        assert result.code == "OrderException"

    def test_input_exception_maps_to_input_error(self):
        """InputException → BrokerInputError."""
        mock_exc = MagicMock()
        mock_exc.__class__.__name__ = "InputException"
        mock_exc.__str__ = MagicMock(return_value="invalid symbol")

        result = _kite_exc(mock_exc)
        assert isinstance(result, BrokerInputError)
        assert result.code == "InputException"

    def test_data_exception_maps_to_input_error(self):
        """DataException → BrokerInputError."""
        mock_exc = MagicMock()
        mock_exc.__class__.__name__ = "DataException"
        mock_exc.__str__ = MagicMock(return_value="invalid data")

        result = _kite_exc(mock_exc)
        assert isinstance(result, BrokerInputError)

    def test_general_exception_maps_to_broker_error(self):
        """GeneralException → BrokerError."""
        mock_exc = MagicMock()
        mock_exc.__class__.__name__ = "GeneralException"
        mock_exc.__str__ = MagicMock(return_value="unknown error")

        result = _kite_exc(mock_exc)
        assert isinstance(result, BrokerError)
        assert result.code == "GeneralException"

    def test_unmapped_exception_maps_to_broker_error(self):
        """Unmapped exception types → BrokerError."""
        mock_exc = MagicMock()
        mock_exc.__class__.__name__ = "UnknownException"
        mock_exc.__str__ = MagicMock(return_value="unknown")

        result = _kite_exc(mock_exc)
        assert isinstance(result, BrokerError)
        assert result.code == "UnknownException"


class TestKiteQtyTranslation:
    """Test to_kite_qty and from_kite_qty."""

    def test_from_kite_qty_mcx_multiplies(self):
        """from_kite_qty: MCX converts lots back to contracts."""
        # Kite reports MCX CRUDEOIL qty in lots
        assert from_kite_qty("MCX", 2, 100) == 200

    def test_from_kite_qty_nse_passthrough(self):
        """from_kite_qty: NSE passes through unchanged."""
        assert from_kite_qty("NSE", 50, 1) == 50
        assert from_kite_qty("NSE", 10, 50) == 10

    def test_from_kite_qty_nfo_passthrough(self):
        """from_kite_qty: NFO passes through unchanged."""
        assert from_kite_qty("NFO", 100, 50) == 100

    def test_from_kite_qty_nco_multiplies(self):
        """from_kite_qty: NCO also multiplies like MCX."""
        assert from_kite_qty("NCO", 1, 100) == 100

    def test_from_kite_qty_zero_lot_size(self):
        """from_kite_qty: zero lot_size passes through."""
        assert from_kite_qty("MCX", 100, 0) == 100

    def test_from_kite_qty_zero_qty(self):
        """from_kite_qty: zero qty returns zero."""
        assert from_kite_qty("MCX", 0, 100) == 0

    def test_to_kite_qty_mcx_divides(self):
        """to_kite_qty: MCX contracts convert to lots."""
        # 200 contracts / 100 lot_size = 2 lots
        assert to_kite_qty("MCX", 200, 100) == 2

    def test_to_kite_qty_nse_passthrough(self):
        """to_kite_qty: NSE unchanged."""
        assert to_kite_qty("NSE", 50, 1) == 50

    def test_to_kite_qty_nfo_passthrough(self):
        """to_kite_qty: NFO unchanged."""
        assert to_kite_qty("NFO", 1000, 50) == 1000

    def test_to_kite_qty_sub_lot_size_passthrough(self):
        """to_kite_qty: qty < lot_size passes through unchanged."""
        assert to_kite_qty("MCX", 50, 100) == 50

    def test_to_kite_qty_mcx_zero_lot_size_raises(self):
        """to_kite_qty: MCX with lot_size=0 raises ValueError."""
        with pytest.raises(ValueError, match="lot_size=0"):
            to_kite_qty("MCX", 100, 0)

    def test_to_kite_qty_mcx_lot_size_one_raises(self):
        """to_kite_qty: MCX with lot_size=1 raises ValueError."""
        with pytest.raises(ValueError, match="lot_size=1"):
            to_kite_qty("MCX", 100, 1)

    def test_to_kite_qty_nco_zero_lot_size_raises(self):
        """to_kite_qty: NCO with lot_size=0 raises ValueError."""
        with pytest.raises(ValueError, match="lot_size=0"):
            to_kite_qty("NCO", 100, 0)

    def test_to_kite_qty_nse_zero_lot_size_ok(self):
        """to_kite_qty: NSE with lot_size=0 is allowed (no-op)."""
        assert to_kite_qty("NSE", 50, 0) == 50

    def test_to_kite_qty_minimum_one_lot(self):
        """to_kite_qty: division rounds to minimum 1 lot."""
        # 100 contracts / 100 lot_size = 1 lot
        assert to_kite_qty("MCX", 100, 100) == 1

    def test_to_kite_qty_nco_divides(self):
        """to_kite_qty: NCO also divides."""
        assert to_kite_qty("NCO", 100, 100) == 1


class TestKiteBrokerInit:
    """Test KiteBroker initialization."""

    def test_kite_broker_init(self):
        """KiteBroker initializes with a mocked KiteConnection."""
        mock_conn = MagicMock()
        mock_conn.account = "ZG0790"

        broker = KiteBroker(mock_conn)
        assert broker._conn is mock_conn
        assert broker._last_req == {}
        assert broker._last_resp == {}

    def test_kite_broker_account_property(self):
        """KiteBroker.account returns connection account."""
        mock_conn = MagicMock()
        mock_conn.account = "ZG0790"

        broker = KiteBroker(mock_conn)
        assert broker.account == "ZG0790"

    def test_kite_broker_broker_id(self):
        """KiteBroker.broker_id returns 'zerodha_kite'."""
        mock_conn = MagicMock()
        broker = KiteBroker(mock_conn)
        assert broker.broker_id == "zerodha_kite"

    def test_kite_broker_last_request_debug(self):
        """KiteBroker.last_request_debug returns request+response."""
        mock_conn = MagicMock()
        broker = KiteBroker(mock_conn)
        broker._last_req = {"method": "POST"}
        broker._last_resp = {"status": 200}

        debug = broker.last_request_debug()
        assert debug["request"] == {"method": "POST"}
        assert debug["response"] == {"status": 200}


class TestKiteBrokerQtyTranslate:
    """Test KiteBroker.translate_qty and normalise_qty."""

    def test_kite_broker_translate_qty_mcx(self):
        """KiteBroker.translate_qty for MCX divides."""
        mock_conn = MagicMock()
        broker = KiteBroker(mock_conn)
        # to_kite_qty is called, which divides for MCX
        assert broker.translate_qty("MCX", 100, 100) == 1

    def test_kite_broker_translate_qty_nse(self):
        """KiteBroker.translate_qty for NSE passes through."""
        mock_conn = MagicMock()
        broker = KiteBroker(mock_conn)
        assert broker.translate_qty("NSE", 50, 1) == 50

    def test_kite_broker_normalise_qty_alias(self):
        """KiteBroker.normalise_qty calls translate_qty."""
        mock_conn = MagicMock()
        broker = KiteBroker(mock_conn)
        assert broker.normalise_qty("MCX", 100, 100) == broker.translate_qty("MCX", 100, 100)


class TestKiteBrokerPlaceOrder:
    """Test KiteBroker.place_order qty ceiling guards."""

    def test_place_order_mcx_under_ceiling(self):
        """place_order: MCX qty < 50 goes through."""
        mock_conn = MagicMock()
        mock_kite = MagicMock()
        mock_kite.place_order = MagicMock(return_value="order_123")
        mock_conn.get_kite_conn = MagicMock(return_value=mock_kite)

        broker = KiteBroker(mock_conn)
        result = broker.place_order(
            exchange="MCX",
            tradingsymbol="CRUDEOIL25MAR",
            quantity=10,
            transaction_type="BUY",
            order_type="MARKET",
            product="MIS",
        )
        assert result == "order_123"

    def test_place_order_mcx_exceeds_ceiling_new_order(self):
        """place_order: MCX qty > 50 raises for new orders."""
        mock_conn = MagicMock()
        broker = KiteBroker(mock_conn)

        with pytest.raises(ValueError, match="50-lot"):
            broker.place_order(
                exchange="MCX",
                tradingsymbol="CRUDEOIL25MAR",
                quantity=100,
                transaction_type="BUY",
                order_type="MARKET",
                product="MIS",
            )

    def test_place_order_mcx_exceeds_ceiling_close_allowed(self):
        """place_order: MCX qty > 50 allowed for close orders."""
        mock_conn = MagicMock()
        mock_kite = MagicMock()
        mock_kite.place_order = MagicMock(return_value="order_456")
        mock_conn.get_kite_conn = MagicMock(return_value=mock_kite)

        broker = KiteBroker(mock_conn)
        result = broker.place_order(
            intent="close",
            exchange="MCX",
            tradingsymbol="CRUDEOIL25MAR",
            quantity=100,
            transaction_type="SELL",
            order_type="MARKET",
            product="MIS",
        )
        assert result == "order_456"

    def test_place_order_nfo_under_ceiling(self):
        """place_order: NFO qty < 50000 goes through."""
        mock_conn = MagicMock()
        mock_kite = MagicMock()
        mock_kite.place_order = MagicMock(return_value="order_789")
        mock_conn.get_kite_conn = MagicMock(return_value=mock_kite)

        broker = KiteBroker(mock_conn)
        result = broker.place_order(
            exchange="NFO",
            tradingsymbol="NIFTY25MAR25000CE",
            quantity=10000,
            transaction_type="BUY",
            order_type="MARKET",
            product="MIS",
        )
        assert result == "order_789"

    def test_place_order_nfo_exceeds_ceiling_new_order(self):
        """place_order: NFO qty > 50000 raises for new orders."""
        mock_conn = MagicMock()
        broker = KiteBroker(mock_conn)

        with pytest.raises(ValueError, match="50000"):
            broker.place_order(
                exchange="NFO",
                tradingsymbol="NIFTY25MAR25000CE",
                quantity=100000,
                transaction_type="BUY",
                order_type="MARKET",
                product="MIS",
            )

    def test_place_order_nfo_exceeds_ceiling_close_allowed(self):
        """place_order: NFO qty > 50000 allowed for close orders."""
        mock_conn = MagicMock()
        mock_kite = MagicMock()
        mock_kite.place_order = MagicMock(return_value="order_999")
        mock_conn.get_kite_conn = MagicMock(return_value=mock_kite)

        broker = KiteBroker(mock_conn)
        result = broker.place_order(
            intent="close",
            exchange="NFO",
            tradingsymbol="NIFTY25MAR25000CE",
            quantity=100000,
            transaction_type="SELL",
            order_type="MARKET",
            product="MIS",
        )
        assert result == "order_999"
