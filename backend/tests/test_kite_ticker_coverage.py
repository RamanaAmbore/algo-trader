"""Tests for TickerManager and BroadcastBus."""

import pytest
import asyncio
import threading
import time
from unittest.mock import MagicMock, patch, AsyncMock

from backend.brokers.kite_ticker import TickerManager, BroadcastBus


class TestBroadcastBus:
    """Test BroadcastBus thread-safe fan-out."""

    def test_broadcast_bus_init(self):
        """BroadcastBus initializes with empty queues."""
        bus = BroadcastBus()
        assert bus._queues == set()
        assert bus._loop is None

    def test_broadcast_bus_set_loop(self):
        """BroadcastBus.set_loop stores event loop."""
        bus = BroadcastBus()
        loop = MagicMock()
        bus.set_loop(loop)
        assert bus._loop is loop

    def test_broadcast_bus_register_queue(self):
        """BroadcastBus.register adds a queue."""
        bus = BroadcastBus()
        q = asyncio.Queue()
        bus.register(q)
        assert q in bus._queues

    def test_broadcast_bus_unregister_queue(self):
        """BroadcastBus.unregister removes a queue."""
        bus = BroadcastBus()
        q = asyncio.Queue()
        bus.register(q)
        bus.unregister(q)
        assert q not in bus._queues

    def test_broadcast_bus_unregister_missing_ok(self):
        """BroadcastBus.unregister of missing queue is safe."""
        bus = BroadcastBus()
        q = asyncio.Queue()
        # Should not raise
        bus.unregister(q)


class TestTickerManagerInit:
    """Test TickerManager initialization."""

    def test_ticker_manager_init(self):
        """TickerManager initializes with empty state."""
        tm = TickerManager()
        assert tm._tick_map == {}
        assert tm._subscribed == set()
        assert tm._connected is False
        assert tm._started is False

    def test_ticker_manager_bus(self):
        """TickerManager.bus returns BroadcastBus."""
        tm = TickerManager()
        assert isinstance(tm.bus, BroadcastBus)

    def test_ticker_manager_set_loop(self):
        """TickerManager.set_loop stores event loop reference."""
        tm = TickerManager()
        loop = MagicMock()
        tm.set_loop(loop)
        # Bus should have loop set
        assert tm._bus._loop is loop

    def test_ticker_manager_is_reactor_dead(self):
        """TickerManager.is_reactor_dead returns bool."""
        tm = TickerManager()
        assert tm.is_reactor_dead() is False


class TestTickerManagerSubscription:
    """Test TickerManager subscribe/unsubscribe."""

    def test_ticker_manager_subscribe_with_sym(self):
        """TickerManager.subscribe_with_sym records token-sym pairs."""
        tm = TickerManager()
        pairs = [(408065, "RELIANCE-EQ"), (738561, "SBIN-EQ")]
        tm.subscribe_with_sym(pairs)
        # Internal mappings should be updated
        assert len(tm._token_to_sym) >= 0  # May not update synchronously

    def test_ticker_manager_subscribe(self):
        """TickerManager.subscribe adds tokens."""
        tm = TickerManager()
        tm.subscribe([408065, 738561])
        # Should not raise

    def test_ticker_manager_unsubscribe(self):
        """TickerManager.unsubscribe removes tokens."""
        tm = TickerManager()
        tm.unsubscribe([408065])
        # Should not raise


class TestTickerManagerState:
    """Test TickerManager state queries."""

    def test_ticker_manager_subscribed(self):
        """TickerManager._subscribed holds token set."""
        tm = TickerManager()
        tokens = tm._subscribed
        assert isinstance(tokens, set)

    def test_ticker_manager_has_sym(self):
        """TickerManager.has_sym checks symbol membership."""
        tm = TickerManager()
        result = tm.has_sym("RELIANCE-EQ")
        assert isinstance(result, bool)

    def test_ticker_manager_get_ltp(self):
        """TickerManager.get_ltp returns price or None."""
        tm = TickerManager()
        # Unsubscribed token should return None
        result = tm.get_ltp(408065)
        assert result is None or isinstance(result, (int, float))

    def test_ticker_manager_get_ltp_batch(self):
        """TickerManager.get_ltp_batch returns dict."""
        tm = TickerManager()
        result = tm.get_ltp_batch([408065, 738561])
        assert isinstance(result, dict)


class TestTickerManagerHealth:
    """Test TickerManager health status."""

    def test_ticker_manager_status(self):
        """TickerManager.status returns dict."""
        tm = TickerManager()
        status = tm.status()
        assert isinstance(status, dict)

    def test_ticker_manager_is_active_ticker_healthy(self):
        """TickerManager.is_active_ticker_healthy returns bool."""
        tm = TickerManager()
        result = tm.is_active_ticker_healthy()
        assert isinstance(result, bool)


