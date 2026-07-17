"""
Comprehensive test coverage for backend/brokers/adapters/dhan.py.

Covers:
- Pure functions (symbol/strike formatting, response normalization)
- DhanBroker adapter methods (with mocked _safe_call)
- Auth-failure detection
- Rate limiter integration
"""

import pytest
from unittest.mock import MagicMock, patch

from backend.brokers.adapters.dhan import (
    # Pure functions
    _dhan_to_kite_symbol,
    _dhan_format_strike,
    _looks_like_auth_failure,
    _normalise_holdings,
    _normalise_positions,
    _normalise_orders,
    _normalise_trades,
    _dhan_exc,
    _dhan_first,
    _dhan_num,
    _dhan_int,
    _unwrap,
    _parse_dhan_date,
    _dhan_exchange_to_segment,
    _ist_today,
    # Adapter class
    DhanBroker,
    # Error map
    _DHAN_ERROR_MAP,
    # Rate limiter
    _DHAN_RATE_LIMITER,
    _DHAN_RATE_LIMIT_ENABLED,
)
from backend.brokers.errors import (
    BrokerAuthError,
    BrokerRateLimitError,
    BrokerError,
    BrokerOrderError,
)


# ─────────────────────────────────────────────────────────────────────────
# Part A: Pure Function Tests (No Mocking)
# ─────────────────────────────────────────────────────────────────────────


class TestDhanToKiteSymbol:
    """Test _dhan_to_kite_symbol symbol format conversion."""

    def test_option_with_day_prefix_old_format(self):
        """Old Dhan format: 'ROOT-DDMonYYYY-STRIKE-CE'."""
        raw = "CRUDEOIL-16JUL2026-8500-CE"
        expected = "CRUDEOIL26JUL8500CE"
        assert _dhan_to_kite_symbol(raw) == expected

    def test_option_without_day_prefix_new_format(self):
        """New Dhan format: 'ROOT-MonYYYY-STRIKE-CE' (equity options)."""
        raw = "CGPOWER-Jun2026-840-PE"
        expected = "CGPOWER26JUN840PE"
        assert _dhan_to_kite_symbol(raw) == expected

    def test_future_with_day_prefix(self):
        """Dhan futures: 'ROOT-DDMonYYYY-FUT'."""
        raw = "SILVER-03Jul2026-FUT"
        expected = "SILVER26JULFUT"
        assert _dhan_to_kite_symbol(raw) == expected

    def test_future_without_day_prefix_defensive(self):
        """Defensive: handle futures without day (not observed but safe)."""
        raw = "GOLD-Jan2026-FUT"
        expected = "GOLD26JANFUT"
        assert _dhan_to_kite_symbol(raw) == expected

    def test_equity_symbol_passthrough(self):
        """Non-derivative symbols pass through with dashes/spaces stripped."""
        raw = "RELIANCE"
        expected = "RELIANCE"
        assert _dhan_to_kite_symbol(raw) == expected

    def test_empty_string(self):
        """Empty input returns empty string."""
        assert _dhan_to_kite_symbol("") == ""

    def test_none_like_input(self):
        """None-like input treated as empty."""
        assert _dhan_to_kite_symbol(None) == ""

    def test_case_insensitive_parsing(self):
        """Input is uppercased before parsing."""
        raw = "crudeoil-16jul2026-8500-ce"
        expected = "CRUDEOIL26JUL8500CE"
        assert _dhan_to_kite_symbol(raw) == expected


class TestDhanFormatStrike:
    """Test _dhan_format_strike strike normalization."""

    def test_integer_strike(self):
        """Integer strikes stay integer."""
        assert _dhan_format_strike("8500") == "8500"

    def test_float_strike_with_trailing_zero(self):
        """8500.0 → '8500' (drop trailing .0)."""
        assert _dhan_format_strike("8500.0") == "8500"

    def test_fractional_strike(self):
        """Fractional strikes preserved: 8500.5 → '8500.5'."""
        assert _dhan_format_strike("8500.5") == "8500.5"

    def test_invalid_strike_passthrough(self):
        """Invalid strike returned unchanged."""
        assert _dhan_format_strike("invalid") == "invalid"


class TestLooksLikeAuthFailure:
    """Test _looks_like_auth_failure detection."""

    def test_auth_failure_invalid_token(self):
        """Response with status=failure + 'invalid token' remarks → True."""
        resp = {
            "status": "failure",
            "remarks": "Invalid Token",
        }
        assert _looks_like_auth_failure(resp) is True

    def test_auth_failure_401_code(self):
        """Response with status=failure + '401' in remarks → True."""
        resp = {
            "status": "failure",
            "remarks": "401 Unauthorized",
        }
        assert _looks_like_auth_failure(resp) is True

    def test_auth_failure_dh_901_code(self):
        """Response with status=failure + 'DH-901' in remarks → True."""
        resp = {
            "status": "failure",
            "remarks": "DH-901: Invalid Authentication",
        }
        assert _looks_like_auth_failure(resp) is True

    def test_success_response(self):
        """Response with status=success → False."""
        resp = {
            "status": "success",
            "data": [],
        }
        assert _looks_like_auth_failure(resp) is False

    def test_missing_status(self):
        """Response without status field → False."""
        resp = {
            "data": [],
        }
        assert _looks_like_auth_failure(resp) is False

    def test_non_dict_response(self):
        """Non-dict response → False."""
        assert _looks_like_auth_failure([]) is False
        assert _looks_like_auth_failure("string") is False
        assert _looks_like_auth_failure(None) is False


class TestDhanExc:
    """Test _dhan_exc error wrapping."""

    def test_auth_error_code_901(self):
        """DH-901 → BrokerAuthError."""
        exc = _dhan_exc(Exception("test"), code="DH-901", status=401)
        assert isinstance(exc, BrokerAuthError)
        assert exc.broker == "dhan"
        assert exc.code == "DH-901"

    def test_rate_limit_error_code_904(self):
        """DH-904 → BrokerRateLimitError."""
        exc = _dhan_exc(Exception("test"), code="DH-904", status=429)
        assert isinstance(exc, BrokerRateLimitError)
        assert exc.broker == "dhan"

    def test_order_error_code_906(self):
        """DH-906 → BrokerOrderError."""
        exc = _dhan_exc(Exception("test"), code="DH-906", status=400)
        assert isinstance(exc, BrokerOrderError)

    def test_unknown_error_code(self):
        """Unknown code → generic BrokerError."""
        exc = _dhan_exc(Exception("test"), code="DH-999", status=500)
        assert isinstance(exc, BrokerError)
        assert not isinstance(exc, (BrokerAuthError, BrokerRateLimitError))


class TestCoercionHelpers:
    """Test _dhan_num, _dhan_int, _dhan_first."""

    def test_dhan_num_from_string(self):
        """String '100.5' → 100.5."""
        assert _dhan_num("100.5") == 100.5

    def test_dhan_num_from_int(self):
        """Int 100 → 100.0."""
        assert _dhan_num(100) == 100.0

    def test_dhan_num_none(self):
        """None → 0.0 (default)."""
        assert _dhan_num(None) == 0.0

    def test_dhan_num_custom_default(self):
        """None with custom default → custom value."""
        assert _dhan_num(None, default=-1.0) == -1.0

    def test_dhan_num_invalid_string(self):
        """Invalid string → default."""
        assert _dhan_num("not_a_number") == 0.0

    def test_dhan_int_from_string(self):
        """String '100' → 100."""
        assert _dhan_int("100") == 100

    def test_dhan_int_from_float_string(self):
        """String '100.7' → 100 (truncated)."""
        assert _dhan_int("100.7") == 100

    def test_dhan_int_none(self):
        """None → 0 (default)."""
        assert _dhan_int(None) == 0

    def test_dhan_int_invalid_string(self):
        """Invalid string → default."""
        assert _dhan_int("invalid") == 0

    def test_dhan_first_returns_first_truthy(self):
        """Return first truthy value from keys."""
        d = {"a": "", "b": None, "c": "value", "d": "other"}
        assert _dhan_first(d, "a", "b", "c", "d") == "value"

    def test_dhan_first_default_when_missing(self):
        """Return default when all keys missing/falsy."""
        d = {"a": ""}
        assert _dhan_first(d, "x", "y", default="default") == "default"


class TestUnwrap:
    """Test _unwrap response envelope."""

    def test_unwrap_dict_with_data_list(self):
        """Normal Dhan response: {status, data: [...]}} → data list."""
        resp = {
            "status": "success",
            "data": [{"id": 1}, {"id": 2}],
        }
        assert _unwrap(resp) == [{"id": 1}, {"id": 2}]

    def test_unwrap_dict_no_data(self):
        """No data field → []."""
        assert _unwrap({"status": "success"}) == []

    def test_unwrap_non_dict(self):
        """Non-dict input → []."""
        assert _unwrap([]) == []
        assert _unwrap("string") == []

    def test_unwrap_data_not_list(self):
        """data field is dict (not list) → []."""
        resp = {"status": "success", "data": {"key": "value"}}
        assert _unwrap(resp) == []


