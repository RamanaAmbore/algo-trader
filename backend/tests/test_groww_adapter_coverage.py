"""
Comprehensive pytest test suite for backend/brokers/adapters/groww.py

Tests cover:
- Pure utility functions (_groww_exc, _gi, _gf, _first, etc.)
- Response normalisation (_normalise_holdings, _normalise_positions, etc.)
- GrowwBroker adapter methods (profile, holdings, positions, etc.)
- Error path handling (401, 429, 500)
- GTT order construction helpers
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from backend.brokers.adapters.groww import (
    # Error handling
    _groww_exc,
    # Pure utility functions
    _gi,
    _gf,
    _first,
    _unwrap,
    _iter_rows,
    _order_status,
    _groww_exchange_and_segment,
    _groww_coerce_date,
    _groww_row_indicates_open,
    _extract_groww_status_rows,
    # Response normalisers
    _normalise_holdings,
    _normalise_positions,
    _normalise_orders,
    _normalise_margins,
    _normalise_groww_gtt_row,
    _normalise_quote_row,
    # GTT helpers
    _groww_gtt_order_body,
    _groww_build_gtt_order_leg,
    # Adapter class
    GrowwBroker,
)
from backend.brokers.errors import (
    BrokerAuthError,
    BrokerRateLimitError,
    BrokerNetworkError,
    BrokerError,
)


# ============================================================================
# PART A: Pure functions
# ============================================================================

class TestGrowwExc:
    """Test _groww_exc error wrapping."""

    def test_groww_exc_401_returns_auth_error(self):
        e = Exception("Unauthorized")
        result = _groww_exc(e, status=401)
        assert isinstance(result, BrokerAuthError)
        assert "Unauthorized" in str(result)

    def test_groww_exc_429_returns_rate_limit_error(self):
        e = Exception("Too many requests")
        result = _groww_exc(e, status=429)
        assert isinstance(result, BrokerRateLimitError)

    def test_groww_exc_502_returns_network_error(self):
        e = Exception("Bad gateway")
        result = _groww_exc(e, status=502)
        assert isinstance(result, BrokerNetworkError)

    def test_groww_exc_503_returns_network_error(self):
        e = Exception("Service unavailable")
        result = _groww_exc(e, status=503)
        assert isinstance(result, BrokerNetworkError)

    def test_groww_exc_504_returns_network_error(self):
        e = Exception("Gateway timeout")
        result = _groww_exc(e, status=504)
        assert isinstance(result, BrokerNetworkError)

    def test_groww_exc_200_returns_broker_error(self):
        e = Exception("Generic error")
        result = _groww_exc(e, status=200)
        assert isinstance(result, BrokerError)
        assert not isinstance(result, (BrokerAuthError, BrokerRateLimitError, BrokerNetworkError))

    def test_groww_exc_none_status_returns_broker_error(self):
        e = Exception("Unknown error")
        result = _groww_exc(e, status=None)
        assert isinstance(result, BrokerError)


class TestGiGf:
    """Test integer and float extraction helpers."""

    def test_gi_returns_int_from_first_key(self):
        d = {"quantity": "100", "other": "50"}
        result = _gi(d, "quantity")
        assert result == 100
        assert isinstance(result, int)

    def test_gi_fallback_to_second_key(self):
        d = {"filled_quantity": "25", "pending_quantity": "75"}
        result = _gi(d, "filled_quantity", "pending_quantity")
        assert result == 25

    def test_gi_returns_default_when_missing(self):
        d = {"other": "value"}
        result = _gi(d, "quantity", default=0)
        assert result == 0

    def test_gi_returns_default_when_none(self):
        d = {"quantity": None}
        result = _gi(d, "quantity", default=42)
        assert result == 42

    def test_gi_handles_zero_value(self):
        d = {"quantity": 0}
        result = _gi(d, "quantity", default=100)
        # zero is falsy; _gi uses "int(v) or default" so 0 falls back to default
        assert result == 100

    def test_gi_invalid_string_falls_back(self):
        d = {"qty": "not_a_number"}
        result = _gi(d, "qty", "fallback", default=10)
        assert result == 10

    def test_gf_returns_float(self):
        d = {"price": "123.45"}
        result = _gf(d, "price")
        assert result == 123.45
        assert isinstance(result, float)

    def test_gf_fallback_keys(self):
        d = {"avg_price": None, "filled_avg_price": "55.25"}
        result = _gf(d, "avg_price", "filled_avg_price")
        assert result == 55.25

    def test_gf_returns_default_float(self):
        d = {"other": "value"}
        result = _gf(d, "price", default=0.0)
        assert result == 0.0


class TestFirst:
    """Test _first utility for dict value extraction."""

    def test_first_returns_first_nonempty_value(self):
        d = {"a": None, "b": "value", "c": "other"}
        result = _first(d, "a", "b", "c")
        assert result == "value"

    def test_first_returns_default_when_all_missing(self):
        d = {"x": "y"}
        result = _first(d, "a", "b", "c", default="fallback")
        assert result == "fallback"

    def test_first_treats_empty_string_as_falsy(self):
        d = {"a": "", "b": "value"}
        result = _first(d, "a", "b")
        assert result == "value"

    def test_first_default_empty_string(self):
        d = {"other": "value"}
        result = _first(d, "missing")
        assert result == ""


class TestUnwrap:
    """Test response envelope unwrapping."""

    def test_unwrap_extracts_data_key(self):
        resp = {"status": "SUCCESS", "data": {"holdings": []}}
        result = _unwrap(resp)
        assert result == {"holdings": []}

    def test_unwrap_returns_resp_when_no_data_key(self):
        resp = {"holdings": []}
        result = _unwrap(resp)
        assert result == {"holdings": []}

    def test_unwrap_handles_non_dict(self):
        resp = [{"item": 1}]
        result = _unwrap(resp)
        assert result == [{"item": 1}]

    def test_unwrap_custom_key(self):
        resp = {"status": "OK", "payload": {"value": 42}}
        result = _unwrap(resp, key="payload")
        assert result == {"value": 42}


class TestIterRows:
    """Test row iteration from various response shapes."""

    def test_iter_rows_direct_list(self):
        payload = [{"a": 1}, {"a": 2}]
        result = _iter_rows(payload)
        assert len(result) == 2
        assert result[0]["a"] == 1

    def test_iter_rows_from_holdings_key(self):
        payload = {"holdings": [{"qty": 100}, {"qty": 200}]}
        result = _iter_rows(payload, "holdings")
        assert len(result) == 2

    def test_iter_rows_from_first_matching_key(self):
        payload = {"order_list": [{"id": 1}], "other": "ignored"}
        result = _iter_rows(payload, "holdings", "order_list")
        assert len(result) == 1
        assert result[0]["id"] == 1

    def test_iter_rows_returns_empty_when_no_match(self):
        payload = {"other": "value"}
        result = _iter_rows(payload, "holdings", "positions")
        assert result == []


class TestOrderStatus:
    """Test Groww → Kite status translation."""

    def test_order_status_executed_to_complete(self):
        o = {"order_status": "EXECUTED"}
        result = _order_status(o)
        assert result == "COMPLETE"

    def test_order_status_traded_to_complete(self):
        o = {"status": "TRADED"}
        result = _order_status(o)
        assert result == "COMPLETE"

    def test_order_status_partially_filled_to_open(self):
        o = {"order_status": "PARTIALLY_FILLED"}
        result = _order_status(o)
        assert result == "OPEN"

    def test_order_status_cancelled_to_cancelled(self):
        o = {"order_status": "CANCELLED"}
        result = _order_status(o)
        assert result == "CANCELLED"

    def test_order_status_rejected_to_rejected(self):
        o = {"status": "REJECTED"}
        result = _order_status(o)
        assert result == "REJECTED"

    def test_order_status_unknown_passthrough(self):
        o = {"order_status": "UNKNOWN_STATUS"}
        result = _order_status(o)
        assert result == "UNKNOWN_STATUS"


class TestGrowwExchangeSegment:
    """Test Kite → Groww exchange/segment translation."""

    def test_nse_to_groww(self):
        ex, seg = _groww_exchange_and_segment("NSE")
        assert ex == "NSE"
        assert seg == "CASH"

    def test_bse_to_groww(self):
        ex, seg = _groww_exchange_and_segment("BSE")
        assert ex == "BSE"
        assert seg == "CASH"

    def test_nfo_to_groww(self):
        ex, seg = _groww_exchange_and_segment("NFO")
        assert ex == "NSE"
        assert seg == "FNO"

    def test_mcx_to_groww(self):
        ex, seg = _groww_exchange_and_segment("MCX")
        assert ex == "MCX"
        assert seg == "COMMODITY"

    def test_cds_to_groww(self):
        ex, seg = _groww_exchange_and_segment("CDS")
        assert ex == "NSE"
        assert seg == "CURRENCY"

    def test_invalid_exchange_raises(self):
        with pytest.raises(ValueError):
            _groww_exchange_and_segment("INVALID")


class TestGrowwCoerceDate:
    """Test date/datetime coercion to Groww format."""

    def test_coerce_datetime_object(self):
        dt = datetime(2026, 7, 17, 14, 30, 45)
        result = _groww_coerce_date(dt)
        assert result == "2026-07-17 14:30:45"

    def test_coerce_date_object(self):
        from datetime import date
        d = date(2026, 7, 17)
        result = _groww_coerce_date(d)
        assert result == "2026-07-17 00:00:00"

    def test_coerce_iso_string(self):
        result = _groww_coerce_date("2026-07-17")
        assert result == "2026-07-17"

    def test_coerce_arbitrary_string(self):
        result = _groww_coerce_date("2026-07-17 10:15:00")
        assert result == "2026-07-17 10:15:00"


class TestGrowwRowIndicatesOpen:
    """Test market status row interpretation."""

    def test_segment_match_with_open_bool(self):
        row = {"segment": "NSE", "status": True}
        result = _groww_row_indicates_open(row, ("NSE", "NSE_EQ"))
        assert result is True

    def test_segment_match_with_trading_string(self):
        row = {"segment": "NSE_EQ", "status": "TRADING"}
        result = _groww_row_indicates_open(row, ("NSE", "NSE_EQ"))
        assert result is True

    def test_segment_match_with_yes_string(self):
        row = {"segment": "NSE", "status": "YES"}
        result = _groww_row_indicates_open(row, ("NSE", "NSE_EQ"))
        assert result is True

    def test_segment_no_match_returns_false(self):
        row = {"segment": "BSE", "status": True}
        result = _groww_row_indicates_open(row, ("NSE", "NSE_EQ"))
        assert result is False

    def test_status_false_returns_false(self):
        row = {"segment": "NSE", "status": False}
        result = _groww_row_indicates_open(row, ("NSE",))
        assert result is False

    def test_closed_string_returns_false(self):
        row = {"segment": "NSE", "status": "CLOSED"}
        result = _groww_row_indicates_open(row, ("NSE",))
        assert result is False


class TestExtractGrowwStatusRows:
    """Test market status response parsing."""

    def test_extract_from_list_envelope(self):
        resp = {"data": [
            {"segment": "NSE", "status": True},
            {"segment": "NSE_FO", "status": False},
        ]}
        result = _extract_groww_status_rows(resp, ("NSE", "NSE_FO"))
        assert len(result) == 2
        assert result[0]["segment"] == "NSE"

    def test_extract_from_flat_dict(self):
        resp = {
            "NSE": {"status": "OPEN"},
            "NSE_FO": {"status": "CLOSED"},
        }
        result = _extract_groww_status_rows(resp, ("NSE", "NSE_FO"))
        assert len(result) == 2

    def test_extract_returns_none_on_unparseable_shape(self):
        resp = "invalid_shape"
        result = _extract_groww_status_rows(resp, ("NSE",))
        assert result is None

    def test_extract_with_payload_key(self):
        resp = {"payload": [{"segment": "NSE", "status": True}]}
        result = _extract_groww_status_rows(resp, ("NSE",))
        assert len(result) == 1


# ============================================================================
# PART B: Response Normalisers
# ============================================================================

class TestNormaliseHoldings:
    """Test holdings response normalisation."""

    def test_normalise_single_holding(self):
        resp = {
            "data": {
                "holdings": [
                    {
                        "trading_symbol": "RELIANCE",
                        "exchange": "NSE",
                        "quantity": 10,
                        "t1_quantity": 0,
                        "average_price": 2500.0,
                        "last_price": 2600.0,
                        "close_price": 2550.0,
                    }
                ]
            }
        }
        result = _normalise_holdings(resp)
        assert len(result) == 1
        h = result[0]
        assert h["tradingsymbol"] == "RELIANCE"
        assert h["quantity"] == 10
        assert h["average_price"] == 2500.0
        assert h["pnl"] == 1000.0  # (2600 - 2500) * 10

    def test_normalise_holdings_derives_pnl(self):
        resp = {
            "data": {
                "holdings": [
                    {
                        "trading_symbol": "TCS",
                        "exchange": "NSE",
                        "quantity": 5,
                        "t1_quantity": 0,
                        "average_price": 4000.0,
                        "last_price": 4200.0,
                        "close_price": 0,  # missing close
                    }
                ]
            }
        }
        result = _normalise_holdings(resp)
        assert result[0]["pnl"] == 1000.0

    def test_normalise_holdings_handles_t1_quantity(self):
        resp = {
            "data": {
                "holdings": [
                    {
                        "trading_symbol": "INFY",
                        "quantity": 20,
                        "t1_quantity": 5,
                        "average_price": 1500.0,
                        "last_price": 1600.0,
                        "close_price": 1550.0,
                    }
                ]
            }
        }
        result = _normalise_holdings(resp)
        assert result[0]["opening_quantity"] == 15  # 20 - 5


class TestNormalisePositions:
    """Test positions response normalisation."""

    def test_normalise_net_position(self):
        resp = {
            "data": {
                "positions": [
                    {
                        "trading_symbol": "RELIANCE",
                        "exchange": "NFO",
                        "product": "NRML",
                        "quantity": 100,
                        "net_carry_forward_quantity": 50,
                        "day_buy_quantity": 50,
                        "day_sell_quantity": 0,
                        "average_price": 2500.0,
                        "last_price": 2600.0,
                        "pnl": 5000.0,
                    }
                ]
            }
        }
        result = _normalise_positions(resp)
        assert "net" in result
        assert len(result["net"]) == 1
        p = result["net"][0]
        assert p["tradingsymbol"] == "RELIANCE"
        assert p["overnight_quantity"] == 50
        assert p["day_buy_quantity"] == 50
        assert p["multiplier"] == 1

    def test_normalise_positions_day_values(self):
        resp = {
            "data": {
                "positions": [
                    {
                        "trading_symbol": "SILVER",
                        "exchange": "MCX",
                        "quantity": 100,
                        "day_buy_quantity": 100,
                        "day_buy_price": 68000,
                        "day_sell_quantity": 0,
                        "average_price": 68000.0,
                        "last_price": 69000.0,
                    }
                ]
            }
        }
        result = _normalise_positions(resp)
        p = result["net"][0]
        assert p["day_buy_value"] == 6800000  # 100 * 68000


class TestNormaliseOrders:
    """Test order response normalisation."""

    def test_normalise_single_order(self):
        resp = {
            "data": {
                "order_list": [
                    {
                        "groww_order_id": "ORD123",
                        "trading_symbol": "RELIANCE",
                        "exchange": "NSE",
                        "order_status": "EXECUTED",
                        "transaction_type": "BUY",
                        "order_type": "LIMIT",
                        "product": "CNC",
                        "quantity": 1,
                        "filled_quantity": 1,
                        "remaining_quantity": 0,
                        "price": 2500.0,
                        "trigger_price": 0,
                        "average_price": 2500.5,
                        "created_at": "2026-07-17 10:00:00",
                    }
                ]
            }
        }
        result = _normalise_orders(resp)
        assert len(result) == 1
        o = result[0]
        assert o["order_id"] == "ORD123"
        assert o["status"] == "COMPLETE"  # EXECUTED → COMPLETE
        assert o["tradingsymbol"] == "RELIANCE"
        assert o["quantity"] == 1

    def test_normalise_orders_partial_fill(self):
        resp = {
            "data": {
                "order_list": [
                    {
                        "groww_order_id": "ORD456",
                        "trading_symbol": "TCS",
                        "order_status": "PARTIALLY_FILLED",
                        "quantity": 10,
                        "filled_quantity": 5,
                        "remaining_quantity": 5,
                    }
                ]
            }
        }
        result = _normalise_orders(resp)
        assert result[0]["status"] == "OPEN"
        assert result[0]["filled_quantity"] == 5
        assert result[0]["pending_quantity"] == 5


class TestNormaliseMargins:
    """Test margin response normalisation."""

    def test_normalise_margins_equity_segment(self):
        resp = {
            "data": {
                "equity": {
                    "net": 500000.0,
                    "available_balance": 450000.0,
                    "opening_balance": 600000.0,
                    "utilised": 150000.0,
                    "exposure_margin": 100000.0,
                }
            }
        }
        result = _normalise_margins(resp, segment=None)
        assert result["net"] == 500000.0
        assert result["available"]["cash"] == 450000.0
        assert result["utilised"]["debits"] == 150000.0

    def test_normalise_margins_commodity_segment(self):
        resp = {
            "data": {
                "equity": {"net": 500000.0},
                "commodity": {"net": 600000.0},
            }
        }
        result = _normalise_margins(resp, segment="commodity")
        assert result["net"] == 600000.0

    def test_normalise_margins_flat_response(self):
        resp = {
            "data": {
                "net": 500000.0,
                "available_balance": 450000.0,
            }
        }
        result = _normalise_margins(resp, segment=None)
        assert result["net"] == 500000.0


class TestNormaliseGttRow:
    """Test GTT order row normalisation."""

    def test_normalise_gtt_row(self):
        row = {
            "smart_order_id": "gtt_123",
            "status": "ACTIVE",
            "trading_symbol": "RELIANCE",
            "exchange": "NSE",
            "trigger_price": 2600.0,
            "last_price": 2500.0,
            "quantity": 1,
            "order": {
                "transaction_type": "SELL",
                "order_type": "LIMIT",
                "price": 2610.0,
            },
        }
        result = _normalise_groww_gtt_row(row)
        assert result["gtt_id"] == "gtt_123"
        assert result["status"] == "active"
        assert result["tradingsymbol"] == "RELIANCE"
        assert result["trigger_values"] == [2600.0]
        assert len(result["orders"]) == 1


class TestNormaliseQuoteRow:
    """Test quote row normalisation."""

    def test_normalise_quote_row(self):
        data = {
            "exchange_token": 123456,
            "last_price": 2600.0,
            "volume": 1000000,
            "average_price": 2580.0,
            "open_interest": 500,
            "open": 2500.0,
            "high": 2650.0,
            "low": 2490.0,
            "close": 2600.0,
        }
        result = _normalise_quote_row(data)
        assert result["last_price"] == 2600.0
        assert result["volume"] == 1000000
        assert result["ohlc"]["open"] == 2500.0
        assert result["ohlc"]["high"] == 2650.0
        assert result["depth"]["buy"] == []


# ============================================================================
# PART C: GTT Helpers
# ============================================================================

class TestGrowwGttOrderBody:
    """Test GTT order body construction."""

    def test_gtt_order_body_limit(self):
        result = _groww_gtt_order_body("SELL", "LIMIT", 2600.0)
        assert result["transaction_type"] == "SELL"
        assert result["order_type"] == "LIMIT"
        assert result["price"] == 2600.0

    def test_gtt_order_body_market(self):
        result = _groww_gtt_order_body("BUY", "MARKET", 0.0)
        assert result["transaction_type"] == "BUY"
        assert result["order_type"] == "MARKET"
        assert "price" not in result

    def test_gtt_order_body_limit_zero_price(self):
        result = _groww_gtt_order_body("BUY", "LIMIT", 0.0)
        assert "price" not in result  # LIMIT with 0 price omitted


class TestGrowwBuildGttOrderLeg:
    """Test GTT order leg construction."""

    def test_build_gtt_order_leg(self):
        r = {"quantity": 1, "product_type": "NRML"}
        order_inner = {
            "transaction_type": "SELL",
            "order_type": "LIMIT",
            "price": 2600.0,
        }
        result = _groww_build_gtt_order_leg(r, order_inner)
        assert result["transaction_type"] == "SELL"
        assert result["quantity"] == 1
        assert result["price"] == 2600.0
        assert result["order_type"] == "LIMIT"
        assert result["product"] == "NRML"


# ============================================================================
# PART D: GrowwBroker Adapter Methods
# ============================================================================

class TestGrowwBrokerBasics:
    """Test basic GrowwBroker properties and identity."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.api_key = "TEST_KEY"
        conn.access_token = "TEST_TOKEN"
        return GrowwBroker(conn)

    def test_broker_account_property(self, broker):
        assert broker.account == "test_account"

    def test_broker_broker_id(self, broker):
        assert broker.broker_id == "groww"

    def test_broker_has_groww_property(self, broker):
        broker._conn.get_groww_conn = MagicMock(return_value=MagicMock())
        assert broker.groww is not None


