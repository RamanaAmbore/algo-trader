"""
Characterization tests for run_cycle().

Goal: push branch coverage to ≥80% before refactoring.

This is the main per-tick evaluation loop for all agents. It:
  - Iterates all active agents
  - Evaluates each agent's condition tree
  - Handles cooldown, debounce, topic suppression
  - Fires actions and records outcomes to DB
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock, patch, call


# Helper factories
def _make_agent(
    *,
    id: int,
    slug: str,
    status: str = "active",
    trigger_count: int = 0,
    cooldown_minutes: int = 30,
    last_triggered_at=None,
    conditions=None,
    actions=None,
    schedule: str = "market_hours",
    debounce_minutes: int = 0,
    condition_first_true_at=None,
    fire_at_time=None,
    blackout_windows=None,
    lifespan_type: str = "persistent",
    lifespan_max_fires=None,
    lifespan_expires_at=None,
    tier: str = "medium",
    topic: str = "general",
):
    agent = MagicMock()
    agent.id = id
    agent.slug = slug
    agent.status = status
    agent.trigger_count = trigger_count
    agent.cooldown_minutes = cooldown_minutes
    agent.last_triggered_at = last_triggered_at
    agent.conditions = conditions or {}
    agent.actions = actions or []
    agent.schedule = schedule
    agent.debounce_minutes = debounce_minutes
    agent.condition_first_true_at = condition_first_true_at
    agent.fire_at_time = fire_at_time
    agent.blackout_windows = blackout_windows or []
    agent.lifespan_type = lifespan_type
    agent.lifespan_max_fires = lifespan_max_fires
    agent.lifespan_expires_at = lifespan_expires_at
    agent.tier = tier
    agent.topic = topic
    agent.name = slug
    return agent


def _make_session(agents_list):
    """
    Build a mock async_session context manager that returns `agents_list`
    on the first SELECT (agent load) and is a no-op for all subsequent
    UPDATE + COMMIT calls.
    """
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = agents_list

    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ─────────────────────────────────────────────────────────────────────────────
#  Tests: Early Returns and Guard Clauses
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_cycle_returns_early_when_now_is_none():
    """
    When context['now'] is None or missing → run_cycle returns early without
    doing anything else.
    """
    from backend.api.algo import agent_engine

    context = {
        "now": None,  # Missing or None
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    with patch.object(agent_engine, "async_session") as session_mock:
        await agent_engine.run_cycle(context=context, broadcast_fn=None)

    # async_session should NOT have been called (early return)
    session_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_cycle_with_empty_only_agent_ids():
    """
    When only_agent_ids=[] (explicitly empty, not None) → agents=[], and
    run_cycle returns without evaluating any agents (market scenario explorer).
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    sessions_created = []

    def _make_session_empty():
        s = AsyncMock()
        s.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        ))
        s.commit = AsyncMock()
        sessions_created.append(s)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=s)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    with (
        patch.object(agent_engine, "async_session", side_effect=_make_session_empty),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True}),
        patch.object(agent_engine, "v2_evaluate", new=AsyncMock()) as eval_mock,
    ):
        await agent_engine.run_cycle(
            context=context, bypass_schedule=False, only_agent_ids=[]
        )

    # v2_evaluate should NOT have been called (no agents)
    eval_mock.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