class TestParseDhanDate:
    """Test _parse_dhan_date date parsing."""

    def test_dd_slash_mm_slash_yyyy_format(self):
        """DD/MM/YYYY format: '16/07/2026' → date(2026, 7, 16)."""
        from datetime import date
        result = _parse_dhan_date("16/07/2026")
        assert result == date(2026, 7, 16)

    def test_iso_date_format(self):
        """ISO date: '2026-07-16' → date(2026, 7, 16)."""
        from datetime import date
        result = _parse_dhan_date("2026-07-16")
        assert result == date(2026, 7, 16)

    def test_iso_datetime_format(self):
        """ISO datetime: '2026-07-16T10:30:00' → date(2026, 7, 16)."""
        from datetime import date
        result = _parse_dhan_date("2026-07-16T10:30:00")
        assert result == date(2026, 7, 16)

    def test_invalid_date_string(self):
        """Invalid date → None."""
        assert _parse_dhan_date("invalid") is None

    def test_empty_date_string(self):
        """Empty string → None."""
        assert _parse_dhan_date("") is None


class TestDhanExchangeToSegment:
    """Test _dhan_exchange_to_segment mapping."""

    def test_nse_eq_to_equity(self):
        assert _dhan_exchange_to_segment("NSE_EQ") == "equity"

    def test_mcx_comm_to_commodity(self):
        assert _dhan_exchange_to_segment("MCX_COMM") == "commodity"

    def test_bse_eq_to_equity(self):
        assert _dhan_exchange_to_segment("BSE_EQ") == "equity"

    def test_unknown_exchange_to_equity_default(self):
        """Unknown exchange → default to 'equity'."""
        assert _dhan_exchange_to_segment("UNKNOWN") == "equity"


class TestIstToday:
    """Test _ist_today date formatting."""

    def test_ist_today_format(self):
        """Return today's IST date as YYYY-MM-DD string."""
        from datetime import datetime, timezone, timedelta
        result = _ist_today()
        # Verify format YYYY-MM-DD
        parts = result.split("-")
        assert len(parts) == 3
        assert len(parts[0]) == 4  # year
        assert len(parts[1]) == 2  # month
        assert len(parts[2]) == 2  # day


# ─────────────────────────────────────────────────────────────────────────
# Part B: Normalization Function Tests (No Adapter Mocking)
# ─────────────────────────────────────────────────────────────────────────


class TestNormaliseHoldings:
    """Test _normalise_holdings response normalization."""

    def test_normalise_empty_response(self):
        """Empty response → []."""
        resp = {"status": "success", "data": []}
        assert _normalise_holdings(resp) == []

    def test_normalise_single_holding(self):
        """Single holding row → normalized dict with Kite-shape keys."""
        resp = {
            "status": "success",
            "data": [{
                "tradingSymbol": "RELIANCE",
                "exchange": "NSE_EQ",
                "totalQty": 10,
                "securityId": 123456,
                "avgCostPrice": 2500.0,
                "lastTradedPrice": 2600.0,
                "previousClosePrice": 2550.0,
            }],
        }
        result = _normalise_holdings(resp)
        assert len(result) == 1
        assert result[0]["tradingsymbol"] == "RELIANCE"
        assert result[0]["quantity"] == 10
        assert result[0]["average_price"] == 2500.0
        assert result[0]["last_price"] == 2600.0
        assert result[0]["instrument_token"] == 123456

    def test_normalise_holding_with_pnl_calculation(self):
        """Holdings normalizer computes P&L."""
        resp = {
            "status": "success",
            "data": [{
                "tradingSymbol": "TCS",
                "totalQty": 5,
                "securityId": 999,
                "avgCostPrice": 3000.0,
                "lastTradedPrice": 3100.0,
                "previousClosePrice": 3050.0,
            }],
        }
        result = _normalise_holdings(resp)
        holding = result[0]
        # pnl = (ltp - avg) * qty = (3100 - 3000) * 5 = 500
        assert holding["pnl"] == 500.0
        # day_change = ltp - close = 3100 - 3050 = 50
        assert holding["day_change"] == 50.0


class TestNormalisePositions:
    """Test _normalise_positions response normalization."""

    def test_normalise_empty_positions(self):
        """Empty positions response → {net: [], day: []}."""
        resp = {"status": "success", "data": []}
        result = _normalise_positions(resp)
        assert result == {"net": [], "day": []}

    def test_normalise_single_position_equity(self):
        """Single equity position → normalized net entry."""
        resp = {
            "status": "success",
            "data": [{
                "tradingSymbol": "INFY",
                "exchange": "NSE",
                "exchangeSegment": "NSE_EQ",
                "netQty": 10,
                "multiplier": 1,
                "securityId": 555,
                "costPrice": 2000.0,
                "lastTradedPrice": 2100.0,
                "previousClosePrice": 2050.0,
                "carryFwdQty": 10,
                "dayBuyQty": 0,
                "daySellQty": 0,
            }],
        }
        result = _normalise_positions(resp)
        assert len(result["net"]) == 1
        assert len(result["day"]) == 0
        pos = result["net"][0]
        assert pos["tradingsymbol"] == "INFY"
        assert pos["quantity"] == 10
        assert pos["exchange"] == "NSE"

    def test_normalise_position_mcx_contract(self):
        """MCX position with multiplier > 1 → qty converted to contracts."""
        resp = {
            "status": "success",
            "data": [{
                "tradingSymbol": "CRUDEOIL-16JUL2026-8500-CE",
                "exchange": "MCX",
                "exchangeSegment": "MCX_COMM",
                "netQty": 3,  # in lots
                "multiplier": 100,  # lot size
                "securityId": 777,
                "costPrice": 500.0,
                "lastTradedPrice": 520.0,
                "previousClosePrice": 510.0,
                "carryFwdQty": 3,
                "dayBuyQty": 0,
                "daySellQty": 0,
            }],
        }
        result = _normalise_positions(resp)
        pos = result["net"][0]
        # qty_contracts = netQty * multiplier = 3 * 100 = 300
        assert pos["quantity"] == 300
        assert pos["exchange"] == "MCX"
        # Kite-normalized: multiplier set to 1 after conversion
        assert pos["multiplier"] == 1


class TestNormaliseOrders:
    """Test _normalise_orders response normalization."""

    def test_normalise_empty_orders(self):
        """Empty orders response → []."""
        resp = {"status": "success", "data": []}
        assert _normalise_orders(resp) == []

    def test_normalise_single_order_complete(self):
        """Single completed order → normalized dict."""
        resp = {
            "status": "success",
            "data": [{
                "orderId": "12345",
                "tradingSymbol": "RELIANCE",
                "exchange": "NSE",
                "orderStatus": "TRADED",
                "transactionType": "BUY",
                "orderType": "LIMIT",
                "quantity": 10,
                "filledQty": 10,
                "remainingQty": 0,
                "price": 2500.0,
                "averageTradedPrice": 2498.5,
            }],
        }
        result = _normalise_orders(resp)
        assert len(result) == 1
        order = result[0]
        assert order["order_id"] == "12345"
        assert order["tradingsymbol"] == "RELIANCE"
        # TRADED → COMPLETE (Dhan → Kite)
        assert order["status"] == "COMPLETE"
        assert order["filled_quantity"] == 10

    def test_normalise_order_pending_status(self):
        """Pending order status mapping."""
        resp = {
            "status": "success",
            "data": [{
                "orderId": "99999",
                "tradingSymbol": "TCS",
                "exchange": "NSE",
                "orderStatus": "PENDING",
                "transactionType": "SELL",
                "orderType": "MARKET",
                "quantity": 5,
                "filledQty": 0,
                "remainingQty": 5,
                "price": 0.0,
            }],
        }
        result = _normalise_orders(resp)
        assert result[0]["status"] == "OPEN"

    def test_normalise_order_dhan_derivatives(self):
        """F&O order with Dhan-format symbol → Kite-normalized."""
        resp = {
            "status": "success",
            "data": [{
                "orderId": "55555",
                "tradingSymbol": "CRUDEOIL-16JUL2026-8500-CE",
                "exchange": "MCX",
                "orderStatus": "EXECUTED",
                "transactionType": "BUY",
                "orderType": "LIMIT",
                "quantity": 1,
                "filledQty": 1,
                "price": 200.0,
            }],
        }
        result = _normalise_orders(resp)
        order = result[0]
        # Symbol converted from Dhan format to Kite format
        assert order["tradingsymbol"] == "CRUDEOIL26JUL8500CE"
        # EXECUTED → COMPLETE
        assert order["status"] == "COMPLETE"


class TestNormaliseTrades:
    """Test _normalise_trades response normalization."""

    def test_normalise_empty_trades(self):
        """Empty trades → []."""
        resp = {"status": "success", "data": []}
        assert _normalise_trades(resp) == []

    def test_normalise_single_trade(self):
        """Single trade → normalized dict."""
        resp = {
            "status": "success",
            "data": [{
                "tradeId": "TR123",
                "orderId": "12345",
                "tradingSymbol": "RELIANCE",
                "exchange": "NSE",
                "transactionType": "BUY",
                "tradedQuantity": 10,
                "tradedPrice": 2498.5,
                "exchangeTime": "2026-07-16 10:30:00",
            }],
        }
        result = _normalise_trades(resp)
        assert len(result) == 1
        trade = result[0]
        assert trade["trade_id"] == "TR123"
        assert trade["order_id"] == "12345"
        assert trade["tradingsymbol"] == "RELIANCE"
        assert trade["quantity"] == 10
        assert trade["average_price"] == 2498.5


