"""
Tests for Sprint E of the Agent/Bracket/Template unification phase
(Phase 2 — Automation redesign; see ~/.claude/plans/purrfect-marinating-pixel.md
"Sprint E"). Brackets (`OrderTemplate` rows — TP/SL/Wing exit-rule
presets) are now ALSO mirrored into `Agent(kind='bracket')` rows
(`backend.api.algo.bracket_registry`), which is what the LIVE
order-fill resolver (`template_attach.load_template_for_slug_or_id`)
actually reads. `order_templates` stays the real, unchanged FK anchor
for `AlgoOrder.template_id` — never dropped, never renumbered.

This is the HIGHEST-RISK sprint in the phase (it touches the live
order-fill path) — a `council-risk` review BLOCKED the original brief
and required five fixes, reflected below:

  1. `_ae_prune_retired_builtins()` excludes `kind != "bracket"` too
     (same landmine Sprint D already hit once for `kind='template'`).
  2. `AlgoOrder.template_id` stays canonical; `Agent.legacy_template_id`
     is the back-reference, read in the OPPOSITE direction. Tested here
     via a deliberate `Agent.id`/`OrderTemplate.id` numeric collision —
     the resolver must still arm the CORRECT bracket.
  3. The `_fire_template_attach_on_fill` mode gate is untouched by this
     migration — one cheap regression test, not the centerpiece.
  4. `_APPLIES_TO_CHOICES` widened to admit "buy_option" (the seeded
     `default-long-option` row's own value) — the seed data is never
     touched.
  5. Sequencing — this sprint lands strictly after D2 (already merged,
     confirmed via `git log` before this file was written).

Per the review's explicit instruction, the two highest-stakes tests
(collision-on-fill, reboot-survives-migration) were written BEFORE the
migration code that makes them pass.

Pattern: in-process sqlite DB, mirroring test_agent_template_
unification.py's own `sqlite_env` fixture (sqlite-compatible stand-ins
for `Agent`/`OrderTemplate` — the real models use JSONB/Numeric,
Postgres-only types — monkeypatched over every module-level AND lazy
reference to the real classes/async_session).
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


# ═══════════════════════════════════════════════════════════════════════
#  In-process SQLite fixture
# ═══════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def sqlite_env():
    from sqlalchemy import (
        Column, Integer, String, Text, Boolean, JSON, DateTime, Float,
        ForeignKey,
    )
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
        # Sprint E additions under test.
        legacy_template_id   = Column(Integer, ForeignKey("order_templates.id"),
                                       unique=True, nullable=True)
        bracket_config       = Column(JSON, nullable=True)
        created_at           = Column(DateTime, nullable=False,
                                       default=lambda: datetime.now(timezone.utc))
        updated_at           = Column(DateTime, nullable=False,
                                       default=lambda: datetime.now(timezone.utc))

    class _OrderTemplate(_Base):
        __tablename__ = "order_templates"
        id                   = Column(Integer, primary_key=True, autoincrement=True)
        slug                 = Column(String(64), nullable=True, unique=True)
        name                 = Column(String(128), nullable=False)
        description          = Column(Text, nullable=False, default="")
        applies_to           = Column(String(16), nullable=False, default="both")
        tp_pct               = Column(Float, nullable=True)
        sl_pct               = Column(Float, nullable=True)
        wing_premium_pct     = Column(Float, nullable=True)
        wing_strike_offset   = Column(Integer, nullable=True)
        wing_max_spread_pct  = Column(Float, nullable=True)
        tp_order_type        = Column(String(8), nullable=False, default="LIMIT")
        tp_scales_json       = Column(Text, nullable=True)
        sl_trail_pct         = Column(Float, nullable=True)
        is_default           = Column(Boolean, nullable=False, default=False)
        is_system            = Column(Boolean, nullable=False, default=False)
        is_active            = Column(Boolean, nullable=False, default=True)
        owner_user_id        = Column(Integer, nullable=True)
        group_name           = Column(String(64), nullable=True)
        tags                 = Column(JSON, nullable=False, default=list)
        created_at           = Column(DateTime, nullable=False,
                                       default=lambda: datetime.now(timezone.utc))
        updated_at           = Column(DateTime, nullable=False,
                                       default=lambda: datetime.now(timezone.utc))

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    factory._test_engine = engine

    import backend.api.models as _models
    import backend.api.database as _database
    import backend.api.algo.agent_engine as _agent_engine

    patches = [
        patch.object(_models, "Agent", _Agent),
        patch.object(_models, "OrderTemplate", _OrderTemplate),
        patch.object(_database, "async_session", factory),
        patch.object(_agent_engine, "Agent", _Agent),
        patch.object(_agent_engine, "async_session", factory),
    ]
    for p in patches:
        p.start()

    try:
        yield factory, _Agent, _OrderTemplate
    finally:
        for p in patches:
            p.stop()
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await engine.dispose()


def _system_bracket_specs():
    """Mirrors templates_seed.SYSTEM_TEMPLATES shape closely enough for
    these tests — five system rows (four directional defaults + 'none')."""
    return [
        dict(slug="default-bull", name="Default Bull", applies_to="buy_any",
             tp_pct=30.0, sl_pct=20.0, is_default=True, is_system=True),
        dict(slug="default-long-option", name="Default Long Option",
             applies_to="buy_option", tp_pct=80.0, sl_pct=None,
             is_default=True, is_system=True),
        dict(slug="default-bear", name="Default Bear", applies_to="sell_any",
             tp_pct=30.0, sl_pct=20.0, is_default=True, is_system=True),
        dict(slug="default-short-vol", name="Default Short Vol",
             applies_to="sell_option", tp_pct=50.0, sl_pct=None,
             wing_premium_pct=10.0, is_default=True, is_system=True),
        dict(slug="none", name="None", applies_to="both",
             tp_pct=None, sl_pct=None, is_default=False, is_system=True),
    ]


# ═══════════════════════════════════════════════════════════════════════
# TEST 1 (written first, per the review) — id-collision-on-fill
# ═══════════════════════════════════════════════════════════════════════

async def test_resolution_survives_agent_id_order_template_id_collision(sqlite_env):
    """Deliberately cross Agent.id and OrderTemplate.id so a naive
    'resolve by Agent.id' implementation would arm the WRONG bracket.

    OrderTemplate N (id=N) carries tp_pct=30 ("default-bull"-shaped).
    OrderTemplate M (id=M) carries tp_pct=80 ("default-long-option"-shaped).
    The Agent row MIRRORING N is deliberately given Agent.id == M, and
    the Agent row mirroring M is given Agent.id == N — the exact
    numeric collision the council-risk review's fix #2 worried about.

    Drives the test through `template_attach.load_template_for_slug_or_id`
    — the REAL function the live-fill path
    (`_apply_template_to_order_impl`) calls — not a lone helper.
    """
    factory, _Agent, _OrderTemplate = sqlite_env
    from backend.api.algo import template_attach as _ta

    async with factory() as s:
        ot_n = _OrderTemplate(name="Bull Template", applies_to="buy_any",
                               tp_pct=30.0, sl_pct=20.0, is_system=True,
                               is_active=True)
        ot_m = _OrderTemplate(name="Long Option Template", applies_to="buy_option",
                               tp_pct=80.0, sl_pct=None, is_system=True,
                               is_active=True)
        s.add_all([ot_n, ot_m])
        await s.flush()
        n_id, m_id = ot_n.id, ot_m.id
        assert n_id != m_id

        # Cross the Agent primary keys relative to legacy_template_id —
        # EXPLICIT ids (sqlite permits assigning an autoincrement PK
        # directly) so the collision is deterministic rather than
        # relying on each table's own autoincrement counter happening
        # to assign the same sequence of integers.
        agent_for_n = _Agent(
            id=m_id,
            slug=f"bracket-{n_id}", name=ot_n.name, kind="bracket",
            is_system=True, status="active",
            conditions={}, events=[], actions=[],
            legacy_template_id=n_id,
            bracket_config={
                "tp_pct": 30.0, "sl_pct": 20.0, "applies_to": "buy_any",
                "wing_premium_pct": None, "wing_strike_offset": None,
                "wing_max_spread_pct": None, "tp_order_type": "LIMIT",
                "tp_scales_json": None, "sl_trail_pct": None,
                "is_default": True,
            },
        )
        agent_for_m = _Agent(
            id=n_id,
            slug=f"bracket-{m_id}", name=ot_m.name, kind="bracket",
            is_system=True, status="active",
            conditions={}, events=[], actions=[],
            legacy_template_id=m_id,
            bracket_config={
                "tp_pct": 80.0, "sl_pct": None, "applies_to": "buy_option",
                "wing_premium_pct": None, "wing_strike_offset": None,
                "wing_max_spread_pct": None, "tp_order_type": "MARKET",
                "tp_scales_json": None, "sl_trail_pct": None,
                "is_default": True,
            },
        )
        s.add_all([agent_for_n, agent_for_m])
        await s.commit()

        # Force the exact numeric collision: whichever Agent row
        # mirrors ot_n must NOT have Agent.id == n_id, and vice versa,
        # so a resolve-by-Agent.id bug would read the wrong
        # bracket_config. SQLite autoincrement ids are assigned in
        # insertion order starting after the OrderTemplate rows, so
        # this is already guaranteed true as long as OrderTemplate and
        # Agent don't happen to share a sequence (they don't — separate
        # tables/sequences). Assert it explicitly so the test fails
        # loudly (not silently no-ops its own premise) if sqlite's id
        # assignment ever changed.
        assert agent_for_n.id != n_id or agent_for_m.id != m_id

    # Resolve by template_id=n_id (what a real AlgoOrder.template_id
    # column — and a real fill — would carry).
    resolved = await _ta.load_template_for_slug_or_id(
        template_id=n_id, template_slug=None,
    )
    assert resolved is not None
    assert resolved["id"] == n_id          # real OrderTemplate.id, never Agent.id
    assert resolved["tp_pct"] == 30.0       # default-bull's own TP, not 80
    assert resolved["applies_to"] == "buy_any"

    resolved_m = await _ta.load_template_for_slug_or_id(
        template_id=m_id, template_slug=None,
    )
    assert resolved_m is not None
    assert resolved_m["id"] == m_id
    assert resolved_m["tp_pct"] == 80.0     # default-long-option's own TP, not 30
    assert resolved_m["applies_to"] == "buy_option"


# ═══════════════════════════════════════════════════════════════════════
# TEST 2 (written first, per the review) — reboot survives migration
# ═══════════════════════════════════════════════════════════════════════

async def test_system_brackets_survive_prune_across_two_boots(sqlite_env):
    """Seed system OrderTemplate rows, migrate to Agent(kind='bracket')
    mirrors, then simulate TWO process restarts
    (`_ae_prune_retired_builtins` — the real boot-time sweep, run with
    an empty builtin_slugs set exactly like a real boot would for
    brackets, since brackets are never in BUILTIN_AGENTS). All five
    system brackets ('none' included) must survive both passes, with
    legacy_template_id intact and no duplicate rows created."""
    factory, _Agent, _OrderTemplate = sqlite_env
    from sqlalchemy import select
    from backend.api.algo.bracket_registry import migrate_order_templates_to_bracket_agents
    from backend.api.algo.agent_engine import _ae_prune_retired_builtins

    specs = _system_bracket_specs()
    async with factory() as s:
        for spec in specs:
            s.add(_OrderTemplate(
                slug=spec["slug"], name=spec["name"],
                applies_to=spec["applies_to"],
                tp_pct=spec.get("tp_pct"), sl_pct=spec.get("sl_pct"),
                wing_premium_pct=spec.get("wing_premium_pct"),
                is_default=spec["is_default"], is_system=spec["is_system"],
                is_active=True,
            ))
        await s.commit()

    # Boot 1: migrate.
    await migrate_order_templates_to_bracket_agents()

    async with factory() as s:
        rows = (await s.execute(
            select(_Agent).where(_Agent.kind == "bracket")
        )).scalars().all()
    assert {r.slug for r in rows} == {spec["slug"] for spec in specs}
    legacy_ids_boot1 = {r.slug: r.legacy_template_id for r in rows}
    assert all(v is not None for v in legacy_ids_boot1.values())

    # Boot 1's own prune (seed_agents() calls this with the REAL
    # BUILTIN_AGENTS slugs — brackets never appear there, so an empty
    # set is the worst case / exact reproduction of the original
    # landmine).
    async with factory() as s:
        await _ae_prune_retired_builtins(s, builtin_slugs=set())
        await s.commit()

    async with factory() as s:
        rows_after_prune1 = (await s.execute(
            select(_Agent).where(_Agent.kind == "bracket")
        )).scalars().all()
    assert {r.slug for r in rows_after_prune1} == {spec["slug"] for spec in specs}, (
        "a system bracket was deleted by _ae_prune_retired_builtins — the "
        "exact landmine the council-risk review's fix #1 was required to close"
    )

    # Boot 2: migrate again (idempotent re-sync) then prune again.
    await migrate_order_templates_to_bracket_agents()
    async with factory() as s:
        await _ae_prune_retired_builtins(s, builtin_slugs=set())
        await s.commit()

    async with factory() as s:
        rows_boot2 = (await s.execute(
            select(_Agent).where(_Agent.kind == "bracket")
        )).scalars().all()
    by_slug_boot2 = {r.slug: r for r in rows_boot2}
    assert set(by_slug_boot2) == {spec["slug"] for spec in specs}
    # No duplicates — exactly one row per slug.
    assert len(rows_boot2) == len(specs)
    # legacy_template_id intact across both boots (same OrderTemplate ids).
    for slug, row in by_slug_boot2.items():
        assert row.legacy_template_id == legacy_ids_boot1[slug]


# ═══════════════════════════════════════════════════════════════════════
# Fix #1 — slug-collision pre-check (migration-time, fail-closed-per-row)
# ═══════════════════════════════════════════════════════════════════════

async def test_migration_skips_colliding_row_without_aborting_others(sqlite_env):
    factory, _Agent, _OrderTemplate = sqlite_env
    from sqlalchemy import select
    from backend.api.algo.bracket_registry import migrate_order_templates_to_bracket_agents

    async with factory() as s:
        # An unrelated, pre-existing Agent already owns the slug a
        # custom OrderTemplate row would derive ("bracket-{id}").
        ot = _OrderTemplate(name="My Custom Bracket", applies_to="both",
                             is_system=False, is_active=True)
        s.add(ot)
        await s.flush()
        colliding_slug = f"bracket-{ot.id}"
        s.add(_Agent(
            slug=colliding_slug, name="Unrelated Cycle Agent", kind="cycle",
            is_system=False, status="inactive",
            conditions={}, events=[], actions=[],
        ))
        ot2 = _OrderTemplate(slug="default-bull", name="Default Bull",
                              applies_to="buy_any", tp_pct=30.0, sl_pct=20.0,
                              is_default=True, is_system=True, is_active=True)
        s.add(ot2)
        await s.commit()

    await migrate_order_templates_to_bracket_agents()

    async with factory() as s:
        brackets = (await s.execute(
            select(_Agent).where(_Agent.kind == "bracket")
        )).scalars().all()
    # The colliding custom row never got a bracket mirror...
    assert all(b.legacy_template_id != ot.id for b in brackets)
    # ...but the unrelated collision did not abort the rest of the pass.
    assert any(b.slug == "default-bull" for b in brackets)


# ═══════════════════════════════════════════════════════════════════════
# Fix #2 (CRUD path) — mirror_bracket_upsert raises on collision instead
# of silently overwriting an unrelated row
# ═══════════════════════════════════════════════════════════════════════

async def test_mirror_bracket_upsert_raises_on_real_collision(sqlite_env):
    factory, _Agent, _OrderTemplate = sqlite_env
    from backend.api.algo.bracket_registry import (
        mirror_bracket_upsert, BracketSlugCollisionError,
    )

    async with factory() as s:
        ot = _OrderTemplate(slug="default-bull", name="Default Bull",
                             applies_to="buy_any", is_system=True, is_active=True)
        s.add(ot)
        await s.flush()
        s.add(_Agent(
            slug="default-bull", name="Unrelated", kind="cycle",
            is_system=False, status="inactive",
            conditions={}, events=[], actions=[],
        ))
        await s.commit()

        with pytest.raises(BracketSlugCollisionError):
            await mirror_bracket_upsert(s, ot)


# ═══════════════════════════════════════════════════════════════════════
# _demote_existing_default — Agent-side mirror stays in lockstep
# ═══════════════════════════════════════════════════════════════════════

async def test_demote_existing_default_brackets_flips_agent_mirror(sqlite_env):
    factory, _Agent, _OrderTemplate = sqlite_env
    from backend.api.algo.bracket_registry import demote_existing_default_brackets

    async with factory() as s:
        ot_old = _OrderTemplate(name="Old Default", applies_to="buy_any",
                                 is_default=True, is_system=False, is_active=True)
        ot_new = _OrderTemplate(name="New Default", applies_to="buy_any",
                                 is_default=True, is_system=False, is_active=True)
        s.add_all([ot_old, ot_new])
        await s.flush()
        agent_old = _Agent(
            slug=f"bracket-{ot_old.id}", name=ot_old.name, kind="bracket",
            is_system=False, status="active", conditions={}, events=[], actions=[],
            legacy_template_id=ot_old.id,
            bracket_config={"applies_to": "buy_any", "is_default": True},
        )
        agent_new = _Agent(
            slug=f"bracket-{ot_new.id}", name=ot_new.name, kind="bracket",
            is_system=False, status="active", conditions={}, events=[], actions=[],
            legacy_template_id=ot_new.id,
            bracket_config={"applies_to": "buy_any", "is_default": True},
        )
        s.add_all([agent_old, agent_new])
        await s.commit()

        await demote_existing_default_brackets(
            s, "buy_any", exclude_template_id=ot_new.id,
        )
        await s.commit()
        await s.refresh(agent_old)
        await s.refresh(agent_new)

    assert agent_old.bracket_config["is_default"] is False
    assert agent_new.bracket_config["is_default"] is True


# ═══════════════════════════════════════════════════════════════════════
# Fix #4 — applies_to validator widened to admit "buy_option"; seed data
# itself is untouched
# ═══════════════════════════════════════════════════════════════════════

def test_applies_to_validator_admits_buy_option_without_raising():
    from backend.api.routes.templates import _validate_applies_to, _APPLIES_TO_CHOICES

    assert "buy_option" in _APPLIES_TO_CHOICES
    _validate_applies_to("buy_option")   # must not raise


def test_seed_data_default_long_option_still_uses_buy_option():
    """The seed row itself is untouched — re-seeding to e.g. 'buy_any'
    would silently change which exits arm on a real BUY-option fill."""
    from backend.api.algo.templates_seed import SYSTEM_TEMPLATES

    row = next(s for s in SYSTEM_TEMPLATES if s["slug"] == "default-long-option")
    assert row["applies_to"] == "buy_option"
    assert row["tp_pct"] == 80.0
    assert row["sl_pct"] is None


# ═══════════════════════════════════════════════════════════════════════
# Fix #3 — mode-gate regression (cheap, NOT the centerpiece; see fix #2's
# tests above for the real live-fill risk)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_fire_template_attach_on_fill_still_skips_non_live_modes():
    """Sprint E does not touch `_fire_template_attach_on_fill`'s mode
    gate — this is a cheap confirmation it still works post-migration,
    not a substitute for the collision test above."""
    from backend.api.routes.orders_place import _fire_template_attach_on_fill

    with patch(
        "backend.api.routes.orders_place._opp_load_row_for_attach",
        new=AsyncMock(return_value={"overrides": {}}),
    ) as mock_load:
        await _fire_template_attach_on_fill(
            parent_row_id=1, parent_account="ZG0790",
            parent_symbol="NIFTY24APR25000CE", parent_exchange="NFO",
            parent_side="SELL", parent_qty=50, fill_price=100.0,
            template_id=7, parent_product="NRML", mode="paper",
        )
    mock_load.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# Unresolved template_id/slug on a real fill is now a loud failure, not
# a silent ad-hoc fallback (council-risk review fix #2, second half)
# ═══════════════════════════════════════════════════════════════════════

async def test_unresolved_template_id_is_loud_not_silent():
    from backend.api.algo import template_attach as _ta

    with patch.object(_ta, "load_template_for_slug_or_id", new=AsyncMock(return_value=None)), \
         patch.object(_ta, "_fire_attach_fail_alert") as mock_alert:
        result = await _ta._apply_template_to_order_impl(
            template_id=999999, template_slug=None, overrides={},
            parent_account="ZG0790", parent_symbol="NIFTY24APR25000CE",
            parent_side="SELL", parent_qty=50, parent_exchange="NFO",
            parent_fill_price=100.0, apply_path="preview",
        )
    mock_alert.assert_called_once()
    assert result is not None
    assert result.errors
    assert "999999" in result.errors[0]
