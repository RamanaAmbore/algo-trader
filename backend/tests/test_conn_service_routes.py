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

import time
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from litestar.testing import TestClient

import pandas as pd

# Explicit imports to ensure coverage tracking
import backend.brokers.service.routes


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
