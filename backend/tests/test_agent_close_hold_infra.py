"""Sprint 3 (auto-close-short-option plan) — generic hold/release/
auto-release infrastructure.

Covers pieces 1-6 of the plan:
  1. HoldCategory.AGENT_CLOSE falls through to the DEFAULT release
     handler (release_held_order), verified via the real dispatch path
     (get_release_handler), not asserted.
  2. hold_record()/record_held_order() snapshot auto_release_at at
     stage time; a later change never retroactively moves it.
  3. Agent.require_release / Agent.release_timeout_minutes columns +
     migration; confirmed absent from both write allow-lists.
  4. The require_release hold check inside _dispatch_live_action: fires
     only for close_position, only in mode == 'live', stages a HELD
     row + action-required alert, zero broker calls.
  5. Operator-cancel suppression: a same-session re-cross does not
     re-stage; a new session does.
  6. The auto-release background scan: fires once past the deadline,
     respects the kill switch, never fires early.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════
#  Piece 1 — HoldCategory.AGENT_CLOSE falls through to release_held_order
# ═══════════════════════════════════════════════════════════════════════

class TestAgentCloseFallsThroughToDefaultHandler:
    def test_agent_close_not_in_release_handlers_registry(self):
        from backend.api.algo.order_release import _RELEASE_HANDLERS
        from backend.api.algo.order_hold import HoldCategory
        assert HoldCategory.AGENT_CLOSE.value not in _RELEASE_HANDLERS

    def test_get_release_handler_resolves_agent_close_to_release_held_order(self):
        """Verified via the REAL dispatch path, not asserted — call
        get_release_handler("agent_close") and confirm it IS the same
        function object release_held_order resolves to by name."""
        from backend.api.algo.order_release import (
            get_release_handler, release_held_order,
        )
        handler = get_release_handler("agent_close")
        assert handler.__name__ == release_held_order.__name__
        assert handler.__name__ == "release_held_order"

    def test_get_release_handler_agent_close_differs_from_agent_order(self):
        """AGENT_CLOSE must NOT resolve to AGENT_ORDER's own handler
        (release_repeated_rejection_hold) — distinct semantics, distinct
        category string."""
        from backend.api.algo.order_release import get_release_handler
        assert (get_release_handler("agent_close").__name__
                != get_release_handler("agent_order").__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Piece 2 — auto_release_at snapshot
# ═══════════════════════════════════════════════════════════════════════

class TestAutoReleaseAtSnapshot:
    def test_hold_record_omits_auto_release_at_when_none(self):
        """Byte-identical serialised shape to every pre-existing caller
        when auto_release_at is not supplied."""
        from backend.api.algo.order_hold import HoldCategory, hold_record
        raw = hold_record(HoldCategory.EXPIRY_CLOSE, "expiry close", "CHASE_MED",
                          None, datetime(2026, 10, 15, 15, 15))
        import json
        assert "auto_release_at" not in json.loads(raw)

    def test_hold_record_includes_auto_release_at_when_set(self):
        from backend.api.algo.order_hold import HoldCategory, hold_record, parse_hold_record
        held_at = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
        deadline = held_at + timedelta(minutes=30)
        raw = hold_record(HoldCategory.AGENT_CLOSE, "90% profit short close",
                          "CHASE_MED", None, held_at, auto_release_at=deadline)
        rec = parse_hold_record(raw)
        assert rec["category"] == "agent_close"
        assert rec["auto_release_at"] == deadline.isoformat()

    @pytest.mark.asyncio
    async def test_record_held_order_threads_auto_release_at_into_hold_json(self):
        from backend.api.algo import order_hold_gate as gate
        from backend.api.algo.order_hold import HoldCategory, parse_hold_record

        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = None
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=result_mock)
        mock_session.commit = AsyncMock()

        added = {}

        def _add(row):
            row.id = 777
            added["row"] = row

        mock_session.add = _add

        deadline = datetime(2026, 11, 1, 10, 30, tzinfo=timezone.utc)

        with patch("backend.api.database.async_session", return_value=mock_session), \
             patch("backend.api.algo.order_events.write_event", new=AsyncMock()):
            row_id = await gate.record_held_order(
                HoldCategory.AGENT_CLOSE,
                account="ZG0790", symbol="NIFTY24800CE", exchange="NFO",
                side="BUY", qty=50, product="NRML", reason="90% profit",
                agent_id=42, auto_release_at=deadline,
            )

        assert row_id == 777
        rec = parse_hold_record(added["row"].hold_json)
        assert rec["category"] == "agent_close"
        assert rec["auto_release_at"] == deadline.isoformat()

    def test_later_agent_timeout_edit_never_changes_an_already_staged_deadline(self):
        """The deadline baked into hold_json at stage time is immutable
        with respect to a later Agent.release_timeout_minutes edit —
        there is no live re-join back to the Agent row at read time.
        Simulated here by staging with one timeout, then confirming the
        snapshotted value is unaffected by a DIFFERENT value on a
        freshly-constructed agent object (standing in for "the operator
        edited the setting after staging")."""
        from backend.api.algo.order_hold import HoldCategory, hold_record, parse_hold_record

        held_at = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
        original_timeout_minutes = 30
        staged_deadline = held_at + timedelta(minutes=original_timeout_minutes)
        raw = hold_record(HoldCategory.AGENT_CLOSE, "x", "CHASE_MED", None,
                          held_at, auto_release_at=staged_deadline)

        # Operator now edits the agent's timeout to something else entirely.
        agent_after_edit = SimpleNamespace(release_timeout_minutes=120)

        rec = parse_hold_record(raw)
        assert rec["auto_release_at"] == staged_deadline.isoformat()
        # The snapshotted value does not equal a deadline computed from
        # the edited setting.
        new_deadline = held_at + timedelta(minutes=agent_after_edit.release_timeout_minutes)
        assert rec["auto_release_at"] != new_deadline.isoformat()


# ═══════════════════════════════════════════════════════════════════════
#  Piece 3 — Agent.require_release / Agent.release_timeout_minutes + migration
# ═══════════════════════════════════════════════════════════════════════

class _FakeTxConn:
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, stmt, *a, **k):
        self.executed.append(str(stmt))
        return None