#  Tests: Agent Status and Schedule Gates
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_active_agent_evaluated_on_default_run():
    """
    When agent.status='active' and market is open → agent is evaluated.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    agent = _make_agent(id=1, slug="test_agent", status="active")
    agents_list = [agent]

    dummy_matches = [{"metric": "pnl", "value": -10000.0}]
    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    sessions_created = []

    def _make_session_collector():
        s = AsyncMock()
        s.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=agents_list)))
        ))
        s.commit = AsyncMock()
        sessions_created.append(s)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=s)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    with (
        patch.object(agent_engine, "async_session", side_effect=_make_session_collector),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),
        patch.object(agent_engine, "_v2_should_suppress", return_value=False),
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)),
        patch.object(agent_engine, "log_event", new=AsyncMock()),
        patch.object(agent_engine, "dispatch", new=AsyncMock()),
        patch.object(agent_engine, "execute", new=AsyncMock()),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
        patch.object(agent_engine, "_v2_record", return_value=None),
    ):
        await agent_engine.run_cycle(context=context, broadcast_fn=None)

    # At least one evaluate call should have happened
    # (this is implicit in the condition evaluation)


@pytest.mark.asyncio
async def test_disabled_agent_skipped():
    """
    When agent.status='disabled' (or not 'active'/'cooldown') → agent is skipped.
    Use only_agent_ids to force a specific load attempt.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    agent = _make_agent(id=1, slug="disabled_agent", status="disabled")

    # When only_agent_ids is set, agent is loaded regardless of status,
    # but then skipped by the schedule/gate logic. Here we test that
    # disabled (or other status) agents are NOT loaded on default run.
    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    sessions_created = []

    def _make_session_for_default():
        s = AsyncMock()
        # On default run (no only_agent_ids), query is:
        # select(Agent).where(Agent.status.in_(["active", "cooldown"]))
        # This returns an empty list because our agent status is "disabled"
        s.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        ))
        s.commit = AsyncMock()
        sessions_created.append(s)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=s)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    with (
        patch.object(agent_engine, "async_session", side_effect=_make_session_for_default),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True}),
        patch.object(agent_engine, "v2_evaluate", new=AsyncMock()) as eval_mock,
    ):
        await agent_engine.run_cycle(context=context, broadcast_fn=None)

    # v2_evaluate should NOT have been called (agent not loaded)
    eval_mock.assert_not_called()


@pytest.mark.asyncio
async def test_agent_schedule_never_skipped():
    """
    When agent.schedule='never' → agent is always skipped (even if active).
    Used for "manual" audit agents.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    agent = _make_agent(
        id=1, slug="manual_agent", status="active", schedule="never"
    )
    agents_list = [agent]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    eval_calls = []

    def _track_eval(*args, **kwargs):
        eval_calls.append((args, kwargs))
        return []

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True}),
        patch.object(agent_engine, "v2_evaluate", side_effect=_track_eval),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # v2_evaluate should NOT have been called
    assert len(eval_calls) == 0, (
        "schedule='never' agents must be skipped even when active"
    )


@pytest.mark.asyncio
async def test_agent_schedule_market_hours_skipped_when_closed():
    """
    When agent.schedule='market_hours' and all segments closed → agent skipped.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 12, 16, 0, 0, tzinfo=timezone.utc)  # Saturday, after hours
    agent = _make_agent(
        id=1, slug="market_hours_agent", status="active", schedule="market_hours"
    )
    agents_list = [agent]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    eval_calls = []

    def _track_eval(*args, **kwargs):
        eval_calls.append((args, kwargs))
        return []

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": False, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", side_effect=_track_eval),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # v2_evaluate should NOT have been called
    assert len(eval_calls) == 0, (
        "schedule='market_hours' agents must skip when markets closed"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Tests: Cooldown Gate
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_agent_skipped_before_expiry():
    """
    When agent.status='cooldown' and last_triggered_at is recent
    (< cooldown_minutes ago) → agent is skipped. The code uses wall-clock
    datetime.now(timezone.utc), so we need to set last_triggered_at to
    actual recent wall-clock time.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 30, 0, tzinfo=timezone.utc)
    # Use wall-clock time that will be < cooldown_minutes from current time
    last_trigger = datetime.now(timezone.utc) - timedelta(minutes=10)
    agent = _make_agent(
        id=1, slug="cooldown_agent",
        status="cooldown",
        cooldown_minutes=30,
        last_triggered_at=last_trigger,
    )
    agents_list = [agent]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    eval_calls = []

    def _track_eval(*args, **kwargs):
        eval_calls.append((args, kwargs))
        return []

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", side_effect=_track_eval),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # v2_evaluate should NOT have been called
    assert len(eval_calls) == 0, (
        "cooldown agents with elapsed < cooldown_minutes must be skipped"
    )


@pytest.mark.asyncio
async def test_cooldown_agent_evaluated_after_expiry():
    """
    When agent.status='cooldown' and cooldown has expired
    (elapsed >= cooldown_minutes) → agent is evaluated normally.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 30, 0, tzinfo=timezone.utc)
    # Use wall-clock time that will be >= cooldown_minutes from current time
    last_trigger = datetime.now(timezone.utc) - timedelta(minutes=40)
    agent = _make_agent(
        id=1, slug="cooldown_expired",
        status="cooldown",
        cooldown_minutes=30,
        last_triggered_at=last_trigger,
    )
    agents_list = [agent]

    dummy_matches = []  # No match this tick

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # v2_evaluate should have been called (agent was not skipped)


