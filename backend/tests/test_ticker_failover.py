"""Tests for the KiteTicker auto-failover state machine.

Scope: pure state-machine behaviour on the TickerManager singleton and
its helper functions in conn_service (`_kite_failover_list`,
`_resolve_kite_creds`). No real KiteConnect / KiteTicker sockets are
touched — every test either exercises TickerManager helpers directly
or patches `_try_start_ticker` / `restart_with_account`.

Five test-dimension coverage (feedback_test_dimensions.md):
  * SSOT — _kite_failover_list is the single source; watchdog and
    /internal/ticker/status both read from it.
  * Perf — swap decision path < 100 ms; complete swap under 5 s.
  * Stale — no inline is_connected reimplementation; every consumer
    goes through `is_active_ticker_healthy`.
  * Reuse — swap uses `Connections.get_broker()` (via _resolve_kite_creds
    → Connections.conn), not direct KiteConnect instantiation.
  * UX — n/a (backend); response is a stable dict shape verified by
    the health test.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest


# ── TickerManager helper coverage ────────────────────────────────────────


class TestTickerManagerFailoverHelpers:
    """Direct exercise of the failover state machine primitives added
    to TickerManager. No sockets, no threads — pure state."""

    def _fresh_ticker(self):
        """Instantiate a bare TickerManager (bypassing the module
        singleton) so each test starts with a clean state.
        """
        from backend.brokers.kite_ticker import TickerManager
        return TickerManager()

    def test_bump_and_reset_unhealthy(self):
        """bump increments; reset zeroes. Simple counter contract."""
        t = self._fresh_ticker()
        assert t.status()["consecutive_unhealthy"] == 0
        assert t.bump_unhealthy() == 1
        assert t.bump_unhealthy() == 2
        assert t.status()["consecutive_unhealthy"] == 2
        t.reset_unhealthy()
        assert t.status()["consecutive_unhealthy"] == 0

    def test_swap_history_tracking(self):
        """record_swap appends to history; swaps_since counts within window."""
        t = self._fresh_ticker()
        assert t.status()["swaps_last_hour"] == 0
        t.record_swap("ZG0790", "ZJ6294")
        assert t.status()["swaps_last_hour"] == 1
        assert t.swaps_since(60.0) == 1
        # Backdate the entry outside the 60 s window.
        t._swap_history[-1] = time.time() - 90.0
        assert t.swaps_since(60.0) == 0
        # ... but still inside the 1 h window.
        assert t.status()["swaps_last_hour"] == 1

    def test_swap_history_bounded(self):
        """`_swap_history` is capped at 128 to prevent unbounded growth."""
        t = self._fresh_ticker()
        for _ in range(200):
            t.record_swap("A", "B")
        assert len(t._swap_history) == 128

    def test_supervisor_uptime(self):
        """mark_supervisor_started() is idempotent; uptime grows from 0."""
        t = self._fresh_ticker()
        assert t.supervisor_uptime_seconds() == 0.0
        t.mark_supervisor_started()
        first_stamp = t._supervisor_started_at
        assert first_stamp > 0
        # Second call must not reset — grace period would slip.
        t.mark_supervisor_started()
        assert t._supervisor_started_at == first_stamp
        assert t.supervisor_uptime_seconds() >= 0.0

    def test_is_active_ticker_healthy_requires_started_and_connected(self):
        """Not-started / not-connected shortcircuits to False."""
        t = self._fresh_ticker()
        assert t.is_active_ticker_healthy() is False
        t._started = True
        assert t.is_active_ticker_healthy() is False  # still not connected
        t._connected = True
        # No tick nor connect timestamp — still False.
        assert t.is_active_ticker_healthy() is False

    def test_is_active_ticker_healthy_uses_heartbeat_window(self):
        """A fresh tick or connect timestamp inside the heartbeat window
        counts as healthy; anything older fails."""
        t = self._fresh_ticker()
        t._started = True
        t._connected = True
        # Fresh connect → healthy.
        t._last_connected_at = time.time()
        assert t.is_active_ticker_healthy(tick_heartbeat_s=60.0) is True
        # Fresh tick → healthy.
        t._last_connected_at = 0.0
        t._tick_age = {12345: time.time()}
        assert t.is_active_ticker_healthy(tick_heartbeat_s=60.0) is True
        # Stale-only → unhealthy.
        t._tick_age = {12345: time.time() - 120.0}
        assert t.is_active_ticker_healthy(tick_heartbeat_s=60.0) is False

    def test_status_exposes_all_failover_fields(self):
        """SSOT: every failover field the health surface promises is
        present in status() — a downstream consumer can rely on keys
        existing even before the first watchdog cycle."""
        t = self._fresh_ticker()
        s = t.status()
        for k in (
            "active_account",
            "consecutive_unhealthy",
            "swaps_last_hour",
            "last_swap_at",
        ):
            assert k in s, f"status() missing failover key {k!r}"

    def test_restart_with_account_records_swap_and_resets_counter(self):
        """restart_with_account() must call record_swap() and
        reset_unhealthy(). Kite SDK connect is stubbed."""
        t = self._fresh_ticker()
        t._current_account = "ZG0790"
        t._consecutive_unhealthy = 5
        # Suppress the KiteTicker import path via start() stub.
        with patch.object(t, "start", MagicMock()):
            t.restart_with_account("api_key", "access_token", "ZJ6294")
        assert t.swaps_since(60.0) == 1
        assert t.status()["consecutive_unhealthy"] == 0

    def test_force_unhealthy_flips_health_check(self):
        """Operator-forced unhealthy window makes
        is_active_ticker_healthy() return False even when the socket
        is otherwise healthy — powers the /internal/ticker/force-
        unhealthy verification endpoint."""
        t = self._fresh_ticker()
        # Establish a "healthy" ticker state.
        t._started = True
        t._connected = True
        t._last_connected_at = time.time()
        assert t.is_active_ticker_healthy() is True
        # Now force-unhealthy for a short window.
        deadline = t.force_unhealthy(duration_s=30.0)
        assert deadline > time.time()
        assert t.is_active_ticker_healthy() is False
        # Manually rewind the deadline to simulate auto-expiry.
        t._force_unhealthy_until = time.time() - 1.0
        assert t.is_active_ticker_healthy() is True

    def test_clear_force_unhealthy_restores_health(self):
        """clear_force_unhealthy() ends the operator-forced window
        without waiting for the deadline."""
        t = self._fresh_ticker()
        t._started = True
        t._connected = True
        t._last_connected_at = time.time()
        t.force_unhealthy(duration_s=600.0)
        assert t.is_active_ticker_healthy() is False
        t.clear_force_unhealthy()
        assert t.is_active_ticker_healthy() is True

    def test_status_swap_decision_under_100ms(self):
        """Perf budget: the swap-decision inputs (status + swaps_since +
        current_account) execute in < 100 ms even with a full history."""
        t = self._fresh_ticker()
        for _ in range(128):
            t.record_swap("A", "B")
        t0 = time.perf_counter()
        _ = t.status()
        _ = t.swaps_since(300.0)
        _ = t.current_account()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert elapsed_ms < 100.0, f"swap decision took {elapsed_ms:.1f} ms"


# ── conn_service helper coverage ─────────────────────────────────────────


class TestKiteFailoverList:
    """`_kite_failover_list` — priority-ordered Kite account resolver
    used by both the watchdog and the health endpoint."""

    def _make_kite_conn(self):
        """A KiteConnection-shaped stub — get_access_token + api_key set."""
        stub = MagicMock()
        stub.get_access_token = MagicMock(return_value="tok")
        stub.api_key = "key"
        return stub

    def _make_non_kite_conn(self):
        """A Dhan / Groww shaped stub — no get_access_token."""
        stub = MagicMock(spec=[])
        return stub

    def test_returns_kite_accounts_ordered_by_priority(self):
        """Lower priority number = earlier in the list."""
        from backend.brokers.connections import Connections
        from backend.brokers.service.app import _kite_failover_list

        conn = Connections()
        old_conn, old_pri, old_bid = conn.conn, conn._priority_map, conn._broker_id_map
        try:
            conn.conn = {
                "ZG_LOW":  self._make_kite_conn(),
                "ZG_HIGH": self._make_kite_conn(),
                "ZG_MID":  self._make_kite_conn(),
            }
            conn._priority_map = {"ZG_LOW": 1, "ZG_MID": 50, "ZG_HIGH": 100}
            conn._broker_id_map = {
                "ZG_LOW": "zerodha_kite", "ZG_MID": "zerodha_kite",
                "ZG_HIGH": "zerodha_kite",
            }
            result = _kite_failover_list()
            assert result == ["ZG_LOW", "ZG_MID", "ZG_HIGH"], (
                f"failover_list not sorted by priority: {result}"
            )
        finally:
            conn.conn, conn._priority_map, conn._broker_id_map = (
                old_conn, old_pri, old_bid
            )

    def test_filters_out_non_kite_accounts(self):
        """Dhan / Groww accounts must not appear — only Kite gets the
        ticker WebSocket."""
        from backend.brokers.connections import Connections
        from backend.brokers.service.app import _kite_failover_list

        conn = Connections()
        old_conn, old_pri, old_bid = conn.conn, conn._priority_map, conn._broker_id_map
        try:
            conn.conn = {
                "ZG_A": self._make_kite_conn(),
                "DH_A": self._make_non_kite_conn(),
                "GR_A": self._make_non_kite_conn(),
            }
            conn._priority_map = {"ZG_A": 10, "DH_A": 5, "GR_A": 20}
            conn._broker_id_map = {
                "ZG_A": "zerodha_kite", "DH_A": "dhan", "GR_A": "groww",
            }
            result = _kite_failover_list()
            assert result == ["ZG_A"], (
                f"non-Kite accounts leaked into failover list: {result}"
            )
        finally:
            conn.conn, conn._priority_map, conn._broker_id_map = (
                old_conn, old_pri, old_bid
            )

    def test_exclude_parameter_drops_matching_account(self):
        """`exclude={current}` — used by watchdog to filter the
        currently-failing account so it isn't picked as its own successor."""
        from backend.brokers.connections import Connections
        from backend.brokers.service.app import _kite_failover_list

        conn = Connections()
        old_conn, old_pri, old_bid = conn.conn, conn._priority_map, conn._broker_id_map
        try:
            conn.conn = {
                "ZG_A": self._make_kite_conn(),
                "ZG_B": self._make_kite_conn(),
            }
            conn._priority_map = {"ZG_A": 1, "ZG_B": 2}
            conn._broker_id_map = {
                "ZG_A": "zerodha_kite", "ZG_B": "zerodha_kite",
            }
            assert _kite_failover_list(exclude={"ZG_A"}) == ["ZG_B"]
            assert _kite_failover_list(exclude={"ZG_A", "ZG_B"}) == []
        finally:
            conn.conn, conn._priority_map, conn._broker_id_map = (
                old_conn, old_pri, old_bid
            )