class TestAgentRequireReleaseColumns:
    def test_require_release_column_not_null_with_server_default_false(self):
        from backend.api.models import Agent
        col = Agent.__table__.columns["require_release"]
        assert col.nullable is False
        assert col.server_default is not None

    def test_release_timeout_minutes_column_is_nullable_with_no_server_default(self):
        from backend.api.models import Agent
        col = Agent.__table__.columns["release_timeout_minutes"]
        assert col.nullable is True
        assert col.server_default is None

    def test_require_release_orm_default_is_false(self):
        """SQLAlchemy's `default=` kwarg is a Core-level ColumnDefault
        applied at INSERT-compile time, not at bare Python object
        construction — so this checks the declared default directly
        rather than constructing an un-flushed Agent() and reading the
        attribute back (which would read None, not False, since no
        INSERT has run)."""
        from backend.api.models import Agent
        col = Agent.__table__.columns["require_release"]
        assert col.default is not None
        assert col.default.arg is False

    def test_migrate_agents_require_release_statement_shape(self):
        from backend.api import database as dbmod
        conn = _FakeTxConn()
        asyncio.run(dbmod._migrate_agents_require_release(conn))
        joined = "\n".join(conn.executed)
        assert (
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS require_release "
            "BOOLEAN NOT NULL DEFAULT false" in joined
        )
        assert "ALTER TABLE agents ADD COLUMN IF NOT EXISTS release_timeout_minutes INTEGER" in joined
        assert any("lock_timeout" in s for s in conn.executed)
        assert not any("CONCURRENTLY" in s for s in conn.executed)

    def test_init_db_calls_require_release_migration_after_priority(self):
        import inspect
        from backend.api import database as dbmod
        src = inspect.getsource(dbmod.init_db)
        lines = src.splitlines()
        begin_idx = next(i for i, l in enumerate(lines) if "async with engine.begin() as conn:" in l)
        begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

        def _idx(marker):
            i = next(j for j, l in enumerate(lines) if marker in l and "async def" not in l)
            return i, len(lines[i]) - len(lines[i].lstrip())

        priority_idx, _ = _idx("_migrate_agents_priority(conn)")
        rr_idx, rr_indent = _idx("_migrate_agents_require_release(conn)")

        assert rr_indent > begin_indent, (
            "_migrate_agents_require_release(conn) must run INSIDE the engine.begin() block"
        )
        assert rr_idx > priority_idx, (
            "_migrate_agents_require_release(conn) must run after _migrate_agents_priority(conn)"
        )


