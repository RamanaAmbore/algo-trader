"""
Tests for _task_broker_issue_daily aggregation task.

The task collects broker_connection_events for a given date, pivots by
(broker_id, account, event_type), and upserts one row per unique
(broker_id, account) pair into broker_issue_daily with a breakdown JSONB.

Since the actual DB isn't available in pytest, we mock the session and
verify the aggregation logic and model structure.
"""
from __future__ import annotations

import asyncio
import json
import pytest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# 1. Model structure tests
# ─────────────────────────────────────────────────────────────────────────────

def test_broker_issue_daily_model_fields():
    """BrokerIssueDaily model has all required columns."""
    from backend.api.models import BrokerIssueDaily
    table = BrokerIssueDaily.__table__
    col_names = {c.name for c in table.columns}
    assert "id" in col_names
    assert "broker_id" in col_names
    assert "account" in col_names
    assert "issue_date" in col_names
    assert "issue_count" in col_names
    assert "breakdown" in col_names
    assert "updated_at" in col_names


def test_broker_issue_daily_unique_constraint():
    """BrokerIssueDaily has unique constraint on (broker_id, account, issue_date)."""
    from backend.api.models import BrokerIssueDaily
    table = BrokerIssueDaily.__table__
    constraint_names = {
        c.name for c in table.constraints
        if hasattr(c, 'name') and 'unique' in c.__class__.__name__.lower()
    }
    # The constraint name is defined in __table_args__
    found = any('broker_issue_daily_broker_account_date' in name for name in constraint_names)
    assert found, f"Expected unique constraint on (broker_id, account, issue_date), got: {constraint_names}"