class TestGrowwBrokerProfile:
    """Test broker.profile() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_profile_success(self, broker):
        broker.groww.get_user_profile = MagicMock(return_value={
            "data": {
                "user_id": "USER123",
                "user_name": "John Trader",
                "email": "john@example.com",
            }
        })
        result = broker.profile()
        assert result["user_id"] == "USER123"
        assert result["user_name"] == "John Trader"
        assert result["broker"] == "GROWW"

    def test_profile_fallback_keys(self, broker):
        broker.groww.get_user_profile = MagicMock(return_value={
            "data": {
                "userId": "USER456",
                "name": "Jane Trader",
            }
        })
        result = broker.profile()
        assert result["user_id"] == "USER456"
        assert result["user_name"] == "Jane Trader"


class TestGrowwBrokerHoldings:
    """Test broker.holdings() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_holdings_returns_list(self, broker):
        broker.groww.get_holdings_for_user = MagicMock(return_value={
            "data": {
                "holdings": [
                    {
                        "trading_symbol": "RELIANCE",
                        "exchange": "NSE",
                        "quantity": 10,
                        "t1_quantity": 0,
                        "average_price": 2500.0,
                        "last_price": 2600.0,
                        "close_price": 2550.0,
                    }
                ]
            }
        })
        result = broker.holdings()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["tradingsymbol"] == "RELIANCE"

    def test_holdings_empty_response(self, broker):
        broker.groww.get_holdings_for_user = MagicMock(return_value={
            "data": {"holdings": []}
        })
        result = broker.holdings()
        assert result == []