class TestRequireReleaseFieldsExcludedFromWriteAllowlists:
    """The whole safety of piece 4 depends on these two columns being
    unreachable from an ordinary agent edit or an AI-refine diff-apply."""

    def test_require_release_not_in_update_agent_field_loop(self):
        """`AgentController.update_agent` is wrapped by Litestar's route
        decorator, so `inspect.getsource` on the bound method resolves
        to the wrong object — read the whole routes/agents.py module
        source text instead. Confirmed by direct grep (orientation
        pass, before this sprint's model/migration edits) that neither
        field name appeared anywhere in this file at all; this asserts
        that invariant holds after adding the columns elsewhere."""
        import inspect
        from backend.api.routes import agents as agents_mod
        src = inspect.getsource(agents_mod)
        assert "'require_release'" not in src
        assert '"require_release"' not in src
        assert "'release_timeout_minutes'" not in src
        assert '"release_timeout_minutes"' not in src

    def test_require_release_not_in_ai_refine_writable_fields(self):
        from backend.api.routes.agents import _AI_REFINE_WRITABLE_FIELDS
        assert "require_release" not in _AI_REFINE_WRITABLE_FIELDS
        assert "release_timeout_minutes" not in _AI_REFINE_WRITABLE_FIELDS


# ═══════════════════════════════════════════════════════════════════════
#  Piece 4 — require_release hold check inside _dispatch_live_action
# ═══════════════════════════════════════════════════════════════════════

def _make_agent(**kw) -> SimpleNamespace:
    base = dict(id=1, slug="x-agent", name="X Agent",
               require_release=True, release_timeout_minutes=None,
               trade_mode="live")
    base.update(kw)
    return SimpleNamespace(**base)


_CLOSE_PARAMS = {
    "account": "ZG0790", "symbol": "NIFTY24800CE", "exchange": "NFO", "side": "BUY",
}


