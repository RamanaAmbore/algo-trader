"""Tests for orders timeout guards (Fix: Chase tab hanging).

Covers:
  1. _fetch_orders returns promptly when a broker hangs (8s per-broker timeout).
  2. _chase_snapshot_broker_status_by_id returns {} on asyncio.TimeoutError without raising.

These tests FAIL if the fixes are reverted.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Test 1: _fetch_orders per-broker timeout ──────────────────────────────────

class TestFetchOrdersBrokerTimeout:
    """_fetch_orders must not block indefinitely when a broker hangs."""

    def _make_slow_broker(self, account: str, sleep_seconds: float):
        """Return a fake broker whose orders() sleeps for sleep_seconds."""
        def slow_orders():
            time.sleep(sleep_seconds)
            return [{"order_id": "X", "status": "COMPLETE", "tradingsymbol": "INFY",
                     "exchange": "NSE", "transaction_type": "BUY", "quantity": 1,
                     "pending_quantity": 0, "filled_quantity": 1, "price": 0.0,
                     "trigger_price": 0.0, "average_price": 100.0,
                     "order_type": "MARKET", "product": "CNC", "variety": "regular",
                     "order_timestamp": "", "exchange_timestamp": "",
                     "status_message": "", "tag": ""}]

        broker = MagicMock()
        broker.account = account
        broker.orders.side_effect = slow_orders
        return broker

    def _make_fast_broker(self, account: str):
        """Return a fast broker with one order."""
        broker = MagicMock()
        broker.account = account
        broker.orders.return_value = [
            {"order_id": "FAST-1", "status": "COMPLETE", "tradingsymbol": "TCS",
             "exchange": "NSE", "transaction_type": "SELL", "quantity": 2,
             "pending_quantity": 0, "filled_quantity": 2, "price": 0.0,
             "trigger_price": 0.0, "average_price": 3500.0,
             "order_type": "MARKET", "product": "CNC", "variety": "regular",
             "order_timestamp": "", "exchange_timestamp": "",
             "status_message": "", "tag": ""}
        ]
        return broker

    def test_slow_broker_does_not_block_forever(self):
        """A broker that hangs for 10s must not block _fetch_orders past ~9s.

        The fix uses shutdown(wait=False, cancel_futures=True) so the caller
        returns as soon as all per-broker timeouts fire, without waiting for
        slow threads to actually finish.
        """
        slow = self._make_slow_broker("ACC-SLOW", sleep_seconds=10)

        from backend.api.routes.orders_helpers import _fetch_orders

        with patch("backend.brokers.registry.all_brokers", return_value=[slow]):
            t0 = time.monotonic()
            result = _fetch_orders()
            elapsed = time.monotonic() - t0

        # Must complete in under 9.5 seconds (8s timeout + small overhead)
        assert elapsed < 9.5, f"_fetch_orders blocked for {elapsed:.1f}s (expected < 9.5)"
        # Timed-out account contributes empty list → no rows from it
        assert result.rows == []

    def test_slow_broker_does_not_block_fast_broker(self):
        """Orders from a fast broker must appear even when another broker hangs."""
        slow = self._make_slow_broker("ACC-SLOW", sleep_seconds=10)
        fast = self._make_fast_broker("ACC-FAST")

        from backend.api.routes.orders_helpers import _fetch_orders

        with patch("backend.brokers.registry.all_brokers", return_value=[slow, fast]):
            t0 = time.monotonic()
            result = _fetch_orders()
            elapsed = time.monotonic() - t0

        assert elapsed < 9.5, f"_fetch_orders blocked for {elapsed:.1f}s"
        # Fast broker's order must be present
        order_ids = [r.order_id for r in result.rows]
        assert "FAST-1" in order_ids

    def test_no_brokers_returns_empty(self):
        """With no brokers registered, returns empty OrdersResponse immediately."""
        from backend.api.routes.orders_helpers import _fetch_orders

        with patch("backend.brokers.registry.all_brokers", return_value=[]):
            result = _fetch_orders()

        assert result.rows == []

    def test_broker_exception_contributes_empty_list(self):
        """A broker that raises an exception must not crash _fetch_orders."""
        broken = MagicMock()
        broken.account = "ACC-BROKEN"
        broken.orders.side_effect = RuntimeError("network error")

        from backend.api.routes.orders_helpers import _fetch_orders

        with patch("backend.brokers.registry.all_brokers", return_value=[broken]):
            result = _fetch_orders()

        assert result.rows == []


# ── Test 2: _chase_snapshot_broker_status_by_id asyncio.TimeoutError ─────────

class TestChaseSnapshotTimeout:
    """_chase_snapshot_broker_status_by_id must return {} on timeout without raising."""

    @pytest.mark.asyncio
    async def test_returns_empty_dict_on_asyncio_timeout(self):
        """When get_or_fetch hangs and wait_for times out, must return {}."""
        from backend.api.routes.orders import _chase_snapshot_broker_status_by_id

        async def _hanging_get_or_fetch(*args, **kwargs):
            await asyncio.sleep(60)  # hangs

        with patch("backend.api.routes.orders.get_or_fetch", side_effect=_hanging_get_or_fetch):
            with patch("backend.api.routes.orders.asyncio.wait_for",
                       side_effect=asyncio.TimeoutError):
                result = await _chase_snapshot_broker_status_by_id()

        assert result == {}

    @pytest.mark.asyncio
    async def test_does_not_raise_on_asyncio_timeout(self):
        """asyncio.TimeoutError must be caught — no exception propagates to caller."""
        from backend.api.routes.orders import _chase_snapshot_broker_status_by_id

        with patch("backend.api.routes.orders.asyncio.wait_for",
                   side_effect=asyncio.TimeoutError):
            try:
                result = await _chase_snapshot_broker_status_by_id()
            except asyncio.TimeoutError:
                pytest.fail("_chase_snapshot_broker_status_by_id must not propagate TimeoutError")

        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_empty_dict_on_generic_exception(self):
        """Generic exceptions from get_or_fetch must also be swallowed."""
        from backend.api.routes.orders import _chase_snapshot_broker_status_by_id

        with patch("backend.api.routes.orders.asyncio.wait_for",
                   side_effect=RuntimeError("connection refused")):
            result = await _chase_snapshot_broker_status_by_id()

        assert result == {}

    @pytest.mark.asyncio
    async def test_happy_path_returns_order_map(self):
        """When get_or_fetch succeeds, order_id → status mapping is returned."""
        from backend.api.routes.orders import _chase_snapshot_broker_status_by_id
        from backend.api.schemas import OrdersResponse
        from backend.shared.helpers.date_time_utils import timestamp_display

        fake_row = SimpleNamespace(
            order_id="ORD-123",
            status="COMPLETE",
            average_price=250.5,
        )
        fake_resp = OrdersResponse(rows=[fake_row], refreshed_at=timestamp_display())

        async def _immediate(*args, **kwargs):
            return fake_resp

        with patch("backend.api.routes.orders.asyncio.wait_for", side_effect=_immediate):
            result = await _chase_snapshot_broker_status_by_id()

        assert "ORD-123" in result
        assert result["ORD-123"]["status"] == "COMPLETE"
        assert result["ORD-123"]["average_price"] == 250.5
