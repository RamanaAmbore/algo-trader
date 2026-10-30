"""Tests for the closed-hours Orders/GTT fix (2026-10-30 live incident).

Operator reported "so many error alerts before 8:00 AM IST" — root cause
was `/api/orders` and `/api/orders/gtts` polling the broker unconditionally
(the frontend's own poll loop doesn't know or care about market hours), and
before the day's Kite access_token refreshes every call fails identically,
fanning one real condition into one ERROR_AGENT alert per poll. Follow-up
operator instruction: "it should show the stored order info without
checking with brokers during market close hours."

Covers:
  1. `dedup_log_error()` (recovery.py) — repeat-suppression for a call
     site that catches its own exception and wants one alert per cooldown
     window, not one per repeat.
  2. `_fetch_orders()`/`_fetch_gtts()` skip the broker entirely when
     `is_any_segment_open_cached()` says closed (no `.orders()`/`.get_gtts()`
     call at all).
  3. `_save_order_book_snapshot_async`/`_load_order_book_snapshot_async`
     (orders_helpers.py) — the DB-backed "stored order info" persistence,
     independent of the sync `run_on_main_loop` bridge.
  4. `_closed_hours_orders_response()` prefers the DB snapshot over the
     in-process TTL cache, and that over empty.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest

from backend.shared.helpers.recovery import (
    DEDUP_COOLDOWN_S,
    _reset_dedup_for_tests,
    dedup_log_error,
)


# ── dedup_log_error ──────────────────────────────────────────────────────

class TestDedupLogError:
    def setup_method(self):
        _reset_dedup_for_tests()

    def test_first_occurrence_logs_at_error(self):
        logger = MagicMock()
        logged = dedup_log_error(logger, "k1", "boom")
        assert logged is True
        logger.error.assert_called_once_with("boom")
        logger.debug.assert_not_called()

    def test_repeat_within_cooldown_is_suppressed_to_debug(self):
        logger = MagicMock()
        dedup_log_error(logger, "k1", "boom")
        logger.reset_mock()
        logged = dedup_log_error(logger, "k1", "boom again")
        assert logged is False
        logger.error.assert_not_called()
        logger.debug.assert_called_once()

    def test_different_key_is_not_suppressed(self):
        """Different account / exception-class key must not dedup against
        an unrelated failure — only the SAME failure class is throttled."""
        logger = MagicMock()
        dedup_log_error(logger, "k1", "boom")
        logger.reset_mock()
        logged = dedup_log_error(logger, "k2", "a different boom")
        assert logged is True
        logger.error.assert_called_once()

    def test_repeat_after_cooldown_elapses_logs_again(self, monkeypatch):
        logger = MagicMock()
        t = [1000.0]
        monkeypatch.setattr(time, "monotonic", lambda: t[0])
        dedup_log_error(logger, "k1", "boom", cooldown_s=5)
        t[0] += 5.1
        logger.reset_mock()
        logged = dedup_log_error(logger, "k1", "boom again", cooldown_s=5)
        assert logged is True
        logger.error.assert_called_once()

    def test_default_cooldown_is_five_minutes(self):
        assert DEDUP_COOLDOWN_S == 300


# ── _fetch_orders / _fetch_gtts closed-window gate ──────────────────────

class TestFetchOrdersClosedWindowGate:
    def test_closed_window_never_calls_broker(self):
        from backend.api.routes.orders_helpers import _fetch_orders

        broker = MagicMock()
        broker.account = "ZG0790"
        broker.broker_id = "zerodha_kite"

        with patch("backend.brokers.registry.all_brokers", return_value=[broker]), \
             patch("backend.api.background.is_any_segment_open_cached", return_value=False), \
             patch("backend.api.routes.orders_helpers._load_order_book_snapshot_sync",
                   return_value=None), \
             patch("backend.api.cache.peek_stale", return_value=None):
            result = _fetch_orders()

        broker.orders.assert_not_called()
        assert result.rows == []

    def test_open_window_calls_broker_as_normal(self):
        from backend.api.routes.orders_helpers import _fetch_orders

        broker = MagicMock()
        broker.account = "ZG0790"
        broker.broker_id = "zerodha_kite"
        broker.orders.return_value = []

        with patch("backend.brokers.registry.all_brokers", return_value=[broker]), \
             patch("backend.api.background.is_any_segment_open_cached", return_value=True):
            _fetch_orders()

        broker.orders.assert_called_once()

    def test_closed_window_prefers_db_snapshot_over_in_process_cache(self):
        """Priority order: DB snapshot (durable) > in-process TTL cache
        (`peek_stale`) > empty. The DB snapshot must win even when the
        in-process cache also has a (different) value."""
        from backend.api.routes.orders_helpers import _fetch_orders
        from backend.api.schemas import OrderRow

        db_row = OrderRow(
            order_id="DB-1", account="ZG0790", exchange="NFO",
            tradingsymbol="NIFTY26OCTFUT", transaction_type="BUY",
            quantity=50, pending_quantity=0, filled_quantity=50,
            price=0.0, trigger_price=0.0, average_price=100.0,
            status="COMPLETE", order_type="LIMIT", product="NRML",
            variety="regular", order_timestamp="2026-10-29 10:00:00",
        )
        payload_json = msgspec.json.encode([db_row]).decode()

        broker = MagicMock()
        broker.account = "ZG0790"
        broker.broker_id = "zerodha_kite"

        with patch("backend.brokers.registry.all_brokers", return_value=[broker]), \
             patch("backend.api.background.is_any_segment_open_cached", return_value=False), \
             patch("backend.api.routes.orders_helpers._load_order_book_snapshot_sync",
                   return_value=(payload_json, None)):
            result = _fetch_orders()

        broker.orders.assert_not_called()
        assert len(result.rows) == 1
        assert result.rows[0].order_id == "DB-1"

    def test_closed_window_falls_back_to_in_process_cache_when_db_empty(self):
        from backend.api.routes.orders_helpers import _fetch_orders
        from backend.api.schemas import OrdersResponse

        broker = MagicMock()
        broker.account = "ZG0790"
        cached = OrdersResponse(rows=[], refreshed_at="cached-stamp")

        with patch("backend.brokers.registry.all_brokers", return_value=[broker]), \
             patch("backend.api.background.is_any_segment_open_cached", return_value=False), \
             patch("backend.api.routes.orders_helpers._load_order_book_snapshot_sync",
                   return_value=None), \
             patch("backend.api.cache.peek_stale", return_value=cached):
            result = _fetch_orders()

        broker.orders.assert_not_called()
        assert result is cached


# ── DB snapshot persistence (async, independent of run_on_main_loop) ────

class TestOrderBookSnapshotPersistence:
    @pytest.mark.asyncio
    async def test_save_upserts_by_kind_and_date(self):
        from backend.api.routes.orders_helpers import _save_order_book_snapshot_async

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()

        with patch("backend.api.database.async_session", lambda: mock_session):
            await _save_order_book_snapshot_async("orders", '[{"order_id":"X"}]')

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_failure_is_swallowed_and_logged(self):
        """A DB outage while saving must never propagate into the live
        response path — `_fetch_orders()` already returned its result
        to the caller by the time this fires."""
        from backend.api.routes.orders_helpers import _save_order_book_snapshot_async

        with patch("backend.api.database.async_session", side_effect=RuntimeError("db down")):
            await _save_order_book_snapshot_async("orders", "[]")  # must not raise

    @pytest.mark.asyncio
    async def test_load_returns_payload_and_captured_at(self):
        from backend.api.routes.orders_helpers import _load_order_book_snapshot_async

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        _result = MagicMock()
        _result.first.return_value = ('[{"order_id":"X"}]', "2026-10-29T18:00:00+00:00")
        mock_session.execute = AsyncMock(return_value=_result)

        with patch("backend.api.database.async_session", lambda: mock_session):
            out = await _load_order_book_snapshot_async("orders")

        assert out == ('[{"order_id":"X"}]', "2026-10-29T18:00:00+00:00")

    @pytest.mark.asyncio
    async def test_load_returns_none_when_never_saved(self):
        from backend.api.routes.orders_helpers import _load_order_book_snapshot_async

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        _result = MagicMock()
        _result.first.return_value = None
        mock_session.execute = AsyncMock(return_value=_result)

        with patch("backend.api.database.async_session", lambda: mock_session):
            out = await _load_order_book_snapshot_async("orders")

        assert out is None

    @pytest.mark.asyncio
    async def test_load_failure_is_swallowed_and_returns_none(self):
        from backend.api.routes.orders_helpers import _load_order_book_snapshot_async

        with patch("backend.api.database.async_session", side_effect=RuntimeError("db down")):
            out = await _load_order_book_snapshot_async("orders")  # must not raise

        assert out is None
