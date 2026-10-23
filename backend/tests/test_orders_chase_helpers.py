"""Unit tests for the chase-reconcile helpers behind `list_active_chases`
in backend/api/routes/orders.py.

2026-10-23 (order-poll consolidation) — `_chase_process_live_row` (and
its `_CHASE_KITE_TO_ALGO`/`_CHASE_LIVE_TERMINAL` constants) is DELETED.
The "has a broker_order_id" branch it used to own is now
`_rco_reconcile_live_row`, a thin wrapper around the canonical
single-order-view accessor (`orders_helpers.py:get_broker_order_truth` /
`apply_order_truth`) — the Kite-status-mapping matrix those two functions
cover is already exhaustively tested in `test_order_truth_accessor.py`;
this file tests the WRAPPER's own branching (available/terminal/withheld
→ dropped/filled), not a re-derivation of that matrix. `_chase_process_
paper_row` is UNCHANGED — its own tests below are untouched.

These lock in the branch matrix so the surrounding controller can
keep its cc score under budget without silently re-introducing the
paper/live/no-id edge cases the pre-refactor loop handled inline.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.routes.orders import (
    _chase_process_paper_row,
    _rco_reconcile_live_row,
    _rco_reconcile_no_id_row,
    _rco_reconcile_paper_row,
    _single_row_locked_update,
)
from backend.api.routes.orders_helpers import OrderTruth


def _row(**kw):
    """Minimal AlgoOrder stand-in. status/detail default to sensible values."""
    kw.setdefault("status", "OPEN")
    kw.setdefault("detail", "")
    kw.setdefault("quantity", 100)
    kw.setdefault("filled_quantity", 0)
    kw.setdefault("broker_order_id", "")
    return SimpleNamespace(**kw)


def _mock_session(row):
    """Mirrors test_order_truth_accessor.py's helper — a fake async
    session whose `.execute()` always returns `row` from both
    `.scalar_one_or_none()` and `.scalars().all()`/`.first()`, so it
    works against both `apply_order_truth`'s and `_single_row_locked_
    update`'s differing result-unwrap calls."""
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row
    _result.scalars.return_value.all.return_value = [row] if row is not None else []
    _result.scalars.return_value.first.return_value = row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


# ── paper branch (pure function, unchanged) ─────────────────────────────
class TestPaperBranch:
    def test_still_open_in_engine_keeps_row(self):
        r = _row(id=1)
        drop, delta = _chase_process_paper_row(r, {1, 2})
        assert drop is False
        assert delta == 0
        assert r.status == "OPEN"

    def test_missing_from_engine_open_row_flips_and_counts(self):
        r = _row(id=1)
        drop, delta = _chase_process_paper_row(r, {2, 3})
        assert drop is True
        assert delta == 1
        assert r.status == "UNFILLED"
        assert "paper engine no longer tracking" in r.detail

    def test_missing_from_engine_already_terminal_drops_but_no_count(self):
        """Race: concurrent step() may have already flipped the row.
        We still drop it from the response but must NOT double-count."""
        r = _row(id=1, status="FILLED")
        drop, delta = _chase_process_paper_row(r, {2})
        assert drop is True
        assert delta == 0
        assert r.status == "FILLED"

    def test_detail_truncated_to_200_char_prefix(self):
        r = _row(id=1, detail="x" * 300)
        _chase_process_paper_row(r, set())
        # existing 200 char prefix + suffix
        assert r.detail.startswith("x" * 200)
        assert r.detail.endswith("paper engine no longer tracking")


# ── _rco_reconcile_paper_row (async wrapper around the pure function) ──
@pytest.mark.asyncio
class TestReconcilePaperRowWrapper:
    async def test_none_paper_open_ids_never_drops(self):
        r = _row(id=1)
        assert await _rco_reconcile_paper_row(r, None) is False

    async def test_still_tracked_not_dropped_no_lock_taken(self):
        r = _row(id=1)
        with patch("backend.api.database.async_session") as mock_session_cls:
            dropped = await _rco_reconcile_paper_row(r, {1})
        assert dropped is False
        mock_session_cls.assert_not_called()

    async def test_already_terminal_dropped_without_a_write(self):
        r = _row(id=1, status="FILLED")
        with patch("backend.api.database.async_session") as mock_session_cls:
            dropped = await _rco_reconcile_paper_row(r, {2})
        assert dropped is True
        mock_session_cls.assert_not_called()

    async def test_untracked_open_row_locks_and_writes_unfilled(self):
        r = _row(id=1, status="OPEN")
        mock_session = _mock_session(r)
        with patch("backend.api.database.async_session", return_value=mock_session):
            dropped = await _rco_reconcile_paper_row(r, {2})
        assert dropped is True
        assert r.status == "UNFILLED"
        mock_session.commit.assert_awaited_once()


# ── _rco_reconcile_no_id_row — placement never returned a broker id ────
@pytest.mark.asyncio
class TestReconcileNoIdRow:
    async def test_withheld_when_chase_active_keeps_row(self):
        r = _row(id=1, broker_order_id="")
        with patch(
            "backend.api.routes.orders_postback._pb_should_withhold_status",
            return_value=True,
        ), patch("backend.api.database.async_session") as mock_session_cls:
            dropped = await _rco_reconcile_no_id_row(r)
        assert dropped is False
        mock_session_cls.assert_not_called()

    async def test_not_withheld_locks_and_marks_rejected(self):
        r = _row(id=1, broker_order_id="", status="OPEN")
        mock_session = _mock_session(r)
        with patch(
            "backend.api.routes.orders_postback._pb_should_withhold_status",
            return_value=False,
        ), patch("backend.api.database.async_session", return_value=mock_session):
            dropped = await _rco_reconcile_no_id_row(r)
        assert dropped is True
        assert r.status == "REJECTED"
        assert "never returned broker_order_id" in r.detail
        mock_session.commit.assert_awaited_once()


# ── _rco_reconcile_live_row — thin wrapper over the canonical accessor ──
@pytest.mark.asyncio
class TestReconcileLiveRowWrapper:
    async def test_unavailable_truth_keeps_row(self):
        r = _row(id=1, broker_order_id="B1")
        truth = OrderTruth(available=False)
        with patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
        ), patch(
            "backend.api.routes.orders_helpers.apply_order_truth",
            new=AsyncMock(),
        ) as mock_apply:
            dropped, filled = await _rco_reconcile_live_row(r, {})
        assert dropped is False
        assert filled is False
        mock_apply.assert_not_awaited()

    async def test_not_yet_terminal_keeps_row(self):
        r = _row(id=1, broker_order_id="B1")
        truth = OrderTruth(available=True, algo_status=None, raw_broker_status="OPEN")
        with patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
        ), patch(
            "backend.api.routes.orders_helpers.apply_order_truth",
            new=AsyncMock(),
        ) as mock_apply:
            dropped, filled = await _rco_reconcile_live_row(r, {})
        assert dropped is False
        assert filled is False
        mock_apply.assert_not_awaited()

    async def test_withheld_or_raced_keeps_row(self):
        """apply_order_truth returning False (withheld mid-chase-retry,
        or lost a race to another writer) must not drop the row."""
        r = _row(id=1, broker_order_id="B1")
        truth = OrderTruth(available=True, algo_status="CANCELLED", raw_broker_status="CANCELLED")
        with patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
        ), patch(
            "backend.api.routes.orders_helpers.apply_order_truth",
            new=AsyncMock(return_value=False),
        ) as mock_apply:
            dropped, filled = await _rco_reconcile_live_row(r, {})
        assert dropped is False
        assert filled is False
        # withhold_if_active=True must always be threaded through.
        _, kwargs = mock_apply.call_args
        assert kwargs.get("withhold_if_active") is True

    async def test_confirmed_fill_drops_and_flags_filled(self):
        r = _row(id=1, broker_order_id="B1")
        truth = OrderTruth(available=True, algo_status="FILLED",
                           average_price=101.5, raw_broker_status="COMPLETE")
        with patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
        ), patch(
            "backend.api.routes.orders_helpers.apply_order_truth",
            new=AsyncMock(return_value=True),
        ):
            dropped, filled = await _rco_reconcile_live_row(r, {})
        assert dropped is True
        assert filled is True

    async def test_confirmed_cancel_drops_without_filled_flag(self):
        r = _row(id=1, broker_order_id="B1")
        truth = OrderTruth(available=True, algo_status="CANCELLED", raw_broker_status="CANCELLED")
        with patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
        ), patch(
            "backend.api.routes.orders_helpers.apply_order_truth",
            new=AsyncMock(return_value=True),
        ):
            dropped, filled = await _rco_reconcile_live_row(r, {})
        assert dropped is True
        assert filled is False


# ── _single_row_locked_update — shared lock-only-when-needed helper ────
@pytest.mark.asyncio
class TestSingleRowLockedUpdate:
    async def test_row_not_found_no_write(self):
        mock_session = _mock_session(None)
        with patch("backend.api.database.async_session", return_value=mock_session):
            changed = await _single_row_locked_update(1, {"OPEN"}, MagicMock())
        assert changed is False
        mock_session.commit.assert_not_awaited()

    async def test_status_raced_out_of_allowed_set_no_write(self):
        r = _row(id=1, status="FILLED")
        mock_session = _mock_session(r)
        with patch("backend.api.database.async_session", return_value=mock_session):
            changed = await _single_row_locked_update(1, {"OPEN"}, MagicMock())
        assert changed is False
        mock_session.commit.assert_not_awaited()

    async def test_matching_status_applies_mutator_and_commits(self):
        r = _row(id=1, status="OPEN")
        mock_session = _mock_session(r)
        mutator = MagicMock()
        with patch("backend.api.database.async_session", return_value=mock_session):
            changed = await _single_row_locked_update(1, {"OPEN"}, mutator)
        assert changed is True
        mutator.assert_called_once_with(r)
        mock_session.commit.assert_awaited_once()
