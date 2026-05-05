"""
Shared test fixtures for RamboQuant backend tests.

Provides:
  - AsyncTestClient for Litestar routes (with DB init bypassed)
  - Demo and admin request mocking
  - Singleton cleanup (Connections, SimDriver, PaperEngine)
  - Database session isolation
"""

import os
import sys
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from litestar.testing import AsyncTestClient

from backend.shared.helpers.singleton_base import SingletonBase

# Mark pytest as running so any code that checks can skip DB init
os.environ['PYTEST_RUNNING'] = '1'


@pytest_asyncio.fixture
async def app(request):
    """Get the Litestar app instance for testing.

    Creates a test-mode Litestar app by patching out the DB initialization
    and background task startup. The real app's route handlers are preserved,
    but expensive startup operations are skipped.
    """
    # Create no-op async functions for startup/shutdown tasks
    async def noop():
        pass

    # Patch the imports in app.py BEFORE it's imported (if not already)
    with patch('backend.api.app.init_db', new=noop), \
         patch('backend.api.app._rebuild_broker_connections', new=noop), \
         patch('backend.api.app.bg_startup', new=noop), \
         patch('backend.api.app.bg_shutdown', new=noop):

        # Import the app (will use the patched functions)
        from backend.api.app import app as litestar_app

        # Additionally patch the app's on_startup/on_shutdown to be empty
        # in case the patch above didn't work
        litestar_app.on_startup = []
        litestar_app.on_shutdown = []

        yield litestar_app


@pytest_asyncio.fixture
async def async_client(app):
    """Async test client for the Litestar app.

    The app's startup handlers have been patched to skip database initialization
    and background tasks. Routes that require DB access should mock the relevant
    session/model methods.
    """
    async with AsyncTestClient(app=app) as client:
        yield client


@pytest.fixture
def reset_singletons():
    """
    Reset all singleton instances before and after a test.
    Call this fixture to wipe Connections, SimDriver, PaperEngine, etc.
    """
    # Clear all singleton instances
    SingletonBase._instances.clear()
    yield
    # Clean up after test
    SingletonBase._instances.clear()


@pytest.fixture
def stub_kite_connection():
    """
    Create a stub KiteConnection-like object with minimal attributes
    needed by the broker code paths. Returns a dict mapping account
    ID to the stub so tests can patch Connections().conn.
    """
    stub = MagicMock()
    stub._api_secret = "test_secret_123"
    stub.get_kite_conn = MagicMock(return_value=MagicMock())
    return {"ZG0790": stub}


@pytest.fixture
def stub_connections(reset_singletons, stub_kite_connection):
    """
    Patch the Connections singleton with stub KiteConnections.
    Tests can use this to avoid hitting real Kite API.
    """
    from backend.shared.helpers.connections import Connections
    conn = Connections()
    conn.conn = stub_kite_connection
    return conn


@pytest.fixture
def demo_request_state():
    """
    Create a request.state mock with is_demo=True for testing
    demo-mode gating in routes.
    """
    state = MagicMock()
    state.is_demo = True
    return state


@pytest.fixture
def admin_request_state():
    """
    Create a request.state mock with is_demo=False for testing
    admin path routes.
    """
    state = MagicMock()
    state.is_demo = False
    return state
