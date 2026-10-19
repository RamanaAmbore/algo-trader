"""Tests for _snapshot_unsub_nonmcx / _snapshot_stop_ticker /
_snapshot_restart_ticker (backend/api/background.py).

Prior to the 2026-10-19 fix these three functions branched on
hasattr(ticker_obj, "...") instead of the explicit is_cutover_on() mode
flag — a bug class because MmapTickReader genuinely HAS a .stop()
method (it really cancels the local poll task), so the hasattr guard
let _snapshot_stop_ticker silently kill the local mmap-tick forwarder
every night in conn-service mode, with no corresponding 08:00 restart
(MmapTickReader has no .restart(), so that hasattr guard correctly
no-op'd — but for the wrong reason, and _snapshot_restart_ticker now
actively self-heals instead of skipping).

Covers both branches explicitly:
  • conn-service mode (is_cutover_on() -> True): stop/unsub are no-ops
    on the ticker object; restart calls check_and_heal() instead.
  • direct-KiteTicker mode (is_cutover_on() -> False): behavior is
    byte-for-byte unchanged from before this fix — real .stop() /
    .restart() / .unsubscribe_non_mcx() are called.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ── _snapshot_unsub_nonmcx ──────────────────────────────────────────────────

async def test_unsub_nonmcx_conn_service_mode_is_noop():
    """In conn-service mode, unsubscribe_non_mcx() is never called on the
    ticker object — MmapTickReader owns no socket/subscription list to
    trim (that lives in conn_service)."""
    from backend.api import background as _bg

    mock_ticker = MagicMock()
    mock_ticker.unsubscribe_non_mcx = AsyncMock()

    with patch("backend.api.background.is_cutover_on", return_value=True), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker):
        await _bg._snapshot_unsub_nonmcx()

    mock_ticker.unsubscribe_non_mcx.assert_not_called()


async def test_unsub_nonmcx_direct_mode_calls_real_method():
    """In direct-KiteTicker mode, unsubscribe_non_mcx() IS called —
    unchanged behavior from before the fix."""
    from backend.api import background as _bg

    mock_ticker = MagicMock()
    mock_ticker.unsubscribe_non_mcx = AsyncMock()

    with patch("backend.api.background.is_cutover_on", return_value=False), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker):
        await _bg._snapshot_unsub_nonmcx()

    mock_ticker.unsubscribe_non_mcx.assert_awaited_once()


async def test_unsub_nonmcx_direct_mode_exception_is_caught_and_logged():
    """A broker exception during unsubscribe_non_mcx() is caught, not
    propagated — matches the function's existing try/except contract."""
    from backend.api import background as _bg

    mock_ticker = MagicMock()
    mock_ticker.unsubscribe_non_mcx = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("backend.api.background.is_cutover_on", return_value=False), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker):
        await _bg._snapshot_unsub_nonmcx()  # must not raise


# ── _snapshot_stop_ticker ───────────────────────────────────────────────────

async def test_stop_ticker_conn_service_mode_is_noop():
    """In conn-service mode, .stop() is never called on the ticker object
    — this is the exact bug this fix closes: MmapTickReader.stop() is a
    real method that really cancels the local poll task, so calling it
    here would kill the local forwarder every night with no restart."""
    from backend.api import background as _bg

    mock_ticker = MagicMock()
    mock_ticker.stop = MagicMock()

    with patch("backend.api.background.is_cutover_on", return_value=True), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker):
        _bg._snapshot_stop_ticker()

    mock_ticker.stop.assert_not_called()


async def test_stop_ticker_direct_mode_calls_real_method():
    """In direct-KiteTicker mode, .stop() IS called — unchanged behavior."""
    from backend.api import background as _bg

    mock_ticker = MagicMock()
    mock_ticker.stop = MagicMock()

    with patch("backend.api.background.is_cutover_on", return_value=False), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker):
        _bg._snapshot_stop_ticker()

    mock_ticker.stop.assert_called_once()


async def test_stop_ticker_direct_mode_exception_is_caught_and_logged():
    from backend.api import background as _bg

    mock_ticker = MagicMock()
    mock_ticker.stop = MagicMock(side_effect=RuntimeError("socket gone"))

    with patch("backend.api.background.is_cutover_on", return_value=False), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker):
        _bg._snapshot_stop_ticker()  # must not raise


