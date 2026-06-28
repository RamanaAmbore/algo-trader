"""
FIX 1 — Postback HMAC verification

Tests for /api/orders/postback endpoint HMAC signature validation
(Kite postback verification over order_id + order_timestamp + api_secret).

The postback endpoint:
  - Validates HMAC-SHA256 signature before processing
  - Returns 401 with "Invalid postback signature." on bad signature
  - Broadcasts WS message only on valid signature
"""

import hashlib
import hmac
import json
import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.asyncio
async def test_postback_valid_hmac(async_client, stub_connections):
    """
    POST /api/orders/postback with valid HMAC signature is accepted.

    Computes the expected checksum and verifies it passes through.
    """
    order_id = "12345"
    order_timestamp = "2026-05-04 10:30:00"
    api_secret = "test_secret_123"

    # Compute plain SHA-256: sha256(order_id + order_timestamp + api_secret)
    msg = (str(order_id) + str(order_timestamp) + api_secret).encode()
    checksum = hashlib.sha256(msg).hexdigest()

    payload = {
        "order_id": order_id,
        "order_timestamp": order_timestamp,
        "checksum": checksum,
        "user_id": "ZG0790",
        "status": "COMPLETE",
        "tradingsymbol": "NIFTY25APRFUT",
        "transaction_type": "BUY",
        "quantity": 1,
        "average_price": 22500.0,
        "price": 22500.0,
        "status_message": "Order filled",
    }

    # Mock broadcast to verify it was called (good path only)
    with patch("backend.api.routes.orders.broadcast") as mock_broadcast:
        response = await async_client.post("/api/orders/postback", json=payload)

    # POST handler defaults to 201 Created
    assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
    data = response.json()
    assert data.get("status") == "ok"
    # The handler broadcasts three events on a COMPLETE order:
    # `order_update` (status change), `position_filled` (qty>0 fill
    # signal), and `book_changed` (terminal-state bus event added in
    # the postback fan-out work — wakes Pulse/Performance/Derivatives
    # auto-refresh). qty=1 triggers position_filled.
    import json as _json
    assert mock_broadcast.call_count == 3
    call_events = [
        _json.loads(c.args[0]).get("event")
        for c in mock_broadcast.call_args_list
    ]
    assert "order_update" in call_events
    assert "position_filled" in call_events
    assert "book_changed" in call_events


@pytest.mark.asyncio
async def test_postback_invalid_hmac(async_client, stub_connections):
    """
    POST /api/orders/postback with invalid HMAC checksum returns 401.

    Verifies that a bad signature is rejected before broadcast.
    """
    payload = {
        "order_id": "12345",
        "order_timestamp": "2026-05-04 10:30:00",
        "checksum": "badbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadb",
        "user_id": "ZG0790",
        "status": "COMPLETE",
        "tradingsymbol": "NIFTY25APRFUT",
        "transaction_type": "BUY",
        "quantity": 1,
        "average_price": 22500.0,
        "status_message": "Order filled",
    }

    with patch("backend.api.routes.orders.broadcast") as mock_broadcast:
        response = await async_client.post("/api/orders/postback", json=payload)

    assert response.status_code == 401
    data = response.json()
    assert "Invalid postback signature" in data.get("detail", "")
    # Broadcast should NOT have been called for a bad signature
    mock_broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_postback_missing_checksum(async_client, stub_connections):
    """
    POST /api/orders/postback without checksum field returns 401.

    The endpoint requires the checksum field per Kite postback protocol.
    """
    payload = {
        "order_id": "12345",
        "order_timestamp": "2026-05-04 10:30:00",
        # Missing checksum
        "user_id": "ZG0790",
        "status": "COMPLETE",
        "tradingsymbol": "NIFTY25APRFUT",
    }

    with patch("backend.api.routes.orders.broadcast") as mock_broadcast:
        response = await async_client.post("/api/orders/postback", json=payload)

    assert response.status_code == 401
    data = response.json()
    assert "Invalid postback signature" in data.get("detail", "")
    mock_broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_postback_fallback_account_lookup(async_client, reset_singletons):
    """
    POST /api/orders/postback tries multiple accounts when user_id is
    empty or unrecognized (Kite postback doesn't always include user_id).

    Verifies that the fallback-iteration path works when the account
    is in the middle of the candidate list.
    """
    from backend.brokers.connections import Connections, KiteConnection

    # Create stubs for multiple accounts.
    # Set both _api_secret (legacy) and api_secret (public property mirror)
    # so the postback HMAC path works regardless of which accessor is used.
    # `spec=KiteConnection` makes isinstance(stub, KiteConnection) True so
    # the postback handler's `isinstance(c, KiteConnection)` candidate
    # filter accepts them (Dhan/Groww connections are intentionally
    # skipped because they don't expose api_secret).
    stub1 = MagicMock(spec=KiteConnection)
    stub1._api_secret = "secret_acct1"
    stub1.api_secret = "secret_acct1"
    stub2 = MagicMock(spec=KiteConnection)
    stub2._api_secret = "test_secret_123"
    stub2.api_secret = "test_secret_123"
    stub3 = MagicMock(spec=KiteConnection)
    stub3._api_secret = "secret_acct3"
    stub3.api_secret = "secret_acct3"

    conn = Connections()
    conn.conn = {"ACCT1": stub1, "ZG0790": stub2, "ACCT3": stub3}

    order_id = "12345"
    order_timestamp = "2026-05-04 10:30:00"
    api_secret = "test_secret_123"  # Matches ZG0790

    msg = (str(order_id) + str(order_timestamp) + api_secret).encode()
    checksum = hashlib.sha256(msg).hexdigest()

    payload = {
        "order_id": order_id,
        "order_timestamp": order_timestamp,
        "checksum": checksum,
        # Empty user_id forces fallback iteration
        "user_id": "",
        "status": "COMPLETE",
        "tradingsymbol": "NIFTY25APRFUT",
    }

    with patch("backend.api.routes.orders.broadcast") as mock_broadcast:
        response = await async_client.post("/api/orders/postback", json=payload)

    # POST handler defaults to 201 Created
    assert response.status_code == 201
    data = response.json()
    assert data.get("status") == "ok"
    # Payload omits quantity so position_filled is skipped (qty>0 guard);
    # COMPLETE still fires order_update + book_changed.
    import json as _json
    assert mock_broadcast.call_count == 2
    call_events = [
        _json.loads(c.args[0]).get("event")
        for c in mock_broadcast.call_args_list
    ]
    assert "order_update" in call_events
    assert "book_changed" in call_events
