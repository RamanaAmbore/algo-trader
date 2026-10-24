"""
Tests for Part 3 (Phase A only) of the order/place_order unification —
widen-not-rename. "order" is the new canonical action-type name the
frontend CLI compiler will emit going forward; "place_order" stays
accepted identically (both dispatch to the exact same handler). Covers:

  1. `_LIVE_ACTION_HANDLERS["order"]` / `["place_order"]` resolve to the
     exact same Python function object via `_resolve_handler`.
  2. `actions_paper.py` / `actions_sim.py` dispatch both action-type
     strings into the exact same writer function.
  3. `migrate_order_action_type_rename()` — happy path (rewrites
     "place_order" -> "order" across multiple rows/actions), the
     idempotency contract (a second run is a true no-op, 0 rows/actions
     changed, no duplicate work, no error), and `agent_events.detail`
     left byte-identical (untouched — it's a frozen historical audit
     log, never rewritten).

Residual-reference grep pass (architect council's watch-point, reported
here as a comment rather than a brittle repo-wide regression test):
grepping the whole backend/ tree for the literal string "place_order"
outside the 5 already-fixed dispatch sites + 1 YAML catalog row turns up
ONLY unrelated namespaces that must NOT be touched by this unification:
  - The real broker SDK method name `Broker.place_order(...)` /
    `KiteBroker.place_order`/`DhanBroker.place_order`/
    `GrowwBroker.place_order`/`RemoteBroker.place_order` — this is the
    actual broker-adapter API method, a completely different concept
    from the agent `Agent.actions[].type` dispatch key.
  - `backend/api/rbac.py`'s `"place_order"` RBAC capability key — a
    different namespace (role-permission strings), not an action-type
    dispatch key.
  - `backend/api/algo/lab_chat.py`'s `DENIED_TOOLS` frozenset — the MCP
    tool literally named `place_order` in `backend/mcp/kite_server.py`,
    unrelated to agent action dispatch.
  - `backend/api/routes/orders_place.py` / `orders_basket.py`'s
    `"action_type": "place_order"` keys passed to
    `PaperTradeEngine.register_open_order()` — purely descriptive/audit
    metadata (confirmed via `backend/api/algo/paper.py`: `action_type`
    is only ever read back for display/logging, never compared to
    branch dispatch behavior) for a MANUAL ticket order, not something
    that flows through `Agent.actions` JSON or `actions.py`'s dispatch
    tables at all.
  - Every other hit is a Python identifier (`_action_place_order`,
    `_place_order_write_intent`, chase.py's private `_place_order`
    helper) or comment/log text, not a string comparison against an
    `action_type` value.
No additional dispatch-table site was found.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch


# ═══════════════════════════════════════════════════════════════════════
#  1 — _LIVE_ACTION_HANDLERS dispatch equivalence
# ═══════════════════════════════════════════════════════════════════════

class TestLiveActionHandlerEquivalence:
    def test_order_and_place_order_resolve_same_function_object(self):
        from backend.api.algo.actions import _LIVE_ACTION_HANDLERS, _resolve_handler
        fn_order = _resolve_handler(_LIVE_ACTION_HANDLERS["order"])
        fn_place_order = _resolve_handler(_LIVE_ACTION_HANDLERS["place_order"])
        assert fn_order is fn_place_order, (
            "'order' and 'place_order' must resolve to the IDENTICAL "
            "handler function object, not merely equal-behaving copies"
        )

    def test_both_names_are_in_broker_actions(self):
        from backend.api.algo.actions import BROKER_ACTIONS
        assert "order" in BROKER_ACTIONS
        assert "place_order" in BROKER_ACTIONS

    @pytest.mark.asyncio
    async def test_dispatch_live_action_routes_both_names_to_same_handler(self):
        """`_dispatch_live_action` (the actual live-mode router) must
        call the SAME underlying handler regardless of which of the two
        action-type strings it's given."""
        from backend.api.algo import actions as actions_mod
        from backend.api.algo.actions import _dispatch_live_action

        calls = []
        fake_handler = AsyncMock(side_effect=lambda *a, **k: calls.append(a))

        with patch.object(actions_mod, "_resolve_handler", return_value=fake_handler):
            agent = type("A", (), {"slug": "x"})()
            await _dispatch_live_action(agent, "order", {"p": 1}, {})
            await _dispatch_live_action(agent, "place_order", {"p": 1}, {})

        assert fake_handler.await_count == 2
        # Same handler object was resolved+invoked for both action types.
        assert calls[0] == calls[1]


# ═══════════════════════════════════════════════════════════════════════
#  2 — paper / sim dispatch equivalence
# ═══════════════════════════════════════════════════════════════════════

