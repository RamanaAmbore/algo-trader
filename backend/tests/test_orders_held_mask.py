"""
Fix 1 (orders-page P1 audit): `GET /api/orders/held` returned unmasked
account IDs to demo-guard sessions. `list_held` (orders_held.py) is
guarded by `auth_or_demo_guard` (admits anonymous demo visitors) but
had no masking branch, unlike every sibling list route in this
codebase (`/orders`, `/orders/drafts`, `/orders/chases/*`) which all
apply `do_mask = not is_admin_request(request)` before returning
account-identifying fields.

Covers:
  - A real anonymous request (no Authorization header) dispatched
    through the actual Litestar app/guard chain never sees a raw
    account code anywhere in the response, including inside the
    free-text `hold` (hold_json) field.
  - A real authenticated admin request sees the unmasked account.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_session(rows: list) -> AsyncMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result)
    return session


def _held_row(**extra) -> SimpleNamespace:
    base = dict(
        id=77, status="HELD",
        hold_json='{"category": "agent_order", "reason": "held for account ZG0790"}',
        account="ZG0790", symbol="NIFTY24APR25000CE", exchange="NFO",
        transaction_type="BUY", quantity=50, product="NRML",
    )
    base.update(extra)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_held_route_masks_account_for_anonymous_demo_request(async_client):
    """Real anonymous GET — auth_or_demo_guard admits it as a demo
    session. Must never leak the raw account code, including inside
    the free-text hold_json blob."""
    row = _held_row()
    with patch("backend.api.database.async_session", return_value=_mock_session([row])):
        resp = await async_client.get("/api/orders/held")

    assert resp.status_code == 200
    body = resp.json()
    assert "ZG0790" not in json.dumps(body), (
        "demo session must never see a raw account code anywhere in the "
        "/api/orders/held response"
    )
    held = body["held"]
    assert len(held) == 1
    assert held[0]["account"] != "ZG0790"
    assert held[0]["account"].endswith("####")


@pytest.mark.asyncio
async def test_held_route_shows_real_account_for_admin_request(async_client):
    """A real authenticated admin/designated request sees the raw
    account code, matching every sibling list route's convention."""
    row = _held_row()
    with patch("backend.api.database.async_session", return_value=_mock_session([row])), \
         patch("backend.api.routes.orders_held.is_admin_request", return_value=True):
        resp = await async_client.get(
            "/api/orders/held", headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code == 200
    held = resp.json()["held"]
    assert held[0]["account"] == "ZG0790"
    assert "ZG0790" in held[0]["hold"]


@pytest.mark.asyncio
async def test_list_held_fn_direct_masks_for_non_admin():
    """Direct unit-level check on the handler function itself (the
    established convention in this test file's siblings, e.g.
    test_order_hold_repeated_rejection_release.py), independent of the
    HTTP/guard layer."""
    from backend.api.routes.orders_held import HeldOrdersController

    row = _held_row()
    mock_request = MagicMock()
    ctrl = HeldOrdersController(owner=None)

    with patch("backend.api.database.async_session", return_value=_mock_session([row])), \
         patch("backend.api.routes.orders_held.is_admin_request", return_value=False):
        resp = await HeldOrdersController.list_held.fn(ctrl, request=mock_request)

    assert resp["held"][0]["account"] != "ZG0790"
    assert resp["held"][0]["account"].endswith("####")