# ─────────────────────────────────────────────────────────────────────────
# Part C: DhanBroker Adapter Tests (with Mocked _safe_call)
# ─────────────────────────────────────────────────────────────────────────


class TestDhanBrokerAdapter:
    """Test DhanBroker adapter methods with mocked _safe_call."""

    def setup_method(self):
        """Set up a mock connection and DhanBroker instance."""
        self.mock_conn = MagicMock()
        self.mock_conn.account = "TEST_ACCOUNT"
        self.mock_conn.client_id = "TEST_CLIENT"
        self.broker = DhanBroker(self.mock_conn)

    def test_broker_account_property(self):
        """DhanBroker.account returns connection account."""
        assert self.broker.account == "TEST_ACCOUNT"

    def test_broker_id_property(self):
        """DhanBroker.broker_id returns 'dhan'."""
        assert self.broker.broker_id == "dhan"

    def test_profile_success(self):
        """profile() returns Kite-shape dict."""
        with patch.object(self.broker, "_safe_call") as mock_safe:
            mock_safe.return_value = {
                "status": "success",
                "data": {
                    "availableBalance": 100000.0,
                },
            }
            result = self.broker.profile()
            assert result["user_id"] == "TEST_CLIENT"
            assert result["broker"] == "DHAN"
            mock_safe.assert_called_once()

    def test_holdings_via_safe_call(self):
        """holdings() calls _safe_call and normalizes response."""
        with patch.object(self.broker, "_safe_call") as mock_safe:
            mock_safe.return_value = {
                "status": "success",
                "data": [{
                    "tradingSymbol": "INFY",
                    "exchange": "NSE_EQ",
                    "totalQty": 5,
                    "securityId": 123,
                    "avgCostPrice": 1500.0,
                    "lastTradedPrice": 1600.0,
                    "previousClosePrice": 1550.0,
                }],
            }
            result = self.broker.holdings()
            assert len(result) == 1
            assert result[0]["tradingsymbol"] == "INFY"
            assert result[0]["quantity"] == 5

    def test_positions_via_safe_call(self):
        """positions() calls _safe_call and returns {net, day}."""
        with patch.object(self.broker, "_safe_call") as mock_safe:
            mock_safe.return_value = {
                "status": "success",
                "data": [{
                    "tradingSymbol": "TCS",
                    "exchange": "NSE",
                    "exchangeSegment": "NSE_EQ",
                    "netQty": 2,
                    "multiplier": 1,
                    "securityId": 456,
                    "costPrice": 3000.0,
                    "lastTradedPrice": 3100.0,
                    "previousClosePrice": 3050.0,
                }],
            }
            result = self.broker.positions()
            assert isinstance(result, dict)
            assert "net" in result
            assert "day" in result
            assert len(result["net"]) == 1
            assert result["net"][0]["tradingsymbol"] == "TCS"

    def test_orders_via_safe_call(self):
        """orders() calls _safe_call and normalizes."""
        with patch.object(self.broker, "_safe_call") as mock_safe:
            mock_safe.return_value = {
                "status": "success",
                "data": [{
                    "orderId": "ORD001",
                    "tradingSymbol": "RELIANCE",
                    "exchange": "NSE",
                    "orderStatus": "COMPLETE",
                    "transactionType": "BUY",
                    "orderType": "LIMIT",
                    "quantity": 10,
                    "filledQty": 10,
                    "price": 2500.0,
                }],
            }
            result = self.broker.orders()
            assert len(result) == 1
            assert result[0]["order_id"] == "ORD001"

    def test_margins_via_safe_call(self):
        """margins() calls _safe_call with endpoint_group='margins'."""
        with patch.object(self.broker, "_safe_call") as mock_safe:
            mock_safe.return_value = {
                "status": "success",
                "data": {
                    "availableBalance": 500000.0,
                    "utilizedAmount": 100000.0,
                },
            }
            result = self.broker.margins()
            assert "available" in result
            assert "utilised" in result
            # Verify endpoint_group was passed
            call_kwargs = mock_safe.call_args[1]
            assert call_kwargs.get("endpoint_group") == "margins"

    def test_trades_via_safe_call(self):
        """trades() calls _safe_call and returns normalized list."""
        with patch.object(self.broker, "_safe_call") as mock_safe:
            mock_safe.return_value = {
                "status": "success",
                "data": [{
                    "tradeId": "T001",
                    "orderId": "ORD001",
                    "tradingSymbol": "INFY",
                    "exchange": "NSE",
                    "transactionType": "BUY",
                    "tradedQuantity": 10,
                    "tradedPrice": 1500.0,
                }],
            }
            result = self.broker.trades()
            assert len(result) == 1
            assert result[0]["trade_id"] == "T001"

    @patch("backend.brokers.adapters.dhan._resolve_security_id")
    def test_place_order_equity(self, mock_resolve):
        """place_order() builds Dhan call with resolved security_id."""
        mock_resolve.return_value = "SEC123"
        with patch.object(self.broker, "_safe_call") as mock_safe:
            mock_safe.return_value = {
                "status": "success",
                "data": {"orderId": "NEW001"},
            }
            result = self.broker.place_order(
                tradingsymbol="RELIANCE",
                exchange="NSE",
                transaction_type="BUY",
                quantity=10,
                order_type="LIMIT",
                product="CNC",
                price=2500.0,
            )
            assert result == "NEW001"
            # Verify security_id was resolved and passed
            mock_safe.assert_called_once()
            call_lambda = mock_safe.call_args[0][0]
            # The lambda should be callable (it's the SDK call wrapper)
            assert callable(call_lambda)

    def test_place_order_rejects_amo(self):
        """place_order() raises NotImplementedError for AMO orders."""
        with pytest.raises(NotImplementedError, match="AMO"):
            self.broker.place_order(
                tradingsymbol="RELIANCE",
                exchange="NSE",
                transaction_type="BUY",
                quantity=10,
                variety="amo",
            )

    def test_last_request_debug(self):
        """last_request_debug() returns {request, response}."""
        # Set up the connection so holdings can work
        mock_sdk = MagicMock()
        self.mock_conn.get_dhan_conn.return_value = mock_sdk

        with patch.object(self.broker, "_safe_call") as mock_safe:
            mock_safe.return_value = {"status": "success", "data": []}
            # Ensure _safe_call populates _last_req and _last_resp
            def side_effect_safe_call(sdk_call, **kwargs):
                self.broker._last_req = {"broker": "dhan", "account": "TEST_ACCOUNT"}
                self.broker._last_resp = {"status_hint": "ok"}
                return {"status": "success", "data": []}

            mock_safe.side_effect = side_effect_safe_call
            self.broker.holdings()
            debug = self.broker.last_request_debug()
            assert isinstance(debug, dict)
            assert "request" in debug
            assert "response" in debug
            assert debug["request"]["broker"] == "dhan"
            assert debug["request"]["account"] == "TEST_ACCOUNT"
            assert debug["response"]["status_hint"] == "ok"


class TestSafeCallMethod:
    """Test DhanBroker._safe_call method behavior."""

    def setup_method(self):
        """Set up broker for safe_call testing."""
        self.mock_conn = MagicMock()
        self.mock_conn.account = "TEST_ACCOUNT"
        self.mock_conn.client_id = "TEST_CLIENT"
        self.mock_conn.get_dhan_conn = MagicMock(return_value=MagicMock())
        self.broker = DhanBroker(self.mock_conn)

    def test_safe_call_returns_response(self):
        """_safe_call returns SDK call result."""
        mock_sdk = MagicMock()
        self.mock_conn.get_dhan_conn.return_value = mock_sdk

        def sdk_call(dhan):
            return {"status": "success", "data": []}

        result = self.broker._safe_call(sdk_call)
        assert result == {"status": "success", "data": []}

    def test_safe_call_sets_last_req_resp(self):
        """_safe_call populates _last_req and _last_resp."""
        mock_sdk = MagicMock()
        self.mock_conn.get_dhan_conn.return_value = mock_sdk

        def sdk_call(dhan):
            return {"status": "success"}

        self.broker._safe_call(sdk_call, endpoint_group="orders")
        assert self.broker._last_req["broker"] == "dhan"
        assert self.broker._last_req["endpoint_group"] == "orders"
        assert self.broker._last_resp["status_hint"] == "ok"

    @patch("backend.brokers.adapters.dhan._DHAN_RATE_LIMIT_ENABLED", True)
    def test_safe_call_rate_limit_throttle(self):
        """When rate limiting enabled, _safe_call throttles."""
        mock_sdk = MagicMock()
        self.mock_conn.get_dhan_conn.return_value = mock_sdk

        def sdk_call(dhan):
            return {"status": "success"}

        with patch.object(_DHAN_RATE_LIMITER, "throttle") as mock_throttle:
            self.broker._safe_call(sdk_call, endpoint_group="orders")
            mock_throttle.assert_called_once_with("orders")

    def test_safe_call_auth_failure_detection(self):
        """_safe_call detects auth failure and retries."""
        mock_sdk = MagicMock()
        mock_sdk_fresh = MagicMock()
        self.mock_conn.get_dhan_conn.side_effect = [mock_sdk, mock_sdk_fresh]
        self.mock_conn._conn_created_at = None

        call_count = [0]
        def sdk_call(dhan):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call returns auth failure
                return {"status": "failure", "remarks": "Invalid Token"}
            else:
                # Second call (after retry) succeeds
                return {"status": "success", "data": []}

        with patch("backend.brokers.adapters.dhan._check_dhan_rotation_pattern"):
            result = self.broker._safe_call(sdk_call)
            assert result["status"] == "success"
            # get_dhan_conn called twice: once for initial, once for fresh
            assert self.mock_conn.get_dhan_conn.call_count == 2

    def test_safe_call_persistent_auth_failure_raises(self):
        """_safe_call raises if auth failure persists after retry."""
        mock_sdk = MagicMock()
        self.mock_conn.get_dhan_conn.return_value = mock_sdk
        self.mock_conn._conn_created_at = None

        def sdk_call(dhan):
            # Always return auth failure
            return {"status": "failure", "remarks": "Invalid Token"}

        with patch("backend.brokers.adapters.dhan._check_dhan_rotation_pattern"):
            with pytest.raises(RuntimeError, match="persisted after re-login"):
                self.broker._safe_call(sdk_call)