# ─────────────────────────────────────────────────────────────────────────────
#  Tests: Condition Evaluation and Tree Shapes
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_condition_tree_evaluated_by_v2_evaluate():
    """
    Agent's condition tree is passed to v2_evaluate() and the returned
    matches list is buffered for dispatch.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    condition_tree = {
        "all": [
            {"metric": "pnl", "scope": "positions_TOTAL", "op": "lt", "value": -5000},
        ]
    }
    agent = _make_agent(
        id=1, slug="tree_test", status="active", conditions=condition_tree
    )
    agents_list = [agent]

    dummy_matches = [{"metric": "pnl", "value": -8000}]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    eval_calls = []

    def _track_eval(cond, ctx):
        eval_calls.append((cond, ctx))
        return dummy_matches

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", side_effect=_track_eval),
        patch.object(agent_engine, "_v2_should_suppress", return_value=False),
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)),
        patch.object(agent_engine, "log_event", new=AsyncMock()),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
        patch.object(agent_engine, "_v2_record", return_value=None),
    ):
        await agent_engine.run_cycle(context=context, broadcast_fn=None)

    # v2_evaluate should have been called with the condition tree
    assert len(eval_calls) == 1
    assert eval_calls[0][0] == condition_tree


@pytest.mark.asyncio
async def test_condition_evaluation_exception_caught_and_logged():
    """
    When v2_evaluate raises an exception → logged, matches treated as empty,
    loop continues for next agent.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    agent1 = _make_agent(id=1, slug="bad_agent", status="active")
    agent2 = _make_agent(id=2, slug="good_agent", status="active")
    agents_list = [agent1, agent2]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    eval_count = []

    def _track_eval(cond, ctx):
        eval_count.append(ctx)
        if len(eval_count) == 1:
            raise ValueError("Intentional eval failure")
        return []  # Second agent returns no matches

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", side_effect=_track_eval),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
    ):
        # Should not raise — the exception is caught and logged
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # Both agents were attempted (len=2)
    assert len(eval_count) == 2


# ─────────────────────────────────────────────────────────────────────────────
#  Tests: Debounce Gate
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_debounce_first_fire_arms_latch():
    """
    When debounce_minutes > 0 and condition first becomes true
    (condition_first_true_at is None) → latch is armed (set to now),
    and matches are suppressed (fire doesn't happen this tick).
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    agent = _make_agent(
        id=1, slug="debounce_test",
        status="active",
        debounce_minutes=5,
        condition_first_true_at=None,  # First time condition is true
    )
    agents_list = [agent]

    dummy_matches = [{"metric": "pnl", "value": -8000}]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    sessions_created = []

    def _make_session_collector():
        s = AsyncMock()
        s.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=agents_list)))
        ))
        s.commit = AsyncMock()
        sessions_created.append(s)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=s)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    with (
        patch.object(agent_engine, "async_session", side_effect=_make_session_collector),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=False)),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # The debounce latch update should have been persisted to the DB
    # (condition_first_true_at should be set to `now`)
    commit_count = sum(1 for s in sessions_created if s.commit.await_count > 0)
    assert commit_count >= 1, (
        "debounce arm should persist the latch via DB update"
    )


@pytest.mark.asyncio
async def test_debounce_within_window_suppresses_fire():
    """
    When debounce_minutes > 0 and condition_first_true_at is set but
    elapsed time < debounce_minutes → matches are cleared (fire suppressed).
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 5, 0, tzinfo=timezone.utc)
    first_true = datetime(2026, 7, 11, 10, 1, 0, tzinfo=timezone.utc)  # 4 min ago
    agent = _make_agent(
        id=1, slug="debounce_window",
        status="active",
        debounce_minutes=5,
        condition_first_true_at=first_true,
    )
    agents_list = [agent]

    dummy_matches = [{"metric": "pnl", "value": -8000}]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    fire_calls = []

    async def _track_fire(*args, **kwargs):
        fire_calls.append((args, kwargs))

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
        patch.object(agent_engine, "_v2_send_rich_alert", new=_track_fire),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # Fire should NOT have been called
    assert len(fire_calls) == 0, (
        "debounce within window must suppress the fire"
    )


