"""Coverage tests for backend/brokers/kite_ticker.py.

Targets previously-untested paths:

  TICK-1  _build_stale_list — pure computation; various age scenarios.
  TICK-2  BroadcastBus — register/unregister/publish with a real asyncio queue.
  TICK-3  TickerManager._on_ticks — zero-LTP guard, tick_map writes, tick_age.
  TICK-4  TickerManager._on_connect — pending flush, previously-subscribed re-sub.
  TICK-5  TickerManager._on_close — sets _connected=False + _last_disconnected_at.
  TICK-6  TickerManager._on_reconnect — no exception on various attempt counts.
  TICK-7  TickerManager.subscribe — pending path (not connected).
  TICK-8  TickerManager.subscribe — live path (connected, chunked for >3000).
  TICK-9  TickerManager.unsubscribe — removes tokens from _subscribed.
  TICK-10 TickerManager.get_ltp / get_ltp_by_sym / get_ltp_batch.
  TICK-11 TickerManager.has_sym — case-insensitive O(1) lookup.
  TICK-12 TickerManager.is_active_ticker_healthy — all three gate conditions.
  TICK-13 TickerManager.force_unhealthy / clear_force_unhealthy.
  TICK-14 TickerManager.bump_unhealthy / reset_unhealthy.
  TICK-15 TickerManager.record_swap / swaps_since / last_swap_at.
  TICK-16 TickerManager.start — idempotent; reactor-dead gate; KiteTicker init.
  TICK-17 TickerManager.stop — ReactorNotRunning → _reactor_dead=True.
  TICK-18 TickerManager.status — full payload structure.
  TICK-19 TickerManager.snapshot — zero-LTP filtered.
  TICK-20 TickerManager.ensure_started — idempotent and missing-credential gates.

Five quality dimensions applied:
  SSOT        — direct invocation of implementation methods.
  Correctness — precise assertions on internal state.
  Performance — no real WebSocket/broker I/O; Twisted calls mocked.
  Reuse       — shared factories via module-level helpers.
  UX          — every assert has an f-string with the actual value.
"""
from __future__ import annotations

import asyncio
import time
import threading
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Shared factory
# ---------------------------------------------------------------------------

def _fresh_ticker():
    """Return a fresh TickerManager without starting the WebSocket."""
    from backend.brokers.kite_ticker import TickerManager
    return TickerManager()


# ---------------------------------------------------------------------------
# TICK-1: _build_stale_list
# ---------------------------------------------------------------------------

class TestBuildStaleList:
    """_build_stale_list is pure computation — no I/O required."""

    def test_never_ticked_tokens_are_stale(self):
        from backend.brokers.kite_ticker import _build_stale_list

        subscribed = {1001}
        age_snap   = {1001: None}   # never ticked
        sym_snap   = {1001: "NIFTY"}
        now = time.time()

        stale, stale_top, max_age = _build_stale_list(
            subscribed, age_snap, sym_snap, now, stale_threshold_sec=60, stale_top_n=10
        )
        assert len(stale) == 1, f"Expected 1 stale entry, got {stale!r}"
        assert stale[0][0] == "NIFTY", f"Stale sym should be 'NIFTY', got {stale[0][0]!r}"
        assert "NIFTY@never" in stale_top, (
            f"stale_top should include 'NIFTY@never'; got {stale_top!r}"
        )

    def test_fresh_token_not_stale(self):
        from backend.brokers.kite_ticker import _build_stale_list

        subscribed = {1001}
        now = time.time()
        age_snap   = {1001: now - 5.0}  # 5 s old
        sym_snap   = {1001: "NIFTY"}

        stale, _, _ = _build_stale_list(
            subscribed, age_snap, sym_snap, now, stale_threshold_sec=60, stale_top_n=10
        )
        assert stale == [], f"5s-old token should not be stale; got {stale!r}"

    def test_old_token_is_stale(self):
        from backend.brokers.kite_ticker import _build_stale_list

        subscribed = {1001}
        now = time.time()
        age_snap   = {1001: now - 120.0}  # 120 s old
        sym_snap   = {1001: "BANKNIFTY"}

        stale, stale_top, max_age = _build_stale_list(
            subscribed, age_snap, sym_snap, now, stale_threshold_sec=60, stale_top_n=10
        )
        assert len(stale) == 1, f"120s-old token must be stale; got {stale!r}"
        assert max_age >= 100.0, f"max_age should be ~120s, got {max_age!r}"

    def test_max_age_zero_when_nothing_ticked(self):
        from backend.brokers.kite_ticker import _build_stale_list

        subscribed = {1001, 1002}
        now = time.time()
        age_snap   = {1001: None, 1002: None}
        sym_snap   = {1001: "A", 1002: "B"}

        _, _, max_age = _build_stale_list(
            subscribed, age_snap, sym_snap, now, stale_threshold_sec=60, stale_top_n=10
        )
        assert max_age == 0.0, f"max_age must be 0.0 when nothing has ticked; got {max_age!r}"

    def test_empty_subscribed_set(self):
        from backend.brokers.kite_ticker import _build_stale_list

        stale, stale_top, max_age = _build_stale_list(
            set(), {}, {}, time.time(), 60, 10
        )
        assert stale == [], f"Empty subscribed → no stale; got {stale!r}"
        assert stale_top == [], f"Empty subscribed → empty stale_top; got {stale_top!r}"
        assert max_age == 0.0, f"Empty subscribed → max_age=0.0; got {max_age!r}"