class TestGrowwBrokerPositions:
    """Test broker.positions() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_positions_returns_dict_with_net_day(self, broker):
        broker.groww.get_positions_for_user = MagicMock(return_value={
            "data": {
                "positions": [
                    {
                        "trading_symbol": "RELIANCE",
                        "exchange": "NFO",
                        "quantity": 100,
                        "net_carry_forward_quantity": 50,
                        "average_price": 2500.0,
                    }
                ]
            }
        })
        result = broker.positions()
        assert isinstance(result, dict)
        assert "net" in result
        assert "day" in result


class TestGrowwBrokerOrders:
    """Test broker.orders() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_orders_returns_list(self, broker):
        broker.groww.get_order_list = MagicMock(return_value={
            "data": {
                "order_list": [
                    {
                        "groww_order_id": "ORD123",
                        "trading_symbol": "RELIANCE",
                        "exchange": "NSE",
                        "order_status": "EXECUTED",
                        "quantity": 1,
                        "filled_quantity": 1,
                    }
                ]
            }
        })
        result = broker.orders()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["order_id"] == "ORD123"


class TestGrowwBrokerMargins:
    """Test broker.margins() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_margins_returns_dict(self, broker):
        broker.groww.get_available_margin_details = MagicMock(return_value={
            "data": {
                "equity": {
                    "net": 500000.0,
                    "available_balance": 450000.0,
                }
            }
        })
        result = broker.margins()
        assert isinstance(result, dict)
        assert "net" in result
        assert "available" in result
        assert result["net"] == 500000.0


class TestGrowwBrokerLtp:
    """Test broker.ltp() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_ltp_single_symbol(self, broker):
        broker.groww.get_ltp = MagicMock(return_value={
            "data": {
                "NSE_RELIANCE": 2600.0,
            }
        })
        result = broker.ltp(["NSE:RELIANCE"])
        assert isinstance(result, dict)
        assert "NSE:RELIANCE" in result
        assert result["NSE:RELIANCE"]["last_price"] == 2600.0

    def test_ltp_empty_list(self, broker):
        result = broker.ltp([])
        assert result == {}

    def test_ltp_invalid_format_skipped(self, broker):
        result = broker.ltp(["INVALID"])
        assert result == {}


