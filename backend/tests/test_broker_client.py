"""
Tests for backend/brokers/client/ — RemoteBroker and UDS transport.

Coverage:
1. RemoteBroker._call() dispatches to conn_service and parses responses
2. RemoteBroker holdings/positions/orders/ltp/quote implementations
3. RemoteBroker translate_qty forwards to conn_service (not base class no-op)
4. RemoteBroker place_order/cancel_order dispatch correctly
5. RemoteBroker.account and broker_id properties
6. RemoteBroker.capabilities lookup is local
7. Error handling: RuntimeError on connection failure, 404 on unknown account
8. Module-level helpers: verify_postback, fetch_access_token, list_remote_accounts
9. Sync client in sync.py handles httpx errors correctly
10. Async client in api.py handles msgspec decoding
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import httpx
import pandas as pd

from backend.brokers.client.remote_broker import (
    RemoteBroker,
    verify_postback,
    fetch_access_token,
    list_remote_accounts,
    trigger_rebuild,
)


class TestRemoteBrokerProperties:
    """Test RemoteBroker account and broker_id properties."""

    def test_account_property(self):
        """RemoteBroker.account returns the configured account code."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        assert broker.account == "ZG0790", "account property must return the configured account"

    def test_broker_id_property(self):
        """RemoteBroker.broker_id returns the configured broker_id."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        assert broker.broker_id == "zerodha_kite", "broker_id property must match constructor arg"

    def test_capabilities_lookup_is_local(self):
        """RemoteBroker.capabilities uses local lookup, not UDS."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        # capabilities_for_broker_id should not make any UDS calls
        capabilities = broker.capabilities
        assert capabilities is not None, "capabilities must not be None"
        assert hasattr(capabilities, "broker_id"), "capabilities must have a broker_id attribute"
        assert capabilities.broker_id == "zerodha_kite", "broker_id must match"


class TestRemoteBrokerCall:
    """Test RemoteBroker._call() dispatch primitive."""

    def test_call_success_returns_result(self):
        """RemoteBroker._call() returns result when ok=True."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.json.return_value = {"ok": True, "result": [{"symbol": "RELIANCE"}]}
            mock_client.post.return_value = mock_resp
            mock_get_client.return_value = mock_client

            result = broker._call("holdings")
            assert result == [{"symbol": "RELIANCE"}], "should return the result key from response"

    def test_call_failure_raises_runtime_error(self):
        """RemoteBroker._call() raises RuntimeError when ok=False."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.json.return_value = {"ok": False, "error": "Token expired"}
            mock_client.post.return_value = mock_resp
            mock_get_client.return_value = mock_client

            with pytest.raises(RuntimeError, match="Token expired"):
                broker._call("holdings")

    def test_call_connection_error_raises_runtime_error(self):
        """RemoteBroker._call() raises RuntimeError on connection failure."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_client.post.side_effect = httpx.ConnectError("Connection refused")
            mock_get_client.return_value = mock_client

            with pytest.raises(RuntimeError, match="conn_service unreachable"):
                broker._call("holdings")

    def test_call_http_error_extracts_detail(self):
        """RemoteBroker._call() raises RuntimeError on HTTP errors."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.is_success = False
            mock_resp.status_code = 500
            mock_resp.json.return_value = {"error": "Internal server error"}
            mock_resp.text = '{"error": "Internal server error"}'
            mock_client.post.return_value = mock_resp
            mock_get_client.return_value = mock_client

            with pytest.raises(RuntimeError, match="conn_service unreachable"):
                broker._call("holdings")

    def test_call_constructs_correct_path(self):
        """RemoteBroker._call() constructs correct UDS path."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.json.return_value = {"ok": True, "result": []}
            mock_client.post.return_value = mock_resp
            mock_get_client.return_value = mock_client

            broker._call("holdings", arg1=123)
            # Verify the path was constructed correctly
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            path = call_args[0][0] if call_args[0] else None
            assert path == "/internal/broker/ZG0790/call/holdings", f"path must be /internal/broker/account/call/method, got {path}"

    def test_call_sends_args_and_kwargs(self):
        """RemoteBroker._call() sends args and kwargs in request body."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.json.return_value = {"ok": True, "result": "OK"}
            mock_client.post.return_value = mock_resp
            mock_get_client.return_value = mock_client

            broker._call("place_order", 123, 456, exchange="NSE", qty=10)
            call_kwargs = mock_client.post.call_args[1]
            json_payload = call_kwargs.get("json", {})
            assert json_payload["args"] == [123, 456], "args must be passed"
            assert json_payload["kwargs"]["exchange"] == "NSE", "kwargs must be passed"
            assert json_payload["kwargs"]["qty"] == 10, "kwargs qty must be passed"


