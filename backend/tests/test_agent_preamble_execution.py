"""
Tests for the grammar-unification plan's Design item 0 — saved cycle-agent
`preamble` execution + the `agents.preamble`/`agents.preamble_fired_at`
additive columns + their request/response wiring.

Mirrors `test_agents_given_column_wiring.py`'s structure for the
persistence/wiring half, and `test_run_cycle_characterization.py`'s
mock-session convention for the engine-execution half.

Covers:
  1. `_migrate_agents_preamble` issues the right SQL for both columns.
  2. The step is wired into `init_db()` inside the `engine.begin()`
     transaction, after `_migrate_agents_given`.
  3. `Agent.preamble` / `Agent.preamble_fired_at` model columns exist,
     nullable, default to `None` on a bare row.
  4. `AgentCreateRequest`/`AgentUpdateRequest`/`AgentInfo` all accept and
     round-trip a `preamble` field; `create_agent`/`update_agent`
     actually persist it onto the ORM row; `_agent_to_info` reads it
     back defensively.
  5. `_age_set_status` resets `preamble_fired_at` to None on a genuine
     inactive/paused -> active transition, but NOT on an already-active
     -> active no-op (re-clicking Activate must not re-arm a live
     agent's one-shot preamble entries).
  6. `_cycle_run_preamble`'s execution-semantics split — THE DIRECT
     REGRESSION TEST for the audit's confirmed P1 finding:
       - `log`/`help`/`alert` entries run on EVERY call (simulated poll
         cycle), not just the first.
       - A side-effecting entry (`order`) fires EXACTLY ONCE across
         multiple simulated poll cycles for the same agent, never once
         per cycle.
       - `bracket` logs a WARNING and is skipped, never crashes, never
         silently does nothing without a trace.
       - sim-mode isolation: a simulator run never touches the real
         `preamble_fired_at` DB column.
  7. Producer-log fallback REGRESSION LOCK — an agent with a populated
     `events` list (the real shape every seeded `loss-*` builtin agent
     carries) still gets the automatic `_v2_send_rich_alert()` call,
     completely unchanged. This file does NOT implement the plan's
     literal "skip the rich alert when `agent.events` is non-empty"
     check — see the test's own docstring for why that heuristic was
     rejected (it would silently disable the rich kind/threshold alert
     table for every currently-live `loss-*` agent, which ships with a
     non-empty `events` list by seed default, unrelated to any DO-level
     `alert(...)` call).
"""
from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.api.database as dbmod
from backend.api.models import Agent


# ═══════════════════════════════════════════════════════════════════════
#  1 — migration statement shape
# ═══════════════════════════════════════════════════════════════════════

class _FakeTxConn:
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, stmt, *a, **k):
        self.executed.append(str(stmt))
        return MagicMock(scalar_one_or_none=lambda: None)

    async def run_sync(self, fn, *a, **k):
        return None


def test_migrate_agents_preamble_statement_shape():
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_agents_preamble(conn))

    joined = "\n".join(conn.executed)
    assert "ALTER TABLE agents ADD COLUMN IF NOT EXISTS preamble JSONB" in joined
    assert "ALTER TABLE agents ADD COLUMN IF NOT EXISTS preamble_fired_at TIMESTAMPTZ" in joined
    assert any("lock_timeout" in s for s in conn.executed), (
        "no explicit lock_timeout set on the ADD COLUMN step"
    )
    assert not any("CONCURRENTLY" in s for s in conn.executed), (
        "CONCURRENTLY statement found inside a transactional ADD COLUMN "
        "step — this would crash init_db()."
    )


# ═══════════════════════════════════════════════════════════════════════
#  2 — wired inside the engine.begin() block, after given migration
# ═══════════════════════════════════════════════════════════════════════

def test_init_db_calls_preamble_migration_inside_begin_block_after_given():
    src = inspect.getsource(dbmod.init_db)
    lines = src.splitlines()
    begin_idx = next(
        i for i, l in enumerate(lines) if "async with engine.begin() as conn:" in l
    )
    begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

    given_idx = next(
        i for i, l in enumerate(lines)
        if "_migrate_agents_given(conn)" in l and "async def" not in l
    )
    preamble_idx = next(
        i for i, l in enumerate(lines)
        if "_migrate_agents_preamble(conn)" in l and "async def" not in l
    )
    preamble_indent = len(lines[preamble_idx]) - len(lines[preamble_idx].lstrip())

    assert preamble_idx > begin_idx and preamble_indent > begin_indent, (
        "_migrate_agents_preamble(conn) must be called INSIDE the "
        "engine.begin() transactional block."
    )
    assert preamble_idx > given_idx, (
        "_migrate_agents_preamble(conn) must run after "
        "_migrate_agents_given(conn)"
    )


# ═══════════════════════════════════════════════════════════════════════
#  3 — Agent.preamble / Agent.preamble_fired_at model columns
# ═══════════════════════════════════════════════════════════════════════

def test_agent_model_has_preamble_columns_nullable_default_none():
    col = Agent.__table__.columns["preamble"]
    assert col.nullable is True
    col2 = Agent.__table__.columns["preamble_fired_at"]
    assert col2.nullable is True
    agent = Agent(slug="x", name="x", conditions={}, events=[], actions=[])
    assert agent.preamble is None
    assert agent.preamble_fired_at is None


# ═══════════════════════════════════════════════════════════════════════
#  3b — Design item 2's preamble_alert_last_fired_at + item 0.C's
#       has_explicit_alert — migrations + model columns
# ═══════════════════════════════════════════════════════════════════════

def test_migrate_agents_preamble_alert_cooldown_statement_shape():
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_agents_preamble_alert_cooldown(conn))
    joined = "\n".join(conn.executed)
    assert "ALTER TABLE agents ADD COLUMN IF NOT EXISTS preamble_alert_last_fired_at TIMESTAMPTZ" in joined
    assert any("lock_timeout" in s for s in conn.executed)
    assert not any("CONCURRENTLY" in s for s in conn.executed)


def test_migrate_agents_has_explicit_alert_statement_shape():
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_agents_has_explicit_alert(conn))
    joined = "\n".join(conn.executed)
    assert (
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS has_explicit_alert "
        "BOOLEAN NOT NULL DEFAULT false" in joined
    ), "must carry a DDL-level DEFAULT — a bare NOT NULL would fail against an already-populated table"
    assert any("lock_timeout" in s for s in conn.executed)
    assert not any("CONCURRENTLY" in s for s in conn.executed)


def test_init_db_calls_both_new_migrations_after_preamble_in_order():
    src = inspect.getsource(dbmod.init_db)
    lines = src.splitlines()
    begin_idx = next(
        i for i, l in enumerate(lines) if "async with engine.begin() as conn:" in l
    )
    begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

    def _idx(marker):
        i = next(j for j, l in enumerate(lines) if marker in l and "async def" not in l)
        return i, len(lines[i]) - len(lines[i].lstrip())

    preamble_idx, _ = _idx("_migrate_agents_preamble(conn)")
    cooldown_idx, cooldown_indent = _idx("_migrate_agents_preamble_alert_cooldown(conn)")
    explicit_idx, explicit_indent = _idx("_migrate_agents_has_explicit_alert(conn)")

    assert cooldown_indent > begin_indent and explicit_indent > begin_indent, (
        "both new migrations must run INSIDE the engine.begin() transactional block"
    )
    assert cooldown_idx > preamble_idx, (
        "_migrate_agents_preamble_alert_cooldown(conn) must run after _migrate_agents_preamble(conn)"
    )
    assert explicit_idx > cooldown_idx, (
        "_migrate_agents_has_explicit_alert(conn) must run after the cooldown migration"
    )


