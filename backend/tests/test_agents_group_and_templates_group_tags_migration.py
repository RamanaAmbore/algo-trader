"""
Tests for Sprint 7 (CLI/text grammar agent-authoring effort) — the
additive `agents.group_name` column plus `order_templates.group_name` /
`order_templates.tags` columns (`#group=...` / `#tag=...` directives).

This project has no Alembic — schema migrations are idempotent
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements wired into
`backend/api/database.py:init_db()` (see `_migrate_agents_cli_source`
for the pattern this mirrors, and `test_agents_cli_source_migration.py`
for the real-init_db-call test pattern reused here).

Covers:
  1. `_migrate_agents_group_name` issues the right SQL, with a
     lock_timeout guard, no CONCURRENTLY (safe inside init_db's
     transactional block).
  2. `_migrate_order_templates_group_and_tags` issues the right SQL for
     BOTH columns — `group_name` nullable no-default, `tags` NOT NULL
     with an explicit `DEFAULT '[]'::jsonb` (required because
     `order_templates` already has system rows seeded every boot, so a
     NOT NULL column without a default would fail against those rows).
  3. Both steps are wired into `init_db()` inside the
     `engine.begin()` transaction (plain ADD COLUMN, never CREATE INDEX
     CONCURRENTLY).
  4. A real (mocked-I/O) call to `init_db()` does not raise with the
     new steps wired in, and both ALTER statements actually run.
  5. `Agent.group_name`, `OrderTemplate.group_name`, and
     `OrderTemplate.tags` model columns exist with the right
     nullability/type.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects.postgresql import JSONB

import backend.api.database as dbmod
from backend.api.models import Agent, OrderTemplate


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


# ═══════════════════════════════════════════════════════════════════════
# agents.group_name
# ═══════════════════════════════════════════════════════════════════════

def test_migrate_agents_group_name_statement_shape():
    """Plain ADD COLUMN IF NOT EXISTS, nullable VARCHAR(64), lock_timeout
    set, no CONCURRENTLY (would hard-error inside init_db's transaction)."""
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_agents_group_name(conn))

    joined = "\n".join(conn.executed)
    assert "ALTER TABLE agents ADD COLUMN IF NOT EXISTS group_name VARCHAR(64)" in joined
    assert any("lock_timeout" in s for s in conn.executed), (
        "no explicit lock_timeout set on the ADD COLUMN step"
    )
    assert not any("CONCURRENTLY" in s for s in conn.executed), (
        "CONCURRENTLY statement found inside a transactional ADD COLUMN "
        "step — this would crash init_db()."
    )


def test_init_db_calls_agents_group_name_migration_inside_begin_block():
    """Structural guard: init_db must call _migrate_agents_group_name(conn)
    INSIDE the `async with engine.begin() as conn:` block."""
    src = inspect.getsource(dbmod.init_db)
    lines = src.splitlines()
    begin_idx = next(
        i for i, l in enumerate(lines) if "async with engine.begin() as conn:" in l
    )
    begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

    call_idx = next(
        i for i, l in enumerate(lines)
        if "_migrate_agents_group_name(conn)" in l and "async def" not in l
    )
    call_indent = len(lines[call_idx]) - len(lines[call_idx].lstrip())

    assert call_idx > begin_idx and call_indent > begin_indent, (
        "_migrate_agents_group_name(conn) must be called INSIDE the "
        "engine.begin() transactional block."
    )


# ═══════════════════════════════════════════════════════════════════════
# order_templates.group_name + order_templates.tags
# ═══════════════════════════════════════════════════════════════════════

def test_migrate_order_templates_group_and_tags_statement_shape():
    """group_name: plain nullable ADD COLUMN. tags: NOT NULL with an
    explicit DEFAULT '[]'::jsonb (order_templates has seeded system rows
    on every boot — a NOT NULL column with no default would fail)."""
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_order_templates_group_and_tags(conn))

    joined = "\n".join(conn.executed)
    assert (
        "ALTER TABLE order_templates ADD COLUMN IF NOT EXISTS group_name VARCHAR(64)"
        in joined
    )
    assert "ALTER TABLE order_templates ADD COLUMN IF NOT EXISTS tags JSONB" in joined
    assert "NOT NULL" in joined and "DEFAULT '[]'::jsonb" in joined
    assert any("lock_timeout" in s for s in conn.executed), (
        "no explicit lock_timeout set on the ADD COLUMN step"
    )
    assert not any("CONCURRENTLY" in s for s in conn.executed), (
        "CONCURRENTLY statement found inside a transactional ADD COLUMN "
        "step — this would crash init_db()."
    )