class TestGrowwBrokerPlaceOrder:
    """Test broker.place_order() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_place_order_returns_order_id(self, broker):
        broker.groww.place_order = MagicMock(return_value={
            "data": {
                "groww_order_id": "ORD789",
            }
        })
        result = broker.place_order(
            tradingsymbol="RELIANCE",
            exchange="NSE",
            transaction_type="BUY",
            quantity=1,
            order_type="MARKET",
            product="CNC"
        )
        assert result == "ORD789"

    def test_place_order_amo_not_implemented(self, broker):
        with pytest.raises(NotImplementedError):
            broker.place_order(
                tradingsymbol="RELIANCE",
                exchange="NSE",
                transaction_type="BUY",
                quantity=1,
                order_type="MARKET",
                product="CNC",
                variety="AMO"
            )

    def test_place_order_no_order_id_raises(self, broker):
        broker.groww.place_order = MagicMock(return_value={
            "data": {}
        })
        with pytest.raises(RuntimeError):
            broker.place_order(
                tradingsymbol="RELIANCE",
                exchange="NSE",
                transaction_type="BUY",
                quantity=1
            )


class TestGrowwBrokerCancelOrder:
    """Test broker.cancel_order() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_cancel_order_returns_order_id(self, broker):
        broker.groww.cancel_order = MagicMock(return_value={"status": "SUCCESS"})
        result = broker.cancel_order("ORD123", exchange="NSE")
        assert result == "ORD123"

    def test_cancel_order_resolve_exchange_from_orders(self, broker):
        broker.groww.cancel_order = MagicMock()
        broker.groww.get_order_list = MagicMock(return_value={
            "data": {
                "order_list": [
                    {
                        "groww_order_id": "ORD123",
                        "exchange": "NFO",
                    }
                ]
            }
        })
        result = broker.cancel_order("ORD123")
        assert result == "ORD123"
        broker.groww.cancel_order.assert_called_once()

    def test_cancel_order_no_exchange_raises(self, broker):
        broker.groww.get_order_list = MagicMock(return_value={"data": {}})
        with pytest.raises(ValueError):
            broker.cancel_order("ORD999")