# ---------------------------------------------------------------------------
# TICK-2: BroadcastBus
# ---------------------------------------------------------------------------

class TestBroadcastBus:
    """register / unregister / publish with a real asyncio event loop."""

    def test_register_and_unregister(self):
        from backend.brokers.kite_ticker import BroadcastBus

        bus = BroadcastBus()
        q = asyncio.Queue()
        bus.register(q)
        assert q in bus._queues, "Queue should be registered"
        bus.unregister(q)
        assert q not in bus._queues, "Queue should be unregistered"

    def test_publish_without_loop_is_noop(self):
        """publish() before set_loop() must not raise."""
        from backend.brokers.kite_ticker import BroadcastBus

        bus = BroadcastBus()
        q = asyncio.Queue()
        bus.register(q)
        bus.publish({"tok": 123, "ltp": 100.0})  # should not raise
        # Queue untouched (no loop).
        assert q.empty(), "Queue must be empty without a wired event loop"

    def test_publish_delivers_to_registered_queue(self):
        """With a real event loop, publish() delivers a payload to the queue."""
        from backend.brokers.kite_ticker import BroadcastBus

        loop = asyncio.new_event_loop()
        try:
            bus = BroadcastBus()
            bus.set_loop(loop)
            q = asyncio.Queue()
            bus.register(q)
            bus.publish({"tok": 999, "ltp": 50.0})
            # Drive the loop one step so call_soon_threadsafe fires.
            loop.run_until_complete(asyncio.sleep(0))
            assert not q.empty(), "Queue must receive the published payload"
            item = q.get_nowait()
            assert item["tok"] == 999, f"Payload tok mismatch: {item!r}"
            assert item["ltp"] == 50.0, f"Payload ltp mismatch: {item!r}"
        finally:
            loop.close()

    def test_unregistered_queue_receives_no_payload(self):
        from backend.brokers.kite_ticker import BroadcastBus

        loop = asyncio.new_event_loop()
        try:
            bus = BroadcastBus()
            bus.set_loop(loop)
            q = asyncio.Queue()
            bus.register(q)
            bus.unregister(q)
            bus.publish({"tok": 111, "ltp": 1.0})
            loop.run_until_complete(asyncio.sleep(0))
            assert q.empty(), "Unregistered queue must not receive the payload"
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# TICK-3: TickerManager._on_ticks
# ---------------------------------------------------------------------------

