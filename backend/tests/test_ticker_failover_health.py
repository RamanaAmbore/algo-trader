"""Tests for the failover-related health surfaces.

Covers:
  * TickerStatus schema populates the failover fields from a
    TickerManager status() dict.
  * BrokerAccountHealth `is_active_ticker` flips to True for the
    account matching TickerManager.current_account().
  * conn_service /health returns the `ticker` block including
    `failover_list` derived from the priority map.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestTickerStatusSchemaMap:
    """`_ticker_status()` in health.py passes every failover key from
    the TickerManager dict into the msgspec Struct."""

    def test_failover_fields_populate_from_status_dict(self):
        from backend.api.routes.health import _ticker_status, TickerStatus

        stub_dict = {
            "started": True,
            "connected": True,
            "subscribed_count": 42,
            "ticks_held": 40,
            "stale_count": 2,
            "max_age_seconds": 12.5,
            "stale_top": ["NIFTY@never", "RELIANCE@75s"],
            "active_account": "ZG0790",
            "failover_list": ["ZG0790", "ZJ6294"],
            "consecutive_unhealthy": 1,
            "swaps_last_hour": 2,
            "last_swap_at": 1_700_000_000.0,
        }
        with patch(
            "backend.brokers.kite_ticker.get_ticker",
            return_value=MagicMock(status=MagicMock(return_value=stub_dict)),
        ):
            result: TickerStatus = _ticker_status()
        assert result.active_account == "ZG0790"
        assert result.failover_list == ["ZG0790", "ZJ6294"]
        assert result.consecutive_unhealthy == 1
        assert result.swaps_last_hour == 2
        assert result.last_swap_at == 1_700_000_000.0

    def test_missing_failover_keys_default_safely(self):
        """Older conn_service responses may lack the new keys — the
        Struct should default to empty/zero, not raise."""
        from backend.api.routes.health import _ticker_status

        stub_dict = {
            "started": True, "connected": True,
            "subscribed_count": 0, "ticks_held": 0,
        }
        with patch(
            "backend.brokers.kite_ticker.get_ticker",
            return_value=MagicMock(status=MagicMock(return_value=stub_dict)),
        ):
            result = _ticker_status()
        assert result.active_account == ""
        assert result.failover_list == []
        assert result.consecutive_unhealthy == 0
        assert result.swaps_last_hour == 0
        assert result.last_swap_at == 0.0


class TestBrokerAccountHealthActiveTickerFlag:
    """`is_active_ticker` in BrokerAccountHealth mirrors the active
    Kite account. Only ONE account may carry the flag at any time."""

    def test_flag_defaults_false(self):
        from backend.api.routes.health import BrokerAccountHealth

        row = BrokerAccountHealth(
            account="ZG0790", broker="kite", state="green", reason="ok",
            last_good_at=None, last_check_at=None,
        )
        assert row.is_active_ticker is False

    def test_flag_can_be_set_true(self):
        from backend.api.routes.health import BrokerAccountHealth

        row = BrokerAccountHealth(
            account="ZG0790", broker="kite", state="green", reason="ok",
            last_good_at=None, last_check_at=None, is_active_ticker=True,
        )
        assert row.is_active_ticker is True


class TestConnServiceHealthPayload:
    """`/health` in conn_service exposes the ticker sub-object with
    failover fields — smoke check that the response builder wires
    them through without dropping keys.

    We exercise the controller method as an unbound function
    (Controller instances are constructed by Litestar's router at
    request time, not by tests) — `HealthController.health(self=None)`
    still runs the function body since it never uses `self`.
    """

    @pytest.mark.asyncio
    async def test_ticker_block_populated(self):
        from backend.brokers.service.routes import HealthController

        stub_ticker = MagicMock()
        stub_ticker.status.return_value = {
            "started": True, "connected": True,
            "subscribed_count": 10, "ticks_held": 10,
            "stale_count": 0, "max_age_seconds": 5.0, "stale_top": [],
            "active_account": "ZG0790",
            "consecutive_unhealthy": 0,
            "swaps_last_hour": 0,
            "last_swap_at": 0.0,
        }
        with patch(
            "backend.brokers.kite_ticker.get_ticker", return_value=stub_ticker
        ), patch(
            "backend.brokers.service.app._kite_failover_list",
            return_value=["ZG0790", "ZJ6294"],
        ), patch(
            "backend.brokers.connections.Connections"
        ) as MockConn:
            MockConn.return_value.conn = {"ZG0790": object(), "ZJ6294": object()}
            resp = await HealthController.health.fn(self=None)

        assert resp.ok is True
        assert resp.ticker is not None
        assert resp.ticker["active_account"] == "ZG0790"
        assert resp.ticker["failover_list"] == ["ZG0790", "ZJ6294"]
        assert resp.ticker["consecutive_unhealthy"] == 0
        assert resp.ticker["swaps_last_hour"] == 0

    @pytest.mark.asyncio
    async def test_ticker_snapshot_failure_does_not_break_health(self):
        """When the ticker singleton isn't ready (very early boot),
        /health must still return ok=True so systemd probes pass."""
        from backend.brokers.service.routes import HealthController

        with patch(
            "backend.brokers.kite_ticker.get_ticker",
            side_effect=RuntimeError("not ready yet"),
        ), patch(
            "backend.brokers.connections.Connections"
        ) as MockConn:
            MockConn.return_value.conn = {}
            resp = await HealthController.health.fn(self=None)

        assert resp.ok is True
        assert resp.ticker is None