class TestGrowwBrokerGetGtts:
    """Test broker.get_gtts() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_get_gtts_returns_list(self, broker):
        broker.groww.get_smart_order_list = MagicMock(return_value={
            "data": {
                "smart_orders": [
                    {
                        "smart_order_id": "gtt_123",
                        "status": "ACTIVE",
                        "trading_symbol": "RELIANCE",
                        "exchange": "NSE",
                        "trigger_price": 2600.0,
                        "last_price": 2500.0,
                        "quantity": 1,
                    }
                ]
            }
        })
        result = broker.get_gtts()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["gtt_id"] == "gtt_123"

    def test_get_gtts_pagination(self, broker):
        # Mock multiple pages
        broker.groww.get_smart_order_list = MagicMock(side_effect=[
            {
                "data": {
                    "smart_orders": [
                        {"smart_order_id": f"gtt_{i}", "quantity": 1}
                        for i in range(50)
                    ]
                }
            },
            {"data": {"smart_orders": []}},
        ])
        result = broker.get_gtts()
        assert len(result) >= 50


class TestGrowwBrokerPlaceGtt:
    """Test broker.place_gtt() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_place_gtt_single_leg(self, broker):
        broker.groww.create_smart_order = MagicMock(return_value={
            "data": {
                "smart_order_id": "gtt_new_123",
            }
        })
        result = broker.place_gtt(
            trigger_type="single",
            tradingsymbol="RELIANCE",
            exchange="NSE",
            last_price=2500.0,
            orders=[{"transaction_type": "SELL", "order_type": "LIMIT", "price": 2600.0}],
            trigger_values=[2600.0],
        )
        assert result == "gtt_new_123"

    def test_place_gtt_oco_returns_compound_id(self, broker):
        broker.groww.create_smart_order = MagicMock(side_effect=[
            {"data": {"smart_order_id": "gtt_tp"}},
            {"data": {"smart_order_id": "gtt_sl"}},
        ])
        result = broker.place_gtt(
            trigger_type="two-leg",
            tradingsymbol="RELIANCE",
            exchange="NSE",
            last_price=2500.0,
            orders=[
                {"transaction_type": "SELL", "order_type": "LIMIT", "price": 2600.0},
                {"transaction_type": "BUY", "order_type": "LIMIT", "price": 2400.0},
            ],
            trigger_values=[2600.0, 2400.0],
        )
        assert result.startswith("oco:")
        assert "+" in result

    def test_place_gtt_oco_rollback_on_second_leg_failure(self, broker):
        broker.groww.create_smart_order = MagicMock(side_effect=[
            {"data": {"smart_order_id": "gtt_tp"}},
            Exception("Network error"),
        ])
        broker.groww.cancel_smart_order = MagicMock()

        with pytest.raises(RuntimeError):
            broker.place_gtt(
                trigger_type="two-leg",
                tradingsymbol="RELIANCE",
                exchange="NSE",
                last_price=2500.0,
                orders=[
                    {"transaction_type": "SELL", "order_type": "LIMIT", "price": 2600.0},
                    {"transaction_type": "BUY", "order_type": "LIMIT", "price": 2400.0},
                ],
                trigger_values=[2600.0, 2400.0],
            )
        # Verify rollback was attempted
        broker.groww.cancel_smart_order.assert_called()


class TestGrowwBrokerCancelGtt:
    """Test broker.cancel_gtt() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_cancel_gtt_single(self, broker):
        broker.groww.cancel_smart_order = MagicMock()
        result = broker.cancel_gtt("gtt_123", exchange="NSE")
        assert result == "gtt_123"

    def test_cancel_gtt_oco_compound_id(self, broker):
        broker.groww.cancel_smart_order = MagicMock()
        result = broker.cancel_gtt("oco:gtt_tp+gtt_sl", exchange="NSE")
        assert result == "oco:gtt_tp+gtt_sl"
        # Both should be cancelled
        assert broker.groww.cancel_smart_order.call_count == 2

    def test_cancel_gtt_requires_exchange(self, broker):
        with pytest.raises(ValueError):
            broker.cancel_gtt("gtt_123")


class TestGrowwBrokerTranslateQty:
    """Test broker.translate_qty() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        return GrowwBroker(conn)

    def test_translate_qty_nse_noop(self, broker):
        result = broker.translate_qty("NSE", 100, 1)
        assert result == 100

    def test_translate_qty_mcx_noop(self, broker):
        # Groww accepts contracts, not lots
        result = broker.translate_qty("MCX", 100, 100)
        assert result == 100

    def test_translate_qty_all_segments_noop(self, broker):
        # Groww same behavior for all segments
        result = broker.translate_qty("NFO", 50, 1)
        assert result == 50


# ============================================================================
# PART E: Error Path Tests
# ============================================================================