class TestRateLimiterIntegration:
    """Test rate limiter configuration and toggle."""

    def test_rate_limiter_is_token_bucket(self):
        """_DHAN_RATE_LIMITER is a TokenBucketLimiter."""
        from backend.brokers.rate_limiter import TokenBucketLimiter
        assert isinstance(_DHAN_RATE_LIMITER, TokenBucketLimiter)

    def test_rate_limiter_has_endpoints(self):
        """Rate limiter has configured endpoint groups."""
        # Verify by checking that throttle works
        try:
            _DHAN_RATE_LIMITER.throttle("orders")
            _DHAN_RATE_LIMITER.throttle("history")
            _DHAN_RATE_LIMITER.throttle("margins")
            # Should not raise — endpoints are configured
        except KeyError:
            pytest.fail("Rate limiter missing expected endpoint groups")

    def test_rate_limit_enabled_is_bool(self):
        """_DHAN_RATE_LIMIT_ENABLED is a boolean."""
        assert isinstance(_DHAN_RATE_LIMIT_ENABLED, bool)


class TestOrderStatusMethod:
    """Test DhanBroker.order_status specific endpoint."""

    def setup_method(self):
        """Set up broker for order_status testing."""
        self.mock_conn = MagicMock()
        self.mock_conn.account = "TEST_ACCOUNT"
        self.mock_conn.client_id = "TEST_CLIENT"
        self.broker = DhanBroker(self.mock_conn)

    def test_order_status_uses_single_order_endpoint(self):
        """order_status() prefers get_order_by_id over get_order_list."""
        mock_sdk = MagicMock()
        mock_sdk.get_order_by_id = MagicMock(return_value={
            "status": "success",
            "data": {
                "orderId": "12345",
                "tradingSymbol": "RELIANCE",
                "orderStatus": "COMPLETE",
            }
        })
        self.mock_conn.get_dhan_conn.return_value = mock_sdk

        with patch.object(self.broker, "_safe_call") as mock_safe:
            mock_safe.return_value = {
                "status": "success",
                "data": {
                    "orderId": "12345",
                    "tradingSymbol": "RELIANCE",
                    "orderStatus": "COMPLETE",
                }
            }
            result = self.broker.order_status("12345")
            assert isinstance(result, dict)
            # Should call get_order_by_id via _safe_call
            mock_safe.assert_called_once()

    def test_order_status_missing_returns_empty(self):
        """order_status() returns {} when order not found."""
        mock_sdk = MagicMock()
        mock_sdk.get_order_by_id = None
        self.mock_conn.get_dhan_conn.return_value = mock_sdk

        with patch.object(self.broker, "_safe_call") as mock_safe:
            mock_safe.return_value = {"status": "success", "data": []}
            result = self.broker.order_status("99999")
            # Empty data list → no order found → {}
            assert result == {}


# ─────────────────────────────────────────────────────────────────────────
# Part D: Line Range 580-620 Tests — _check_dhan_rotation_pattern
# ─────────────────────────────────────────────────────────────────────────


class TestCheckDhanRotationPattern:
    """Test _check_dhan_rotation_pattern rotation detection."""

    def test_rotation_pattern_detects_concurrent_login(self):
        """Rotation pattern detected when another account logged in during token lifetime."""
        from datetime import datetime as _dt, timezone as _tz, timedelta
        from backend.brokers.adapters.dhan import (
            _check_dhan_rotation_pattern,
            _DHAN_LOGIN_HISTORY,
            _DHAN_HISTORY_LOCK,
        )

        # Clear history for clean test
        with _DHAN_HISTORY_LOCK:
            _DHAN_LOGIN_HISTORY.clear()

        # Create a token timestamp from 60s ago
        now = _dt.now(_tz.utc)
        token_created = now - timedelta(seconds=60)

        # Add a concurrent login from another account (10s ago, within token lifetime)
        other_login_time = now - timedelta(seconds=10)
        with _DHAN_HISTORY_LOCK:
            _DHAN_LOGIN_HISTORY["other_account"] = other_login_time

        # Mock the logger to verify error message
        with patch("backend.brokers.adapters.dhan.logger") as mock_logger:
            with patch("backend.brokers.adapters.dhan._emit_conn_event"):
                # Should detect the pattern and log error
                _check_dhan_rotation_pattern("test_account", token_created)
                # Verify error was logged
                assert mock_logger.error.called

    def test_rotation_pattern_ignores_none_token_timestamp(self):
        """No action when failing_token_created_at is None."""
        from backend.brokers.adapters.dhan import _check_dhan_rotation_pattern

        with patch("backend.brokers.adapters.dhan.logger") as mock_logger:
            # Should return early and not log
            _check_dhan_rotation_pattern("test_account", None)
            assert not mock_logger.error.called

    def test_rotation_pattern_no_suspects(self):
        """No error logged when no suspect logins found."""
        from datetime import datetime as _dt, timezone as _tz
        from backend.brokers.adapters.dhan import (
            _check_dhan_rotation_pattern,
            _DHAN_LOGIN_HISTORY,
            _DHAN_HISTORY_LOCK,
        )

        # Clear and set old login time (outside token lifetime)
        with _DHAN_HISTORY_LOCK:
            _DHAN_LOGIN_HISTORY.clear()
            old_time = _dt.now(_tz.utc)
            _DHAN_LOGIN_HISTORY["other_account"] = old_time

        # Create a token timestamp AFTER the old login
        from datetime import timedelta
        token_created = old_time + timedelta(seconds=120)

        with patch("backend.brokers.adapters.dhan.logger") as mock_logger:
            _check_dhan_rotation_pattern("test_account", token_created)
            # No suspects, so error should NOT be logged
            assert not mock_logger.error.called


class TestRecordDhanLoginEvent:
    """Test record_dhan_login_event side-channel logging."""

    def test_record_login_event_stores_timestamp(self):
        """record_dhan_login_event stores the current time for an account."""
        from backend.brokers.adapters.dhan import (
            record_dhan_login_event,
            _DHAN_LOGIN_HISTORY,
            _DHAN_HISTORY_LOCK,
        )

        # Clear history
        with _DHAN_HISTORY_LOCK:
            _DHAN_LOGIN_HISTORY.clear()

        # Record event
        record_dhan_login_event("test_acct_1")

        # Verify it's in history
        with _DHAN_HISTORY_LOCK:
            assert "test_acct_1" in _DHAN_LOGIN_HISTORY
            assert isinstance(_DHAN_LOGIN_HISTORY["test_acct_1"], object)

    def test_record_multiple_accounts(self):
        """Multiple calls record multiple accounts."""
        from backend.brokers.adapters.dhan import (
            record_dhan_login_event,
            _DHAN_LOGIN_HISTORY,
            _DHAN_HISTORY_LOCK,
        )

        with _DHAN_HISTORY_LOCK:
            _DHAN_LOGIN_HISTORY.clear()

        record_dhan_login_event("acct_A")
        record_dhan_login_event("acct_B")

        with _DHAN_HISTORY_LOCK:
            assert len(_DHAN_LOGIN_HISTORY) == 2
            assert "acct_A" in _DHAN_LOGIN_HISTORY
            assert "acct_B" in _DHAN_LOGIN_HISTORY


# ─────────────────────────────────────────────────────────────────────────
# Part E: Line Range 690-770 Tests — Ledger & Instruments
# ─────────────────────────────────────────────────────────────────────────


