"""
Tests for backend/brokers/service/routes.py — conn_service HTTP endpoints.

Coverage:
1. GET /health — health probe returns status with ticker snapshot
2. GET /internal/accounts — returns loaded broker accounts
3. POST /internal/rebuild — triggers Connections.rebuild_from_db()
4. GET /internal/holdings|positions|margins — multi-broker aggregations
5. GET /internal/health/brokers — per-account fetch-health snapshot
6. POST /internal/broker/{account}/call/{method} — generic dispatch
7. POST /internal/broker/{account}/verify_postback — Kite HMAC verification
8. POST /internal/ticker/subscribe — KiteTicker subscription
9. GET /internal/ticker/status — ticker health snapshot
10. Error handling: 404 for unknown account, 403 for disallowed method
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, time as dtime

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from litestar.testing import TestClient

import pandas as pd

# Explicit imports to ensure coverage tracking
import backend.brokers.service.routes
import backend.brokers.service.calendar_gate


@pytest.fixture
def conn_app():
    """Create conn_service Litestar app for testing.

    Patches startup tasks to avoid real broker calls.
    """
    # Patch the startup hooks to skip real broker connection
    with patch('backend.brokers.service.app._init_connections_on_startup', new_callable=AsyncMock), \
         patch('backend.brokers.service.app._start_kite_ticker', new_callable=AsyncMock):

        from backend.brokers.service.app import create_app
        app = create_app()

        # Manually set up mock connections in the routes for testing
        yield app


@pytest.fixture(autouse=True)
def _reset_calendar_gate_sentinels():
    """`calendar_gate.py`'s module-level `_last_recorded_date` /
    `_last_logged_closed` sentinels are process-run state, not
    per-request state — reset before and after every test in this file
    so one test's `conn_should_attempt_connect()` /
    `record_daily_verdict_if_needed()` call can never leak into
    another's assertions."""
    import backend.brokers.service.calendar_gate as _cg
    _cg._last_recorded_date = None
    _cg._last_logged_closed = None
    yield
    _cg._last_recorded_date = None
    _cg._last_logged_closed = None


def test_health_endpoint_returns_ok(conn_app):
    """GET /health returns 200 with ok=True and service name."""
    with TestClient(app=conn_app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.json()
        assert data["ok"] is True, "health response must have ok=True"
        assert data["service"] == "ramboq_conn", "service name must be 'ramboq_conn'"
        assert "accounts_loaded" in data, "health response must include accounts_loaded"
        assert isinstance(data["accounts"], list), "accounts must be a list"


def test_health_endpoint_includes_ticker_snapshot(conn_app):
    """GET /health includes ticker snapshot when available."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.kite_ticker.get_ticker') as mock_ticker:
            ticker = MagicMock()
            ticker.status.return_value = {
                "started": True,
                "connected": True,
                "subscribed_count": 10,
                "stale_count": 0,
                "max_age_seconds": 5.0,
                "active_account": "ZG0790",
                "consecutive_unhealthy": 0,
                "swaps_last_hour": 0,
                "last_swap_at": 0.0,
            }
            mock_ticker.return_value = ticker

            with patch('backend.brokers.service.app._kite_failover_list', return_value=["ZG0790"]):
                resp = client.get("/health")
                assert resp.status_code == 200
                data = resp.json()
                assert data.get("ticker") is not None, "ticker snapshot must be present"
                assert data["ticker"]["started"] is True, "ticker should be marked started"


def test_accounts_endpoint_returns_loaded_accounts(conn_app):
    """GET /internal/accounts returns list of loaded accounts with broker info."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.connections.Connections') as mock_conns:
            mock_conn = MagicMock()
            mock_conn.conn = {
                "ZG0790": MagicMock(__class__=MagicMock(__name__="KiteConnection")),
                "DH6847": MagicMock(__class__=MagicMock(__name__="DhanConnection")),
            }
            mock_conn._broker_id_map = {
                "ZG0790": "zerodha_kite",
                "DH6847": "dhan",
            }
            mock_conn._client_id_map = {"DH6847": "1100012345"}
            mock_conns.return_value = mock_conn

            resp = client.get("/internal/accounts")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            data = resp.json()
            assert "accounts" in data, "response must include 'accounts' key"
            accounts = data["accounts"]
            assert len(accounts) >= 2, "should have at least 2 accounts"

            # Check account structure
            for acc in accounts:
                assert "account" in acc, "each account must have 'account' key"
                assert "broker_id" in acc, "each account must have 'broker_id' key"

            # 2026-10-24: client_id is threaded through for accounts that
            # have one, and absent (None) for those that don't.
            by_account = {acc["account"]: acc for acc in accounts}
            assert by_account["DH6847"].get("client_id") == "1100012345", (
                f"got {by_account['DH6847'].get('client_id')!r}"
            )
            assert by_account["ZG0790"].get("client_id") is None


def test_rebuild_endpoint_rebuilds_connections(conn_app):
    """POST /internal/rebuild calls Connections.rebuild_from_db()."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.connections.Connections') as mock_conns_cls:
            mock_conn = MagicMock()
            mock_conn.rebuild_from_db = AsyncMock(return_value=None)
            mock_conn.conn = {"ZG0790": MagicMock(), "DH6847": MagicMock()}
            mock_conns_cls.return_value = mock_conn

            resp = client.post("/internal/rebuild")
            assert resp.status_code in (200, 201), f"Expected 200/201, got {resp.status_code}"
            data = resp.json()
            assert "ok" in data, "response must include 'ok' key"


def test_holdings_endpoint_aggregates_multi_broker(conn_app):
    """GET /internal/holdings returns per-account holdings envelopes."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.broker_apis.fetch_holdings') as mock_fetch:
            df1 = pd.DataFrame([{"account": "ZG0790", "tradingsymbol": "RELIANCE", "quantity": 10}])
            df2 = pd.DataFrame([{"account": "DH6847", "tradingsymbol": "TCS", "quantity": 5}])
            mock_fetch.return_value = [df1, df2]

            resp = client.get("/internal/holdings")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            data = resp.json()
            assert "accounts" in data, "response must include 'accounts'"
            assert len(data["accounts"]) == 2, "should have 2 account envelopes"
            assert data["accounts"][0]["account"] == "ZG0790"
            assert len(data["accounts"][0]["rows"]) > 0, "account should have holdings rows"


def test_positions_endpoint_aggregates_multi_broker(conn_app):
    """GET /internal/positions returns per-account positions envelopes."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.broker_apis.fetch_positions') as mock_fetch:
            df1 = pd.DataFrame([{"account": "ZG0790", "symbol": "RELIANCE", "quantity": 1, "close_price": 2800}])
            mock_fetch.return_value = [df1]

            resp = client.get("/internal/positions")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            data = resp.json()
            assert "accounts" in data, "response must include 'accounts'"
            assert len(data["accounts"]) >= 1


def test_positions_endpoint_force_query_param_forwards_force_refresh(conn_app):
    """2026-09-30 fix: GET /internal/positions?force=1 must call
    broker_apis.fetch_positions(force_refresh=True) inside conn_service —
    this is what lets an operator-explicit ?fresh=1 on the main API side
    bypass conn_service's OWN independent _POSITIONS_SSOT_TTL cache, not
    just the API process's own cache layer."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.broker_apis.fetch_positions') as mock_fetch:
            df1 = pd.DataFrame([{"account": "ZG0790", "symbol": "RELIANCE", "quantity": 1}])
            mock_fetch.return_value = [df1]

            resp = client.get("/internal/positions?force=1")

            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            mock_fetch.assert_called_once_with(force_refresh=True)


def test_positions_endpoint_default_does_not_force_refresh(conn_app):
    """Default GET /internal/positions (no query param) must call
    fetch_positions(force_refresh=False) — normal poll cadence is
    unaffected by this fix."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.broker_apis.fetch_positions') as mock_fetch:
            df1 = pd.DataFrame([{"account": "ZG0790", "symbol": "RELIANCE", "quantity": 1}])
            mock_fetch.return_value = [df1]

            resp = client.get("/internal/positions")

            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            mock_fetch.assert_called_once_with(force_refresh=False)


def test_margins_endpoint_aggregates_multi_broker(conn_app):
    """GET /internal/margins returns per-account margins envelopes."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.broker_apis.fetch_margins') as mock_fetch:
            df1 = pd.DataFrame([{"account": "ZG0790", "avail cash": 100000, "utilised": 50000}])
            mock_fetch.return_value = [df1]

            resp = client.get("/internal/margins")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            data = resp.json()
            assert "accounts" in data, "response must include 'accounts'"


def test_broker_health_endpoint_returns_snapshot(conn_app):
    """GET /internal/health/brokers returns per-account fetch-health."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.broker_apis.fetch_health_snapshot') as mock_health:
            mock_health.return_value = {
                "ZG0790": {"last_ok": 1234567890, "last_fail": 0},
                "DH6847": {"last_ok": 1234567880, "last_fail": 0},
            }

            resp = client.get("/internal/health/brokers")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            data = resp.json()
            assert "health" in data, "response must include 'health' key"
            health = data["health"]
            assert isinstance(health, dict), "health should be a dict"


def test_broker_call_dispatch_allowed_method(conn_app):
    """POST /internal/broker/{account}/call/{method} dispatches to broker."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.registry.get_broker') as mock_get_broker:
            mock_broker = MagicMock()
            mock_broker.holdings.return_value = [{"tradingsymbol": "RELIANCE"}]
            mock_get_broker.return_value = mock_broker

            resp = client.post(
                "/internal/broker/ZG0790/call/holdings",
                json={"args": [], "kwargs": {}},
            )
            assert resp.status_code in (200, 201), f"Expected 200/201, got {resp.status_code}: {resp.text}"
            data = resp.json()
            assert data.get("ok") is True, "dispatch should succeed"
            assert data.get("result") is not None, "should return broker result"