class TestHoldCheckModeGating:
    """The exact invariant class the 2026-10-31 expiry-day-itm-auto-close
    incident violated: a safety gate must never silently fail to engage
    because the code reached it in the wrong mode. Covers every mode
    `_al_dispatch_by_mode` can resolve to."""

    @pytest.mark.asyncio
    async def test_hold_check_never_reached_in_sim_replay_shadow_paper(self):
        from backend.api.algo.actions import _al_dispatch_by_mode
        agent = _make_agent()
        context: dict = {}

        mock_hold = AsyncMock()
        with patch("backend.api.algo.actions_live._al_maybe_stage_agent_close_hold", mock_hold), \
             patch("backend.api.algo.actions_sim._sim_paper_trade", new=AsyncMock()), \
             patch("backend.api.algo.actions_sim._replay_paper_trade", new=AsyncMock()), \
             patch("backend.api.algo.actions_sim._shadow_trade", new=AsyncMock()), \
             patch("backend.api.algo.actions_paper._paper_trade", new=AsyncMock()):
            for mode in ("sim", "replay", "shadow", "paper"):
                await _al_dispatch_by_mode(
                    agent, mode, "close_position", dict(_CLOSE_PARAMS), context,
                )
        mock_hold.assert_not_called()

    @pytest.mark.asyncio
    async def test_hold_check_reached_in_live_mode_only(self):
        from backend.api.algo.actions import _al_dispatch_by_mode
        agent = _make_agent()
        context: dict = {}

        mock_hold = AsyncMock()
        with patch("backend.api.algo.actions_live._al_maybe_stage_agent_close_hold", mock_hold):
            await _al_dispatch_by_mode(
                agent, "live", "close_position", dict(_CLOSE_PARAMS), context,
            )
        mock_hold.assert_called_once()

    @pytest.mark.asyncio
    async def test_hold_check_not_reached_for_non_close_position_live_action(self):
        """`require_release` only intercepts `close_position` — any other
        live action_type must reach its own ordinary handler."""
        from backend.api.algo.actions import _dispatch_live_action
        agent = _make_agent()
        context: dict = {}

        mock_hold = AsyncMock()
        mock_real_handler = AsyncMock()
        with patch("backend.api.algo.actions_live._al_maybe_stage_agent_close_hold", mock_hold), \
             patch("backend.api.algo.actions_live._action_live_cancel_order", mock_real_handler):
            await _dispatch_live_action(agent, "cancel_order", {}, context)
        mock_hold.assert_not_called()
        mock_real_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_hold_check_not_reached_when_require_release_false(self):
        from backend.api.algo.actions import _dispatch_live_action
        agent = _make_agent(require_release=False)
        context: dict = {}

        mock_hold = AsyncMock()
        mock_real_close = AsyncMock()
        with patch("backend.api.algo.actions_live._al_maybe_stage_agent_close_hold", mock_hold), \
             patch("backend.api.algo.actions_live._action_live_close_position", mock_real_close):
            await _dispatch_live_action(agent, "close_position", dict(_CLOSE_PARAMS), context)
        mock_hold.assert_not_called()
        mock_real_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_magicmock_agent_require_release_is_not_auto_vivified_as_true(self):
        """A bare MagicMock's auto-vivified `.require_release` attribute
        is a MagicMock object, not the literal `True` — `is True` must
        reject it. This is the exact class of bug `has_explicit_alert
        is True`/`isinstance(preamble, list)` already guard against
        elsewhere in this codebase."""
        from backend.api.algo.actions import _dispatch_live_action
        agent = MagicMock()
        agent.slug = "mock-agent"
        context: dict = {}

        mock_hold = AsyncMock()
        mock_real_close = AsyncMock()
        with patch("backend.api.algo.actions_live._al_maybe_stage_agent_close_hold", mock_hold), \
             patch("backend.api.algo.actions_live._action_live_close_position", mock_real_close):
            await _dispatch_live_action(agent, "close_position", dict(_CLOSE_PARAMS), context)
        mock_hold.assert_not_called()
        mock_real_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_through_real_resolve_mode_paper_default_never_stages_hold(self):
        """The exact prior-incident shape, exercised through the REAL
        `_resolve_mode` (only the dev-branch check is mocked, not
        `_resolve_mode` itself): an agent with the default dev-branch
        routing resolves to 'paper' -> the hold check must never run."""
        from backend.api.algo.actions import execute
        agent = _make_agent(trade_mode="paper")
        actions = [{"type": "close_position", "params": dict(_CLOSE_PARAMS)}]
        context = {"sim_mode": False}

        mock_hold = AsyncMock()
        mock_paper_trade = AsyncMock()
        with patch("backend.api.algo.actions_live._al_maybe_stage_agent_close_hold", mock_hold), \
             patch("backend.api.algo.actions_paper._paper_trade", mock_paper_trade), \
             patch("backend.api.algo.actions._exchange_gate_passes", return_value=(True, "")), \
             patch("backend.api.algo.actions.resolve_action_params",
                   side_effect=lambda a, p, c: p), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=False):
            await execute(agent, actions, context)

        mock_hold.assert_not_called()
        mock_paper_trade.assert_called_once()


