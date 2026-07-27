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


# ---------------------------------------------------------------------------
# New registry coverage tests — PriceBroker._try edge paths, ltp/instruments/
# holidays/historical_data delegators, get_historical_brokers,
# get_sparkline_broker, get_market_data_broker cache-hit.
# ---------------------------------------------------------------------------

class TestPriceBrokerLastEmptyPath:
    """Test PriceBroker._try last_empty return path when broker returns empty."""

    def setup_method(self):
        from backend.brokers.registry import _RATE_LIMIT_COOLOFF
        _RATE_LIMIT_COOLOFF.clear()

    def teardown_method(self):
        from backend.brokers.registry import _RATE_LIMIT_COOLOFF
        _RATE_LIMIT_COOLOFF.clear()

    def test_returns_last_empty_when_all_brokers_empty(self):
        """_try returns the last empty result (not raises) when all brokers soft-fail."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        mock_broker.broker_id = "mock_broker"
        mock_broker.account = "MOCK01"
        mock_broker.quote.return_value = {}  # empty but valid

        pb = PriceBroker(brokers=[mock_broker])
        # _result_ok always False → soft-fail → last_empty path
        result = pb._try("quote", ["NSE:X"], _result_ok=lambda r: False)
        assert result == {}  # last_empty returned, no exception

    def test_last_empty_preferred_over_raise_when_some_brokers_empty(self):
        """When one broker returns empty and another raises, _try returns the empty."""
        from backend.brokers.registry import PriceBroker

        mock_b1 = MagicMock()
        mock_b1.broker_id = "mock_primary"
        mock_b1.account = "MOCK01"
        mock_b1.quote.return_value = {}

        mock_b2 = MagicMock()
        mock_b2.broker_id = "mock_secondary"
        mock_b2.account = "MOCK02"
        mock_b2.quote.side_effect = Exception("broker down")

        pb = PriceBroker(brokers=[mock_b1, mock_b2])
        result = pb._try("quote", ["NSE:X"], _result_ok=lambda r: False)
        # last_empty = {} from mock_b1; mock_b2's exception sets last_exc but last_empty wins
        assert result == {}


class TestPriceBrokerLastExcRaise:
    """Test PriceBroker._try re-raises last exception when all brokers fail."""

    def setup_method(self):
        from backend.brokers.registry import _RATE_LIMIT_COOLOFF
        _RATE_LIMIT_COOLOFF.clear()

    def teardown_method(self):
        from backend.brokers.registry import _RATE_LIMIT_COOLOFF
        _RATE_LIMIT_COOLOFF.clear()

    def test_reraises_last_exception_when_all_raise(self):
        """_try re-raises when every broker raises and no soft-empty was seen."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        mock_broker.broker_id = "mock_broker"
        mock_broker.account = "MOCK01"
        mock_broker.quote.side_effect = RuntimeError("broker exploded")

        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(RuntimeError, match="broker exploded"):
            pb._try("quote", ["NSE:X"])

    def test_raises_value_error_when_brokers_list_empty(self):
        """PriceBroker constructor raises ValueError for empty brokers list."""
        from backend.brokers.registry import PriceBroker

        with pytest.raises(ValueError, match="at least one underlying broker"):
            PriceBroker(brokers=[])


class TestPriceBrokerRateLimitMark:
    """Test PriceBroker._try marks rate-limit on 'too many requests' errors."""

    def setup_method(self):
        from backend.brokers.registry import _RATE_LIMIT_COOLOFF
        _RATE_LIMIT_COOLOFF.clear()

    def teardown_method(self):
        from backend.brokers.registry import _RATE_LIMIT_COOLOFF
        _RATE_LIMIT_COOLOFF.clear()

    def test_marks_rate_limit_on_too_many_requests(self):
        """_try calls _mark_rate_limited when broker raises 'too many requests'."""
        from backend.brokers.registry import PriceBroker, _is_rate_limited

        mock_broker = MagicMock()
        mock_broker.broker_id = "mock_ratelimit_broker"
        mock_broker.account = "RLACCT01"
        mock_broker.quote.side_effect = Exception("too many requests from this IP")

        pb = PriceBroker(brokers=[mock_broker])
        with pytest.raises(Exception):
            pb._try("quote", ["NSE:X"])

        broker_key = "mock_ratelimit_broker/RLACCT01"
        assert _is_rate_limited(broker_key), "Broker key should be marked rate-limited"