# ── _snapshot_restart_ticker ─────────────────────────────────────────────────

async def test_restart_ticker_conn_service_mode_calls_check_and_heal():
    """In conn-service mode with the engine active, restart calls
    check_and_heal() on the MmapTickReader instead of silently skipping
    (the old hasattr-based no-op)."""
    from backend.api import background as _bg

    mock_ticker = MagicMock()
    mock_ticker.check_and_heal = MagicMock(return_value=True)
    mock_ticker.restart = MagicMock()  # must NOT be called in this mode

    with patch("backend.api.background.is_cutover_on", return_value=True), \
         patch("backend.shared.helpers.utils.is_engine_idle", return_value=False), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker):
        _bg._snapshot_restart_ticker()

    mock_ticker.check_and_heal.assert_called_once()
    mock_ticker.restart.assert_not_called()


async def test_restart_ticker_conn_service_mode_idle_skips_heal():
    """In conn-service mode with the engine idle, restart must NOT call
    check_and_heal() — resurrecting a reader the app chose not to start
    would fire a false CRITICAL on an idle dev boot."""
    from backend.api import background as _bg

    mock_ticker = MagicMock()
    mock_ticker.check_and_heal = MagicMock(return_value=True)

    with patch("backend.api.background.is_cutover_on", return_value=True), \
         patch("backend.shared.helpers.utils.is_engine_idle", return_value=True), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker):
        _bg._snapshot_restart_ticker()

    mock_ticker.check_and_heal.assert_not_called()


async def test_restart_ticker_direct_mode_calls_real_restart_with_credentials():
    """In direct-KiteTicker mode, .restart(api_key, access_token, account)
    IS called with credentials resolved from Connections().conn —
    unchanged behavior from before this fix."""
    from backend.api import background as _bg

    mock_ticker = MagicMock()
    mock_ticker.restart = MagicMock()
    mock_ticker.check_and_heal = MagicMock()  # must NOT be called in this mode

    mock_kite = MagicMock()
    mock_kite.api_key = "key_123"
    mock_kite.access_token = "token_456"
    mock_conn = MagicMock()
    mock_conn.kite = mock_kite

    mock_connections_instance = MagicMock()
    mock_connections_instance.conn = {"ACC1": mock_conn}

    with patch("backend.api.background.is_cutover_on", return_value=False), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker), \
         patch("backend.brokers.connections.Connections", return_value=mock_connections_instance):
        _bg._snapshot_restart_ticker()

    mock_ticker.restart.assert_called_once_with("key_123", "token_456", "ACC1")
    mock_ticker.check_and_heal.assert_not_called()


async def test_restart_ticker_direct_mode_no_credentials_logs_warning_and_skips():
    """Direct mode with no live Kite credentials found: restart() is not
    called, and the function doesn't raise."""
    from backend.api import background as _bg

    mock_ticker = MagicMock()
    mock_ticker.restart = MagicMock()

    mock_conn = MagicMock(spec=[])  # no `kite` attribute at all
    mock_connections_instance = MagicMock()
    mock_connections_instance.conn = {"ACC1": mock_conn}

    with patch("backend.api.background.is_cutover_on", return_value=False), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker), \
         patch("backend.brokers.connections.Connections", return_value=mock_connections_instance):
        _bg._snapshot_restart_ticker()  # must not raise

    mock_ticker.restart.assert_not_called()


async def test_restart_ticker_direct_mode_exception_is_caught_and_logged():
    from backend.api import background as _bg

    mock_ticker = MagicMock()
    mock_ticker.restart = MagicMock(side_effect=RuntimeError("connect failed"))

    mock_kite = MagicMock()
    mock_kite.api_key = "key_123"
    mock_kite.access_token = "token_456"
    mock_conn = MagicMock()
    mock_conn.kite = mock_kite
    mock_connections_instance = MagicMock()
    mock_connections_instance.conn = {"ACC1": mock_conn}

    with patch("backend.api.background.is_cutover_on", return_value=False), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker), \
         patch("backend.brokers.connections.Connections", return_value=mock_connections_instance):
        _bg._snapshot_restart_ticker()  # must not raise
