"""
Tests for Sprint 1 (CLI/text grammar agent-authoring effort) — the
additive `agents.cli_source` column.

This project has no Alembic — schema migrations are idempotent
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements wired into
`backend/api/database.py:init_db()` (see `_migrate_algo_orders_mcp_
request_id` for the pattern this mirrors, and
`test_order_lifecycle_sprint1a.py` for the real-init_db-call test
pattern reused here).

Covers:
  1. `_migrate_agents_cli_source` issues the right SQL, with a
     lock_timeout guard, no CONCURRENTLY (safe inside init_db's
     transactional block).
  2. The step is wired into `init_db()` inside the `engine.begin()`
     transaction (not after it — this is a plain ADD COLUMN, never
     CREATE INDEX CONCURRENTLY, so it belongs inside the transaction
     unlike the Sprint 1a index steps).
  3. A real (mocked-I/O) call to `init_db()` does not raise with the
     new step wired in.
  4. `Agent.cli_source` model column exists, is nullable, defaults to
     None.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

import backend.api.database as dbmod
from backend.api.models import Agent


class _FakeResult:
    def fetchone(self): return None
    def fetchall(self): return []
    def scalar(self): return None
    def scalar_one_or_none(self): return None
    def all(self): return []
    def first(self): return None
    def scalars(self): return self
    def __iter__(self): return iter(())


class _FakeTxConn:
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, stmt, *a, **k):
        self.executed.append(str(stmt))
        return _FakeResult()

    async def run_sync(self, fn, *a, **k):
        return None


class _FakeBeginCtx:
    def __init__(self, conn: _FakeTxConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakeAutocommitConn:
    def __init__(self, recorder: dict):
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execution_options(self, **kw):
        self._recorder.setdefault("execution_options", []).append(kw)
        return self

    async def execute(self, stmt, *a, **k):
        self._recorder.setdefault("executed", []).append(str(stmt))
        return None


class _FakeEngine:
    def __init__(self, begin=None, connect=None):
        self.begin = begin or MagicMock()
        self.connect = connect or MagicMock()


def test_migrate_agents_cli_source_statement_shape():
    """Plain ADD COLUMN IF NOT EXISTS, nullable TEXT, lock_timeout set,
    no CONCURRENTLY (would hard-error inside init_db's transaction)."""
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_agents_cli_source(conn))

    joined = "\n".join(conn.executed)
    assert "ALTER TABLE agents ADD COLUMN IF NOT EXISTS cli_source TEXT" in joined
    assert any("lock_timeout" in s for s in conn.executed), (
        "no explicit lock_timeout set on the ADD COLUMN step"
    )
    assert not any("CONCURRENTLY" in s for s in conn.executed), (
        "CONCURRENTLY statement found inside a transactional ADD COLUMN "
        "step — this would crash init_db()."
    )


def test_init_db_calls_cli_source_migration_inside_begin_block():
    """Structural guard: init_db must call _migrate_agents_cli_source(conn)
    INSIDE the `async with engine.begin() as conn:` block (it's a plain
    ADD COLUMN, not CREATE INDEX CONCURRENTLY — no reason to defer it)."""
    src = inspect.getsource(dbmod.init_db)
    lines = src.splitlines()
    begin_idx = next(
        i for i, l in enumerate(lines) if "async with engine.begin() as conn:" in l
    )
    begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

    call_idx = next(
        i for i, l in enumerate(lines)
        if "_migrate_agents_cli_source(conn)" in l and "async def" not in l
    )
    call_indent = len(lines[call_idx]) - len(lines[call_idx].lstrip())

    assert call_idx > begin_idx and call_indent > begin_indent, (
        "_migrate_agents_cli_source(conn) must be called INSIDE the "
        "engine.begin() transactional block."
    )


def test_init_db_real_call_does_not_raise_with_cli_source_step():
    """End-to-end: call the REAL init_db() coroutine (engine faked, no
    real DB, every other seeding substep patched to a no-op) and confirm
    wiring in the new cli_source step doesn't make init_db() raise, and
    that the ADD COLUMN statement really ran."""
    tx_conn = _FakeTxConn()
    recorder: dict = {}
    fake_engine = _FakeEngine(
        begin=MagicMock(return_value=_FakeBeginCtx(tx_conn)),
        connect=MagicMock(side_effect=lambda: _FakeAutocommitConn(recorder)),
    )

    from unittest.mock import patch
    with patch.object(dbmod, "engine", fake_engine), \
         patch.object(dbmod, "_ensure_shared_broker_schema", new=AsyncMock()), \
         patch.object(dbmod, "seed_special_sessions", new=AsyncMock()), \
         patch("backend.api.algo.grammar.seed_grammar_tokens", new=AsyncMock()), \
         patch("backend.api.algo.grammar_registry.REGISTRY.reload", new=AsyncMock()), \
         patch("backend.api.algo.template_registry.seed_agent_templates", new=AsyncMock()), \
         patch("backend.api.algo.agent_engine.seed_agents", new=AsyncMock()), \
         patch("backend.shared.helpers.settings.seed_settings", new=AsyncMock()), \
         patch("backend.api.algo.templates_seed.seed_templates", new=AsyncMock()), \
         patch("backend.api.routes.watchlist.seed_global_pinned", new=AsyncMock()), \
         patch("backend.shared.helpers.alert_utils.refresh_alert_recipients", new=AsyncMock()):

        asyncio.run(dbmod.init_db())  # must not raise

    assert any(
        "ADD COLUMN IF NOT EXISTS cli_source" in s for s in tx_conn.executed
    ), "cli_source ADD COLUMN statement did not run during init_db()"


def test_agent_model_has_cli_source_column_nullable_default_none():
    col = Agent.__table__.columns["cli_source"]
    assert col.nullable is True
    # New instance (no explicit value) defaults to None — purely additive,
    # no agent-row construction site anywhere needs to supply it.
    agent = Agent()
    assert getattr(agent, "cli_source", None) is None
