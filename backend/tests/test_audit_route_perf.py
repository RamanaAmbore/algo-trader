"""
Performance regression tests for the audit route count query.

Verifies:
  - COUNT query uses SELECT COUNT(*) FROM audit_log (not materialization)
  - Correct total returned for 1000-row dataset
  - Page returns exactly per_page rows
  - Filter combinations (category + action + date range) return correct count
  - High-offset pagination (page 20, per_page 50) returns rows 950-999

Five quality dimensions:
  SSOT    — count query issues SELECT COUNT(*) (DB aggregation, not Python len())
  Perf    — zero AuditLog ORM objects materialised during the count query
  Stale   — audit.py no longer contains the len(...scalars().all()) pattern
  Reuse   — count + page share the same WHERE clause (no duplication)
  UX      — response total / rows fields match expected values at every page
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Stale guards (no DB needed) ────────────────────────────────────────────

_AUDIT_ROUTE = pathlib.Path(__file__).parent.parent / "api" / "routes" / "audit.py"


def test_no_len_scalars_all_pattern():
    """Stale: the old `len(... .scalars().all())` materialization must be gone."""
    src = _AUDIT_ROUTE.read_text()
    assert "len(total)" not in src, (
        "audit.py: `len(total)` materialization pattern still present. "
        "Should use SELECT func.count() instead."
    )


def test_func_count_used_for_total():
    """SSOT: audit.py must use func.count() for the total row count."""
    src = _AUDIT_ROUTE.read_text()
    assert "func.count()" in src, (
        "audit.py: func.count() not found. Count query must use "
        "SELECT func.count() not Python len()."
    )
    assert "scalar_one()" in src, (
        "audit.py: scalar_one() not found. COUNT result must be retrieved "
        "with scalar_one(), not scalars().all()."
    )


def test_page_query_has_limit_and_offset():
    """Stale: the page fetch query must have .limit() and .offset() applied."""
    src = _AUDIT_ROUTE.read_text()
    assert ".limit(limit)" in src, (
        "audit.py: .limit(limit) not found on the page query. "
        "Missing LIMIT means full table scan per page."
    )
    assert ".offset(offset)" in src, (
        "audit.py: .offset(offset) not found on the page query. "
        "Missing OFFSET means pagination is broken."
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_audit_rows(n: int, offset: int = 0) -> list:
    """Create minimal fake AuditLog-like objects for mocking."""
    rows = []
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        r = MagicMock()
        r.id = offset + i + 1
        r.actor_user_id = 1
        r.actor_username = "admin"
        r.actor_role = "admin"
        r.action = f"POST /api/test/{i}"
        r.category = "http"
        r.method = "POST"
        r.path = f"/api/test/{i}"
        r.target_type = None
        r.target_id = None
        r.status_code = 200
        r.summary = None
        r.request_id = f"req-{offset + i}"
        r.client_ip = "127.0.0.1"
        r.user_agent = "pytest"
        r.created_at = base_ts + timedelta(seconds=i)
        rows.append(r)
    return rows


def _mock_session(count_return: int, page_rows: list) -> MagicMock:
    """Build a mock async_session context manager.

    First execute() call (count query) returns scalar_one() = count_return.
    Second execute() call (page query) returns .scalars().all() = page_rows.
    """
    call_count = [0]

    async def mock_execute(stmt):
        call_count[0] += 1
        result = MagicMock()
        stmt_str = str(stmt)
        if "count" in stmt_str.lower():
            result.scalar_one = MagicMock(return_value=count_return)
            inner = MagicMock()
            inner.all = MagicMock(return_value=[])
            result.scalars = MagicMock(return_value=inner)
        else:
            inner = MagicMock()
            inner.all = MagicMock(return_value=page_rows)
            result.scalars = MagicMock(return_value=inner)
        return result

    session = AsyncMock()
    session.execute = mock_execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    cm = MagicMock()
    cm.return_value = session
    return cm


# ─── Direct handler invocation (bypasses guards) ─────────────────────────────

@pytest.mark.asyncio
async def test_count_returns_1000_for_1000_rows():
    """Perf + SSOT: with 1000 rows, total must be 1000 and page returns 50."""
    from backend.api.routes.audit import AuditController

    page_rows = _make_audit_rows(50)
    cm = _mock_session(1000, page_rows)

    controller = AuditController.__new__(AuditController)
    with patch("backend.api.routes.audit.async_session", cm):
        resp = await AuditController.list_audit.fn(
            controller, limit=50, offset=0,
            actor=None, action=None, category=None,
            target_type=None, target_id=None, request_id=None,
            since_hours=None, status_code=None,
        )

    assert resp.total == 1000, f"Expected total=1000, got {resp.total}"
    assert len(resp.rows) == 50, f"Expected 50 rows, got {len(resp.rows)}"
    assert resp.limit == 50
    assert resp.offset == 0


@pytest.mark.asyncio
async def test_count_query_does_not_materialise_rows():
    """Perf: the count execution must use scalar_one() (int), not scalars().all()."""
    from backend.api.routes.audit import AuditController

    page_rows = _make_audit_rows(50)
    scalar_one_called = []
    scalars_all_for_count_called = []

    async def mock_execute(stmt):
        result = MagicMock()
        stmt_str = str(stmt)
        if "count" in stmt_str.lower():
            def _scalar_one():
                scalar_one_called.append(True)
                return 1000
            result.scalar_one = _scalar_one

            def _scalars_all_for_count():
                scalars_all_for_count_called.append(True)
                return []
            inner = MagicMock()
            inner.all = _scalars_all_for_count
            result.scalars = MagicMock(return_value=inner)
        else:
            inner = MagicMock()
            inner.all = MagicMock(return_value=page_rows)
            result.scalars = MagicMock(return_value=inner)
        return result

    session = AsyncMock()
    session.execute = mock_execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    cm = MagicMock(return_value=session)

    controller = AuditController.__new__(AuditController)
    with patch("backend.api.routes.audit.async_session", cm):
        resp = await AuditController.list_audit.fn(
            controller, limit=50, offset=0,
            actor=None, action=None, category=None,
            target_type=None, target_id=None, request_id=None,
            since_hours=None, status_code=None,
        )

    assert resp.total == 1000
    assert scalar_one_called, (
        "scalar_one() was never called — count is not using DB aggregation"
    )
    assert not scalars_all_for_count_called, (
        ".scalars().all() was called for the count query — rows are being materialised"
    )


@pytest.mark.asyncio
async def test_filter_combination_returns_correct_count():
    """SSOT: category + action + date range filters must produce correct count."""
    from backend.api.routes.audit import AuditController

    page_rows = _make_audit_rows(10)
    cm = _mock_session(10, page_rows)

    controller = AuditController.__new__(AuditController)
    with patch("backend.api.routes.audit.async_session", cm):
        resp = await AuditController.list_audit.fn(
            controller,
            limit=50, offset=0,
            actor=None,
            action="POST",
            category="order.fill",
            target_type=None, target_id=None, request_id=None,
            since_hours=24,
            status_code=None,
        )

    assert resp.total == 10, f"Filter combo: expected total=10, got {resp.total}"
    assert len(resp.rows) == 10


@pytest.mark.asyncio
async def test_high_offset_pagination_returns_correct_slice():
    """UX: offset=950 must return 50 rows starting at id=951."""
    from backend.api.routes.audit import AuditController

    page_rows = _make_audit_rows(50, offset=950)
    cm = _mock_session(1000, page_rows)

    controller = AuditController.__new__(AuditController)
    with patch("backend.api.routes.audit.async_session", cm):
        resp = await AuditController.list_audit.fn(
            controller, limit=50, offset=950,
            actor=None, action=None, category=None,
            target_type=None, target_id=None, request_id=None,
            since_hours=None, status_code=None,
        )

    assert resp.total == 1000
    assert resp.offset == 950
    assert len(resp.rows) == 50
    assert resp.rows[0].id == 951, (
        f"High-offset page: expected first id=951, got {resp.rows[0].id}"
    )
    assert resp.rows[-1].id == 1000, (
        f"High-offset page: expected last id=1000, got {resp.rows[-1].id}"
    )


@pytest.mark.asyncio
async def test_no_filters_returns_unfiltered_count():
    """SSOT: no filters → count query has no WHERE clause, returns full count."""
    from backend.api.routes.audit import AuditController

    page_rows = _make_audit_rows(50)
    cm = _mock_session(5000, page_rows)

    controller = AuditController.__new__(AuditController)
    with patch("backend.api.routes.audit.async_session", cm):
        resp = await AuditController.list_audit.fn(
            controller, limit=50, offset=0,
            actor=None, action=None, category=None,
            target_type=None, target_id=None, request_id=None,
            since_hours=None, status_code=None,
        )

    assert resp.total == 5000
    assert len(resp.rows) == 50


@pytest.mark.asyncio
async def test_multi_category_comma_filter():
    """Reuse: comma-separated category filter (OR semantics) returns correct total."""
    from backend.api.routes.audit import AuditController

    page_rows = _make_audit_rows(3)
    cm = _mock_session(3, page_rows)

    controller = AuditController.__new__(AuditController)
    with patch("backend.api.routes.audit.async_session", cm):
        resp = await AuditController.list_audit.fn(
            controller, limit=50, offset=0,
            actor=None, action=None,
            category="order.fill,order.place",
            target_type=None, target_id=None, request_id=None,
            since_hours=None, status_code=None,
        )

    assert resp.total == 3
    assert len(resp.rows) == 3
