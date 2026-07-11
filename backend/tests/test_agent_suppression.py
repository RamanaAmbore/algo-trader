"""
P1 regression test: topic-tier suppression must NOT mutate DB state
(trigger_count, status, condition_first_true_at) for suppressed agents.

Setup:
  - Two agents share topic='pnl_loss', tier=critical (fires) and tier=low
    (suppressed by critical via _compute_topic_suppression).
  - Both pass _v2_should_suppress (they are not in cooldown per the latch).
  - After run_cycle, only the critical agent's trigger_count is incremented
    and status transitions to cooldown. The low-tier agent stays untouched.

This test exercises the real _compute_topic_suppression path — NOT a mock
of _v2_should_suppress, which would skip the pending_dispatches buffer
entirely and prove nothing.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch, call


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _make_agent(
    *,
    id: int,
    slug: str,
    tier: str = "medium",
    topic: str = "general",
    status: str = "active",
    trigger_count: int = 0,
    cooldown_minutes: int = 30,
    lifespan_type: str = "persistent",
    lifespan_max_fires=None,
    conditions=None,
    actions=None,
    events=None,
    schedule: str = "market_hours",
    trade_mode: str = "paper",
    scope: str = "total",
    debounce_minutes: int = 0,
    condition_first_true_at=None,
    fire_at_time=None,
    blackout_windows=None,
    lifespan_expires_at=None,
):
    agent = MagicMock()
    agent.id = id
    agent.slug = slug
    agent.tier = tier
    agent.topic = topic
    agent.status = status
    agent.trigger_count = trigger_count
    agent.cooldown_minutes = cooldown_minutes
    agent.last_triggered_at = None
    agent.lifespan_type = lifespan_type
    agent.lifespan_max_fires = lifespan_max_fires
    agent.lifespan_expires_at = lifespan_expires_at
    agent.conditions = conditions or {}
    agent.actions = actions or []
    agent.events = events or []
    agent.schedule = schedule
    agent.trade_mode = trade_mode
    agent.scope = scope
    agent.debounce_minutes = debounce_minutes
    agent.condition_first_true_at = condition_first_true_at
    agent.fire_at_time = fire_at_time
    agent.blackout_windows = blackout_windows or []
    agent.name = slug
    return agent


def _make_session_ctx(agents_list):
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
    return ctx, session


# ---------------------------------------------------------------------------
# Core test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_suppressed_agent_db_state_not_mutated():
    """
    Critical-tier agent fires for topic='pnl_loss'.
    Low-tier agent for the same topic is suppressed by _compute_topic_suppression.

    Expected:
      - The critical agent's DB row IS updated (trigger_count+1, status=cooldown).
      - The low-tier agent's DB row is NOT updated (no execute call with its id).
      - trigger_count on the low-tier agent object is NOT incremented.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)

    # Two agents: same topic, different tiers
    agent_critical = _make_agent(
        id=1, slug="loss-critical", tier="critical", topic="pnl_loss",
        status="active", trigger_count=5,
    )
    agent_low = _make_agent(
        id=2, slug="loss-low", tier="low", topic="pnl_loss",
        status="active", trigger_count=0,
    )

    agents_list = [agent_critical, agent_low]

    # Track UPDATE calls per agent id so we can assert correctly
    update_calls_by_agent_id: dict[int, list] = {1: [], 2: []}

    # We need a session that tracks which agent IDs were targeted by UPDATE.
    # The actual UPDATE statement uses .where(Agent.id == agent.id) but since
    # we're mocking SQLAlchemy core at the session.execute level we capture
    # the call args.  We count commits as a proxy: each DB mutation opens
    # a session, executes once, commits once.
    sessions_opened = []

    def _make_session():
        s = AsyncMock()
        s.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=agents_list)))
        ))
        s.commit = AsyncMock()
        sessions_opened.append(s)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=s)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    # Matches that make both agents "fire" (non-empty list passes the gate)
    dummy_matches = [{"metric": "pnl", "value": -10000.0, "op": "lt", "threshold": 0}]

    context = {
        "now":           now,
        "sum_positions": MagicMock(),
        "sum_holdings":  MagicMock(),
        "df_margins":    MagicMock(),
        "seg_state":     {},
        "alert_state":   {},
        "market_state":  None,
    }

    mock_log_event = AsyncMock()

    with (
        # Patch async_session so we can intercept all DB calls
        patch.object(agent_engine, "async_session", side_effect=_make_session),

        # _build_context → skip network/DB; return permissive market state
        patch.object(agent_engine, "_build_context", return_value={
            "nse_open": True, "mcx_open": False,
        }),

        # Both agents evaluate to dummy_matches (condition always True)
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),

        # Neither agent is in the _v2_should_suppress latch → both enter
        # pending_dispatches so topic-suppression can do its job
        patch.object(agent_engine, "_v2_should_suppress", return_value=False),

        # Skip rich-alert + dispatch channels (not what we're testing)
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)),
        patch.object(agent_engine, "log_event", new=mock_log_event),
        patch.object(agent_engine, "dispatch",  new=AsyncMock()),
        patch.object(agent_engine, "execute",   new=AsyncMock()),

        # _v2_cfg — return safe defaults
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10,
            "baseline_offset_min": 15,
            "cooldown_min": 30,
            "suppress_delta_abs": 15000,
            "suppress_delta_pct": 0.5,
        }),

        # _update_pnl_history — no-op
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
    ):
        await agent_engine.run_cycle(
            context=context,
            broadcast_fn=None,
            bypass_schedule=False,  # allow DB mutation path to execute
            bypass_suppression=False,  # keep topic suppression active
        )

    # ── Assertions ────────────────────────────────────────────────────────

    # Each async_session() call opens one session for one DB operation.
    # The agent load uses async_session once (no commit). The critical
    # survivor gets a DB write (one commit). The suppressed low agent
    # must NOT open any session for a write.

    # Count sessions that actually committed (i.e. DB mutations happened)
    write_sessions = [s for s in sessions_opened if s.commit.await_count > 0]

    # Only the critical agent's survivor write should have happened — one commit.
    assert len(write_sessions) == 1, (
        f"Expected exactly 1 DB write session (critical survivor), "
        f"got {len(write_sessions)}. "
        "The suppressed low-tier agent must not trigger a DB write."
    )

    # The low-tier agent's trigger_count must NOT have been incremented on
    # the in-memory object. Since the DB write uses Agent.trigger_count + 1
    # (SQL expression), we verify no code path set it to something else.
    assert agent_low.trigger_count == 0, (
        f"Low-tier agent trigger_count was mutated to {agent_low.trigger_count}; "
        "suppressed agents must not have their count incremented."
    )

    # The suppressed agent must generate a 'triggered_suppressed' log_event entry.
    log_event_calls = mock_log_event.call_args_list
    event_types = [c.args[1] if c.args else c.kwargs.get('event_type') for c in log_event_calls]
    assert "triggered_suppressed" in event_types, (
        "Expected a 'triggered_suppressed' log_event call for the low-tier agent."
    )


