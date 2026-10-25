"""2026-10 notification-masking audit, Wave 2 cluster 3 — 4 MCP ping call
sites in backend/api/routes/lab.py built their Telegram ping body with the
raw, unmasked account code (`acct={acct}`): cancel-live, cancel-paper,
modify-paper, modify-live. Only the place-order ping
(`_res_place_telegram_ping`, covered by test_lab_mcp_ping_account_masking.py)
was already masked.

Each test captures the real `_mcp_ping` call (patched) and asserts the raw
account never appears in the rendered body, while the masked form (computed
at runtime via `mask_account`, not hardcoded — the module-global mask
registry's exact output for a given raw code depends on test execution
order) does.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.shared.helpers.utils import mask_account

RAW_ACCOUNT = "ZJ6294"


@pytest.mark.asyncio
async def test_cancel_live_ping_masks_account():
    from backend.api.routes import lab

    captured: list[str] = []
    fake_res = MagicMock(order_id="O123")
    audit_fn = AsyncMock()

    # `.fn` is a read-only property on the Litestar route-handler object,
    # so the whole `cancel_order` class attribute is swapped for a stub
    # that exposes the same `.fn` shape `lab.py` actually calls.
    fake_handler = MagicMock(fn=AsyncMock(return_value=fake_res))
    with patch.object(lab, "_mcp_ping", side_effect=lambda tg: captured.append(tg)), \
         patch.object(lab, "_audit_link_html", return_value="req-1"), \
         patch("backend.api.routes.orders.OrdersController.cancel_order", fake_handler):
        resp = await lab._res_cancel_live(
            oid="O123", acct=RAW_ACCOUNT,
            data=MagicMock(variety="regular"),
            request_id="req-1", user_id=7,
            audit_fn=audit_fn, request=MagicMock(),
        )

    assert resp.order_id == "O123"
    assert len(captured) == 1
    assert RAW_ACCOUNT not in captured[0]
    assert f"acct={mask_account(RAW_ACCOUNT)}" in captured[0]


@pytest.mark.asyncio
async def test_cancel_paper_ping_masks_account():
    from backend.api.routes import lab

    captured: list[str] = []
    mock_engine = MagicMock()
    mock_engine.cancel_paper_order = MagicMock(return_value=True)
    audit_fn = AsyncMock()

    with patch.object(lab, "_mcp_ping", side_effect=lambda tg: captured.append(tg)), \
         patch.object(lab, "_audit_link_html", return_value="req-2"), \
         patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_engine):
        resp = await lab._res_cancel_paper(
            oid="42", acct=RAW_ACCOUNT, request_id="req-2", user_id=7,
            audit_fn=audit_fn,
        )

    assert resp.order_id == "42"
    assert len(captured) == 1
    assert RAW_ACCOUNT not in captured[0]
    assert f"acct={mask_account(RAW_ACCOUNT)}" in captured[0]


@pytest.mark.asyncio
async def test_modify_paper_ping_masks_account():
    from backend.api.routes import lab

    captured: list[str] = []
    mock_engine = MagicMock()
    mock_engine.modify_paper_order = MagicMock(return_value=True)
    audit_fn = AsyncMock()
    data = MagicMock(price=100.0, trigger_price=None, order_type="LIMIT")

    with patch.object(lab, "_mcp_ping", side_effect=lambda tg: captured.append(tg)), \
         patch.object(lab, "_audit_link_html", return_value="req-3"), \
         patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_engine):
        resp = await lab._mcp_modify_paper(
            oid="42", qty=10, data=data, acct=RAW_ACCOUNT, chunks="qty=10",
            request_id="req-3", user_id=7, audit_fn=audit_fn,
        )

    assert resp.order_id == "42"
    assert len(captured) == 1
    assert RAW_ACCOUNT not in captured[0]
    assert f"acct={mask_account(RAW_ACCOUNT)}" in captured[0]


@pytest.mark.asyncio
async def test_modify_live_ping_masks_account():
    from backend.api.routes import lab

    captured: list[str] = []
    fake_res = MagicMock(order_id="O999")
    audit_fn = AsyncMock()
    data = MagicMock(
        price=100.0, trigger_price=None, order_type="LIMIT",
        validity="DAY", variety="regular",
    )

    fake_handler = MagicMock(fn=AsyncMock(return_value=fake_res))
    with patch.object(lab, "_mcp_ping", side_effect=lambda tg: captured.append(tg)), \
         patch.object(lab, "_audit_link_html", return_value="req-4"), \
         patch("backend.api.routes.orders.OrdersController.modify_order", fake_handler):
        resp = await lab._mcp_modify_live(
            oid="O999", qty=10, data=data, acct=RAW_ACCOUNT, chunks="qty=10",
            request_id="req-4", user_id=7, audit_fn=audit_fn, request=MagicMock(),
        )

    assert resp.order_id == "O999"
    assert len(captured) == 1
    assert RAW_ACCOUNT not in captured[0]
    assert f"acct={mask_account(RAW_ACCOUNT)}" in captured[0]