def test_broker_call_dispatch_allowed_method_validate_gtt_exchange(conn_app):
    """validate_gtt_exchange must be in _ALLOWED_BROKER_METHODS — it's
    the GTT-exchange refusal gate RemoteBroker forwards from
    template_attach.apply_plan_live. Without this entry, the route's
    whitelist check 403s before the dispatch ever reaches the real
    adapter, defeating RemoteBroker's own forwarding override."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.registry.get_broker') as mock_get_broker:
            mock_broker = MagicMock()
            mock_broker.validate_gtt_exchange.return_value = None
            mock_get_broker.return_value = mock_broker

            resp = client.post(
                "/internal/broker/GR1234/call/validate_gtt_exchange",
                json={"args": ["MCX"], "kwargs": {}},
            )
            assert resp.status_code in (200, 201), f"Expected 200/201, got {resp.status_code}: {resp.text}"
            data = resp.json()
            assert data.get("ok") is True
            mock_broker.validate_gtt_exchange.assert_called_once_with("MCX")


def test_broker_call_dispatch_disallowed_method_returns_403(conn_app):
    """POST /internal/broker/{account}/call/{method} returns 403 for disallowed methods."""
    with TestClient(app=conn_app) as client:
        # Try to call a method not in _ALLOWED_BROKER_METHODS
        resp = client.post(
            "/internal/broker/ZG0790/call/internal_secret_method",
            json={"args": [], "kwargs": {}},
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"


def test_broker_call_unknown_account_returns_404(conn_app):
    """POST /internal/broker/{account}/call/{method} returns 404 for unknown account."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.registry.get_broker') as mock_get_broker:
            mock_get_broker.side_effect = KeyError("Unknown account")

            resp = client.post(
                "/internal/broker/UNKNOWN/call/holdings",
                json={"args": [], "kwargs": {}},
            )
            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"


def test_verify_postback_valid_signature(conn_app):
    """POST /internal/broker/{account}/verify_postback validates Kite HMAC."""
    import hashlib

    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.connections.Connections') as mock_conns_cls:
            mock_conn = MagicMock()
            kite_conn = MagicMock()
            kite_conn.api_secret = "test_secret"
            mock_conn.conn = {"ZG0790": kite_conn}
            mock_conns_cls.return_value = mock_conn

            order_id = "123456"
            order_ts = "1234567890"
            api_secret = "test_secret"
            msg = (order_id + order_ts + api_secret).encode()
            checksum = hashlib.sha256(msg).hexdigest()

            resp = client.post(
                "/internal/broker/ZG0790/verify_postback",
                json={
                    "order_id": order_id,
                    "order_timestamp": order_ts,
                    "checksum": checksum,
                },
            )
            assert resp.status_code in (200, 201), f"Expected 200/201, got {resp.status_code}"
            data = resp.json()
            assert data["ok"] is True, "postback signature should be valid"


def test_verify_postback_invalid_signature(conn_app):
    """POST /internal/broker/{account}/verify_postback rejects bad checksum."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.connections.Connections') as mock_conns_cls:
            mock_conn = MagicMock()
            kite_conn = MagicMock()
            kite_conn.api_secret = "test_secret"
            mock_conn.conn = {"ZG0790": kite_conn}
            mock_conns_cls.return_value = mock_conn

            resp = client.post(
                "/internal/broker/ZG0790/verify_postback",
                json={
                    "order_id": "123456",
                    "order_timestamp": "1234567890",
                    "checksum": "wrong_checksum_12345",
                },
            )
            assert resp.status_code in (200, 201), f"Expected 200/201, got {resp.status_code}"
            data = resp.json()
            assert data["ok"] is False, "bad checksum should fail verification"


def test_verify_postback_non_kite_account_returns_false(conn_app):
    """POST /internal/broker/{account}/verify_postback returns ok=False for non-Kite accounts."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.connections.Connections') as mock_conns_cls:
            mock_conn = MagicMock()
            dhan_conn = MagicMock()
            dhan_conn.api_secret = None  # Dhan doesn't have api_secret
            mock_conn.conn = {"DH6847": dhan_conn}
            mock_conns_cls.return_value = mock_conn

            resp = client.post(
                "/internal/broker/DH6847/verify_postback",
                json={
                    "order_id": "123456",
                    "order_timestamp": "1234567890",
                    "checksum": "anything",
                },
            )
            assert resp.status_code in (200, 201)
            data = resp.json()
            assert data["ok"] is False, "non-Kite account should return ok=False"


def test_ticker_subscribe_endpoint(conn_app):
    """POST /internal/ticker/subscribe pushes token-symbol pairs to KiteTicker."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.kite_ticker.get_ticker') as mock_ticker:
            ticker = MagicMock()
            ticker.subscribe_with_sym = MagicMock()
            ticker.status.return_value = {"subscribed_count": 5}
            mock_ticker.return_value = ticker

            resp = client.post(
                "/internal/ticker/subscribe",
                json={"pairs": [[408065, "RELIANCE"], [408067, "TCS"]]},
            )
            assert resp.status_code in (200, 201), f"Expected 200/201, got {resp.status_code}"
            data = resp.json()
            assert data["ok"] is True, "subscribe should succeed"
            assert data["subscribed"] == 2, "should report subscribed count"


def test_ticker_subscribe_invalid_pairs_ignored(conn_app):
    """POST /internal/ticker/subscribe silently skips invalid pairs."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.kite_ticker.get_ticker') as mock_ticker:
            ticker = MagicMock()
            ticker.subscribe_with_sym = MagicMock()
            ticker.status.return_value = {"subscribed_count": 1}
            mock_ticker.return_value = ticker

            # Include one valid pair and several invalid ones
            resp = client.post(
                "/internal/ticker/subscribe",
                json={"pairs": [[408065, "RELIANCE"], [None, "BAD"], ["invalid", "WORSE"]]},
            )
            assert resp.status_code in (200, 201)
            data = resp.json()
            # Should skip the invalid entries and only process valid ones
            assert "ok" in data


def test_ticker_status_endpoint(conn_app):
    """GET /internal/ticker/status returns KiteTicker health snapshot."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.kite_ticker.get_ticker') as mock_ticker:
            ticker = MagicMock()
            ticker.status.return_value = {
                "started": True,
                "connected": True,
                "subscribed_count": 10,
                "stale_count": 0,
                "max_age_seconds": 5.0,
                "active_account": "ZG0790",
                "consecutive_unhealthy": 0,
            }
            mock_ticker.return_value = ticker

            with patch('backend.brokers.service.app._kite_failover_list', return_value=["ZG0790"]):
                resp = client.get("/internal/ticker/status")
                assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
                data = resp.json()
                assert data["ok"] is True, "status endpoint should succeed"
                assert "status" in data, "should include status snapshot"
                status = data["status"]
                assert status["started"] is True, "ticker should be marked started"


def test_dhan_poll_reset_all_accounts(conn_app):
    """POST /internal/dhan/poll_reset clears Dhan poll gates."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.broker_apis.dhan_next_poll_clear') as mock_clear:
            mock_clear.return_value = None

            resp = client.post(
                "/internal/dhan/poll_reset",
                json={},
            )
            assert resp.status_code in (200, 201), f"Expected 200/201, got {resp.status_code}"
            data = resp.json()
            assert data["ok"] is True, "poll_reset should succeed"
            assert data["cleared"] == "all", "should report cleared=all"