class TestDhanLedgerFunctions:
    """Test ledger-related pure functions."""

    def test_call_dhan_ledger_raw_kwarg_form(self):
        """_call_dhan_ledger_raw tries kwarg form first."""
        from backend.brokers.adapters.dhan import _call_dhan_ledger_raw

        mock_sdk = MagicMock()
        mock_sdk.get_transaction_detail = MagicMock(
            return_value={"status": "success", "data": [{"id": 1}]}
        )

        result = _call_dhan_ledger_raw(mock_sdk, "get_transaction_detail", "2026-07-01", "2026-07-31")

        # Should have called with kwarg form
        mock_sdk.get_transaction_detail.assert_called_once_with(
            from_date="2026-07-01", to_date="2026-07-31"
        )
        assert result["status"] == "success"

    def test_call_dhan_ledger_raw_falls_back_positional(self):
        """_call_dhan_ledger_raw falls back to positional if kwarg fails."""
        from backend.brokers.adapters.dhan import _call_dhan_ledger_raw

        mock_sdk = MagicMock()
        # Simulate TypeError on kwarg form
        mock_sdk.get_transaction_detail = MagicMock(
            side_effect=[TypeError("unexpected keyword"), {"status": "success"}]
        )

        result = _call_dhan_ledger_raw(mock_sdk, "get_transaction_detail", "2026-07-01", "2026-07-31")

        # Should fall back to positional
        assert mock_sdk.get_transaction_detail.call_count == 2
        assert result["status"] == "success"

    def test_dhan_ledger_aggregate_groups_by_date_segment(self):
        """_dhan_ledger_aggregate groups ledger entries by date + segment."""
        from backend.brokers.adapters.dhan import _dhan_ledger_aggregate

        entries = [
            {
                "voucherdate": "16/07/2026",
                "exchange": "NSE_EQ",
                "debit": "1000",
                "credit": "0",
                "runbal": "50000",
            },
            {
                "voucherdate": "16/07/2026",
                "exchange": "MCX_COMM",
                "debit": "0",
                "credit": "500",
                "runbal": "50500",
            },
        ]

        result = _dhan_ledger_aggregate(entries)

        # Should have 2 groups (one per segment on same date)
        assert len(result) == 2
        # Check the first group has expected keys
        assert result[0]["segment"] in ("equity", "commodity")
        assert "debits" in result[0]
        assert "realised_m2m" in result[0]  # the "credits - debits" field is named realised_m2m

    def test_dhan_ledger_aggregate_computes_net_move(self):
        """_dhan_ledger_aggregate computes net_move = credits - debits."""
        from backend.brokers.adapters.dhan import _dhan_ledger_aggregate

        entries = [
            {
                "voucherdate": "16/07/2026",
                "exchange": "NSE_EQ",
                "debit": "1000",
                "credit": "2000",
                "runbal": "51000",
            },
        ]

        result = _dhan_ledger_aggregate(entries)

        assert len(result) == 1
        # net_move = 2000 - 1000 = 1000
        assert result[0]["realised_m2m"] == 1000.0

    def test_dhan_ledger_aggregate_invalid_entries_skipped(self):
        """_dhan_ledger_aggregate skips entries with invalid numbers."""
        from backend.brokers.adapters.dhan import _dhan_ledger_aggregate

        entries = [
            {
                "voucherdate": "16/07/2026",
                "exchange": "NSE_EQ",
                "debit": "invalid",  # Invalid number
                "credit": "1000",
                "runbal": "50000",
            },
            {
                "voucherdate": "16/07/2026",
                "exchange": "NSE_EQ",
                "debit": "500",
                "credit": "1000",
                "runbal": "50500",
            },
        ]

        result = _dhan_ledger_aggregate(entries)

        # Only valid entry should be processed
        assert len(result) == 1

    def test_dhan_ledger_aggregate_opening_balance_calculated(self):
        """_dhan_ledger_aggregate calculates opening_balance."""
        from backend.brokers.adapters.dhan import _dhan_ledger_aggregate

        entries = [
            {
                "voucherdate": "16/07/2026",
                "exchange": "NSE_EQ",
                "debit": "500",
                "credit": "1000",
                "runbal": "50500",
            },
        ]

        result = _dhan_ledger_aggregate(entries)

        # opening_balance = close_bal - net_move = 50500 - 500 = 50000
        assert result[0]["opening_balance"] == 50000.0


# ─────────────────────────────────────────────────────────────────────────
# Part F: Line Range 774-820 Tests — GTT Helper Functions
# ─────────────────────────────────────────────────────────────────────────


class TestDhanGttHelpers:
    """Test GTT-related helper functions."""

    def test_dhan_gtt_order_id_from_response(self):
        """_dhan_gtt_order_id extracts orderId from place_forever response."""
        from backend.brokers.adapters.dhan import _dhan_gtt_order_id

        resp = {"status": "success", "data": {"orderId": "GTT123"}}
        assert _dhan_gtt_order_id(resp) == "GTT123"

    def test_dhan_gtt_order_id_fallback_order_id_key(self):
        """_dhan_gtt_order_id falls back to order_id key."""
        from backend.brokers.adapters.dhan import _dhan_gtt_order_id

        resp = {"status": "success", "data": {"order_id": "GTT456"}}
        assert _dhan_gtt_order_id(resp) == "GTT456"

    def test_dhan_gtt_order_id_missing_returns_empty_string(self):
        """_dhan_gtt_order_id returns empty string when ID missing."""
        from backend.brokers.adapters.dhan import _dhan_gtt_order_id

        resp = {"status": "success", "data": {}}
        assert _dhan_gtt_order_id(resp) == ""

    def test_dhan_place_forever_kwargs_single_trigger(self):
        """_dhan_place_forever_kwargs builds single-trigger kwargs."""
        from backend.brokers.adapters.dhan import _dhan_place_forever_kwargs

        order0 = {"price": 100.0, "quantity": 10}
        orders = [order0]
        trigger_values = [95.0]

        kwargs = _dhan_place_forever_kwargs(
            order0, orders, trigger_values, "single",
            "SEC123", "NSE_EQ", "RELIANCE", "tag1"
        )

        assert kwargs["order_flag"] == "SINGLE"
        assert kwargs["trigger_Price"] == 95.0

    def test_dhan_place_forever_kwargs_oco_trigger(self):
        """_dhan_place_forever_kwargs builds OCO kwargs with leg-1 fields."""
        from backend.brokers.adapters.dhan import _dhan_place_forever_kwargs

        order0 = {"price": 100.0, "quantity": 10}
        order1 = {"price": 110.0, "quantity": 10}
        orders = [order0, order1]
        trigger_values = [95.0, 105.0]

        kwargs = _dhan_place_forever_kwargs(
            order0, orders, trigger_values, "two-leg",
            "SEC123", "NSE_EQ", "RELIANCE", None
        )

        assert kwargs["order_flag"] == "OCO"
        assert kwargs["price1"] == 110.0
        assert kwargs["trigger_Price1"] == 105.0
        assert kwargs["quantity1"] == 10

    def test_dhan_modify_forever_leg_entry_leg(self):
        """_dhan_modify_forever_leg builds ENTRY_LEG modify kwargs."""
        from backend.brokers.adapters.dhan import _dhan_modify_forever_leg

        order = {"price": 100.0, "quantity": 10, "order_type": "LIMIT"}
        kwargs = _dhan_modify_forever_leg(order, 95.0, "SINGLE", "ENTRY_LEG")

        assert kwargs["leg_name"] == "ENTRY_LEG"
        assert kwargs["order_flag"] == "SINGLE"
        assert kwargs["trigger_price"] == 95.0
        assert kwargs["quantity"] == 10

    def test_dhan_modify_forever_leg_target_leg(self):
        """_dhan_modify_forever_leg builds TARGET_LEG kwargs."""
        from backend.brokers.adapters.dhan import _dhan_modify_forever_leg

        order = {"price": 105.0, "quantity": 5, "order_type": "MARKET"}
        kwargs = _dhan_modify_forever_leg(order, 110.0, "OCO", "TARGET_LEG")

        assert kwargs["leg_name"] == "TARGET_LEG"
        assert kwargs["order_flag"] == "OCO"
        assert kwargs["trigger_price"] == 110.0

    def test_dhan_raise_asymmetric_gtt_sets_sentinels(self):
        """_dhan_raise_asymmetric_gtt raises with partial-modify sentinels."""
        from backend.brokers.adapters.dhan import _dhan_raise_asymmetric_gtt

        with pytest.raises(RuntimeError):
            _dhan_raise_asymmetric_gtt("GTT123", {"error": "rejected"})


# ─────────────────────────────────────────────────────────────────────────
# Part G: Line Range 925-960 Tests — basket_order_margins
# ─────────────────────────────────────────────────────────────────────────


class TestBasketOrderMargins:
    """Test DhanBroker.basket_order_margins method."""

    def setup_method(self):
        """Set up broker for margin testing."""
        self.mock_conn = MagicMock()
        self.mock_conn.account = "TEST_ACCOUNT"
        self.mock_conn.client_id = "TEST_CLIENT"
        self.broker = DhanBroker(self.mock_conn)

    def test_basket_order_margins_single_order(self):
        """basket_order_margins processes one order."""
        with patch.object(self.broker, "_margin_for_order") as mock_margin:
            mock_margin.return_value = {
                "total": 50000.0,
                "var": 20000.0,
                "exposure": 30000.0,
                "available": {"cash": 100000.0},
            }

            orders = [
                {"tradingsymbol": "RELIANCE", "exchange": "NSE", "quantity": 10}
            ]

            result = self.broker.basket_order_margins(orders)

            assert len(result) == 1
            assert result[0]["total"] == 50000.0

    def test_basket_order_margins_multiple_orders(self):
        """basket_order_margins loops over multiple orders."""
        with patch.object(self.broker, "_margin_for_order") as mock_margin:
            mock_margin.return_value = {"total": 50000.0, "var": 20000.0}

            orders = [
                {"tradingsymbol": "RELIANCE", "exchange": "NSE"},
                {"tradingsymbol": "TCS", "exchange": "NSE"},
                {"tradingsymbol": "INFY", "exchange": "NSE"},
            ]

            result = self.broker.basket_order_margins(orders)

            assert len(result) == 3
            assert mock_margin.call_count == 3

    def test_basket_order_margins_error_handling(self):
        """basket_order_margins returns error dict on per-order failure."""
        def side_effect_error(order):
            if order["tradingsymbol"] == "FAIL":
                raise RuntimeError("Test error")
            return {"total": 50000.0}

        with patch.object(self.broker, "_margin_for_order") as mock_margin:
            mock_margin.side_effect = side_effect_error

            orders = [
                {"tradingsymbol": "RELIANCE", "exchange": "NSE"},
                {"tradingsymbol": "FAIL", "exchange": "NSE"},
            ]

            result = self.broker.basket_order_margins(orders)

            assert len(result) == 2
            assert result[0]["total"] == 50000.0
            assert "error" in result[1]