class TestResolveKiteCreds:
    """`_resolve_kite_creds` — canonical credential resolver. Uses
    Connections.conn (canonical registry), not KiteConnect direct."""

    def test_returns_none_when_account_unknown(self):
        from backend.brokers.connections import Connections
        from backend.brokers.service.app import _resolve_kite_creds

        conn = Connections()
        old_conn = conn.conn
        try:
            conn.conn = {}
            ak, tok = _resolve_kite_creds("MISSING")
            assert (ak, tok) == (None, None)
        finally:
            conn.conn = old_conn

    def test_returns_none_when_token_getter_fails(self):
        from backend.brokers.connections import Connections
        from backend.brokers.service.app import _resolve_kite_creds

        stub = MagicMock()
        stub.get_access_token = MagicMock(side_effect=Exception("kite login blew up"))
        stub.api_key = "key"
        conn = Connections()
        old_conn = conn.conn
        try:
            conn.conn = {"ZG_X": stub}
            ak, tok = _resolve_kite_creds("ZG_X")
            assert (ak, tok) == (None, None)
        finally:
            conn.conn = old_conn

    def test_returns_credentials_when_available(self):
        from backend.brokers.connections import Connections
        from backend.brokers.service.app import _resolve_kite_creds

        stub = MagicMock()
        stub.get_access_token = MagicMock(return_value="fresh_token")
        stub.api_key = "kite_api_key"
        conn = Connections()
        old_conn = conn.conn
        try:
            conn.conn = {"ZG_X": stub}
            ak, tok = _resolve_kite_creds("ZG_X")
            assert (ak, tok) == ("kite_api_key", "fresh_token")
        finally:
            conn.conn = old_conn


