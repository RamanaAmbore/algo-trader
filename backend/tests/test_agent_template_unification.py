"""
Tests for Sprint D2 of the Agent/Bracket/Template unification phase
(Phase 2 — Console retirement, Agent/Bracket/Template unification,
Automation redesign; see ~/.claude/plans/purrfect-marinating-pixel.md).

Sprint D2 retired the `AgentTemplate` model, the `agent_fragments` table,
the separate `/api/admin/fragments` CRUD route, and Sprint D's own
transitional mirror-write/fallback-read machinery. A saved condition/
notify sub-tree is now just an ordinary `Agent` row with `kind='template'`
— authored/edited/deleted through the SAME unified `/api/agents/`
endpoints every other agent kind uses.

Covers:
  1. `$ref` resolution (agent_evaluator.evaluate()/validate()) against a
     directly-created kind='template' Agent row — condition-kind.
  2. `$ref` resolution (template_registry.resolve_events()) against a
     directly-created kind='template' Agent row — notify-kind.
  3. `seed_agent_templates()` now seeds system Templates as Agent rows
     directly (no `AgentTemplate` table involved at all) — insert,
     refresh-on-reseed, and the slug-collision skip guards.
  4. Regression — kind='template' rows are excluded from BOTH
     agent_engine._cycle_load_agents() (run_cycle()'s loader) and
     event_agents.load_agents(), asserted directly.
  5. create_agent/update_agent accept kind='template': body-shape
     validation (exactly one of conditions/events non-empty, events
     entries need a 'channel' key, condition bodies run through the
     real grammar validator), slug-format validation at create, and the
     is_system body-lock guard at update.
  6. _ae_prune_retired_builtins() does not delete a kind='template' row
     even though its slug is never in BUILTIN_AGENTS.
  7. `migrate_and_retire_agent_fragments()` — the one-time, self-
     contained backfill-then-DROP migration: happy path (every row
     backfills and the table drops), the fail-closed collision path
     (table stays in place), and the already-dropped no-op path.

In-process SQLite DB for isolation, mirroring the pattern already used
by test_orders_drafts.py / test_order_events.py: redeclare a lightweight
sqlite-compatible stand-in for `Agent` (the real model uses JSONB, a
Postgres-only type) and monkeypatch every module that imported the real
class/async_session (module-level OR lazy) to point at the sqlite one
for the duration of each test. The legacy `agent_fragments` table (for
the retirement-migration tests only) is created from
`template_registry._LEGACY_FRAGMENTS_TABLE` itself — the EXACT Core
`Table` object the migration code reads — so the test schema can never
drift from what the migration actually expects.
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
# pytestmark needed.


# ═══════════════════════════════════════════════════════════════════════
#  In-process SQLite fixture
# ═══════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def sqlite_env():
    """Builds a sqlite-compatible `agents` table (+ the legacy
    `agent_fragments` table, for the retirement-migration tests),
    monkeypatches every module-level AND lazy reference to the real
    `Agent`/`async_session`, and yields `(session_factory, AgentModel)`.
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
        given                = Column(JSON, nullable=True)
        preamble             = Column(JSON, nullable=True)
        preamble_fired_at    = Column(DateTime, nullable=True)
        preamble_alert_last_fired_at = Column(DateTime, nullable=True)
        has_explicit_alert   = Column(Boolean, nullable=False, default=False)
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

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    factory._test_engine = engine   # stashed for legacy_fragments_table below

    import backend.api.models as _models
    import backend.api.database as _database
    import backend.api.routes.agents as _agents_route
    import backend.api.algo.agent_engine as _agent_engine

    patches = [
        patch.object(_models, "Agent", _Agent),
        patch.object(_database, "async_session", factory),
        patch.object(_agents_route, "Agent", _Agent),
        patch.object(_agents_route, "async_session", factory),
        patch.object(_agent_engine, "Agent", _Agent),
        patch.object(_agent_engine, "async_session", factory),
    ]
    for p in patches:
        p.start()

    try:
        yield factory, _Agent
    finally:
        for p in patches:
            p.stop()
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def legacy_fragments_table(sqlite_env):
    """Creates the legacy `agent_fragments` table on the SAME sqlite
    engine `sqlite_env` already built, using the EXACT `Table` object
    `migrate_and_retire_agent_fragments()` reads from — so the test
    schema can never drift from what the migration code expects."""
    factory, _Agent = sqlite_env
    from backend.api.algo.template_registry import _LEGACY_FRAGMENTS_TABLE

    engine = factory._test_engine
    async with engine.begin() as conn:
        await conn.run_sync(_LEGACY_FRAGMENTS_TABLE.metadata.create_all)
    yield factory, _Agent, _LEGACY_FRAGMENTS_TABLE
    async with engine.begin() as conn:
        await conn.run_sync(_LEGACY_FRAGMENTS_TABLE.metadata.drop_all)


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
# 1-2 — $ref resolution against a directly-created kind='template' row
# ═══════════════════════════════════════════════════════════════════════