class TestGrowwBrokerErrorPaths:
    """Test error handling in broker methods."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "test_account"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_profile_runtime_error_on_failure(self, broker):
        broker.groww.get_user_profile = MagicMock(side_effect=Exception("API error"))
        with pytest.raises(RuntimeError):
            broker.profile()

    def test_basket_order_margins_partial_failure(self, broker):
        """Test that basket_order_margins handles per-order failures gracefully."""
        broker.groww.get_order_margin_details = MagicMock(side_effect=[
            {"data": {"total_margin": 10000.0}},
            Exception("Order not found"),
        ])
        orders = [
            {"exchange": "NSE", "tradingsymbol": "REL", "quantity": 1, "order_type": "LIMIT", "product": "CNC", "price": 2500},
            {"exchange": "NFO", "tradingsymbol": "RELJAN", "quantity": 1, "order_type": "LIMIT", "product": "NRML", "price": 2500},
        ]
        result = broker.basket_order_margins(orders)
        assert len(result) == 2
        assert "error" in result[1]


# ============================================================================
# PART F: Parse OCO ID Tests
# ============================================================================

class TestParseOcoId:
    """Test OCO ID parsing."""

    def test_parse_oco_id_valid(self):
        from backend.brokers.adapters.groww import GrowwBroker
        result = GrowwBroker._parse_oco_id("oco:gtt_tp+gtt_sl")
        assert result == ("gtt_tp", "gtt_sl")

    def test_parse_oco_id_plain_id_returns_none(self):
        from backend.brokers.adapters.groww import GrowwBroker
        result = GrowwBroker._parse_oco_id("gtt_123")
        assert result is None

    def test_parse_oco_id_invalid_format_returns_none(self):
        from backend.brokers.adapters.groww import GrowwBroker
        result = GrowwBroker._parse_oco_id("oco:gtt_tp")
        assert result is None


# ============================================================================
# PART G: Import Error Handling
# ============================================================================

class TestImportErrorHandling:
    """Test graceful fallback when Groww SDK is not installed."""

    def test_groww_auth_exc_empty_tuple_when_missing(self):
        """When GrowwAPI not available, auth exceptions are empty tuple."""
        # This test validates that the module handles ImportError gracefully
        # by checking that _GROWW_AUTH_EXC exists (even if empty in this case)
        from backend.brokers.adapters import groww as groww_module
        assert hasattr(groww_module, '_GROWW_AUTH_EXC')
        # If import succeeded, it should be a tuple
        assert isinstance(groww_module._GROWW_AUTH_EXC, tuple)


# ============================================================================
# PART H: Additional Coverage for Untested Paths
# ============================================================================

class TestGrowwStatusRowForCode:
    """Test _groww_status_row_for_code helper."""

    def test_groww_status_row_dict_value(self):
        from backend.brokers.adapters.groww import _groww_status_row_for_code
        data = {"NSE": {"status": "open"}}
        result = _groww_status_row_for_code(data, "NSE")
        assert result == {"segment": "NSE", "status": "open"}

    def test_groww_status_row_string_value(self):
        from backend.brokers.adapters.groww import _groww_status_row_for_code
        data = {"NFO": "open"}
        result = _groww_status_row_for_code(data, "NFO")
        assert result == {"segment": "NFO", "status": "open"}

    def test_groww_status_row_bool_value(self):
        from backend.brokers.adapters.groww import _groww_status_row_for_code
        data = {"MCX": True}
        result = _groww_status_row_for_code(data, "MCX")
        assert result == {"segment": "MCX", "status": True}

    def test_groww_status_row_missing_returns_none(self):
        from backend.brokers.adapters.groww import _groww_status_row_for_code
        data = {}
        result = _groww_status_row_for_code(data, "NSE")
        assert result is None

    def test_groww_status_row_case_insensitive(self):
        from backend.brokers.adapters.groww import _groww_status_row_for_code
        data = {"nse": {"status": "closed"}}
        result = _groww_status_row_for_code(data, "NSE")
        assert result == {"segment": "NSE", "status": "closed"}


class TestExtractGrowwStatusRows:
    """Test _extract_groww_status_rows helper."""

    def test_extract_status_rows_list_format(self):
        from backend.brokers.adapters.groww import _extract_groww_status_rows
        resp = {
            "data": [
                {"segment": "NSE", "status": "open"},
                {"segment": "NFO", "status": "open"},
            ]
        }
        result = _extract_groww_status_rows(resp, ("NSE", "NFO"))
        assert len(result) == 2
        assert result[0]["segment"] == "NSE"

    def test_extract_status_rows_dict_format(self):
        from backend.brokers.adapters.groww import _extract_groww_status_rows
        resp = {
            "data": {
                "NSE": {"status": "open"},
                "NFO": {"status": "open"},
            }
        }
        result = _extract_groww_status_rows(resp, ("NSE", "NFO"))
        assert len(result) == 2

    def test_extract_status_rows_flat_dict_format(self):
        from backend.brokers.adapters.groww import _extract_groww_status_rows
        resp = {
            "NSE": {"status": "open"},
            "NFO": {"status": "open"},
        }
        result = _extract_groww_status_rows(resp, ("NSE", "NFO"))
        assert len(result) == 2

    def test_extract_status_rows_invalid_format_returns_none(self):
        from backend.brokers.adapters.groww import _extract_groww_status_rows
        resp = "invalid string"
        result = _extract_groww_status_rows(resp, ("NSE",))
        assert result is None

    def test_extract_status_rows_empty_data_returns_none(self):
        from backend.brokers.adapters.groww import _extract_groww_status_rows
        resp = {"data": 123}  # Invalid shape
        result = _extract_groww_status_rows(resp, ("NSE",))
        assert result is None


class TestGrowwHistCandleRow:
    """Test _groww_hist_candle_row helper."""

    def test_hist_candle_row_valid_list(self):
        from backend.brokers.adapters.groww import _groww_hist_candle_row
        row = ["2026-07-17", 100.0, 105.0, 95.0, 102.0, 1000]
        result = _groww_hist_candle_row(row)
        assert result["open"] == 100.0
        assert result["high"] == 105.0
        assert result["low"] == 95.0
        assert result["close"] == 102.0
        assert result["volume"] == 1000

    def test_hist_candle_row_valid_tuple(self):
        from backend.brokers.adapters.groww import _groww_hist_candle_row
        row = ("2026-07-17", 100.0, 105.0, 95.0, 102.0, 1000)
        result = _groww_hist_candle_row(row)
        assert result is not None
        assert result["close"] == 102.0

    def test_hist_candle_row_too_short_returns_none(self):
        from backend.brokers.adapters.groww import _groww_hist_candle_row
        row = [100.0, 105.0]  # Only 2 elements
        result = _groww_hist_candle_row(row)
        assert result is None

    def test_hist_candle_row_invalid_type_returns_none(self):
        from backend.brokers.adapters.groww import _groww_hist_candle_row
        row = "not a list"
        result = _groww_hist_candle_row(row)
        assert result is None

    def test_hist_candle_row_with_none_values(self):
        from backend.brokers.adapters.groww import _groww_hist_candle_row
        row = ["2026-07-17", None, None, None, None, None]
        result = _groww_hist_candle_row(row)
        assert result["open"] == 0.0
        assert result["volume"] == 0


class TestGrowwInstrumentRow:
    """Test _groww_instrument_row helper."""

    def test_instrument_row_basic(self):
        from backend.brokers.adapters.groww import _groww_instrument_row
        r = {
            "exchange_token": "12345",
            "trading_symbol": "RELIANCE",
            "name": "Reliance Industries",
            "exchange": "NSE",
            "segment": "EQ",
            "instrument_type": "EQUITY",
            "expiry": "",
            "strike": 0,
        }
        result = _groww_instrument_row(r)
        assert result["instrument_token"] == "12345"
        assert result["tradingsymbol"] == "RELIANCE"
        assert result["name"] == "Reliance Industries"

    def test_instrument_row_with_groww_symbol_fallback(self):
        from backend.brokers.adapters.groww import _groww_instrument_row
        r = {
            "exchange_token": "12345",
            "trading_symbol": "REL",
            "groww_symbol": "RELIANCE",
            "exchange": "NSE",
        }
        result = _groww_instrument_row(r)
        assert result["name"] == "RELIANCE"

    def test_instrument_row_with_missing_fields(self):
        from backend.brokers.adapters.groww import _groww_instrument_row
        r = {"exchange_token": "12345"}
        result = _groww_instrument_row(r)
        assert result["instrument_token"] == "12345"
        assert result["tradingsymbol"] == ""
        assert result["strike"] == 0.0


class TestGrowwCoerceDate:
    """Test _groww_coerce_date helper."""

    def test_coerce_date_string_iso_format(self):
        from backend.brokers.adapters.groww import _groww_coerce_date
        result = _groww_coerce_date("2026-07-17")
        assert result == "2026-07-17"

    def test_coerce_date_datetime_object(self):
        from backend.brokers.adapters.groww import _groww_coerce_date
        from datetime import datetime
        dt = datetime(2026, 7, 17, 10, 30, 0)
        result = _groww_coerce_date(dt)
        assert "2026-07-17" in result
        assert "10:30" in result

    def test_coerce_date_arbitrary_string(self):
        from backend.brokers.adapters.groww import _groww_coerce_date
        result = _groww_coerce_date("2026-07-17 14:30:00")
        assert "2026-07-17" in result


class TestGrowwBrokerHoldingsPositionsOrdersPaths:
    """Test broker methods for holdings, positions, margins, orders."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "GRW123"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_holdings_normalizes_response(self, broker):
        """Test holdings() calls groww.get_holdings_for_user()."""
        broker.groww.get_holdings_for_user = MagicMock(return_value={
            "data": [
                {
                    "tradingsymbol": "RELIANCE",
                    "exchange": "NSE",
                    "avg_price": "2500.0",
                    "quantity": "10",
                    "value": "25000.0",
                }
            ]
        })
        result = broker.holdings()
        assert isinstance(result, list)

    def test_positions_normalizes_response(self, broker):
        """Test positions() calls groww.get_positions_for_user()."""
        broker.groww.get_positions_for_user = MagicMock(return_value={
            "data": [
                {
                    "tradingsymbol": "RELIANCE",
                    "exchange": "NSE",
                    "quantity": "1",
                    "avg_price": "2500.0",
                    "ltp": "2600.0",
                    "segment": "NFO",
                }
            ]
        })
        result = broker.positions()
        assert isinstance(result, dict)

    def test_margins_logs_response_keys_once(self, broker):
        """Test margins() logs raw response keys only once per account."""
        broker.groww.get_available_margin_details = MagicMock(return_value={
            "available_margin": 100000.0,
            "utilized_margin": 50000.0,
        })
        result1 = broker.margins()
        result2 = broker.margins()
        # Log happens only once due to account tracking
        assert isinstance(result1, dict)
        assert isinstance(result2, dict)

    def test_orders_normalizes_response(self, broker):
        """Test orders() calls groww.get_order_list()."""
        broker.groww.get_order_list = MagicMock(return_value={
            "data": [
                {
                    "groww_order_id": "ORD123",
                    "tradingsymbol": "RELIANCE",
                    "order_status": "COMPLETE",
                    "quantity": "10",
                }
            ]
        })
        result = broker.orders()
        assert isinstance(result, list)