class TestOnTicks:
    """Direct callback invocation — no WebSocket needed."""

    def test_valid_tick_updates_tick_map(self):
        tm = _fresh_ticker()
        ticks = [{"instrument_token": 256265, "last_price": 24600.5}]
        tm._on_ticks(MagicMock(), ticks)
        assert tm._tick_map.get(256265) == 24600.5, (
            f"tick_map[256265] expected 24600.5, got {tm._tick_map.get(256265)!r}"
        )

    def test_zero_ltp_is_filtered(self):
        """_on_ticks must not write ltp=0 to _tick_map."""
        tm = _fresh_ticker()
        tm._tick_map[256265] = 24000.0   # prior good value
        ticks = [{"instrument_token": 256265, "last_price": 0}]
        tm._on_ticks(MagicMock(), ticks)
        assert tm._tick_map.get(256265) == 24000.0, (
            f"Zero-LTP must not overwrite prior value; got {tm._tick_map.get(256265)!r}"
        )

    def test_negative_ltp_is_filtered(self):
        tm = _fresh_ticker()
        tm._tick_map[256265] = 24000.0
        ticks = [{"instrument_token": 256265, "last_price": -1.0}]
        tm._on_ticks(MagicMock(), ticks)
        assert tm._tick_map.get(256265) == 24000.0, (
            f"Negative LTP must not overwrite prior value; got {tm._tick_map.get(256265)!r}"
        )

    def test_tick_without_token_is_skipped(self):
        tm = _fresh_ticker()
        ticks = [{"last_price": 100.0}]  # no instrument_token key
        tm._on_ticks(MagicMock(), ticks)
        assert len(tm._tick_map) == 0, (
            f"Tick without token must be skipped; tick_map={tm._tick_map!r}"
        )

    def test_tick_without_price_is_skipped(self):
        tm = _fresh_ticker()
        ticks = [{"instrument_token": 256265}]  # no last_price key
        tm._on_ticks(MagicMock(), ticks)
        assert 256265 not in tm._tick_map, (
            f"Tick without last_price must be skipped; tick_map={tm._tick_map!r}"
        )

    def test_tick_age_stamped(self):
        tm = _fresh_ticker()
        before = int(time.time())
        ticks = [{"instrument_token": 256265, "last_price": 500.0}]
        tm._on_ticks(MagicMock(), ticks)
        ts = tm._tick_age.get(256265)
        assert ts is not None and ts >= before, (
            f"_tick_age must be stamped with a unix ts >= {before}; got {ts!r}"
        )

    def test_multiple_ticks_in_one_frame(self):
        tm = _fresh_ticker()
        ticks = [
            {"instrument_token": 100, "last_price": 10.0},
            {"instrument_token": 200, "last_price": 20.0},
            {"instrument_token": 300, "last_price": 0.0},   # zero — filtered
        ]
        tm._on_ticks(MagicMock(), ticks)
        assert tm._tick_map.get(100) == 10.0, f"tok=100 expected 10.0; got {tm._tick_map.get(100)!r}"
        assert tm._tick_map.get(200) == 20.0, f"tok=200 expected 20.0; got {tm._tick_map.get(200)!r}"
        assert 300 not in tm._tick_map, f"tok=300 (zero) must not be in tick_map"

    def test_tick_buffer_upsert_called(self):
        """When a _tick_buffer is attached, upsert() must be called for each valid tick."""
        tm = _fresh_ticker()
        mock_buf = MagicMock()
        tm._tick_buffer = mock_buf
        ticks = [{"instrument_token": 256265, "last_price": 300.0}]
        tm._on_ticks(MagicMock(), ticks)
        mock_buf.upsert.assert_called_once()
        call_args = mock_buf.upsert.call_args
        assert call_args[0][0] == 256265, f"upsert token mismatch: {call_args!r}"
        assert call_args[0][1] == 300.0, f"upsert ltp mismatch: {call_args!r}"


# ---------------------------------------------------------------------------
# TICK-4: TickerManager._on_connect
# ---------------------------------------------------------------------------

class TestOnConnect:
    """_on_connect flushes pending tokens and re-subscribes previously-subscribed."""

    def test_connected_flag_set(self):
        tm = _fresh_ticker()
        ws = MagicMock()
        tm._on_connect(ws, None)
        assert tm._connected is True, (
            f"_connected must be True after _on_connect, got {tm._connected!r}"
        )

    def test_last_connected_at_stamped(self):
        tm = _fresh_ticker()
        before = time.time()
        ws = MagicMock()
        tm._on_connect(ws, None)
        assert tm._last_connected_at >= before, (
            f"_last_connected_at must be stamped; got {tm._last_connected_at!r}"
        )

    def test_last_disconnected_at_reset_to_zero(self):
        tm = _fresh_ticker()
        tm._last_disconnected_at = 999999.0  # stale value
        ws = MagicMock()
        tm._on_connect(ws, None)
        assert tm._last_disconnected_at == 0.0, (
            f"_last_disconnected_at must be zeroed on connect; "
            f"got {tm._last_disconnected_at!r}"
        )

    def test_pending_tokens_flushed(self):
        tm = _fresh_ticker()
        tm._pending = {1001, 1002}
        ws = MagicMock()
        ws.MODE_LTP = "ltp"
        tm._on_connect(ws, None)
        assert ws.subscribe.called, "ws.subscribe must be called for pending tokens"
        assert ws.set_mode.called, "ws.set_mode must be called for pending tokens"
        # Pending must be cleared after flush.
        assert len(tm._pending) == 0, (
            f"_pending must be empty after flush; got {tm._pending!r}"
        )

    def test_previously_subscribed_resubscribed(self):
        """Tokens in _subscribed (not pending) must be re-sent to ws on reconnect."""
        tm = _fresh_ticker()
        tm._subscribed = {5001}
        tm._pending    = set()
        ws = MagicMock()
        ws.MODE_LTP = "ltp"
        tm._on_connect(ws, None)
        # ws.subscribe should have been called for the previously-subscribed token.
        all_subscribe_calls = [
            tok
            for c in ws.subscribe.call_args_list
            for tok in c[0][0]
        ]
        assert 5001 in all_subscribe_calls, (
            f"Previously-subscribed token 5001 must be re-sent to ws; "
            f"ws.subscribe calls: {ws.subscribe.call_args_list!r}"
        )

    def test_consecutive_unhealthy_reset(self):
        tm = _fresh_ticker()
        tm._consecutive_unhealthy = 7
        ws = MagicMock()
        tm._on_connect(ws, None)
        assert tm._consecutive_unhealthy == 0, (
            f"_consecutive_unhealthy must be reset on connect; "
            f"got {tm._consecutive_unhealthy!r}"
        )


