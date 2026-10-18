"""
Tests for backend/api/routes/agents.py::get_recent_events (GET
/api/agents/events/recent) — the query that feeds LogPanel's Agents tab
(frontend: fetchRecentAgentEvents -> LogPanel.svelte's `_loadAgents`).

Root cause (see .claude/PLAN.md item 1): `record_manual_event()`
(agent_engine.py) writes a real `agent_events` row attributed to the
built-in `MANUAL_AGENT` sentinel (slug='manual') for every manually-placed
order, purely so order-origin labeling elsewhere (alert/fill
notifications) has an agent_id to key off. That sentinel is NOT a real
automation agent, so its events must never surface in the Agents tab.

Fix: get_recent_events() now excludes rows whose agent_id resolves to the
'manual' slug, via a NOT IN subquery applied in SQL before ORDER BY/LIMIT.

Five quality dimensions:
  SSOT        — exercises the real get_recent_events() query against a
                real (in-process sqlite) database — a MagicMock session
                would return canned rows regardless of whether the SQL
                filter exists, proving nothing.
  Correctness — manual-origin row excluded; genuine agent's row still
                included (negative control); filter applies BEFORE LIMIT
                (an n=1 discriminating case — a post-limit Python filter
                would wrongly return [] here).
  Perf        — n/a (query-shape test only, no broker/network calls).
  Reuse       — mirrors the sqlite_env fixture pattern already used by
                test_agent_template_unification.py for Agent-table tests.
  UX          — every assert carries an f-string with actual vs expected.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from datetime import datetime, timezone

import pytest
import pytest_asyncio

# pytest-asyncio runs in Mode.AUTO for this suite (see pyproject.toml) —
# async def test_* functions are auto-detected; no module-level
# pytestmark needed.


@pytest_asyncio.fixture
async def sqlite_env():
    """Builds sqlite-compatible `agents` + `agent_events` tables,
    monkeypatches every module-level reference in
    backend.api.routes.agents to point at them, and yields
    (session_factory, AgentModel, AgentEventModel). Reverses all patches
    on teardown. Mirrors test_agent_template_unification.py's sqlite_env."""
    from unittest.mock import patch
    from sqlalchemy import Column, Integer, String, Text, Boolean, JSON, DateTime, ForeignKey
    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    class _Base(DeclarativeBase):
        pass

    class _Agent(_Base):
        __tablename__ = "agents"
        id       = Column(Integer, primary_key=True, autoincrement=True)
        slug     = Column(String(64), unique=True, nullable=False)
        name     = Column(String(128), nullable=False)
        kind     = Column(String(16), nullable=False, default="cycle")
        status   = Column(String(16), nullable=False, default="inactive")
        conditions = Column(JSON, nullable=False, default=dict)
        events     = Column(JSON, nullable=False, default=list)
        actions    = Column(JSON, nullable=False, default=list)
        is_system  = Column(Boolean, nullable=False, default=False)

    class _AgentEvent(_Base):
        __tablename__ = "agent_events"
        id        = Column(Integer, primary_key=True, autoincrement=True)
        agent_id  = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
        event_type = Column(String(32), nullable=False)
        trigger_condition = Column(Text, nullable=True)
        detail    = Column(Text, nullable=True)
        sim_mode  = Column(Boolean, nullable=False, default=False)
        timestamp = Column(DateTime, nullable=False,
                            default=lambda: datetime.now(timezone.utc))

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    import backend.api.routes.agents as _agents_route

    patches = [
        patch.object(_agents_route, "Agent", _Agent),
        patch.object(_agents_route, "AgentEvent", _AgentEvent),
        patch.object(_agents_route, "async_session", factory),
    ]
    for p in patches:
        p.start()

    try:
        yield factory, _Agent, _AgentEvent
    finally:
        for p in patches:
            p.stop()
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await engine.dispose()


def _handler_fn(handler):
    """Extract the raw coroutine from a Litestar-decorated method — same
    helper used in test_agent_kind_tier_topic.py / test_agents_interpret_
    auth_fix.py."""
    return handler.fn


async def _seed_manual_and_real_agent(factory, _Agent, _AgentEvent):
    """Seeds a 'manual' sentinel agent + a genuine automation agent, and
    one AgentEvent row for each (manual event inserted LAST, i.e. it gets
    the higher/most-recent id). Returns (manual_agent_id, real_agent_id)."""
    async with factory() as s:
        manual = _Agent(slug="manual", name="Manual", kind="cycle",
                         status="active", conditions={}, events=[], actions=[])
        real = _Agent(slug="loss-positions-total", name="Loss guard",
                       kind="cycle", status="active",
                       conditions={}, events=[], actions=[])
        s.add_all([manual, real])
        await s.commit()
        await s.refresh(manual)
        await s.refresh(real)
        manual_id, real_id = manual.id, real.id

        # Real agent's event first (lower id / older)...
        s.add(_AgentEvent(
            agent_id=real_id, event_type="agent_fire",
            trigger_condition="pnl <= -50000", detail="{}", sim_mode=False,
        ))
        await s.commit()
        # ...manual event second (higher id / newer) — the exact shape
        # record_manual_event() writes in production.
        s.add(_AgentEvent(
            agent_id=manual_id, event_type="action_success",
            trigger_condition="manual via ticket", detail="{}", sim_mode=False,
        ))
        await s.commit()
    return manual_id, real_id


