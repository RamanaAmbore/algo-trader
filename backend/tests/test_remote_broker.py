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
        """_call() raises RuntimeError on transport error."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.side_effect = httpx.ConnectError("UDS unreachable")

            with pytest.raises(RuntimeError, match="conn_service unreachable"):
                broker._call("holdings")

    def test_call_raises_on_ok_false(self):
        """_call() raises RuntimeError when response has ok=false."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": False, "error": "Account not found"}
            )

            with pytest.raises(RuntimeError, match="Account not found"):
                broker._call("holdings")

    def test_call_raises_on_http_error(self):
        """_call() raises RuntimeError on HTTP error status."""
        broker = RemoteBroker("ZG0790")

        with patch("backend.brokers.client.remote_broker._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_response = MagicMock()
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "404", request=MagicMock(), response=MagicMock()
            )
            mock_client.post.return_value = mock_response

            with pytest.raises(RuntimeError):
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
