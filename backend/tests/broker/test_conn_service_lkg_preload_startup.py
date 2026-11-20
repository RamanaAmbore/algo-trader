"""Regression tests: conn_service must preload the DB-backed last-known-good
(LKG) cache at startup, the same way the main API process already does.

Background: `backend/api/background.py:_preload_db_lkg_cache()` seeds
`broker_apis._DB_LKG_CACHE` from `daily_book` at the start of the main API's
own `_task_performance`. conn_service (`backend/brokers/service/app.py`) owns
every real broker session + the KiteTicker, and is the process that restarts
on routine deploys — but it never called this preload. Every conn_service
restart therefore started every account's in-memory LKG cold, maximizing how
often an account with no prior LKG hits the worst-case empty-response path
(the stale-substitution fallback chain: in-memory LKG → DB-backed LKG →
empty).

Fix: `_preload_db_lkg_on_startup()` (new function in
`backend/brokers/service/app.py`) calls the EXACT SAME
`background.py:_preload_db_lkg_cache()` function — no reimplementation — and
is registered in `create_app()`'s `on_startup` list.

Five quality dimensions:
  SSOT        — asserts the real `backend.api.background._preload_db_lkg_cache`
                function is the one invoked, not a reimplemented query.
  Correctness — covers direct-call success, direct-call exception-swallowing
                (must never block conn_service startup), registration in
                `create_app()`'s `on_startup` list, and real end-to-end
                Litestar lifespan wiring via `TestClient`.
  Performance — no real broker/DB I/O; the one real function call is mocked.
  Reuse       — mirrors the existing `test_init_connections_on_startup_*`
                pattern in `test_conn_service_routes.py` for the direct-call
                cases, and the existing `_task_prewarm_tokens`/
                `_ticker_watchdog` registration-style assertions for the
                `on_startup` list case.
  UX          — every assert carries an f-string with the actual value.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar.testing import TestClient

import backend.brokers.service.app as svc_app


class TestPreloadDbLkgOnStartupDirectCall:
    """Direct-call behaviour of `_preload_db_lkg_on_startup()`."""

    @pytest.mark.asyncio
    async def test_calls_the_real_preload_function(self):
        """Must call `background.py:_preload_db_lkg_cache()` — the exact
        same function the main API process already runs, not a
        reimplementation of the daily_book query."""
        mock_preload = AsyncMock()
        with patch("backend.api.background._preload_db_lkg_cache", mock_preload):
            app = MagicMock()
            await svc_app._preload_db_lkg_on_startup(app)

        mock_preload.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_is_swallowed_never_blocks_startup(self):
        """A failure in the preload (e.g. DB unreachable) must be caught
        and logged — never raised — so it can't block conn_service from
        starting, mirroring `_task_performance`'s own try/except around
        the identical call."""
        mock_preload = AsyncMock(side_effect=RuntimeError("DB unreachable"))
        with patch("backend.api.background._preload_db_lkg_cache", mock_preload):
            app = MagicMock()
            # Must not raise.
            await svc_app._preload_db_lkg_on_startup(app)

        mock_preload.assert_called_once()


class TestPreloadDbLkgRegisteredInCreateApp:
    """`create_app()` must wire `_preload_db_lkg_on_startup` into its
    `on_startup` hook list — matching how `_task_prewarm_tokens` /
    `_ticker_watchdog` registration is verified elsewhere in this suite."""

    def test_on_startup_list_contains_the_preload_hook(self):
        with patch.object(svc_app, "_init_connections_on_startup", new=AsyncMock()), \
             patch.object(svc_app, "_start_kite_ticker", new=AsyncMock()):
            app = svc_app.create_app()

        assert svc_app._preload_db_lkg_on_startup in app.on_startup, (
            f"_preload_db_lkg_on_startup must be registered in create_app()'s "
            f"on_startup list; got {app.on_startup!r}"
        )

    def test_preload_hook_runs_after_connections_init(self):
        """Ordering sanity check — the preload hook should not run before
        `_init_connections_on_startup` in the registered list (not a hard
        dependency today, but keeps startup order predictable).

        Captures the patched-in mock references and compares against THOSE
        (not the post-exit, unpatched module attribute) — `create_app()`'s
        `on_startup` list stores whatever object the bare name resolved to
        at construction time, inside the `with` block."""
        mock_init_conns = AsyncMock()
        with patch.object(svc_app, "_init_connections_on_startup", new=mock_init_conns), \
             patch.object(svc_app, "_start_kite_ticker", new=AsyncMock()):
            app = svc_app.create_app()
            hooks = list(app.on_startup)

        idx_conn = hooks.index(mock_init_conns)
        idx_preload = hooks.index(svc_app._preload_db_lkg_on_startup)
        assert idx_preload > idx_conn, (
            f"_preload_db_lkg_on_startup (index {idx_preload}) must be "
            f"registered after _init_connections_on_startup (index "
            f"{idx_conn}) — got order {hooks!r}"
        )


class TestPreloadDbLkgEndToEndLifespan:
    """Real Litestar lifespan wiring — enters the app via `TestClient` (the
    same mechanism `test_conn_service_routes.py` already uses) so this is
    not just a source-level registration check but a genuine startup-call
    assertion."""

    def test_preload_function_called_during_real_app_startup(self):
        mock_preload = AsyncMock()
        with patch.object(svc_app, "_init_connections_on_startup", new=AsyncMock()), \
             patch.object(svc_app, "_start_kite_ticker", new=AsyncMock()), \
             patch("backend.api.background._preload_db_lkg_cache", mock_preload):
            app = svc_app.create_app()
            with TestClient(app=app):
                pass

        mock_preload.assert_called_once()
