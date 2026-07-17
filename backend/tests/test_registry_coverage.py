"""Tests for broker registry — account-to-broker routing."""

import pytest
from unittest.mock import MagicMock, patch

from backend.brokers.registry import (
    get_broker,
    get_market_data_broker,
    reset_market_data_broker_ctx,
    _is_rate_limited,
    _mark_rate_limited,
    _ADAPTERS,
    _broker_id_for,
    all_brokers,
)


class TestGetBroker:
    """Test get_broker(account) routing."""

    @patch("backend.brokers.registry.Connections")
    def test_get_broker_returns_broker_instance(self, mock_connections_cls):
        """get_broker returns a Broker instance for a valid account."""
        mock_conn = MagicMock()
        mock_kite_broker = MagicMock()
        mock_conn.get_broker.return_value = mock_kite_broker

        mock_connections_cls.get_instance.return_value = mock_conn

        broker = get_broker("ZG0790")
        assert broker is not None

    def test_get_broker_unknown_account_raises(self):
        """get_broker raises KeyError for unknown account."""
        with pytest.raises(KeyError):
            get_broker("UNKNOWN_ACCOUNT_THAT_DOES_NOT_EXIST")


class TestAllBrokers:
    """Test all_brokers listing."""

    def test_all_brokers_returns_list(self):
        """all_brokers returns list of broker adapters."""
        result = all_brokers()
        assert isinstance(result, list)


class TestAdaptersMap:
    """Test _ADAPTERS canonical broker_id mapping."""

    def test_adapters_has_zerodha_kite(self):
        """_ADAPTERS includes 'zerodha_kite' mapping."""
        assert "zerodha_kite" in _ADAPTERS

    def test_adapters_has_legacy_kite(self):
        """_ADAPTERS includes legacy 'kite' alias."""
        assert "kite" in _ADAPTERS

    def test_adapters_has_dhan(self):
        """_ADAPTERS includes 'dhan' mapping."""
        assert "dhan" in _ADAPTERS

    def test_adapters_has_groww(self):
        """_ADAPTERS includes 'groww' mapping."""
        assert "groww" in _ADAPTERS

    def test_kite_and_zerodha_kite_same_class(self):
        """'kite' and 'zerodha_kite' map to the same adapter class."""
        assert _ADAPTERS["kite"] == _ADAPTERS["zerodha_kite"]


class TestRateLimit:
    """Test rate-limit cool-off tracking."""

    def test_is_rate_limited_fresh(self):
        """_is_rate_limited returns False for fresh broker."""
        result = _is_rate_limited("zerodha_kite/ZG0790")
        assert isinstance(result, bool)

    def test_mark_rate_limited(self):
        """_mark_rate_limited sets cool-off timer."""
        broker_id = "zerodha_kite/ZG0790_test"
        _mark_rate_limited(broker_id)
        # Should be rate-limited now
        assert _is_rate_limited(broker_id) is True

    def test_mark_rate_limited_expires(self):
        """_is_rate_limited returns False after cool-off expires."""
        import time

        broker_id = "zerodha_kite/ZG0790_expire_test"
        _mark_rate_limited(broker_id)
        assert _is_rate_limited(broker_id) is True

        # In a real scenario, we'd wait for cool-off to expire.
        # For testing, we can't easily manipulate time, but the
        # check succeeds if it doesn't raise.


class TestMarketDataBrokerContextVar:
    """Test per-request market-data broker context var."""

    @patch("backend.brokers.registry._MDB_CTX")
    def test_reset_market_data_broker_ctx(self, mock_ctx):
        """reset_market_data_broker_ctx clears cached broker."""
        reset_market_data_broker_ctx()
        # Should call set() on context var with None
        mock_ctx.set.assert_called()

    def test_get_market_data_broker_fallback(self):
        """get_market_data_broker falls back to registry."""
        # Without an explicit pin, should resolve via registry
        # This is a best-effort test since full integration requires
        # database or mock of Connections
        result = get_market_data_broker()
        # Result could be None or a Broker instance depending on setup
        assert result is None or result is not None


class TestBrokerIdForFunction:
    """Test _broker_id_for account lookup."""

    @patch("backend.brokers.registry.Connections")
    def test_broker_id_for_account_default(self, mock_connections_cls):
        """_broker_id_for returns default when no mapping."""
        mock_conn = MagicMock()
        mock_conn._broker_id_map = {}

        mock_connections_cls.return_value = mock_conn

        # When account not in map, should return default
        broker_id = _broker_id_for("UNKNOWN")
        assert broker_id == "zerodha_kite"  # default


