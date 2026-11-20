"""Tests for F1(a,b) and F7 (2026-11-20 incident: account ZG0790's Kite
login was blocked by a Zerodha CAPTCHA challenge for ~8.5h):

F1(a) — conn_service's `_health_heartbeat()` must NOT call
  `record_session_ok()` for a Kite account that is currently cooling off
  (`KiteConnection.is_login_cooling_off()`), regardless of the pre-existing
  `_is_token_expired()` gate (which `KiteConnection` never implements, so
  it was previously skipped entirely for every Kite account — producing a
  false-green health chip throughout the incident despite 1,503 real auth
  failures).

F1(b) — conn_service's `/health/brokers` payload must carry each Kite
  account's live cooloff state as `is_cooling_off: bool` and
  `blocked_until: float | None`, merged on top of
  `broker_apis.fetch_health_snapshot()`'s own per-account dict.

F7 — `PriceBroker._try()` (registry.py) must proactively skip a Kite
  broker that's currently cooling off, with NO call into the underlying
  SDK/`get_kite_conn()` — a pure efficiency cleanup alongside the one-shot
  exception-fallthrough behaviour that already worked correctly during
  the incident.

Five quality dimensions:
  SSOT        — all three fixes read the single source of truth,
                `KiteConnection.is_login_cooling_off()` /
                `cooloff_blocked_until()` — no duplicated cooloff logic.
  Correctness — a cooling-off Kite account never gets a green stamp, is
                never counted as 'available' by health, and never takes a
                real network hit via PriceBroker; a non-cooling account
                (or a Dhan/Groww account with no such method at all) is
                completely unaffected.
  Performance — F7's own assertion is that the underlying broker method
                is NEVER CALLED when cooling off — the actual efficiency
                claim, not just "falls through eventually".
  Reuse       — exercises the REAL `_health_heartbeat()` coroutine (one
                iteration, via a mocked `asyncio.sleep` escape hatch) for
                F1(a), not a hand-copied replica of its gate logic.
  UX          — F1(b)'s payload assertions check both fields' exact
                values, not just presence.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# F1(a) — _health_heartbeat skips record_session_ok while cooling off
# ---------------------------------------------------------------------------

class TestHeartbeatSkipsGreenStampWhileCoolingOff:
    @pytest.mark.asyncio
    async def test_cooling_off_kite_account_never_gets_record_session_ok(self, monkeypatch):
        import backend.brokers.service.app as app_mod

        # Kite-shaped mock: HAS is_login_cooling_off, explicitly does NOT
        # have _is_token_expired (spec= restricts the attribute surface —
        # a bare MagicMock() auto-vivifies any attribute access, which
        # would silently defeat this exact test).
        cooling_conn = MagicMock(spec=["is_login_cooling_off"])
        cooling_conn.is_login_cooling_off.return_value = True

        healthy_conn = MagicMock(spec=["is_login_cooling_off"])
        healthy_conn.is_login_cooling_off.return_value = False

        mock_connections = MagicMock()
        mock_connections.conn = {
            "ZG_COOLING": cooling_conn,
            "ZJ_HEALTHY": healthy_conn,
        }

        record_mock = MagicMock()

        app_mod._heartbeat_cooloff_warned.discard("ZG_COOLING")
        app_mod._heartbeat_cooloff_warned.discard("ZJ_HEALTHY")

        sleep_calls = {"n": 0}

        async def fake_sleep(secs):
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 2:
                raise asyncio.CancelledError()

        with patch("backend.brokers.connections.Connections", return_value=mock_connections), \
             patch("backend.brokers.broker_apis.record_session_ok", record_mock), \
             patch("asyncio.sleep", fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await app_mod._health_heartbeat()

        assert record_mock.call_args_list == [(("ZJ_HEALTHY",), {})], (
            f"expected record_session_ok called ONLY for the non-cooling "
            f"account, got calls: {record_mock.call_args_list!r}"
        )

    @pytest.mark.asyncio
    async def test_dhan_style_account_with_no_cooloff_method_unaffected(self, monkeypatch):
        """A Dhan/Groww-style connection object has no `is_login_cooling_off`
        at all — the new gate must be a complete no-op for it, falling
        through to the existing `_is_token_expired` gate unchanged."""
        import backend.brokers.service.app as app_mod

        dhan_conn = MagicMock(spec=["_is_token_expired"])
        dhan_conn._is_token_expired.return_value = False

        mock_connections = MagicMock()
        mock_connections.conn = {"DH_ACCT": dhan_conn}

        record_mock = MagicMock()
        app_mod._heartbeat_cooloff_warned.discard("DH_ACCT")

        sleep_calls = {"n": 0}

        async def fake_sleep(secs):
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 2:
                raise asyncio.CancelledError()

        with patch("backend.brokers.connections.Connections", return_value=mock_connections), \
             patch("backend.brokers.broker_apis.record_session_ok", record_mock), \
             patch("asyncio.sleep", fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await app_mod._health_heartbeat()

        record_mock.assert_called_once_with("DH_ACCT")


# ---------------------------------------------------------------------------
# F1(b) — /health/brokers payload carries is_cooling_off / blocked_until
# ---------------------------------------------------------------------------

class TestMergeKiteCooloffState:
    """`broker_apis.merge_kite_cooloff_state()` — the single shared
    helper both `fetch_health_snapshot()`'s local branch AND
    conn_service's `/health/brokers` route (via that same local branch —
    conn_service leaves `RAMBOQ_USE_CONN_SERVICE` unset in its own
    process) rely on. Testing it directly, rather than through the
    route, is more durable: it stays correct even if a future caller
    reaches it a different way."""

    def test_cooling_off_kite_account_merged_into_snapshot(self):
        from backend.brokers import broker_apis

        cooling_conn = MagicMock(spec=["is_login_cooling_off", "cooloff_blocked_until"])
        cooling_conn.is_login_cooling_off.return_value = True
        cooling_conn.cooloff_blocked_until.return_value = 1234567890.5

        mock_connections = MagicMock()
        mock_connections.conn = {"ZG0790": cooling_conn}

        snapshot = {"ZG0790": {"last_ok_at": 1.0, "last_fail_at": 2.0}}
        with patch(
            "backend.brokers.connections.Connections", return_value=mock_connections
        ):
            result = broker_apis.merge_kite_cooloff_state(snapshot)

        entry = result["ZG0790"]
        assert entry["is_cooling_off"] is True
        assert entry["blocked_until"] == 1234567890.5
        # Pre-existing fields must survive the merge untouched.
        assert entry["last_ok_at"] == 1.0
        assert entry["last_fail_at"] == 2.0

    def test_not_cooling_off_kite_account_has_false_and_none(self):
        from backend.brokers import broker_apis

        healthy_conn = MagicMock(spec=["is_login_cooling_off", "cooloff_blocked_until"])
        healthy_conn.is_login_cooling_off.return_value = False

        mock_connections = MagicMock()
        mock_connections.conn = {"ZJ6294": healthy_conn}

        with patch(
            "backend.brokers.connections.Connections", return_value=mock_connections
        ):
            result = broker_apis.merge_kite_cooloff_state({})

        entry = result["ZJ6294"]
        assert entry["is_cooling_off"] is False
        assert entry["blocked_until"] is None
        healthy_conn.cooloff_blocked_until.assert_not_called(), (
            "blocked_until must only be resolved when actually cooling off"
        )

    def test_dhan_account_gets_no_cooloff_fields_at_all(self):
        """An account whose connection has no is_login_cooling_off method
        (Dhan/Groww) must be left completely untouched — no fields added."""
        from backend.brokers import broker_apis

        dhan_conn = MagicMock(spec=["_is_token_expired"])

        mock_connections = MagicMock()
        mock_connections.conn = {"DH3747": dhan_conn}

        snapshot = {"DH3747": {"last_ok_at": 5.0}}
        with patch(
            "backend.brokers.connections.Connections", return_value=mock_connections
        ):
            result = broker_apis.merge_kite_cooloff_state(snapshot)

        entry = result["DH3747"]
        assert "is_cooling_off" not in entry, (
            f"Dhan account must not get cooloff fields at all, got {entry!r}"
        )
        assert "blocked_until" not in entry

    def test_cooloff_check_raising_fails_open(self):
        """A failure inside the cooloff check itself must degrade to
        is_cooling_off=False, never raise into the caller."""
        from backend.brokers import broker_apis

        flaky_conn = MagicMock(spec=["is_login_cooling_off"])
        flaky_conn.is_login_cooling_off.side_effect = RuntimeError("boom")

        mock_connections = MagicMock()
        mock_connections.conn = {"ZG0790": flaky_conn}

        with patch(
            "backend.brokers.connections.Connections", return_value=mock_connections
        ):
            result = broker_apis.merge_kite_cooloff_state({})

        assert result["ZG0790"]["is_cooling_off"] is False


class TestFetchHealthSnapshotLocalBranchCallsTheMerge:
    """`fetch_health_snapshot()`'s LOCAL (non-cutover) branch must call
    `merge_kite_cooloff_state()` itself — this is what conn_service's own
    `/health/brokers` route relies on (conn_service leaves
    `RAMBOQ_USE_CONN_SERVICE` unset in its own process, so it always
    takes this branch), and it's also what a non-cutover main API
    process relies on directly. Covers the gap a route-only merge would
    have left: the route calling `fetch_health_snapshot()` and getting
    back an unmerged snapshot."""

    def test_local_branch_merges_cooloff_state(self):
        from backend.brokers import broker_apis

        cooling_conn = MagicMock(spec=["is_login_cooling_off", "cooloff_blocked_until"])
        cooling_conn.is_login_cooling_off.return_value = True
        cooling_conn.cooloff_blocked_until.return_value = 999.0

        mock_connections = MagicMock()
        mock_connections.conn = {"ZG0790": cooling_conn}

        with patch.object(broker_apis, "_USE_CONN_SERVICE", False), \
             patch.object(broker_apis, "_FETCH_HEALTH", {"ZG0790": {"last_ok_at": 1.0}}), \
             patch(
                 "backend.brokers.connections.Connections", return_value=mock_connections
             ):
            result = broker_apis.fetch_health_snapshot()

        assert result["ZG0790"]["is_cooling_off"] is True
        assert result["ZG0790"]["blocked_until"] == 999.0
        assert result["ZG0790"]["last_ok_at"] == 1.0


class TestBrokerHealthRouteDelegatesToFetchHealthSnapshot:
    """The conn_service route is now a plain pass-through — the merge
    lives entirely in `fetch_health_snapshot()` so it applies uniformly
    whether the caller is the route or a direct local caller."""

    @pytest.mark.asyncio
    async def test_route_returns_exactly_what_fetch_health_snapshot_returns(self):
        from backend.brokers.service.routes import InternalBrokerController

        sentinel = {"ZG0790": {"is_cooling_off": True, "blocked_until": 42.0}}
        with patch(
            "backend.brokers.broker_apis.fetch_health_snapshot",
            return_value=sentinel,
        ):
            controller = InternalBrokerController.__new__(InternalBrokerController)
            result = await InternalBrokerController.broker_health.fn(controller)

        assert result == {"health": sentinel}


# ---------------------------------------------------------------------------
# F7 — PriceBroker._try() proactively skips a cooling-off Kite broker
# ---------------------------------------------------------------------------

class TestPriceBrokerProactivelySkipsCoolingOffKite:
    def test_cooling_off_broker_is_skipped_with_no_network_call(self):
        from backend.brokers.registry import PriceBroker

        cooling_kite = MagicMock()
        cooling_kite.broker_id = "zerodha_kite"
        cooling_kite.account = "ZG0790"
        cooling_kite._conn = MagicMock(spec=["is_login_cooling_off"])
        cooling_kite._conn.is_login_cooling_off.return_value = True
        cooling_kite.quote = MagicMock(
            side_effect=AssertionError("must never be called while cooling off")
        )

        fallback_broker = MagicMock()
        fallback_broker.broker_id = "dhan"
        fallback_broker.account = "DH3747"
        fallback_broker.quote = MagicMock(return_value={"INFY": {"last_price": 100.0}})
        # Dhan has no `_conn.is_login_cooling_off` — getattr(None, ...) path.
        del fallback_broker._conn

        pb = PriceBroker([cooling_kite, fallback_broker])
        result = pb.quote(["INFY"])

        cooling_kite.quote.assert_not_called()
        fallback_broker.quote.assert_called_once()
        assert result == {"INFY": {"last_price": 100.0}}
        assert pb.last_served_by() == "dhan/DH3747"

    def test_not_cooling_off_kite_broker_is_called_normally(self):
        from backend.brokers.registry import PriceBroker

        healthy_kite = MagicMock()
        healthy_kite.broker_id = "zerodha_kite"
        healthy_kite.account = "ZG0790"
        healthy_kite._conn = MagicMock(spec=["is_login_cooling_off"])
        healthy_kite._conn.is_login_cooling_off.return_value = False
        healthy_kite.quote = MagicMock(return_value={"INFY": {"last_price": 101.0}})

        pb = PriceBroker([healthy_kite])
        result = pb.quote(["INFY"])

        healthy_kite.quote.assert_called_once()
        assert result == {"INFY": {"last_price": 101.0}}

    def test_broker_with_no_conn_attribute_is_unaffected(self):
        """A broker adapter with no `_conn` at all (or a RemoteBroker stub
        under conn-service cutover) must not be skipped by this check —
        getattr(..., None) + callable() must degrade to a no-op."""
        from backend.brokers.registry import PriceBroker

        remote_stub = MagicMock(spec=["broker_id", "account", "quote"])
        remote_stub.broker_id = "zerodha_kite"
        remote_stub.account = "ZJ6294"
        remote_stub.quote = MagicMock(return_value={"INFY": {"last_price": 99.0}})

        pb = PriceBroker([remote_stub])
        result = pb.quote(["INFY"])

        remote_stub.quote.assert_called_once()
        assert result == {"INFY": {"last_price": 99.0}}

    def test_cooloff_check_raising_fails_open_tries_the_broker_anyway(self):
        """A failure inside the cooloff check itself (e.g. a weird mock or
        future refactor) must never itself block an otherwise-healthy
        broker — fail open, try the real call."""
        from backend.brokers.registry import PriceBroker

        flaky_kite = MagicMock()
        flaky_kite.broker_id = "zerodha_kite"
        flaky_kite.account = "ZG0790"
        flaky_kite._conn = MagicMock(spec=["is_login_cooling_off"])
        flaky_kite._conn.is_login_cooling_off.side_effect = RuntimeError("boom")
        flaky_kite.quote = MagicMock(return_value={"INFY": {"last_price": 102.0}})

        pb = PriceBroker([flaky_kite])
        result = pb.quote(["INFY"])

        flaky_kite.quote.assert_called_once()
        assert result == {"INFY": {"last_price": 102.0}}