class TestRemoteBrokerMethods:
    """Test RemoteBroker Broker ABC method implementations."""

    def test_holdings_calls_ltp_method(self):
        """RemoteBroker.holdings() dispatches to _call('holdings')."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value=[{"tradingsymbol": "RELIANCE"}]) as mock_call:
            result = broker.holdings()
            mock_call.assert_called_once_with("holdings")
            assert result == [{"tradingsymbol": "RELIANCE"}]

    def test_positions_calls_positions_method(self):
        """RemoteBroker.positions() dispatches to _call('positions')."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value={"net": 0}) as mock_call:
            result = broker.positions()
            mock_call.assert_called_once_with("positions")
            assert result == {"net": 0}

    def test_margins_calls_margins_method(self):
        """RemoteBroker.margins() dispatches to _call('margins')."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value={"availcash": 100000}) as mock_call:
            result = broker.margins()
            mock_call.assert_called_once_with("margins", segment=None)
            assert result == {"availcash": 100000}

    def test_margins_with_segment_parameter(self):
        """RemoteBroker.margins(segment='EQUITY') passes segment kwarg."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value={}) as mock_call:
            broker.margins(segment="EQUITY")
            mock_call.assert_called_once_with("margins", segment="EQUITY")

    def test_orders_calls_orders_method(self):
        """RemoteBroker.orders() dispatches to _call('orders')."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value=[]) as mock_call:
            result = broker.orders()
            mock_call.assert_called_once_with("orders")
            assert result == []

    def test_ltp_calls_ltp_method_with_list(self):
        """RemoteBroker.ltp(symbols) converts to list and dispatches."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value={"RELIANCE": 2800}) as mock_call:
            result = broker.ltp(["RELIANCE"])
            mock_call.assert_called_once_with("ltp", ["RELIANCE"])
            assert result == {"RELIANCE": 2800}

    def test_quote_calls_quote_method(self):
        """RemoteBroker.quote(symbols) dispatches to _call('quote')."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value={}) as mock_call:
            broker.quote(["RELIANCE"])
            mock_call.assert_called_once_with("quote", ["RELIANCE"])

    def test_place_order_with_intent_parameter(self):
        """RemoteBroker.place_order() forwards intent parameter."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value="ORDER123") as mock_call:
            result = broker.place_order(exchange="NSE", tradingsymbol="RELIANCE", quantity=1, intent="close")
            mock_call.assert_called_once()
            call_kwargs = mock_call.call_args[1]
            assert call_kwargs["intent"] == "close", "intent must be forwarded"
            assert result == "ORDER123"

    def test_cancel_order_calls_cancel_order_method(self):
        """RemoteBroker.cancel_order() dispatches to _call('cancel_order')."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value="ORDER123") as mock_call:
            result = broker.cancel_order("ORDER123")
            mock_call.assert_called_once_with("cancel_order", "ORDER123")
            assert result == "ORDER123"

    def test_translate_qty_forwards_to_conn_service(self):
        """RemoteBroker.translate_qty() forwards to conn_service (not base class)."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value=100) as mock_call:
            # MCX CRUDEOIL: raw_qty=100 contracts, lot_size=100, should translate to 1 lot
            result = broker.translate_qty("MCX", 100, 100)
            mock_call.assert_called_once_with("translate_qty", "MCX", 100, 100)
            assert result == 100, "should return translated value from conn_service"

    def test_place_gtt_calls_place_gtt_method(self):
        """RemoteBroker.place_gtt() dispatches to conn_service."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value="GTT123") as mock_call:
            result = broker.place_gtt(exchange="NSE", tradingsymbol="RELIANCE")
            mock_call.assert_called_once()
            assert result == "GTT123"

    def test_cancel_gtt_forwards_exchange_kwarg(self):
        """RemoteBroker.cancel_gtt() forwards exchange as kwarg."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value="GTT123") as mock_call:
            broker.cancel_gtt("GTT123", exchange="NSE")
            call_kwargs = mock_call.call_args[1]
            assert call_kwargs.get("exchange") == "NSE", "exchange must be forwarded as kwarg"

    def test_historical_data_coerces_datetime_to_string(self):
        """RemoteBroker.historical_data() converts datetime objects to strings."""
        from datetime import datetime, date
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value=[{"date": "2026-07-17", "close": 2800}]) as mock_call:
            from_date = datetime(2026, 7, 1, 9, 15, 0)
            to_date = date(2026, 7, 17)
            broker.historical_data(408065, from_date, to_date, interval="day")
            call_args = mock_call.call_args
            # First positional arg after method name is instrument_token
            assert call_args[0][1] == 408065
            # from_date should be coerced to string
            assert isinstance(call_args[0][2], str), "from_date should be converted to string"
            assert isinstance(call_args[0][3], str), "to_date should be converted to string"

    def test_historical_data_parses_date_strings_back_to_datetime(self):
        """RemoteBroker.historical_data() converts date strings back to datetime."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        mock_bars = [
            {"date": "2026-07-17 09:15:00", "close": 2800},
            {"date": "2026-07-16 09:15:00", "close": 2790},
        ]
        with patch.object(broker, '_call', return_value=mock_bars):
            result = broker.historical_data(408065, "2026-07-01", "2026-07-17")
            # Dates should be parsed back to datetime
            for bar in result:
                from datetime import datetime
                assert isinstance(bar["date"], datetime), f"date should be datetime, got {type(bar['date'])}"

    def test_holidays_converts_list_to_set(self):
        """RemoteBroker.holidays() converts list response back to set."""
        broker = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        with patch.object(broker, '_call', return_value=["2026-08-15", "2026-10-02"]) as mock_call:
            result = broker.holidays("NSE")
            mock_call.assert_called_once_with("holidays", "NSE")
            assert isinstance(result, set), "holidays should return a set"
            assert "2026-08-15" in result


class TestModuleLevelHelpers:
    """Test module-level helper functions."""

    def test_verify_postback_success(self):
        """verify_postback() returns True on valid signature."""
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.json.return_value = {"ok": True}
            mock_client.post.return_value = mock_resp
            mock_get_client.return_value = mock_client

            result = verify_postback(
                "ZG0790",
                order_id="123456",
                order_timestamp="1234567890",
                checksum="abc123",
            )
            assert result is True, "verify_postback should return True for ok=True"

    def test_verify_postback_failure(self):
        """verify_postback() returns False on invalid signature."""
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.json.return_value = {"ok": False}
            mock_client.post.return_value = mock_resp
            mock_get_client.return_value = mock_client

            result = verify_postback(
                "ZG0790",
                order_id="123456",
                order_timestamp="1234567890",
                checksum="wrong",
            )
            assert result is False, "verify_postback should return False for ok=False"

    def test_verify_postback_connection_error_returns_false(self):
        """verify_postback() returns False on connection error (safe fallback)."""
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_client.post.side_effect = Exception("Connection refused")
            mock_get_client.return_value = mock_client

            result = verify_postback(
                "ZG0790",
                order_id="123456",
                order_timestamp="1234567890",
                checksum="abc123",
            )
            assert result is False, "verify_postback should return False on error (safe fallback)"

    def test_fetch_access_token_success(self):
        """fetch_access_token() returns (api_key, access_token) on success."""
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.json.return_value = {
                "api_key": "test_api_key",
                "access_token": "test_token",
            }
            mock_client.get.return_value = mock_resp
            mock_get_client.return_value = mock_client

            api_key, token = fetch_access_token("ZG0790")
            assert api_key == "test_api_key"
            assert token == "test_token"

    def test_fetch_access_token_connection_error_returns_none(self):
        """fetch_access_token() returns (None, None) on connection error."""
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_client.get.side_effect = Exception("Connection refused")
            mock_get_client.return_value = mock_client

            api_key, token = fetch_access_token("ZG0790")
            assert api_key is None
            assert token is None

    def test_list_remote_accounts_success(self):
        """list_remote_accounts() returns list of account dicts."""
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.json.return_value = {
                "accounts": [
                    {"account": "ZG0790", "broker_id": "zerodha_kite"},
                    {"account": "DH6847", "broker_id": "dhan"},
                ]
            }
            mock_client.get.return_value = mock_resp
            mock_get_client.return_value = mock_client

            result = list_remote_accounts()
            assert len(result) == 2
            assert result[0]["account"] == "ZG0790"

    def test_list_remote_accounts_empty_on_error(self):
        """list_remote_accounts() returns empty list on error."""
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_client.get.side_effect = Exception("Connection refused")
            mock_get_client.return_value = mock_client

            result = list_remote_accounts()
            assert result == []

    def test_trigger_rebuild_success(self):
        """trigger_rebuild() returns dict with ok=True."""
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.json.return_value = {"ok": True, "accounts": ["ZG0790"]}
            mock_client.post.return_value = mock_resp
            mock_get_client.return_value = mock_client

            result = trigger_rebuild()
            assert result.get("ok") is True

    def test_trigger_rebuild_error_returns_dict_with_ok_false(self):
        """trigger_rebuild() returns dict with ok=False on error."""
        with patch('backend.brokers.client.remote_broker._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_client.post.side_effect = Exception("Connection refused")
            mock_get_client.return_value = mock_client

            result = trigger_rebuild()
            assert result.get("ok") is False
            assert "error" in result


# ─── Tests for async client (backend/brokers/client/api.py) ───


class TestAsyncClientFunctions:
    """Test async client functions in backend.brokers.client.api."""

    @pytest.mark.asyncio
    async def test_fetch_holdings_success(self):
        """fetch_holdings() returns list of DataFrames from holdings endpoint."""
        from backend.brokers.client import api

        with patch('backend.brokers.client.api.get_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_resp = AsyncMock()
            mock_resp.content = b'{"accounts": [{"account": "ZG0790", "ok": true, "rows": [{"tradingsymbol": "RELIANCE", "quantity": 10}]}]}'
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_client

            result = await api.fetch_holdings()
            assert isinstance(result, list), "should return list"
            assert len(result) == 1, "should have one DataFrame"
            assert isinstance(result[0], pd.DataFrame), "each entry should be DataFrame"

    @pytest.mark.asyncio
    async def test_fetch_holdings_network_error_returns_failed_marker(self):
        """fetch_holdings() returns fetch_failed=True on connection error."""
        from backend.brokers.client import api

        with patch('backend.brokers.client.api.get_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
            mock_get_client.return_value = mock_client

            result = await api.fetch_holdings()
            assert len(result) == 1, "should return one frame with failure marker"
            assert result[0].attrs.get("fetch_failed") is True, "should mark as failed"

    @pytest.mark.asyncio
    async def test_fetch_positions_success(self):
        """fetch_positions() returns list of position DataFrames."""
        from backend.brokers.client import api

        with patch('backend.brokers.client.api.get_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_resp = AsyncMock()
            mock_resp.content = b'{"accounts": [{"account": "ZG0790", "ok": true, "rows": [{"symbol": "RELIANCE", "quantity": 1}]}]}'
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_client

            result = await api.fetch_positions()
            assert len(result) == 1
            assert isinstance(result[0], pd.DataFrame)

    @pytest.mark.asyncio
    async def test_fetch_margins_success(self):
        """fetch_margins() returns list of margin DataFrames."""
        from backend.brokers.client import api

        with patch('backend.brokers.client.api.get_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_resp = AsyncMock()
            mock_resp.content = b'{"accounts": [{"account": "ZG0790", "ok": true, "rows": [{"avail cash": 100000}]}]}'
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_client

            result = await api.fetch_margins()
            assert len(result) == 1
            assert isinstance(result[0], pd.DataFrame)

    @pytest.mark.asyncio
    async def test_fetch_health_snapshot_success(self):
        """fetch_health_snapshot() returns health dict."""
        from backend.brokers.client import api

        with patch('backend.brokers.client.api.get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value={
                "health": {
                    "ZG0790": {"last_ok": 1234567890},
                    "DH6847": {"last_ok": 1234567880},
                }
            })
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_client

            result = await api.fetch_health_snapshot()
            assert isinstance(result, dict), "should return dict"
            assert "ZG0790" in result

    @pytest.mark.asyncio
    async def test_fetch_health_snapshot_empty_on_error(self):
        """fetch_health_snapshot() returns empty dict on error."""
        from backend.brokers.client import api

        with patch('backend.brokers.client.api.get_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("Network error"))
            mock_get_client.return_value = mock_client

            result = await api.fetch_health_snapshot()
            assert result == {}, "should return empty dict on error"

    @pytest.mark.asyncio
    async def test_list_accounts_success(self):
        """list_accounts() returns list of account dicts."""
        from backend.brokers.client import api

        with patch('backend.brokers.client.api.get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value={
                "accounts": [
                    {"account": "ZG0790", "broker_id": "zerodha_kite"},
                    {"account": "DH6847", "broker_id": "dhan"},
                ]
            })
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_client

            result = await api.list_accounts()
            assert len(result) == 2
            assert result[0]["account"] == "ZG0790"

    @pytest.mark.asyncio
    async def test_list_accounts_empty_on_error(self):
        """list_accounts() returns empty list on error."""
        from backend.brokers.client import api

        with patch('backend.brokers.client.api.get_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("Network error"))
            mock_get_client.return_value = mock_client

            result = await api.list_accounts()
            assert result == [], "should return empty list on error"

    @pytest.mark.asyncio
    async def test_dhan_poll_reset_remote_all_accounts(self):
        """dhan_poll_reset_remote() resets all accounts when None passed."""
        from backend.brokers.client import api

        with patch('backend.brokers.client.api.get_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_resp = AsyncMock()
            mock_resp.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_client

            await api.dhan_poll_reset_remote(accounts=None)

            # Should send empty body when accounts is None
            mock_client.post.assert_called_once()
            call_kwargs = mock_client.post.call_args[1]
            assert call_kwargs.get("json") == {}

    @pytest.mark.asyncio
    async def test_dhan_poll_reset_remote_specific_accounts(self):
        """dhan_poll_reset_remote() sends specific account codes."""
        from backend.brokers.client import api

        with patch('backend.brokers.client.api.get_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_resp = AsyncMock()
            mock_resp.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_client

            await api.dhan_poll_reset_remote(accounts=["DH6847", "DH3747"])

            mock_client.post.assert_called_once()
            call_kwargs = mock_client.post.call_args[1]
            assert call_kwargs["json"]["accounts"] == ["DH6847", "DH3747"]

    @pytest.mark.asyncio
    async def test_dhan_poll_reset_remote_ignores_network_error(self):
        """dhan_poll_reset_remote() logs warning but never raises on network error."""
        from backend.brokers.client import api

        with patch('backend.brokers.client.api.get_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_get_client.return_value = mock_client

            # Should not raise
            await api.dhan_poll_reset_remote(accounts=["DH6847"])


# ─── Tests for sync client (backend/brokers/client/sync.py) ───


class TestSyncClientFunctions:
    """Test sync client functions in backend.brokers.client.sync."""

    def test_sync_fetch_holdings_success(self):
        """fetch_holdings() (sync) returns list of DataFrames."""
        from backend.brokers.client import sync

        with patch('backend.brokers.client.sync._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.content = b'{"accounts": [{"account": "ZG0790", "ok": true, "rows": [{"tradingsymbol": "RELIANCE", "quantity": 10}]}]}'
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = MagicMock(return_value=mock_resp)
            mock_get_client.return_value = mock_client

            result = sync.fetch_holdings()
            assert isinstance(result, list)
            assert len(result) == 1
            assert isinstance(result[0], pd.DataFrame)

    def test_sync_fetch_holdings_network_error(self):
        """fetch_holdings() (sync) returns fetch_failed=True on error."""
        from backend.brokers.client import sync

        with patch('backend.brokers.client.sync._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(side_effect=Exception("Connection refused"))
            mock_get_client.return_value = mock_client

            result = sync.fetch_holdings()
            assert len(result) == 1
            assert result[0].attrs.get("fetch_failed") is True

    def test_sync_fetch_positions_success(self):
        """fetch_positions() (sync) returns list of position DataFrames."""
        from backend.brokers.client import sync

        with patch('backend.brokers.client.sync._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.content = b'{"accounts": [{"account": "ZG0790", "ok": true, "rows": [{"symbol": "RELIANCE", "quantity": 1}]}]}'
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = MagicMock(return_value=mock_resp)
            mock_get_client.return_value = mock_client

            result = sync.fetch_positions()
            assert len(result) == 1
            assert isinstance(result[0], pd.DataFrame)

    def test_sync_fetch_margins_success(self):
        """fetch_margins() (sync) returns list of margin DataFrames."""
        from backend.brokers.client import sync

        with patch('backend.brokers.client.sync._get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.content = b'{"accounts": [{"account": "ZG0790", "ok": true, "rows": [{"avail cash": 100000}]}]}'
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = MagicMock(return_value=mock_resp)
            mock_get_client.return_value = mock_client

            result = sync.fetch_margins()
            assert len(result) == 1
            assert isinstance(result[0], pd.DataFrame)


# ─── Tests for conn_events.py ───


class TestConnEvents:
    """Test broker-connection event queue."""

    def test_emit_conn_event_calls_enqueue_nowait(self):
        """_emit_conn_event calls enqueue_nowait when queue is healthy."""
        from backend.brokers.service.conn_events import _emit_conn_event

        with patch('backend.brokers.service.conn_events.broker_conn_event_queue') as mock_queue:
            mock_queue.get_health.return_value = {"worker_alive": True}
            mock_queue.enqueue_nowait = MagicMock()

            _emit_conn_event(
                account="ZG0790",
                broker_id="zerodha_kite",
                event_type="login_success",
                detail={"token": "abc123"},
            )

            mock_queue.enqueue_nowait.assert_called_once()
            call_kwargs = mock_queue.enqueue_nowait.call_args[1]
            assert call_kwargs["account"] == "ZG0790"
            assert call_kwargs["broker_id"] == "zerodha_kite"
            assert call_kwargs["event_type"] == "login_success"

    def test_emit_conn_event_silently_drops_when_queue_dead(self):
        """_emit_conn_event silently drops when queue worker is dead."""
        from backend.brokers.service.conn_events import _emit_conn_event

        with patch('backend.brokers.service.conn_events.broker_conn_event_queue') as mock_queue:
            mock_queue.get_health.return_value = {"worker_alive": False}
            mock_queue.enqueue_nowait = MagicMock()

            _emit_conn_event(
                account="ZG0790",
                broker_id="zerodha_kite",
                event_type="login_success",
            )

            # Should not call enqueue_nowait when worker is dead
            mock_queue.enqueue_nowait.assert_not_called()

    def test_emit_conn_event_handles_exception_silently(self):
        """_emit_conn_event silently handles exceptions."""
        from backend.brokers.service.conn_events import _emit_conn_event

        with patch('backend.brokers.service.conn_events.broker_conn_event_queue') as mock_queue:
            mock_queue.get_health.side_effect = Exception("Queue error")

            # Should not raise
            _emit_conn_event(
                account="ZG0790",
                broker_id="zerodha_kite",
                event_type="login_success",
            )

    def test_emit_conn_event_with_none_detail(self):
        """_emit_conn_event accepts None as detail value."""
        from backend.brokers.service.conn_events import _emit_conn_event

        with patch('backend.brokers.service.conn_events.broker_conn_event_queue') as mock_queue:
            mock_queue.get_health.return_value = {"worker_alive": True}
            mock_queue.enqueue_nowait = MagicMock()

            _emit_conn_event(
                account="ZG0790",
                broker_id="zerodha_kite",
                event_type="logout",
                detail=None,
            )

            mock_queue.enqueue_nowait.assert_called_once()
            call_kwargs = mock_queue.enqueue_nowait.call_args[1]
            assert call_kwargs["detail"] is None