def test_dhan_poll_reset_specific_accounts(conn_app):
    """POST /internal/dhan/poll_reset clears specific account gates."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.broker_apis.dhan_next_poll_clear') as mock_clear:
            mock_clear.return_value = None

            resp = client.post(
                "/internal/dhan/poll_reset",
                json={"accounts": ["DH6847", "DH3747"]},
            )
            assert resp.status_code in (200, 201), f"Expected 200/201, got {resp.status_code}"
            data = resp.json()
            assert data["ok"] is True, "poll_reset should succeed"
            assert "DH" in data["cleared"], "should report cleared accounts"


# ─── Additional tests for uncovered service/app.py sections ───


def test_kite_failover_list_filters_non_kite_accounts():
    """_kite_failover_list excludes non-Kite brokers (Dhan, Groww)."""
    from backend.brokers.service.app import _kite_failover_list

    with patch('backend.brokers.connections.Connections') as mock_conns_cls:
        mock_conn = MagicMock()
        kite_acct = MagicMock()
        kite_acct.get_access_token = MagicMock()
        kite_acct.api_key = "pk_123"

        dhan_acct = MagicMock()
        dhan_acct.get_access_token = None

        mock_conn.conn = {
            "ZG0790": kite_acct,
            "DH6847": dhan_acct,
        }
        mock_conn._priority_map = {"ZG0790": 1, "DH6847": 2}
        mock_conns_cls.return_value = mock_conn

        with patch('backend.brokers.registry._broker_id_for') as mock_broker_id:
            def broker_id_side_effect(acct):
                return "zerodha_kite" if acct == "ZG0790" else "dhan"
            mock_broker_id.side_effect = broker_id_side_effect

            result = _kite_failover_list()
            assert "ZG0790" in result, "should include Kite account"
            assert "DH6847" not in result, "should exclude Dhan account"


def test_kite_failover_list_respects_exclude_set():
    """_kite_failover_list excludes accounts in the exclude set."""
    from backend.brokers.service.app import _kite_failover_list

    with patch('backend.brokers.connections.Connections') as mock_conns_cls:
        mock_conn = MagicMock()
        kite1 = MagicMock()
        kite1.get_access_token = MagicMock()
        kite1.api_key = "pk_123"

        kite2 = MagicMock()
        kite2.get_access_token = MagicMock()
        kite2.api_key = "pk_456"

        mock_conn.conn = {
            "ZG0790": kite1,
            "ZG0791": kite2,
        }
        mock_conn._priority_map = {"ZG0790": 1, "ZG0791": 2}
        mock_conns_cls.return_value = mock_conn

        with patch('backend.brokers.registry._broker_id_for', return_value="zerodha_kite"):
            result = _kite_failover_list(exclude={"ZG0790"})
            assert "ZG0790" not in result, "should exclude ZG0790"
            assert "ZG0791" in result, "should include ZG0791"


def test_kite_failover_list_sorts_by_priority():
    """_kite_failover_list sorts accounts by priority ASC."""
    from backend.brokers.service.app import _kite_failover_list

    with patch('backend.brokers.connections.Connections') as mock_conns_cls:
        mock_conn = MagicMock()
        for acct in ["ZG0790", "ZG0791", "ZG0792"]:
            kite = MagicMock()
            kite.get_access_token = MagicMock()
            kite.api_key = "pk_123"
            mock_conn.conn = {**mock_conn.conn, acct: kite} if hasattr(mock_conn.conn, '__len__') else {acct: kite}

        mock_conn.conn = {
            "ZG0790": MagicMock(get_access_token=MagicMock(), api_key="pk_1"),
            "ZG0791": MagicMock(get_access_token=MagicMock(), api_key="pk_2"),
            "ZG0792": MagicMock(get_access_token=MagicMock(), api_key="pk_3"),
        }
        # Lower priority number = higher priority
        mock_conn._priority_map = {"ZG0792": 1, "ZG0790": 3, "ZG0791": 2}
        mock_conns_cls.return_value = mock_conn

        with patch('backend.brokers.registry._broker_id_for', return_value="zerodha_kite"):
            result = _kite_failover_list()
            # Should be sorted: ZG0792 (priority 1), ZG0791 (priority 2), ZG0790 (priority 3)
            assert result[0] == "ZG0792", f"first should be ZG0792, got {result[0]}"


def test_resolve_kite_creds_returns_none_on_missing_connection():
    """_resolve_kite_creds returns (None, None) when account not found."""
    from backend.brokers.service.app import _resolve_kite_creds

    with patch('backend.brokers.connections.Connections') as mock_conns_cls:
        mock_conn = MagicMock()
        mock_conn.conn = {}
        mock_conns_cls.return_value = mock_conn

        result = _resolve_kite_creds("UNKNOWN")
        assert result == (None, None), "should return (None, None) for unknown account"


def test_resolve_kite_creds_returns_none_on_missing_api_key():
    """_resolve_kite_creds returns (None, None) when api_key is None."""
    from backend.brokers.service.app import _resolve_kite_creds

    with patch('backend.brokers.connections.Connections') as mock_conns_cls:
        mock_conn = MagicMock()
        kite_conn = MagicMock()
        kite_conn.get_access_token = MagicMock()
        kite_conn.api_key = None
        mock_conn.conn = {"ZG0790": kite_conn}
        mock_conns_cls.return_value = mock_conn

        result = _resolve_kite_creds("ZG0790")
        assert result == (None, None), "should return (None, None) when api_key is None"


def test_resolve_kite_creds_returns_none_on_token_error():
    """_resolve_kite_creds returns (None, None) when get_access_token fails."""
    from backend.brokers.service.app import _resolve_kite_creds

    with patch('backend.brokers.connections.Connections') as mock_conns_cls:
        mock_conn = MagicMock()
        kite_conn = MagicMock()
        kite_conn.get_access_token = MagicMock(side_effect=Exception("Auth failed"))
        kite_conn.api_key = "pk_123"
        mock_conn.conn = {"ZG0790": kite_conn}
        mock_conns_cls.return_value = mock_conn

        result = _resolve_kite_creds("ZG0790")
        assert result == (None, None), "should return (None, None) on token fetch error"


def test_resolve_kite_creds_returns_none_when_token_is_empty():
    """_resolve_kite_creds returns (None, None) when token is falsy."""
    from backend.brokers.service.app import _resolve_kite_creds

    with patch('backend.brokers.connections.Connections') as mock_conns_cls:
        mock_conn = MagicMock()
        kite_conn = MagicMock()
        kite_conn.get_access_token = MagicMock(return_value=None)
        kite_conn.api_key = "pk_123"
        mock_conn.conn = {"ZG0790": kite_conn}
        mock_conns_cls.return_value = mock_conn

        result = _resolve_kite_creds("ZG0790")
        assert result == (None, None), "should return (None, None) when token is None"


def test_resolve_kite_creds_success():
    """_resolve_kite_creds returns (api_key, token) on success."""
    from backend.brokers.service.app import _resolve_kite_creds

    with patch('backend.brokers.connections.Connections') as mock_conns_cls:
        mock_conn = MagicMock()
        kite_conn = MagicMock()
        kite_conn.get_access_token = MagicMock(return_value="token123")
        kite_conn.api_key = "pk_123"
        mock_conn.conn = {"ZG0790": kite_conn}
        mock_conns_cls.return_value = mock_conn

        result = _resolve_kite_creds("ZG0790")
        assert result == ("pk_123", "token123"), "should return (api_key, token)"


def test_pick_kite_account_returns_first_live_account():
    """_pick_kite_account returns credentials of first live Kite account."""
    from backend.brokers.service.app import _pick_kite_account

    with patch('backend.brokers.service.app._kite_failover_list') as mock_list, \
         patch('backend.brokers.service.app._resolve_kite_creds') as mock_resolve:

        mock_list.return_value = ["ZG0790", "ZG0791"]

        def resolve_side_effect(acct):
            if acct == "ZG0790":
                return None, None
            else:
                return "pk_456", "token456"

        mock_resolve.side_effect = resolve_side_effect

        api_key, token, acct = _pick_kite_account()
        assert acct == "ZG0791", "should return second account when first has no token"
        assert api_key == "pk_456"
        assert token == "token456"


def test_pick_kite_account_returns_empty_when_no_live_accounts():
    """_pick_kite_account returns (None, None, '') when no accounts have tokens."""
    from backend.brokers.service.app import _pick_kite_account

    with patch('backend.brokers.service.app._kite_failover_list') as mock_list, \
         patch('backend.brokers.service.app._resolve_kite_creds') as mock_resolve:

        mock_list.return_value = ["ZG0790"]
        mock_resolve.return_value = (None, None)

        result = _pick_kite_account()
        assert result == (None, None, ""), "should return (None, None, '') when no live accounts"


def test_try_start_ticker_returns_true_if_already_started():
    """_try_start_ticker returns True if ticker is already running."""
    from backend.brokers.service.app import _try_start_ticker

    with patch('backend.brokers.kite_ticker.get_ticker') as mock_get_ticker:
        ticker = MagicMock()
        ticker.status.return_value = {"started": True}
        mock_get_ticker.return_value = ticker

        result = _try_start_ticker()
        assert result is True, "should return True when ticker already started"
        # Should not try to pick account or start
        ticker.start.assert_not_called()


def test_try_start_ticker_returns_false_when_no_token():
    """_try_start_ticker returns False when no live Kite account found."""
    from backend.brokers.service.app import _try_start_ticker

    with patch('backend.brokers.kite_ticker.get_ticker') as mock_get_ticker, \
         patch('backend.brokers.service.app._pick_kite_account') as mock_pick:

        ticker = MagicMock()
        ticker.status.return_value = {"started": False}
        ticker._tick_buffer = None
        mock_get_ticker.return_value = ticker

        mock_pick.return_value = (None, None, "")

        result = _try_start_ticker()
        assert result is False, "should return False when no token available"


def test_try_start_ticker_attaches_tick_buffer():
    """_try_start_ticker attaches TickBufferWriter if not present."""
    from backend.brokers.service.app import _try_start_ticker

    with patch('backend.brokers.kite_ticker.get_ticker') as mock_get_ticker, \
         patch('backend.brokers.tick_buffer.TickBufferWriter') as mock_buffer, \
         patch('backend.brokers.service.app._pick_kite_account') as mock_pick, \
         patch('asyncio.get_running_loop'):

        ticker = MagicMock()
        ticker.status.return_value = {"started": False}
        ticker._tick_buffer = None
        ticker.attach_tick_buffer = MagicMock()
        mock_get_ticker.return_value = ticker

        mock_pick.return_value = ("pk_123", "token123", "ZG0790")

        try:
            result = _try_start_ticker()
        except RuntimeError:
            # asyncio.get_running_loop() will fail outside async context
            pass

        # Should have called attach_tick_buffer
        ticker.attach_tick_buffer.assert_called()


def test_watchdog_threshold_respects_setting():
    """_watchdog_threshold reads from settings."""
    from backend.brokers.service.app import _watchdog_threshold

    with patch('backend.shared.helpers.settings.get_int') as mock_get_int:
        mock_get_int.return_value = 3
        result = _watchdog_threshold()
        assert result == 3, "should return setting value"
        mock_get_int.assert_called_with("kite_ticker.unhealthy_threshold", 2)


def test_watchdog_threshold_enforces_minimum():
    """_watchdog_threshold enforces minimum of 1."""
    from backend.brokers.service.app import _watchdog_threshold

    with patch('backend.shared.helpers.settings.get_int') as mock_get_int:
        mock_get_int.return_value = 0
        result = _watchdog_threshold()
        assert result == 1, "should enforce minimum of 1"


def test_watchdog_cooldown_enforces_minimum():
    """_watchdog_cooldown_s enforces minimum of 30 seconds."""
    from backend.brokers.service.app import _watchdog_cooldown_s

    with patch('backend.shared.helpers.settings.get_int') as mock_get_int:
        mock_get_int.return_value = 10
        result = _watchdog_cooldown_s()
        assert result == 30.0, "should enforce minimum of 30 seconds"


def test_watchdog_slowed_interval_enforces_minimum():
    """_watchdog_slowed_interval_s enforces minimum of 30 seconds."""
    from backend.brokers.service.app import _watchdog_slowed_interval_s

    with patch('backend.shared.helpers.settings.get_int') as mock_get_int:
        mock_get_int.return_value = 10
        result = _watchdog_slowed_interval_s()
        assert result == 30.0, "should enforce minimum of 30 seconds"


def test_attempt_failover_swap_suppressed_by_cooldown():
    """_attempt_failover_swap returns True when within cooldown window."""
    from backend.brokers.service.app import _attempt_failover_swap
    import logging

    ticker = MagicMock()
    ticker.swaps_since.return_value = 1  # swap happened recently
    ticker.last_swap_at.return_value = time.time() - 10  # 10s ago
    ticker.current_account.return_value = "ZG0790"

    log = logging.getLogger(__name__)

    result = _attempt_failover_swap(ticker, log, cooldown_s=300, slowed_s=60)
    assert result is True, "should return True when swap suppressed by cooldown"


def test_attempt_failover_swap_no_eligible_accounts():
    """_attempt_failover_swap returns False when all accounts exhausted."""
    from backend.brokers.service.app import _attempt_failover_swap
    import logging

    ticker = MagicMock()
    ticker.swaps_since.return_value = 0
    ticker.current_account.return_value = "ZG0790"
    ticker.is_reactor_dead.return_value = False

    log = logging.getLogger(__name__)

    with patch('backend.brokers.service.app._kite_failover_list') as mock_list:
        mock_list.return_value = []  # no eligible accounts

        result = _attempt_failover_swap(ticker, log, cooldown_s=300, slowed_s=60)
        assert result is False, "should return False when no eligible accounts"


@pytest.mark.asyncio
async def test_init_connections_on_startup_rebuilds_from_db():
    """_init_connections_on_startup calls Connections.rebuild_from_db()."""
    from backend.brokers.service.app import _init_connections_on_startup

    with patch('backend.brokers.connections.Connections') as mock_conns_cls:
        mock_conn = AsyncMock()
        mock_conn.conn = {"ZG0790": MagicMock()}
        mock_conns_cls.return_value = mock_conn

        app = MagicMock()
        await _init_connections_on_startup(app)

        mock_conn.rebuild_from_db.assert_called_once()


@pytest.mark.asyncio
async def test_init_connections_on_startup_handles_exception(conn_app):
    """_init_connections_on_startup logs exception but doesn't raise."""
    from backend.brokers.service.app import _init_connections_on_startup

    with patch('backend.brokers.connections.Connections') as mock_conns_cls:
        mock_conn = MagicMock()
        mock_conn.rebuild_from_db = AsyncMock(side_effect=Exception("DB error"))
        mock_conns_cls.return_value = mock_conn

        app = MagicMock()
        # Should not raise
        await _init_connections_on_startup(app)