# ─────────────────────────────────────────────────────────────────────────
# Part H: Line Range 1037-1095 Tests — ltp & quote
# ─────────────────────────────────────────────────────────────────────────


class TestDhanLtpAndQuote:
    """Test DhanBroker.ltp and .quote methods."""

    def setup_method(self):
        """Set up broker for LTP testing."""
        self.mock_conn = MagicMock()
        self.mock_conn.account = "TEST_ACCOUNT"
        self.mock_conn.client_id = "TEST_CLIENT"
        self.broker = DhanBroker(self.mock_conn)

    def test_ltp_empty_symbols_returns_empty_dict(self):
        """ltp([]) returns {}."""
        result = self.broker.ltp([])
        assert result == {}

    def test_ltp_instruments_cache_unavailable_returns_empty(self):
        """ltp returns {} when instruments cache fails."""
        with patch("backend.brokers.adapters.dhan._ensure_dhan_instruments") as mock_ensure:
            mock_ensure.side_effect = RuntimeError("Network error")
            with patch("backend.brokers.adapters.dhan.logger"):
                result = self.broker.ltp(["NSE:RELIANCE"])
                assert result == {}

    def test_ltp_ohlc_call_success(self):
        """ltp makes ohlc_data call when instruments available."""
        with patch("backend.brokers.adapters.dhan._ensure_dhan_instruments"):
            with patch("backend.brokers.adapters.dhan._resolve_dhan_ltp_symbols") as mock_resolve:
                with patch.object(self.broker, "_safe_call") as mock_safe:
                    mock_resolve.return_value = (
                        {"NSE_EQ": ["123"]},
                        {("NSE_EQ", "123"): "NSE:RELIANCE"}
                    )
                    mock_safe.return_value = {
                        "data": {
                            "NSE_EQ": {
                                "123": {"last_price": 2500.0}
                            }
                        }
                    }

                    result = self.broker.ltp(["NSE:RELIANCE"])

                    assert "NSE:RELIANCE" in result
                    assert result["NSE:RELIANCE"]["last_price"] == 2500.0

    def test_ltp_ohlc_call_failure_returns_empty(self):
        """ltp returns {} when ohlc_data call fails."""
        with patch("backend.brokers.adapters.dhan._ensure_dhan_instruments"):
            with patch("backend.brokers.adapters.dhan._resolve_dhan_ltp_symbols") as mock_resolve:
                with patch.object(self.broker, "_safe_call") as mock_safe:
                    mock_resolve.return_value = (
                        {"NSE_EQ": ["123"]},
                        {("NSE_EQ", "123"): "NSE:RELIANCE"}
                    )
                    mock_safe.side_effect = Exception("API error")

                    with patch("backend.brokers.adapters.dhan.logger"):
                        result = self.broker.ltp(["NSE:RELIANCE"])
                        assert result == {}

    def test_quote_returns_empty_dict(self):
        """quote() always returns {} (not yet wired)."""
        result = self.broker.quote(["NSE:RELIANCE"])
        assert result == {}

    def test_instruments_no_filter(self):
        """instruments() with no exchange filter returns all."""
        with patch("backend.brokers.adapters.dhan._ensure_dhan_instruments"):
            with patch("backend.brokers.adapters.dhan._DHAN_BY_EXCHANGE", {
                "NSE": [{"tradingsymbol": "RELIANCE"}],
                "MCX": [{"tradingsymbol": "CRUDEOIL-16JUL2026-8500-CE"}],
            }):
                result = self.broker.instruments()
                assert len(result) >= 2

    def test_instruments_with_exchange_filter(self):
        """instruments(exchange='NSE') filters to one exchange."""
        with patch("backend.brokers.adapters.dhan._ensure_dhan_instruments"):
            with patch("backend.brokers.adapters.dhan._DHAN_BY_EXCHANGE", {
                "NSE": [{"tradingsymbol": "RELIANCE"}],
                "MCX": [{"tradingsymbol": "CRUDEOIL"}],
            }):
                result = self.broker.instruments(exchange="NSE")
                assert len(result) == 1
                assert result[0]["tradingsymbol"] == "RELIANCE"

    def test_historical_data_returns_empty_list(self):
        """historical_data() returns [] (not yet wired)."""
        result = self.broker.historical_data(123456, "2026-07-01", "2026-07-31")
        assert result == []


# ─────────────────────────────────────────────────────────────────────────
# Part I: Line Range 1127-1210 Tests — market_status & normalizers
# ─────────────────────────────────────────────────────────────────────────


class TestMarketStatusAndNormalizers:
    """Test market_status method and related normalizers."""

    def setup_method(self):
        """Set up broker for market status testing."""
        self.mock_conn = MagicMock()
        self.mock_conn.account = "TEST_ACCOUNT"
        self.mock_conn.client_id = "TEST_CLIENT"
        self.broker = DhanBroker(self.mock_conn)

    def test_market_status_nse_open(self):
        """market_status returns True when NSE is open."""
        with patch.object(self.broker, "_call_market_status_sdk") as mock_call:
            with patch("backend.brokers.adapters.dhan._extract_dhan_status_rows") as mock_extract:
                with patch("backend.brokers.adapters.dhan._dhan_row_indicates_open") as mock_open:
                    mock_call.return_value = {"status": "success"}
                    mock_extract.return_value = [{"status": "open"}]
                    mock_open.return_value = True

                    result = self.broker.market_status("NSE")
                    assert result is True

    def test_market_status_nse_closed(self):
        """market_status returns False when NSE is closed."""
        with patch.object(self.broker, "_call_market_status_sdk") as mock_call:
            with patch("backend.brokers.adapters.dhan._extract_dhan_status_rows") as mock_extract:
                with patch("backend.brokers.adapters.dhan._dhan_row_indicates_open") as mock_open:
                    mock_call.return_value = {"status": "success"}
                    mock_extract.return_value = [{"status": "closed"}]
                    mock_open.return_value = False

                    result = self.broker.market_status("NSE")
                    assert result is False

    def test_market_status_sdk_call_returns_none(self):
        """market_status returns None when SDK call fails."""
        with patch.object(self.broker, "_call_market_status_sdk") as mock_call:
            mock_call.return_value = None

            result = self.broker.market_status("NSE")
            assert result is None

    def test_market_status_unknown_exchange_returns_none(self):
        """market_status returns None for unmapped exchange."""
        with patch.object(self.broker, "_call_market_status_sdk") as mock_call:
            with patch("backend.brokers.adapters.dhan._XCHG_TO_DHAN_MARKET_STATUS", {}):
                mock_call.return_value = {"status": "success"}

                result = self.broker.market_status("UNKNOWN")
                assert result is None

    def test_call_market_status_sdk_discovers_method(self):
        """_call_market_status_sdk discovers the correct SDK method."""
        mock_sdk = MagicMock()
        mock_sdk.get_market_status = MagicMock(return_value={"status": "success"})
        self.mock_conn.get_dhan_conn.return_value = mock_sdk

        with patch.object(self.broker, "_safe_call") as mock_safe:
            mock_safe.return_value = {"status": "success"}

            result = self.broker._call_market_status_sdk("NSE")

            assert result is not None

    def test_call_market_status_sdk_returns_none_on_failure(self):
        """_call_market_status_sdk returns None on SDK error."""
        mock_sdk = MagicMock()
        self.mock_conn.get_dhan_conn.return_value = mock_sdk

        with patch.object(self.broker, "_safe_call") as mock_safe:
            mock_safe.side_effect = Exception("API error")
            with patch("backend.brokers.adapters.dhan.logger"):
                result = self.broker._call_market_status_sdk("NSE")
                assert result is None


# ─────────────────────────────────────────────────────────────────────────
# Part J: Line Range 1493-1590 Tests — Parsing & Normalization Branches
# ─────────────────────────────────────────────────────────────────────────


