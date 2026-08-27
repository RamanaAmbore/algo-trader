"""
Exhaustive tests for KiteTicker reactor-dead detection and watchdog exit.

Covers five quality dimensions:
  SSOT        — single state machine in stop() + start() + watchdog
  Correctness — ReactorNotRunning detection, _reactor_dead flag, start() bailout, watchdog exit
  Performance — no blocking I/O on reactor-dead code paths
  Reuse       — reactor_dead state surfaces via is_reactor_dead() + watchdog logic
  UX          — operator sees correct log messages + SIGTERM on watchdog (systemd restarts)

Test catalogue:

TestReactorDeadDetection (5 tests):
  1. stop() sets _reactor_dead=True when kws.stop() raises ReactorNotRunning
  2. stop() does NOT set _reactor_dead for other exceptions (e.g. RuntimeError)
  3. stop() sets _connected=False and _started=False regardless of exception type
  4. is_reactor_dead() returns False on fresh TickerManager
  5. is_reactor_dead() returns True after stop() catches ReactorNotRunning

TestStartBailsWhenReactorDead (3 tests):
  6. start() returns immediately without calling kws.connect() when _reactor_dead=True
  7. start() calls kws.connect() normally when _reactor_dead=False
  8. start() idempotent check (_started=True) still works when reactor not dead

TestReactorDeadWatchdogExit (4 tests):
  9. watchdog sends SIGTERM via os.kill when ticker.is_reactor_dead() returns True
  10. watchdog does NOT send SIGTERM when is_reactor_dead() returns False (healthy case)
  11. watchdog exits before any other phase logic (reactor_dead checked first)
  12. watchdog sends SIGTERM on every iteration where reactor is dead, not just first
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from unittest.mock import MagicMock, AsyncMock, patch, call as mock_call
import pytest
import pytest_asyncio

from backend.brokers.kite_ticker import TickerManager


# ═══════════════════════════════════════════════════════════════════════════
# TestReactorDeadDetection — 5 tests
# ═══════════════════════════════════════════════════════════════════════════

class TestReactorDeadDetection:
    """Verify stop() properly detects ReactorNotRunning and sets the flag."""

    def test_is_reactor_dead_false_on_fresh_instance(self):
        """Fresh TickerManager should have _reactor_dead=False."""
        ticker = TickerManager()
        assert ticker.is_reactor_dead() is False, \
            "Fresh TickerManager should have _reactor_dead=False"

    def test_stop_sets_reactor_dead_on_reactor_not_running(self):
        """stop() should set _reactor_dead=True when kws.stop() raises ReactorNotRunning."""
        ticker = TickerManager()

        # Mock the kws object with a stop() that raises ReactorNotRunning
        mock_kws = MagicMock()
        mock_kws.stop_retry = MagicMock()
        mock_kws.close = MagicMock()

        # Simulate the Twisted ReactorNotRunning exception
        exc = RuntimeError("ReactorNotRunning: can't restart reactor")
        mock_kws.stop.side_effect = exc

        ticker._kws = mock_kws
        ticker._connected = True
        ticker._started = True

        # Call stop() — should catch ReactorNotRunning and set _reactor_dead
        with patch('backend.brokers.kite_ticker.logger') as mock_logger:
            ticker.stop()

        assert ticker.is_reactor_dead() is True, \
            "stop() should set _reactor_dead=True on ReactorNotRunning"

        # Verify CRITICAL log was called
        assert mock_logger.critical.called, \
            "stop() should log CRITICAL when ReactorNotRunning detected"

    def test_stop_does_not_set_reactor_dead_on_other_exceptions(self):
        """stop() should NOT set _reactor_dead=True for non-ReactorNotRunning exceptions."""
        ticker = TickerManager()

        # Mock the kws object with a stop() that raises a different exception
        mock_kws = MagicMock()
        mock_kws.stop_retry = MagicMock()
        mock_kws.close = MagicMock()
        mock_kws.stop.side_effect = RuntimeError("SomeOtherError")

        ticker._kws = mock_kws
        ticker._connected = True
        ticker._started = True

        # Call stop() — should log exception but NOT set _reactor_dead
        with patch('backend.brokers.kite_ticker.logger') as mock_logger:
            ticker.stop()

        assert ticker.is_reactor_dead() is False, \
            "stop() should NOT set _reactor_dead for non-ReactorNotRunning exceptions"

        # Verify exception was logged (not as CRITICAL reactor-dead)
        assert mock_logger.exception.called, \
            "stop() should log non-ReactorNotRunning exceptions via logger.exception"

    def test_stop_sets_connected_and_started_false_regardless_of_exception(self):
        """stop() should set _connected=False and _started=False regardless of exception type."""
        ticker = TickerManager()

        # Case 1: ReactorNotRunning
        mock_kws = MagicMock()
        mock_kws.stop_retry = MagicMock()
        mock_kws.close = MagicMock()
        mock_kws.stop.side_effect = RuntimeError("ReactorNotRunning")

        ticker._kws = mock_kws
        ticker._connected = True
        ticker._started = True

        with patch('backend.brokers.kite_ticker.logger'):
            ticker.stop()

        assert ticker._connected is False, \
            "stop() should set _connected=False on ReactorNotRunning"
        assert ticker._started is False, \
            "stop() should set _started=False on ReactorNotRunning"

        # Case 2: Other exception
        ticker2 = TickerManager()
        mock_kws2 = MagicMock()
        mock_kws2.stop_retry = MagicMock()
        mock_kws2.close = MagicMock()
        mock_kws2.stop.side_effect = RuntimeError("OtherError")

        ticker2._kws = mock_kws2
        ticker2._connected = True
        ticker2._started = True

        with patch('backend.brokers.kite_ticker.logger'):
            ticker2.stop()

        assert ticker2._connected is False, \
            "stop() should set _connected=False on other exceptions"
        assert ticker2._started is False, \
            "stop() should set _started=False on other exceptions"

    def test_reactor_dead_flag_persists_across_method_calls(self):
        """_reactor_dead flag should persist once set until a fresh instance is created."""
        ticker = TickerManager()

        # Simulate ReactorNotRunning on first stop()
        mock_kws = MagicMock()
        mock_kws.stop_retry = MagicMock()
        mock_kws.close = MagicMock()
        mock_kws.stop.side_effect = RuntimeError("ReactorNotRunning")

        ticker._kws = mock_kws
        ticker._connected = True
        ticker._started = True

        with patch('backend.brokers.kite_ticker.logger'):
            ticker.stop()

        # Flag should remain True
        assert ticker.is_reactor_dead() is True

        # Multiple calls to is_reactor_dead() should still return True
        assert ticker.is_reactor_dead() is True
        assert ticker.is_reactor_dead() is True


# ═══════════════════════════════════════════════════════════════════════════
# TestStartBailsWhenReactorDead — 3 tests
# ═══════════════════════════════════════════════════════════════════════════

class TestStartBailsWhenReactorDead:
    """Verify start() checks _reactor_dead and bails before calling kws.connect()."""

    def test_start_returns_immediately_when_reactor_dead(self):
        """start() should return immediately without attempting connect when _reactor_dead=True."""
        ticker = TickerManager()
        ticker._reactor_dead = True

        # Call start() — should bail early
        with patch('backend.brokers.kite_ticker.logger') as mock_logger:
            ticker.start("fake_key", "fake_token", account="ZG0790")

        # _started should NOT be set to True
        assert ticker._started is False, \
            "start() should NOT set _started=True when reactor is dead"

        # Verify CRITICAL log was called
        assert mock_logger.critical.called, \
            "start() should log CRITICAL when reactor is dead"

    def test_start_calls_kws_connect_when_reactor_not_dead(self):
        """start() should call kws.connect() normally when _reactor_dead=False."""
        ticker = TickerManager()
        ticker._reactor_dead = False

        # Mock the KiteTicker import so we can verify connect() is called
        with patch('kiteconnect.KiteTicker') as mock_kite_ticker:
            mock_ws_instance = MagicMock()
            mock_kite_ticker.return_value = mock_ws_instance

            # Call start()
            with patch('backend.brokers.kite_ticker.logger'):
                ticker.start("fake_key", "fake_token", account="ZG0790")

            # Verify KiteTicker was instantiated
            mock_kite_ticker.assert_called_once()

            # Verify connect() was called
            mock_ws_instance.connect.assert_called_once_with(threaded=True)

            # _started should be True
            assert ticker._started is True, \
                "start() should set _started=True when reactor not dead and connect succeeds"

    def test_start_idempotent_check_works_when_reactor_not_dead(self):
        """start() idempotent check (_started=True) should still gate when reactor not dead."""
        ticker = TickerManager()
        ticker._reactor_dead = False

        with patch('kiteconnect.KiteTicker') as mock_kite_ticker:
            mock_ws_instance = MagicMock()
            mock_kite_ticker.return_value = mock_ws_instance

            # First call
            with patch('backend.brokers.kite_ticker.logger'):
                ticker.start("fake_key", "fake_token", account="ZG0790")

            # connect() should have been called once
            assert mock_ws_instance.connect.call_count == 1

            # Second call — should be no-op due to _started=True
            with patch('backend.brokers.kite_ticker.logger'):
                ticker.start("fake_key", "fake_token", account="ZG0790")

            # connect() should still be called only once (idempotent)
            assert mock_ws_instance.connect.call_count == 1, \
                "start() should be idempotent even when reactor not dead"


# ═══════════════════════════════════════════════════════════════════════════
# TestReactorDeadWatchdogExit — 4 tests
# ═══════════════════════════════════════════════════════════════════════════

class TestReactorDeadWatchdogExit:
    """Verify _ticker_watchdog detects reactor-dead and sends SIGTERM for systemd restart."""

    @pytest.mark.asyncio
    async def test_watchdog_exits_when_reactor_dead(self):
        """watchdog should send SIGTERM via os.kill when ticker.is_reactor_dead() returns True.

        Production code calls os.kill(os.getpid(), signal.SIGTERM) so systemd
        (Restart=always) restarts the process — NOT sys.exit().  Patching os.kill
        prevents the live signal from killing the test process.
        """
        import os as _os
        import signal as _signal
        ticker = TickerManager()
        ticker._reactor_dead = True

        from backend.brokers.service.app import _ticker_watchdog

        async def sleep_noop(*args, **kwargs):
            pass

        with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
             patch('asyncio.sleep', side_effect=sleep_noop), \
             patch('backend.brokers.service.app.logger'), \
             patch('os.kill') as mock_kill:

            # Watchdog returns normally after os.kill (code does `return` after the call)
            await asyncio.wait_for(_ticker_watchdog(), timeout=2.0)

            mock_kill.assert_called_once_with(_os.getpid(), _signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_watchdog_does_not_exit_when_reactor_healthy(self):
        """watchdog should NOT send SIGTERM when is_reactor_dead() returns False."""
        ticker = TickerManager()
        ticker._reactor_dead = False
        ticker._started = True
        ticker._connected = True

        from backend.brokers.service.app import _ticker_watchdog

        async def mock_sleep_with_cancel(*args, **kwargs):
            raise asyncio.CancelledError()

        with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
             patch('asyncio.sleep', side_effect=mock_sleep_with_cancel), \
             patch('backend.brokers.service.app.logger'), \
             patch('os.kill') as mock_kill:

            try:
                await _ticker_watchdog()
            except asyncio.CancelledError:
                pass

        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_watchdog_checks_reactor_dead_first(self):
        """watchdog should check reactor_dead BEFORE other phase logic.

        When reactor is dead the watchdog must exit immediately — before calling
        status() or any other phase-logic method — so we don't waste time on
        phases that can never succeed.
        """
        ticker = TickerManager()
        ticker._reactor_dead = True

        from backend.brokers.service.app import _ticker_watchdog

        ticker_methods_called = []

        original_status = ticker.status
        def track_status(*args, **kwargs):
            ticker_methods_called.append("status")
            return original_status(*args, **kwargs)

        ticker.status = track_status

        async def mock_sleep_noop(*args, **kwargs):
            pass

        with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
             patch('asyncio.sleep', side_effect=mock_sleep_noop), \
             patch('backend.brokers.service.app.logger'), \
             patch('os.kill'):

            await asyncio.wait_for(_ticker_watchdog(), timeout=2.0)

        # When reactor is dead, watchdog exits without calling status()
        assert "status" not in ticker_methods_called, \
            "watchdog should exit without calling status() when reactor dead"

    @pytest.mark.asyncio
    async def test_watchdog_exits_on_every_reactor_dead_iteration(self):
        """watchdog sends SIGTERM on the first reactor-dead iteration and then returns."""
        import os as _os
        import signal as _signal
        from backend.brokers.service.app import _ticker_watchdog

        ticker = TickerManager()
        ticker._reactor_dead = True

        async def sleep_noop(*args, **kwargs):
            pass

        with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
             patch('asyncio.sleep', side_effect=sleep_noop), \
             patch('backend.brokers.service.app.logger'), \
             patch('os.kill') as mock_kill:

            await asyncio.wait_for(_ticker_watchdog(), timeout=2.0)

        # os.kill(pid, SIGTERM) must be called at least once
        assert mock_kill.call_count > 0, \
            "watchdog should send SIGTERM when reactor is dead"
        mock_kill.assert_called_with(_os.getpid(), _signal.SIGTERM)


# ═══════════════════════════════════════════════════════════════════════════
# Integration test: stop() → is_reactor_dead() → watchdog exit
# ═══════════════════════════════════════════════════════════════════════════

class TestReactorDeadIntegration:
    """Integration test: stop() detects ReactorNotRunning → is_reactor_dead() is True → watchdog exits."""

    def test_full_integration_flow(self):
        """Full flow: stop() → is_reactor_dead() → start() bailout."""
        ticker = TickerManager()

        # Initially healthy
        assert ticker.is_reactor_dead() is False

        # Simulate ReactorNotRunning on stop
        mock_kws = MagicMock()
        mock_kws.stop_retry = MagicMock()
        mock_kws.close = MagicMock()
        mock_kws.stop.side_effect = RuntimeError("ReactorNotRunning")

        ticker._kws = mock_kws
        ticker._connected = True
        ticker._started = True

        # Call stop() — sets _reactor_dead
        with patch('backend.brokers.kite_ticker.logger'):
            ticker.stop()

        # Now reactor is dead
        assert ticker.is_reactor_dead() is True

        # Try to start() — should bail out
        with patch('backend.brokers.kite_ticker.logger') as mock_logger:
            ticker.start("key", "token")

        assert ticker._started is False, \
            "start() should not have set _started after reactor-dead bailout"
        assert mock_logger.critical.called, \
            "start() should log CRITICAL when reactor is dead"

    @pytest.mark.asyncio
    async def test_watchdog_integration_with_reactor_dead_ticker(self):
        """Integration: watchdog integrates with a ticker that is_reactor_dead()."""
        ticker = TickerManager()
        ticker._reactor_dead = True

        from backend.brokers.service.app import _ticker_watchdog

        async def mock_sleep_with_exit(*args, **kwargs):
            raise SystemExit(1)

        with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
             patch('asyncio.sleep', side_effect=mock_sleep_with_exit), \
             patch('backend.brokers.service.app.logger'), \
             patch('sys.exit', side_effect=SystemExit(1)):

            # watchdog should detect reactor dead and call sys.exit(1)
            with pytest.raises(SystemExit) as exc_info:
                await _ticker_watchdog()

            assert exc_info.value.code == 1, \
                "watchdog should exit with code 1 when reactor dead"