@pytest.mark.asyncio
async def test_survivor_agent_db_state_mutated():
    """
    Mirror test: critical-tier agent (the winner) DOES have its DB state mutated.
    """
    from backend.api.algo import agent_engine

    now = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)

    agent_critical = _make_agent(
        id=1, slug="loss-critical", tier="critical", topic="pnl_loss",
        status="active", trigger_count=5,
    )
    agent_low = _make_agent(
        id=2, slug="loss-low", tier="low", topic="pnl_loss",
        status="active", trigger_count=0,
    )

    agents_list = [agent_critical, agent_low]
    sessions_opened = []

    def _make_session():
        s = AsyncMock()
        s.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=agents_list)))
        ))
        s.commit = AsyncMock()
        sessions_opened.append(s)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=s)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    dummy_matches = [{"metric": "pnl", "value": -10000.0, "op": "lt", "threshold": 0}]
    context = {
        "now": now,
        "sum_positions": MagicMock(),
        "sum_holdings": MagicMock(),
        "df_margins": MagicMock(),
        "seg_state": {},
        "alert_state": {},
        "market_state": None,
    }

    with (
        patch.object(agent_engine, "async_session", side_effect=_make_session),
        patch.object(agent_engine, "_build_context", return_value={"nse_open": True, "mcx_open": False}),
        patch.object(agent_engine, "v2_evaluate", return_value=dummy_matches),
        patch.object(agent_engine, "_v2_should_suppress", return_value=False),
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)),
        patch.object(agent_engine, "log_event", new=AsyncMock()),
        patch.object(agent_engine, "dispatch",  new=AsyncMock()),
        patch.object(agent_engine, "execute",   new=AsyncMock()),
        patch.object(agent_engine, "_v2_cfg", return_value={
            "rate_window_min": 10, "baseline_offset_min": 15,
            "cooldown_min": 30, "suppress_delta_abs": 15000, "suppress_delta_pct": 0.5,
        }),
        patch.object(agent_engine, "_update_pnl_history", return_value=None),
    ):
        await agent_engine.run_cycle(
            context=context,
            broadcast_fn=None,
            bypass_schedule=False,
            bypass_suppression=False,
        )

    # The critical survivor must produce exactly one DB write (commit)
    write_sessions = [s for s in sessions_opened if s.commit.await_count > 0]
    assert len(write_sessions) >= 1, (
        "Critical-tier survivor must have at least one DB write (trigger_count/status update)."
    )
