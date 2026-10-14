"""
Tests for the `agents.given` additive column + request/response wiring
(RamboQL Part 2 — GIVEN server-evaluated bindings).

This closes a gap found during implementation review: the backend
GIVEN-evaluation step (`agent_engine._cycle_evaluate_given_bindings`,
covered by `test_agent_given_bindings.py`) reads `agent.given`
defensively via `getattr`, but nothing actually persisted that field —
`Agent` (models.py) had no `given` column, and `AgentCreateRequest`/
`AgentUpdateRequest`/`AgentInfo` (routes/agents.py) had no `given`
field at all. A msgspec.Struct silently drops an unknown JSON key on
decode, so a frontend compiler emitting `{"given": [...], ...}` would
have had that key dropped on save, and the backend evaluation step
would never activate for any real, persisted agent.

Covers:
  1. `_migrate_agents_given` issues the right SQL (ADD COLUMN IF NOT
     EXISTS, nullable JSONB, lock_timeout guard, no CONCURRENTLY).
  2. The step is wired into `init_db()` inside the `engine.begin()`
     transaction, after `_migrate_agents_bracket_columns`.
  3. A real (mocked-I/O) call to `init_db()` does not raise with the
     new step wired in, and `init_db()` genuinely calls
     `migrate_order_action_type_rename()` exactly once (closes the
     "nothing proves init_db calls it" coverage gap from the Part 3
     migration — see test_order_place_order_unification.py for that
     function's own idempotency/correctness coverage).
  4. `Agent.given` model column exists, is nullable, defaults to None
     on a bare row.
  5. `AgentCreateRequest`/`AgentUpdateRequest`/`AgentInfo` all accept
     and round-trip a `given` field; `create_agent`/`update_agent`
     actually persist it onto the ORM row; `_agent_to_info` reads it
     back (defensively, via getattr, same convention as every other
     nullable/optional field in that function).
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


# ═══════════════════════════════════════════════════════════════════════
#  1 — migration statement shape
# ═══════════════════════════════════════════════════════════════════════

def test_migrate_agents_given_statement_shape():
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_agents_given(conn))

    joined = "\n".join(conn.executed)
    assert "ALTER TABLE agents ADD COLUMN IF NOT EXISTS given JSONB" in joined
    assert any("lock_timeout" in s for s in conn.executed), (
        "no explicit lock_timeout set on the ADD COLUMN step"
    )
    assert not any("CONCURRENTLY" in s for s in conn.executed), (
        "CONCURRENTLY statement found inside a transactional ADD COLUMN "
        "step — this would crash init_db()."
    )


# ═══════════════════════════════════════════════════════════════════════
#  2 — wired inside the engine.begin() block, after bracket_columns
# ═══════════════════════════════════════════════════════════════════════

def test_init_db_calls_given_migration_inside_begin_block_after_bracket_columns():
    src = inspect.getsource(dbmod.init_db)
    lines = src.splitlines()
    begin_idx = next(
        i for i, l in enumerate(lines) if "async with engine.begin() as conn:" in l
    )
    begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

    bracket_idx = next(
        i for i, l in enumerate(lines)
        if "_migrate_agents_bracket_columns(conn)" in l and "async def" not in l
    )
    given_idx = next(
        i for i, l in enumerate(lines)
        if "_migrate_agents_given(conn)" in l and "async def" not in l
    )
    given_indent = len(lines[given_idx]) - len(lines[given_idx].lstrip())

    assert given_idx > begin_idx and given_indent > begin_indent, (
        "_migrate_agents_given(conn) must be called INSIDE the "
        "engine.begin() transactional block."
    )
    assert given_idx > bracket_idx, (
        "_migrate_agents_given(conn) must run after "
        "_migrate_agents_bracket_columns(conn)"
    )


# ═══════════════════════════════════════════════════════════════════════
#  3 — real init_db() call, no raise; migrate_order_action_type_rename
#      is genuinely invoked exactly once
# ═══════════════════════════════════════════════════════════════════════

def test_init_db_real_call_does_not_raise_with_given_step_and_calls_order_migration():
    tx_conn = _FakeTxConn()
    recorder: dict = {}
    fake_engine = _FakeEngine(
        begin=MagicMock(return_value=_FakeBeginCtx(tx_conn)),
        connect=MagicMock(side_effect=lambda: _FakeAutocommitConn(recorder)),
    )

    from unittest.mock import patch
    order_migration_mock = AsyncMock()
    with patch.object(dbmod, "engine", fake_engine), \
         patch.object(dbmod, "_ensure_shared_broker_schema", new=AsyncMock()), \
         patch.object(dbmod, "seed_special_sessions", new=AsyncMock()), \
         patch("backend.api.algo.grammar.seed_grammar_tokens", new=AsyncMock()), \
         patch("backend.api.algo.grammar_registry.REGISTRY.reload", new=AsyncMock()), \
         patch("backend.api.algo.template_registry.seed_agent_templates", new=AsyncMock()), \
         patch("backend.api.algo.agent_engine.seed_agents", new=AsyncMock()), \
         patch("backend.api.algo.agent_engine.migrate_order_action_type_rename",
               new=order_migration_mock), \
         patch("backend.api.algo.template_registry.migrate_and_retire_agent_fragments", new=AsyncMock()), \
         patch("backend.api.algo.template_registry.REGISTRY.reload", new=AsyncMock()), \
         patch("backend.shared.helpers.settings.seed_settings", new=AsyncMock()), \
         patch("backend.api.algo.templates_seed.seed_templates", new=AsyncMock()), \
         patch("backend.api.algo.bracket_registry.migrate_order_templates_to_bracket_agents", new=AsyncMock()), \
         patch("backend.api.routes.watchlist.seed_global_pinned", new=AsyncMock()), \
         patch("backend.shared.helpers.alert_utils.refresh_alert_recipients", new=AsyncMock()):

        asyncio.run(dbmod.init_db())  # must not raise

    assert any("ADD COLUMN IF NOT EXISTS given JSONB" in s for s in tx_conn.executed)
    order_migration_mock.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════
#  4 — Agent.given model column
# ═══════════════════════════════════════════════════════════════════════

def test_agent_model_has_given_column_nullable_default_none():
    col = Agent.__table__.columns["given"]
    assert col.nullable is True
    agent = Agent(slug="x", name="x", conditions={}, events=[], actions=[])
    assert agent.given is None


# ═══════════════════════════════════════════════════════════════════════
#  5 — request/response struct wiring
# ═══════════════════════════════════════════════════════════════════════

class TestRequestResponseStructWiring:
    def test_agent_info_has_given_field_defaults_empty_list(self):
        from backend.api.routes.agents import AgentInfo
        info = AgentInfo(
            id=1, slug="x", name="x", long_name=None, description=None,
            conditions={}, events=[], actions=[], scope="total",
            schedule=None, cooldown_minutes=30, fire_at_time=None,
            status="active", last_triggered_at=None, trigger_count=0,
            last_error=None, is_system=False,
        )
        assert info.given == []

    def test_agent_create_request_accepts_given(self):
        from backend.api.routes.agents import AgentCreateRequest
        req = AgentCreateRequest(
            slug="x", name="x", conditions={}, events=[],
            given=[{"name": "p", "expr": "pnl@positions.total"}],
        )
        assert req.given == [{"name": "p", "expr": "pnl@positions.total"}]

    def test_agent_create_request_given_defaults_to_empty_list(self):
        from backend.api.routes.agents import AgentCreateRequest
        req = AgentCreateRequest(slug="x", name="x", conditions={}, events=[])
        assert req.given == []

    def test_agent_update_request_accepts_given(self):
        from backend.api.routes.agents import AgentUpdateRequest
        req = AgentUpdateRequest(given=[{"name": "p", "expr": "pnl@positions.total"}])
        assert req.given == [{"name": "p", "expr": "pnl@positions.total"}]

    def test_agent_update_request_given_defaults_to_none_meaning_unchanged(self):
        from backend.api.routes.agents import AgentUpdateRequest
        req = AgentUpdateRequest()
        assert req.given is None

    def test_update_agent_setattr_loop_includes_given(self):
        """Structural guard: the generic field-copy loop in update_agent
        must include 'given', or a client sending given=[...] on PUT
        would be silently dropped (same class of bug this whole file
        exists to close). `inspect.getsource` can't target the method
        directly — Litestar's `@put` decorator wraps it into a route-
        handler descriptor, not a plain function/method — so this reads
        the whole module's source text instead (same approach
        test_events_dispatch.py's module-level `_SRC` constant uses)."""
        from pathlib import Path
        src = Path("backend/api/routes/agents.py").read_text()
        def_idx = src.index("async def update_agent")
        next_def_idx = src.index("\n    async def ", def_idx + 10)
        body = src[def_idx:next_def_idx]
        assert "'given'" in body or '"given"' in body, (
            "update_agent's generic field-copy loop must include 'given'"
        )

    def test_create_agent_source_persists_given_onto_agent_row(self):
        from pathlib import Path
        src = Path("backend/api/routes/agents.py").read_text()
        def_idx = src.index("async def create_agent")
        next_def_idx = src.index("\n    def _age_apply_lifespan_fields", def_idx)
        body = src[def_idx:next_def_idx]
        assert "given=" in body, (
            "create_agent must pass given=... into the Agent(...) constructor"
        )

    def test_agent_to_info_reads_given_defensively(self):
        import backend.api.routes.agents as agents_mod
        src = inspect.getsource(agents_mod._agent_to_info)
        assert 'getattr(a, "given"' in src, (
            "_agent_to_info must read given via getattr (same defensive "
            "convention as every other optional field)"
        )

    def test_agent_to_info_handles_none_given_as_empty_list(self):
        from backend.api.routes.agents import _agent_to_info

        class _Row:
            id = 1
            slug = "x"
            name = "x"
            long_name = None
            description = None
            conditions = {}
            events = []
            actions = []
            scope = "total"
            schedule = None
            cooldown_minutes = 30
            fire_at_time = None
            status = "active"
            last_triggered_at = None
            trigger_count = 0
            last_error = None
            is_system = False
            given = None   # real DB row, pre-migration-backfill shape

        info = _agent_to_info(_Row())
        assert info.given == []
