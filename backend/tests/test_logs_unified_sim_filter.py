"""Sprint 2a fix — `sim_mode` query filter now also applies to order
events, and `UnifiedLogRow.sim_mode` correctly reflects the underlying
AlgoOrder.mode instead of a hard-coded False.

Covers the full `LogsController.unified_log` controller method (not just
the pure helpers in test_logs_unified_helpers.py), since the bug required
both `_build_order_row` (per-row flag) AND `_fetch_order_events` (query
filter) to change together — a per-row-only fix would have left
`?sim_mode=false` leaking simulator rows through, and `?sim_mode=true`
excluding them, exactly backwards.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.routes.logs import LogsController


def _oe(id_, order_id, kind="fill", ts=None):
    return SimpleNamespace(
        id=id_, ts=ts or datetime(2026, 7, 4, 12, tzinfo=timezone.utc),
        kind=kind, message="m", order_id=order_id, payload_json=None,
    )


def _make_session(order_rows, agent_rows):
    """async_session() context manager whose .execute() returns order
    rows on the first call, agent rows on the second — mirroring
    unified_log's two sequential queries on the same session."""
    mock_session = AsyncMock()
    order_result = MagicMock()
    order_result.all = MagicMock(return_value=order_rows)
    agent_result = MagicMock()
    agent_result.all = MagicMock(return_value=agent_rows)
    mock_session.execute = AsyncMock(side_effect=[order_result, agent_result])
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=mock_session)


def _mock_request():
    req = MagicMock()
    req.headers = {}
    return req


@pytest.mark.asyncio
async def test_unified_log_sim_mode_flag_reflects_algo_order_mode():
    """No sim_mode filter applied — both a 'sim' row and a 'live' row
    come back, each correctly tagged."""
    order_rows = [
        (_oe(1, 10), "ZG0790", "sim"),
        (_oe(2, 11), "ZG0790", "live"),
    ]
    ctrl = LogsController.__new__(LogsController)

    with patch("backend.api.routes.logs.async_session", _make_session(order_rows, [])), \
         patch("backend.api.routes.logs.is_admin_request", return_value=True):
        rows = await LogsController.unified_log.fn(
            ctrl, _mock_request(), limit=50, kinds="", accounts="",
            since="", sim_mode="",
        )

    by_id = {r.id: r for r in rows}
    assert by_id[1].sim_mode is True
    assert by_id[2].sim_mode is False


@pytest.mark.asyncio
async def test_unified_log_sim_mode_false_excludes_sim_order_rows():
    """?sim_mode=false ('real only') must exclude the simulator order
    row at the query level — pre-fix this param was a no-op for order
    rows (every row read sim_mode=False already)."""
    order_rows = [(_oe(2, 11), "ZG0790", "live")]
    ctrl = LogsController.__new__(LogsController)

    with patch("backend.api.routes.logs.async_session", _make_session(order_rows, [])) as mock_sess, \
         patch("backend.api.routes.logs.is_admin_request", return_value=True):
        rows = await LogsController.unified_log.fn(
            ctrl, _mock_request(), limit=50, kinds="", accounts="",
            since="", sim_mode="false",
        )

    assert all(r.sim_mode is False for r in rows)
    # Confirm the mode!='sim' predicate actually reached the SQL query,
    # not just filtered client-side.
    first_call_query = mock_sess.return_value.execute.call_args_list[0].args[0]
    assert "mode" in str(first_call_query).lower()


@pytest.mark.asyncio
async def test_unified_log_sim_mode_true_only_returns_sim_order_rows():
    order_rows = [(_oe(1, 10), "ZG0790", "sim")]
    ctrl = LogsController.__new__(LogsController)

    with patch("backend.api.routes.logs.async_session", _make_session(order_rows, [])), \
         patch("backend.api.routes.logs.is_admin_request", return_value=True):
        rows = await LogsController.unified_log.fn(
            ctrl, _mock_request(), limit=50, kinds="", accounts="",
            since="", sim_mode="true",
        )

    assert len(rows) == 1
    assert rows[0].sim_mode is True


@pytest.mark.asyncio
async def test_unified_log_paper_order_row_is_not_sim_mode():
    """Paper orders write algo_order_events too (the original bug this
    fix addresses) but per the UnifiedLogRow docstring, sim_mode is
    reserved for genuine simulator runs — paper is 'real' (live+paper)."""
    order_rows = [(_oe(3, 12), "ZG0790", "paper")]
    ctrl = LogsController.__new__(LogsController)

    with patch("backend.api.routes.logs.async_session", _make_session(order_rows, [])), \
         patch("backend.api.routes.logs.is_admin_request", return_value=True):
        rows = await LogsController.unified_log.fn(
            ctrl, _mock_request(), limit=50, kinds="", accounts="",
            since="", sim_mode="",
        )

    assert rows[0].sim_mode is False