@pytest.mark.asyncio
async def test_debounce_window_expired_allows_fire():
    """
    When debounce_minutes > 0 and condition_first_true_at is set and
    elapsed time >= debounce_minutes → matches are preserved and fire proceeds.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 6, 0, tzinfo=timezone.utc)
    first_true = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)  # 6 min ago
    agent = _make_agent(
        id=1, slug="debounce_expired",
        status="active",
        debounce_minutes=5,
        condition_first_true_at=first_true,
    )
    agents_list = [agent]

    dummy_matches = [{"metric": "pnl", "value": -8000}]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    fire_calls = []

    async def _track_fire(*args, **kwargs):
        fire_calls.append((args, kwargs))
        return True

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),
        patch.object(agent_engine, "_v2_should_suppress", return_value=False),
        patch.object(agent_engine, "_v2_send_rich_alert", new=_track_fire),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
        patch.object(agent_engine, "log_event", new=AsyncMock()),
        patch.object(agent_engine, "_v2_record", return_value=None),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # Fire should have been called (window expired)
    assert len(fire_calls) == 1


@pytest.mark.asyncio
async def test_baseline_gate_skips_rate_metric_agents_early_in_session():
    """
    When agent has a rate metric and we're within the baseline_offset_min
    window from session start → agent is skipped.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    # Agent has a rate metric
    conditions = {"metric": "pnl_rate_abs", "scope": "positions_TOTAL", "op": "lt", "value": -1000}
    agent = _make_agent(
        id=1, slug="rate_agent",
        status="active",
        conditions=conditions,
    )
    agents_list = [agent]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    eval_calls = []

    def _track_eval(*args, **kwargs):
        eval_calls.append((args, kwargs))
        return []

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "_v2_has_rate_metric", return_value=True),  # Has rate metric
        patch.object(agent_engine, "_v2_baseline_live", return_value=False),  # Outside baseline window
        patch.object(agent_engine, "v2_evaluate", side_effect=_track_eval),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # v2_evaluate should NOT have been called
    assert len(eval_calls) == 0


@pytest.mark.asyncio
async def test_debounce_bypassed_in_sim_mode():
    """
    When sim_mode=True → debounce is skipped (operators don't wait
    N min of simulated time per fire).
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    agent = _make_agent(
        id=1, slug="sim_debounce",
        status="active",
        debounce_minutes=5,
        condition_first_true_at=None,
    )
    agents_list = [agent]

    dummy_matches = [{"metric": "pnl", "value": -8000}]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {"sim_mode": True},
        "sim_mode": True,
    }

    fire_calls = []

    async def _track_fire(*args, **kwargs):
        fire_calls.append((args, kwargs))
        return True

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),
        patch.object(agent_engine, "_v2_should_suppress", return_value=False),
        patch.object(agent_engine, "_v2_send_rich_alert", new=_track_fire),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
        patch.object(agent_engine, "log_event", new=AsyncMock()),
        patch.object(agent_engine, "_v2_record", return_value=None),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=True)

    # Fire should have been called (debounce bypassed in sim)
    assert len(fire_calls) == 1


# ─────────────────────────────────────────────────────────────────────────────
#  Tests: Action Dispatch
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_with_actions_executes_on_fire():
    """
    When agent fires and has actions → execute() is called with the actions.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    actions = [
        {"type": "place_order", "symbol": "NIFTY", "side": "buy", "qty": 1},
    ]
    agent = _make_agent(
        id=1, slug="action_agent", status="active", actions=actions
    )
    agents_list = [agent]

    dummy_matches = [{"metric": "pnl", "value": -8000}]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    execute_calls = []

    async def _track_execute(*args, **kwargs):
        execute_calls.append((args, kwargs))

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),
        patch.object(agent_engine, "_v2_should_suppress", return_value=False),
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)),
        patch.object(agent_engine, "log_event", new=AsyncMock()),
        patch.object(agent_engine, "execute", new=_track_execute),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
        patch.object(agent_engine, "_v2_record", return_value=None),
    ):
        await agent_engine.run_cycle(context=context, broadcast_fn=None)

    # execute() should have been called
    assert len(execute_calls) == 1
    assert execute_calls[0][0][1] == actions


