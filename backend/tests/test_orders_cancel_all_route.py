"""
Tests for the new `POST /api/orders/cancel-all` route
(`backend/api/routes/orders.py:OrdersController.cancel_all_orders`).

Shares its underlying implementation
(`backend.api.algo.actions_live.cancel_all_orders_scoped`) with the
`cancel_all_orders` agent DO-action -- same scope/account/symbol/side
narrowing, same `scope="account"` without `account` refusal. Uses the
SAME RBAC pattern as the existing admin `cancel_order` route (demo
refused, admin-only) -- never a weaker-auth, bare/unauthenticated
endpoint despite its destructive, fleet-wide blast radius.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from litestar.exceptions import HTTPException

pytestmark = pytest.mark.asyncio


def _fake_request(is_demo: bool = False):
    return SimpleNamespace(state=SimpleNamespace(is_demo=is_demo))


def _make_ctrl():
    from backend.api.routes.orders import OrdersController
    return OrdersController(owner=None)


async def test_cancel_all_orders_happy_path_calls_shared_helper():
    from backend.api.routes.orders import OrdersController
    from backend.api.schemas import CancelAllOrdersRequest

    ctrl = _make_ctrl()
    data = CancelAllOrdersRequest(
        scope="account", account="ZG0790", symbol="NIFTY", side="BUY",
    )

    with patch("backend.api.routes.orders.is_admin_request", return_value=True), \
         patch("backend.api.routes.orders.invalidate") as mock_invalidate, \
         patch(
             "backend.api.algo.actions_live.cancel_all_orders_scoped",
             new=AsyncMock(return_value=(3, 1)),
         ) as mock_helper:
        result = await OrdersController.cancel_all_orders.fn(
            ctrl, data=data, request=_fake_request(),
        )

    mock_helper.assert_awaited_once_with(
        scope="account", account="ZG0790", symbol="NIFTY", side="BUY",
    )
    assert result.cancelled == 3
    assert result.errors == 1
    assert result.scope == "account"
    assert result.account == "ZG0790"
    mock_invalidate.assert_called_once_with("orders")


async def test_cancel_all_orders_default_scope_is_total():
    from backend.api.routes.orders import OrdersController
    from backend.api.schemas import CancelAllOrdersRequest

    ctrl = _make_ctrl()
    data = CancelAllOrdersRequest()  # scope=None -> defaults to "total"

    with patch("backend.api.routes.orders.is_admin_request", return_value=True), \
         patch("backend.api.routes.orders.invalidate"), \
         patch(
             "backend.api.algo.actions_live.cancel_all_orders_scoped",
             new=AsyncMock(return_value=(0, 0)),
         ) as mock_helper:
        result = await OrdersController.cancel_all_orders.fn(
            ctrl, data=data, request=_fake_request(),
        )

    mock_helper.assert_awaited_once_with(
        scope="total", account=None, symbol=None, side=None,
    )
    assert result.scope == "total"


async def test_cancel_all_orders_scope_account_without_account_is_400_not_fleet_wide():
    """The refusal rule must reach the route (via the real, unmocked
    shared helper) as a 400 -- never silently widening to a fleet-wide
    cancel and never a 500."""
    from backend.api.routes.orders import OrdersController
    from backend.api.schemas import CancelAllOrdersRequest

    ctrl = _make_ctrl()
    data = CancelAllOrdersRequest(scope="account", account=None)

    with patch("backend.api.routes.orders.is_admin_request", return_value=True), \
         patch("backend.api.routes.orders.invalidate") as mock_invalidate:
        with pytest.raises(HTTPException) as exc_info:
            await OrdersController.cancel_all_orders.fn(
                ctrl, data=data, request=_fake_request(),
            )

    assert exc_info.value.status_code == 400
    mock_invalidate.assert_not_called()


async def test_cancel_all_orders_demo_refused():
    from backend.api.routes.orders import OrdersController
    from backend.api.schemas import CancelAllOrdersRequest

    ctrl = _make_ctrl()
    data = CancelAllOrdersRequest()

    with pytest.raises(HTTPException) as exc_info:
        await OrdersController.cancel_all_orders.fn(
            ctrl, data=data, request=_fake_request(is_demo=True),
        )
    assert exc_info.value.status_code == 403


async def test_cancel_all_orders_non_admin_refused():
    from backend.api.routes.orders import OrdersController
    from backend.api.schemas import CancelAllOrdersRequest

    ctrl = _make_ctrl()
    data = CancelAllOrdersRequest()

    with patch("backend.api.routes.orders.is_admin_request", return_value=False):
        with pytest.raises(HTTPException) as exc_info:
            await OrdersController.cancel_all_orders.fn(
                ctrl, data=data, request=_fake_request(),
            )
    assert exc_info.value.status_code == 403