def test_agent_model_has_preamble_alert_cooldown_column_nullable_default_none():
    col = Agent.__table__.columns["preamble_alert_last_fired_at"]
    assert col.nullable is True
    agent = Agent(slug="x", name="x", conditions={}, events=[], actions=[])
    assert agent.preamble_alert_last_fired_at is None


def test_agent_model_has_explicit_alert_column_not_null_default_false():
    """`default=False` is a Python-level default SQLAlchemy applies at
    INSERT/flush time, not at bare `Agent(...)` construction — so a
    freshly-constructed, never-flushed instance reads back `None` for
    this attribute (same as every other column with a Python-level
    `default=` and no explicit value passed). This test checks the
    column's declared configuration directly instead of a pre-flush
    instance's attribute value, which would be a false signal either
    way."""
    col = Agent.__table__.columns["has_explicit_alert"]
    assert col.nullable is False
    assert col.default is not None and col.default.arg is False
    assert col.server_default is not None


# ═══════════════════════════════════════════════════════════════════════
#  4 — request/response struct wiring + persistence
# ═══════════════════════════════════════════════════════════════════════

class TestRequestResponseStructWiring:
    def test_agent_info_has_preamble_field_defaults_empty_list(self):
        from backend.api.routes.agents import AgentInfo
        info = AgentInfo(
            id=1, slug="x", name="x", long_name=None, description=None,
            conditions={}, events=[], actions=[], scope="total",
            schedule=None, cooldown_minutes=30, fire_at_time=None,
            status="active", last_triggered_at=None, trigger_count=0,
            last_error=None, is_system=False,
        )
        assert info.preamble == []

    def test_agent_create_request_accepts_preamble(self):
        from backend.api.routes.agents import AgentCreateRequest
        req = AgentCreateRequest(
            slug="x", name="x", conditions={}, events=[],
            preamble=[{"type": "log", "params": {"message": "hi"}}],
        )
        assert req.preamble == [{"type": "log", "params": {"message": "hi"}}]

    def test_agent_create_request_preamble_defaults_to_empty_list(self):
        from backend.api.routes.agents import AgentCreateRequest
        req = AgentCreateRequest(slug="x", name="x", conditions={}, events=[])
        assert req.preamble == []

    def test_agent_update_request_accepts_preamble(self):
        from backend.api.routes.agents import AgentUpdateRequest
        req = AgentUpdateRequest(preamble=[{"type": "help", "params": {"name": "pnl"}}])
        assert req.preamble == [{"type": "help", "params": {"name": "pnl"}}]

    def test_agent_update_request_preamble_defaults_to_none_meaning_unchanged(self):
        from backend.api.routes.agents import AgentUpdateRequest
        req = AgentUpdateRequest()
        assert req.preamble is None

    def test_agent_create_request_decodes_preamble_from_json(self):
        import msgspec
        from backend.api.routes.agents import AgentCreateRequest
        body = json.dumps({
            "slug": "x", "name": "X", "conditions": {}, "events": [],
            "preamble": [{"type": "order", "params": {"account": "ZG0790"}}],
        }).encode()
        decoded = msgspec.json.decode(body, type=AgentCreateRequest)
        assert decoded.preamble == [{"type": "order", "params": {"account": "ZG0790"}}]

    def test_update_agent_setattr_loop_includes_preamble(self):
        """Structural guard — same class of bug the 'given' column's own
        wiring test file exists to close: a msgspec.Struct silently
        drops an unknown JSON key on decode, and the generic field-copy
        loop in update_agent must explicitly name 'preamble' or a client
        PUT would be silently dropped even after decode succeeds."""
        from pathlib import Path
        src = Path("backend/api/routes/agents.py").read_text()
        def_idx = src.index("async def update_agent")
        next_def_idx = src.index("\n    async def ", def_idx + 10)
        body = src[def_idx:next_def_idx]
        assert "'preamble'" in body or '"preamble"' in body, (
            "update_agent's generic field-copy loop must include 'preamble'"
        )

    def test_create_agent_source_persists_preamble_onto_agent_row(self):
        from pathlib import Path
        src = Path("backend/api/routes/agents.py").read_text()
        def_idx = src.index("async def create_agent")
        next_def_idx = src.index("\n    def _age_apply_lifespan_fields", def_idx)
        body = src[def_idx:next_def_idx]
        assert "preamble=" in body, (
            "create_agent must pass preamble=... into the Agent(...) constructor"
        )

    def test_agent_to_info_reads_preamble_defensively(self):
        import backend.api.routes.agents as agents_mod
        src = inspect.getsource(agents_mod._agent_to_info)
        assert 'getattr(a, "preamble"' in src, (
            "_agent_to_info must read preamble via getattr (same defensive "
            "convention as every other optional field)"
        )

    def test_agent_to_info_handles_none_preamble_as_empty_list(self):
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
            given = None
            preamble = None   # real DB row, pre-migration-backfill shape

        info = _agent_to_info(_Row())
        assert info.preamble == []


class TestHasExplicitAlertStructWiring:
    """Design item 0.C's `has_explicit_alert` field — same round-trip
    coverage as `preamble` above."""

    def test_agent_info_has_explicit_alert_field_defaults_false(self):
        from backend.api.routes.agents import AgentInfo
        info = AgentInfo(
            id=1, slug="x", name="x", long_name=None, description=None,
            conditions={}, events=[], actions=[], scope="total",
            schedule=None, cooldown_minutes=30, fire_at_time=None,
            status="active", last_triggered_at=None, trigger_count=0,
            last_error=None, is_system=False,
        )
        assert info.has_explicit_alert is False

    def test_agent_create_request_accepts_has_explicit_alert(self):
        from backend.api.routes.agents import AgentCreateRequest
        req = AgentCreateRequest(
            slug="x", name="x", conditions={}, events=[], has_explicit_alert=True,
        )
        assert req.has_explicit_alert is True

    def test_agent_create_request_has_explicit_alert_defaults_false(self):
        from backend.api.routes.agents import AgentCreateRequest
        req = AgentCreateRequest(slug="x", name="x", conditions={}, events=[])
        assert req.has_explicit_alert is False

    def test_agent_update_request_accepts_true_and_false_and_none(self):
        from backend.api.routes.agents import AgentUpdateRequest
        assert AgentUpdateRequest(has_explicit_alert=True).has_explicit_alert is True
        assert AgentUpdateRequest(has_explicit_alert=False).has_explicit_alert is False
        assert AgentUpdateRequest().has_explicit_alert is None, (
            "None must mean 'leave unchanged' — a bool default of False "
            "here would silently clear an already-True flag on every "
            "PUT that doesn't mention it"
        )

    def test_update_agent_setattr_loop_includes_has_explicit_alert(self):
        from pathlib import Path
        src = Path("backend/api/routes/agents.py").read_text()
        def_idx = src.index("async def update_agent")
        next_def_idx = src.index("\n    async def ", def_idx + 10)
        body = src[def_idx:next_def_idx]
        assert "'has_explicit_alert'" in body or '"has_explicit_alert"' in body

    def test_create_agent_source_persists_has_explicit_alert_onto_agent_row(self):
        from pathlib import Path
        src = Path("backend/api/routes/agents.py").read_text()
        def_idx = src.index("async def create_agent")
        next_def_idx = src.index("\n    def _age_apply_lifespan_fields", def_idx)
        body = src[def_idx:next_def_idx]
        assert "has_explicit_alert=" in body

    def test_agent_to_info_reads_has_explicit_alert_defensively(self):
        import backend.api.routes.agents as agents_mod
        src = inspect.getsource(agents_mod._agent_to_info)
        assert 'getattr(a, "has_explicit_alert"' in src

    def test_agent_to_info_defaults_missing_has_explicit_alert_to_false(self):
        """A real DB row predating this migration (attribute absent
        entirely on a plain stand-in) must read back False, not raise."""
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
            given = None
            preamble = None
            # has_explicit_alert deliberately absent entirely

        info = _agent_to_info(_Row())
        assert info.has_explicit_alert is False

    @pytest.mark.asyncio
    async def test_update_agent_update_can_set_has_explicit_alert_true_and_false(self):
        """End-to-end through the real route handler — a PUT with
        has_explicit_alert=True sets it; a later PUT with
        has_explicit_alert=False clears it; a PUT that omits the field
        entirely leaves it unchanged."""
        from backend.api.routes.agents import AgentController, AgentUpdateRequest

        agent = _FakeAgentRow(
            has_explicit_alert=False, kind="cycle", is_system=False,
            actions=[], events=[], conditions={},
        )
        session_factory, _ = _mock_session(existing=agent)
        controller = AgentController.__new__(AgentController)
        update_fn = AgentController.update_agent.fn

        with patch("backend.api.routes.agents.async_session", session_factory):
            await update_fn(controller, "t-agent", AgentUpdateRequest(has_explicit_alert=True))
        assert agent.has_explicit_alert is True

        with patch("backend.api.routes.agents.async_session", session_factory):
            await update_fn(controller, "t-agent", AgentUpdateRequest(has_explicit_alert=False))
        assert agent.has_explicit_alert is False

        with patch("backend.api.routes.agents.async_session", session_factory):
            await update_fn(controller, "t-agent", AgentUpdateRequest(name="renamed only"))
        assert agent.has_explicit_alert is False, "omitted field must stay unchanged"


