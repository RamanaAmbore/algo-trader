"""
Tests for the admin manual modify-order route's MCX quantity translation
fix (2026-10).

`OrdersController.modify_order` (backend/api/routes/orders.py) used to send
`data.quantity` straight to `broker.modify_order()` unconverted, with a
comment claiming this was deliberate. That is the same "qty vs lot_size"
trap the agent `modify_order` action path already closed via
`actions_live.py`'s `_al_modify_resolve_qty` (G1 lot-multiple check +
`broker.translate_qty`). This route now resolves the order's own
exchange/symbol (via `_al_modify_fetch_order_meta`) and reuses that exact
same helper instead of duplicating the logic.

Covers:
  1. A quantity-bearing modify resolves exchange/symbol and calls
     `broker.translate_qty` before `broker.modify_order` — the translated
     value (not the raw one) reaches the broker.
  2. A price/trigger-only modify (no quantity) never touches qty
     resolution at all — unaffected by this fix.
  3. A resolution failure (e.g. G1 lot-multiple violation) fails closed:
     the route returns 400 and `broker.modify_order` is never called.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar.exceptions import HTTPException

pytestmark = pytest.mark.asyncio


def _fake_request():
    return SimpleNamespace(state=SimpleNamespace(is_demo=False))


def _make_ctrl():
    from backend.api.routes.orders import OrdersController
    return OrdersController(owner=None)


async def test_modify_order_with_quantity_resolves_and_translates_before_broker_call():
    from backend.api.routes.orders import OrdersController
    from backend.api.schemas import ModifyOrderRequest

    broker = MagicMock()
    broker.modify_order = MagicMock()

    data = ModifyOrderRequest(account="ZG0790", quantity=2, price=None)
    ctrl = _make_ctrl()

    with patch("backend.api.routes.orders.is_admin_request", return_value=True), \
         patch("backend.api.routes.orders._broker_for", return_value=broker), \
         patch("backend.api.routes.orders.invalidate"), \
         patch(
             "backend.api.algo.actions_live._al_modify_fetch_order_meta",
             new=AsyncMock(return_value=("MCX", "CRUDEOIL26OCTFUT")),
         ) as mock_meta, \
         patch(
             "backend.api.algo.actions_live._al_modify_resolve_qty",
             new=AsyncMock(return_value=200),
         ) as mock_resolve:
        result = await OrdersController.modify_order.fn(
            ctrl, order_id="ORD123", data=data, request=_fake_request(),
        )

    mock_meta.assert_awaited_once_with("ORD123")
    mock_resolve.assert_awaited_once_with(broker, 2, "MCX", "CRUDEOIL26OCTFUT")
    broker.modify_order.assert_called_once_with(
        "ORD123", variety="regular", quantity=200,
    )
    assert result.order_id == "ORD123"


async def test_modify_order_without_quantity_skips_resolution_entirely():
    from backend.api.routes.orders import OrdersController
    from backend.api.schemas import ModifyOrderRequest

    broker = MagicMock()
    broker.modify_order = MagicMock()

    data = ModifyOrderRequest(account="ZG0790", quantity=None, price=123.45)
    ctrl = _make_ctrl()

    with patch("backend.api.routes.orders.is_admin_request", return_value=True), \
         patch("backend.api.routes.orders._broker_for", return_value=broker), \
         patch("backend.api.routes.orders.invalidate"), \
         patch(
             "backend.api.algo.actions_live._al_modify_fetch_order_meta",
             new=AsyncMock(),
         ) as mock_meta, \
         patch(
             "backend.api.algo.actions_live._al_modify_resolve_qty",
             new=AsyncMock(),
         ) as mock_resolve:
        result = await OrdersController.modify_order.fn(
            ctrl, order_id="ORD456", data=data, request=_fake_request(),
        )

    mock_meta.assert_not_awaited()
    mock_resolve.assert_not_awaited()
    broker.modify_order.assert_called_once_with(
        "ORD456", variety="regular", price=123.45,
    )
    assert result.order_id == "ORD456"


async def test_modify_order_qty_resolution_failure_fails_closed_never_calls_broker():
    from backend.api.routes.orders import OrdersController
    from backend.api.schemas import ModifyOrderRequest

    broker = MagicMock()
    broker.modify_order = MagicMock()

    data = ModifyOrderRequest(account="ZG0790", quantity=37)
    ctrl = _make_ctrl()

    with patch("backend.api.routes.orders.is_admin_request", return_value=True), \
         patch("backend.api.routes.orders._broker_for", return_value=broker), \
         patch(
             "backend.api.algo.actions_live._al_modify_fetch_order_meta",
             new=AsyncMock(return_value=("MCX", "CRUDEOIL26OCTFUT")),
         ), \
         patch(
             "backend.api.algo.actions_live._al_modify_resolve_qty",
             new=AsyncMock(side_effect=ValueError(
                 "modify_order: G1 lot-multiple violation"
             )),
         ):
        with pytest.raises(HTTPException) as exc_info:
            await OrdersController.modify_order.fn(
                ctrl, order_id="ORD789", data=data, request=_fake_request(),
            )

    assert exc_info.value.status_code == 400
    broker.modify_order.assert_not_called()