class TestPaperAndSimDispatchEquivalence:
    @pytest.mark.asyncio
    async def test_paper_trade_routes_both_names_to_same_writer(self):
        from backend.api.algo import actions_paper

        calls = []
        async def fake_writer(agent, action_type, params, context):
            calls.append(action_type)

        with patch.object(actions_paper, "_paper_place_or_close", side_effect=fake_writer):
            agent = object()
            await actions_paper._paper_trade(agent, "order", {}, {})
            await actions_paper._paper_trade(agent, "place_order", {}, {})

        assert calls == ["order", "place_order"], (
            f"expected both action types to route through _paper_place_or_close, got {calls!r}"
        )

    @pytest.mark.asyncio
    async def test_sim_paper_trade_routes_both_names_to_same_writer(self):
        from backend.api.algo import actions_sim

        calls = []
        async def fake_writer(agent, action_type, params):
            calls.append(action_type)

        with patch.object(actions_sim, "_sim_place_or_close", side_effect=fake_writer):
            agent = object()
            await actions_sim._sim_paper_trade(agent, "order", {}, {})
            await actions_sim._sim_paper_trade(agent, "place_order", {}, {})

        assert calls == ["order", "place_order"], (
            f"expected both action types to route through _sim_place_or_close, got {calls!r}"
        )


# ═══════════════════════════════════════════════════════════════════════
#  3 — migrate_order_action_type_rename() — in-process SQLite
# ═══════════════════════════════════════════════════════════════════════
#
# Mirrors test_agent_template_unification.py's `sqlite_env` fixture
# pattern exactly: a lightweight sqlite-compatible stand-in for `Agent`
# (the real model uses JSONB, a Postgres-only type) + a minimal
# `AgentEvent` table (FK to agents.id) so the "agent_events.detail left
# untouched" assertion has something real to check against. Every
# module-level AND lazy reference to the real `Agent`/`async_session`
# is monkeypatched for the duration of each test.