# ═══════════════════════════════════════════════════════════════════════
#  5 — _age_set_status: activation reset semantics
# ═══════════════════════════════════════════════════════════════════════

class _FakeAgentRow:
    """Plain settable object standing in for the Agent ORM row — mirrors
    test_agent_kind_tier_topic.py's own stand-in."""
    def __init__(self, **kw):
        self.id = 1
        self.slug = "t-agent"
        self.name = "Test"
        self.status = "inactive"
        self.preamble = []
        self.preamble_fired_at = None
        for k, v in kw.items():
            setattr(self, k, v)


def _mock_session(existing=None):
    session = MagicMock()
    session.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=existing)
    session.execute = AsyncMock(return_value=result)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), session


@pytest.mark.asyncio
async def test_age_set_status_resets_preamble_fired_at_on_real_activation():
    from backend.api.routes.agents import AgentController
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    agent = _FakeAgentRow(status="inactive", preamble_fired_at=t, preamble_alert_last_fired_at=t)
    session_factory, _ = _mock_session(existing=agent)
    controller = AgentController.__new__(AgentController)

    with patch("backend.api.routes.agents.async_session", session_factory):
        await controller._age_set_status("t-agent", "active")

    assert agent.status == "active"
    assert agent.preamble_fired_at is None, (
        "a real inactive->active transition must re-arm the once-per-"
        "activation preamble marker"
    )
    assert agent.preamble_alert_last_fired_at is None, (
        "the SAME real activation transition must also re-arm the "
        "alert/log cooldown clock (Design item 2) — a fresh activation "
        "alerts immediately, not stuck behind a prior activation's cooldown"
    )


@pytest.mark.asyncio
async def test_age_set_status_does_not_reset_when_already_active():
    from backend.api.routes.agents import AgentController
    fired_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    agent = _FakeAgentRow(status="active", preamble_fired_at=fired_at, preamble_alert_last_fired_at=fired_at)
    session_factory, _ = _mock_session(existing=agent)
    controller = AgentController.__new__(AgentController)

    with patch("backend.api.routes.agents.async_session", session_factory):
        await controller._age_set_status("t-agent", "active")

    assert agent.status == "active"
    assert agent.preamble_fired_at == fired_at, (
        "re-clicking Activate on an already-active agent must NOT re-arm "
        "the one-shot — that would re-fire a real broker order"
    )
    assert agent.preamble_alert_last_fired_at == fired_at


@pytest.mark.asyncio
async def test_age_set_status_deactivate_does_not_touch_preamble_fired_at():
    from backend.api.routes.agents import AgentController
    fired_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    agent = _FakeAgentRow(status="active", preamble_fired_at=fired_at, preamble_alert_last_fired_at=fired_at)
    session_factory, _ = _mock_session(existing=agent)
    controller = AgentController.__new__(AgentController)

    with patch("backend.api.routes.agents.async_session", session_factory):
        await controller._age_set_status("t-agent", "inactive")

    assert agent.status == "inactive"
    assert agent.preamble_fired_at == fired_at
    assert agent.preamble_alert_last_fired_at == fired_at


@pytest.mark.asyncio
async def test_age_set_status_cooldown_to_active_does_not_reset_preamble_fired_at():
    """`cooldown` is a LIVE, actively-polled state (`_cycle_load_agents`
    selects status IN ('active', 'cooldown')) — clicking Activate on a
    cooldown agent (e.g. from an admin UI that always calls activate on
    submit) must NOT re-arm the once-per-activation marker, or it would
    re-fire a real broker order on an agent that was already running."""
    from backend.api.routes.agents import AgentController
    fired_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    agent = _FakeAgentRow(status="cooldown", preamble_fired_at=fired_at, preamble_alert_last_fired_at=fired_at)
    session_factory, _ = _mock_session(existing=agent)
    controller = AgentController.__new__(AgentController)

    with patch("backend.api.routes.agents.async_session", session_factory):
        await controller._age_set_status("t-agent", "active")

    assert agent.status == "active"
    assert agent.preamble_fired_at == fired_at, (
        "cooldown->active must NOT reset preamble_fired_at — cooldown is "
        "a live state, not a real re-activation"
    )
    assert agent.preamble_alert_last_fired_at == fired_at


# ═══════════════════════════════════════════════════════════════════════
#  6 — _cycle_run_preamble execution semantics (the P1 regression test)
# ═══════════════════════════════════════════════════════════════════════

class _PreambleAgent:
    """Minimal Agent-row stand-in for the engine-level tests — carries
    only what _cycle_run_preamble/_cycle_claim_preamble_activation read."""
    def __init__(self, id=1, slug="pa", name="Preamble Agent", preamble=None,
                 preamble_fired_at=None, kind="cycle", cooldown_minutes=30,
                 preamble_alert_last_fired_at=None):
        self.id = id
        self.slug = slug
        self.name = name
        self.preamble = preamble or []
        self.preamble_fired_at = preamble_fired_at
        self.preamble_alert_last_fired_at = preamble_alert_last_fired_at
        self.kind = kind
        self.events = []
        self.actions = []
        self.tier = "medium"
        self.topic = "general"
        self.cooldown_minutes = cooldown_minutes


def _claim_session_factory(state: dict):
    """Stateful fake `async_session()` for the atomic claim UPDATE —
    first call "wins" the claim (rowcount=1), every subsequent call sees
    it already claimed (rowcount=0). Mirrors the real conditional
    `WHERE preamble_fired_at IS NULL` UPDATE's observable behavior
    without touching a real DB."""
    def _factory():
        session = AsyncMock()

        async def _execute(*a, **kw):
            res = MagicMock()
            if not state.get("claimed"):
                state["claimed"] = True
                res.rowcount = 1
            else:
                res.rowcount = 0
            return res

        session.execute = _execute
        session.commit = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm
    return _factory


