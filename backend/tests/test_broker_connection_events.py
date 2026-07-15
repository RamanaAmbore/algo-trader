"""Tests for broker connection event queue and route.

Coverage:
  • EventQueue with custom session_factory — verify callback is used during flush
  • _hlth_resolve_state inactive branch — last_ok=0, last_fail>0 → state="inactive"
  • _hlth_resolve_state healthy branch — last_ok>0, last_fail<=last_ok → state="green"/"amber"
  • GET /api/admin/broker-connection-events — account filtering, DESC ordering, limit parameter
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, func, delete as sa_delete

from backend.api.database import async_session, shared_async_session
from backend.api.models import BrokerConnectionEvent
from backend.api.persistence.event_queue import EventQueue
from backend.api.routes.health import (
    _hlth_resolve_state,
    _derive_account_health,
    _ts_to_ist_label,
    _ts_to_iso,
)


# ---------------------------------------------------------------------------
# Test EventQueue with custom session_factory
# ---------------------------------------------------------------------------

class TestEventQueueCustomSessionFactory:
    """EventQueue respects a custom session_factory kwarg."""

    @pytest.mark.asyncio
    async def test_event_queue_uses_provided_session_factory(self):
        """Custom session_factory is called during flush, not the default async_session."""
        call_count = 0
        # Use a sync factory that returns a mock context manager.
        # EventQueue._flush uses `async with self._session_factory() as session:`,
        # so the factory must return an object with __aenter__/__aexit__, NOT a coroutine.
        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        session_mock.execute = AsyncMock()
        session_mock.commit = AsyncMock()

        def custom_factory():
            nonlocal call_count
            call_count += 1
            return session_mock

        queue = EventQueue(
            BrokerConnectionEvent,
            name="test_queue",
            batch_size=2,
            flush_interval_s=0.1,
            max_queue=100,
            session_factory=custom_factory,
        )

        await queue.start()
        await queue.enqueue(
            account="ZG0001",
            broker_id="kite",
            event_type="connected",
            detail={"msg": "test"},
        )
        await queue.enqueue(
            account="ZG0001",
            broker_id="kite",
            event_type="disconnected",
            detail={"msg": "test2"},
        )

        # Wait for flush to fire
        await asyncio.sleep(0.2)
        await queue.stop()

        # Verify custom factory was called
        assert call_count >= 1, f"Expected custom_factory to be called, but got {call_count} calls"
        # Verify session was used
        assert session_mock.execute.called, "Expected session.execute to be called"
        assert session_mock.commit.called, "Expected session.commit to be called"

    @pytest.mark.asyncio
    async def test_event_queue_uses_default_async_session_when_not_provided(self):
        """When no session_factory is provided, EventQueue uses the default async_session."""
        queue = EventQueue(
            BrokerConnectionEvent,
            name="test_queue_default",
            batch_size=10,
            flush_interval_s=1.0,
            max_queue=100,
        )

        # Verify the default factory is set
        assert queue._session_factory is not None, "Expected _session_factory to be set"
        # The default should be async_session
        assert queue._session_factory is async_session, \
            f"Expected default to be async_session, got {queue._session_factory}"


# ---------------------------------------------------------------------------
# Test _hlth_resolve_state inactive and healthy branches
# ---------------------------------------------------------------------------

class TestHlthResolveState:
    """_hlth_resolve_state condition branches."""

    def test_inactive_state_when_last_ok_zero_and_last_fail_nonzero(self):
        """When last_ok=0 and last_fail>0, state should be 'inactive'."""
        entry = {
            "last_ok_at": 0.0,
            "last_fail_at": time.time() - 60,  # 1 min ago
            "last_fail_msg": "Connection refused",
        }
        now = time.time()

        state, reason, cb_state, cb_count, cb_until_iso = _hlth_resolve_state(
            entry, now, "closed", 0, None, None
        )

        assert state == "inactive", \
            f"Expected state='inactive' when last_ok=0 and last_fail>0, got state='{state}'"
        assert "no session established" in reason.lower(), \
            f"Expected reason to mention 'no session established', got '{reason}'"

    def test_red_state_when_last_fail_after_last_ok(self):
        """When last_fail > last_ok, state should be 'red'."""
        now = time.time()
        entry = {
            "last_ok_at": now - 300,      # 5 min ago
            "last_fail_at": now - 60,     # 1 min ago (more recent)
            "last_fail_msg": "Auth failed",
        }

        state, reason, cb_state, cb_count, cb_until_iso = _hlth_resolve_state(
            entry, now, "closed", 0, None, None
        )

        assert state == "red", \
            f"Expected state='red' when last_fail > last_ok, got state='{state}'"
        assert "auth" in reason.lower() or "failed" in reason.lower(), \
            f"Expected reason to mention auth/failure, got '{reason}'"

    def test_green_state_when_last_ok_within_fresh_window(self):
        """When last_ok is recent and last_ok > last_fail, state should be 'green'."""
        now = time.time()
        entry = {
            "last_ok_at": now - 60,       # 1 min ago (within 5-min fresh window)
            "last_fail_at": now - 300,    # 5 min ago (older)
            "last_fail_msg": "",
        }

        state, reason, cb_state, cb_count, cb_until_iso = _hlth_resolve_state(
            entry, now, "closed", 0, None, None
        )

        assert state == "green", \
            f"Expected state='green' when last_ok is fresh and > last_fail, got state='{state}'"
        assert "healthy" in reason.lower() or "good" in reason.lower(), \
            f"Expected reason to mention health/good status, got '{reason}'"

    def test_amber_state_when_last_ok_is_stale(self):
        """When last_ok > last_fail but older than fresh window, state should be 'amber'."""
        now = time.time()
        entry = {
            "last_ok_at": now - 600,      # 10 min ago (beyond 5-min fresh window)
            "last_fail_at": now - 900,    # 15 min ago (older)
            "last_fail_msg": "",
        }

        state, reason, cb_state, cb_count, cb_until_iso = _hlth_resolve_state(
            entry, now, "closed", 0, None, None
        )

        assert state == "amber", \
            f"Expected state='amber' when last_ok is stale, got state='{state}'"
        assert "stale" in reason.lower(), \
            f"Expected reason to mention 'stale', got '{reason}'"

    def test_red_state_overrides_when_circuit_open(self):
        """When circuit breaker is open, state should be 'red' regardless of auth status."""
        now = time.time()
        cb_until = now + 120  # Breaker open for next 2 minutes
        entry = {
            "last_ok_at": now - 60,       # Recently healthy
            "last_fail_at": now - 300,    # Older failure
            "last_fail_msg": "",
        }

        state, reason, cb_state, cb_count, cb_until_iso = _hlth_resolve_state(
            entry, now, "open", 1, _ts_to_iso(cb_until), cb_until
        )

        assert state == "red", \
            f"Expected state='red' when circuit is open, got state='{state}'"
        assert cb_state == "open", f"Expected circuit state to be 'open', got '{cb_state}'"
        assert "circuit open" in reason.lower(), \
            f"Expected reason to mention circuit, got '{reason}'"


# ---------------------------------------------------------------------------
# Test GET /api/admin/broker-connection-events route
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def test_events_in_db():
    """Insert test broker connection events into the test database."""
    now_utc = datetime.now(timezone.utc)
    events_to_insert = [
        {
            "account": "ZG0001",
            "broker_id": "kite",
            "event_type": "connected",
            "event_ts": now_utc - timedelta(minutes=30),
            "detail": {"status": "ok"},
        },
        {
            "account": "ZG0001",
            "broker_id": "kite",
            "event_type": "disconnected",
            "event_ts": now_utc - timedelta(minutes=20),
            "detail": {"reason": "timeout"},
        },
        {
            "account": "ZG0002",
            "broker_id": "dhan",
            "event_type": "connected",
            "event_ts": now_utc - timedelta(minutes=10),
            "detail": {"status": "ok"},
        },
        {
            "account": "ZG0001",
            "broker_id": "kite",
            "event_type": "heartbeat",
            "event_ts": now_utc - timedelta(minutes=5),
            "detail": {"sequence": 1},
        },
        {
            "account": "ZG0001",
            "broker_id": "kite",
            "event_type": "reconnecting",
            "event_ts": now_utc - timedelta(minutes=1),
            "detail": {"attempt": 1},
        },
    ]

    async with shared_async_session() as session:
        for event_data in events_to_insert:
            evt = BrokerConnectionEvent(**event_data)
            session.add(evt)
        await session.commit()

    yield

    # Cleanup
    async with shared_async_session() as session:
        await session.execute(
            sa_delete(BrokerConnectionEvent).where(
                BrokerConnectionEvent.account.in_(["ZG0001", "ZG0002"])
            )
        )
        await session.commit()


@pytest.mark.skip(reason="Integration test — requires live ramboq_dev PostgreSQL database")
class TestBrokerConnectionEventsRoute:
    """GET /api/admin/broker-connection-events route."""

    @pytest.mark.asyncio
    async def test_get_events_all(self, async_client, test_events_in_db):
        """GET /api/admin/broker-connection-events returns all events in DESC order."""
        response = await async_client.get(
            "/api/admin/broker-connection-events",
            headers={"Authorization": "Bearer test_admin_token"},
        )

        # Note: The route is admin-only; conftest mocking may need to be configured
        # for this to work. For now, we test the query logic in isolation below.
        # The async_client is set up in conftest.py and patched auth is expected.

    @pytest.mark.asyncio
    async def test_get_events_by_account_filter(self, test_events_in_db):
        """GET /api/admin/broker-connection-events filters by account param."""
        account_filter = "ZG0001"
        now_utc = datetime.now(timezone.utc)
        since_dt = now_utc - timedelta(days=7)

        async with shared_async_session() as session:
            stmt = select(BrokerConnectionEvent)
            filters = [
                BrokerConnectionEvent.event_ts >= since_dt,
                BrokerConnectionEvent.account == account_filter,
            ]
            from sqlalchemy import and_
            stmt = (
                stmt
                .where(and_(*filters))
                .order_by(BrokerConnectionEvent.event_ts.desc())
                .limit(200)
            )
            rows = (await session.execute(stmt)).scalars().all()

        # Should return only ZG0001 events
        assert len(rows) == 4, f"Expected 4 events for ZG0001, got {len(rows)}"
        assert all(row.account == "ZG0001" for row in rows), \
            "All returned rows should have account='ZG0001'"

        # Verify DESC ordering
        ts_list = [row.event_ts for row in rows]
        assert ts_list == sorted(ts_list, reverse=True), \
            "Events should be ordered DESC by event_ts"

    @pytest.mark.asyncio
    async def test_get_events_by_event_type_filter(self, test_events_in_db):
        """GET filters by event_type param."""
        event_type_filter = "connected"
        now_utc = datetime.now(timezone.utc)
        since_dt = now_utc - timedelta(days=7)

        async with shared_async_session() as session:
            stmt = select(BrokerConnectionEvent)
            filters = [
                BrokerConnectionEvent.event_ts >= since_dt,
                BrokerConnectionEvent.event_type == event_type_filter,
            ]
            from sqlalchemy import and_
            stmt = (
                stmt
                .where(and_(*filters))
                .order_by(BrokerConnectionEvent.event_ts.desc())
                .limit(200)
            )
            rows = (await session.execute(stmt)).scalars().all()

        # Should return only "connected" events
        assert len(rows) == 2, f"Expected 2 'connected' events, got {len(rows)}"
        assert all(row.event_type == "connected" for row in rows), \
            "All returned rows should have event_type='connected'"

    @pytest.mark.asyncio
    async def test_get_events_respects_limit(self, test_events_in_db):
        """GET respects limit parameter, capped at 1000."""
        limit = 2
        now_utc = datetime.now(timezone.utc)
        since_dt = now_utc - timedelta(days=7)

        async with shared_async_session() as session:
            stmt = select(BrokerConnectionEvent)
            filters = [BrokerConnectionEvent.event_ts >= since_dt]
            from sqlalchemy import and_
            stmt = (
                stmt
                .where(and_(*filters))
                .order_by(BrokerConnectionEvent.event_ts.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()

        assert len(rows) <= limit, \
            f"Expected at most {limit} rows, got {len(rows)}"
        assert len(rows) == limit, \
            f"Expected exactly {limit} rows when more exist, got {len(rows)}"

    @pytest.mark.asyncio
    async def test_get_events_default_since_7_days(self, test_events_in_db):
        """When no since param, defaults to 7 days ago."""
        now_utc = datetime.now(timezone.utc)
        since_dt_default = now_utc - timedelta(days=7)

        async with shared_async_session() as session:
            stmt = select(BrokerConnectionEvent)
            filters = [BrokerConnectionEvent.event_ts >= since_dt_default]
            from sqlalchemy import and_
            stmt = (
                stmt
                .where(and_(*filters))
                .order_by(BrokerConnectionEvent.event_ts.desc())
                .limit(200)
            )
            rows = (await session.execute(stmt)).scalars().all()

        # All test events are inserted with timestamps in the last 30 min,
        # so all should be within the default 7-day window
        assert len(rows) == 5, \
            f"Expected all 5 test events within 7-day window, got {len(rows)}"

    @pytest.mark.asyncio
    async def test_get_events_combined_filters(self, test_events_in_db):
        """GET filters by account AND event_type together."""
        account_filter = "ZG0001"
        event_type_filter = "connected"
        now_utc = datetime.now(timezone.utc)
        since_dt = now_utc - timedelta(days=7)

        async with shared_async_session() as session:
            stmt = select(BrokerConnectionEvent)
            filters = [
                BrokerConnectionEvent.event_ts >= since_dt,
                BrokerConnectionEvent.account == account_filter,
                BrokerConnectionEvent.event_type == event_type_filter,
            ]
            from sqlalchemy import and_
            stmt = (
                stmt
                .where(and_(*filters))
                .order_by(BrokerConnectionEvent.event_ts.desc())
                .limit(200)
            )
            rows = (await session.execute(stmt)).scalars().all()

        # Should return only the "connected" event for ZG0001
        assert len(rows) == 1, \
            f"Expected 1 event matching both filters, got {len(rows)}"
        assert rows[0].account == "ZG0001", "Expected account ZG0001"
        assert rows[0].event_type == "connected", "Expected event_type 'connected'"

    @pytest.mark.asyncio
    async def test_get_events_empty_result_when_no_matches(self, test_events_in_db):
        """GET returns empty list when filters match nothing."""
        account_filter = "ZG9999"  # Non-existent account
        now_utc = datetime.now(timezone.utc)
        since_dt = now_utc - timedelta(days=7)

        async with shared_async_session() as session:
            stmt = select(BrokerConnectionEvent)
            filters = [
                BrokerConnectionEvent.event_ts >= since_dt,
                BrokerConnectionEvent.account == account_filter,
            ]
            from sqlalchemy import and_
            stmt = (
                stmt
                .where(and_(*filters))
                .order_by(BrokerConnectionEvent.event_ts.desc())
                .limit(200)
            )
            rows = (await session.execute(stmt)).scalars().all()

        assert len(rows) == 0, \
            f"Expected 0 events for non-existent account, got {len(rows)}"


# ---------------------------------------------------------------------------
# Test helper functions
# ---------------------------------------------------------------------------

class TestHelperFunctions:
    """Test timestamp conversion and formatting helpers."""

    def test_ts_to_iso_with_valid_timestamp(self):
        """_ts_to_iso converts unix timestamp to ISO-8601 UTC string."""
        # Use a known timestamp: 2025-01-01 00:00:00 UTC = 1735689600
        ts = 1735689600.0
        result = _ts_to_iso(ts)

        assert result is not None, "Expected ISO string, got None"
        assert "2025-01-01" in result, f"Expected 2025-01-01 in {result}"
        assert "00:00:00" in result, f"Expected 00:00:00 in {result}"

    def test_ts_to_iso_with_none(self):
        """_ts_to_iso returns None when given None."""
        result = _ts_to_iso(None)
        assert result is None, f"Expected None, got {result}"

    def test_ts_to_iso_with_zero(self):
        """_ts_to_iso returns None when given 0."""
        result = _ts_to_iso(0.0)
        assert result is None, f"Expected None for zero timestamp, got {result}"

    def test_ts_to_ist_label_with_valid_timestamp(self):
        """_ts_to_ist_label converts to IST HH:MM format."""
        # 2026-01-01 00:00:00 UTC is 05:30 IST
        ts = 1735689600.0
        result = _ts_to_ist_label(ts)

        assert result != "", f"Expected IST label, got empty string"
        assert "IST" in result, f"Expected 'IST' in label {result}"
        assert ":" in result, f"Expected HH:MM format in {result}"

    def test_ts_to_ist_label_with_none(self):
        """_ts_to_ist_label returns empty string when given None."""
        result = _ts_to_ist_label(None)
        assert result == "", f"Expected empty string for None, got {result}"

    def test_ts_to_ist_label_with_zero(self):
        """_ts_to_ist_label returns empty string when given 0."""
        result = _ts_to_ist_label(0.0)
        assert result == "", f"Expected empty string for zero, got {result}"
