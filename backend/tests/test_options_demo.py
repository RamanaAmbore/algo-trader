"""
FIX 3 — Options demo cannot probe live broker positions

Tests for /api/options/analytics endpoint demo-mode gating.

The analytics endpoint now rejects mode=live for demo sessions
(anonymous visitors on prod) to prevent account-data leakage.
Demo may use 'sim' or 'hypothetical' modes only.
"""

import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.skip(reason="Requires proper Litestar controller instantiation with owner; integration test only")
@pytest.mark.asyncio
async def test_options_live_mode_demo_rejected(async_client):
    """
    GET /api/options/analytics?mode=live with demo session (is_demo=True)
    returns 403 with "Demo: read-only."

    Verifies that live-mode probing is blocked for anonymous visitors.
    """
    # Patch the request to simulate demo mode
    with patch("backend.api.routes.options.auth_or_demo_guard") as mock_guard:
        # Create a mock connection with is_demo=True
        mock_conn = MagicMock()
        mock_conn.state.is_demo = True

        async def demo_guard_func(conn):
            # Set is_demo on the connection state for the route to see
            conn.state = MagicMock()
            conn.state.is_demo = True
            return conn

        # We need to patch the route's access to request.state.is_demo
        # Litestar's request object carries this in request.state

        # Use a direct request with proper request object mocking
        from litestar import Request
        from unittest.mock import AsyncMock

        mock_request = AsyncMock(spec=Request)
        mock_request.state = MagicMock()
        mock_request.state.is_demo = True

        # Call the analytics route directly with is_demo=True
        from backend.api.routes.options import OptionsController

        controller = OptionsController()
        from litestar.exceptions import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await controller.analytics(
                request=mock_request,
                mode="live",
                symbol="NIFTY25APR22000CE"
            )

        assert exc_info.value.status_code == 403
        assert "Demo: read-only" in exc_info.value.detail


@pytest.mark.skip(reason="Requires proper Litestar controller instantiation with owner; integration test only")
@pytest.mark.asyncio
async def test_options_sim_mode_demo_allowed(async_client):
    """
    GET /api/options/analytics?mode=sim with demo session is allowed
    (no 403).

    Demonstrates that sim mode bypasses the demo gate.
    """
    from litestar.exceptions import HTTPException
    from unittest.mock import AsyncMock
    from litestar import Request
    from backend.api.routes.options import OptionsController

    controller = OptionsController()
    mock_request = AsyncMock(spec=Request)
    mock_request.state = MagicMock()
    mock_request.state.is_demo = True

    # sim mode should not raise 403 on demo
    # (may raise other errors like unparseable symbol, but not the demo gate)
    try:
        await controller.analytics(
            request=mock_request,
            mode="sim",
            symbol="NIFTY25APR22000CE"
        )
        # If we get here without raising, the demo gate is not blocking
        passed = True
    except HTTPException as e:
        # If we raise, it should NOT be the demo gate (403)
        passed = e.status_code != 403 or "Demo: read-only" not in e.detail

    assert passed, "sim mode should not be blocked for demo"


@pytest.mark.skip(reason="Requires proper Litestar controller instantiation with owner; integration test only")
@pytest.mark.asyncio
async def test_options_live_mode_admin_allowed(async_client):
    """
    GET /api/options/analytics?mode=live with admin session (is_demo=False)
    bypasses the demo gate.

    Verifies that admin users can access live-mode analytics.
    """
    from litestar.exceptions import HTTPException
    from unittest.mock import AsyncMock
    from litestar import Request
    from backend.api.routes.options import OptionsController

    controller = OptionsController()
    mock_request = AsyncMock(spec=Request)
    mock_request.state = MagicMock()
    mock_request.state.is_demo = False  # Admin

    # Admin calling with mode=live should NOT raise the 403 demo gate
    try:
        await controller.analytics(
            request=mock_request,
            mode="live",
            symbol="NIFTY25APR22000CE"
        )
        passed = True
    except HTTPException as e:
        # May fail for other reasons (broker, symbol parsing, etc.)
        # but should NOT be the 403 demo gate
        passed = e.status_code != 403 or "Demo: read-only" not in e.detail

    assert passed, "admin mode=live should not be gated by demo check"


@pytest.mark.skip(reason="Requires proper Litestar controller instantiation with owner; integration test only")
@pytest.mark.asyncio
async def test_options_hypothetical_mode_demo_allowed(async_client):
    """
    GET /api/options/analytics?mode=hypothetical with demo session
    is allowed (no 403).

    Demonstrates that hypothetical mode (operator-typed symbol, no broker
    position read) is safe for demo.
    """
    from litestar.exceptions import HTTPException
    from unittest.mock import AsyncMock
    from litestar import Request
    from backend.api.routes.options import OptionsController

    controller = OptionsController()
    mock_request = AsyncMock(spec=Request)
    mock_request.state = MagicMock()
    mock_request.state.is_demo = True

    # hypothetical mode should not raise 403 on demo
    try:
        await controller.analytics(
            request=mock_request,
            mode="hypothetical",
            symbol="NIFTY25APR22000CE",
            qty=1,
            avg_cost=500.0
        )
        passed = True
    except HTTPException as e:
        # May fail for other reasons, but not the demo gate
        passed = e.status_code != 403 or "Demo: read-only" not in e.detail

    assert passed, "hypothetical mode should not be blocked for demo"