@pytest.mark.asyncio
async def test_help_and_debug_log_run_on_every_simulated_poll_cycle_uncapped():
    """`help`, and `log(level="debug")` (the debug(x) sugar form), fire
    on EVERY call, completely uncapped — contrast case for both the
    once-per-activation test below AND the cooldown-gated alert/log
    test (Design item 2). Neither entry type is ever cooldown-gated
    (see `_cycle_preamble_entry_is_cooldown_gated`'s docstring: `help`
    has no notification channel to rate-limit; `debug` is structurally
    exempt from the whole alert pipeline already), so no once-per-
    activation OR cooldown DB claim is ever attempted (async_session
    patched to raise if called at all)."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "log", "params": {"level": "debug", "message": "tick"}},
        {"type": "help", "params": {"name": "pnl"}},
    ])

    def _session_should_not_be_called():
        raise AssertionError(
            "a preamble with only help/debug(x) entries must never "
            "attempt any DB claim"
        )

    with (
        patch.object(agent_engine, "execute", new=AsyncMock()) as execute_mock,
        patch.object(agent_engine, "async_session", side_effect=_session_should_not_be_called),
    ):
        for _ in range(3):
            await agent_engine._cycle_run_preamble(
                agent, {}, datetime.now(timezone.utc),
                sim_mode=False, alert_state={},
            )

    assert execute_mock.await_count == 6, "debug-log + help, 3 cycles => 6 execute() calls"


@pytest.mark.asyncio
async def test_preamble_alert_respects_agent_cooldown_minutes():
    """Design item 2 — preamble `alert(...)` is NOT uncapped: it fires
    on cycle 1, does NOT fire again on cycle 2 if cycle 2 is within the
    agent's own `cooldown_minutes` window, and fires again once the
    window has elapsed. Uses the patchable `_preamble_utcnow()` to
    simulate elapsed time without a real sleep."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "alert", "events": [{"channel": "telegram", "enabled": True}]},
    ])
    agent.cooldown_minutes = 30
    t0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=timezone.utc)
    claim_state: dict = {}

    def _fake_now():
        return claim_state["now"]

    def _always_succeeds_session_factory():
        """Every call that actually REACHES the DB must succeed — the
        Python pre-check in `_cycle_preamble_alert_cooldown_ready`
        (reading `agent.preamble_alert_last_fired_at` directly, updated
        in-memory on each successful claim) is what filters out cycle
        2 (still inside the window) before the DB is ever touched; by
        the time cycle 1 or cycle 3 reaches here, eligibility is already
        confirmed."""
        session = AsyncMock()
        res = MagicMock()
        res.rowcount = 1
        session.execute = AsyncMock(return_value=res)
        session.commit = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    with (
        patch.object(agent_engine, "dispatch", new=AsyncMock()) as dispatch_mock,
        patch.object(agent_engine, "async_session", side_effect=_always_succeeds_session_factory),
        patch.object(agent_engine, "_preamble_utcnow", side_effect=_fake_now),
    ):
        claim_state["now"] = t0
        await agent_engine._cycle_run_preamble(
            agent, {}, t0, sim_mode=False, alert_state={},
        )
        assert dispatch_mock.await_count == 1, "cycle 1 (first ever) must fire"

        claim_state["now"] = t0 + timedelta(minutes=5)
        await agent_engine._cycle_run_preamble(
            agent, {}, claim_state["now"], sim_mode=False, alert_state={},
        )
        assert dispatch_mock.await_count == 1, "cycle 2, still inside the 30-min window, must NOT fire again"

        claim_state["now"] = t0 + timedelta(minutes=31)
        await agent_engine._cycle_run_preamble(
            agent, {}, claim_state["now"], sim_mode=False, alert_state={},
        )
        assert dispatch_mock.await_count == 2, "cycle 3, past the 30-min window, must fire again"


@pytest.mark.asyncio
async def test_preamble_non_debug_log_respects_agent_cooldown_minutes():
    """The coordinator's spec explicitly covers `log` too, not just
    `alert` — a hand-typed `log(level=warning, ...)` entry (no `debug`
    level) must respect the same windowed cooldown."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "log", "params": {"level": "warning", "message": "margin tight"}},
    ])
    agent.cooldown_minutes = 30
    t0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=timezone.utc)
    claim_state: dict = {}

    def _fake_now():
        return claim_state["now"]

    def _always_succeeds_session_factory():
        session = AsyncMock()
        res = MagicMock()
        res.rowcount = 1
        session.execute = AsyncMock(return_value=res)
        session.commit = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    with (
        patch.object(agent_engine, "execute", new=AsyncMock()) as execute_mock,
        patch.object(agent_engine, "async_session", side_effect=_always_succeeds_session_factory),
        patch.object(agent_engine, "_preamble_utcnow", side_effect=_fake_now),
    ):
        claim_state["now"] = t0
        await agent_engine._cycle_run_preamble(agent, {}, t0, sim_mode=False, alert_state={})
        assert execute_mock.await_count == 1, "cycle 1 must fire"

        claim_state["now"] = t0 + timedelta(minutes=5)
        await agent_engine._cycle_run_preamble(agent, {}, claim_state["now"], sim_mode=False, alert_state={})
        assert execute_mock.await_count == 1, "cycle 2, inside the window, must NOT fire again"

        claim_state["now"] = t0 + timedelta(minutes=31)
        await agent_engine._cycle_run_preamble(agent, {}, claim_state["now"], sim_mode=False, alert_state={})
        assert execute_mock.await_count == 2, "cycle 3, past the window, must fire again"


@pytest.mark.asyncio
async def test_cooldown_claim_failure_fails_safe_help_still_runs():
    """Same fail-safe contract as the once-per-activation claim's own
    failure test: a DB blip on the alert/log cooldown claim must never
    raise, the alert must simply be skipped this cycle, and `help`
    (ungated) in the SAME preamble must still run."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "alert", "events": [{"channel": "telegram", "enabled": True}]},
        {"type": "help", "params": {"name": "pnl"}},
    ])

    def _session_raises():
        raise RuntimeError("DB blip")

    with (
        patch.object(agent_engine, "execute", new=AsyncMock()) as execute_mock,
        patch.object(agent_engine, "dispatch", new=AsyncMock()) as dispatch_mock,
        patch.object(agent_engine, "async_session", side_effect=_session_raises),
    ):
        await agent_engine._cycle_run_preamble(
            agent, {}, datetime.now(timezone.utc),
            sim_mode=False, alert_state={},
        )  # must not raise

    dispatch_mock.assert_not_awaited(), "alert must be skipped, not fired, on a claim failure"
    assert execute_mock.await_count == 1, "help must still run — it's never cooldown-gated"


@pytest.mark.asyncio
async def test_preamble_alert_cooldown_sim_mode_never_touches_real_db_column():
    """Sim mode tracks the cooldown window on an isolated alert_state
    set, never the real `preamble_alert_last_fired_at` DB column —
    async_session patched to raise if called at all."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "alert", "events": [{"channel": "telegram", "enabled": True}]},
    ])
    alert_state: dict = {}

    def _session_should_not_be_called():
        raise AssertionError("sim_mode must never touch the real DB column")

    with (
        patch.object(agent_engine, "dispatch", new=AsyncMock()) as dispatch_mock,
        patch.object(agent_engine, "async_session", side_effect=_session_should_not_be_called),
    ):
        await agent_engine._cycle_run_preamble(
            agent, {}, datetime.now(timezone.utc),
            sim_mode=True, alert_state=alert_state,
        )

    dispatch_mock.assert_awaited_once()
    assert agent.preamble_alert_last_fired_at is None, "the real column must remain untouched in sim mode"
    assert agent.id in alert_state.get("_sim_preamble_alert_cooldown", {})


@pytest.mark.asyncio
async def test_bypass_suppression_always_fires_preamble_alert():
    """bypass_suppression (isolated single-agent "Run in Simulator",
    and the manual Test Fire path) must fire the alert every call, same
    exemption `_v2_apply_escalation_gate` already grants."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "alert", "events": [{"channel": "telegram", "enabled": True}]},
    ])

    with (
        patch.object(agent_engine, "dispatch", new=AsyncMock()) as dispatch_mock,
        patch.object(agent_engine, "async_session", side_effect=lambda: pytest.fail(
            "bypass_suppression must short-circuit before any DB claim")),
    ):
        for _ in range(3):
            await agent_engine._cycle_run_preamble(
                agent, {}, datetime.now(timezone.utc),
                sim_mode=False, alert_state={}, bypass_suppression=True,
            )

    assert dispatch_mock.await_count == 3, "every call must fire when bypass_suppression=True"