@pytest.mark.asyncio
async def test_start_conn_event_queue(conn_app):
    """_start_conn_event_queue calls broker_conn_event_queue.start()."""
    from backend.brokers.service.app import _start_conn_event_queue

    with patch('backend.brokers.service.conn_events.broker_conn_event_queue') as mock_queue:
        mock_queue.start = AsyncMock()
        app = MagicMock()

        await _start_conn_event_queue(app)
        mock_queue.start.assert_called_once()


@pytest.mark.asyncio
async def test_stop_conn_event_queue(conn_app):
    """_stop_conn_event_queue calls broker_conn_event_queue.stop()."""
    from backend.brokers.service.app import _stop_conn_event_queue

    with patch('backend.brokers.service.conn_events.broker_conn_event_queue') as mock_queue:
        mock_queue.stop = AsyncMock()
        app = MagicMock()

        await _stop_conn_event_queue(app)
        mock_queue.stop.assert_called_once()


# ============================================================================
# Additional coverage for error paths and edge cases
# ============================================================================






def test_account_list_shows_broker_types(conn_app):
    """GET /internal/accounts includes broker_id for each account."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.connections.Connections') as mock_conns:
            kite_conn = MagicMock()
            kite_conn.__class__.__name__ = "KiteConnection"
            dhan_conn = MagicMock()
            dhan_conn.__class__.__name__ = "DhanConnection"
            groww_conn = MagicMock()
            groww_conn.__class__.__name__ = "GrowwConnection"

            mock_conns_instance = MagicMock()
            mock_conns_instance.conn = {
                "ZG0790": kite_conn,
                "DH6847": dhan_conn,
                "GRW123": groww_conn,
            }
            mock_conns_instance._broker_id_map = {
                "ZG0790": "zerodha_kite",
                "DH6847": "dhan",
                "GRW123": "groww",
            }
            mock_conns_instance._client_id_map = {}
            mock_conns.return_value = mock_conns_instance

            resp = client.get("/internal/accounts")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["accounts"]) == 3
            # Verify broker types are included
            broker_ids = [acc.get("broker_id") for acc in data["accounts"]]
            assert "zerodha_kite" in broker_ids
            assert "dhan" in broker_ids
            assert "groww" in broker_ids


# ============================================================================
# Error path tests for uncovered lines
# ============================================================================


def test_rebuild_endpoint_handles_exception(conn_app):
    """POST /internal/rebuild returns error dict on exception."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.connections.Connections') as mock_conns_cls:
            mock_conn = MagicMock()
            mock_conn.rebuild_from_db = AsyncMock(side_effect=Exception("DB connection failed"))
            mock_conns_cls.return_value = mock_conn

            resp = client.post("/internal/rebuild")
            assert resp.status_code in (200, 201)
            data = resp.json()
            assert data.get("ok") is False, "Should return ok=False on exception"
            assert "error" in data, "Should include error message"
            assert "connection failed" in data["error"].lower() or "DB" in data["error"]


def test_holdings_endpoint_handles_exception(conn_app):
    """GET /internal/holdings returns error envelope on broker failure."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.broker_apis.fetch_holdings') as mock_fetch:
            mock_fetch.side_effect = Exception("Broker API timeout")

            resp = client.get("/internal/holdings")
            assert resp.status_code == 200
            data = resp.json()
            assert "errors" in data, "Should include errors array"
            assert len(data["errors"]) > 0, "Should have at least one error"


def test_positions_endpoint_handles_exception(conn_app):
    """GET /internal/positions returns error envelope on broker failure."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.broker_apis.fetch_positions') as mock_fetch:
            mock_fetch.side_effect = Exception("Network unreachable")

            resp = client.get("/internal/positions")
            assert resp.status_code == 200
            data = resp.json()
            assert "errors" in data
            assert len(data["errors"]) > 0


def test_margins_endpoint_handles_exception(conn_app):
    """GET /internal/margins returns error envelope on broker failure."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.broker_apis.fetch_margins') as mock_fetch:
            mock_fetch.side_effect = Exception("Invalid credentials")

            resp = client.get("/internal/margins")
            assert resp.status_code == 200
            data = resp.json()
            assert "errors" in data
            assert len(data["errors"]) > 0


def test_broker_call_dispatch_method_not_callable(conn_app):
    """POST /internal/broker/{account}/call/{method} returns 404 when method not callable."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.registry.get_broker') as mock_get_broker:
            mock_broker = MagicMock(spec=[])  # Empty spec, no methods
            mock_broker.holdings = None  # Explicitly not callable
            mock_get_broker.return_value = mock_broker

            resp = client.post(
                "/internal/broker/ZG0790/call/holdings",
                json={"args": [], "kwargs": {}},
            )
            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"


def test_broker_call_dispatch_method_raises(conn_app):
    """POST /internal/broker/{account}/call/{method} returns error dict on method exception."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.registry.get_broker') as mock_get_broker:
            mock_broker = MagicMock()
            mock_broker.holdings.side_effect = Exception("API rate limited")
            mock_get_broker.return_value = mock_broker

            resp = client.post(
                "/internal/broker/ZG0790/call/holdings",
                json={"args": [], "kwargs": {}},
            )
            assert resp.status_code in (200, 201)
            data = resp.json()
            assert data.get("ok") is False, "Should return ok=False"
            assert "error" in data, "Should include error message"


def test_verify_postback_unknown_account(conn_app):
    """POST /internal/broker/{account}/verify_postback returns 404 for unknown account."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.connections.Connections') as mock_conns_cls:
            mock_conn = MagicMock()
            mock_conn.conn = {}
            mock_conns_cls.return_value = mock_conn

            resp = client.post(
                "/internal/broker/UNKNOWN_ACCOUNT/verify_postback",
                json={
                    "order_id": "123",
                    "order_timestamp": "1234567890",
                    "checksum": "abc123",
                },
            )
            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"


def test_access_token_unknown_account(conn_app):
    """GET /internal/broker/{account}/access_token returns 404 for unknown account."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.connections.Connections') as mock_conns_cls:
            mock_conn = MagicMock()
            mock_conn.conn = {}
            mock_conns_cls.return_value = mock_conn

            resp = client.get("/internal/broker/UNKNOWN/access_token")
            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"


def test_access_token_non_kite_account(conn_app):
    """GET /internal/broker/{account}/access_token returns 404 for non-Kite account."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.connections.Connections') as mock_conns_cls:
            mock_conn = MagicMock()
            dhan_conn = MagicMock()
            dhan_conn.api_key = None  # Dhan doesn't have api_key
            mock_conn.conn = {"DH6847": dhan_conn}
            mock_conns_cls.return_value = mock_conn

            resp = client.get("/internal/broker/DH6847/access_token")
            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"