class TestManualOriginEventsExcludedFromAgentsTab:
    async def test_manual_event_excluded_real_event_included(self, sqlite_env):
        """Negative control baked into the same call: the manual-origin
        row must be gone, but the genuine agent's row must still be
        present — proves the fix doesn't just filter everything out."""
        factory, _Agent, _AgentEvent = sqlite_env
        manual_id, real_id = await _seed_manual_and_real_agent(factory, _Agent, _AgentEvent)

        from backend.api.routes.agents import AgentController
        controller = AgentController.__new__(AgentController)

        out = await _handler_fn(AgentController.get_recent_events)(controller, n=100, mode="all")

        agent_ids = [e.agent_id for e in out]
        assert manual_id not in agent_ids, (
            f"manual-origin event (agent_id={manual_id}) must be excluded from "
            f"the Agents-tab listing, got agent_ids={agent_ids}"
        )
        assert real_id in agent_ids, (
            f"genuine agent's event (agent_id={real_id}) must still be included, "
            f"got agent_ids={agent_ids}"
        )
        assert len(out) == 1, f"expected exactly 1 event after filtering manual, got {len(out)}"

    async def test_filter_applies_before_limit_not_after(self, sqlite_env):
        """The manual event is seeded as the NEWEST row (highest id). With
        n=1 (ORDER BY id DESC LIMIT 1), a SQL-level filter (applied before
        LIMIT) still surfaces the real agent's event. A naive Python-side
        post-limit filter would instead return [] here, since the manual
        row would already have consumed the only slot."""
        factory, _Agent, _AgentEvent = sqlite_env
        manual_id, real_id = await _seed_manual_and_real_agent(factory, _Agent, _AgentEvent)

        from backend.api.routes.agents import AgentController
        controller = AgentController.__new__(AgentController)

        out = await _handler_fn(AgentController.get_recent_events)(controller, n=1, mode="all")

        assert len(out) == 1, f"expected exactly 1 event with n=1, got {len(out)}"
        assert out[0].agent_id == real_id, (
            f"expected the real agent's event (agent_id={real_id}) to fill the "
            f"n=1 slot once the manual row is excluded, got agent_id={out[0].agent_id} "
            "— this fails if the manual exclusion is applied AFTER LIMIT instead of "
            "as part of the SQL query"
        )

    async def test_mode_live_still_excludes_manual_event(self, sqlite_env):
        """record_manual_event() always writes sim_mode=False, so the
        default mode='live' path (what the Agents tab actually calls) must
        also exclude it — not just mode='all'."""
        factory, _Agent, _AgentEvent = sqlite_env
        manual_id, real_id = await _seed_manual_and_real_agent(factory, _Agent, _AgentEvent)

        from backend.api.routes.agents import AgentController
        controller = AgentController.__new__(AgentController)

        out = await _handler_fn(AgentController.get_recent_events)(controller, n=100, mode="live")

        agent_ids = [e.agent_id for e in out]
        assert manual_id not in agent_ids, (
            f"mode='live' (the Agents tab's actual default) must still exclude the "
            f"manual event, got agent_ids={agent_ids}"
        )
        assert real_id in agent_ids, f"real agent's event missing from mode='live', got {agent_ids}"

    async def test_no_manual_agent_row_does_not_break_the_query(self, sqlite_env):
        """Edge case the advisor flagged: if the 'manual' Agent row doesn't
        exist yet (fresh DB, not seeded), the NOT IN subquery must not
        swallow every real row (a scalar `!=` against an empty subquery
        would silently NULL out and exclude everything; NOT IN against an
        empty set must instead pass everything through)."""
        factory, _Agent, _AgentEvent = sqlite_env
        async with factory() as s:
            real = _Agent(slug="loss-positions-total", name="Loss guard",
                           kind="cycle", status="active",
                           conditions={}, events=[], actions=[])
            s.add(real)
            await s.commit()
            await s.refresh(real)
            real_id = real.id
            s.add(_AgentEvent(
                agent_id=real_id, event_type="agent_fire",
                trigger_condition="pnl <= -50000", detail="{}", sim_mode=False,
            ))
            await s.commit()

        from backend.api.routes.agents import AgentController
        controller = AgentController.__new__(AgentController)

        out = await _handler_fn(AgentController.get_recent_events)(controller, n=100, mode="all")

        assert len(out) == 1, (
            f"with no 'manual' Agent row seeded at all, the real event must still "
            f"come through untouched, got {len(out)} rows"
        )
        assert out[0].agent_id == real_id