@pytest.mark.asyncio
async def test_preamble_alert_suppressed_in_sim_when_notify_during_run_off():
    """Defense-in-depth, not a fix for a confirmed real-message leak:
    telegram/email/ntfy are `_RECORDED_CHANNELS` (events.py) — they
    never reach dispatch()'s direct-send branch at all; they're queued
    via `_emit_breach_record` into event_agents.py's own independent
    `dispatch_rows()` sim-notify gate, which already correctly
    suppresses a sim-mode breach record downstream regardless of
    `skip_channels` here. This test locks the SAME belt-and-suspenders
    pattern `_v2_send_rich_alert` already uses (double-gating, not a
    single point of truth) — `skip_channels` must reflect
    `_ae_sim_notify_suppressed(agent, sim_mode)` when a preamble
    `alert(...)` fires during a sim run."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "alert", "events": [{"channel": "telegram", "enabled": True}]},
    ])

    with (
        patch.object(agent_engine, "dispatch", new=AsyncMock()) as dispatch_mock,
        patch.object(agent_engine, "_ae_sim_notify_suppressed", return_value=True),
    ):
        await agent_engine._cycle_run_preamble(
            agent, {}, datetime.now(timezone.utc),
            sim_mode=True, alert_state={},
        )

    dispatch_mock.assert_awaited_once()
    _, kwargs = dispatch_mock.call_args
    assert kwargs.get("skip_channels") == frozenset({"telegram", "email", "ntfy"})


@pytest.mark.asyncio
async def test_side_effecting_order_fires_exactly_once_across_multiple_poll_cycles():
    """THE DIRECT REGRESSION TEST for the audit's confirmed P1 finding:
    a saved cycle agent with order(...) in its preamble must place a
    real broker order exactly once at activation, never once per poll
    cycle / cooldown window. Simulates run_cycle calling
    _cycle_run_preamble repeatedly for the SAME agent across several
    ticks."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "order", "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "BUY", "qty": 150}},
    ])
    claim_state: dict = {}

    with (
        patch.object(agent_engine, "execute", new=AsyncMock()) as execute_mock,
        patch.object(agent_engine, "async_session", side_effect=_claim_session_factory(claim_state)),
    ):
        for _ in range(5):
            await agent_engine._cycle_run_preamble(
                agent, {}, datetime.now(timezone.utc),
                sim_mode=False, alert_state={},
            )

    assert execute_mock.await_count == 1, (
        f"order(...) in the preamble must fire exactly once across 5 "
        f"simulated poll cycles, fired {execute_mock.await_count} times"
    )


@pytest.mark.asyncio
async def test_mixed_preamble_every_cycle_and_once_entries_are_independent():
    """A preamble mixing a diagnostic entry (log) with a side-effecting
    one (close_position) — the diagnostic entry must keep firing every
    cycle even after the side-effecting one has already fired and is
    being skipped."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "log", "params": {"level": "debug", "message": "checking"}},
        {"type": "close_position", "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT"}},
    ])
    claim_state: dict = {}

    calls: list[str] = []

    async def _fake_execute(agent_, actions_, ctx_):
        calls.append(actions_[0]["type"])

    with (
        patch.object(agent_engine, "execute", side_effect=_fake_execute),
        patch.object(agent_engine, "async_session", side_effect=_claim_session_factory(claim_state)),
    ):
        for _ in range(3):
            await agent_engine._cycle_run_preamble(
                agent, {}, datetime.now(timezone.utc),
                sim_mode=False, alert_state={},
            )

    assert calls.count("log") == 3, "log must fire every cycle"
    assert calls.count("close_position") == 1, "close_position must fire exactly once"


@pytest.mark.asyncio
async def test_bracket_preamble_entry_logs_warning_and_never_dispatches():
    """`bracket` has no registered action-dispatch handler anywhere —
    confirmed by grep against _LIVE_ACTION_HANDLERS/_NOOP_ACTION_HANDLERS.
    It must log a clear WARNING and skip, never crash, never silently
    do nothing without a trace."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "bracket", "params": {"applies_to": "buy_any", "tp_pct": 30}},
    ])
    claim_state: dict = {}

    with (
        patch.object(agent_engine, "execute", new=AsyncMock()) as execute_mock,
        patch.object(agent_engine, "async_session", side_effect=_claim_session_factory(claim_state)),
        patch.object(agent_engine, "logger") as logger_mock,
    ):
        await agent_engine._cycle_run_preamble(
            agent, {}, datetime.now(timezone.utc),
            sim_mode=False, alert_state={},
        )

    execute_mock.assert_not_awaited()
    assert logger_mock.warning.called, (
        "bracket preamble entry must log an explicit WARNING, not fail silently"
    )
    warning_text = str(logger_mock.warning.call_args)
    assert "bracket" in warning_text.lower()


@pytest.mark.asyncio
async def test_bracket_preamble_entry_never_raises():
    """Never crashes run_cycle for other agents, even if something about
    the entry is malformed."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[{"type": "bracket"}])  # no params key at all
    claim_state: dict = {}

    with (
        patch.object(agent_engine, "execute", new=AsyncMock()),
        patch.object(agent_engine, "async_session", side_effect=_claim_session_factory(claim_state)),
    ):
        await agent_engine._cycle_run_preamble(
            agent, {}, datetime.now(timezone.utc),
            sim_mode=False, alert_state={},
        )  # must not raise


@pytest.mark.asyncio
async def test_sim_mode_never_touches_real_preamble_fired_at_column():
    """A simulator run must never consume a live agent's real one-shot —
    tracked instead on an isolated alert_state set, never via a real DB
    write. async_session is patched to raise if called at all."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "order", "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT"}},
    ])
    alert_state: dict = {}

    def _session_should_not_be_called():
        raise AssertionError("sim_mode must never touch the real DB column")

    with (
        patch.object(agent_engine, "execute", new=AsyncMock()) as execute_mock,
        patch.object(agent_engine, "async_session", side_effect=_session_should_not_be_called),
    ):
        for _ in range(3):
            await agent_engine._cycle_run_preamble(
                agent, {}, datetime.now(timezone.utc),
                sim_mode=True, alert_state=alert_state,
            )

    assert execute_mock.await_count == 1, "sim mode still fires exactly once per isolated run"
    assert agent.preamble_fired_at is None, "the real column must remain untouched in sim mode"
    assert agent.id in alert_state.get("_sim_preamble_fired", set())