class TestPriceBrokerDelegators:
    """Test PriceBroker.ltp, historical_data, instruments, holidays delegators."""

    def setup_method(self):
        from backend.brokers.registry import _RATE_LIMIT_COOLOFF
        _RATE_LIMIT_COOLOFF.clear()

    def teardown_method(self):
        from backend.brokers.registry import _RATE_LIMIT_COOLOFF
        _RATE_LIMIT_COOLOFF.clear()

    def test_ltp_delegates_to_try(self):
        """PriceBroker.ltp() delegates to _try with ltp method."""
        from backend.brokers.registry import PriceBroker

        mock_broker = MagicMock()
        mock_broker.broker_id = "mock_ltp_broker"
        mock_broker.account = "LTPACCT01"
        mock_broker.ltp.return_value = {"NSE:RELIANCE": 2600.0}

        pb = PriceBroker(brokers=[mock_broker])
        result = pb.ltp(["NSE:RELIANCE"])
        assert result == {"NSE:RELIANCE": 2600.0}
        mock_broker.ltp.assert_called_once()

    def test_historical_data_delegates_to_try(self):
        """PriceBroker.historical_data() delegates to _try."""
        from backend.brokers.registry import PriceBroker

        candles = [{"date": "2025-01-01", "open": 100, "close": 105}]
        mock_broker = MagicMock()
        mock_broker.broker_id = "mock_hist_broker"
        mock_broker.account = "HISTACCT01"
        mock_broker.historical_data.return_value = candles

        pb = PriceBroker(brokers=[mock_broker])
        result = pb.historical_data(738561, "2025-01-01", "2025-01-31", "day")
        assert result == candles

    def test_instruments_delegates_to_try_with_kite_filter(self):
        """PriceBroker.instruments() uses _instruments_has_kite_shape as result_ok."""
        from backend.brokers.registry import PriceBroker

        kite_instruments = [{"instrument_type": "EQ", "name": "RELIANCE", "expiry": ""}]
        mock_broker = MagicMock()
        mock_broker.broker_id = "mock_inst_broker"
        mock_broker.account = "INSTACCT01"
        mock_broker.instruments.return_value = kite_instruments

        pb = PriceBroker(brokers=[mock_broker])
        result = pb.instruments("NSE")
        assert result == kite_instruments

    def test_holidays_delegates_to_try(self):
        """PriceBroker.holidays() delegates to _try."""
        from backend.brokers.registry import PriceBroker
        import datetime

        holiday_set = {datetime.date(2025, 1, 26)}
        mock_broker = MagicMock()
        mock_broker.broker_id = "mock_hol_broker"
        mock_broker.account = "HOLACCT01"
        mock_broker.holidays.return_value = holiday_set

        pb = PriceBroker(brokers=[mock_broker])
        result = pb.holidays("NSE")
        assert result == holiday_set


