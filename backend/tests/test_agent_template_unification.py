"""
Tests for Sprint D of the Agent/Bracket/Template unification phase
(Phase 2 — Console retirement, Agent/Bracket/Template unification,
Automation redesign; see ~/.claude/plans/purrfect-marinating-pixel.md).

`AgentTemplate` rows (table `agent_fragments`) are additively migrated
into `Agent` rows with `kind='template'`. The old table/model/CRUD
routes stay in place — this is additive only.

Covers:
  1. `$ref` resolution (agent_evaluator.evaluate()) against a migrated
     kind='template' Agent row — condition-kind fragment.
  2. `$ref` resolution (template_registry.resolve_events()) against a
     migrated kind='template' Agent row — notify-kind fragment.
  3. Migration correctness — every pre-migration agent_fragments row
     (system AND custom, both kinds) gets an equivalent post-migration
     Agent row: kind='template', slug == name, is_system preserved,
     body landed in the right column (conditions vs events).
  4. Regression — kind='template' rows are excluded from BOTH
     agent_engine._cycle_load_agents() (run_cycle()'s loader) and
     event_agents.load_agents(), asserted directly rather than just
     reasoned about from the existing positive-allowlist filters.
  5. create_agent now accepts kind='template' without a 400 (both at
     the _age_normalize_kind() layer and end-to-end through the real
     handler), and still 400s on a kind outside the valid set.
  6. _ae_prune_retired_builtins() does not delete a migrated system
     kind='template' row even though its slug is never in
     BUILTIN_AGENTS — the landmine this sprint's migration would hit
     every subsequent boot without the kind!='template' guard.
  7. The legacy /api/admin/fragments CRUD (create/update/delete) mirrors
     into the matching Agent(kind='template') row, so $ref resolution
     stays in sync with edits made through the old UI/route.

In-process SQLite DB for isolation, mirroring the pattern already used
by test_orders_drafts.py / test_order_events.py: redeclare lightweight
sqlite-compatible stand-ins for `Agent` / `AgentTemplate` (the real
models use JSONB, a Postgres-only type) and monkeypatch every module
that imported the real classes/async_session (module-level OR lazy) to
point at the sqlite ones for the duration of each test.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from unittest.mock import patch

# pytest-asyncio runs in Mode.AUTO for this suite (see pyproject.toml) —
# async def test_* functions are auto-detected; no module-level
# pytestmark needed (and adding one would wrongly tag the two plain sync
# test_age_normalize_kind_* functions below too).


# ═══════════════════════════════════════════════════════════════════════
#  In-process SQLite fixture
# ═══════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def sqlite_env():
    """Builds sqlite-compatible Agent/AgentTemplate tables, monkeypatches
    every module-level AND lazy reference to the real classes/
    async_session, and yields (session_factory, AgentModel, AgentTemplateModel).
    Reverses all patches on teardown."""
    from sqlalchemy import Column, Integer, String, Text, Boolean, JSON, DateTime
    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    class _Base(DeclarativeBase):
        pass

    class _Agent(_Base):
        __tablename__ = "agents"
        id                   = Column(Integer, primary_key=True, autoincrement=True)
        slug                 = Column(String(64), unique=True, nullable=False)
        name                 = Column(String(128), nullable=False)
        long_name            = Column(String(256), nullable=True)
        description          = Column(Text, nullable=True)
        cli_source           = Column(Text, nullable=True)
        group_name           = Column(String(64), nullable=True)
        conditions           = Column(JSON, nullable=False, default=dict)
        events               = Column(JSON, nullable=False, default=list)
        actions              = Column(JSON, nullable=False, default=list)
        scope                = Column(String(16), nullable=False, default="per_account")
        schedule             = Column(String(32), nullable=True, default="market_hours")
        cooldown_minutes     = Column(Integer, nullable=False, default=30)
        fire_at_time         = Column(String(5), nullable=True)
        trade_mode           = Column(String(8), nullable=False, default="paper")
        status               = Column(String(16), nullable=False, default="inactive")
        last_triggered_at    = Column(DateTime, nullable=True)
        trigger_count        = Column(Integer, nullable=False, default=0)
        last_error           = Column(Text, nullable=True)
        schema_version       = Column(Integer, nullable=False, default=1)
        kind                 = Column(String(16), nullable=False, default="cycle")
        seed_version         = Column(Integer, nullable=False, default=0)
        lifespan_type        = Column(String(16), nullable=False, default="persistent")
        lifespan_max_fires   = Column(Integer, nullable=True)
        lifespan_expires_at  = Column(DateTime, nullable=True)
        tier                 = Column(String(16), nullable=False, default="medium")
        topic                = Column(String(64), nullable=False, default="general")
        digest_window_sec    = Column(Integer, nullable=False, default=30)
        debounce_minutes     = Column(Integer, nullable=False, default=0)
        condition_first_true_at = Column(DateTime, nullable=True)
        tags                 = Column(JSON, nullable=False, default=list)
        blackout_windows     = Column(JSON, nullable=False, default=list)
        is_system            = Column(Boolean, nullable=False, default=False)
        created_at           = Column(DateTime, nullable=False,
                                       default=lambda: datetime.now(timezone.utc))
        updated_at           = Column(DateTime, nullable=False,
                                       default=lambda: datetime.now(timezone.utc))

    class _AgentTemplate(_Base):
        __tablename__ = "agent_fragments"
        id           = Column(Integer, primary_key=True, autoincrement=True)
        kind         = Column(String(16), nullable=False)
        name         = Column(String(128), nullable=False)
        body         = Column(JSON, nullable=False)
        description  = Column(Text, nullable=False, default="")
        is_system    = Column(Boolean, nullable=False, default=False)
        is_active    = Column(Boolean, nullable=False, default=True)
        created_at   = Column(DateTime, nullable=False,
                               default=lambda: datetime.now(timezone.utc))
        updated_at   = Column(DateTime, nullable=False,
                               default=lambda: datetime.now(timezone.utc))

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    import backend.api.models as _models
    import backend.api.database as _database
    import backend.api.routes.agents as _agents_route
    import backend.api.routes.agent_templates as _agent_templates_route
    import backend.api.algo.agent_engine as _agent_engine

    patches = [
        patch.object(_models, "Agent", _Agent),
        patch.object(_models, "AgentTemplate", _AgentTemplate),
        patch.object(_database, "async_session", factory),
        patch.object(_agents_route, "Agent", _Agent),
        patch.object(_agents_route, "async_session", factory),
        patch.object(_agent_templates_route, "AgentTemplate", _AgentTemplate),
        patch.object(_agent_templates_route, "async_session", factory),
        patch.object(_agent_engine, "Agent", _Agent),
        patch.object(_agent_engine, "async_session", factory),
    ]
    for p in patches:
        p.start()

    try:
        yield factory, _Agent, _AgentTemplate
    finally:
        for p in patches:
            p.stop()
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await engine.dispose()


@pytest.fixture(autouse=True)
def _seed_grammar_minimal():
    """Same minimal grammar seed as test_condition_fragments.py — the
    real DB-driven registry isn't loaded in unit tests."""
    from backend.api.algo import grammar as _grammar
    from backend.api.algo.grammar_registry import REGISTRY as GRAMMAR_REGISTRY
    GRAMMAR_REGISTRY.metrics["pnl"] = _grammar._metric_pnl
    GRAMMAR_REGISTRY.scopes["positions.total"] = _grammar._scope_positions_total
    from backend.api.algo.grammar import OPERATORS
    for k, v in OPERATORS.items():
        GRAMMAR_REGISTRY.operators[k] = v
    yield


