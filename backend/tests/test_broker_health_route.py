"""
Tests for routes/health.py — broker health endpoint.

SSOT: display_order sorting uses direct async DB query, not get_account_order_map().
Correctness: accounts are sorted by display_order without calling the sync
  ThreadPoolExecutor+asyncio.run() path that corrupts the asyncpg pool.
No stale-code regression: get_account_order_map() must NOT be imported/called
  during get_broker_health() route execution.
Performance: no sync DB call wrapper (which uses asyncio.run() from async context).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
import pytest


class TestBrokerHealthDisplayOrderSort:
    """The /api/admin/broker-health route must sort accounts by display_order
    using a direct async DB query (via shared_async_session), not calling
    get_account_order_map() which uses a ThreadPoolExecutor+asyncio.run() wrapper
    that corrupts the asyncpg connection pool."""

    def test_get_broker_health_uses_direct_async_query(self):
        """get_broker_health() must use a direct async query for display_order,
        not the sync get_account_order_map() function.

        Verify the direct async path is present: async with shared_async_session()
        for the sort operation."""
        from pathlib import Path
        import re
        health_py = Path("backend/api/routes/health.py").read_text()

        # Must have the direct async query pattern in the get_broker_health method
        # Look for: async with shared_async_session() / _sas()
        async_query_pattern = r'async with\s+(?:shared_async_session|_sas)\s*\(\)'
        assert re.search(async_query_pattern, health_py), (
            "get_broker_health() must use 'async with shared_async_session()' "
            "for the direct async query of display_order"
        )

        # Must have display_order query
        assert "_BA2.display_order" in health_py or (
            "display_order" in health_py and "_sess.execute" in health_py
        ), (
            "get_broker_health() must query BrokerAccount.display_order directly"
        )

        # Must NOT be calling get_account_order_map() in the get_broker_health method.
        # Extract the method body and check for the actual function call pattern.
        broker_health_method = re.search(
            r'async def get_broker_health\(self\).*?(?=\n    async def|\n    @\w+|\Z)',
            health_py,
            re.DOTALL
        )
        assert broker_health_method, "get_broker_health method not found"
        method_body = broker_health_method.group(0)
        # Look for the actual function call (not just a mention in a comment)
        # Exclude comments by using a pattern that doesn't match "#" lines
        lines = [
            line for line in method_body.split('\n')
            if not line.strip().startswith('#')
        ]
        method_body_no_comments = '\n'.join(lines)
        assert "get_account_order_map()" not in method_body_no_comments, (
            "get_broker_health() must not call get_account_order_map() — "
            "use direct async query instead"
        )


class TestBrokerHealthDirectAsyncQuery:
    """The fix must use shared_async_session directly for display_order lookup."""

    def test_shared_async_session_imported_in_health_route(self):
        """shared_async_session must be imported in the get_broker_health() route."""
        from pathlib import Path
        health_py = Path("backend/api/routes/health.py").read_text()
        assert "shared_async_session" in health_py, (
            "health.py must import shared_async_session for direct async queries"
        )

    def test_display_order_query_exists_in_health_route(self):
        """health.py must have a direct query for _BA.display_order."""
        from pathlib import Path
        health_py = Path("backend/api/routes/health.py").read_text()
        assert "_BA2.display_order" in health_py or (
            "display_order" in health_py and "select(" in health_py
        ), (
            "health.py must have a direct query for BrokerAccount.display_order"
        )

    def test_get_broker_health_method_not_import_get_account_order_map(self):
        """get_account_order_map must NOT be imported/called in get_broker_health method.

        The fix uses a direct async query, so this sync function should not appear
        in the actual implementation."""
        from pathlib import Path
        import re
        health_py = Path("backend/api/routes/health.py").read_text()

        # Extract the get_broker_health method
        broker_health_method = re.search(
            r'async def get_broker_health\(self\).*?(?=\n    async def|\n    @\w+|\Z)',
            health_py,
            re.DOTALL
        )
        assert broker_health_method, "get_broker_health method not found"
        method_body = broker_health_method.group(0)

        # Remove comments to avoid false positives from comment mentions
        lines = [
            line for line in method_body.split('\n')
            if not line.strip().startswith('#')
        ]
        method_body_no_comments = '\n'.join(lines)

        # Check that the function is not called (imported) in the method
        assert "from backend.brokers.broker_apis import get_account_order_map" not in method_body_no_comments, (
            "get_broker_health() must not import get_account_order_map"
        )
        assert "get_account_order_map()" not in method_body_no_comments, (
            "get_broker_health() must not call get_account_order_map() — use direct async query"
        )