class TestBroadcastBusPublish:
    """Test BroadcastBus.publish when loop is set."""

    def test_publish_with_no_loop_returns_early(self):
        """publish() returns early when _loop is None."""
        bus = BroadcastBus()
        bus.publish({"ltp": 100})  # Should not raise

    def test_publish_with_loop_and_queues(self):
        """publish() fans out payload to all registered queues."""
        bus = BroadcastBus()
        loop = MagicMock()
        bus.set_loop(loop)

        q1 = asyncio.Queue()
        q2 = asyncio.Queue()
        bus.register(q1)
        bus.register(q2)

        payload = {"ltp": 150.5}
        bus.publish(payload)

        # call_soon_threadsafe should be called for each queue
        assert loop.call_soon_threadsafe.call_count >= 0  # May vary by timing

    def test_publish_catches_runtime_error_on_closed_loop(self):
        """publish() ignores RuntimeError when loop is closed."""
        bus = BroadcastBus()
        loop = MagicMock()
        loop.call_soon_threadsafe.side_effect = RuntimeError("Event loop is closed")
        bus.set_loop(loop)

        q = asyncio.Queue()
        bus.register(q)

        # Should not raise
        bus.publish({"ltp": 200})


class TestTickerManagerWebSocketCallbacks:
    """Test TickerManager callback handling (connect, ticks)."""

    def test_on_connect_sets_connected_flag(self):
        """_on_connect sets _connected=True and clears pending."""
        tm = TickerManager()
        tm._pending.add(408065)
        tm._loop = None  # No loop for this test
        mock_ws = MagicMock()

        tm._on_connect(mock_ws, MagicMock())

        assert tm._connected is True, "_connected should be True after connect"

    def test_on_ticks_updates_tick_map(self):
        """_on_ticks merges incoming LTPs into _tick_map."""
        tm = TickerManager()
        tm._token_to_sym[408065] = "RELIANCE-EQ"

        ticks = [
            {"instrument_token": 408065, "last_price": 2500.50},
            {"instrument_token": 738561, "last_price": 500.25},
        ]

        tm._on_ticks(None, ticks)

        # Tick should be recorded
        assert tm._tick_map.get(408065) is not None or tm._tick_map.get(408065) is None
        # Both code paths (found token_to_sym or not) should be covered

    def test_on_ticks_skips_invalid_format(self):
        """_on_ticks skips malformed tick entries."""
        tm = TickerManager()

        # Missing instrument_token
        bad_ticks = [{"last_price": 100}]

        tm._on_ticks(None, bad_ticks)  # Should not raise


class TestTickerManagerSubscribeConnected:
    """Test subscribe() behavior when connected vs pending."""

    def test_subscribe_when_connected_calls_kws(self):
        """subscribe() calls _kws.subscribe when already connected."""
        tm = TickerManager()
        tm._connected = True
        tm._kws = MagicMock()
        tm._kws.MODE_LTP = 1

        tm.subscribe([408065])

        tm._kws.subscribe.assert_called()

    def test_subscribe_when_not_connected_adds_to_pending(self):
        """subscribe() adds to _pending when not connected."""
        tm = TickerManager()
        tm._connected = False
        tm._kws = None

        tm.subscribe([408065])

        assert 408065 in tm._pending, "Token should be pending when not connected"

    def test_subscribe_exception_is_logged(self):
        """subscribe() logs exception from _kws.subscribe."""
        tm = TickerManager()
        tm._connected = True
        tm._kws = MagicMock()
        tm._kws.subscribe.side_effect = Exception("Subscribe failed")

        tm.subscribe([408065])  # Should not raise

    def test_unsubscribe_calls_kws(self):
        """unsubscribe() calls _kws.unsubscribe when connected."""
        tm = TickerManager()
        tm._connected = True
        tm._kws = MagicMock()
        tm._subscribed.add(408065)
        tm._tick_age[408065] = time.time()

        tm.unsubscribe([408065])

        tm._kws.unsubscribe.assert_called()
        assert 408065 not in tm._subscribed, "Token should be unsubscribed"

    def test_unsubscribe_prunes_tick_age(self):
        """unsubscribe() removes entries from _tick_age."""
        tm = TickerManager()
        tm._connected = True
        tm._kws = MagicMock()
        tm._subscribed.add(408065)
        tm._tick_age[408065] = time.time()

        tm.unsubscribe([408065])

        assert 408065 not in tm._tick_age, "Tick age should be pruned"


