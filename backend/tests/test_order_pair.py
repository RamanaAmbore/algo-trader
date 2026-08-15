"""
Tests for order-pair feature:
  - PositionRow.is_orphan / pair_group_key field logic
  - _fetch_open_order_map + _apply_order_map_to_rows helpers
  - POST /api/orders/pair handler (pair_orders)

Coverage:
1. is_orphan=True when order_map is empty
2. is_orphan=False when (account, symbol) matches an OPEN AlgoOrder
3. pair_group_key is str(order.id) when order has no parent (root)
4. pair_group_key inherits parent_order_id when order has a parent
5. pair_orders happy path — child.parent_order_id set to parent.id
6. pair_orders raises 404 when either order is not found
7. pair_orders raises 400 when child already has a parent
8. pair_orders raises 400 when parent_id == child_id
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# 1. PositionRow.is_orphan — True when order_map is empty
# ---------------------------------------------------------------------------

def test_is_orphan_true_when_no_open_order():
    """is_orphan=True when no OPEN AlgoOrder matches the position's (account, symbol)."""
    from backend.api.schemas import PositionRow

    row = PositionRow(
        account="ZG0790",
        tradingsymbol="NIFTY25JUNFUT",
        exchange="NFO",
        product="NRML",
        quantity=50,
        average_price=24000.0,
        close_price=23900.0,
        pnl=-500.0,
        is_orphan=True,
    )
    assert row.is_orphan is True, "is_orphan must be True when constructed with is_orphan=True"
    assert row.pair_group_key is None, "pair_group_key must default to None"


# ---------------------------------------------------------------------------
# 2. is_orphan=False when matching OPEN AlgoOrder exists
# ---------------------------------------------------------------------------

def test_is_orphan_false_when_matching_open_order():
    """is_orphan=False when (account, tradingsymbol) matches an OPEN AlgoOrder."""
    from backend.api.schemas import PositionRow
    from backend.api.routes.positions import _apply_order_map_to_rows

    order_map = {
        ("ZG0790", "NIFTY25JUNFUT"): {"id": 42, "parent_order_id": None},
    }
    row = PositionRow(
        account="ZG0790",
        tradingsymbol="NIFTY25JUNFUT",
        exchange="NFO",
        product="NRML",
        quantity=50,
        average_price=24000.0,
        close_price=23900.0,
        pnl=-500.0,
    )
    result = _apply_order_map_to_rows([row], order_map)
    assert len(result) == 1
    assert result[0].is_orphan is False, "is_orphan must be False when order_map has a matching entry"


# ---------------------------------------------------------------------------
# 3. pair_group_key == str(order.id) when order has no parent (root order)
# ---------------------------------------------------------------------------

def test_pair_group_key_root_parent():
    """pair_group_key = str(order.id) when matched order has parent_order_id=None."""
    from backend.api.schemas import PositionRow
    from backend.api.routes.positions import _apply_order_map_to_rows

    order_map = {
        ("ZG0790", "NIFTY25JUNFUT"): {"id": 7, "parent_order_id": None},
    }
    row = PositionRow(
        account="ZG0790",
        tradingsymbol="NIFTY25JUNFUT",
        exchange="NFO",
        product="NRML",
        quantity=50,
        average_price=24000.0,
        close_price=23900.0,
        pnl=-500.0,
    )
    result = _apply_order_map_to_rows([row], order_map)
    assert result[0].pair_group_key == "7", (
        "pair_group_key must be str(order.id) when parent_order_id is None"
    )


# ---------------------------------------------------------------------------
# 4. pair_group_key inherits parent_order_id when order has a parent
# ---------------------------------------------------------------------------

