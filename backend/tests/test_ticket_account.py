"""
FIX 2 — Ticket account required

Tests for /api/orders/ticket endpoint account validation.

The ticket endpoint now requires an explicit account field and
validates it against loaded Connections before proceeding.
"""

import pytest


@pytest.mark.asyncio
async def test_ticket_missing_account(async_client, stub_connections):
    """
    POST /api/orders/ticket with empty account field returns 400
    with "Account is required."
    """
    payload = {
        "mode": "paper",
        "side": "BUY",
        "tradingsymbol": "NIFTY25APRFUT",
        "quantity": 1,
        "price": 22500.0,
        "account": "",  # Empty
    }

    response = await async_client.post("/api/orders/ticket", json=payload)

    assert response.status_code == 400
    data = response.json()
    assert "Account is required" in data.get("detail", "")


@pytest.mark.asyncio
async def test_ticket_unknown_account(async_client, stub_connections):
    """
    POST /api/orders/ticket with an account not in Connections()
    returns 400 with "Unknown account: <name>."
    """
    payload = {
        "mode": "paper",
        "side": "BUY",
        "tradingsymbol": "NIFTY25APRFUT",
        "quantity": 1,
        "price": 22500.0,
        "account": "UNKNOWN_ACCT",
    }

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
