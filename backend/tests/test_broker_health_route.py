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
import time


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


class TestBrokerAccountHealthIsActiveField:
    """`BrokerAccountHealth.is_active` mirrors `broker_accounts.is_active`,
    independent of the fetch-derived `state` (green/amber/red/inactive).

    This is purely additive plumbing for the navbar connection-status
    popup's enable/disable toggle — it must not change `_derive_account_health`
    or the existing state-derivation logic."""

    def test_disabled_account_is_active_false(self):
        """An account with broker_accounts.is_active=False must surface
        is_active=False on its BrokerAccountHealth row, regardless of its
        fetch-health state."""
        from backend.api.routes.health import _build_account_health_entry

        now = time.time()
        row = _build_account_health_entry(
            "ZG0790",
            {"last_ok_at": now, "last_fail_at": 0.0},
            now,
            broker_label_map={"ZG0790": "kite"},
            poll_priority_map={},
            active_ticker_acct="",
            active_map={"ZG0790": False},
        )
        assert row.is_active is False
        # state derivation must be untouched by the disabled flag
        assert row.state == "green"

    def test_enabled_account_is_active_true(self):
        """An enabled account (broker_accounts.is_active=True) surfaces
        is_active=True."""
        from backend.api.routes.health import _build_account_health_entry

        now = time.time()
        row = _build_account_health_entry(
            "ZJ6294",
            {"last_ok_at": now, "last_fail_at": 0.0},
            now,
            broker_label_map={"ZJ6294": "kite"},
            poll_priority_map={},
            active_ticker_acct="",
            active_map={"ZJ6294": True},
        )
        assert row.is_active is True

    def test_struct_default_is_active_true_for_backcompat(self):
        """BrokerAccountHealth() built with no is_active kwarg (e.g. an
        in-flight rolling-deploy response) defaults to True."""
        from backend.api.routes.health import BrokerAccountHealth

        row = BrokerAccountHealth(
            account="ZG0790", broker="kite", state="green", reason="ok",
            last_good_at=None, last_check_at=None,
        )
        assert row.is_active is True

    def test_fallback_branch_empty_active_map_does_not_crash(self):
        """When active_map is an empty dict (the empty-broker_label_map
        DB-failure fallback branch in get_broker_health() defaults to {}),
        every account must default to is_active=True rather than raising."""
        from backend.api.routes.health import _build_account_health_entry

        now = time.time()
        row = _build_account_health_entry(
            "DH6847",
            {"last_ok_at": 0.0, "last_fail_at": 0.0},
            now,
            broker_label_map={},
            poll_priority_map={},
            active_ticker_acct="",
            active_map={},
        )
        assert row.is_active is True
        assert row.state == "inactive"

    def test_missing_account_in_active_map_defaults_true(self):
        """An account present in health_map/broker_label_map but absent from
        active_map (e.g. a DB row created between the two queries) must not
        be misreported as disabled."""
        from backend.api.routes.health import _build_account_health_entry

        now = time.time()
        row = _build_account_health_entry(
            "ZG0790",
            {"last_ok_at": now, "last_fail_at": 0.0},
            now,
            broker_label_map={"ZG0790": "kite"},
            poll_priority_map={},
            active_ticker_acct="",
            active_map={},
        )
        assert row.is_active is True


class TestLoadBrokerLabelAndPriorityMapsReturnsActiveMap:
    """_load_broker_label_and_priority_maps() must return a third dict,
    active_map, mirroring BrokerAccount.is_active per account."""

    def test_returns_three_dicts_with_is_active_values(self):
        from backend.api.routes import health as health_mod

        async def _run():
            fake_row_active = MagicMock(
                account="ZG0790", broker_id="kite", is_active=True,
                poll_priority="hot", auto_downgrade_enabled=False,
                auto_downgraded_at=None, auto_downgrade_reason=None,
                circuit_breaker_enabled=False,
            )
            fake_row_disabled = MagicMock(
                account="ZJ6294", broker_id="kite", is_active=False,
                poll_priority="hot", auto_downgrade_enabled=False,
                auto_downgraded_at=None, auto_downgrade_reason=None,
                circuit_breaker_enabled=False,
            )

            fake_scalars = MagicMock()
            fake_scalars.all.return_value = [fake_row_active, fake_row_disabled]
            fake_result = MagicMock()
            fake_result.scalars.return_value = fake_scalars

            fake_session = AsyncMock()
            fake_session.execute = AsyncMock(return_value=fake_result)

            class _CtxMgr:
                async def __aenter__(self):
                    return fake_session

                async def __aexit__(self, *a):
                    return False

            with patch.object(
                health_mod, "shared_async_session", return_value=_CtxMgr()
            ):
                return await health_mod._load_broker_label_and_priority_maps()

        broker_label_map, poll_priority_map, active_map = asyncio.run(_run())
        assert broker_label_map["ZG0790"] == "kite"
        assert broker_label_map["ZJ6294"] == "kite"
        assert active_map["ZG0790"] is True
        assert active_map["ZJ6294"] is False