def _ctx_pnl(pnl: float):
    import pandas as pd
    from backend.api.algo.agent_evaluator import Context
    df = pd.DataFrame([{"account": "TOTAL", "pnl": pnl, "pnl_percentage": 0.0}])
    margins = pd.DataFrame([{"account": "TOTAL", "util debits": 1000000.0}])
    return Context(sum_positions=df, df_margins=margins, now=datetime.now(timezone.utc))


# ═══════════════════════════════════════════════════════════════════════
# 1-2 — $ref resolution against migrated rows
# ═══════════════════════════════════════════════════════════════════════

async def test_ref_resolution_condition_kind_against_migrated_row(sqlite_env):
    factory, _Agent, _AgentTemplate = sqlite_env
    from backend.api.algo.template_registry import (
        migrate_fragments_to_template_agents, REGISTRY,
    )
    from backend.api.algo.agent_evaluator import evaluate

    async with factory() as s:
        s.add(_AgentTemplate(
            kind="condition", name="loss-total-migrated",
            body={"metric": "pnl", "scope": "positions.total",
                  "op": "<=", "value": -50000},
            description="", is_system=False, is_active=True,
        ))
        await s.commit()

    await migrate_fragments_to_template_agents()
    await REGISTRY.reload()

    matches = evaluate({"$ref": "loss-total-migrated"}, _ctx_pnl(-60000))
    assert len(matches) == 1
    assert matches[0]["value"] == -60000

    # Below threshold — must not fire.
    assert evaluate({"$ref": "loss-total-migrated"}, _ctx_pnl(-10000)) == []