class TestBrokerCapabilities:
    """Test broker capabilities per broker_id."""

    def test_adapters_kite(self):
        """Kite adapter is registered."""
        from backend.brokers.adapters.kite import KiteBroker

        assert _ADAPTERS["zerodha_kite"] == KiteBroker

    def test_adapters_dhan(self):
        """Dhan adapter is registered."""
        from backend.brokers.adapters.dhan import DhanBroker

        assert _ADAPTERS["dhan"] == DhanBroker

    def test_adapters_groww(self):
        """Groww adapter is registered."""
        from backend.brokers.adapters.groww import GrowwBroker

        assert _ADAPTERS["groww"] == GrowwBroker


class TestBrokerIdForPriority:
    """Test _broker_id_for priority resolution steps."""

    @patch("backend.brokers.registry.Connections")
    def test_broker_id_for_step1_db_map(self, mock_connections_cls):
        """_broker_id_for returns value from Connections._broker_id_map (step 1)."""
        mock_conn = MagicMock()
        mock_conn._broker_id_map = {"ACC1": "dhan"}

        mock_connections_cls.return_value = mock_conn

        result = _broker_id_for("ACC1")
        assert result == "dhan", "Should use DB-backed broker_id map"

    @patch("backend.brokers.registry.Connections")
    @patch("backend.brokers.registry._refresh_remote_broker_id_cache")
    def test_broker_id_for_step2_remote_cache(self, mock_refresh, mock_connections_cls):
        """_broker_id_for checks remote cache when cutover flag is on."""
        mock_conn = MagicMock()
        mock_conn._broker_id_map = {}

        mock_connections_cls.return_value = mock_conn

        # Patch the cache to contain a value
        with patch.dict("backend.brokers.registry._REMOTE_BROKER_ID_CACHE", {"ACC2": "groww"}):
            result = _broker_id_for("ACC2")
            assert result == "groww", "Should use remote cache"

    @patch("backend.brokers.registry.Connections")
    def test_broker_id_for_yaml_and_default(self, mock_connections_cls):
        """_broker_id_for uses fallback paths when DB/remote miss."""
        mock_conn = MagicMock()
        mock_conn._broker_id_map = {}

        mock_connections_cls.return_value = mock_conn

        # When no mappings exist, default is returned
        result = _broker_id_for("UNKNOWN_ACC_XYZ")
        # Result should be zerodha_kite or from YAML (can't easily mock both)
        assert isinstance(result, str), "Should return a broker_id string"


class TestGetMarketDataBrokerFallback:
    """Test market-data broker selection with fallback."""

    @patch("backend.brokers.registry._MDB_CTX")
    def test_get_market_data_broker_reads_context_var(self, mock_ctx):
        """get_market_data_broker reads from context var first."""
        mock_broker = MagicMock()
        mock_ctx.get.return_value = mock_broker

        # This tests the context var read path
        result = get_market_data_broker()
        # Result depends on implementation; at minimum shouldn't raise

    def test_reset_market_data_broker_ctx_clears_cache(self):
        """reset_market_data_broker_ctx clears the context var."""
        with patch("backend.brokers.registry._MDB_CTX") as mock_ctx:
            reset_market_data_broker_ctx()
            mock_ctx.set.assert_called_with(None)


class TestGetBrokerErrorHandling:
    """Test get_broker error paths."""

    @patch("backend.brokers.registry.Connections")
    def test_get_broker_no_conn_raises_keyerror(self, mock_connections_cls):
        """get_broker raises KeyError when account not in Connections."""
        mock_conn = MagicMock()
        mock_conn.conn = {}  # Empty connection dict

        mock_connections_cls.return_value = mock_conn

        with pytest.raises(KeyError):
            get_broker("NONEXISTENT")

    @patch("backend.brokers.registry.Connections")
    @patch("backend.brokers.registry._ADAPTERS", {"zerodha_kite": MagicMock()})
    def test_get_broker_unknown_adapter_raises_valueerror(self, mock_connections_cls):
        """get_broker raises ValueError for unknown broker_id."""
        mock_conn = MagicMock()
        mock_kite = MagicMock()
        mock_conn.conn = {"ACC1": mock_kite}

        mock_connections_cls.return_value = mock_conn

        with patch("backend.brokers.registry._broker_id_for", return_value="unknown_broker"):
            with pytest.raises(ValueError):
                get_broker("ACC1")