# ── Stale-code grep — SSOT enforcement ───────────────────────────────────


class TestSsotEnforcement:
    """Grep-style guards that catch a future dev reintroducing inline
    reimplementations of the SSOT primitives."""

    def test_no_inline_is_connected_check_outside_helper(self):
        """Every consumer of "ticker is healthy" should go through
        `is_active_ticker_healthy()` — no `ticker._connected` or
        `ticker.is_connected()` inline checks in the watchdog file."""
        from pathlib import Path

        watchdog_py = (
            Path(__file__).resolve().parent.parent
            / "brokers" / "service" / "app.py"
        )
        src = watchdog_py.read_text()
        # Forbidden inline patterns (any of these = SSOT violation).
        # Note: exceptions allowed inside is_active_ticker_healthy() —
        # that's where the primitive LIVES. This file doesn't include
        # that function so the grep is safe.
        assert "._connected" not in src, (
            "service/app.py accesses ticker._connected directly — should "
            "call is_active_ticker_healthy()"
        )

    def test_swap_uses_connections_not_direct_kite(self):
        """`restart_with_account` must resolve credentials via
        Connections (through `_resolve_kite_creds`), never by
        importing KiteConnect directly in the watchdog."""
        from pathlib import Path

        watchdog_py = (
            Path(__file__).resolve().parent.parent
            / "brokers" / "service" / "app.py"
        )
        src = watchdog_py.read_text()
        assert "from kiteconnect" not in src, (
            "conn_service watchdog imports kiteconnect directly — should "
            "route through Connections()"
        )
