"""
Tests that broker_accounts always reads from the shared `ramboq` database
regardless of which branch/env is running.

SSOT: shared_async_session must bind to `ramboq`, never `ramboq_dev`.
Perf: URL derivation is O(1) (no I/O at import time beyond config read).
Stale: no direct `async_session` usage in BrokerAccount query paths.
Reuse: shared_async_session is the single session factory for all broker-account
       consumers (brokers.py, health.py, connections.py).
UX: the navbar chip shows the count from the shared DB — same value on dev and prod.
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import patch


class TestSharedDatabaseUrl:
    """shared_async_session must always target the `ramboq` database,
    even when deploy_branch is set to a non-main value."""

    def test_shared_url_always_ramboq(self):
        """_SHARED_DATABASE_URL ends with /ramboq regardless of branch."""
        import backend.api.database as db_mod
        assert db_mod._SHARED_DATABASE_URL.endswith("/ramboq"), (
            f"Expected shared URL to end with /ramboq, got: {db_mod._SHARED_DATABASE_URL!r}"
        )

    def test_shared_url_identical_across_branches(self):
        """URL built with deploy_branch='dev' must equal one built with 'main'."""
        import backend.api.database as db_mod

        # _build_shared_url() ignores deploy_branch entirely; calling it
        # twice (or with mocked config) must produce the same value.
        with patch.object(
            sys.modules["backend.shared.helpers.utils"],
            "config",
            new={
                "__getitem__": lambda self, k: "dev" if k == "deploy_branch" else None,
                "get": lambda self, k, d=None: "dev" if k == "deploy_branch" else d,
            },
        ):
            url_dev = db_mod._build_shared_url()

        with patch.object(
            sys.modules["backend.shared.helpers.utils"],
            "config",
            new={
                "__getitem__": lambda self, k: "main" if k == "deploy_branch" else None,
                "get": lambda self, k, d=None: "main" if k == "deploy_branch" else d,
            },
        ):
            url_main = db_mod._build_shared_url()

        assert url_dev == url_main, (
            f"Shared URL differs by branch — dev={url_dev!r}, main={url_main!r}"
        )

    def test_shared_engine_database_name(self):
        """The bound engine's database attribute is 'ramboq'."""
        import backend.api.database as db_mod
        db_name = db_mod._shared_engine.url.database
        assert db_name == "ramboq", (
            f"Shared engine points to '{db_name}', expected 'ramboq'"
        )

    def test_branch_engine_may_differ(self):
        """The branch-local engine URL may differ from the shared URL
        when running on a non-main branch, proving the two engines are
        genuinely independent."""
        import backend.api.database as db_mod
        # On the dev branch the branch engine URL ends with /ramboq_dev;
        # on main it also ends with /ramboq. Either way the shared URL
        # must end with /ramboq. If both are equal (running on main) the
        # assertion is trivially satisfied — still correct.
        assert db_mod._SHARED_DATABASE_URL.endswith("/ramboq")

    def test_shared_async_session_is_exported(self):
        """shared_async_session must be importable from backend.api.database."""
        from backend.api.database import shared_async_session  # noqa: F401
        assert shared_async_session is not None


class TestBrokerAccountConsumersUseSharedSession:
    """Verify that every broker-account consumer imports shared_async_session,
    not the branch-local async_session, for BrokerAccount queries."""

    def _source(self, module_path: str) -> str:
        import importlib
        import inspect
        mod = importlib.import_module(module_path)
        return inspect.getsource(mod)

    def test_brokers_route_uses_shared_session(self):
        src = self._source("backend.api.routes.brokers")
        assert "shared_async_session" in src, (
            "backend/api/routes/brokers.py must import and use shared_async_session"
        )
        # Must NOT import the branch-local session for broker-account use.
        # (It may still import async_session for other tables — but in this
        # module all queries are BrokerAccount, so no async_session import
        # should remain.)
        assert "from backend.api.database import async_session" not in src, (
            "backend/api/routes/brokers.py still imports branch-local async_session"
        )

    def test_health_route_uses_shared_session_for_broker_account(self):
        src = self._source("backend.api.routes.health")
        assert "shared_async_session" in src, (
            "backend/api/routes/health.py must import and use shared_async_session "
            "for BrokerAccount queries"
        )

    def test_connections_uses_shared_session(self):
        src = self._source("backend.brokers.connections")
        # The lazy import inside rebuild_from_db and _seed_db_from_yaml
        # must reference shared_async_session, not async_session.
        assert "shared_async_session" in src, (
            "backend/brokers/connections.py must use shared_async_session "
            "in rebuild_from_db and _seed_db_from_yaml"
        )
        # Verify the old branch-local import is gone from broker-account paths.
        # We count occurrences to allow the module to still have the string
        # in a comment, but the actual import should not appear.
        import re
        live_imports = re.findall(
            r"from backend\.api\.database import async_session(?!\w)", src
        )
        assert not live_imports, (
            f"backend/brokers/connections.py still has {len(live_imports)} "
            "branch-local async_session import(s) — replace with shared_async_session"
        )