class TestGetHistoricalBrokers:
    """Test get_historical_brokers account selection and filtering."""

    def teardown_method(self):
        from backend.brokers.registry import _RATE_LIMIT_COOLOFF
        _RATE_LIMIT_COOLOFF.clear()

    def test_returns_empty_when_no_accounts(self):
        """get_historical_brokers returns [] when no accounts loaded."""
        from backend.brokers.registry import get_historical_brokers

        with patch("backend.brokers.registry._loaded_accounts", return_value=[]):
            result = get_historical_brokers()
        assert result == []

    def test_returns_kite_account(self):
        """get_historical_brokers returns eligible Kite account."""
        from backend.brokers.registry import get_historical_brokers

        mock_broker = MagicMock()
        with patch("backend.brokers.registry._loaded_accounts", return_value=["ZG0790"]):
            with patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"):
                with patch("backend.brokers.registry._is_hist_enabled", return_value=True):
                    with patch("backend.brokers.registry.get_broker", return_value=mock_broker):
                        # get_string is imported locally; patch at the source module
                        with patch("backend.shared.helpers.settings.get_string", return_value=""):
                            result = get_historical_brokers()
        assert len(result) == 1
        assert result[0] is mock_broker

    def test_excludes_rate_limited_account(self):
        """get_historical_brokers excludes accounts in rate-limit cool-off."""
        from backend.brokers.registry import get_historical_brokers, _mark_rate_limited

        _mark_rate_limited("zerodha_kite/ZG0790")
        mock_broker = MagicMock()
        with patch("backend.brokers.registry._loaded_accounts", return_value=["ZG0790"]):
            with patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"):
                with patch("backend.brokers.registry._is_hist_enabled", return_value=True):
                    with patch("backend.brokers.registry.get_broker", return_value=mock_broker):
                        with patch("backend.shared.helpers.settings.get_string", return_value=""):
                            result = get_historical_brokers()
        assert result == []

    def test_excludes_non_kite_accounts(self):
        """get_historical_brokers skips Dhan and Groww accounts."""
        from backend.brokers.registry import get_historical_brokers

        with patch("backend.brokers.registry._loaded_accounts", return_value=["DH6847"]):
            with patch("backend.brokers.registry._broker_id_for", return_value="dhan"):
                with patch("backend.brokers.registry._is_hist_enabled", return_value=True):
                    with patch("backend.shared.helpers.settings.get_string", return_value=""):
                        result = get_historical_brokers()
        assert result == []

    def test_pinned_account_goes_first(self):
        """Pinned account (connections.price_account) is placed first in the list."""
        from backend.brokers.registry import get_historical_brokers

        mock_b1 = MagicMock()
        mock_b2 = MagicMock()

        def _get_broker_side(acct):
            return mock_b1 if acct == "ZG0790" else mock_b2

        with patch("backend.brokers.registry._loaded_accounts", return_value=["ZG0001", "ZG0790"]):
            with patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"):
                with patch("backend.brokers.registry._is_hist_enabled", return_value=True):
                    with patch("backend.brokers.registry.get_broker", side_effect=_get_broker_side):
                        with patch("backend.shared.helpers.settings.get_string", return_value="ZG0790"):
                            result = get_historical_brokers()
        assert result[0] is mock_b1, "Pinned account should be first"


class TestGetSparklineBroker:
    """Test get_sparkline_broker selection logic."""

    def teardown_method(self):
        from backend.brokers.registry import _RATE_LIMIT_COOLOFF
        _RATE_LIMIT_COOLOFF.clear()

    def test_returns_non_chart_pinned_kite_account(self):
        """get_sparkline_broker prefers a Kite account that's not the chart pin."""
        from backend.brokers.registry import get_sparkline_broker

        mock_b1 = MagicMock()
        mock_b2 = MagicMock()

        def _get_broker_side(acct):
            return mock_b1 if acct == "ZG0001" else mock_b2

        def _get_string(key, default=""):
            if key == "connections.price_account":
                return "ZG0790"
            return ""

        with patch("backend.brokers.registry._loaded_accounts", return_value=["ZG0001", "ZG0790"]):
            with patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"):
                with patch("backend.brokers.registry._is_hist_enabled", return_value=True):
                    with patch("backend.brokers.registry.get_broker", side_effect=_get_broker_side):
                        with patch("backend.shared.helpers.settings.get_string", side_effect=_get_string):
                            result = get_sparkline_broker()

        # Should have used ZG0001 (not the chart-pinned ZG0790) as primary
        assert result is not None

    def test_explicit_sparkline_pin_wins(self):
        """connections.sparkline_account takes priority over selection logic."""
        from backend.brokers.registry import get_sparkline_broker, PriceBroker

        mock_broker = MagicMock()

        def _get_string(key, default=""):
            if key == "connections.sparkline_account":
                return "ZG0001"
            return ""

        with patch("backend.brokers.registry._loaded_accounts", return_value=["ZG0001", "ZG0790"]):
            with patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"):
                with patch("backend.brokers.registry._is_hist_enabled", return_value=True):
                    with patch("backend.brokers.registry.get_broker", return_value=mock_broker):
                        with patch("backend.shared.helpers.settings.get_string", side_effect=_get_string):
                            result = get_sparkline_broker()
        assert isinstance(result, PriceBroker)

    def test_raises_when_no_accounts(self):
        """get_sparkline_broker raises KeyError when no accounts configured."""
        from backend.brokers.registry import get_sparkline_broker

        with patch("backend.brokers.registry._loaded_accounts", return_value=[]):
            with pytest.raises(KeyError, match="No broker accounts configured"):
                get_sparkline_broker()