@pytest.mark.asyncio
async def test_claim_failure_fails_safe_does_not_abort_tick_or_fire_order():
    """run_cycle's per-agent loop has NO surrounding try/except (same
    contract _cycle_evaluate_given_bindings documents) — a raised
    exception out of the atomic claim UPDATE must never escape
    _cycle_run_preamble, or it would abort evaluation for every agent
    still queued this tick. The order must NOT fire this cycle (fail
    safe, not fail loud); the every-cycle `log` entry in the SAME
    preamble must still run."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "log", "params": {"level": "debug", "message": "still runs"}},
        {"type": "order", "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT"}},
    ])

    def _session_raises():
        raise RuntimeError("DB blip")

    with (
        patch.object(agent_engine, "execute", new=AsyncMock()) as execute_mock,
        patch.object(agent_engine, "async_session", side_effect=_session_raises),
    ):
        await agent_engine._cycle_run_preamble(
            agent, {}, datetime.now(timezone.utc),
            sim_mode=False, alert_state={},
        )  # must not raise

    # Only the debug-log entry fired (once); the order did not. The
    # debug-log is exempt from both the once-bucket claim AND the
    # alert/log cooldown claim, so it's unaffected by _session_raises.
    assert execute_mock.await_count == 1
    assert execute_mock.await_args_list[0].args[1] == [
        {"type": "log", "params": {"level": "debug", "message": "still runs"}}
    ]


@pytest.mark.asyncio
async def test_preamble_fires_through_the_real_ae_cycle_eval_and_buffer_call_site():
    """Wiring guard, distinct from the direct _cycle_run_preamble tests
    above: every other test in this file calls _cycle_run_preamble
    directly, so none of them would fail if the real call site in
    _ae_cycle_eval_and_buffer were ever deleted or moved below the
    `if in_cooldown: return` guard. This test goes through
    _ae_cycle_eval_and_buffer itself — the actual function run_cycle
    calls per agent per tick — with in_cooldown=True (an agent that's
    mid-cooldown from its own condition firing must still run its
    preamble this tick) and _cycle_evaluate_agent patched to a no-match
    result so only the preamble path is exercised.

    The agent carries a real DO action (`close_position`), making it a
    MIXED preamble+WHEN/DO agent, not preamble-only — this deliberately
    exercises the path that proceeds PAST the preamble-only early return
    (`_cycle_agent_is_preamble_only` is False here, since `actions` is
    non-empty) so `in_cooldown`'s own short-circuit, not the
    preamble-only guard, is what's actually being tested."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "log", "params": {"level": "debug", "message": "tick"}},
        {"type": "order", "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT"}},
    ])
    agent.actions = [{"type": "close_position", "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT"}}]
    assert agent_engine._cycle_agent_is_preamble_only(agent) is False, (
        "this test must exercise the MIXED path, not the preamble-only early return"
    )
    claim_state: dict = {}

    with (
        patch.object(agent_engine, "_cycle_evaluate_agent", return_value=([], [])),
        patch.object(agent_engine, "execute", new=AsyncMock()) as execute_mock,
        patch.object(agent_engine, "async_session", side_effect=_claim_session_factory(claim_state)),
        patch.object(agent_engine, "_v2_apply_recovery") as recovery_mock,
    ):
        for _ in range(3):
            await agent_engine._ae_cycle_eval_and_buffer(
                agent, {}, {}, datetime.now(timezone.utc),
                alert_state={}, sim_mode=False,
                bypass_schedule=False, bypass_suppression=False,
                broadcast_fn=None, pending_dispatches=[],
                in_cooldown=True,
            )

    calls = [c.args[1][0]["type"] for c in execute_mock.await_args_list]
    assert calls.count("log") == 3, "log must fire on every tick, including while in_cooldown"
    assert calls.count("order") == 1, (
        "order must fire exactly once across 3 ticks through the REAL "
        "_ae_cycle_eval_and_buffer call site, not a reimplementation"
    )
    # The function proceeded PAST the preamble-only early return (since
    # this agent isn't preamble-only) and reached the recovery pass,
    # which runs unconditionally even while in_cooldown — confirming
    # in_cooldown's own short-circuit (not the preamble-only guard) is
    # what stopped it from reaching buffer-fire.
    assert recovery_mock.call_count == 3