def test_access_token_fetch_failure_returns_none_token(conn_app):
    """GET /internal/broker/{account}/access_token returns None token on fetch error."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.connections.Connections') as mock_conns_cls:
            mock_conn = MagicMock()
            kite_conn = MagicMock()
            kite_conn.api_key = "pk_123"
            kite_conn.get_access_token = MagicMock(side_effect=Exception("Auth failed"))
            mock_conn.conn = {"ZG0790": kite_conn}
            mock_conns_cls.return_value = mock_conn

            resp = client.get("/internal/broker/ZG0790/access_token")
            assert resp.status_code in (200, 201)
            data = resp.json()
            assert data.get("api_key") == "pk_123", "Should include api_key"
            assert data.get("access_token") is None, "Should return None token on error"


def test_ticker_subscribe_no_pairs(conn_app):
    """POST /internal/ticker/subscribe with no pairs returns ok=True, subscribed=0."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.kite_ticker.get_ticker') as mock_ticker:
            ticker = MagicMock()
            mock_ticker.return_value = ticker

            resp = client.post(
                "/internal/ticker/subscribe",
                json={"pairs": []},
            )
            assert resp.status_code in (200, 201)
            data = resp.json()
            assert data["ok"] is True
            assert data["subscribed"] == 0


def test_ticker_subscribe_exception(conn_app):
    """POST /internal/ticker/subscribe returns error on exception."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.kite_ticker.get_ticker') as mock_ticker:
            ticker = MagicMock()
            ticker.subscribe_with_sym.side_effect = Exception("Subscription failed")
            mock_ticker.return_value = ticker

            resp = client.post(
                "/internal/ticker/subscribe",
                json={"pairs": [[408065, "RELIANCE"]]},
            )
            assert resp.status_code in (200, 201)
            data = resp.json()
            assert data.get("ok") is False, "Should return ok=False"
            assert "error" in data


def test_ticker_force_unhealthy_exception(conn_app):
    """POST /internal/ticker/force-unhealthy returns error on exception."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.kite_ticker.get_ticker') as mock_ticker:
            ticker = MagicMock()
            ticker.force_unhealthy.side_effect = Exception("Force failed")
            mock_ticker.return_value = ticker

            resp = client.post(
                "/internal/ticker/force-unhealthy",
                json={"duration_s": 120},
            )
            assert resp.status_code in (200, 201)
            data = resp.json()
            assert data.get("ok") is False, "Should return ok=False"
            assert "error" in data