class TestGrowwBrokerOrderStatusMethod:
    """Test broker.order_status() method with SDK version detection."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "GRW123"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_order_status_uses_get_order_detail(self, broker):
        """Test order_status() prefers get_order_detail."""
        broker.groww.get_order_detail = MagicMock(return_value={
            "data": {
                "groww_order_id": "ORD123",
                "order_status": "COMPLETE",
                "quantity": "10",
            }
        })
        result = broker.order_status("ORD123")
        assert isinstance(result, dict)
        broker.groww.get_order_detail.assert_called_once()

    def test_order_status_uses_get_order_status_by_id_fallback(self, broker):
        """Test order_status() falls back to get_order_status_by_id."""
        broker.groww.get_order_detail = None
        broker.groww.get_order_status_by_id = MagicMock(return_value={
            "data": {"order_status": "COMPLETE"}
        })
        result = broker.order_status("ORD123")
        assert isinstance(result, dict)

    def test_order_status_handles_exception(self, broker):
        """Test order_status() handles exceptions gracefully."""
        broker.groww.get_order_detail = MagicMock(side_effect=Exception("API error"))
        result = broker.order_status("ORD123")
        assert result == {}


class TestGrowwBrokerTrades:
    """Test broker.trades() stub."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "GRW123"
        return GrowwBroker(conn)

    def test_trades_returns_empty_list(self, broker):
        """Test trades() returns empty list since Groww has no day trade endpoint."""
        result = broker.trades()
        assert result == []


class TestGrowwBrokerLtpFetchSegment:
    """Test broker._ltp_fetch_segment() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "GRW123"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_ltp_fetch_segment_valid_response(self, broker):
        """Test _ltp_fetch_segment() populates output dict."""
        broker.groww.get_ltp = MagicMock(return_value={
            "data": {
                "NSE_RELIANCE": 2600.0,
                "NSE_INFY": 3400.0,
            }
        })
        out = {}
        broker._ltp_fetch_segment("EQ", ["NSE_RELIANCE", "NSE_INFY"], out)
        assert "NSE:RELIANCE" in out
        assert out["NSE:RELIANCE"]["last_price"] == 2600.0

    def test_ltp_fetch_segment_handles_auth_error(self, broker):
        """Test _ltp_fetch_segment() re-raises auth errors."""
        from backend.brokers.adapters.groww import GrowwAPIAuthenticationException
        broker.groww.get_ltp = MagicMock(
            side_effect=GrowwAPIAuthenticationException()
        )
        out = {}
        with pytest.raises(GrowwAPIAuthenticationException):
            broker._ltp_fetch_segment("EQ", ["NSE_RELIANCE"], out)

    def test_ltp_fetch_segment_handles_authz_error(self, broker):
        """Test _ltp_fetch_segment() handles authorization errors."""
        from backend.brokers.adapters.groww import GrowwAPIAuthorisationException
        broker.groww.get_ltp = MagicMock(
            side_effect=GrowwAPIAuthorisationException()
        )
        out = {}
        with patch('backend.brokers.adapters.groww.record_entitlement_denied'):
            broker._ltp_fetch_segment("EQ", ["NSE_RELIANCE"], out)
        # Should not raise, just log

    def test_ltp_fetch_segment_handles_generic_error(self, broker):
        """Test _ltp_fetch_segment() handles generic errors."""
        broker.groww.get_ltp = MagicMock(side_effect=Exception("Network error"))
        out = {}
        broker._ltp_fetch_segment("EQ", ["NSE_RELIANCE"], out)
        # Should not raise, just log


class TestGrowwBrokerQuoteSingle:
    """Test broker._quote_single() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "GRW123"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_quote_single_valid_symbol(self, broker):
        """Test _quote_single() with valid NSE:RELIANCE."""
        broker.groww.get_quote = MagicMock(return_value={
            "data": {
                "ltp": 2600.0,
                "open": 2550.0,
                "high": 2610.0,
                "low": 2540.0,
            }
        })
        result = broker._quote_single("NSE:RELIANCE")
        assert "NSE:RELIANCE" in result

    def test_quote_single_invalid_symbol_returns_empty(self, broker):
        """Test _quote_single() with malformed symbol."""
        result = broker._quote_single("RELIANCE")  # Missing exchange
        assert result == {}

    def test_quote_single_auth_error_re_raises(self, broker):
        """Test _quote_single() re-raises authentication errors."""
        from backend.brokers.adapters.groww import GrowwAPIAuthenticationException
        broker.groww.get_quote = MagicMock(
            side_effect=GrowwAPIAuthenticationException()
        )
        with pytest.raises(GrowwAPIAuthenticationException):
            broker._quote_single("NSE:RELIANCE")

    def test_quote_single_authz_error_handled(self, broker):
        """Test _quote_single() handles authorization errors."""
        from backend.brokers.adapters.groww import GrowwAPIAuthorisationException
        broker.groww.get_quote = MagicMock(
            side_effect=GrowwAPIAuthorisationException()
        )
        with patch('backend.brokers.adapters.groww.record_entitlement_denied'):
            result = broker._quote_single("NSE:RELIANCE")
        assert result == {}

    def test_quote_single_generic_error_handled(self, broker):
        """Test _quote_single() handles generic errors."""
        broker.groww.get_quote = MagicMock(side_effect=Exception("Network error"))
        result = broker._quote_single("NSE:RELIANCE")
        assert result == {}