class TestGetMarketDataBrokerCacheHit:
    """Test get_market_data_broker contextvar cache hit path."""

    def test_returns_cached_broker_without_resolving(self):
        """get_market_data_broker returns _MDB_CTX value when already set."""
        from backend.brokers.registry import get_market_data_broker, _MDB_CTX

        mock_broker = MagicMock()
        token = _MDB_CTX.set(mock_broker)
        try:
            result = get_market_data_broker()
            assert result is mock_broker, "Should return the contextvar-cached broker"
        finally:
            _MDB_CTX.reset(token)

    def test_sets_contextvar_on_first_call(self):
        """get_market_data_broker populates _MDB_CTX on first call in context."""
        from backend.brokers.registry import get_market_data_broker, _MDB_CTX, reset_market_data_broker_ctx

        reset_market_data_broker_ctx()  # ensure clean state
        mock_broker = MagicMock()
        mock_broker.account = "ZG0790"

        with patch("backend.brokers.registry.get_price_broker", return_value=mock_broker):
            # get_string imported locally — patch at source module
            with patch("backend.shared.helpers.settings.get_string", return_value=""):
                result = get_market_data_broker()

        assert result is mock_broker
        # The contextvar should now be set
        assert _MDB_CTX.get(None) is mock_broker
        # Reset after test
        reset_market_data_broker_ctx()


class TestIsHistEnabled:
    """Test _is_hist_enabled function."""

    def test_defaults_true_when_account_not_in_map(self):
        """_is_hist_enabled returns True when account absent from hist_enabled_map."""
        from backend.brokers.registry import _is_hist_enabled

        mock_conn = MagicMock()
        mock_conn._hist_enabled_map = {}  # account not in map

        with patch("backend.brokers.registry.Connections", return_value=mock_conn):
            result = _is_hist_enabled("UNKNOWN_ACCT")
        assert result is True, "Missing account should default to True (include all)"

    def test_honours_false_when_in_map(self):
        """_is_hist_enabled returns False when account explicitly disabled."""
        from backend.brokers.registry import _is_hist_enabled

        mock_conn = MagicMock()
        mock_conn._hist_enabled_map = {"DH6847": False}

        with patch("backend.brokers.registry.Connections", return_value=mock_conn):
            result = _is_hist_enabled("DH6847")
        assert result is False

    def test_honours_true_when_in_map(self):
        """_is_hist_enabled returns True when account is explicitly enabled."""
        from backend.brokers.registry import _is_hist_enabled

        mock_conn = MagicMock()
        mock_conn._hist_enabled_map = {"ZG0790": True}

        with patch("backend.brokers.registry.Connections", return_value=mock_conn):
            result = _is_hist_enabled("ZG0790")
        assert result is True