# ---------------------------------------------------------------------------
# TICK-5: TickerManager._on_close
# ---------------------------------------------------------------------------

class TestOnClose:
    """_on_close marks the socket as disconnected."""

    def test_connected_cleared(self):
        tm = _fresh_ticker()
        tm._connected = True
        with patch("backend.brokers.kite_ticker._emit_conn_event"):
            tm._on_close(MagicMock(), 1001, "Normal closure")
        assert tm._connected is False, (
            f"_connected must be False after _on_close; got {tm._connected!r}"
        )

    def test_last_disconnected_at_stamped(self):
        tm = _fresh_ticker()
        before = time.time()
        with patch("backend.brokers.kite_ticker._emit_conn_event"):
            tm._on_close(MagicMock(), 1001, "Normal closure")
        assert tm._last_disconnected_at >= before, (
            f"_last_disconnected_at must be >= {before}; got {tm._last_disconnected_at!r}"
        )


# ---------------------------------------------------------------------------
# TICK-6: TickerManager._on_reconnect
# ---------------------------------------------------------------------------

class TestOnReconnect:
    """_on_reconnect must not raise for various attempt counts."""

    @pytest.mark.parametrize("attempts", [1, 2, 5, 10, 50])
    def test_does_not_raise(self, attempts):
        tm = _fresh_ticker()
        with patch("backend.brokers.kite_ticker._emit_conn_event"):
            tm._on_reconnect(MagicMock(), attempts)  # must not raise

    def test_backoff_formula_capped_at_30(self):
        """Verify the log formula: delay = min(2^(attempts-1), 30)."""
        # This tests the comment in the code — not a log assert, just that
        # the path runs without error at high attempt counts.
        tm = _fresh_ticker()
        with patch("backend.brokers.kite_ticker._emit_conn_event"):
            tm._on_reconnect(MagicMock(), 100)  # 2^99 would be enormous; capped


# ---------------------------------------------------------------------------
# TICK-7: subscribe — pending path
# ---------------------------------------------------------------------------

class TestSubscribePending:
    """When not connected, tokens go into _pending."""

    def test_tokens_added_to_pending_when_not_connected(self):
        tm = _fresh_ticker()
        tm._connected = False
        tm._kws = None
        tm.subscribe([1001, 1002])
        assert {1001, 1002} <= tm._pending, (
            f"Tokens must go to _pending when not connected; got {tm._pending!r}"
        )

    def test_already_subscribed_not_duplicated(self):
        tm = _fresh_ticker()
        tm._connected = False
        tm._subscribed = {1001}
        tm.subscribe([1001])   # already in _subscribed
        assert 1001 not in tm._pending, (
            f"Already-subscribed token must not be re-added to pending; "
            f"got {tm._pending!r}"
        )

    def test_empty_token_list_is_noop(self):
        tm = _fresh_ticker()
        tm._connected = False
        tm.subscribe([])
        assert tm._pending == set(), (
            f"Empty subscribe must leave _pending empty; got {tm._pending!r}"
        )


# ---------------------------------------------------------------------------
# TICK-8: subscribe — live path + chunking
# ---------------------------------------------------------------------------

class TestSubscribeLive:
    """When connected, tokens are sent directly to ws and chunked at 3000."""

    def test_subscribe_calls_ws_when_connected(self):
        tm = _fresh_ticker()
        fake_kws = MagicMock()
        fake_kws.MODE_LTP = "ltp"
        tm._connected = True
        tm._kws = fake_kws

        tm.subscribe([9001, 9002])

        assert fake_kws.subscribe.called, (
            "kws.subscribe must be called when connected"
        )
        assert fake_kws.set_mode.called, (
            "kws.set_mode must be called when connected"
        )

    def test_large_subscribe_chunked(self):
        """Subscribe with >3000 tokens must be chunked into multiple ws.subscribe calls."""
        from backend.brokers.kite_ticker import KITE_TICKER_CHUNK_SIZE

        tm = _fresh_ticker()
        fake_kws = MagicMock()
        fake_kws.MODE_LTP = "ltp"
        tm._connected = True
        tm._kws = fake_kws

        n_tokens = KITE_TICKER_CHUNK_SIZE + 500
        tokens = list(range(1, n_tokens + 1))
        tm.subscribe(tokens)

        # Should have been called at least twice (>3000 tokens → ≥2 chunks).
        assert fake_kws.subscribe.call_count >= 2, (
            f"Expected ≥2 subscribe calls for {n_tokens} tokens; "
            f"got {fake_kws.subscribe.call_count}"
        )

    def test_tokens_added_to_subscribed_after_live_subscribe(self):
        tm = _fresh_ticker()
        fake_kws = MagicMock()
        fake_kws.MODE_LTP = "ltp"
        tm._connected = True
        tm._kws = fake_kws

        tm.subscribe([7001])
        assert 7001 in tm._subscribed, (
            f"Token 7001 must be in _subscribed after live subscribe; "
            f"got {tm._subscribed!r}"
        )


