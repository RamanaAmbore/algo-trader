"""Tests for the broker error hierarchy and base Broker class."""

import pytest

from backend.brokers.errors import (
    BrokerError,
    BrokerAuthError,
    BrokerRateLimitError,
    BrokerNetworkError,
    BrokerOrderError,
    BrokerInputError,
)
from backend.brokers.base import Broker


class TestBrokerErrorHierarchy:
    """Test error classes inherit correctly and store attributes."""

    def test_broker_error_base(self):
        """BrokerError with no args initializes correctly."""
        e = BrokerError()
        assert str(e) == ""
        assert e.broker is None
        assert e.code is None
        assert e.status is None
        assert isinstance(e, Exception)

    def test_broker_error_with_message(self):
        """BrokerError message stored correctly."""
        e = BrokerError("test message")
        assert str(e) == "test message"

    def test_broker_error_with_kwargs(self):
        """BrokerError stores broker, code, status kwargs."""
        e = BrokerError(
            "auth failed",
            broker="zerodha_kite",
            code="TokenException",
            status=401,
        )
        assert str(e) == "auth failed"
        assert e.broker == "zerodha_kite"
        assert e.code == "TokenException"
        assert e.status == 401

    def test_broker_auth_error_inheritance(self):
        """BrokerAuthError is a BrokerError."""
        e = BrokerAuthError("token expired", broker="kite", code="401")
        assert isinstance(e, BrokerError)
        assert isinstance(e, Exception)
        assert str(e) == "token expired"
        assert e.broker == "kite"
        assert e.code == "401"

    def test_broker_rate_limit_error(self):
        """BrokerRateLimitError stores attributes."""
        e = BrokerRateLimitError("rate limited", broker="kite", status=429)
        assert isinstance(e, BrokerError)
        assert e.status == 429

    def test_broker_network_error(self):
        """BrokerNetworkError stores attributes."""
        e = BrokerNetworkError("connection timeout", broker="kite", code="TIMEOUT")
        assert isinstance(e, BrokerError)
        assert e.code == "TIMEOUT"

    def test_broker_order_error(self):
        """BrokerOrderError stores attributes."""
        e = BrokerOrderError("order rejected", broker="kite", code="ORDER_INVALID")
        assert isinstance(e, BrokerError)
        assert e.code == "ORDER_INVALID"

    def test_broker_input_error(self):
        """BrokerInputError stores attributes."""
        e = BrokerInputError("invalid symbol", broker="kite", code="INVALID_SYMBOL")
        assert isinstance(e, BrokerError)
        assert e.code == "INVALID_SYMBOL"


class _TestBroker(Broker):
    """Concrete test implementation of Broker ABC."""

    def __init__(self, account: str = "TEST001"):
        super().__init__()
        self._account = account

    @property
    def account(self) -> str:
        return self._account

    @property
    def broker_id(self) -> str:
        return "test_broker"

    def profile(self) -> dict:
        return {}

    def holdings(self) -> list[dict]:
        return []

    def positions(self) -> dict:
        return {"net": [], "day": []}

    def margins(self, segment: str | None = None) -> dict:
        return {}

    def orders(self) -> list[dict]:
        return []

    def trades(self) -> list[dict]:
        return []

    def ltp(self, symbols: list[str]) -> dict:
        return {}

    def quote(self, symbols: list[str]) -> dict:
        return {}

    def instruments(self, exchange: str | None = None) -> list[dict]:
        return []

    def historical_data(self, instrument_token: int, from_date, to_date, interval: str = "day") -> list[dict]:
        return []

    def holidays(self, exchange: str) -> set[str]:
        return set()

    def basket_order_margins(self, orders: list[dict]) -> list[dict]:
        return []

    def place_order(self, *, intent: str | None = None, **kwargs) -> str:
        return "test_order_id"

    def modify_order(self, order_id: str, **kwargs) -> str:
        return order_id

    def cancel_order(self, order_id: str, **kwargs) -> str:
        return order_id


class TestBrokerBase:
    """Test Broker base class methods."""

    def test_broker_initialization(self):
        """Broker initializes _last_req and _last_resp dicts."""
        broker = _TestBroker()
        assert broker._last_req == {}
        assert broker._last_resp == {}

    def test_last_request_debug(self):
        """last_request_debug returns both request and response dicts."""
        broker = _TestBroker()
        broker._last_req = {"method": "GET", "path": "/test"}
        broker._last_resp = {"status": 200, "body": "ok"}

        debug = broker.last_request_debug()
        assert debug == {
            "request": {"method": "GET", "path": "/test"},
            "response": {"status": 200, "body": "ok"},
        }

    def test_last_request_debug_empty(self):
        """last_request_debug returns empty dicts by default."""
        broker = _TestBroker()
        debug = broker.last_request_debug()
        assert debug == {"request": {}, "response": {}}

    def test_last_request_debug_copies_dict(self):
        """last_request_debug returns copies, not references."""
        broker = _TestBroker()
        broker._last_req = {"key": "value"}

        debug1 = broker.last_request_debug()
        debug1["request"]["key"] = "modified"

        debug2 = broker.last_request_debug()
        assert debug2["request"]["key"] == "value"

    def test_order_status_matching_order(self):
        """order_status filters orders() by order_id."""
        broker = _TestBroker()
        # Mock orders() to return a list
        test_orders = [
            {"order_id": "123", "status": "COMPLETE", "filled_quantity": 10},
            {"order_id": "456", "status": "PENDING", "filled_quantity": 0},
        ]
        broker.orders = lambda: test_orders

        result = broker.order_status("123")
        assert result == {"order_id": "123", "status": "COMPLETE", "filled_quantity": 10}

    def test_order_status_non_matching(self):
        """order_status returns empty dict when order_id not found."""
        broker = _TestBroker()
        broker.orders = lambda: [
            {"order_id": "123", "status": "COMPLETE"},
        ]

        result = broker.order_status("999")
        assert result == {}

    def test_order_status_string_coercion(self):
        """order_status coerces both order_id and dict order_id to strings."""
        broker = _TestBroker()
        broker.orders = lambda: [
            {"order_id": 123, "status": "COMPLETE"},
        ]

        result = broker.order_status("123")
        assert result == {"order_id": 123, "status": "COMPLETE"}

    def test_translate_qty_default_noop(self):
        """translate_qty default returns raw_qty unchanged."""
        broker = _TestBroker()
        assert broker.translate_qty("NSE", 50, 1) == 50
        assert broker.translate_qty("MCX", 100, 100) == 100
        assert broker.translate_qty("NFO", 1000, 50) == 1000

    def test_normalise_qty_alias(self):
        """normalise_qty aliases translate_qty."""
        broker = _TestBroker()
        assert broker.normalise_qty("NSE", 50, 1) == broker.translate_qty("NSE", 50, 1)
        assert broker.normalise_qty("MCX", 100, 100) == broker.translate_qty("MCX", 100, 100)

    def test_market_status_default_none(self):
        """market_status default returns None."""
        broker = _TestBroker()
        assert broker.market_status("NSE") is None
        assert broker.market_status("MCX") is None

    def test_broker_abstract_methods(self):
        """Broker ABC prevents instantiation without implementing abstract methods."""
        # _TestBroker implements all abstract methods, so it should be instantiable.
        broker = _TestBroker()
        assert broker is not None