def test_ticker_status_with_failover_exception(conn_app):
    """GET /internal/ticker/status handles failover_list computation exception."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.kite_ticker.get_ticker') as mock_ticker:
            ticker = MagicMock()
            ticker.status.return_value = {
                "started": True,
                "connected": True,
                "subscribed_count": 5,
            }
            mock_ticker.return_value = ticker

            with patch('backend.brokers.service.app._kite_failover_list', side_effect=Exception("Failover failed")):
                resp = client.get("/internal/ticker/status")
                assert resp.status_code == 200
                data = resp.json()
                assert data["ok"] is True
                # failover_list should be empty due to exception handling
                assert data["status"]["failover_list"] == []


def test_ticker_status_exception(conn_app):
    """GET /internal/ticker/status returns error dict on exception."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.kite_ticker.get_ticker') as mock_ticker:
            ticker = MagicMock()
            ticker.status.side_effect = Exception("Ticker unavailable")
            mock_ticker.return_value = ticker

            resp = client.get("/internal/ticker/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("ok") is False, "Should return ok=False"
            assert "error" in data


def test_dhan_poll_reset_exception(conn_app):
    """POST /internal/dhan/poll_reset returns error on exception."""
    with TestClient(app=conn_app) as client:
        with patch('backend.brokers.broker_apis.dhan_next_poll_clear') as mock_clear:
            mock_clear.side_effect = Exception("Reset failed")

            resp = client.post(
                "/internal/dhan/poll_reset",
                json={"accounts": ["DH6847"]},
            )
            assert resp.status_code in (200, 201)
            data = resp.json()
            assert data.get("ok") is False, "Should return ok=False"
            assert "error" in data


# ─────────────────────────────────────────────────────────────────────────
# Calendar-gated connect attempt (2026-10-24) —
# backend/brokers/service/calendar_gate.py + its wiring into
# _start_kite_ticker / _ticker_watchdog's Phase 1.
# ─────────────────────────────────────────────────────────────────────────

_CG_SEGMENTS = {
    "equity":    {"holiday_exchange": "NSE", "evening_open_on_holidays": False},
    "commodity": {"holiday_exchange": "MCX", "evening_open_on_holidays": True},
}


# --- _conn_calendar_verdict_detail / conn_calendar_verdict ----------------

@pytest.mark.asyncio
async def test_conn_calendar_verdict_open_on_weekday_no_holiday():
    """A normal weekday with no holiday resolves OPEN without ever
    consulting special sessions (short-circuits)."""
    from backend.brokers.service.calendar_gate import conn_calendar_verdict, CAL_OPEN

    monday = date(2026, 10, 19)  # confirmed Monday
    with patch('backend.brokers.service.calendar_gate._segments_config', return_value=_CG_SEGMENTS), \
         patch('backend.brokers.service.calendar_gate._conn_extra_trading_days',
               new_callable=AsyncMock, return_value=set()), \
         patch('backend.brokers.broker_apis._read_market_holidays_async',
               new_callable=AsyncMock, return_value=set()), \
         patch(
             'backend.brokers.broker_apis._read_special_sessions_async',
             new_callable=AsyncMock,
             side_effect=AssertionError("must not be called — calendar already says open"),
         ):
        verdict = await conn_calendar_verdict(monday)
    assert verdict == CAL_OPEN


@pytest.mark.asyncio
async def test_conn_calendar_verdict_closed_on_weekend_no_special_session():
    from backend.brokers.service.calendar_gate import conn_calendar_verdict, CAL_CLOSED

    saturday = date(2026, 10, 17)  # confirmed Saturday
    with patch('backend.brokers.service.calendar_gate._segments_config', return_value=_CG_SEGMENTS), \
         patch('backend.brokers.service.calendar_gate._conn_extra_trading_days',
               new_callable=AsyncMock, return_value=set()), \
         patch('backend.brokers.broker_apis._read_market_holidays_async',
               new_callable=AsyncMock, return_value=set()), \
         patch('backend.brokers.broker_apis._read_special_sessions_async',
               new_callable=AsyncMock, return_value=[]):
        verdict = await conn_calendar_verdict(saturday)
    assert verdict == CAL_CLOSED


@pytest.mark.asyncio
async def test_conn_calendar_verdict_non_zero_width_special_session_opens():
    """A late-announced, non-zero-width market_special_sessions row for
    today opens that segment even though the base calendar says
    closed — the mechanism the closed-day recheck loop relies on."""
    from backend.brokers.service.calendar_gate import conn_calendar_verdict, CAL_OPEN

    a_holiday = date(2026, 10, 20)  # confirmed Tuesday

    async def _special(exch, today):
        if exch == "NSE":
            return [{"date": today, "start": dtime(18, 0), "end": dtime(19, 0)}]
        return []

    with patch('backend.brokers.service.calendar_gate._segments_config', return_value=_CG_SEGMENTS), \
         patch('backend.brokers.service.calendar_gate._conn_extra_trading_days',
               new_callable=AsyncMock, return_value=set()), \
         patch('backend.brokers.broker_apis._read_market_holidays_async',
               new_callable=AsyncMock, return_value={a_holiday}), \
         patch('backend.brokers.broker_apis._read_special_sessions_async', side_effect=_special):
        verdict = await conn_calendar_verdict(a_holiday)
    assert verdict == CAL_OPEN


@pytest.mark.asyncio
async def test_conn_calendar_verdict_zero_width_special_session_blocks_evening_open():
    """A zero-width override row (the auto-inserted 'MCX evening closed'
    marker) must NOT be read as an open signal and must block the
    evening_open_on_holidays default too."""
    from backend.brokers.service.calendar_gate import conn_calendar_verdict, CAL_CLOSED

    a_holiday = date(2026, 10, 20)  # confirmed Tuesday — a weekday, so the
    # MCX evening-open default would otherwise fire if not blocked.

    async def _special(exch, today):
        if exch == "MCX":
            return [{"date": today, "start": dtime(0, 0), "end": dtime(0, 0)}]
        return []

    with patch('backend.brokers.service.calendar_gate._segments_config', return_value=_CG_SEGMENTS), \
         patch('backend.brokers.service.calendar_gate._conn_extra_trading_days',
               new_callable=AsyncMock, return_value=set()), \
         patch('backend.brokers.broker_apis._read_market_holidays_async',
               new_callable=AsyncMock, return_value={a_holiday}), \
         patch('backend.brokers.broker_apis._read_special_sessions_async', side_effect=_special):
        verdict = await conn_calendar_verdict(a_holiday)
    assert verdict == CAL_CLOSED


@pytest.mark.asyncio
async def test_conn_calendar_verdict_holiday_read_failure_is_unknown():
    """A DB error reading market_holidays resolves UNKNOWN, not CLOSED —
    fail-safe-to-connect direction."""
    from backend.brokers.service.calendar_gate import conn_calendar_verdict, CAL_UNKNOWN

    saturday = date(2026, 10, 17)
    with patch('backend.brokers.service.calendar_gate._segments_config', return_value=_CG_SEGMENTS), \
         patch('backend.brokers.service.calendar_gate._conn_extra_trading_days',
               new_callable=AsyncMock, return_value=set()), \
         patch('backend.brokers.broker_apis._read_market_holidays_async',
               new_callable=AsyncMock, side_effect=Exception("DB down")), \
         patch('backend.brokers.broker_apis._read_special_sessions_async',
               new_callable=AsyncMock, return_value=[]):
        verdict = await conn_calendar_verdict(saturday)
    assert verdict == CAL_UNKNOWN


@pytest.mark.asyncio
async def test_conn_calendar_verdict_extra_trading_days_db_error_is_unknown():
    """A DB error reading the `market.extra_trading_days` operator
    setting resolves UNKNOWN (not an empty-set "no override"), even
    when every segment itself cleanly resolves closed."""
    from backend.brokers.service.calendar_gate import conn_calendar_verdict, CAL_UNKNOWN

    saturday = date(2026, 10, 17)
    with patch('backend.brokers.service.calendar_gate._segments_config', return_value=_CG_SEGMENTS), \
         patch('backend.brokers.service.calendar_gate._conn_extra_trading_days',
               new_callable=AsyncMock, return_value=None), \
         patch('backend.brokers.broker_apis._read_market_holidays_async',
               new_callable=AsyncMock, return_value=set()), \
         patch('backend.brokers.broker_apis._read_special_sessions_async',
               new_callable=AsyncMock, return_value=[]):
        verdict = await conn_calendar_verdict(saturday)
    assert verdict == CAL_UNKNOWN


@pytest.mark.asyncio
async def test_conn_calendar_verdict_detail_is_always_json_serialisable():
    """`_conn_calendar_verdict_detail`'s `detail` dict must round-trip
    through `json.dumps` in every branch — it is persisted as JSONB and
    must never carry a raw `datetime.time`/`date` object."""
    import json
    from backend.brokers.service.calendar_gate import _conn_calendar_verdict_detail

    saturday = date(2026, 10, 17)

    cases = [
        # (extra_trading_days, holidays, special_sessions)
        (set(), set(), []),
        (None, set(), []),  # extra-trading-days DB error
    ]
    for extra, holidays, special in cases:
        with patch('backend.brokers.service.calendar_gate._segments_config', return_value=_CG_SEGMENTS), \
             patch('backend.brokers.service.calendar_gate._conn_extra_trading_days',
                   new_callable=AsyncMock, return_value=extra), \
             patch('backend.brokers.broker_apis._read_market_holidays_async',
                   new_callable=AsyncMock, return_value=holidays), \
             patch('backend.brokers.broker_apis._read_special_sessions_async',
                   new_callable=AsyncMock, return_value=special):
            verdict, detail = await _conn_calendar_verdict_detail(saturday)
        json.dumps(detail)  # must not raise

    # Exception path also produces a JSON-serialisable detail.
    with patch('backend.brokers.service.calendar_gate._conn_extra_trading_days',
               new_callable=AsyncMock, side_effect=RuntimeError("boom")):
        verdict, detail = await _conn_calendar_verdict_detail(saturday)
    assert verdict == "unknown"
    json.dumps(detail)


# --- _conn_extra_trading_days (direct, unmocked implementation) ---------

@pytest.mark.asyncio
async def test_conn_extra_trading_days_parses_csv_value():
    from backend.brokers.service.calendar_gate import _conn_extra_trading_days

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = "2026-10-21, 2026-12-25"

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch('backend.api.database.async_session', MagicMock(return_value=mock_session)):
        result = await _conn_extra_trading_days()

    assert result == {date(2026, 10, 21), date(2026, 12, 25)}


@pytest.mark.asyncio
async def test_conn_extra_trading_days_returns_empty_set_when_row_absent():
    from backend.brokers.service.calendar_gate import _conn_extra_trading_days

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch('backend.api.database.async_session', MagicMock(return_value=mock_session)):
        result = await _conn_extra_trading_days()

    assert result == set()


@pytest.mark.asyncio
async def test_conn_extra_trading_days_returns_none_on_db_error():
    from backend.brokers.service.calendar_gate import _conn_extra_trading_days

    with patch('backend.api.database.async_session', side_effect=Exception("DB down")):
        result = await _conn_extra_trading_days()

    assert result is None


# --- _persist_verdict_row — single-row-per-day UPSERT --------------------

@pytest.mark.asyncio
async def test_persist_verdict_row_issues_single_row_upsert_on_date():
    """The UPSERT statement conflicts on `date` (the table's PK), so a
    second write for the same calendar day overwrites in place rather
    than inserting a second row."""
    from backend.brokers.service.calendar_gate import _persist_verdict_row

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch('backend.api.database.async_session', MagicMock(return_value=mock_session)):
        await _persist_verdict_row(
            date(2026, 10, 24), "closed", {"segments": {"equity": "closed"}},
        )

    mock_session.execute.assert_called_once()
    sql_arg, params_arg = mock_session.execute.call_args.args
    sql_text = str(sql_arg)
    assert "ticker_calendar_verdicts" in sql_text
    assert "ON CONFLICT (date) DO UPDATE" in sql_text
    assert params_arg["date"] == date(2026, 10, 24)
    assert params_arg["verdict"] == "closed"
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_persist_verdict_row_raises_on_db_error():
    """`_persist_verdict_row` does NOT swallow its own failure — the
    caller (`conn_should_attempt_connect`) is responsible for that, so
    it can prove the connect decision survives a persistence failure."""
    from backend.brokers.service.calendar_gate import _persist_verdict_row

    with patch('backend.api.database.async_session', side_effect=Exception("DB down")):
        with pytest.raises(Exception):
            await _persist_verdict_row(date(2026, 10, 24), "open", {})


# --- conn_should_attempt_connect ------------------------------------------

@pytest.mark.asyncio
async def test_conn_should_attempt_connect_true_when_open():
    from backend.brokers.service.calendar_gate import conn_should_attempt_connect, CAL_OPEN

    with patch('backend.brokers.service.calendar_gate._conn_calendar_verdict_detail',
               new_callable=AsyncMock, return_value=(CAL_OPEN, {"resolved_via": "equity"})), \
         patch('backend.brokers.service.calendar_gate._persist_verdict_row',
               new_callable=AsyncMock) as mock_persist:
        result = await conn_should_attempt_connect()

    assert result is True
    mock_persist.assert_called_once()
    assert mock_persist.call_args.args[1] == CAL_OPEN


@pytest.mark.asyncio
async def test_conn_should_attempt_connect_false_when_closed():
    from backend.brokers.service.calendar_gate import conn_should_attempt_connect, CAL_CLOSED

    with patch('backend.brokers.service.calendar_gate._conn_calendar_verdict_detail',
               new_callable=AsyncMock, return_value=(CAL_CLOSED, {"segments": {}})), \
         patch('backend.brokers.service.calendar_gate._persist_verdict_row',
               new_callable=AsyncMock) as mock_persist:
        result = await conn_should_attempt_connect()

    assert result is False
    mock_persist.assert_called_once()
    assert mock_persist.call_args.args[1] == CAL_CLOSED


@pytest.mark.asyncio
async def test_conn_should_attempt_connect_true_when_unknown():
    """A verdict of UNKNOWN (any lookup raised) still attempts to
    connect — fail-safe-to-connect direction, same as a real DB error
    reaching `_conn_calendar_verdict_detail` in the test above."""
    from backend.brokers.service.calendar_gate import conn_should_attempt_connect, CAL_UNKNOWN

    with patch('backend.brokers.service.calendar_gate._conn_calendar_verdict_detail',
               new_callable=AsyncMock, return_value=(CAL_UNKNOWN, {"error": "boom"})), \
         patch('backend.brokers.service.calendar_gate._persist_verdict_row',
               new_callable=AsyncMock):
        result = await conn_should_attempt_connect()

    assert result is True


@pytest.mark.asyncio
async def test_conn_should_attempt_connect_persist_failure_never_changes_decision():
    """A persistence failure (e.g. the table not existing yet on a
    fresh deploy) must never flip the already-decided connect/skip
    return value — tested for BOTH the CLOSED and OPEN verdicts."""
    from backend.brokers.service.calendar_gate import (
        conn_should_attempt_connect, CAL_OPEN, CAL_CLOSED,
    )

    for verdict, expected in [(CAL_CLOSED, False), (CAL_OPEN, True)]:
        with patch('backend.brokers.service.calendar_gate._conn_calendar_verdict_detail',
                   new_callable=AsyncMock, return_value=(verdict, {})), \
             patch('backend.brokers.service.calendar_gate._persist_verdict_row',
                   new_callable=AsyncMock, side_effect=Exception("table missing")):
            result = await conn_should_attempt_connect()
        assert result is expected


@pytest.mark.asyncio
async def test_conn_should_attempt_connect_fresh_day_computes_and_writes_one_row():
    """A fresh day with no existing row: the verdict is computed and a
    row is written for that date — `_persist_verdict_row` is called
    exactly once with today's date."""
    from backend.brokers.service.calendar_gate import conn_should_attempt_connect, CAL_OPEN
    from datetime import datetime as _dt

    with patch('backend.shared.helpers.date_time_utils.timestamp_indian') as mock_ts, \
         patch('backend.brokers.service.calendar_gate._conn_calendar_verdict_detail',
               new_callable=AsyncMock, return_value=(CAL_OPEN, {})), \
         patch('backend.brokers.service.calendar_gate._persist_verdict_row',
               new_callable=AsyncMock) as mock_persist:
        mock_ts.return_value = _dt(2026, 10, 24, 10, 0, 0)

        await conn_should_attempt_connect()

    mock_persist.assert_called_once()
    assert mock_persist.call_args.args[0] == date(2026, 10, 24)


@pytest.mark.asyncio
async def test_conn_should_attempt_connect_same_day_two_calls_recompute_and_upsert_same_key():
    """This module's design (documented in `calendar_gate.py`) is
    "always recompute, always upsert" — NOT "read existing row and
    skip recomputation" — because `market_special_sessions` carries no
    cheap staleness signal and a same-day special session must still
    be picked up. Two calls on the same day each recompute the verdict
    fresh and each upsert the SAME date key (never a second row)."""
    from backend.brokers.service.calendar_gate import (
        conn_should_attempt_connect, CAL_CLOSED, CAL_OPEN,
    )
    from datetime import datetime as _dt

    with patch('backend.shared.helpers.date_time_utils.timestamp_indian') as mock_ts, \
         patch('backend.brokers.service.calendar_gate._conn_calendar_verdict_detail',
               new_callable=AsyncMock, side_effect=[
                   (CAL_CLOSED, {"segments": {}}),
                   (CAL_OPEN, {"resolved_via": "commodity"}),
               ]) as mock_verdict, \
         patch('backend.brokers.service.calendar_gate._persist_verdict_row',
               new_callable=AsyncMock) as mock_persist:
        mock_ts.return_value = _dt(2026, 10, 24, 9, 0, 0)

        first = await conn_should_attempt_connect()
        second = await conn_should_attempt_connect()

    assert first is False
    assert second is True
    assert mock_verdict.call_count == 2, "each call must recompute fresh, never read a cached row"
    assert mock_persist.call_count == 2
    # Same date key both times — upsert, not a second row.
    assert mock_persist.call_args_list[0].args[0] == date(2026, 10, 24)
    assert mock_persist.call_args_list[1].args[0] == date(2026, 10, 24)
    assert mock_persist.call_args_list[0].args[1] == CAL_CLOSED
    assert mock_persist.call_args_list[1].args[1] == CAL_OPEN


# --- app.py wiring: _start_kite_ticker ------------------------------------

@pytest.mark.asyncio
async def test_start_kite_ticker_skips_connect_when_calendar_closed():
    """A confirmed-closed day: `_try_start_ticker` must never be
    called from `_start_kite_ticker`'s inline boot attempt."""
    from backend.brokers.service.app import _start_kite_ticker

    with patch('backend.brokers.service.calendar_gate.conn_should_attempt_connect',
               new_callable=AsyncMock, return_value=False), \
         patch('backend.brokers.service.app._try_start_ticker') as mock_try_start, \
         patch('backend.brokers.service.app._ticker_watchdog', new_callable=AsyncMock), \
         patch('backend.brokers.service.app._health_heartbeat', new_callable=AsyncMock), \
         patch('backend.brokers.service.app._task_prewarm_tokens', new_callable=AsyncMock):
        app = MagicMock()
        await _start_kite_ticker(app)

    mock_try_start.assert_not_called()


@pytest.mark.asyncio
async def test_start_kite_ticker_connects_when_calendar_open():
    """A confirmed-open (or uncertain) day: behaviour is unchanged from
    today — `_try_start_ticker` is called exactly once."""
    from backend.brokers.service.app import _start_kite_ticker

    with patch('backend.brokers.service.calendar_gate.conn_should_attempt_connect',
               new_callable=AsyncMock, return_value=True), \
         patch('backend.brokers.service.app._try_start_ticker', return_value=True) as mock_try_start, \
         patch('backend.brokers.service.app._ticker_watchdog', new_callable=AsyncMock), \
         patch('backend.brokers.service.app._health_heartbeat', new_callable=AsyncMock), \
         patch('backend.brokers.service.app._task_prewarm_tokens', new_callable=AsyncMock):
        app = MagicMock()
        await _start_kite_ticker(app)

    mock_try_start.assert_called_once()


# --- app.py wiring: _ticker_watchdog Phase 1 ------------------------------

def _make_not_started_ticker():
    ticker = MagicMock()
    ticker.status.return_value = {"started": False}
    ticker.is_reactor_dead.return_value = False
    ticker.mark_supervisor_started = MagicMock()
    return ticker


def _make_started_ticker():
    """A ticker already connected (no restart since boot) — used for
    the `record_daily_verdict_if_needed` audit-coverage tests below.
    `supervisor_uptime_seconds` is a real float (not a bare MagicMock)
    so Phase 2's `< BOOT_GRACE_S` comparison doesn't raise TypeError."""
    ticker = MagicMock()
    ticker.status.return_value = {"started": True}
    ticker.is_reactor_dead.return_value = False
    ticker.mark_supervisor_started = MagicMock()
    ticker.reset_unhealthy = MagicMock()
    ticker.supervisor_uptime_seconds.return_value = 1000.0
    ticker.current_account.return_value = "ZG0790"
    return ticker


@pytest.mark.asyncio
async def test_ticker_watchdog_phase1_never_calls_try_start_when_calendar_closed():
    """A confirmed-closed day: the watchdog's Phase 1 branch must never
    call `_try_start_ticker()` at all — not just tolerate a no-op
    result from it."""
    from backend.brokers.service.app import _ticker_watchdog

    ticker = _make_not_started_ticker()

    with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
         patch('backend.brokers.service.calendar_gate.conn_should_attempt_connect',
               new_callable=AsyncMock, return_value=False), \
         patch('backend.brokers.service.app._try_start_ticker') as mock_try_start, \
         patch('asyncio.sleep', new_callable=AsyncMock,
               side_effect=[None, asyncio.CancelledError()]):
        await _ticker_watchdog()

    mock_try_start.assert_not_called()


@pytest.mark.asyncio
async def test_ticker_watchdog_phase1_calls_try_start_when_calendar_open():
    """A confirmed-open day: behaviour is unchanged — `_try_start_ticker`
    is still called from Phase 1."""
    from backend.brokers.service.app import _ticker_watchdog

    ticker = _make_not_started_ticker()

    with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
         patch('backend.brokers.service.calendar_gate.conn_should_attempt_connect',
               new_callable=AsyncMock, return_value=True), \
         patch('backend.brokers.service.app._try_start_ticker', return_value=True) as mock_try_start, \
         patch('asyncio.sleep', new_callable=AsyncMock,
               side_effect=[None, asyncio.CancelledError()]):
        await _ticker_watchdog()

    mock_try_start.assert_called_once()


@pytest.mark.asyncio
async def test_ticker_watchdog_phase1_db_error_verdict_still_attempts_connect():
    """A GENUINE DB error reading market_holidays while computing the
    calendar verdict (not a mocked shortcut on the function under test)
    still results in a connect attempt — `conn_should_attempt_connect`
    is left UNMOCKED here so its real fail-safe-to-CAL_UNKNOWN path is
    actually exercised end to end through the watchdog."""
    from backend.brokers.service.app import _ticker_watchdog
    from backend.brokers.service.calendar_gate import CAL_UNKNOWN

    ticker = _make_not_started_ticker()
    saturday = date(2026, 10, 17)  # confirmed Saturday — so nothing BUT
    # the DB error pushes the verdict toward "connect"; a weekday would
    # already resolve OPEN via the calendar and prove nothing about the
    # error path.

    with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
         patch('backend.shared.helpers.date_time_utils.timestamp_indian',
               return_value=datetime(2026, 10, 17, 10, 0, 0)), \
         patch('backend.brokers.service.calendar_gate._segments_config',
               return_value=_CG_SEGMENTS), \
         patch('backend.brokers.service.calendar_gate._conn_extra_trading_days',
               new_callable=AsyncMock, return_value=set()), \
         patch('backend.brokers.broker_apis._read_market_holidays_async',
               new_callable=AsyncMock, side_effect=Exception("DB down")) as mock_holidays, \
         patch('backend.brokers.service.calendar_gate._persist_verdict_row',
               new_callable=AsyncMock) as mock_persist, \
         patch('backend.brokers.service.app._try_start_ticker', return_value=True) as mock_try_start, \
         patch('asyncio.sleep', new_callable=AsyncMock,
               side_effect=[None, asyncio.CancelledError()]):
        await _ticker_watchdog()

    mock_try_start.assert_called_once()
    # Proves the DB-error path genuinely ran, not a mocked shortcut:
    assert mock_holidays.await_count >= 1
    mock_persist.assert_called_once()
    assert mock_persist.call_args.args[0] == saturday
    assert mock_persist.call_args.args[1] == CAL_UNKNOWN


@pytest.mark.asyncio
async def test_ticker_watchdog_phase1_same_day_special_session_picked_up_next_tick():
    """A verdict that flips CLOSED → OPEN between watchdog ticks
    (simulating a same-day special session appearing) results in a
    connect attempt on the SECOND tick, having skipped the first."""
    from backend.brokers.service.app import _ticker_watchdog

    ticker = _make_not_started_ticker()

    with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
         patch('backend.brokers.service.calendar_gate.conn_should_attempt_connect',
               new_callable=AsyncMock, side_effect=[False, True]) as mock_gate, \
         patch('backend.brokers.service.app._try_start_ticker', return_value=True) as mock_try_start, \
         patch('asyncio.sleep', new_callable=AsyncMock,
               side_effect=[None, None, asyncio.CancelledError()]):
        await _ticker_watchdog()

    assert mock_gate.call_count == 2
    mock_try_start.assert_called_once()


# --- app.py wiring: _ticker_watchdog audit coverage for an ---------------
# already-connected ticker (record_daily_verdict_if_needed) --------------

@pytest.mark.asyncio
async def test_ticker_watchdog_started_records_once_per_day_across_ticks():
    """Ticker already connected (no restart since boot): two
    consecutive watchdog ticks on the SAME IST date must persist
    exactly one `ticker_calendar_verdicts` row, not one per tick, and
    Phase 1 must never fire (`_try_start_ticker` not called) since the
    ticker never stops."""
    from backend.brokers.service.app import _ticker_watchdog
    from backend.brokers.service.calendar_gate import CAL_OPEN

    ticker = _make_started_ticker()
    fixed_now = datetime(2026, 10, 24, 10, 0, 0)

    with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
         patch('backend.shared.helpers.date_time_utils.is_any_segment_open',
               return_value=False), \
         patch('backend.shared.helpers.date_time_utils.timestamp_indian',
               return_value=fixed_now), \
         patch('backend.brokers.service.calendar_gate._conn_calendar_verdict_detail',
               new_callable=AsyncMock,
               return_value=(CAL_OPEN, {"resolved_via": "equity"})), \
         patch('backend.brokers.service.calendar_gate._persist_verdict_row',
               new_callable=AsyncMock) as mock_persist, \
         patch('backend.brokers.service.app._try_start_ticker') as mock_try_start, \
         patch('asyncio.sleep', new_callable=AsyncMock,
               side_effect=[None, None, asyncio.CancelledError()]):
        await _ticker_watchdog()

    assert mock_persist.call_count == 1
    assert mock_persist.call_args.args[0] == date(2026, 10, 24)
    detail_arg = mock_persist.call_args.args[2]
    assert detail_arg["ticker_already_connected"] is True
    mock_try_start.assert_not_called()


@pytest.mark.asyncio
async def test_ticker_watchdog_started_records_new_row_when_date_advances():
    """Same scenario, but the IST date advances between the two ticks
    (a midnight rollover while conn_service stays connected throughout):
    a SECOND persisted row is written for the new date, not a
    duplicate/skip of the first."""
    from backend.brokers.service.app import _ticker_watchdog
    from backend.brokers.service.calendar_gate import CAL_OPEN

    ticker = _make_started_ticker()
    dates = [
        datetime(2026, 10, 24, 23, 59, 0),
        datetime(2026, 10, 25, 0, 1, 0),
    ]
    # `timestamp_indian()` is read twice per tick (once inside
    # `record_daily_verdict_if_needed`, once in Phase 2b) — both call
    # sites within one tick must see the SAME simulated date. Advance
    # the index only from the `asyncio.sleep` side effect (once per
    # loop iteration), never from `timestamp_indian` itself.
    tick = {"n": 0}
    sleep_calls = {"n": 0}

    def _fake_timestamp_indian():
        return dates[tick["n"]]

    async def _fake_sleep(*args, **kwargs):
        sleep_calls["n"] += 1
        if sleep_calls["n"] == 1:
            return  # first tick: tick["n"] stays 0 → dates[0]
        if sleep_calls["n"] == 2:
            tick["n"] = 1  # second tick: dates[1]
            return
        raise asyncio.CancelledError()  # stop after exactly 2 ticks

    with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
         patch('backend.shared.helpers.date_time_utils.is_any_segment_open',
               return_value=False), \
         patch('backend.shared.helpers.date_time_utils.timestamp_indian',
               side_effect=_fake_timestamp_indian), \
         patch('backend.brokers.service.calendar_gate._conn_calendar_verdict_detail',
               new_callable=AsyncMock,
               return_value=(CAL_OPEN, {"resolved_via": "equity"})), \
         patch('backend.brokers.service.calendar_gate._persist_verdict_row',
               new_callable=AsyncMock) as mock_persist, \
         patch('backend.brokers.service.app._try_start_ticker') as mock_try_start, \
         patch('asyncio.sleep', side_effect=_fake_sleep):
        await _ticker_watchdog()

    assert mock_persist.call_count == 2
    assert mock_persist.call_args_list[0].args[0] == date(2026, 10, 24)
    assert mock_persist.call_args_list[1].args[0] == date(2026, 10, 25)
    mock_try_start.assert_not_called()


@pytest.mark.asyncio
async def test_ticker_watchdog_started_persist_failure_does_not_block_health_check():
    """A persistence failure inside `record_daily_verdict_if_needed`
    (e.g. the audit table missing) must never stall or skip the
    watchdog's pre-existing Phase 2b health-check logic on the same
    tick — the new call's one hard invariant is that it never interacts
    with the failover/health machinery, including on its own failure."""
    from backend.brokers.service.app import _ticker_watchdog
    from backend.brokers.service.calendar_gate import CAL_OPEN

    ticker = _make_started_ticker()
    fixed_now = datetime(2026, 10, 24, 10, 0, 0)

    with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
         patch('backend.shared.helpers.date_time_utils.is_any_segment_open',
               return_value=False) as mock_segment_open, \
         patch('backend.shared.helpers.date_time_utils.timestamp_indian',
               return_value=fixed_now), \
         patch('backend.brokers.service.calendar_gate._conn_calendar_verdict_detail',
               new_callable=AsyncMock,
               return_value=(CAL_OPEN, {"resolved_via": "equity"})), \
         patch('backend.brokers.service.calendar_gate._persist_verdict_row',
               new_callable=AsyncMock, side_effect=Exception("table missing")), \
         patch('backend.brokers.service.app._try_start_ticker') as mock_try_start, \
         patch('asyncio.sleep', new_callable=AsyncMock,
               side_effect=[None, asyncio.CancelledError()]):
        await _ticker_watchdog()

    mock_segment_open.assert_called_once()
    ticker.reset_unhealthy.assert_called_once()
    mock_try_start.assert_not_called()


@pytest.mark.asyncio
async def test_ticker_watchdog_boot_then_connected_records_exactly_one_row():
    """Cross-function handoff: Phase 1's REAL `conn_should_attempt_
    connect()` (left unmocked here) persists the boot-day row and sets
    the shared `_last_recorded_date` sentinel on tick 1; once the ticker
    comes up (simulated by `_try_start_ticker`'s own side effect),
    `record_daily_verdict_if_needed()` on the VERY NEXT tick must see
    that sentinel already set and no-op — not double-write the same
    day. This is the actual handoff between the two writers, not either
    one in isolation: if `global _last_recorded_date` were ever dropped
    from `conn_should_attempt_connect`, this test would catch it (tick 2
    would recompute+persist again, call_count would go to 2)."""
    from backend.brokers.service.app import _ticker_watchdog
    from backend.brokers.service.calendar_gate import CAL_OPEN

    started = {"v": False}
    ticker = _make_started_ticker()
    ticker.status.side_effect = lambda: {"started": started["v"]}

    def _fake_try_start(*args, **kwargs):
        started["v"] = True
        return True

    fixed_now = datetime(2026, 10, 24, 10, 0, 0)

    with patch('backend.brokers.kite_ticker.get_ticker', return_value=ticker), \
         patch('backend.shared.helpers.date_time_utils.is_any_segment_open',
               return_value=False), \
         patch('backend.shared.helpers.date_time_utils.timestamp_indian',
               return_value=fixed_now), \
         patch('backend.brokers.service.calendar_gate._conn_calendar_verdict_detail',
               new_callable=AsyncMock,
               return_value=(CAL_OPEN, {"resolved_via": "equity"})), \
         patch('backend.brokers.service.calendar_gate._persist_verdict_row',
               new_callable=AsyncMock) as mock_persist, \
         patch('backend.brokers.service.app._try_start_ticker',
               side_effect=_fake_try_start) as mock_try_start, \
         patch('asyncio.sleep', new_callable=AsyncMock,
               side_effect=[None, None, asyncio.CancelledError()]):
        await _ticker_watchdog()

    # Tick 1: not started → Phase 1's real conn_should_attempt_connect()
    # writes the boot-day row (no "ticker_already_connected" tag — that
    # tag is only added by record_daily_verdict_if_needed()) and starts
    # the ticker. Tick 2: started → record_daily_verdict_if_needed()
    # sees _last_recorded_date already == today and no-ops.
    assert mock_persist.call_count == 1
    assert "ticker_already_connected" not in mock_persist.call_args.args[2]
    mock_try_start.assert_called_once()


@pytest.mark.asyncio
async def test_conn_should_attempt_connect_closed_day_log_dedup():
    """The closed-day INFO log line is deduped on (date, verdict) across
    repeated calls — but persistence itself is NOT deduped and still
    upserts every call (only the log line is)."""
    from backend.brokers.service.calendar_gate import conn_should_attempt_connect, CAL_CLOSED

    d1 = datetime(2026, 10, 24, 9, 0, 0)
    d2 = datetime(2026, 10, 25, 9, 0, 0)

    with patch('backend.brokers.service.calendar_gate.logger') as mock_logger, \
         patch('backend.shared.helpers.date_time_utils.timestamp_indian',
               side_effect=[d1, d1, d2]), \
         patch('backend.brokers.service.calendar_gate._conn_calendar_verdict_detail',
               new_callable=AsyncMock, return_value=(CAL_CLOSED, {})), \
         patch('backend.brokers.service.calendar_gate._persist_verdict_row',
               new_callable=AsyncMock) as mock_persist:
        await conn_should_attempt_connect()  # d1 — logs
        await conn_should_attempt_connect()  # d1 again — suppressed
        await conn_should_attempt_connect()  # d2 — new key, logs again

    assert mock_logger.info.call_count == 2
    assert mock_persist.call_count == 3