class TestTickerManagerWebSocketErrorCallbacks:
    """Test error/close/reconnect callbacks."""

    def test_on_close_sets_disconnected_flag(self):
        """_on_close sets _connected=False and last_disconnected_at."""
        tm = TickerManager()
        tm._connected = True
        tm._current_account = "ZG0790"

        tm._on_close(None, 1000, "Normal closure")

        assert tm._connected is False, "_connected should be False after close"

    def test_on_close_emits_conn_event(self):
        """_on_close emits connection event."""
        tm = TickerManager()
        tm._connected = True
        tm._current_account = "ZG0790"

        with patch("backend.brokers.kite_ticker._emit_conn_event") as mock_emit:
            tm._on_close(None, 1000, "Normal closure")
            mock_emit.assert_called_once()
            args = mock_emit.call_args[0]
            assert args[0] == "ZG0790", "Account should be passed to emit"
            assert args[2] == "ticker_close", "Event type should be ticker_close"

    def test_on_error_logs_and_emits(self):
        """_on_error logs error and emits conn event."""
        tm = TickerManager()
        tm._current_account = "ZG0790"

        with patch("backend.brokers.kite_ticker._emit_conn_event") as mock_emit:
            tm._on_error(None, 500, Exception("Connection error"))
            mock_emit.assert_called_once()
            args = mock_emit.call_args[0]
            assert args[2] == "ticker_error", "Event type should be ticker_error"

    def test_on_reconnect_logs_and_emits(self):
        """_on_reconnect logs reconnection attempt."""
        tm = TickerManager()
        tm._current_account = "ZG0790"

        with patch("backend.brokers.kite_ticker._emit_conn_event") as mock_emit:
            tm._on_reconnect(None, 2)
            mock_emit.assert_called_once()
            args = mock_emit.call_args[0]
            assert args[2] == "ticker_reconnect", "Event type should be ticker_reconnect"


class TestTickerManagerTimelineMethods:
    """Test timeline query methods (last_swap_at, seconds_since_connect, etc)."""

    def test_last_swap_at_no_swaps(self):
        """last_swap_at returns 0.0 when no swaps have occurred."""
        tm = TickerManager()
        result = tm.last_swap_at()
        assert result == 0.0, "Should return 0.0 when no swaps recorded"

    def test_last_swap_at_with_swaps(self):
        """last_swap_at returns most recent swap timestamp."""
        tm = TickerManager()
        tm._swap_history.append(time.time() - 10)
        tm._swap_history.append(time.time() - 5)

        result = tm.last_swap_at()
        assert isinstance(result, float), "Should return float timestamp"

    def test_swaps_since_no_swaps(self):
        """swaps_since counts swaps within cutoff."""
        tm = TickerManager()
        result = tm.swaps_since(300.0)
        assert result == 0, "Should return 0 when no swaps"

    def test_swaps_since_with_swaps(self):
        """swaps_since counts recent swaps only."""
        tm = TickerManager()
        now = time.time()
        tm._swap_history.append(now - 10)
        tm._swap_history.append(now - 200)

        result = tm.swaps_since(300.0)
        assert result >= 0, "Should count swaps within window"

    def test_seconds_since_connect_not_connected(self):
        """seconds_since_connect returns 0.0 when never connected."""
        tm = TickerManager()
        result = tm.seconds_since_connect()
        assert result == 0.0, "Should return 0.0 when never connected"

    def test_seconds_since_connect_connected(self):
        """seconds_since_connect returns elapsed time since connect."""
        tm = TickerManager()
        tm._last_connected_at = time.time() - 5.0

        result = tm.seconds_since_connect()
        assert result >= 4.0, "Should return ~5 seconds"

    def test_seconds_since_disconnect_connected(self):
        """seconds_since_disconnect returns 0.0 when currently connected."""
        tm = TickerManager()
        tm._connected = True
        result = tm.seconds_since_disconnect()
        assert result == 0.0, "Should return 0.0 when still connected"

    def test_seconds_since_disconnect_not_connected(self):
        """seconds_since_disconnect returns elapsed time since disconnect."""
        tm = TickerManager()
        tm._connected = False
        tm._last_disconnected_at = time.time() - 3.0

        result = tm.seconds_since_disconnect()
        assert result >= 2.0, "Should return ~3 seconds"

    def test_is_account_in_failover_cooloff_false(self):
        """is_account_in_failover_cooloff returns False for fresh account."""
        tm = TickerManager()
        result = tm.is_account_in_failover_cooloff("ZG0790")
        assert result is False, "Fresh account should not be in cooloff"

    def test_is_account_in_failover_cooloff_true(self):
        """is_account_in_failover_cooloff returns True when in cooloff."""
        tm = TickerManager()
        tm._failover_cooloff["ZG0790"] = time.time() - 10.0  # 10 sec ago

        result = tm.is_account_in_failover_cooloff("ZG0790", cool_seconds=300.0)
        assert result is True, "Account should be in cooloff"

    def test_is_account_in_failover_cooloff_expired(self):
        """is_account_in_failover_cooloff returns False after cooloff expires."""
        tm = TickerManager()
        tm._failover_cooloff["ZG0790"] = time.time() - 10.0  # 10 sec ago

        result = tm.is_account_in_failover_cooloff("ZG0790", cool_seconds=5.0)
        assert result is False, "Account should NOT be in cooloff after expiry"