@pytest.mark.asyncio
async def test_preamble_only_agent_never_drives_the_sentinel_condition_to_a_fire():
    """CRITICAL regression test — a preamble-only statement (the plan's
    own canonical worked example: `order(...)` alone, no WHEN/DO at all)
    compiles its `conditions` field to the SAME always-true sentinel
    leaf (`avail_margin@funds.any_acct >= -999999999`) the scheduled
    market-open/preclose agents use purely to drive a fire_at_time gate
    — confirmed via agentsGrammar.test.js's own preamble-only fixture.
    `_v2_send_rich_alert()` sends its rich-alert log record on EVERY
    match with no channel-membership gate of its own — so without the
    `_cycle_agent_is_preamble_only` guard, a saved+activated preamble-
    only agent would fire a spurious breach alert every cooldown window
    forever, purely from its sentinel always matching. This test forces
    `_cycle_evaluate_agent` to return a non-empty match (simulating the
    sentinel firing) and asserts the agent never reaches
    `_cycle_maybe_buffer_fire`/`pending_dispatches` at all — the
    preamble itself still runs."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "log", "params": {"level": "debug", "message": "tick"}},
    ])
    agent.actions = []
    agent.events = []
    pending_dispatches: list = []

    fake_match = [{"metric": "avail_margin", "scope": "funds.any_acct", "value": 1.0}]

    with (
        patch.object(agent_engine, "_cycle_evaluate_agent", return_value=(fake_match, fake_match)),
        patch.object(agent_engine, "execute", new=AsyncMock()) as execute_mock,
        patch.object(agent_engine, "_v2_apply_recovery") as recovery_mock,
        patch.object(agent_engine, "_cycle_maybe_buffer_fire") as buffer_fire_mock,
        patch.object(agent_engine, "async_session", side_effect=lambda: pytest.fail(
            "an empty-once-bucket preamble must never touch the DB")),
    ):
        await agent_engine._ae_cycle_eval_and_buffer(
            agent, {}, {}, datetime.now(timezone.utc),
            alert_state={}, sim_mode=False,
            bypass_schedule=False, bypass_suppression=False,
            broadcast_fn=None, pending_dispatches=pending_dispatches,
            in_cooldown=False,
        )

    assert execute_mock.await_count == 1, "the preamble's own log entry still runs"
    recovery_mock.assert_not_called(), "preamble-only must never reach the recovery pass"
    buffer_fire_mock.assert_not_called(), "preamble-only must never reach buffer-fire"
    assert pending_dispatches == []


def test_cycle_agent_is_preamble_only():
    from backend.api.algo.agent_engine import _cycle_agent_is_preamble_only

    preamble_only = _PreambleAgent(preamble=[{"type": "order", "params": {}}])
    preamble_only.actions, preamble_only.events = [], []
    assert _cycle_agent_is_preamble_only(preamble_only) is True

    with_do_actions = _PreambleAgent(preamble=[{"type": "log", "params": {}}])
    with_do_actions.actions = [{"type": "close_position", "params": {}}]
    with_do_actions.events = []
    assert _cycle_agent_is_preamble_only(with_do_actions) is False

    with_do_events = _PreambleAgent(preamble=[{"type": "order", "params": {}}])
    with_do_events.actions, with_do_events.events = [], [{"channel": "telegram", "enabled": True}]
    assert _cycle_agent_is_preamble_only(with_do_events) is False

    no_preamble = _PreambleAgent(preamble=[])
    no_preamble.actions, no_preamble.events = [], []
    assert _cycle_agent_is_preamble_only(no_preamble) is False, (
        "every pre-existing agent (preamble absent/empty) must never classify "
        "as preamble-only"
    )

    builtin_loss_agent = _PreambleAgent(preamble=[])
    builtin_loss_agent.actions = []
    builtin_loss_agent.events = [{"channel": "telegram", "enabled": True}]
    assert _cycle_agent_is_preamble_only(builtin_loss_agent) is False


def test_cycle_agent_is_preamble_only_type_checks_not_just_truthy_checks():
    """REGRESSION LOCK for a real failure hit during this item's own
    implementation: a bare `MagicMock()` test double that never
    explicitly sets `.preamble` auto-vivifies that attribute into a
    fresh child Mock on first access — which is truthy by default, even
    though it's not a list at all. A naive `bool(getattr(agent,
    'preamble', None))` check misclassified such a mock (with real
    `actions=[]`/`events=[]`, the exact shape test_agent_suppression.py
    used for its critical/low-tier agents) as preamble-only, silently
    discarding its real matches. `_cycle_agent_is_preamble_only` must
    require `isinstance(preamble, list)`, not just truthiness — this
    confirmed to fail before that fix."""
    from backend.api.algo.agent_engine import _cycle_agent_is_preamble_only

    bare_mock_agent = MagicMock()
    bare_mock_agent.actions = []
    bare_mock_agent.events = []
    # .preamble is deliberately NEVER set — exactly the real-world test
    # fixture gap this locks against.
    assert _cycle_agent_is_preamble_only(bare_mock_agent) is False


@pytest.mark.asyncio
async def test_cycle_run_preamble_never_crashes_on_non_list_preamble_value():
    """Sibling regression lock for `_cycle_run_preamble` itself — a
    non-list truthy `preamble` value (e.g. a raw/hand-edited DB row
    where the column was written as a scalar instead of a JSON array)
    must not reach `for entry in preamble:` and raise TypeError, which
    (unlike every per-entry failure this function already catches)
    would escape uncaught and abort run_cycle's entire per-agent loop
    for every agent still queued that tick. A bare `MagicMock()` would
    NOT actually exercise this guard — MagicMock supports `__iter__`
    and yields nothing by default — so this uses a real non-iterable
    scalar (`5`) to genuinely reach the type check."""
    from backend.api.algo import agent_engine

    agent = MagicMock()
    agent.id = 99
    agent.slug = "bad-preamble-value"
    agent.preamble = 5  # malformed — must never reach the for-loop below

    await agent_engine._cycle_run_preamble(
        agent, {}, datetime.now(timezone.utc),
        sim_mode=False, alert_state={},
    )  # must not raise — confirmed to raise TypeError without the isinstance guard


@pytest.mark.asyncio
async def test_replay_mode_skips_the_entire_preamble():
    """Replay rebuilds alert_state fresh every historical tick, so there
    is no "once per activation" to anchor the side-effecting bucket to,
    AND `actions_sim.py:_replay_paper_trade` has no action-type filter
    at all — it would write a spurious `mode='replay'` AlgoOrder row for
    a bare diagnostic `log`/`help` entry too (confirmed via its
    `symbol = f"{agent.slug}-{action_type}"` fallback, which has no
    special case for a non-broker type). The entire preamble — alert,
    log/help, and the once-per-activation bucket alike — must be a
    no-op during replay."""
    from backend.api.algo import agent_engine

    agent = _PreambleAgent(preamble=[
        {"type": "alert", "events": [{"channel": "telegram", "enabled": True}]},
        {"type": "log", "params": {"message": "tick"}},
        {"type": "order", "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT"}},
    ])

    with (
        patch.object(agent_engine, "dispatch", new=AsyncMock()) as dispatch_mock,
        patch.object(agent_engine, "execute", new=AsyncMock()) as execute_mock,
        patch.object(agent_engine, "async_session", side_effect=lambda: pytest.fail(
            "replay must never touch the DB claim either")),
    ):
        await agent_engine._cycle_run_preamble(
            agent, {}, datetime.now(timezone.utc),
            sim_mode=False, replay_mode=True, alert_state={},
        )

    dispatch_mock.assert_not_awaited()
    execute_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cycle_preamble_has_once_entries():
    from backend.api.algo.agent_engine import _cycle_preamble_has_once_entries

    assert _cycle_preamble_has_once_entries([]) is False
    assert _cycle_preamble_has_once_entries([
        {"type": "log", "params": {}}, {"type": "help", "params": {}},
        {"type": "alert", "events": []},
    ]) is False
    assert _cycle_preamble_has_once_entries([
        {"type": "log", "params": {}}, {"type": "order", "params": {}},
    ]) is True
    assert _cycle_preamble_has_once_entries([{"type": "bracket", "params": {}}]) is True


@pytest.mark.asyncio
async def test_empty_preamble_is_a_true_noop():
    from backend.api.algo import agent_engine
    agent = _PreambleAgent(preamble=[])

    def _session_should_not_be_called():
        raise AssertionError("an empty preamble must never touch the DB")

    with (
        patch.object(agent_engine, "execute", new=AsyncMock()) as execute_mock,
        patch.object(agent_engine, "dispatch", new=AsyncMock()) as dispatch_mock,
        patch.object(agent_engine, "async_session", side_effect=_session_should_not_be_called),
    ):
        await agent_engine._cycle_run_preamble(
            agent, {}, datetime.now(timezone.utc),
            sim_mode=False, alert_state={},
        )

    execute_mock.assert_not_awaited()
    dispatch_mock.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════
#  7 — producer-log fallback (item 0.C), now SHIPPED with the
#      `has_explicit_alert` discriminator
# ═══════════════════════════════════════════════════════════════════════
#
# History: the plan's original literal item 0.C check ("if the matched
# agent's DO clause produced a real `events` entry, skip the automatic
# _v2_send_rich_alert() call") was rejected during the first round of
# this implementation — `agent.events` being non-empty is NOT a safe
# proxy for "this fire's DO clause contains an explicit alert(...)
# call": every seeded `loss-*` builtin agent ships with a non-empty
# `events` list by default (agent_engine.py's `_LOSS_AGENT_DEFAULTS`),
# completely unrelated to any DO-level alert(...) call, which did not
# exist as a compilable construct before this plan. "If agent.events:
# skip the rich alert" would have silently disabled the kind/threshold
# table and tier-based ntfy priority for EVERY currently-live loss-*
# agent.
#
# The operator resolved this with a dedicated discriminator: a new
# `Agent.has_explicit_alert` boolean column, set ONLY by the frontend
# compiler when `doResult.events.length > 0` (a genuine DO-level
# `alert(...)` call) — `server_default='false'`, so every pre-existing
# row backfills to False and is completely unaffected. The test
# directly below (`test_agent_with_populated_events_still_gets_
# automatic_rich_alert`) is kept UNCHANGED from the first round and
# still passes: a bare MagicMock agent that never sets
# `has_explicit_alert` auto-vivifies that attribute to a truthy child
# Mock, which fails the `is True` check in `_ae_dispatch_survivor_
# entry` and correctly falls through to the rich alert — this is both
# the original regression lock AND, now, the MagicMock-trap guard for
# the new flag (the exact same lesson `_cycle_agent_is_preamble_only`
# already learned the hard way earlier in this file).

@pytest.mark.asyncio
async def test_agent_with_populated_events_still_gets_automatic_rich_alert():
    from backend.api.algo import agent_engine

    agent = MagicMock()
    agent.id = 1
    agent.slug = "loss-funds-negative"
    agent.name = "loss-funds-negative"
    agent.events = [
        {"channel": "telegram", "enabled": True},
        {"channel": "log", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
    ]
    agent.actions = []
    agent.trigger_count = 0

    entry = {
        "agent": agent,
        "matches": [{"metric": "cash", "value": -1000}],
        "result": MagicMock(condition_text="cash < 0", detail={}),
        "sim_mode": False,
        "new_status": "cooldown",
        "bypass_schedule": False,
        "debounce_min": 0,
    }

    with (
        patch.object(agent_engine, "async_session") as session_factory,
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)) as rich_mock,
        patch.object(agent_engine, "dispatch", new=AsyncMock()) as dispatch_mock,
    ):
        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        session_factory.return_value = cm

        await agent_engine._ae_dispatch_survivor_entry(entry, datetime.now(timezone.utc), {}, None)

    rich_mock.assert_awaited_once(), (
        "an agent with a populated events list (the real builtin loss-* "
        "shape) must still get the automatic rich alert — unchanged"
    )
    # rich_sent=True -> telegram/email/ntfy are skipped in dispatch() to
    # avoid duplicate delivery (the pre-existing, unchanged skip_channels
    # contract) — confirms the fallback branch, not the DSL-driven one,
    # is what ran.
    dispatch_mock.assert_awaited_once()
    _, kwargs = dispatch_mock.call_args
    assert kwargs.get("skip_channels") == frozenset({"telegram", "email", "ntfy"})