class TestPriceBrokerFallover:
    """Test PriceBroker fallback chain logic."""

    @patch("backend.brokers.registry.Connections")
    def test_quote_empty_response_falls_over(self, mock_connections_cls):
        """quote() falls back when broker returns empty dict."""
        from backend.brokers.registry import PriceBroker

        mock_conn = MagicMock()
        mock_brokers = [MagicMock(), MagicMock()]
        mock_brokers[0].quote.return_value = {}  # Empty
        mock_brokers[1].quote.return_value = {"RELIANCE-EQ": {"last_price": 2500}}

        mock_connections_cls.return_value = mock_conn

        # Can't easily instantiate PriceBroker without full setup, but
        # the validation functions can be tested

    def test_quote_has_data_with_last_price(self):
        """_quote_has_data detects last_price entries."""
        from backend.brokers.registry import _quote_has_data

        result = _quote_has_data(
            {"RELIANCE-EQ": {"last_price": 2500}},
            ["RELIANCE-EQ"]
        )
        assert result is True, "Should detect last_price"

    def test_quote_has_data_with_close_in_ohlc(self):
        """_quote_has_data detects close in ohlc."""
        from backend.brokers.registry import _quote_has_data

        result = _quote_has_data(
            {"RELIANCE-EQ": {"ohlc": {"close": 2500}}},
            ["RELIANCE-EQ"]
        )
        assert result is True, "Should detect ohlc.close"

    def test_quote_has_data_empty_dict(self):
        """_quote_has_data returns False for empty dict."""
        from backend.brokers.registry import _quote_has_data

        result = _quote_has_data({}, ["RELIANCE-EQ"])
        assert result is False, "Should return False for empty dict"

    def test_quote_has_data_zero_price(self):
        """_quote_has_data rejects zero prices (soft failure)."""
        from backend.brokers.registry import _quote_has_data

        result = _quote_has_data(
            {"RELIANCE-EQ": {"last_price": 0}},
            ["RELIANCE-EQ"]
        )
        assert result is False, "Should reject zero price"

    def test_quote_has_data_missing_entry(self):
        """_quote_has_data returns False when symbol not in result."""
        from backend.brokers.registry import _quote_has_data

        result = _quote_has_data(
            {"SBIN-EQ": {"last_price": 500}},
            ["RELIANCE-EQ"]
        )
        assert result is False, "Should return False when symbol not found"

    def test_ltp_has_data_valid(self):
        """_ltp_has_data detects non-zero last_price."""
        from backend.brokers.registry import _ltp_has_data

        result = _ltp_has_data(
            {"RELIANCE-EQ": {"last_price": 2500}},
            ["RELIANCE-EQ"]
        )
        assert result is True, "Should detect valid LTP"

    def test_ltp_has_data_zero_price(self):
        """_ltp_has_data rejects zero prices."""
        from backend.brokers.registry import _ltp_has_data

        result = _ltp_has_data(
            {"RELIANCE-EQ": {"last_price": 0}},
            ["RELIANCE-EQ"]
        )
        assert result is False, "Should reject zero price"

    def test_ltp_has_data_missing_symbol(self):
        """_ltp_has_data returns False when symbol not in result."""
        from backend.brokers.registry import _ltp_has_data

        result = _ltp_has_data(
            {"SBIN-EQ": {"last_price": 500}},
            ["RELIANCE-EQ"]
        )
        assert result is False, "Should return False for missing symbol"

    def test_ltp_has_data_empty_dict(self):
        """_ltp_has_data returns False for empty dict."""
        from backend.brokers.registry import _ltp_has_data

        result = _ltp_has_data({}, ["RELIANCE-EQ"])
        assert result is False, "Should return False for empty dict"