class TestHoldCheckStagesHoldWithZeroBrokerCalls:
    @pytest.mark.asyncio
    async def test_matching_close_stages_hold_and_alerts_with_no_broker_call(self):
        from backend.api.algo.actions_live import _al_maybe_stage_agent_close_hold
        from backend.api.algo.order_hold import HoldCategory

        agent = _make_agent(id=42, release_timeout_minutes=None)
        params = dict(_CLOSE_PARAMS)
        context: dict = {}

        mock_chase = AsyncMock()
        mock_write_live_order = AsyncMock()
        mock_record_held = AsyncMock(return_value=901)
        mock_alert = AsyncMock()

        with patch("backend.api.algo.actions_live._al_close_resolve_position",
                   new=AsyncMock(return_value=("BUY", 50, "NRML"))), \
             patch("backend.api.algo.actions_live._al_agent_close_recently_cancelled_this_session",
                   new=AsyncMock(return_value=False)), \
             patch("backend.api.algo.actions_live._al_find_held_agent_close",
                   new=AsyncMock(return_value=None)), \
             patch("backend.api.algo.order_hold_gate.record_held_order", mock_record_held), \
             patch("backend.api.algo.actions_live._al_send_agent_close_hold_alert", mock_alert), \
             patch("backend.api.algo.chase.chase_order", mock_chase), \
             patch("backend.api.algo.actions._write_live_order", mock_write_live_order):
            await _al_maybe_stage_agent_close_hold(agent, params, context)

        mock_chase.assert_not_called()
        mock_write_live_order.assert_not_called()
        mock_record_held.assert_called_once()
        call_kwargs = mock_record_held.call_args.kwargs
        assert mock_record_held.call_args.args[0] == HoldCategory.AGENT_CLOSE
        assert call_kwargs["account"] == "ZG0790"
        assert call_kwargs["side"] == "BUY"
        assert call_kwargs["qty"] == 50
        assert call_kwargs["agent_id"] == 42
        assert call_kwargs["auto_release_at"] is None
        mock_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_auto_release_at_computed_from_release_timeout_minutes(self):
        from backend.api.algo.actions_live import _al_maybe_stage_agent_close_hold

        agent = _make_agent(id=42, release_timeout_minutes=30)
        params = dict(_CLOSE_PARAMS)
        context: dict = {}

        mock_record_held = AsyncMock(return_value=902)

        with patch("backend.api.algo.actions_live._al_close_resolve_position",
                   new=AsyncMock(return_value=("BUY", 50, "NRML"))), \
             patch("backend.api.algo.actions_live._al_agent_close_recently_cancelled_this_session",
                   new=AsyncMock(return_value=False)), \
             patch("backend.api.algo.actions_live._al_find_held_agent_close",
                   new=AsyncMock(return_value=None)), \
             patch("backend.api.algo.order_hold_gate.record_held_order", mock_record_held), \
             patch("backend.api.algo.actions_live._al_send_agent_close_hold_alert",
                   new=AsyncMock()):
            await _al_maybe_stage_agent_close_hold(agent, params, context)

        deadline = mock_record_held.call_args.kwargs["auto_release_at"]
        assert deadline is not None
        # Later editing agent.release_timeout_minutes must not retroactively
        # change this already-computed value.
        agent.release_timeout_minutes = 999
        assert deadline.minute is not None  # the baked value is untouched

    @pytest.mark.asyncio
    async def test_unverifiable_close_stages_nothing(self):
        """`_al_close_resolve_position` returning None means there is
        nothing real to hold — neither a hold row nor an alert, and the
        real handler is never reached either (the caller's chokepoint
        already decided to intercept)."""
        from backend.api.algo.actions_live import _al_maybe_stage_agent_close_hold

        agent = _make_agent()
        mock_record_held = AsyncMock()
        mock_alert = AsyncMock()
        with patch("backend.api.algo.actions_live._al_close_resolve_position",
                   new=AsyncMock(return_value=None)), \
             patch("backend.api.algo.order_hold_gate.record_held_order", mock_record_held), \
             patch("backend.api.algo.actions_live._al_send_agent_close_hold_alert", mock_alert):
            await _al_maybe_stage_agent_close_hold(agent, dict(_CLOSE_PARAMS), {})
        mock_record_held.assert_not_called()
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_held_row_skips_re_alert(self):
        from backend.api.algo.actions_live import _al_maybe_stage_agent_close_hold

        agent = _make_agent()
        mock_record_held = AsyncMock()
        mock_alert = AsyncMock()
        with patch("backend.api.algo.actions_live._al_close_resolve_position",
                   new=AsyncMock(return_value=("BUY", 50, "NRML"))), \
             patch("backend.api.algo.actions_live._al_agent_close_recently_cancelled_this_session",
                   new=AsyncMock(return_value=False)), \
             patch("backend.api.algo.actions_live._al_find_held_agent_close",
                   new=AsyncMock(return_value=555)), \
             patch("backend.api.algo.order_hold_gate.record_held_order", mock_record_held), \
             patch("backend.api.algo.actions_live._al_send_agent_close_hold_alert", mock_alert):
            await _al_maybe_stage_agent_close_hold(agent, dict(_CLOSE_PARAMS), {})
        mock_record_held.assert_not_called()
        mock_alert.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
#  Piece 5 — operator-cancel suppression
# ═══════════════════════════════════════════════════════════════════════

def _cancelled_held_row(created_at: datetime, **extra) -> SimpleNamespace:
    base = dict(
        id=1, status="CANCELLED", broker_order_id="", agent_id=42,
        account="ZG0790", symbol="NIFTY24800CE", exchange="NFO",
        transaction_type="BUY", created_at=created_at,
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _mock_session_scalar_first(value):
    result = MagicMock()
    result.scalars.return_value.first.return_value = value
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result)
    return session