async def test_ref_resolution_condition_kind_against_template_agent(sqlite_env):
    factory, _Agent = sqlite_env
    from backend.api.algo.template_registry import REGISTRY
    from backend.api.algo.agent_evaluator import evaluate

    async with factory() as s:
        s.add(_Agent(
            slug="loss-total-direct", name="loss-total-direct", kind="template",
            is_system=False, status="active",
            conditions={"metric": "pnl", "scope": "positions.total",
                        "op": "<=", "value": -50000},
            events=[], actions=[],
        ))
        await s.commit()

    await REGISTRY.reload()

    matches = evaluate({"$ref": "loss-total-direct"}, _ctx_pnl(-60000))
    assert len(matches) == 1
    assert matches[0]["value"] == -60000

    # Below threshold — must not fire.
    assert evaluate({"$ref": "loss-total-direct"}, _ctx_pnl(-10000)) == []


async def test_ref_resolution_notify_kind_against_template_agent(sqlite_env):
    factory, _Agent = sqlite_env
    from backend.api.algo.template_registry import REGISTRY, resolve_events

    async with factory() as s:
        s.add(_Agent(
            slug="notify-trio-direct", name="notify-trio-direct", kind="template",
            is_system=False, status="active", conditions={},
            events=[
                {"channel": "telegram", "enabled": True},
                {"channel": "email", "enabled": True},
            ],
            actions=[],
        ))
        await s.commit()

    await REGISTRY.reload()

    out = resolve_events([{"$ref": "notify-trio-direct"}])
    assert [c["channel"] for c in out] == ["telegram", "email"]


# ═══════════════════════════════════════════════════════════════════════
# 3 — seed_agent_templates() writes directly to Agent rows
# ═══════════════════════════════════════════════════════════════════════

async def test_seed_agent_templates_inserts_system_rows_directly(sqlite_env):
    factory, _Agent = sqlite_env
    from sqlalchemy import select
    from backend.api.algo.template_registry import seed_agent_templates, SYSTEM_AGENT_TEMPLATES

    await seed_agent_templates()

    async with factory() as s:
        rows = (await s.execute(select(_Agent).where(_Agent.kind == "template"))).scalars().all()
    by_slug = {r.slug: r for r in rows}

    assert set(by_slug) == {spec["name"] for spec in SYSTEM_AGENT_TEMPLATES}
    for spec in SYSTEM_AGENT_TEMPLATES:
        row = by_slug[spec["name"]]
        assert row.is_system is True
        if spec["kind"] == "notify":
            assert row.events == spec["body"]
            assert row.conditions == {}
        else:
            assert row.conditions == spec["body"]
            assert row.events == []


