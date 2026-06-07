"""
Fix 1+2 — kill_chase paper path delegates to cancel_paper_order

Tests that:
1. kill_chase calls eng.cancel_paper_order(row.id) and does NOT mutate
   _open_orders directly.
2. Returns ok=True and err=None when cancel_paper_order returns True.
3. Returns ok=True and err set when cancel_paper_order returns False
   (engine not tracking).
4. Already-terminal rows return early without touching engine.
"""

import pytest
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, AsyncMock, patch


def _make_session_ctx(mock_row):
    """Return an async context-manager mock that yields a session yielding mock_row."""
    mock_s = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=mock_row)
    mock_s.execute = AsyncMock(return_value=mock_result)
    mock_s.commit = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield mock_s

    return _ctx, mock_s


@pytest.mark.asyncio
async def test_kill_chase_paper_calls_engine_cancel():
    """Paper kill delegates to cancel_paper_order and returns ok=True, err=None."""
    from litestar import Request
    from backend.api.routes.orders import OrdersController

    mock_row = MagicMock()
    mock_row.id = 42
    mock_row.mode = "paper"
    mock_row.status = "OPEN"
    mock_row.detail = "some detail"

    session_ctx, mock_s = _make_session_ctx(mock_row)

    mock_eng = MagicMock()
    mock_eng.cancel_paper_order = MagicMock(return_value=True)

    mock_request = MagicMock(spec=Request)

    with patch("backend.api.database.async_session", session_ctx), \
         patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_eng), \
         patch("backend.api.routes.orders.invalidate"), \
         patch("backend.api.algo.order_events.write_event", AsyncMock()):

        ctrl = OrdersController.__new__(OrdersController)
        result = await OrdersController.kill_chase.fn(ctrl, 42, mock_request)

    assert result["ok"] is True
    assert result.get("err") is None
    mock_eng.cancel_paper_order.assert_called_once_with(mock_row.id)


@pytest.mark.asyncio
async def test_kill_chase_paper_engine_not_tracking():
    """When cancel_paper_order returns False, err is set but result is still ok."""
    from litestar import Request
    from backend.api.routes.orders import OrdersController

    mock_row = MagicMock()
    mock_row.id = 99
    mock_row.mode = "paper"
    mock_row.status = "OPEN"
    mock_row.detail = ""

    session_ctx, _ = _make_session_ctx(mock_row)

    mock_eng = MagicMock()
    mock_eng.cancel_paper_order = MagicMock(return_value=False)

    mock_request = MagicMock(spec=Request)

    with patch("backend.api.database.async_session", session_ctx), \
         patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_eng), \
         patch("backend.api.routes.orders.invalidate"), \
         patch("backend.api.algo.order_events.write_event", AsyncMock()):

        ctrl = OrdersController.__new__(OrdersController)
        result = await OrdersController.kill_chase.fn(ctrl, 99, mock_request)

    assert result["ok"] is True
    assert result["err"] == "paper engine no longer tracking"
    mock_eng.cancel_paper_order.assert_called_once_with(mock_row.id)


@pytest.mark.asyncio
async def test_kill_chase_already_terminal():
    """Orders not in OPEN status return early without touching the engine."""
    from litestar import Request
    from backend.api.routes.orders import OrdersController

    mock_row = MagicMock()
    mock_row.id = 7
    mock_row.mode = "paper"
    mock_row.status = "FILLED"

    session_ctx, _ = _make_session_ctx(mock_row)

    mock_request = MagicMock(spec=Request)

    mock_eng = MagicMock()
    mock_eng.cancel_paper_order = MagicMock()

    with patch("backend.api.database.async_session", session_ctx), \
         patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_eng):

        ctrl = OrdersController.__new__(OrdersController)
        result = await OrdersController.kill_chase.fn(ctrl, 7, mock_request)

    assert result["already_terminal"] is True
    assert result["status"] == "FILLED"
    mock_eng.cancel_paper_order.assert_not_called()