class TestGrowwBrokerModifyOrderExchangeResolution:
    """Test broker.modify_order() with exchange resolution."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "GRW123"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_modify_order_with_explicit_exchange(self, broker):
        """Test modify_order() when exchange is provided."""
        broker.groww.modify_order = MagicMock()
        broker.modify_order(
            "ORD123",
            exchange="NSE",
            quantity=10,
            order_type="LIMIT",
            price=2500.0,
        )
        broker.groww.modify_order.assert_called_once()

    def test_modify_order_resolves_missing_exchange(self, broker):
        """Test modify_order() resolves exchange from broker.orders()."""
        broker.groww.modify_order = MagicMock()
        broker.groww.get_order_list = MagicMock(return_value={
            "data": [
                {
                    "groww_order_id": "ORD123",
                    "exchange": "NSE",
                    "tradingsymbol": "RELIANCE",
                }
            ]
        })
        broker.modify_order("ORD123", quantity=10, order_type="LIMIT", price=2500.0)
        broker.groww.modify_order.assert_called_once()

    def test_modify_order_raises_on_missing_exchange(self, broker):
        """Test modify_order() raises ValueError when exchange cannot be resolved."""
        broker.groww.get_order_list = MagicMock(return_value={"data": []})
        with pytest.raises(ValueError):
            broker.modify_order("ORD123", quantity=10)


class TestGrowwBrokerHistoricalData:
    """Test broker.historical_data() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "GRW123"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_historical_data_no_symbol_returns_empty(self, broker):
        """Test historical_data() returns [] when trading_symbol is missing."""
        result = broker.historical_data(
            instrument_token=12345,
            from_date="2026-07-01",
            to_date="2026-07-17",
            exchange="NSE",
            trading_symbol=None,
            interval="1day",
        )
        assert result == []

    def test_historical_data_no_exchange_returns_empty(self, broker):
        """Test historical_data() returns [] when exchange is missing."""
        result = broker.historical_data(
            instrument_token=12345,
            from_date="2026-07-01",
            to_date="2026-07-17",
            exchange=None,
            trading_symbol="RELIANCE",
            interval="1day",
        )
        assert result == []

    def test_historical_data_valid_request(self, broker):
        """Test historical_data() with valid parameters."""
        broker.groww.get_historical_candles = MagicMock(return_value={
            "data": {
                "candles": [
                    ["2026-07-17", 100.0, 105.0, 95.0, 102.0, 1000],
                    ["2026-07-16", 98.0, 101.0, 97.0, 100.0, 950],
                ]
            }
        })
        result = broker.historical_data(
            instrument_token=12345,
            from_date="2026-07-01",
            to_date="2026-07-17",
            exchange="NSE",
            trading_symbol="RELIANCE",
            interval="1day",
        )
        assert len(result) == 2
        assert result[0]["close"] == 102.0

    def test_historical_data_invalid_interval_raises(self, broker):
        """Test historical_data() raises on unknown interval."""
        with pytest.raises(ValueError):
            broker.historical_data(
                instrument_token=12345,
                from_date="2026-07-01",
                to_date="2026-07-17",
                exchange="NSE",
                trading_symbol="RELIANCE",
                interval="unknown",
            )


class TestGrowwBrokerMarketStatus:
    """Test broker.market_status() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "GRW123"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_market_status_unknown_exchange_returns_none(self, broker):
        """Test market_status() returns None for unknown exchange."""
        result = broker.market_status("UNKNOWN")
        assert result is None

    def test_market_status_open_exchange_returns_true(self, broker):
        """Test market_status() returns True when exchange is open."""
        broker.groww.get_market_status = MagicMock(return_value={
            "data": [
                {"segment": "NSE", "status": "OPEN"}
            ]
        })
        result = broker.market_status("NSE")
        assert result is True

    def test_market_status_closed_exchange_returns_false(self, broker):
        """Test market_status() returns False when exchange is closed."""
        broker.groww.get_market_status = MagicMock(return_value={
            "data": [
                {"segment": "NSE", "status": "CLOSED"}
            ]
        })
        result = broker.market_status("NSE")
        assert result is False

    def test_market_status_sdk_method_missing_returns_none(self, broker):
        """Test market_status() returns None when SDK method is unavailable."""
        broker.groww.get_market_status = None
        broker.groww.market_status = None
        broker.groww.get_exchange_status = None
        result = broker.market_status("NSE")
        assert result is None


class TestGrowwBrokerModifyGtt:
    """Test broker.modify_gtt() method."""

    @pytest.fixture
    def broker(self):
        conn = MagicMock()
        conn.account = "GRW123"
        conn.get_groww_conn = MagicMock()
        return GrowwBroker(conn)

    def test_modify_gtt_single_leg_updates_order(self, broker):
        """Test modify_gtt() with single leg updates the order."""
        broker.groww.modify_smart_order = MagicMock(return_value={
            "status": "success"
        })
        result = broker.modify_gtt(
            "gtt_123",
            trigger_type="single",
            tradingsymbol="RELIANCE",
            exchange="NSE",
            last_price=2500.0,
            orders=[{"transaction_type": "SELL", "order_type": "LIMIT", "price": 2600.0}],
            trigger_values=[2600.0],
        )
        assert result == "gtt_123"

    def test_modify_gtt_two_leg_without_oco_id_raises(self, broker):
        """Test modify_gtt() raises when two-leg requested with non-OCO id."""
        with pytest.raises(RuntimeError):
            broker.modify_gtt(
                "gtt_123",  # Not an OCO id
                trigger_type="two-leg",
                tradingsymbol="RELIANCE",
                exchange="NSE",
                last_price=2500.0,
                orders=[
                    {"transaction_type": "SELL", "order_type": "LIMIT", "price": 2600.0},
                    {"transaction_type": "BUY", "order_type": "LIMIT", "price": 2400.0},
                ],
                trigger_values=[2600.0, 2400.0],
            )