def _make_survivor_entry(agent):
    return {
        "agent": agent,
        "matches": [{"metric": "cash", "value": -1000}],
        "result": MagicMock(condition_text="cash < 0", detail={}),
        "sim_mode": False,
        "new_status": "cooldown",
        "bypass_schedule": False,
        "debounce_min": 0,
    }


def _patched_db_session_factory():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_has_explicit_alert_true_with_events_skips_rich_alert_uses_dispatch():
    """The SHIPPED item 0.C path: `has_explicit_alert=True` with a real
    `events` list skips `_v2_send_rich_alert()` entirely and lets the
    normal `events.dispatch()` call deliver every channel in
    `agent.events` — including telegram/email/ntfy, since there's no
    rich alert to avoid duplicating against (skip_channels must be
    empty here, not the usual `{telegram, email, ntfy}`)."""
    from backend.api.algo import agent_engine

    agent = MagicMock()
    agent.id = 1
    agent.slug = "custom-alert-agent"
    agent.name = "custom-alert-agent"
    agent.events = [{"channel": "telegram", "enabled": True}]
    agent.actions = []
    agent.trigger_count = 0
    agent.has_explicit_alert = True

    entry = _make_survivor_entry(agent)

    with (
        patch.object(agent_engine, "async_session", side_effect=_patched_db_session_factory),
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)) as rich_mock,
        patch.object(agent_engine, "dispatch", new=AsyncMock()) as dispatch_mock,
    ):
        broadcast_mock = MagicMock()
        await agent_engine._ae_dispatch_survivor_entry(
            entry, datetime.now(timezone.utc), {}, broadcast_mock,
        )

    rich_mock.assert_not_awaited(), "has_explicit_alert=True must skip the automatic rich alert"
    dispatch_mock.assert_awaited_once()
    _, kwargs = dispatch_mock.call_args
    assert kwargs.get("skip_channels") == frozenset(), (
        "no rich alert ran, so no channel needs to be skipped — dispatch() "
        "must deliver every channel in agent.events, telegram/email/ntfy included"
    )
    # The UI "agent fired" websocket feed must not go dark just because
    # the explicit path was taken instead of the rich-alert path.
    assert any(c.args[0] == 'agent_alert' for c in broadcast_mock.call_args_list)


@pytest.mark.asyncio
async def test_has_explicit_alert_true_with_empty_events_falls_back_to_rich_alert():
    """A stale `has_explicit_alert=True` flag with an emptied-out
    `events` list must NOT silently send nothing — fall back to the
    automatic rich alert rather than going dark."""
    from backend.api.algo import agent_engine

    agent = MagicMock()
    agent.id = 1
    agent.slug = "stale-flag-agent"
    agent.name = "stale-flag-agent"
    agent.events = []
    agent.actions = []
    agent.trigger_count = 0
    agent.has_explicit_alert = True

    entry = _make_survivor_entry(agent)

    with (
        patch.object(agent_engine, "async_session", side_effect=_patched_db_session_factory),
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)) as rich_mock,
        patch.object(agent_engine, "dispatch", new=AsyncMock()) as dispatch_mock,
    ):
        await agent_engine._ae_dispatch_survivor_entry(entry, datetime.now(timezone.utc), {}, None)

    rich_mock.assert_awaited_once(), (
        "has_explicit_alert=True with empty events must fall back to the rich alert"
    )
    _, kwargs = dispatch_mock.call_args
    assert kwargs.get("skip_channels") == frozenset({"telegram", "email", "ntfy"})


@pytest.mark.asyncio
async def test_has_explicit_alert_non_true_truthy_value_falls_back_to_rich_alert():
    """Belt-and-suspenders for the `is True` check itself: a non-bool
    truthy value (anything that isn't the literal `True`) must NOT be
    treated as an explicit-alert opt-in."""
    from backend.api.algo import agent_engine

    agent = MagicMock()
    agent.id = 1
    agent.slug = "weird-flag-agent"
    agent.name = "weird-flag-agent"
    agent.events = [{"channel": "telegram", "enabled": True}]
    agent.actions = []
    agent.trigger_count = 0
    agent.has_explicit_alert = "yes"  # truthy, but not `is True`

    entry = _make_survivor_entry(agent)

    with (
        patch.object(agent_engine, "async_session", side_effect=_patched_db_session_factory),
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)) as rich_mock,
        patch.object(agent_engine, "dispatch", new=AsyncMock()),
    ):
        await agent_engine._ae_dispatch_survivor_entry(entry, datetime.now(timezone.utc), {}, None)

    rich_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_has_explicit_alert_suppressed_in_sim_when_notify_during_run_off():
    """Defense-in-depth, not a fix for a confirmed real-message leak —
    see `test_preamble_alert_suppressed_in_sim_when_notify_during_run_off`'s
    docstring for why `dispatch_rows()`'s own independent sim-notify
    gate already covers telegram/email/ntfy regardless of
    `skip_channels`. This locks the SAME belt-and-suspenders pattern
    `_v2_send_rich_alert` already uses for the explicit-alert path:
    `skip_channels` must reflect `_ae_sim_notify_suppressed` here too,
    not just on the automatic rich-alert path."""
    from backend.api.algo import agent_engine

    agent = MagicMock()
    agent.id = 1
    agent.slug = "explicit-alert-sim-agent"
    agent.name = "explicit-alert-sim-agent"
    agent.events = [{"channel": "telegram", "enabled": True}]
    agent.actions = []
    agent.trigger_count = 0
    agent.has_explicit_alert = True

    entry = _make_survivor_entry(agent)
    entry["sim_mode"] = True

    with (
        patch.object(agent_engine, "async_session", side_effect=_patched_db_session_factory),
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock()) as rich_mock,
        patch.object(agent_engine, "dispatch", new=AsyncMock()) as dispatch_mock,
        patch.object(agent_engine, "_ae_sim_notify_suppressed", return_value=True),
    ):
        await agent_engine._ae_dispatch_survivor_entry(entry, datetime.now(timezone.utc), {}, None)

    rich_mock.assert_not_awaited(), "has_explicit_alert=True must still skip the automatic rich alert"
    dispatch_mock.assert_awaited_once()
    _, kwargs = dispatch_mock.call_args
    assert kwargs.get("skip_channels") == frozenset({"telegram", "email", "ntfy"}), (
        "notify_during_run=off during a sim run must suppress telegram/email/ntfy "
        "on the explicit-alert path too, not just the automatic rich-alert path"
    )


def test_emit_breach_record_threads_real_agent_tier_into_log_extra():
    """`event_agents.py:_rec_tracked_ntfy_priority()` reads `extra['tier']`
    from ANY record to resolve a channel's ntfy priority when the
    channel itself sets none — `_v2_send_rich_alert` already carries
    this for RICH_ALERT_AGENT (see CLAUDE.md's "Rich-alert ntfy
    priority" fix); `_emit_breach_record` (the BREACH_AGENT hand-off,
    reached by the NEW item 0.C explicit-alert path and preamble alert
    entries) did not, which would have silently fallen back to the
    generic dispatching agent's own tier instead of the REAL firing
    agent's — the exact bug class that fix closed, on a different
    rendering path. This locks the fix: the real agent's tier must be
    present in the log record's extra."""
    from backend.api.algo import events as events_mod

    agent = MagicMock()
    agent.slug = "critical-agent"
    agent.name = "Critical Agent"
    agent.id = 42
    agent.tier = "critical"

    with patch.object(events_mod, "logger") as logger_mock:
        events_mod._emit_breach_record(
            agent, "", "cash < 0", [{"channel": "ntfy", "enabled": True}],
            "tg body", "ntfy body", "subject", "<p>body</p>", False,
        )

    _, kwargs = logger_mock.info.call_args
    assert kwargs["extra"]["tier"] == "critical"