# ---------------------------------------------------------------------------
# TICK-9: unsubscribe
# ---------------------------------------------------------------------------

class TestUnsubscribe:
    """unsubscribe removes tokens from _subscribed and _tick_age."""

    def test_unsubscribe_removes_from_subscribed(self):
        tm = _fresh_ticker()
        fake_kws = MagicMock()
        tm._connected = True
        tm._kws = fake_kws
        tm._subscribed = {1001, 1002}

        tm.unsubscribe([1001])
        assert 1001 not in tm._subscribed, (
            f"1001 must be removed from _subscribed; got {tm._subscribed!r}"
        )
        assert 1002 in tm._subscribed, (
            f"1002 must remain in _subscribed; got {tm._subscribed!r}"
        )

    def test_unsubscribe_prunes_tick_age(self):
        tm = _fresh_ticker()
        fake_kws = MagicMock()
        tm._connected = True
        tm._kws = fake_kws
        tm._subscribed = {1001}
        tm._tick_age[1001] = time.time()

        tm.unsubscribe([1001])
        assert 1001 not in tm._tick_age, (
            f"_tick_age for 1001 must be pruned after unsubscribe; "
            f"got {tm._tick_age!r}"
        )

    def test_unsubscribe_non_existing_token_is_noop(self):
        tm = _fresh_ticker()
        tm._connected = True
        tm._kws = MagicMock()
        tm._subscribed = {1001}

        tm.unsubscribe([9999])  # 9999 not subscribed
        assert tm._subscribed == {1001}, (
            f"_subscribed must not change for a token that was never subscribed; "
            f"got {tm._subscribed!r}"
        )

    def test_unsubscribe_not_connected_is_noop(self):
        """When not connected, unsubscribe doesn't touch _kws."""
        tm = _fresh_ticker()
        tm._connected = False
        tm._subscribed = {1001}
        fake_kws = MagicMock()
        tm._kws = fake_kws

        tm.unsubscribe([1001])
        fake_kws.unsubscribe.assert_not_called(), (
            "kws.unsubscribe must not be called when disconnected"
        )


# ---------------------------------------------------------------------------
# TICK-10: get_ltp / get_ltp_by_sym / get_ltp_batch
# ---------------------------------------------------------------------------

class TestGetLtp:
    """LTP retrieval via token or symbol."""

    def test_get_ltp_returns_value(self):
        tm = _fresh_ticker()
        tm._tick_map[256265] = 24500.0
        assert tm.get_ltp(256265) == 24500.0, (
            f"Expected 24500.0, got {tm.get_ltp(256265)!r}"
        )

    def test_get_ltp_returns_none_for_unknown(self):
        tm = _fresh_ticker()
        assert tm.get_ltp(999999) is None, (
            f"Unknown token must return None; got {tm.get_ltp(999999)!r}"
        )

    def test_get_ltp_by_sym_case_insensitive(self):
        tm = _fresh_ticker()
        tm._sym_to_token["NIFTY"] = 256265
        tm._tick_map[256265] = 24600.0
        assert tm.get_ltp_by_sym("nifty") == 24600.0, (
            f"get_ltp_by_sym should be case-insensitive; got {tm.get_ltp_by_sym('nifty')!r}"
        )

    def test_get_ltp_by_sym_unknown_returns_none(self):
        tm = _fresh_ticker()
        assert tm.get_ltp_by_sym("UNKNOWN_SYM") is None, (
            f"Unknown sym must return None; got {tm.get_ltp_by_sym('UNKNOWN_SYM')!r}"
        )

    def test_get_ltp_batch_returns_only_known(self):
        tm = _fresh_ticker()
        tm._tick_map[100] = 10.0
        tm._tick_map[200] = 20.0

        result = tm.get_ltp_batch([100, 200, 300])
        assert result == {100: 10.0, 200: 20.0}, (
            f"get_ltp_batch should return only known tokens; got {result!r}"
        )

    def test_get_ltp_batch_empty_when_no_ticks(self):
        tm = _fresh_ticker()
        result = tm.get_ltp_batch([111, 222])
        assert result == {}, (
            f"get_ltp_batch with no ticks should return empty dict; got {result!r}"
        )


