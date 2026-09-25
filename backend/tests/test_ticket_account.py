"""
FIX 2 — Ticket account required

Tests for /api/orders/ticket endpoint account validation.

The ticket endpoint requires an explicit account field and validates
it against loaded Connections before proceeding. These tests skip the
auth_or_demo_guard by patching is_admin_request to return True so the
request flows through as admin (otherwise on a `deploy_branch=main`
test environment the guard tags the unauthenticated request as DEMO
and the ticket route 403s with "Demo mode" before the account check
runs).
"""

import pytest
from unittest.mock import patch, AsyncMock


def _admin_auth_patches():
    """Combined patch context: mark every request as authenticated so
    the `auth_or_demo_guard` doesn't tag the test request as demo on
    prod-branch test envs. Returns a context manager.

    Post-fix `auth_or_demo_guard` uses `is_authenticated_request`
    (any valid JWT — designated/admin/trader/risk/partner) instead of
    the previous `is_admin_request` (admin-tier only). Pre-fix
    rejected trader+risk employees on prod as demo sessions."""
    return patch.multiple(
        "backend.api.auth_guard",
        is_authenticated_request=lambda _conn: True,
        is_admin_request=lambda _conn: True,
        jwt_guard=AsyncMock(return_value=None),
    )


@pytest.mark.asyncio
async def test_ticket_missing_account(async_client, stub_connections):
    """
    POST /api/orders/ticket with empty account field returns 400
    with "Account is required."

    D6 fix (2026-09) — `lot_size_hint` is now required for this test to
    reach the account-validation branch at all: `_ticket_validate_input`
    (which resolves F&O lot_size BEFORE account validation runs) 503s on
    a genuine instruments-cache miss instead of the pre-D6 silent
    fallback of lot_size=1. This test environment has no warmed
    instruments cache, so without the hint every NFO ticket would 503
    on the lot-size guard before ever reaching the account check this
    test is actually exercising.
    """
    payload = {
        "mode": "paper",
        "side": "BUY",
        "tradingsymbol": "NIFTY25APRFUT",
        "quantity": 1,
        "price": 22500.0,
        "account": "",  # Empty
        "lot_size_hint": 50,
    }

    with _admin_auth_patches():
        response = await async_client.post("/api/orders/ticket", json=payload)

    assert response.status_code == 400
    data = response.json()
    assert "Account is required" in data.get("detail", "")


@pytest.mark.asyncio
async def test_ticket_unknown_account(async_client, stub_connections):
    """
    POST /api/orders/ticket with an account not in Connections()
    returns 400 with "Unknown account: <name>."

    D6 fix (2026-09) — see `test_ticket_missing_account`'s docstring:
    `lot_size_hint` is required so the lot-size resolution (which runs
    before account validation) doesn't 503 first in this test's
    cold-cache environment.
    """
    payload = {
        "mode": "paper",
        "side": "BUY",
        "tradingsymbol": "NIFTY25APRFUT",
        "quantity": 1,
        "price": 22500.0,
        "account": "UNKNOWN_ACCT",
        "lot_size_hint": 50,
    }

    with _admin_auth_patches():
        response = await async_client.post("/api/orders/ticket", json=payload)

    assert response.status_code == 400
    data = response.json()
    assert "Unknown account" in data.get("detail", "")
    assert "UNKNOWN_ACCT" in data.get("detail", "")


@pytest.mark.skip(reason="Requires proper async_session mocking; integration test only")
@pytest.mark.asyncio
async def test_ticket_valid_account_paper(async_client, stub_connections, reset_singletons):
    """
    POST /api/orders/ticket with a valid account in Connections()
    proceeds past the account validation (response may be other errors,
    but not account validation error).

    Uses paper mode to avoid live-mode gating.
    """
    from unittest.mock import patch, AsyncMock
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder

    payload = {
        "mode": "paper",
        "side": "BUY",
        "tradingsymbol": "NIFTY25APRFUT",
        "quantity": 1,
        "price": 22500.0,
        "order_type": "LIMIT",
        "account": "ZG0790",
    }

    # Mock the database session to avoid real DB writes
    with patch("backend.api.routes.orders.async_session") as mock_session:
        mock_async_ctx = AsyncMock()
        mock_async_ctx.__aenter__ = AsyncMock(return_value=mock_async_ctx)
        mock_async_ctx.__aexit__ = AsyncMock(return_value=None)

        # Mock the row commitment
        mock_async_ctx.add = MagicMock()
        mock_async_ctx.commit = AsyncMock()

        # Create a mock AlgoOrder row with an id
        mock_row = MagicMock()
        mock_row.id = 42

        # Patch AlgoOrder constructor to return our mock
        with patch("backend.api.routes.orders.AlgoOrder", return_value=mock_row):
            mock_session.return_value = mock_async_ctx

            response = await async_client.post("/api/orders/ticket", json=payload)

    # The request should NOT return a 400 about account validation
    # (it may return other errors, but the account check passed)
    assert response.status_code != 400 or "account" not in response.json().get("detail", "").lower()
    # If we did get past validation, we'd get a 200 response
    if response.status_code == 200:
        data = response.json()
        assert "order_id" in data or "mode" in data