async def test_seed_agent_templates_refreshes_existing_system_row_body(sqlite_env):
    """Re-seeding must overwrite a system row's body/description from
    code (operators clone to a custom template for a permanent change)."""
    factory, _Agent = sqlite_env
    from sqlalchemy import select
    from backend.api.algo.template_registry import seed_agent_templates

    await seed_agent_templates()

    async with factory() as s:
        row = (await s.execute(
            select(_Agent).where(_Agent.slug == "notify-log-only")
        )).scalar_one()
        row.events = [{"channel": "telegram", "enabled": True}]  # operator "edit"
        await s.commit()

    await seed_agent_templates()   # re-seed — must overwrite back

    async with factory() as s:
        row = (await s.execute(
            select(_Agent).where(_Agent.slug == "notify-log-only")
        )).scalar_one()
        assert row.events == [{"channel": "log", "enabled": True}]


async def test_seed_agent_templates_skips_slug_owned_by_non_template_agent(sqlite_env):
    factory, _Agent = sqlite_env
    from sqlalchemy import select
    from backend.api.algo.template_registry import seed_agent_templates

    async with factory() as s:
        s.add(_Agent(
            slug="notify-log-only", name="A real cycle agent", kind="cycle",
            is_system=False, status="active", conditions={}, events=[], actions=[],
        ))
        await s.commit()

    await seed_agent_templates()   # must not raise, must not clobber the cycle row

    async with factory() as s:
        row = (await s.execute(
            select(_Agent).where(_Agent.slug == "notify-log-only")
        )).scalar_one()
        assert row.kind == "cycle"   # untouched


# ═══════════════════════════════════════════════════════════════════════
# 4 — run_cycle() / event_agents.py loaders exclude kind='template'
# ═══════════════════════════════════════════════════════════════════════

async def test_cycle_loader_excludes_template_rows(sqlite_env):
    factory, _Agent = sqlite_env
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
    factory, _Agent = sqlite_env
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
# 5 — create_agent/update_agent validation + guards for kind='template'
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
    factory, _Agent = sqlite_env
    from sqlalchemy import select
    from backend.api.routes.agents import AgentController, AgentCreateRequest

    controller = AgentController.__new__(AgentController)
    data = AgentCreateRequest(
        slug="cli-template-1", name="CLI template", conditions={},
        events=[{"channel": "telegram", "enabled": True}],
        actions=[], kind="template",
    )
    result = await AgentController.create_agent.fn(controller, data)
    assert result == {"detail": "Agent 'cli-template-1' created"}

    async with factory() as s:
        row = (await s.execute(select(_Agent).where(_Agent.slug == "cli-template-1"))).scalar_one()
        assert row.kind == "template"

    # Immediately $ref-resolvable without a restart (reload() triggered
    # by create_agent's own kind=='template' branch).
    from backend.api.algo.template_registry import REGISTRY
    assert REGISTRY.get("notify", "cli-template-1") == [{"channel": "telegram", "enabled": True}]