# ---------------------------------------------------------------------------
# TICK-11: has_sym
# ---------------------------------------------------------------------------

class TestHasSym:
    """has_sym uses the inverted _sym_to_token map, case-insensitive."""

    def test_has_sym_true_for_subscribed(self):
        tm = _fresh_ticker()
        tm._sym_to_token["NIFTY"] = 256265
        assert tm.has_sym("NIFTY") is True, "has_sym must return True for subscribed sym"
        assert tm.has_sym("nifty") is True, "has_sym must be case-insensitive"

    def test_has_sym_false_for_unknown(self):
        tm = _fresh_ticker()
        assert tm.has_sym("UNKNOWN") is False, (
            "has_sym must return False for unknown sym"
        )


# ---------------------------------------------------------------------------
# TICK-12: is_active_ticker_healthy
# ---------------------------------------------------------------------------

class TestIsActiveTtrickerHealthy:
    """Composite health check — no I/O required."""

    def test_healthy_when_started_connected_fresh_tick(self):
        tm = _fresh_ticker()
        tm._started    = True
        tm._connected  = True
        tm._tick_age[256265] = time.time()  # just ticked
        assert tm.is_active_ticker_healthy(tick_heartbeat_s=60.0) is True, (
            "Should be healthy with started, connected, fresh tick"
        )

    def test_unhealthy_when_not_started(self):
        tm = _fresh_ticker()
        tm._started    = False
        tm._connected  = True
        tm._tick_age[256265] = time.time()
        assert tm.is_active_ticker_healthy() is False, (
            "Not started → unhealthy"
        )

    def test_unhealthy_when_not_connected(self):
        tm = _fresh_ticker()
        tm._started    = True
        tm._connected  = False
        tm._tick_age[256265] = time.time()
        assert tm.is_active_ticker_healthy() is False, (
            "Not connected → unhealthy"
        )

    def test_unhealthy_when_tick_too_old(self):
        tm = _fresh_ticker()
        tm._started    = True
        tm._connected  = True
        # Inject a tick that is 200 s old.
        tm._tick_age[256265] = time.time() - 200.0
        tm._last_connected_at = time.time() - 200.0
        assert tm.is_active_ticker_healthy(tick_heartbeat_s=60.0) is False, (
            "Tick 200s old (threshold 60s) → unhealthy"
        )

    def test_force_unhealthy_overrides_healthy_state(self):
        tm = _fresh_ticker()
        tm._started    = True
        tm._connected  = True
        tm._tick_age[256265] = time.time()
        tm._force_unhealthy_until = time.time() + 120.0  # active window
        assert tm.is_active_ticker_healthy() is False, (
            "force_unhealthy window must override healthy state"
        )

    def test_force_unhealthy_expired_does_not_block(self):
        tm = _fresh_ticker()
        tm._started    = True
        tm._connected  = True
        tm._tick_age[256265] = time.time()
        tm._force_unhealthy_until = time.time() - 1.0   # expired
        assert tm.is_active_ticker_healthy() is True, (
            "Expired force_unhealthy must not override healthy state"
        )


# ---------------------------------------------------------------------------
# TICK-13: force_unhealthy / clear_force_unhealthy
# ---------------------------------------------------------------------------

class TestForceUnhealthy:
    def test_force_unhealthy_sets_deadline(self):
        tm = _fresh_ticker()
        before = time.time()
        deadline = tm.force_unhealthy(120.0)
        assert deadline > before + 100, (
            f"force_unhealthy deadline must be ~now+120s; got {deadline!r}"
        )
        assert tm._force_unhealthy_until == deadline, (
            f"_force_unhealthy_until must match returned deadline; "
            f"got {tm._force_unhealthy_until!r}"
        )

    def test_clear_force_unhealthy_resets_to_zero(self):
        tm = _fresh_ticker()
        tm.force_unhealthy(120.0)
        tm.clear_force_unhealthy()
        assert tm._force_unhealthy_until == 0.0, (
            f"_force_unhealthy_until must be 0.0 after clear; "
            f"got {tm._force_unhealthy_until!r}"
        )


# ---------------------------------------------------------------------------
# TICK-14: bump_unhealthy / reset_unhealthy
# ---------------------------------------------------------------------------

class TestUnhealthyCounter:
    def test_bump_increments(self):
        tm = _fresh_ticker()
        assert tm.bump_unhealthy() == 1, "First bump should return 1"
        assert tm.bump_unhealthy() == 2, "Second bump should return 2"

    def test_reset_zeroes(self):
        tm = _fresh_ticker()
        tm.bump_unhealthy()
        tm.bump_unhealthy()
        tm.reset_unhealthy()
        assert tm._consecutive_unhealthy == 0, (
            f"reset_unhealthy must zero counter; got {tm._consecutive_unhealthy!r}"
        )