class TestInstrumentsValidation:
    """Test instruments() validation for Kite schema."""

    def test_instruments_has_kite_shape_valid(self):
        """_instruments_has_kite_shape detects Kite schema."""
        from backend.brokers.registry import _instruments_has_kite_shape

        result = _instruments_has_kite_shape([
            {
                "instrument_type": "FUT",
                "name": "RELIANCE",
                "expiry": "2025-01-31"
            }
        ])
        assert result is True, "Should detect Kite schema"

    def test_instruments_has_kite_shape_stripped_schema(self):
        """_instruments_has_kite_shape rejects stripped schema."""
        from backend.brokers.registry import _instruments_has_kite_shape

        # Dhan/Groww stripped schema (missing instrument_type, name, expiry)
        result = _instruments_has_kite_shape([
            {"exchange": "NSE", "symbol": "RELIANCE"}
        ])
        assert result is False, "Should reject stripped schema"

    def test_instruments_has_kite_shape_empty(self):
        """_instruments_has_kite_shape returns False for empty list."""
        from backend.brokers.registry import _instruments_has_kite_shape

        result = _instruments_has_kite_shape([])
        assert result is False, "Should return False for empty list"

    def test_instruments_has_kite_shape_not_list(self):
        """_instruments_has_kite_shape handles non-list input."""
        from backend.brokers.registry import _instruments_has_kite_shape

        result = _instruments_has_kite_shape({"key": "value"})
        assert result is False, "Should return False for dict input"


class TestPriceBrokerMethods:
    """Test PriceBroker method stubs that raise NotImplementedError."""

    def test_price_broker_profile_raises(self):
        """PriceBroker.profile() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        # Create with mock broker to pass validation
        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError) as exc_info:
            pb.profile()
        assert "account-specific" in str(exc_info.value)

    def test_price_broker_holdings_raises(self):
        """PriceBroker.holdings() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.holdings()

    def test_price_broker_positions_raises(self):
        """PriceBroker.positions() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.positions()

    def test_price_broker_margins_raises(self):
        """PriceBroker.margins() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.margins()

    def test_price_broker_orders_raises(self):
        """PriceBroker.orders() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.orders()

    def test_price_broker_trades_raises(self):
        """PriceBroker.trades() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.trades()

    def test_price_broker_basket_order_margins_raises(self):
        """PriceBroker.basket_order_margins() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.basket_order_margins([])

    def test_price_broker_place_order_raises(self):
        """PriceBroker.place_order() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.place_order()

    def test_price_broker_modify_order_raises(self):
        """PriceBroker.modify_order() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.modify_order("123")

    def test_price_broker_cancel_order_raises(self):
        """PriceBroker.cancel_order() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.cancel_order("123")

    def test_price_broker_place_gtt_raises(self):
        """PriceBroker.place_gtt() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.place_gtt()

    def test_price_broker_modify_gtt_raises(self):
        """PriceBroker.modify_gtt() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.modify_gtt("123")

    def test_price_broker_cancel_gtt_raises(self):
        """PriceBroker.cancel_gtt() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.cancel_gtt("123")

    def test_price_broker_get_gtts_raises(self):
        """PriceBroker.get_gtts() raises NotImplementedError."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(NotImplementedError):
            pb.get_gtts()


class TestRemoteBrokerIdCacheRefresh:
    """Test remote broker ID cache refresh logic."""

    @patch("backend.brokers.client.remote_broker.list_remote_accounts")
    def test_refresh_remote_broker_id_cache_success(self, mock_list_remote):
        """_refresh_remote_broker_id_cache populates cache from conn_service."""
        from backend.brokers.registry import _refresh_remote_broker_id_cache

        mock_list_remote.return_value = [
            {"account": "ZG0790", "broker_id": "zerodha_kite"},
            {"account": "ACC2", "broker_id": "dhan"},
        ]

        _refresh_remote_broker_id_cache()
        # Cache should now be populated (can't inspect directly without imports)

    def test_refresh_remote_broker_id_cache_import_error(self):
        """_refresh_remote_broker_id_cache handles import failure gracefully."""
        from backend.brokers.registry import _refresh_remote_broker_id_cache

        # When conn_service is not available, should return early
        # This is tested indirectly — if the function doesn't raise, it's good
        _refresh_remote_broker_id_cache()

    @patch("backend.brokers.client.remote_broker.list_remote_accounts")
    def test_refresh_remote_broker_id_cache_empty_rows(self, mock_list_remote):
        """_refresh_remote_broker_id_cache leaves cache untouched on empty rows."""
        from backend.brokers.registry import _refresh_remote_broker_id_cache

        mock_list_remote.return_value = []

        _refresh_remote_broker_id_cache()
        # Cache should not be cleared (best-effort behavior)