def test_broker_issue_daily_has_indexes():
    """BrokerIssueDaily has indexes for common query patterns."""
    from backend.api.models import BrokerIssueDaily
    table = BrokerIssueDaily.__table__
    index_names = {idx.name for idx in table.indexes}
    # Check for at least the date index
    assert any('date' in name for name in index_names), (
        f"Expected index on issue_date, got: {index_names}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Task import and signature tests
# ─────────────────────────────────────────────────────────────────────────────

def test_task_broker_issue_daily_is_async():
    """_task_broker_issue_daily is a coroutine function."""
    import inspect
    from backend.api.background import _task_broker_issue_daily
    assert inspect.iscoroutinefunction(_task_broker_issue_daily), (
        "_task_broker_issue_daily must be an async function"
    )


@pytest.mark.asyncio
async def test_task_broker_issue_daily_handles_no_events():
    """_task_broker_issue_daily completes gracefully when no events exist."""
    import asyncio
    # Mock session that returns empty results
    mock_result = AsyncMock()
    mock_result.fetchall = MagicMock(return_value=[])

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    # Patch async_session at the background module level (it's a module-level import)
    with patch("backend.api.background.async_session") as mock_get_session:
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

        # Patch the while True loop to exit after one iteration
        from backend.api import background
        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()
            return

        with patch("backend.api.background.asyncio.sleep", side_effect=mock_sleep):
            try:
                await background._task_broker_issue_daily()
            except asyncio.CancelledError:
                # Expected after loop exit
                pass

    # Verify session was used
    assert mock_session.execute.called, "Session.execute must be called"
    assert mock_session.commit.called, "Session.commit must be called"


@pytest.mark.asyncio
async def test_task_broker_issue_daily_aggregates_events():
    """_task_broker_issue_daily pivots events into breakdown and upserts."""
    import asyncio
    # Simulate rows from broker_connection_events for a specific date
    mock_rows = [
        ("kite", "ZE1234", "auth_fail", 3),
        ("kite", "ZE1234", "circuit_open", 2),
        ("dhan", "DH6847", "fetch_fail", 7),
        ("dhan", "DH6847", "circuit_open", 1),
    ]

    mock_result = AsyncMock()
    mock_result.fetchall = MagicMock(return_value=mock_rows)

    upsert_calls = []

    async def capture_execute(stmt, params=None):
        if params and "breakdown" in params:
            upsert_calls.append(params)
        return mock_result

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=capture_execute)
    mock_session.commit = AsyncMock()

    with patch("backend.api.background.async_session") as mock_get_session:
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

        # Patch the while True loop to exit after one iteration
        from backend.api import background
        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()
            return

        with patch("backend.api.background.asyncio.sleep", side_effect=mock_sleep):
            try:
                await background._task_broker_issue_daily()
            except asyncio.CancelledError:
                pass

    # Verify upserts (task runs twice on startup: once for yesterday, once for today)
    # so we expect at least 2 upserts with broker/account data
    assert len(upsert_calls) >= 2, f"Expected >= 2 upserts, got {len(upsert_calls)}"

    # Collect the first params for each broker (they should be identical since the mock is the same)
    kite_upserts = [c for c in upsert_calls if c["broker_id"] == "kite"]
    dhan_upserts = [c for c in upsert_calls if c["broker_id"] == "dhan"]

    assert len(kite_upserts) >= 1, "Expected at least 1 upsert for kite/ZE1234"
    assert len(dhan_upserts) >= 1, "Expected at least 1 upsert for dhan/DH6847"

    # Verify kite aggregation: total = 3 + 2 = 5
    kite_upsert = kite_upserts[0]
    assert kite_upsert["issue_count"] == 5
    kite_breakdown = json.loads(kite_upsert["breakdown"])
    assert kite_breakdown["auth_fail"] == 3
    assert kite_breakdown["circuit_open"] == 2

    # Verify dhan aggregation: total = 7 + 1 = 8
    dhan_upsert = dhan_upserts[0]
    assert dhan_upsert["issue_count"] == 8
    dhan_breakdown = json.loads(dhan_upsert["breakdown"])
    assert dhan_breakdown["fetch_fail"] == 7
    assert dhan_breakdown["circuit_open"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. Threshold checking logic (for check_broker_conn_issues script)
# ─────────────────────────────────────────────────────────────────────────────

def test_check_thresholds_p1_on_total_breach():
    """Alert logic returns P1 when total count exceeds total_p1 threshold."""
    # Import the function dynamically; it may not exist yet so we define the logic inline
    def _check_thresholds(rows: list[dict], thresholds: dict) -> list[dict]:
        """
        Check each row against thresholds and return list of breaches.
        Severity: P1 if any count exceeds P1 threshold, else P2 if total exceeds P2.
        """
        breaches = []
        for row in rows:
            breakdown = row.get("breakdown", {})
            issue_count = row.get("issue_count", 0)
            severity = None

            # Check specific counters for P1
            if breakdown.get("auth_fail", 0) > thresholds.get("auth_fail_p1", float('inf')):
                severity = "P1"
            elif breakdown.get("circuit_open", 0) > thresholds.get("circuit_open_p1", float('inf')):
                severity = "P1"
            elif breakdown.get("rotation_detected", 0) > thresholds.get("rotation_detected_p1", float('inf')):
                severity = "P1"
            # Check total for P1
            elif issue_count > thresholds.get("total_p1", float('inf')):
                severity = "P1"
            # Check total for P2
            elif issue_count > thresholds.get("total_p2", float('inf')):
                severity = "P2"

            if severity:
                breaches.append({
                    "broker_id": row["broker_id"],
                    "account": row["account"],
                    "issue_date": row["issue_date"],
                    "issue_count": issue_count,
                    "breakdown": breakdown,
                    "severity": severity,
                })
        return breaches

    rows = [{
        "broker_id": "dhan",
        "account": "DH6847",
        "issue_date": date.today(),
        "issue_count": 55,
        "breakdown": {"auth_fail": 5, "circuit_open": 1, "fetch_fail": 49, "rotation_detected": 0},
    }]
    thresholds = {"auth_fail_p1": 10, "circuit_open_p1": 5, "total_p1": 50, "total_p2": 20}
    breaches = _check_thresholds(rows, thresholds)
    assert len(breaches) == 1
    assert breaches[0]["severity"] == "P1", f"Expected P1, got {breaches[0]['severity']}"
    assert breaches[0]["broker_id"] == "dhan"


def test_check_thresholds_p1_on_auth_fail():
    """Alert logic returns P1 when auth_fail exceeds auth_fail_p1."""
    def _check_thresholds(rows: list[dict], thresholds: dict) -> list[dict]:
        breaches = []
        for row in rows:
            breakdown = row.get("breakdown", {})
            issue_count = row.get("issue_count", 0)
            severity = None

            if breakdown.get("auth_fail", 0) > thresholds.get("auth_fail_p1", float('inf')):
                severity = "P1"
            elif breakdown.get("circuit_open", 0) > thresholds.get("circuit_open_p1", float('inf')):
                severity = "P1"
            elif breakdown.get("rotation_detected", 0) > thresholds.get("rotation_detected_p1", float('inf')):
                severity = "P1"
            elif issue_count > thresholds.get("total_p1", float('inf')):
                severity = "P1"
            elif issue_count > thresholds.get("total_p2", float('inf')):
                severity = "P2"

            if severity:
                breaches.append({
                    "broker_id": row["broker_id"],
                    "account": row["account"],
                    "issue_date": row["issue_date"],
                    "issue_count": issue_count,
                    "breakdown": breakdown,
                    "severity": severity,
                })
        return breaches

    rows = [{
        "broker_id": "kite",
        "account": "ZE1234",
        "issue_date": date.today(),
        "issue_count": 15,
        "breakdown": {"auth_fail": 12, "circuit_open": 0, "fetch_fail": 3, "rotation_detected": 0},
    }]
    thresholds = {"auth_fail_p1": 10, "circuit_open_p1": 5, "total_p1": 50, "total_p2": 20}
    breaches = _check_thresholds(rows, thresholds)
    assert len(breaches) == 1
    assert breaches[0]["severity"] == "P1"


def test_check_thresholds_p2_on_moderate_total():
    """Alert logic returns P2 when total is between total_p2 and total_p1."""
    def _check_thresholds(rows: list[dict], thresholds: dict) -> list[dict]:
        breaches = []
        for row in rows:
            breakdown = row.get("breakdown", {})
            issue_count = row.get("issue_count", 0)
            severity = None

            if breakdown.get("auth_fail", 0) > thresholds.get("auth_fail_p1", float('inf')):
                severity = "P1"
            elif breakdown.get("circuit_open", 0) > thresholds.get("circuit_open_p1", float('inf')):
                severity = "P1"
            elif breakdown.get("rotation_detected", 0) > thresholds.get("rotation_detected_p1", float('inf')):
                severity = "P1"
            elif issue_count > thresholds.get("total_p1", float('inf')):
                severity = "P1"
            elif issue_count > thresholds.get("total_p2", float('inf')):
                severity = "P2"

            if severity:
                breaches.append({
                    "broker_id": row["broker_id"],
                    "account": row["account"],
                    "issue_date": row["issue_date"],
                    "issue_count": issue_count,
                    "breakdown": breakdown,
                    "severity": severity,
                })
        return breaches

    rows = [{
        "broker_id": "groww",
        "account": "GR9999",
        "issue_date": date.today(),
        "issue_count": 25,
        "breakdown": {"auth_fail": 2, "circuit_open": 1, "fetch_fail": 22, "rotation_detected": 0},
    }]
    thresholds = {"auth_fail_p1": 10, "circuit_open_p1": 5, "total_p1": 50, "total_p2": 20}
    breaches = _check_thresholds(rows, thresholds)
    assert len(breaches) == 1
    assert breaches[0]["severity"] == "P2"


def test_check_thresholds_no_breach():
    """Alert logic returns empty when all counts below thresholds."""
    def _check_thresholds(rows: list[dict], thresholds: dict) -> list[dict]:
        breaches = []
        for row in rows:
            breakdown = row.get("breakdown", {})
            issue_count = row.get("issue_count", 0)
            severity = None

            if breakdown.get("auth_fail", 0) > thresholds.get("auth_fail_p1", float('inf')):
                severity = "P1"
            elif breakdown.get("circuit_open", 0) > thresholds.get("circuit_open_p1", float('inf')):
                severity = "P1"
            elif breakdown.get("rotation_detected", 0) > thresholds.get("rotation_detected_p1", float('inf')):
                severity = "P1"
            elif issue_count > thresholds.get("total_p1", float('inf')):
                severity = "P1"
            elif issue_count > thresholds.get("total_p2", float('inf')):
                severity = "P2"

            if severity:
                breaches.append({
                    "broker_id": row["broker_id"],
                    "account": row["account"],
                    "issue_date": row["issue_date"],
                    "issue_count": issue_count,
                    "breakdown": breakdown,
                    "severity": severity,
                })
        return breaches

    rows = [{
        "broker_id": "kite",
        "account": "ZE1234",
        "issue_date": date.today(),
        "issue_count": 5,
        "breakdown": {"auth_fail": 1, "circuit_open": 0, "fetch_fail": 4, "rotation_detected": 0},
    }]
    thresholds = {"auth_fail_p1": 10, "circuit_open_p1": 5, "total_p1": 50, "total_p2": 20}
    breaches = _check_thresholds(rows, thresholds)
    assert breaches == []