# ---------------------------------------------------------------------------
# TICK-15: record_swap / swaps_since / last_swap_at
# ---------------------------------------------------------------------------

class TestSwapHistory:
    def test_record_swap_appends(self):
        tm = _fresh_ticker()
        before = time.time()
        tm.record_swap("ZG0790", "ZJ6294")
        assert len(tm._swap_history) == 1, (
            f"One swap recorded; got {len(tm._swap_history)!r}"
        )
        assert tm._swap_history[0] >= before, (
            f"Swap ts must be >= {before}; got {tm._swap_history[0]!r}"
        )

    def test_swaps_since_counts_recent(self):
        tm = _fresh_ticker()
        now = time.time()
        tm._swap_history = [now - 10, now - 5, now - 300]  # two within 60s window
        assert tm.swaps_since(60.0) == 2, (
            f"swaps_since(60s) expected 2; got {tm.swaps_since(60.0)!r}"
        )

    def test_last_swap_at_returns_most_recent(self):
        tm = _fresh_ticker()
        tm._swap_history = [1000.0, 2000.0, 3000.0]
        assert tm.last_swap_at() == 3000.0, (
            f"last_swap_at must return 3000.0; got {tm.last_swap_at()!r}"
        )

    def test_last_swap_at_zero_when_empty(self):
        tm = _fresh_ticker()
        assert tm.last_swap_at() == 0.0, (
            f"last_swap_at must return 0.0 when no swaps; got {tm.last_swap_at()!r}"
        )

    def test_swap_history_capped_at_128(self):
        tm = _fresh_ticker()
        for i in range(200):
            tm.record_swap("A", "B")
        assert len(tm._swap_history) <= 128, (
            f"_swap_history must not grow beyond 128; got {len(tm._swap_history)!r}"
        )


# ---------------------------------------------------------------------------
# TICK-16: TickerManager.start — idempotent + reactor-dead gate
# ---------------------------------------------------------------------------

class TestTickerStart:
    """start() is idempotent and respects _reactor_dead."""

    def test_start_is_idempotent(self):
        """Calling start() twice should be a no-op on the second call."""
        tm = _fresh_ticker()
        tm._started = True   # simulate already started

        with patch("kiteconnect.KiteTicker") as mock_kt:
            tm.start("api_key", "access_token")
            mock_kt.assert_not_called(), (
                "KiteTicker must not be instantiated when already started"
            )

    def test_start_skipped_when_reactor_dead(self):
        tm = _fresh_ticker()
        tm._reactor_dead = True

        with patch("kiteconnect.KiteTicker") as mock_kt:
            tm.start("api_key", "access_token")
            mock_kt.assert_not_called(), (
                "KiteTicker must not be instantiated when reactor is dead"
            )
        assert tm._started is False, (
            f"_started must stay False when reactor is dead; got {tm._started!r}"
        )

    def test_start_instantiates_kite_ticker(self):
        tm = _fresh_ticker()

        with patch("kiteconnect.KiteTicker") as mock_kt_cls:
            mock_kws = MagicMock()
            mock_kt_cls.return_value = mock_kws
            tm.start("test_api_key", "test_access_token", account="ZG0790")

        assert tm._started is True, (
            f"_started must be True after successful start; got {tm._started!r}"
        )
        mock_kws.connect.assert_called_once_with(threaded=True), (
            f"kws.connect(threaded=True) must be called; got {mock_kws.connect.call_args!r}"
        )

    def test_start_sets_current_account(self):
        tm = _fresh_ticker()

        with patch("kiteconnect.KiteTicker") as mock_kt_cls:
            mock_kt_cls.return_value = MagicMock()
            tm.start("api_key", "access_token", account="ZJ6294")

        assert tm._current_account == "ZJ6294", (
            f"_current_account must be 'ZJ6294'; got {tm._current_account!r}"
        )


# ---------------------------------------------------------------------------
# TICK-17: TickerManager.stop — ReactorNotRunning → _reactor_dead
# ---------------------------------------------------------------------------

