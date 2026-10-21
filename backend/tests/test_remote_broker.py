"""Tests for RemoteBroker and conn_service routes.

Coverage:
  • RemoteBroker._call() — UDS HTTP POST dispatch
  • Error handling — network errors → RuntimeError, {ok: false} → RuntimeError
  • historical_data() — datetime formatting, date string parsing
  • holidays() — set coercion from list
  • verify_postback() — HMAC dispatch
  • Conn_service routes — POST /internal/broker/{acct}/call/{method}
"""

import json
from datetime import datetime, date
from unittest.mock import patch, MagicMock, AsyncMock
import pytest
import httpx

from backend.brokers.errors import BrokerError, BrokerNetworkError
from backend.brokers.client.remote_broker import (
    RemoteBroker,
    verify_postback,
    list_remote_accounts,
    fetch_access_token,
)


class MockHTTPTransport(httpx.BaseTransport):
    """Mock UDS transport for testing RemoteBroker."""

    def __init__(self, handler_fn):
        self.handler_fn = handler_fn

    def handle_request(self, request):
        """Sync transport handler."""
        response = self.handler_fn(request)
        return response


class TestRemoteBrokerCall:
    """RemoteBroker._call() — UDS dispatch."""

    def test_call_dispatches_post_to_internal_endpoint(self):
        """_call() POSTs to /internal/broker/{account}/call/{method}."""
        intercepted_requests = []

        def mock_handler(request):
            intercepted_requests.append({
                "method": request.method,
                "url": str(request.url),
                "body": request.content,
            })
            return httpx.Response(200, json={"ok": True, "result": {"data": "test"}})

        broker = RemoteBroker("ZG0790", broker_id="zerodha_kite")
        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": True, "result": {"data": "test"}}
            )

            result = broker._call("holdings")
            mock_client.post.assert_called_once()

            # Check the URL includes account and method
            call_args = mock_client.post.call_args
            assert "ZG0790" in call_args[0][0]
            assert "holdings" in call_args[0][0]

    def test_call_includes_args_and_kwargs(self):
        """_call() includes args and kwargs in JSON body."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": True, "result": []}
            )

            broker._call("place_order", "BUY", quantity=10, price=100.0)

            # Check the body included args and kwargs
            call_args = mock_client.post.call_args
            body = call_args[1]["json"]
            assert body["args"] == ["BUY"]
            assert body["kwargs"] == {"quantity": 10, "price": 100.0}

    def test_call_raises_on_network_error(self):
        """_call() raises BrokerNetworkError on transport error."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.side_effect = httpx.ConnectError("UDS unreachable")

            with pytest.raises(BrokerNetworkError, match="conn_service unreachable"):
                broker._call("holdings")

    def test_call_raises_on_ok_false(self):
        """_call() raises BrokerError when response has ok=false."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": False, "error": "Account not found"}
            )

            with pytest.raises(BrokerError, match="Account not found"):
                broker._call("holdings")

    def test_call_raises_on_http_error(self):
        """_call() raises BrokerNetworkError on HTTP error status."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_response = MagicMock()
            mock_response.is_success = False
            mock_response.json.return_value = {"error": "connection refused"}
            mock_client.post.return_value = mock_response

            with pytest.raises(BrokerNetworkError):
                broker._call("holdings")

    def test_call_returns_result_field(self):
        """_call() returns the 'result' field from response."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": True, "result": {"account_id": "ZG0790", "name": "Test"}}
            )

            result = broker._call("profile")
            assert result == {"account_id": "ZG0790", "name": "Test"}


class TestRemoteBrokerValidateGttExchange:
    """RemoteBroker.validate_gtt_exchange() — must forward to conn_service,
    not silently inherit the Broker ABC's permissive no-op default.

    Groww's adapter has no internal MCX/NCO refusal in place_gtt itself —
    this gate (called from template_attach.apply_plan_live before any
    broker call) is the only thing standing between a templated MCX fill
    on a Groww account and a real unsupported GTT placement when running
    in conn-service mode. Without the override, a RemoteBroker instance
    would inherit Broker.validate_gtt_exchange's `pass`, which never
    calls conn_service at all.
    """

    def test_forwards_to_conn_service_with_correct_method_and_args(self):
        """Confirms the call actually crosses the UDS boundary — the
        pre-fix base no-op never touches _get_client()/post() at all,
        so this assertion fails against the pre-fix code."""
        broker = RemoteBroker("ZG0790", broker_id="groww")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": True, "result": None},
            )

            broker.validate_gtt_exchange("MCX")

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "validate_gtt_exchange" in call_args[0][0]
            assert "ZG0790" in call_args[0][0]
            body = call_args[1]["json"]
            assert body["args"] == ["MCX"]
            assert body["kwargs"] == {}

    def test_remote_refusal_raises_value_error(self):
        """conn_service's Groww adapter raises ValueError("... not
        supported on MCX") internally; the dispatch route serialises it
        as {ok: false, error_type: "ValueError", ...}. "ValueError" is
        not a key in _call's _ERROR_TYPE_MAP, so a naive forward would
        surface a generic BrokerError here instead of ValueError —
        apply_plan_live's `except ValueError` would miss it and the
        exception would propagate unhandled out of apply_plan_live,
        rather than being collected into result.errors as designed."""
        broker = RemoteBroker("GR1234", broker_id="groww")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "ok": False,
                    "error": "Groww Smart Order GTT is not supported on MCX",
                    "error_type": "ValueError",
                },
            )

            with pytest.raises(ValueError, match="not supported on MCX"):
                broker.validate_gtt_exchange("MCX")

    def test_conn_service_unreachable_fails_open_not_value_error(self):
        """Deploy-coupling guard (2026-10-20): a transport failure (conn_service
        down, or the method not yet in conn_service's whitelist because dev
        shares a conn_service process with prod and only a prod deploy updates
        it) must fail OPEN — no exception — not raise ValueError. The prior
        fail-closed behavior silently skipped every templated GTT attach for
        every exchange on dev the moment this RPC shipped to dev ahead of
        conn_service, confirmed live. This test fails against that prior
        behavior."""
        broker = RemoteBroker("ZG0790", broker_id="groww")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.side_effect = httpx.ConnectError("UDS unreachable")

            assert broker.validate_gtt_exchange("MCX") is None

    def test_whitelist_403_on_stale_conn_service_fails_open(self):
        """Deploy-coupling guard (2026-10-20): conn_service's own
        _ALLOWED_BROKER_METHODS whitelist rejects an unrecognised method with
        HTTP 403 before ever reaching the real adapter. On a conn_service
        build that predates this RPC being added (exactly dev's live state
        today — conn_service runs from /opt/ramboq on main, which dev shares
        and which only updates on a prod deploy), every call here 403s. This
        must fail open (no exception), not raise ValueError — the prior
        behavior collapsed this into the same fail-closed path as a genuine
        exchange refusal, breaking every templated GTT attach."""
        broker = RemoteBroker("ZG0790", broker_id="groww")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_resp = MagicMock(
                status_code=403,
                is_success=False,
                json=lambda: {"detail": "method not allowed"},
                text='{"detail": "method not allowed"}',
            )
            mock_resp.request = MagicMock()
            mock_client.post.return_value = mock_resp

            assert broker.validate_gtt_exchange("MCX") is None

    def test_remote_ok_passes_silently(self):
        """A supported-exchange response (ok=True) returns None with no
        exception — mirrors the base ABC's permissive-pass contract for
        the allowed case."""
        broker = RemoteBroker("ZG0790", broker_id="groww")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": True, "result": None},
            )

            assert broker.validate_gtt_exchange("NSE") is None


class TestRemoteBrokerHistoricalData:
    """RemoteBroker.historical_data() — datetime formatting and parsing."""

    def test_historical_data_formats_datetime_with_time(self):
        """historical_data() formats datetime with time component."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": True, "result": []}
            )

            from_dt = datetime(2026, 6, 1, 9, 15, 0)
            to_dt = datetime(2026, 6, 28, 15, 30, 0)

            broker.historical_data(123456, from_dt, to_dt)

            call_args = mock_client.post.call_args
            body = call_args[1]["json"]
            args = body["args"]

            # Should format with space separator, not T
            assert args[1] == "2026-06-01 09:15:00"
            assert args[2] == "2026-06-28 15:30:00"

    def test_historical_data_formats_date_only(self):
        """historical_data() formats date-only args correctly."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": True, "result": []}
            )

            from_date = date(2026, 6, 1)
            to_date = date(2026, 6, 28)

            broker.historical_data(123456, from_date, to_date)

            call_args = mock_client.post.call_args
            body = call_args[1]["json"]
            args = body["args"]

            assert args[1] == "2026-06-01"
            assert args[2] == "2026-06-28"

    def test_historical_data_parses_date_strings_back_to_datetime(self):
        """historical_data() parses date strings in response back to datetime."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "ok": True,
                    "result": [
                        {"date": "2026-06-01 09:15:00", "open": 100, "high": 105, "low": 99, "close": 104},
                        {"date": "2026-06-02 09:15:00", "open": 104, "high": 106, "low": 103, "close": 105},
                    ]
                }
            )

            bars = broker.historical_data(123456, date(2026, 6, 1), date(2026, 6, 2))

            assert len(bars) == 2
            # Dates should be parsed back to datetime objects
            assert isinstance(bars[0]["date"], datetime)
            assert bars[0]["date"] == datetime(2026, 6, 1, 9, 15, 0)

    def test_historical_data_handles_malformed_date_string(self):
        """historical_data() leaves unparseable date strings as-is."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "ok": True,
                    "result": [
                        {"date": "invalid-date", "open": 100},
                    ]
                }
            )

            bars = broker.historical_data(123456, date(2026, 6, 1), date(2026, 6, 2))

            # Should leave it as string since parsing failed
            assert bars[0]["date"] == "invalid-date"


class TestRemoteBrokerHolidays:
    """RemoteBroker.holidays() — set coercion."""

    def test_holidays_coerces_list_to_set(self):
        """holidays() converts list response to set."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "ok": True,
                    "result": ["2026-06-15", "2026-08-15", "2026-10-02"]
                }
            )

            result = broker.holidays("NSE")

            assert isinstance(result, set)
            assert result == {"2026-06-15", "2026-08-15", "2026-10-02"}

    def test_holidays_handles_none_result(self):
        """holidays() returns empty set when result is None."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": True, "result": None}
            )

            result = broker.holidays("NSE")
            assert result == set()


class TestVerifyPostback:
    """verify_postback() — HMAC dispatch."""

    def test_verify_postback_posts_to_endpoint(self):
        """verify_postback() POSTs to /internal/broker/{account}/verify_postback."""
        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": True}
            )

            result = verify_postback(
                "ZG0790",
                order_id="123456",
                order_timestamp="1624790400",
                checksum="abcd1234",
            )

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "ZG0790" in call_args[0][0]
            assert "verify_postback" in call_args[0][0]

            body = call_args[1]["json"]
            assert body["order_id"] == "123456"
            assert body["checksum"] == "abcd1234"

    def test_verify_postback_returns_bool(self):
        """verify_postback() returns True when ok=True."""
        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": True}
            )

            result = verify_postback("ZG0790", order_id="123", order_timestamp="456", checksum="abc")
            assert result is True

    def test_verify_postback_returns_false_on_error(self):
        """verify_postback() returns False on network error."""
        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.side_effect = RuntimeError("UDS error")

            result = verify_postback("ZG0790", order_id="123", order_timestamp="456", checksum="abc")
            assert result is False


class TestListRemoteAccounts:
    """list_remote_accounts() — fetch account list."""

    def test_list_remote_accounts_extracts_accounts(self):
        """list_remote_accounts() returns accounts list from response."""
        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.get.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "accounts": [
                        {"account": "ZG0790", "broker_id": "zerodha_kite"},
                        {"account": "DH1234", "broker_id": "dhan"},
                    ]
                }
            )

            result = list_remote_accounts()

            assert len(result) == 2
            assert result[0]["account"] == "ZG0790"
            assert result[1]["broker_id"] == "dhan"

    def test_list_remote_accounts_returns_empty_on_error(self):
        """list_remote_accounts() returns [] on error."""
        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.get.side_effect = RuntimeError("UDS error")

            result = list_remote_accounts()
            assert result == []


class TestFetchAccessToken:
    """fetch_access_token() — Kite token fetch."""

    def test_fetch_access_token_returns_tuple(self):
        """fetch_access_token() returns (api_key, access_token)."""
        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.get.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "api_key": "abc123",
                    "access_token": "token456",
                }
            )

            api_key, token = fetch_access_token("ZG0790")

            assert api_key == "abc123"
            assert token == "token456"

    def test_fetch_access_token_returns_none_tuple_on_error(self):
        """fetch_access_token() returns (None, None) on error."""
        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.get.side_effect = RuntimeError("UDS error")

            api_key, token = fetch_access_token("ZG0790")

            assert api_key is None
            assert token is None