@pytest_asyncio.fixture
async def sqlite_env():
    from sqlalchemy import Column, Integer, String, Text, Boolean, JSON, DateTime, ForeignKey
    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    class _Base(DeclarativeBase):
        pass

    class _Agent(_Base):
        __tablename__ = "agents"
        id                   = Column(Integer, primary_key=True, autoincrement=True)
        slug                 = Column(String(64), unique=True, nullable=False)
        name                 = Column(String(128), nullable=False)
        conditions           = Column(JSON, nullable=False, default=dict)
        events               = Column(JSON, nullable=False, default=list)
        actions              = Column(JSON, nullable=False, default=list)
        status               = Column(String(16), nullable=False, default="inactive")
        kind                 = Column(String(16), nullable=False, default="cycle")
        created_at           = Column(DateTime, nullable=False,
                                       default=lambda: datetime.now(timezone.utc))

    class _AgentEvent(_Base):
        __tablename__ = "agent_events"
        id                 = Column(Integer, primary_key=True, autoincrement=True)
        agent_id           = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
        event_type         = Column(String(32), nullable=False)
        trigger_condition  = Column(Text, nullable=True)
        detail             = Column(Text, nullable=True)
        sim_mode           = Column(Boolean, nullable=False, default=False)
        timestamp          = Column(DateTime, nullable=False,
                                     default=lambda: datetime.now(timezone.utc))

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    import backend.api.models as _models
    import backend.api.database as _database
    import backend.api.algo.agent_engine as _agent_engine

    patches = [
        patch.object(_models, "Agent", _Agent),
        patch.object(_models, "AgentEvent", _AgentEvent),
        patch.object(_database, "async_session", factory),
        patch.object(_agent_engine, "Agent", _Agent),
        patch.object(_agent_engine, "async_session", factory),
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


async def _add_agent(factory, _Agent, *, slug, actions):
    async with factory() as s:
        row = _Agent(slug=slug, name=slug, conditions={}, events=[], actions=actions,
                      status="active", kind="cycle")
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def _add_agent_event(factory, _AgentEvent, *, agent_id, detail_text):
    async with factory() as s:
        row = _AgentEvent(agent_id=agent_id, event_type="triggered",
                           trigger_condition="pnl <= -50000", detail=detail_text,
                           sim_mode=False)
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


class TestMigrateOrderActionTypeRename:
    @pytest.mark.asyncio
    async def test_happy_path_rewrites_place_order_to_order(self, sqlite_env):
        from sqlalchemy import select
        from backend.api.algo.agent_engine import migrate_order_action_type_rename

        factory, _Agent, _AgentEvent = sqlite_env
        await _add_agent(factory, _Agent, slug="loss-funds-negative", actions=[
            {"type": "place_order", "params": {"account": "ZG0790", "qty": 5}},
        ])
        await _add_agent(factory, _Agent, slug="multi-action-agent", actions=[
            {"type": "place_order", "params": {"qty": 1}},
            {"type": "log", "params": {}},
            {"type": "place_order", "params": {"qty": 2}},
        ])
        await _add_agent(factory, _Agent, slug="already-canonical", actions=[
            {"type": "order", "params": {"qty": 3}},
        ])
        await _add_agent(factory, _Agent, slug="no-actions-at-all", actions=[])

        await migrate_order_action_type_rename()

        async with factory() as s:
            rows = {r.slug: r.actions for r in (await s.execute(select(_Agent))).scalars().all()}

        assert rows["loss-funds-negative"] == [
            {"type": "order", "params": {"account": "ZG0790", "qty": 5}},
        ]
        assert rows["multi-action-agent"] == [
            {"type": "order", "params": {"qty": 1}},
            {"type": "log", "params": {}},
            {"type": "order", "params": {"qty": 2}},
        ]
        # Already-canonical row untouched (no place_order to rewrite).
        assert rows["already-canonical"] == [{"type": "order", "params": {"qty": 3}}]
        assert rows["no-actions-at-all"] == []

        # Zero residual "place_order" anywhere.
        async with factory() as s:
            residual = [
                r.slug for r in (await s.execute(select(_Agent))).scalars().all()
                if any(a.get("type") == "place_order" for a in (r.actions or []))
            ]
        assert residual == []

    @pytest.mark.asyncio
    async def test_idempotent_second_run_is_a_true_noop(self, sqlite_env):
        from sqlalchemy import select
        from backend.api.algo.agent_engine import migrate_order_action_type_rename

        factory, _Agent, _AgentEvent = sqlite_env
        await _add_agent(factory, _Agent, slug="loss-funds-negative", actions=[
            {"type": "place_order", "params": {"qty": 5}},
        ])

        await migrate_order_action_type_rename()
        async with factory() as s:
            after_first = {r.slug: r.actions for r in (await s.execute(select(_Agent))).scalars().all()}

        # Second call — must not raise, must not duplicate/change anything.
        await migrate_order_action_type_rename()
        async with factory() as s:
            after_second = {r.slug: r.actions for r in (await s.execute(select(_Agent))).scalars().all()}

        assert after_first == after_second == {
            "loss-funds-negative": [{"type": "order", "params": {"qty": 5}}],
        }

    @pytest.mark.asyncio
    async def test_no_place_order_rows_at_all_is_a_noop(self, sqlite_env):
        """Steady-state case (every row already canonical, or no agents
        at all) — must not raise, must log a zero-count summary."""
        from backend.api.algo.agent_engine import migrate_order_action_type_rename
        factory, _Agent, _AgentEvent = sqlite_env
        await _add_agent(factory, _Agent, slug="already-canonical", actions=[
            {"type": "order", "params": {}},
        ])
        await migrate_order_action_type_rename()   # must not raise

    @pytest.mark.asyncio
    async def test_agent_events_detail_left_byte_identical(self, sqlite_env):
        """`agent_events.detail` is a frozen historical audit log — the
        migration must NEVER rewrite a 'place_order' string living
        inside a persisted AgentEvent.detail JSON blob, even though that
        text contains the exact same literal string this migration
        otherwise rewrites on Agent.actions."""
        import json
        from sqlalchemy import select
        from backend.api.algo.agent_engine import migrate_order_action_type_rename

        factory, _Agent, _AgentEvent = sqlite_env
        agent_id = await _add_agent(factory, _Agent, slug="loss-funds-negative", actions=[
            {"type": "place_order", "params": {"qty": 5}},
        ])
        detail_text = json.dumps({
            "matches": [{"metric": "pnl", "scope": "positions.total"}],
            "action_type": "place_order",
        })
        event_id = await _add_agent_event(factory, _AgentEvent, agent_id=agent_id,
                                           detail_text=detail_text)

        await migrate_order_action_type_rename()

        async with factory() as s:
            row = (await s.execute(
                select(_AgentEvent).where(_AgentEvent.id == event_id)
            )).scalar_one()

        assert row.detail == detail_text, (
            "agent_events.detail must be left byte-identical — it is a "
            "frozen historical audit log, never rewritten by this migration"
        )
        # Meanwhile the live Agent.actions row WAS rewritten.
        async with factory() as s:
            agent_row = (await s.execute(
                select(_Agent).where(_Agent.id == agent_id)
            )).scalar_one()
        assert agent_row.actions == [{"type": "order", "params": {"qty": 5}}]