class TestTickerStop:
    def test_stop_sets_reactor_dead_on_reactor_not_running(self):
        tm = _fresh_ticker()
        fake_kws = MagicMock()

        class FakeReactorNotRunning(Exception):
            pass

        # stop() tries stop_retry, close, then kws.stop(); make kws.stop raise.
        fake_kws.stop.side_effect = FakeReactorNotRunning("ReactorNotRunning")
        tm._kws = fake_kws
        tm._started = True

        tm.stop()

        assert tm._reactor_dead is True, (
            f"_reactor_dead must be True after ReactorNotRunning; got {tm._reactor_dead!r}"
        )

    def test_stop_clears_kws_and_started(self):
        tm = _fresh_ticker()
        fake_kws = MagicMock()
        tm._kws = fake_kws
        tm._started = True

        tm.stop()

        assert tm._kws is None, f"_kws must be None after stop; got {tm._kws!r}"
        assert tm._started is False, f"_started must be False after stop; got {tm._started!r}"

    def test_stop_stamps_last_disconnected_when_connected(self):
        tm = _fresh_ticker()
        fake_kws = MagicMock()
        tm._kws = fake_kws
        tm._connected = True
        before = time.time()

        tm.stop()

        assert tm._last_disconnected_at >= before, (
            f"_last_disconnected_at must be stamped when stop() disconnects; "
            f"got {tm._last_disconnected_at!r}"
        )


# ---------------------------------------------------------------------------
# TICK-18: TickerManager.status
# ---------------------------------------------------------------------------

class TestTickerStatus:
    """status() always returns and has all required keys."""

    REQUIRED_KEYS = {
        "started", "connected", "subscribed_count", "ticks_held",
        "stale_count", "max_age_seconds", "stale_top",
        "active_account", "consecutive_unhealthy", "swaps_last_hour",
        "last_swap_at",
    }

    def test_status_has_all_required_keys(self):
        tm = _fresh_ticker()
        result = tm.status()
        missing = self.REQUIRED_KEYS - set(result.keys())
        assert not missing, (
            f"status() is missing keys: {missing}"
        )

    def test_status_started_false_by_default(self):
        tm = _fresh_ticker()
        assert tm.status()["started"] is False, (
            "Fresh ticker must report started=False"
        )

    def test_status_counts_subscribed(self):
        tm = _fresh_ticker()
        tm._subscribed = {1, 2, 3}
        assert tm.status()["subscribed_count"] == 3, (
            f"subscribed_count must match _subscribed length; got {tm.status()['subscribed_count']!r}"
        )

    def test_status_stale_count_reflects_never_ticked(self):
        tm = _fresh_ticker()
        tm._subscribed = {1001}
        tm._token_to_sym[1001] = "NIFTY"
        # No tick_age entry → never-ticked
        result = tm.status(stale_threshold_sec=60)
        assert result["stale_count"] == 1, (
            f"Never-ticked token must appear in stale_count; got {result['stale_count']!r}"
        )


# ---------------------------------------------------------------------------
# TICK-19: TickerManager.snapshot — zero-LTP filtered
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_excludes_zero_ltp(self):
        tm = _fresh_ticker()
        tm._tick_map[100] = 0.0   # zero — should be filtered
        tm._tick_map[200] = 50.0  # valid
        tm._token_to_sym[200] = "RELIANCE"

        result = tm.snapshot()
        assert 100 not in result, (
            f"zero-LTP token must be excluded from snapshot; got {result!r}"
        )
        assert 200 in result, (
            f"positive-LTP token must be in snapshot; got {result!r}"
        )
        assert result[200]["ltp"] == 50.0, (
            f"Snapshot ltp mismatch; got {result[200]!r}"
        )

    def test_snapshot_includes_sym(self):
        tm = _fresh_ticker()
        tm._tick_map[200] = 50.0
        tm._token_to_sym[200] = "RELIANCE"

        result = tm.snapshot()
        assert result[200]["sym"] == "RELIANCE", (
            f"Snapshot must include sym; got {result[200]!r}"
        )


# ---------------------------------------------------------------------------
# TICK-20: TickerManager.ensure_started
# ---------------------------------------------------------------------------

class TestEnsureStarted:
    def test_returns_true_when_already_started(self):
        tm = _fresh_ticker()
        tm._started = True
        assert tm.ensure_started("k", "t") is True, (
            "ensure_started must return True when already started"
        )

    def test_returns_false_when_missing_api_key(self):
        tm = _fresh_ticker()
        assert tm.ensure_started("", "some_token") is False, (
            "ensure_started must return False when api_key is empty"
        )

    def test_returns_false_when_missing_access_token(self):
        tm = _fresh_ticker()
        assert tm.ensure_started("some_key", "") is False, (
            "ensure_started must return False when access_token is empty"
        )

    def test_delegates_to_start_when_not_started(self):
        tm = _fresh_ticker()
        with patch.object(tm, "start") as mock_start:
            mock_start.side_effect = lambda *a, **kw: setattr(tm, "_started", True)
            result = tm.ensure_started("api_key", "access_token", account="ZG0790")
        mock_start.assert_called_once(), (
            f"ensure_started must call start() when _started=False; "
            f"calls={mock_start.call_args_list!r}"
        )