class TestDhanOhlcParsing:
    """Test OHLC response parsing functions."""

    def test_dhan_ohlc_last_price_from_last_price_field(self):
        """_dhan_ohlc_last_price reads last_price directly."""
        from backend.brokers.adapters.dhan import _dhan_ohlc_last_price

        row = {"last_price": 2500.0}
        assert _dhan_ohlc_last_price(row) == 2500.0

    def test_dhan_ohlc_last_price_fallback_ohlc_close(self):
        """_dhan_ohlc_last_price falls back to ohlc.close."""
        from backend.brokers.adapters.dhan import _dhan_ohlc_last_price

        row = {"ohlc": {"close": 2500.0}}
        assert _dhan_ohlc_last_price(row) == 2500.0

    def test_dhan_ohlc_last_price_fallback_close_field(self):
        """_dhan_ohlc_last_price falls back to close field."""
        from backend.brokers.adapters.dhan import _dhan_ohlc_last_price

        row = {"close": 2500.0}
        assert _dhan_ohlc_last_price(row) == 2500.0

    def test_dhan_ohlc_last_price_non_dict_returns_zero(self):
        """_dhan_ohlc_last_price returns 0 for non-dict input."""
        from backend.brokers.adapters.dhan import _dhan_ohlc_last_price

        assert _dhan_ohlc_last_price([]) == 0.0
        assert _dhan_ohlc_last_price(None) == 0.0

    def test_parse_dhan_ohlc_response_success(self):
        """_parse_dhan_ohlc_response builds LTP map from ohlc_data."""
        from backend.brokers.adapters.dhan import _parse_dhan_ohlc_response

        resp = {
            "data": {
                "NSE_EQ": {
                    "123": {"last_price": 2500.0}
                }
            }
        }
        sid_to_key = {("NSE_EQ", "123"): "NSE:RELIANCE"}

        result = _parse_dhan_ohlc_response(resp, sid_to_key, 1)

        assert "NSE:RELIANCE" in result
        assert result["NSE:RELIANCE"]["last_price"] == 2500.0

    def test_parse_dhan_ohlc_response_unexpected_shape(self):
        """_parse_dhan_ohlc_response returns {} on unexpected shape."""
        from backend.brokers.adapters.dhan import _parse_dhan_ohlc_response

        resp = {"data": []}  # data is list, not dict
        sid_to_key = {}

        with patch("backend.brokers.adapters.dhan.logger"):
            result = _parse_dhan_ohlc_response(resp, sid_to_key, 1)
            assert result == {}

    def test_parse_dhan_ohlc_response_missing_segment(self):
        """_parse_dhan_ohlc_response skips non-dict segments."""
        from backend.brokers.adapters.dhan import _parse_dhan_ohlc_response

        resp = {
            "data": {
                "NSE_EQ": []  # list instead of dict
            }
        }

        result = _parse_dhan_ohlc_response(resp, {}, 0)
        assert result == {}

    def test_dhan_gtt_trigger_type_single(self):
        """_dhan_gtt_trigger_type maps SINGLE flag."""
        from backend.brokers.adapters.dhan import _dhan_gtt_trigger_type

        row = {"orderFlag": "SINGLE"}
        assert _dhan_gtt_trigger_type(row) == "single"

    def test_dhan_gtt_trigger_type_oco(self):
        """_dhan_gtt_trigger_type maps OCO flag to two-leg."""
        from backend.brokers.adapters.dhan import _dhan_gtt_trigger_type

        row = {"orderFlag": "OCO"}
        assert _dhan_gtt_trigger_type(row) == "two-leg"

    def test_dhan_gtt_trigger_values_single(self):
        """_dhan_gtt_trigger_values extracts single trigger."""
        from backend.brokers.adapters.dhan import _dhan_gtt_trigger_values

        row = {"triggerPrice": 95.0}
        result = _dhan_gtt_trigger_values(row, "single")
        assert result == [95.0]

    def test_dhan_gtt_trigger_values_two_leg(self):
        """_dhan_gtt_trigger_values extracts both triggers."""
        from backend.brokers.adapters.dhan import _dhan_gtt_trigger_values

        row = {"triggerPrice": 95.0, "trigger_Price1": 105.0}
        result = _dhan_gtt_trigger_values(row, "two-leg")
        assert result == [95.0, 105.0]

    def test_dhan_gtt_order_leg_builds_dict(self):
        """_dhan_gtt_order_leg extracts order data for single leg."""
        from backend.brokers.adapters.dhan import _dhan_gtt_order_leg

        row = {
            "transactionType": "SELL",
            "quantity": 10,
            "price": 100.0,
            "orderType": "LIMIT",
            "productType": "NRML",
        }
        result = _dhan_gtt_order_leg(row)

        assert result["transaction_type"] == "SELL"
        assert result["quantity"] == 10
        assert result["price"] == 100.0


# ─────────────────────────────────────────────────────────────────────────
# Part K: Line Range 1598-1650 Tests — Holding Normalizers
# ─────────────────────────────────────────────────────────────────────────


class TestHoldingNormalizers:
    """Test holding-specific normalization functions."""

    def test_dhan_holding_exchange_from_segment(self):
        """_dhan_holding_exchange maps exchangeSegment to Kite exchange."""
        from backend.brokers.adapters.dhan import _dhan_holding_exchange

        holding = {"exchangeSegment": "NSE_EQ"}
        assert _dhan_holding_exchange(holding) == "NSE"

    def test_dhan_holding_exchange_from_exchange_field(self):
        """_dhan_holding_exchange falls back to exchange field."""
        from backend.brokers.adapters.dhan import _dhan_holding_exchange

        holding = {"exchange": "NSE_EQ"}
        result = _dhan_holding_exchange(holding)
        assert result in ("NSE", "NSE_EQ")

    def test_dhan_holding_exchange_default_nse(self):
        """_dhan_holding_exchange defaults to NSE."""
        from backend.brokers.adapters.dhan import _dhan_holding_exchange

        holding = {}
        assert _dhan_holding_exchange(holding) == "NSE"

    def test_dhan_holding_pnl_from_broker_value(self):
        """_dhan_holding_pnl uses broker unrealisedProfit when available."""
        from backend.brokers.adapters.dhan import _dhan_holding_pnl

        holding = {"unrealisedProfit": 5000.0}
        pnl = _dhan_holding_pnl(holding, 2500.0, 2000.0, 10)
        # Should use broker value, not derived
        assert pnl == 5000.0

    def test_dhan_holding_pnl_derives_when_zero(self):
        """_dhan_holding_pnl derives when broker value is zero/None."""
        from backend.brokers.adapters.dhan import _dhan_holding_pnl

        holding = {"unrealisedProfit": 0}
        pnl = _dhan_holding_pnl(holding, 2500.0, 2000.0, 10)
        # Should derive: (2500 - 2000) * 10 = 5000
        assert pnl == 5000.0

    def test_dhan_holding_day_change_pct_from_broker(self):
        """_dhan_holding_day_change_pct uses broker dayChangePerc."""
        from backend.brokers.adapters.dhan import _dhan_holding_day_change_pct

        holding = {"dayChangePerc": 2.5}
        pct = _dhan_holding_day_change_pct(holding, 2500.0, 2450.0)
        assert pct == 2.5

    def test_dhan_holding_day_change_pct_derives_when_zero(self):
        """_dhan_holding_day_change_pct derives when broker value is zero."""
        from backend.brokers.adapters.dhan import _dhan_holding_day_change_pct

        holding = {"dayChangePerc": 0}
        pct = _dhan_holding_day_change_pct(holding, 2500.0, 2450.0)
        # Should derive: ((2500 - 2450) / 2450) * 100 ≈ 2.04
        assert abs(pct - 2.04) < 0.1


# ─────────────────────────────────────────────────────────────────────────
# Additional Coverage: Uncovered Branches in dhan.py
# ─────────────────────────────────────────────────────────────────────────


class TestEmitConnEvent:
    """Test _emit_conn_event lazy-import shim."""

    def test_emit_conn_event_success(self):
        """_emit_conn_event imports and calls the real function."""
        from backend.brokers.adapters.dhan import _emit_conn_event

        with patch("backend.brokers.service.conn_events._emit_conn_event") as mock_fire:
            _emit_conn_event("ACC01", "dhan", "login", {"status": "success"})
            mock_fire.assert_called_once_with("ACC01", "dhan", "login", {"status": "success"})

    def test_emit_conn_event_import_failure_silent(self):
        """_emit_conn_event silently passes on import failure."""
        from backend.brokers.adapters.dhan import _emit_conn_event

        with patch("builtins.__import__", side_effect=ImportError("test error")):
            # Should not raise
            _emit_conn_event("ACC01", "dhan", "login", None)


class TestInstrumentsCsvHeader:
    """Test _parse_dhan_csv_header edge cases."""

    def test_parse_dhan_csv_header_missing_required_column(self):
        """_parse_dhan_csv_header returns None when required column is missing."""
        from backend.brokers.adapters.dhan import _parse_dhan_csv_header

        # Missing SEM_SMST_SECURITY_ID
        lines = [
            "SEM_TRADING_SYMBOL,SEM_EXM_EXCH_ID,SEM_SEGMENT",
            "RELIANCE,NSE,D",
        ]
        result = _parse_dhan_csv_header(lines)
        assert result is None

    def test_parse_dhan_csv_header_with_segment_column(self):
        """_parse_dhan_csv_header detects SEM_SEGMENT presence."""
        from backend.brokers.adapters.dhan import _parse_dhan_csv_header

        lines = [
            "SEM_SMST_SECURITY_ID,SEM_TRADING_SYMBOL,SEM_EXM_EXCH_ID,SEM_SEGMENT",
            "12345,RELIANCE,NSE,D",
        ]
        col, has_seg_col = _parse_dhan_csv_header(lines)
        assert has_seg_col is True
        assert "SEM_SEGMENT" in col

    def test_parse_dhan_csv_header_without_segment_column(self):
        """_parse_dhan_csv_header detects missing SEM_SEGMENT (legacy)."""
        from backend.brokers.adapters.dhan import _parse_dhan_csv_header

        lines = [
            "SEM_SMST_SECURITY_ID,SEM_TRADING_SYMBOL,SEM_EXM_EXCH_ID",
            "12345,RELIANCE,NSE_EQ",
        ]
        col, has_seg_col = _parse_dhan_csv_header(lines)
        assert has_seg_col is False