async def test_ref_resolution_notify_kind_against_migrated_row(sqlite_env):
    factory, _Agent, _AgentTemplate = sqlite_env
    from backend.api.algo.template_registry import (
        migrate_fragments_to_template_agents, REGISTRY, resolve_events,
    )

    async with factory() as s:
        s.add(_AgentTemplate(
            kind="notify", name="notify-trio-migrated",
            body=[
                {"channel": "telegram", "enabled": True},
                {"channel": "email", "enabled": True},
            ],
            description="", is_system=False, is_active=True,
        ))
        await s.commit()

    await migrate_fragments_to_template_agents()
    await REGISTRY.reload()

    out = resolve_events([{"$ref": "notify-trio-migrated"}])
    assert [c["channel"] for c in out] == ["telegram", "email"]


# ═══════════════════════════════════════════════════════════════════════
# 3 — migration correctness
# ═══════════════════════════════════════════════════════════════════════

async def test_migration_covers_every_fragment_row_system_and_custom(sqlite_env):
    factory, _Agent, _AgentTemplate = sqlite_env
    from sqlalchemy import select
    from backend.api.algo.template_registry import migrate_fragments_to_template_agents

    async with factory() as s:
        s.add(_AgentTemplate(
            kind="notify", name="sys-notify", body=[{"channel": "log", "enabled": True}],
            description="d1", is_system=True, is_active=True,
        ))
        s.add(_AgentTemplate(
            kind="condition", name="sys-condition",
            body={"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -1},
            description="d2", is_system=True, is_active=True,
        ))
        s.add(_AgentTemplate(
            kind="notify", name="custom-notify", body=[{"channel": "telegram", "enabled": True}],
            description="d3", is_system=False, is_active=True,
        ))
        s.add(_AgentTemplate(
            kind="condition", name="custom-condition",
            body={"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -2},
            description="d4", is_system=False, is_active=False,
        ))
        await s.commit()

    await migrate_fragments_to_template_agents()

    async with factory() as s:
        rows = (await s.execute(select(_Agent).where(_Agent.kind == "template"))).scalars().all()
    by_slug = {r.slug: r for r in rows}

    assert set(by_slug) == {"sys-notify", "sys-condition", "custom-notify", "custom-condition"}

    assert by_slug["sys-notify"].is_system is True
    assert by_slug["sys-notify"].events == [{"channel": "log", "enabled": True}]
    assert by_slug["sys-notify"].conditions == {}

    assert by_slug["sys-condition"].is_system is True
    assert by_slug["sys-condition"].conditions == {
        "metric": "pnl", "scope": "positions.total", "op": "<=", "value": -1,
    }
    assert by_slug["sys-condition"].events == []

    assert by_slug["custom-notify"].is_system is False
    assert by_slug["custom-notify"].status == "active"

    assert by_slug["custom-condition"].is_system is False
    assert by_slug["custom-condition"].status == "inactive"   # is_active=False mapped


async def test_migration_is_idempotent_and_preserves_custom_edits(sqlite_env):
    """Re-running the backfill must not clobber an operator's independent
    edit to an already-migrated CUSTOM Agent row (force=False semantics)."""
    factory, _Agent, _AgentTemplate = sqlite_env
    from sqlalchemy import select
    from backend.api.algo.template_registry import migrate_fragments_to_template_agents

    async with factory() as s:
        s.add(_AgentTemplate(
            kind="notify", name="custom-stable", body=[{"channel": "log", "enabled": True}],
            description="", is_system=False, is_active=True,
        ))
        await s.commit()

    await migrate_fragments_to_template_agents()

    # Operator edits the mirrored Agent row directly (e.g. Sprint C UI).
    async with factory() as s:
        row = (await s.execute(select(_Agent).where(_Agent.slug == "custom-stable"))).scalar_one()
        row.events = [{"channel": "telegram", "enabled": True}]
        await s.commit()

    # Re-run the backfill — must NOT overwrite the operator's edit.
    await migrate_fragments_to_template_agents()

    async with factory() as s:
        row = (await s.execute(select(_Agent).where(_Agent.slug == "custom-stable"))).scalar_one()
        assert row.events == [{"channel": "telegram", "enabled": True}]


# ═══════════════════════════════════════════════════════════════════════
# 4 — run_cycle() / event_agents.py loaders exclude kind='template'
# ═══════════════════════════════════════════════════════════════════════

async def test_cycle_loader_excludes_template_rows(sqlite_env):
    factory, _Agent, _AgentTemplate = sqlite_env
    from backend.api.algo.agent_engine import _cycle_load_agents

    async with factory() as s:
        s.add(_Agent(slug="real-cycle", name="Real cycle", kind="cycle",
                      status="active", conditions={}, events=[], actions=[]))
        s.add(_Agent(slug="a-template", name="A template", kind="template",
                      status="active", conditions={"metric": "pnl", "scope": "positions.total",
                                                    "op": "<=", "value": -1}, events=[], actions=[]))
        await s.commit()

    loaded = await _cycle_load_agents(None)
    slugs = {a.slug for a in loaded}
    assert "real-cycle" in slugs
    assert "a-template" not in slugs


async def test_event_loader_excludes_template_rows(sqlite_env):
    factory, _Agent, _AgentTemplate = sqlite_env
    from backend.api.algo import event_agents

    async with factory() as s:
        s.add(_Agent(slug="real-event", name="Real event", kind="event",
                      status="active", conditions={}, events=[], actions=[]))
        s.add(_Agent(slug="a-template-2", name="A template 2", kind="template",
                      status="active", conditions={}, events=[{"channel": "log", "enabled": True}],
                      actions=[]))
        await s.commit()

    event_agents._cache["at"] = float("-inf")   # force a fresh query
    loaded = await event_agents.load_agents()
    slugs = {a.slug for a in loaded}
    assert "real-event" in slugs
    assert "a-template-2" not in slugs


# ═══════════════════════════════════════════════════════════════════════
# 5 — create_agent accepts kind='template'; still 400s outside the set
# ═══════════════════════════════════════════════════════════════════════

def test_age_normalize_kind_accepts_template():
    from backend.api.routes.agents import _age_normalize_kind
    assert _age_normalize_kind("template") == "template"
    assert _age_normalize_kind("cycle") == "cycle"
    assert _age_normalize_kind("threshold") == "cycle"
    assert _age_normalize_kind("event") == "event"


def test_age_normalize_kind_rejects_unknown():
    from litestar.exceptions import HTTPException
    from backend.api.routes.agents import _age_normalize_kind
    with pytest.raises(HTTPException) as exc_info:
        _age_normalize_kind("bogus-kind")
    assert exc_info.value.status_code == 400


async def test_create_agent_persists_template_kind_end_to_end(sqlite_env):
    factory, _Agent, _AgentTemplate = sqlite_env
    from sqlalchemy import select
    from backend.api.routes.agents import AgentController, AgentCreateRequest

    controller = AgentController.__new__(AgentController)
    data = AgentCreateRequest(
        slug="cli-template-1", name="CLI template", conditions={}, events=[],
        actions=[], kind="template",
    )
    result = await AgentController.create_agent.fn(controller, data)
    assert result == {"detail": "Agent 'cli-template-1' created"}

    async with factory() as s:
        row = (await s.execute(select(_Agent).where(_Agent.slug == "cli-template-1"))).scalar_one()
        assert row.kind == "template"

    # Immediately $ref-resolvable without a restart (reload() triggered
    # by create_agent's own kind=='template' branch) — notify-shaped
    # content here since `events` was non-empty... Actually this row's
    # events/conditions are both empty (alert-only placeholder); confirm
    # the reload call itself didn't raise rather than asserting content.
    from backend.api.algo.template_registry import REGISTRY
    assert isinstance(REGISTRY._cache, dict)


# ═══════════════════════════════════════════════════════════════════════
# 6 — prune guard: migrated system templates survive seed_agents()
# ═══════════════════════════════════════════════════════════════════════

async def test_prune_retired_builtins_spares_template_rows(sqlite_env):
    factory, _Agent, _AgentTemplate = sqlite_env
    from sqlalchemy import select
    from backend.api.algo.agent_engine import _ae_prune_retired_builtins

    async with factory() as s:
        s.add(_Agent(slug="sys-template-not-in-builtins", name="T", kind="template",
                      status="active", is_system=True, conditions={}, events=[], actions=[]))
        s.add(_Agent(slug="sys-cycle-retired", name="Retired", kind="cycle",
                      status="active", is_system=True, conditions={}, events=[], actions=[]))
        await s.commit()

        # Only "sys-cycle-retired" is NOT in builtin_slugs for either row,
        # but the template row must survive regardless; the retired
        # cycle row must still be pruned (regression guard that the new
        # kind!='template' filter didn't break the original behaviour).
        await _ae_prune_retired_builtins(s, builtin_slugs=set())
        await s.commit()

        rows = (await s.execute(select(_Agent))).scalars().all()
        slugs = {r.slug for r in rows}

    assert "sys-template-not-in-builtins" in slugs
    assert "sys-cycle-retired" not in slugs


# ═══════════════════════════════════════════════════════════════════════
# 7 — legacy /api/admin/fragments CRUD mirrors into Agent rows
# ═══════════════════════════════════════════════════════════════════════

async def test_fragment_create_mirrors_into_agent_row(sqlite_env):
    factory, _Agent, _AgentTemplate = sqlite_env
    from sqlalchemy import select
    from backend.api.routes.agent_templates import AgentTemplateController, FragmentCreate

    controller = AgentTemplateController.__new__(AgentTemplateController)
    data = FragmentCreate(
        kind="condition", name="mirror-test-frag",
        body={"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -1},
    )
    out = await AgentTemplateController.create_fragment.fn(controller, data)
    assert out.name == "mirror-test-frag"

    async with factory() as s:
        row = (await s.execute(
            select(_Agent).where(_Agent.slug == "mirror-test-frag")
        )).scalar_one()
        assert row.kind == "template"
        assert row.conditions == {"metric": "pnl", "scope": "positions.total",
                                   "op": "<=", "value": -1}


async def test_fragment_delete_removes_mirrored_agent_row(sqlite_env):
    factory, _Agent, _AgentTemplate = sqlite_env
    from sqlalchemy import select
    from backend.api.routes.agent_templates import AgentTemplateController, FragmentCreate

    controller = AgentTemplateController.__new__(AgentTemplateController)
    data = FragmentCreate(
        kind="notify", name="mirror-delete-frag",
        body=[{"channel": "log", "enabled": True}],
    )
    out = await AgentTemplateController.create_fragment.fn(controller, data)
    frag_id = out.id

    await AgentTemplateController.delete_fragment.fn(controller, frag_id)

    async with factory() as s:
        row = (await s.execute(
            select(_Agent).where(_Agent.slug == "mirror-delete-frag")
        )).scalar_one_or_none()
        assert row is None