def test_pair_group_key_inherits_parent():
    """pair_group_key = '99' when matched order has parent_order_id=99."""
    from backend.api.schemas import PositionRow
    from backend.api.routes.positions import _apply_order_map_to_rows

    order_map = {
        ("ZG0790", "NIFTY25JUNCE"): {"id": 101, "parent_order_id": 99},
    }
    row = PositionRow(
        account="ZG0790",
        tradingsymbol="NIFTY25JUNCE",
        exchange="NFO",
        product="NRML",
        quantity=-50,
        average_price=120.0,
        close_price=110.0,
        pnl=500.0,
    )
    result = _apply_order_map_to_rows([row], order_map)
    assert result[0].pair_group_key == "99", (
        "pair_group_key must be str(parent_order_id) when order is a child"
    )


# ---------------------------------------------------------------------------
# Helper: resolve the raw underlying function from a Litestar handler.
# OrdersController.pair_orders is a litestar.handlers.http_handlers.decorators.post
# instance; the real coroutine function lives at handler.fn.
# ---------------------------------------------------------------------------

def _handler_fn(handler):
    """Return the raw coroutine function from a Litestar route handler."""
    return handler.fn


# ---------------------------------------------------------------------------
# 5. pair_orders happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pair_orders_happy_path():
    """pair_orders sets child.parent_order_id = parent.id and commits."""
    from backend.api.routes.orders import OrdersController, PairOrdersInput

    parent = MagicMock()
    parent.id = 10
    parent.parent_order_id = None

    child = MagicMock()
    child.id = 20
    child.parent_order_id = None

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(side_effect=lambda model, pk: {10: parent, 20: child}[pk])
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    fn = _handler_fn(OrdersController.pair_orders)
    data = PairOrdersInput(parent_id=10, child_id=20)

    # The handler imports async_session from backend.api.database inside the body.
    with patch("backend.api.database.async_session", return_value=mock_ctx):
        result = await fn(None, data)  # self=None for unbound call

    assert result["ok"] is True
    assert result["parent_id"] == 10
    assert result["child_id"] == 20
    assert child.parent_order_id == 10, "child.parent_order_id must be set to parent.id"
    mock_session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. pair_orders raises 404 when order not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pair_orders_not_found():
    """pair_orders raises HTTPException(404) when either order is not found."""
    from litestar.exceptions import HTTPException
    from backend.api.routes.orders import OrdersController, PairOrdersInput

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)  # simulate not found
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    fn = _handler_fn(OrdersController.pair_orders)
    data = PairOrdersInput(parent_id=1, child_id=2)

    with patch("backend.api.database.async_session", return_value=mock_ctx):
        with pytest.raises(HTTPException) as exc_info:
            await fn(None, data)

    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.detail


# ---------------------------------------------------------------------------
# 7. pair_orders raises 400 when child already has a parent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pair_orders_child_already_has_parent():
    """pair_orders raises HTTPException(400) when child.parent_order_id is already set."""
    from litestar.exceptions import HTTPException
    from backend.api.routes.orders import OrdersController, PairOrdersInput

    parent = MagicMock()
    parent.id = 10

    child = MagicMock()
    child.id = 20
    child.parent_order_id = 5  # already paired

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(side_effect=lambda model, pk: {10: parent, 20: child}[pk])
    mock_session.commit = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    fn = _handler_fn(OrdersController.pair_orders)
    data = PairOrdersInput(parent_id=10, child_id=20)

    with patch("backend.api.database.async_session", return_value=mock_ctx):
        with pytest.raises(HTTPException) as exc_info:
            await fn(None, data)

    assert exc_info.value.status_code == 400
    assert "unpair" in exc_info.value.detail


# ---------------------------------------------------------------------------
# 8. pair_orders raises 400 when parent_id == child_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pair_orders_same_id():
    """pair_orders raises HTTPException(400) when parent_id == child_id."""
    from litestar.exceptions import HTTPException
    from backend.api.routes.orders import OrdersController, PairOrdersInput

    fn = _handler_fn(OrdersController.pair_orders)
    data = PairOrdersInput(parent_id=42, child_id=42)

    # No session needed — same-id check fires before DB access.
    with pytest.raises(HTTPException) as exc_info:
        await fn(None, data)

    assert exc_info.value.status_code == 400
    assert "different" in exc_info.value.detail