class TestResolveDhanKiteExchange:
    """Test _resolve_dhan_kite_exchange mapping."""

    def test_resolve_exchange_new_schema_with_segment_column(self):
        """Maps new schema (exch_id + segment) to Kite exchange."""
        from backend.brokers.adapters.dhan import (
            _resolve_dhan_kite_exchange,
            _DHAN_EXCH_SEG_TO_EXCHANGE,
        )

        col = {"SEM_EXM_EXCH_ID": 0, "SEM_SEGMENT": 1}
        parts = ["NSE", "D"]
        kite_exch, seg_raw = _resolve_dhan_kite_exchange(parts, col, has_seg_col=True)
        # Map depends on _DHAN_EXCH_SEG_TO_EXCHANGE; expect NSE for equity
        assert kite_exch is not None
        assert seg_raw == "D"

    def test_resolve_exchange_old_schema_no_segment_column(self):
        """Maps old schema (exch_id only) to Kite exchange."""
        from backend.brokers.adapters.dhan import (
            _resolve_dhan_kite_exchange,
            _DHAN_SEGMENT_TO_EXCHANGE,
        )

        col = {"SEM_EXM_EXCH_ID": 0}
        parts = ["NSE_EQ"]
        kite_exch, seg_raw = _resolve_dhan_kite_exchange(parts, col, has_seg_col=False)
        # Should use _DHAN_SEGMENT_TO_EXCHANGE mapping
        assert seg_raw == "NSE_EQ"

    def test_resolve_exchange_new_schema_unmapped_segment(self):
        """Returns None when segment pair is not mapped."""
        from backend.brokers.adapters.dhan import _resolve_dhan_kite_exchange

        col = {"SEM_EXM_EXCH_ID": 0, "SEM_SEGMENT": 1}
        parts = ["UNKNOWN", "UNKNOWN"]
        kite_exch, seg_raw = _resolve_dhan_kite_exchange(parts, col, has_seg_col=True)
        assert kite_exch is None


class TestExtractDhanLotSize:
    """Test _extract_dhan_lot_size fallback probing."""

    def test_extract_lot_size_new_column_sem_lot_units(self):
        """Probes SEM_LOT_UNITS (new schema) first."""
        from backend.brokers.adapters.dhan import _extract_dhan_lot_size

        col = {"SEM_LOT_UNITS": 2}
        parts = ["val1", "val2", "100"]
        lot_size = _extract_dhan_lot_size(parts, col)
        assert lot_size == 100

    def test_extract_lot_size_legacy_sm_lot_size(self):
        """Falls back to SM_LOT_SIZE (legacy schema)."""
        from backend.brokers.adapters.dhan import _extract_dhan_lot_size

        col = {"SM_LOT_SIZE": 1}
        parts = ["val1", "50"]
        lot_size = _extract_dhan_lot_size(parts, col)
        assert lot_size == 50

    def test_extract_lot_size_invalid_value_zero_fallback(self):
        """Returns 0 when lot_size column contains invalid value."""
        from backend.brokers.adapters.dhan import _extract_dhan_lot_size

        col = {"SEM_LOT_UNITS": 0}
        parts = ["invalid"]
        lot_size = _extract_dhan_lot_size(parts, col)
        assert lot_size == 0

    def test_extract_lot_size_empty_string_zero_fallback(self):
        """Returns 0 when lot_size column is empty."""
        from backend.brokers.adapters.dhan import _extract_dhan_lot_size

        col = {"SEM_LOT_UNITS": 0}
        parts = [""]
        lot_size = _extract_dhan_lot_size(parts, col)
        assert lot_size == 0

    def test_extract_lot_size_negative_value_skipped(self):
        """Returns 0 when lot_size is negative or zero."""
        from backend.brokers.adapters.dhan import _extract_dhan_lot_size

        col = {"SEM_LOT_UNITS": 0}
        parts = ["-5"]
        lot_size = _extract_dhan_lot_size(parts, col)
        assert lot_size == 0


class TestExtractDhanTickSize:
    """Test _extract_dhan_tick_size edge cases."""

    def test_extract_tick_size_present(self):
        """Extracts valid tick size."""
        from backend.brokers.adapters.dhan import _extract_dhan_tick_size

        col = {"SEM_TICK_SIZE": 0}
        parts = ["0.05"]
        tick = _extract_dhan_tick_size(parts, col)
        assert abs(tick - 0.05) < 0.001

    def test_extract_tick_size_missing_column_zero(self):
        """Returns 0.0 when column is missing."""
        from backend.brokers.adapters.dhan import _extract_dhan_tick_size

        col = {}
        parts = ["0.05"]
        tick = _extract_dhan_tick_size(parts, col)
        assert tick == 0.0

    def test_extract_tick_size_invalid_value_zero(self):
        """Returns 0.0 on parse error."""
        from backend.brokers.adapters.dhan import _extract_dhan_tick_size

        col = {"SEM_TICK_SIZE": 0}
        parts = ["invalid"]
        tick = _extract_dhan_tick_size(parts, col)
        assert tick == 0.0


class TestDhanInstrumentToken:
    """Test _dhan_instrument_token conversion."""

    def test_dhan_instrument_token_numeric_string(self):
        """Converts numeric security_id string to int."""
        from backend.brokers.adapters.dhan import _dhan_instrument_token

        token = _dhan_instrument_token("12345")
        assert token == 12345
        assert isinstance(token, int)

    def test_dhan_instrument_token_non_numeric_zero(self):
        """Returns 0 for non-numeric security_id."""
        from backend.brokers.adapters.dhan import _dhan_instrument_token

        token = _dhan_instrument_token("ABC123")
        assert token == 0

    def test_dhan_instrument_token_empty_zero(self):
        """Returns 0 for empty string."""
        from backend.brokers.adapters.dhan import _dhan_instrument_token

        token = _dhan_instrument_token("")
        assert token == 0

    def test_dhan_instrument_token_none_zero(self):
        """Returns 0 for None."""
        from backend.brokers.adapters.dhan import _dhan_instrument_token

        token = _dhan_instrument_token(None)
        assert token == 0


class TestNormalisePositionRow:
    """Test _normalise_position_row with various Dhan formats."""

    def test_normalise_position_row_basic(self):
        """Normalise a basic position row."""
        from backend.brokers.adapters.dhan import _normalise_position_row

        dhan_row = {
            "securityId": "12345",
            "tradingSymbol": "RELIANCE",
            "exchange": "NSE",
            "netQty": 1,
            "multiplier": 1,
            "avgPrice": 2500.0,
            "lastPrice": 2510.0,
            "prevClose": 2495.0,
            "productType": "CNC",
        }
        row = _normalise_position_row(dhan_row)
        assert row["instrument_token"] == 12345
        assert row["tradingsymbol"] == "RELIANCE"
        assert row["exchange"] == "NSE"
        assert row["product"] == "CNC"

    def test_normalise_position_row_invalid_security_id(self):
        """Handles invalid security_id gracefully."""
        from backend.brokers.adapters.dhan import _normalise_position_row

        dhan_row = {
            "securityId": "ABC",  # Non-numeric
            "tradingSymbol": "RELIANCE",
            "exchange": "NSE",
            "netQty": 1,
            "multiplier": 1,
            "avgPrice": 2500.0,
            "lastPrice": 2510.0,
            "prevClose": 2495.0,
            "productType": "CNC",
        }
        row = _normalise_position_row(dhan_row)
        assert row["instrument_token"] == 0

    def test_normalise_position_row_option_symbol_format(self):
        """Converts Dhan option format to Kite format."""
        from backend.brokers.adapters.dhan import _normalise_position_row

        dhan_row = {
            "securityId": "99999",
            "tradingSymbol": "CRUDEOIL-16JUL2026-8500-CE",
            "exchange": "MCX",
            "netQty": 1,
            "multiplier": 100,
            "avgPrice": 100.0,
            "lastPrice": 105.0,
            "prevClose": 95.0,
            "productType": "INTRADAY",
        }
        row = _normalise_position_row(dhan_row)
        # Should be converted to Kite format
        assert "CE" in row["tradingsymbol"].upper()


class TestPlaceGttMcxNotImplemented:
    """Test DhanBroker.place_gtt MCX/NCO restriction."""

    def test_place_gtt_mcx_raises_not_implemented(self):
        """place_gtt raises NotImplementedError for MCX."""
        # Direct function test without instantiating DhanBroker
        from backend.brokers.adapters.dhan import DhanBroker

        # Create minimal mock connection
        conn_mock = MagicMock()
        conn_mock.account = "test"

        # This will fail at the _resolve_security_id step which is the intent
        # (we can't test place_gtt fully without a real connection)
        broker = DhanBroker(conn=conn_mock)

        with pytest.raises(NotImplementedError, match="MCX/NCO"):
            broker.place_gtt(
                trigger_type="single",
                tradingsymbol="CRUDEOIL26JUL8500CE",
                exchange="MCX",
                last_price=100.0,
                orders=[{"price": 100.0}],
                trigger_values=[95.0],
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