async def test_create_agent_rejects_both_conditions_and_events_for_template(sqlite_env):
    from litestar.exceptions import HTTPException
    from backend.api.routes.agents import AgentController, AgentCreateRequest

    controller = AgentController.__new__(AgentController)
    data = AgentCreateRequest(
        slug="both-bodies", name="Both bodies",
        conditions={"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -1},
        events=[{"channel": "log", "enabled": True}],
        actions=[], kind="template",
    )
    with pytest.raises(HTTPException) as exc_info:
        await AgentController.create_agent.fn(controller, data)
    assert exc_info.value.status_code == 400


async def test_create_agent_rejects_empty_body_for_template(sqlite_env):
    from litestar.exceptions import HTTPException
    from backend.api.routes.agents import AgentController, AgentCreateRequest

    controller = AgentController.__new__(AgentController)
    data = AgentCreateRequest(
        slug="empty-body", name="Empty body", conditions={}, events=[],
        actions=[], kind="template",
    )
    with pytest.raises(HTTPException) as exc_info:
        await AgentController.create_agent.fn(controller, data)
    assert exc_info.value.status_code == 400


async def test_create_agent_rejects_bad_slug_shape_for_template(sqlite_env):
    from litestar.exceptions import HTTPException
    from backend.api.routes.agents import AgentController, AgentCreateRequest

    controller = AgentController.__new__(AgentController)
    data = AgentCreateRequest(
        slug="Not A Valid Slug!", name="Bad slug",
        conditions={}, events=[{"channel": "log", "enabled": True}],
        actions=[], kind="template",
    )
    with pytest.raises(HTTPException) as exc_info:
        await AgentController.create_agent.fn(controller, data)
    assert exc_info.value.status_code == 400


async def test_create_agent_validates_condition_body_through_grammar_evaluator(sqlite_env):
    """Ports the defect-prevention guarantee of the retired
    `agent_templates.py`'s `_validate_condition_body` — a malformed/
    unknown-token condition-kind Template body must 422, not silently
    save a Template that will always resolve to []."""
    from litestar.exceptions import HTTPException
    from backend.api.routes.agents import AgentController, AgentCreateRequest

    controller = AgentController.__new__(AgentController)
    data = AgentCreateRequest(
        slug="bad-condition-template", name="Bad condition",
        conditions={"metric": "totally-unknown-metric", "scope": "positions.total",
                    "op": "<=", "value": -1},
        events=[], actions=[], kind="template",
    )
    with pytest.raises(HTTPException) as exc_info:
        await AgentController.create_agent.fn(controller, data)
    assert exc_info.value.status_code == 422


async def test_update_agent_rejects_body_edit_on_system_template(sqlite_env):
    factory, _Agent = sqlite_env
    from litestar.exceptions import HTTPException
    from backend.api.routes.agents import AgentController, AgentUpdateRequest

    async with factory() as s:
        s.add(_Agent(slug="sys-tpl", name="Sys template", kind="template",
                      is_system=True, status="active", conditions={},
                      events=[{"channel": "log", "enabled": True}], actions=[]))
        await s.commit()

    controller = AgentController.__new__(AgentController)
    data = AgentUpdateRequest(events=[{"channel": "telegram", "enabled": True}])
    with pytest.raises(HTTPException) as exc_info:
        await AgentController.update_agent.fn(controller, "sys-tpl", data)
    assert exc_info.value.status_code == 400


async def test_update_agent_allows_body_edit_on_custom_template(sqlite_env):
    factory, _Agent = sqlite_env
    from sqlalchemy import select
    from backend.api.routes.agents import AgentController, AgentUpdateRequest

    async with factory() as s:
        s.add(_Agent(slug="custom-tpl", name="Custom template", kind="template",
                      is_system=False, status="active", conditions={},
                      events=[{"channel": "log", "enabled": True}], actions=[]))
        await s.commit()

    controller = AgentController.__new__(AgentController)
    data = AgentUpdateRequest(events=[{"channel": "telegram", "enabled": True}])
    result = await AgentController.update_agent.fn(controller, "custom-tpl", data)
    assert result == {"detail": "Agent 'custom-tpl' updated"}

    async with factory() as s:
        row = (await s.execute(select(_Agent).where(_Agent.slug == "custom-tpl"))).scalar_one()
        assert row.events == [{"channel": "telegram", "enabled": True}]


# ═══════════════════════════════════════════════════════════════════════
# 6 — prune guard: template rows survive seed_agents()
# ═══════════════════════════════════════════════════════════════════════

async def test_prune_retired_builtins_spares_template_rows(sqlite_env):
    factory, _Agent = sqlite_env
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
        # cycle row must still be pruned (regression guard that the
        # kind!='template' filter didn't break the original behaviour).
        await _ae_prune_retired_builtins(s, builtin_slugs=set())
        await s.commit()

        rows = (await s.execute(select(_Agent))).scalars().all()
        slugs = {r.slug for r in rows}

    assert "sys-template-not-in-builtins" in slugs
    assert "sys-cycle-retired" not in slugs


# ═══════════════════════════════════════════════════════════════════════
# 7 — migrate_and_retire_agent_fragments()
# ═══════════════════════════════════════════════════════════════════════

async def _insert_legacy_row(factory, table, **kw):
    defaults = dict(description="", is_system=False, is_active=True)
    defaults.update(kw)
    async with factory() as s:
        await s.execute(table.insert().values(**defaults))
        await s.commit()


async def test_retirement_already_dropped_is_a_noop(sqlite_env):
    """No `agent_fragments` table at all (the steady-state case once
    this has run successfully everywhere) — must not raise."""
    from backend.api.algo.template_registry import migrate_and_retire_agent_fragments
    await migrate_and_retire_agent_fragments()   # table never created in this fixture — no-op


async def test_retirement_happy_path_backfills_and_drops_table(legacy_fragments_table):
    factory, _Agent, table = legacy_fragments_table
    from sqlalchemy import select, inspect as sa_inspect
    from backend.api.algo.template_registry import migrate_and_retire_agent_fragments

    await _insert_legacy_row(
        factory, table, kind="notify", name="sys-notify",
        body=[{"channel": "log", "enabled": True}], is_system=True,
    )
    await _insert_legacy_row(
        factory, table, kind="condition", name="custom-condition",
        body={"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -2},
        is_system=False, is_active=False,
    )

    await migrate_and_retire_agent_fragments()

    async with factory() as s:
        rows = (await s.execute(select(_Agent).where(_Agent.kind == "template"))).scalars().all()
        by_slug = {r.slug: r for r in rows}
        has_table = await s.run_sync(
            lambda sync_session: sa_inspect(sync_session.get_bind()).has_table("agent_fragments")
        )

    assert set(by_slug) == {"sys-notify", "custom-condition"}
    assert by_slug["sys-notify"].is_system is True
    assert by_slug["sys-notify"].events == [{"channel": "log", "enabled": True}]
    assert by_slug["custom-condition"].status == "inactive"
    assert has_table is False   # table dropped


async def test_retirement_leaves_table_in_place_on_slug_collision(legacy_fragments_table):
    """A legacy row whose name is already used by a non-template agent
    must NOT be silently dropped/lost — the table stays in place."""
    factory, _Agent, table = legacy_fragments_table
    from sqlalchemy import inspect as sa_inspect
    from backend.api.algo.template_registry import migrate_and_retire_agent_fragments

    async with factory() as s:
        s.add(_Agent(slug="collides", name="A real cycle agent", kind="cycle",
                      status="active", conditions={}, events=[], actions=[]))
        await s.commit()
    await _insert_legacy_row(
        factory, table, kind="notify", name="collides",
        body=[{"channel": "log", "enabled": True}],
    )

    await migrate_and_retire_agent_fragments()

    async with factory() as s:
        has_table = await s.run_sync(
            lambda sync_session: sa_inspect(sync_session.get_bind()).has_table("agent_fragments")
        )
    assert has_table is True   # NOT dropped — manual review needed


async def test_retirement_is_idempotent_across_two_calls(legacy_fragments_table):
    """Re-running after a successful drop (table already gone) must not
    raise, and a row already matched by a prior partial boot (e.g. a
    system Template `seed_agent_templates()` already seeded directly)
    must not be re-inserted or duplicated."""
    factory, _Agent, table = legacy_fragments_table
    from sqlalchemy import select
    from backend.api.algo.template_registry import (
        migrate_and_retire_agent_fragments, seed_agent_templates,
    )

    await seed_agent_templates()   # seeds system Templates as Agent rows directly
    await _insert_legacy_row(
        factory, table, kind="notify", name="notify-log-only",   # same slug as a seeded system row
        body=[{"channel": "log", "enabled": True}], is_system=True,
    )

    await migrate_and_retire_agent_fragments()
    await migrate_and_retire_agent_fragments()   # second call — table already gone, must no-op

    async with factory() as s:
        rows = (await s.execute(
            select(_Agent).where(_Agent.slug == "notify-log-only")
        )).scalars().all()
    assert len(rows) == 1   # no duplicate