class TestGetBrokerHealthHandlerThreadsIsActive:
    """Handler-level coverage: get_broker_health() must actually pass the
    active_map it receives from _load_broker_label_and_priority_maps()
    through to every BrokerAccountHealth row it builds — not just the
    lower-level helpers exercised above. Exercises the controller method
    as an unbound function (same pattern as TestConnServiceHealthPayload
    in test_ticker_failover_health.py), so a future edit that drops the
    active_map argument at either call site in get_broker_health() fails
    this test even though the helpers' own unit tests still pass."""

    @pytest.mark.asyncio
    async def test_normal_branch_threads_active_map_per_account(self):
        from backend.api.routes import health as health_mod

        with patch(
            "backend.brokers.broker_apis.fetch_health_snapshot",
            return_value={
                "ZG0790": {"last_ok_at": time.time(), "last_fail_at": 0.0},
                "ZJ6294": {"last_ok_at": time.time(), "last_fail_at": 0.0},
            },
        ), patch.object(
            health_mod,
            "_load_broker_label_and_priority_maps",
            new=AsyncMock(return_value=(
                {"ZG0790": "kite", "ZJ6294": "kite"},
                {},
                {"ZG0790": True, "ZJ6294": False},
            )),
        ), patch.object(
            health_mod, "_resolve_active_ticker_account", return_value=""
        ), patch.object(
            health_mod, "_resolve_ticker_calendar_status", return_value=(False, "")
        ), patch.object(
            health_mod, "_resolve_entitlement_denied", return_value={}
        ), patch.object(
            health_mod, "_resolve_primary_mdb_account", return_value=""
        ), patch(
            "backend.api.database.shared_async_session",
            side_effect=RuntimeError("no DB in this test"),
        ):
            resp = await health_mod.BrokerHealthController.get_broker_health.fn(self=None)

        by_acct = {row.account: row for row in resp.accounts}
        assert by_acct["ZG0790"].is_active is True
        assert by_acct["ZJ6294"].is_active is False

    @pytest.mark.asyncio
    async def test_fallback_branch_defaults_every_account_to_active(self):
        """When _load_broker_label_and_priority_maps() itself raises,
        get_broker_health() falls back to iterating health_map directly
        with an empty active_map — every row must default to
        is_active=True, not crash, and not silently mis-report a
        disabled account as active (there is no DB signal to report)."""
        from backend.api.routes import health as health_mod

        with patch(
            "backend.brokers.broker_apis.fetch_health_snapshot",
            return_value={
                "DH6847": {"last_ok_at": 0.0, "last_fail_at": 0.0},
            },
        ), patch.object(
            health_mod,
            "_load_broker_label_and_priority_maps",
            new=AsyncMock(side_effect=RuntimeError("DB unavailable")),
        ), patch.object(
            health_mod, "_resolve_active_ticker_account", return_value=""
        ), patch.object(
            health_mod, "_resolve_ticker_calendar_status", return_value=(False, "")
        ), patch.object(
            health_mod, "_resolve_entitlement_denied", return_value={}
        ), patch.object(
            health_mod, "_resolve_primary_mdb_account", return_value=""
        ), patch(
            "backend.api.database.shared_async_session",
            side_effect=RuntimeError("no DB in this test"),
        ):
            resp = await health_mod.BrokerHealthController.get_broker_health.fn(self=None)

        assert len(resp.accounts) == 1
        assert resp.accounts[0].account == "DH6847"
        assert resp.accounts[0].is_active is True