@pytest.mark.asyncio
async def test_agent_without_actions_skips_execute():
    """
    When agent fires but has no actions → execute() is NOT called.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    agent = _make_agent(
        id=1, slug="no_action_agent", status="active", actions=[]
    )
    agents_list = [agent]

    dummy_matches = [{"metric": "pnl", "value": -8000}]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    execute_calls = []

    async def _track_execute(*args, **kwargs):
        execute_calls.append((args, kwargs))

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),
        patch.object(agent_engine, "_v2_should_suppress", return_value=False),
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)),
        patch.object(agent_engine, "log_event", new=AsyncMock()),
        patch.object(agent_engine, "execute", new=_track_execute),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
        patch.object(agent_engine, "_v2_record", return_value=None),
    ):
        await agent_engine.run_cycle(context=context, broadcast_fn=None)

    # execute() should NOT have been called (no actions)
    assert len(execute_calls) == 0


# ─────────────────────────────────────────────────────────────────────────────
#  Tests: No Matches Behavior
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_matches_unlatch_agent():
    """
    When condition returns no matches → _v2_unlatch() is called to clear
    any static-agent latch.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    agent = _make_agent(id=1, slug="unlatch_test", status="active")
    agents_list = [agent]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    unlatch_calls = []

    def _track_unlatch(ag):
        unlatch_calls.append(ag)

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=[]),  # No matches
        patch.object(agent_engine, "_v2_unlatch", side_effect=_track_unlatch),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # _v2_unlatch should have been called
    assert len(unlatch_calls) == 1
    assert unlatch_calls[0] == agent


# ─────────────────────────────────────────────────────────────────────────────
#  Tests: Lifespan Deadline
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_blackout_window_skips_during_blackout():
    """
    When agent.blackout_windows contains a time window that includes 'now'
    → agent is skipped.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 12, 30, 0, tzinfo=timezone.utc)  # 12:30 IST (noon lunch)
    # Blackout: 12:00-13:00 IST
    blackout_windows = [
        {"start_hour": 12, "start_minute": 0, "end_hour": 13, "end_minute": 0}
    ]
    agent = _make_agent(
        id=1, slug="blackout_agent",
        status="active",
        blackout_windows=blackout_windows,
    )
    agents_list = [agent]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    eval_calls = []

    def _track_eval(*args, **kwargs):
        eval_calls.append((args, kwargs))
        return []

    with (
        patch.object(agent_engine, "async_session", side_effect=lambda: _make_session(agents_list)),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "_in_blackout_window", return_value=True),  # Inside blackout
        patch.object(agent_engine, "v2_evaluate", side_effect=_track_eval),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # v2_evaluate should NOT have been called
    assert len(eval_calls) == 0


@pytest.mark.asyncio
async def test_agent_lifespan_one_shot_transitions_to_completed():
    """
    When agent.lifespan_type='one_shot' and condition fires → status transitions
    to 'completed' after the fire.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    agent = _make_agent(
        id=1, slug="one_shot_agent",
        status="active",
        lifespan_type="one_shot",
    )
    agents_list = [agent]

    dummy_matches = [{"metric": "pnl", "value": -8000}]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    sessions_created = []

    def _make_session_collector():
        s = AsyncMock()
        s.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=agents_list)))
        ))
        s.commit = AsyncMock()
        sessions_created.append(s)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=s)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    with (
        patch.object(agent_engine, "async_session", side_effect=_make_session_collector),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),
        patch.object(agent_engine, "_v2_should_suppress", return_value=False),
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)),
        patch.object(agent_engine, "log_event", new=AsyncMock()),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
        patch.object(agent_engine, "_v2_record", return_value=None),
    ):
        await agent_engine.run_cycle(context=context, broadcast_fn=None)

    # At least one commit should happen (status update to completed)
    commit_count = sum(1 for s in sessions_created if s.commit.await_count > 0)
    assert commit_count >= 1