def test_init_db_calls_order_templates_group_and_tags_migration_inside_begin_block():
    src = inspect.getsource(dbmod.init_db)
    lines = src.splitlines()
    begin_idx = next(
        i for i, l in enumerate(lines) if "async with engine.begin() as conn:" in l
    )
    begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

    call_idx = next(
        i for i, l in enumerate(lines)
        if "_migrate_order_templates_group_and_tags(conn)" in l and "async def" not in l
    )
    call_indent = len(lines[call_idx]) - len(lines[call_idx].lstrip())

    assert call_idx > begin_idx and call_indent > begin_indent, (
        "_migrate_order_templates_group_and_tags(conn) must be called "
        "INSIDE the engine.begin() transactional block."
    )


# ═══════════════════════════════════════════════════════════════════════
# End-to-end init_db() wiring
# ═══════════════════════════════════════════════════════════════════════

def test_init_db_real_call_does_not_raise_with_group_and_tags_steps():
    """End-to-end: call the REAL init_db() coroutine (engine faked, no
    real DB, every other seeding substep patched to a no-op) and confirm
    wiring in the two new steps doesn't make init_db() raise, and that
    both ALTER statements really ran."""
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
         patch("backend.api.algo.template_registry.migrate_and_retire_agent_fragments", new=AsyncMock()), \
         patch("backend.api.algo.template_registry.REGISTRY.reload", new=AsyncMock()), \
         patch("backend.shared.helpers.settings.seed_settings", new=AsyncMock()), \
         patch("backend.api.algo.templates_seed.seed_templates", new=AsyncMock()), \
         patch("backend.api.routes.watchlist.seed_global_pinned", new=AsyncMock()), \
         patch("backend.shared.helpers.alert_utils.refresh_alert_recipients", new=AsyncMock()):

        asyncio.run(dbmod.init_db())  # must not raise

    assert any(
        "ADD COLUMN IF NOT EXISTS group_name VARCHAR(64)" in s
        and "agents" in s for s in tx_conn.executed
    ), "agents.group_name ADD COLUMN statement did not run during init_db()"
    assert any(
        "order_templates ADD COLUMN IF NOT EXISTS group_name" in s
        for s in tx_conn.executed
    ), "order_templates.group_name ADD COLUMN statement did not run during init_db()"
    assert any(
        "order_templates ADD COLUMN IF NOT EXISTS tags JSONB" in s
        for s in tx_conn.executed
    ), "order_templates.tags ADD COLUMN statement did not run during init_db()"


# ═══════════════════════════════════════════════════════════════════════
# ORM model columns
# ═══════════════════════════════════════════════════════════════════════

def test_agent_model_has_group_name_column_nullable_default_none():
    col = Agent.__table__.columns["group_name"]
    assert col.nullable is True
    agent = Agent()
    assert getattr(agent, "group_name", None) is None
    # `group` itself must never exist as an attribute — only `group_name`
    # (SQL reserved keyword avoidance).
    assert not hasattr(agent, "group")


def test_order_template_model_has_group_name_column_nullable():
    col = OrderTemplate.__table__.columns["group_name"]
    assert col.nullable is True
    tmpl = OrderTemplate()
    assert getattr(tmpl, "group_name", None) is None
    assert not hasattr(tmpl, "group")


def test_order_template_model_has_tags_column_not_null_jsonb_with_default():
    col = OrderTemplate.__table__.columns["tags"]
    assert col.nullable is False
    assert isinstance(col.type, JSONB)
    # A server_default must exist so a fresh create_all() produces the
    # same column shape as the idempotent ALTER migration does.
    assert col.server_default is not None