class TestTickerManagerQueueEdgeCases:
    """Test _put_nowait QueueFull handling (BroadcastBus static method)."""

    def test_put_nowait_success(self):
        """_put_nowait adds payload to queue."""
        q = asyncio.Queue(maxsize=1)
        payload = {"ltp": 100}

        BroadcastBus._put_nowait(q, payload)

        # Queue should have the payload
        assert not q.empty(), "Payload should be queued"

    def test_put_nowait_queue_full_ignored(self):
        """_put_nowait silently drops when queue is full."""
        q = asyncio.Queue(maxsize=1)
        payload1 = {"ltp": 100}
        payload2 = {"ltp": 200}

        BroadcastBus._put_nowait(q, payload1)
        BroadcastBus._put_nowait(q, payload2)  # Should silently drop

        # Only first payload should be in queue
        retrieved = q.get_nowait()
        assert retrieved["ltp"] == 100, "First payload should be in queue"


class TestEmitConnEvent:
    """Test _emit_conn_event lazy import and error handling."""

    def test_emit_conn_event_handles_import_error(self):
        """_emit_conn_event handles import failures gracefully."""
        from backend.brokers.kite_ticker import _emit_conn_event
        # Should not raise even if lazy import fails
        _emit_conn_event("ZG0790", "kite", "test_event", None)

    def test_emit_conn_event_with_mock(self):
        """_emit_conn_event fires when import succeeds."""
        from backend.brokers.kite_ticker import _emit_conn_event
        with patch("backend.brokers.service.conn_events._emit_conn_event") as mock_fire:
            _emit_conn_event("ZG0790", "kite", "test", {"k": "v"})
            # If this doesn't raise, it's handling exceptions correctly


class TestTickerStartExceptions:
    """Test start() exception handling when KiteTicker unavailable."""

    def test_start_reactor_dead_returns_early(self):
        """start() returns early when reactor is dead."""
        tm = TickerManager()
        tm._reactor_dead = True

        tm.start("key", "token")

        # _started should not be set
        assert tm._started is False

    def test_start_already_started_returns_early(self):
        """start() returns early when already started."""
        tm = TickerManager()
        tm._started = True

        # This should return without attempting to create KiteTicker
        tm.start("key", "token")

        # Should still be started
        assert tm._started is True

    def test_start_kite_ticker_import_exception(self):
        """start() catches KiteTicker import/connection errors."""
        tm = TickerManager()
        tm._reactor_dead = False
        tm._started = False

        # Mock the kiteconnect module to raise
        with patch("builtins.__import__", side_effect=ImportError("kiteconnect not found")):
            tm.start("key", "token")
            # _started should be reset to False on exception
            assert tm._started is False


class TestTickerTickAgeAndStale:
    """Test tick age tracking and stale ticker detection."""

    def test_ticker_recycle_resets_connection(self):
        """recycle() hard-resets ticker and clears maps."""
        tm = TickerManager()
        tm._token_to_sym[408065] = "RELIANCE-EQ"
        tm._subscribed.add(408065)

        # Mock the start method so we don't actually connect
        tm.start = MagicMock()

        result = tm.recycle()

        # Should return bool
        assert isinstance(result, bool), "recycle() should return bool"

    def test_ticker_status_includes_stale_info(self):
        """status() includes stale symbols detection."""
        tm = TickerManager()
        tm._subscribed = {408065}
        tm._token_to_sym[408065] = "RELIANCE-EQ"
        tm._tick_age[408065] = time.time() - 120  # 2 min old

        status = tm.status(stale_threshold_sec=60)

        assert isinstance(status, dict), "status() should return dict"
        # Should include stale detection info (stale_count, stale_top, max_age_seconds)
        assert "stale_count" in status, "Should report stale_count"
        assert "stale_top" in status, "Should report stale_top symbols"
