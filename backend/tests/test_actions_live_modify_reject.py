"""
Regression test for the 2026-10 audit fix to `_al_modify_write_reject`
(backend/api/algo/actions_live.py).

Defect: a failed `modify_order` broker call used to blindly write
status="REJECTED" on the matching AlgoOrder row with NO check of the
row's current status — including rows already FILLED (a genuinely
completed order) or already REJECTED. Writing REJECTED (a FINAL status
per models.ALGO_ORDER_FINAL_STATUSES) on a still-live/OPEN row is also
dangerous: the postback final-status guard (orders_postback.py) then
permanently refuses that order's real later FILLED postback, stranding
a live fill with no take-profit arm and no ledger write.

Fix: never write status="REJECTED" here at all. Only annotate `detail`
for operator visibility, and skip rows already in a final status
entirely so a genuinely-terminal row is never touched. The previous
silent `except Exception: pass` is now a logged warning.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo.actions_live import _al_modify_write_reject


def _mock_session(rows):
    """Build an AsyncMock session whose execute().scalars().all() returns `rows`."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


class _FakeRow:
    def __init__(self, id_, status):
        self.id = id_
        self.status = status
        self.detail = None


@pytest.mark.asyncio
async def test_modify_failure_on_open_row_sets_detail_not_reject():
    """A still-open row's status must be left untouched; only `detail` is set."""
    row = _FakeRow(1, "OPEN")
    session = _mock_session([row])

    with patch("backend.api.database.async_session", return_value=session):
        await _al_modify_write_reject("251001000000001", RuntimeError("boom"))

    assert row.status == "OPEN", "status must never be flipped to REJECTED"
    assert row.detail is not None and "modify failed" in row.detail
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_modify_failure_on_already_filled_row_is_untouched():
    """A row already FILLED (final) must not be mutated at all — writing
    REJECTED here would block any later genuine late-fill reconciliation,
    and even touching `detail` on a terminal row's own record is skipped."""
    row = _FakeRow(2, "FILLED")
    session = _mock_session([row])

    with patch("backend.api.database.async_session", return_value=session):
        await _al_modify_write_reject("251001000000002", RuntimeError("boom"))

    assert row.status == "FILLED"
    assert row.detail is None
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_failure_on_already_rejected_row_is_untouched():
    row = _FakeRow(3, "REJECTED")
    session = _mock_session([row])

    with patch("backend.api.database.async_session", return_value=session):
        await _al_modify_write_reject("251001000000003", RuntimeError("boom"))

    assert row.status == "REJECTED"
    assert row.detail is None
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_failure_db_error_is_logged_not_raised():
    """A DB failure during the lookup/update must be swallowed (logged), not
    propagate — callers already re-raise the original broker exception."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(side_effect=RuntimeError("db down"))

    with patch("backend.api.database.async_session", return_value=session), \
         patch("backend.api.algo.actions_live.logger") as mock_logger:
        await _al_modify_write_reject("251001000000004", RuntimeError("boom"))

    mock_logger.warning.assert_called()


@pytest.mark.asyncio
async def test_modify_failure_never_sets_status_to_rejected_string_anywhere():
    """Belt-and-suspenders: scan the actual mutation — row.status is read-only
    in this function body (grep-style safety net against a future regression
    reintroducing a blind status="REJECTED" write)."""
    import ast
    import inspect
    src = inspect.getsource(_al_modify_write_reject)
    tree = ast.parse(src)
    assigns_to_status = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and target.attr == "status"
    ]
    assert not assigns_to_status, (
        "function must never assign row.status (not even to REJECTED) — "
        "only `detail` should be mutated on modify failure"
    )