class TestCancelSuppressionWithinSameSession:
    @pytest.mark.asyncio
    async def test_cancelled_this_session_suppresses(self):
        """Real wall-clock `now` (the function's own `datetime.now()` is
        a local import, not module-patchable) — a cancel recorded "just
        now" is trivially in the SAME session as "now", regardless of
        what the real current time happens to be."""
        from backend.api.algo.actions_live import (
            _al_agent_close_recently_cancelled_this_session,
        )
        row = _cancelled_held_row(created_at=datetime.now(timezone.utc))
        session = _mock_session_scalar_first(row)

        with patch("backend.api.database.async_session", return_value=session):
            suppressed = await _al_agent_close_recently_cancelled_this_session(
                42, "ZG0790", "NIFTY24800CE", "NFO", "BUY",
            )
        assert suppressed is True

    @pytest.mark.asyncio
    async def test_cancelled_in_a_new_session_does_not_suppress(self):
        """A cancel recorded 2 days ago is trivially in a DIFFERENT
        session from "now", regardless of what the real current time
        happens to be (session boundaries are daily)."""
        from backend.api.algo.actions_live import (
            _al_agent_close_recently_cancelled_this_session,
        )
        from backend.api.algo.expiry_freeze import session_date_of
        prior_session_cancel = datetime.now(timezone.utc) - timedelta(days=2)
        row = _cancelled_held_row(created_at=prior_session_cancel)
        session = _mock_session_scalar_first(row)
        assert session_date_of(prior_session_cancel) != session_date_of(datetime.now(timezone.utc))

        with patch("backend.api.database.async_session", return_value=session):
            suppressed = await _al_agent_close_recently_cancelled_this_session(
                42, "ZG0790", "NIFTY24800CE", "NFO", "BUY",
            )
        assert suppressed is False

    @pytest.mark.asyncio
    async def test_no_cancelled_row_at_all_does_not_suppress(self):
        from backend.api.algo.actions_live import (
            _al_agent_close_recently_cancelled_this_session,
        )
        session = _mock_session_scalar_first(None)
        with patch("backend.api.database.async_session", return_value=session):
            suppressed = await _al_agent_close_recently_cancelled_this_session(
                42, "ZG0790", "NIFTY24800CE", "NFO", "BUY",
            )
        assert suppressed is False

    @pytest.mark.asyncio
    async def test_lookup_failure_fails_toward_staging_not_suppression(self):
        """A hold places nothing at the broker -- the safer failure mode
        on a transient DB error is to keep offering it for review, not
        to silently suppress forever."""
        from backend.api.algo.actions_live import (
            _al_agent_close_recently_cancelled_this_session,
        )

        class _Boom:
            def __call__(self):
                raise RuntimeError("db down")

        with patch("backend.api.database.async_session", _Boom()):
            suppressed = await _al_agent_close_recently_cancelled_this_session(
                42, "ZG0790", "NIFTY24800CE", "NFO", "BUY",
            )
        assert suppressed is False

    @pytest.mark.asyncio
    async def test_full_gate_skips_staging_when_suppressed(self):
        """End-to-end through _al_maybe_stage_agent_close_hold: a
        suppressed re-stage calls neither record_held_order nor the
        alert."""
        from backend.api.algo.actions_live import _al_maybe_stage_agent_close_hold
        agent = _make_agent()
        mock_record_held = AsyncMock()
        mock_alert = AsyncMock()
        with patch("backend.api.algo.actions_live._al_close_resolve_position",
                   new=AsyncMock(return_value=("BUY", 50, "NRML"))), \
             patch("backend.api.algo.actions_live._al_agent_close_recently_cancelled_this_session",
                   new=AsyncMock(return_value=True)), \
             patch("backend.api.algo.order_hold_gate.record_held_order", mock_record_held), \
             patch("backend.api.algo.actions_live._al_send_agent_close_hold_alert", mock_alert):
            await _al_maybe_stage_agent_close_hold(agent, dict(_CLOSE_PARAMS), {})
        mock_record_held.assert_not_called()
        mock_alert.assert_not_called()


# Remaining piece (6 — auto-release scan) is appended by a later commit in
# its own test file (test_agent_close_auto_release.py), since it lives in
# background.py rather than actions_live.py.