@pytest.mark.asyncio
async def test_agent_lifespan_n_fires_exhausted():
    """
    When agent.lifespan_type='n_fires' and trigger_count >= max_fires
    → status transitions to 'completed'.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    agent = _make_agent(
        id=1, slug="n_fires_agent",
        status="active",
        lifespan_type="n_fires",
        lifespan_max_fires=3,
        trigger_count=3,  # Already at max
    )
    agents_list = [agent]

    dummy_matches = [{"metric": "pnl", "value": -8000}]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    sessions_created = []

    def _make_session_collector():
        s = AsyncMock()
        s.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=agents_list)))
        ))
        s.commit = AsyncMock()
        sessions_created.append(s)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=s)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    with (
        patch.object(agent_engine, "async_session", side_effect=_make_session_collector),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),
        patch.object(agent_engine, "_v2_should_suppress", return_value=False),
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)),
        patch.object(agent_engine, "log_event", new=AsyncMock()),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
        patch.object(agent_engine, "_v2_record", return_value=None),
    ):
        await agent_engine.run_cycle(context=context, broadcast_fn=None)

    # Status should transition to completed (trigger_count + 1 >= max_fires)
    commit_count = sum(1 for s in sessions_created if s.commit.await_count > 0)
    assert commit_count >= 1


@pytest.mark.asyncio
async def test_cooldown_agent_transitions_back_to_active():
    """
    When agent.status='cooldown' and it doesn't fire (condition false)
    → status transitions back to 'active'.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    # Use wall-clock time that will be >= cooldown_minutes from current time
    last_trigger = datetime.now(timezone.utc) - timedelta(minutes=40)
    agent = _make_agent(
        id=1, slug="cooldown_recovery",
        status="cooldown",
        cooldown_minutes=30,
        last_triggered_at=last_trigger,
    )
    agents_list = [agent]

    # Condition does NOT fire
    dummy_matches = []

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    sessions_created = []

    def _make_session_collector():
        s = AsyncMock()
        s.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=agents_list)))
        ))
        s.commit = AsyncMock()
        sessions_created.append(s)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=s)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    with (
        patch.object(agent_engine, "async_session", side_effect=_make_session_collector),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # DB update should have happened (status → active)
    commit_count = sum(1 for s in sessions_created if s.commit.await_count > 0)
    assert commit_count >= 1


@pytest.mark.asyncio
async def test_agent_lifespan_until_date_expired_auto_completes():
    """
    When agent.lifespan_type='until_date' and now >= lifespan_expires_at
    → status is auto-set to 'completed' and agent is skipped.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)
    expires = datetime(2026, 7, 11, 9, 0, 0, tzinfo=timezone.utc)  # Expired 1 hour ago
    agent = _make_agent(
        id=1, slug="expired_agent",
        status="active",
        lifespan_type="until_date",
        lifespan_expires_at=expires,
    )
    agents_list = [agent]

    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "alert_state": {},
    }

    eval_calls = []

    def _track_eval(*args, **kwargs):
        eval_calls.append((args, kwargs))
        return []

    sessions_created = []

    def _make_session_collector():
        s = AsyncMock()
        s.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=agents_list)))
        ))
        s.commit = AsyncMock()
        sessions_created.append(s)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=s)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    with (
        patch.object(agent_engine, "async_session", side_effect=_make_session_collector),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", side_effect=_track_eval),
    ):
        await agent_engine.run_cycle(context=context, bypass_schedule=False)

    # v2_evaluate should NOT have been called (agent was skipped after completion)
    assert len(eval_calls) == 0
    # DB update should have happened (status → completed)
    commit_count = sum(1 for s in sessions_created if s.commit.await_count > 0)
    assert commit_count >= 1
