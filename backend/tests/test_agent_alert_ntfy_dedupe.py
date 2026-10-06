"""
Regression test for the agent-alert ntfy dual-dispatch bug.

`_ae_dispatch_survivor_entry` (backend/api/algo/agent_engine.py) sends a rich
alert via `_v2_send_rich_alert` (telegram+email+ntfy, routed through
alert_utils._dispatch -> _alert_route per `alert_routing.agent_alert` in
backend_config.yaml), then ALSO calls `events.dispatch()` for the same fire
(log/websocket/inapp channels that have no rich-path equivalent).

Before the fix, `events.dispatch()`'s skip_channels only ever excluded
telegram/email, so ntfy fired a second time for every agent alert whenever
the rich path succeeded — confirmed live incident: 4 duplicate ntfy pushes
for one MCX pre-close event.

This test exercises the real `send_ntfy_alert` call path at the lowest
mockable layer (`backend.shared.helpers.alert_utils.send_ntfy_alert`) across
BOTH the rich-alert branch (`_alert_route`) and the plain-dispatch branch
(`events._dispatch_channel`), and asserts it is invoked exactly once per
trigger — not twice.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch


def _make_agent(*, id=1, slug="loss-funds-negative", name="loss-funds-negative",
                 events=None, tier="critical", fire_at_time=None, actions=None):
    agent = MagicMock()
    agent.id = id
    agent.slug = slug
    agent.name = name
    agent.events = events or [
        {"channel": "telegram", "enabled": True},
        {"channel": "email", "enabled": True},
        {"channel": "ntfy", "enabled": True},
    ]
    agent.tier = tier
    agent.fire_at_time = fire_at_time
    agent.actions = actions or []
    return agent


@pytest.mark.asyncio
async def test_ntfy_fires_exactly_once_when_rich_alert_succeeds():
    """
    Full path through _ae_dispatch_survivor_entry: rich alert succeeds
    (ntfy routed via _alert_route), then events.dispatch() must skip ntfy
    (as well as telegram/email) to avoid a duplicate send.
    """
    from backend.api.algo import agent_engine
    from backend.api.algo.events import EvalResult

    agent = _make_agent()
    now = datetime(2026, 10, 2, 10, 0, 0, tzinfo=timezone.utc)
    result = EvalResult(triggered=True, condition_text="cash < 0", detail={})
    matches = [{"metric": "cash", "scope": "TOTAL", "value": -500.0}]

    entry = {
        "agent": agent,
        "matches": matches,
        "result": result,
        "sim_mode": False,
        "bypass_schedule": True,  # skip the DB-update branch entirely
    }

    with (
        patch("backend.api.algo.agent_engine.logger") as log_mock,
        patch("backend.api.algo.events.is_enabled", return_value=True),
        patch("backend.shared.helpers.alert_utils.is_enabled", return_value=True),
        patch("backend.shared.helpers.alert_utils.config") as cfg_mock,
        patch("backend.shared.helpers.alert_utils._send_telegram"),
        patch("backend.shared.helpers.alert_utils.send_ntfy_alert") as ntfy_mock,
        patch("backend.api.algo.events._send_telegram", new=AsyncMock()),
        patch("backend.api.algo.events._send_email_raw", new=AsyncMock()),
        patch("backend.api.algo.events.agent_event_queue") as queue_mock,
    ):
        cfg_mock.get.side_effect = lambda key, default=None: (
            {"agent_alert": {"telegram": "ops", "ntfy": "urgent", "email": True}}
            if key == "alert_routing" else default
        )
        queue_mock.enqueue = AsyncMock()

        await agent_engine._ae_dispatch_survivor_entry(entry, now, context={}, broadcast_fn=None)

    assert ntfy_mock.call_count == 0, (
        "events.dispatch() must not send ntfy for an alert the rich path recorded"
    )
    records = [c.kwargs["extra"] for c in log_mock.info.call_args_list
               if (c.kwargs.get("extra") or {}).get("event") == "rich_alert"]
    assert len(records) == 1, "the rich alert must be recorded exactly once"

    from types import SimpleNamespace
    from backend.api.algo import event_agents
    rich_agent = SimpleNamespace(slug="agent-alert-rich", **{
        k: event_agents.RICH_ALERT_AGENT[k] for k in ("conditions", "events", "actions")})
    rec = {"ts": now, "level": "INFO", "logger": "backend.api.algo.agent_engine",
           "message": "x", "tags": ["info", "agent"], "extra": records[0]}
    with patch.object(event_agents, "_channel_enabled", lambda cap: True), \
         patch("backend.shared.helpers.alert_utils.send_ntfy_alert") as ntfy_event, \
         patch("backend.shared.helpers.alert_utils.config", {"deploy_branch": "main"}):
        await event_agents.dispatch([rec], [rich_agent])
    assert ntfy_event.call_count == 1, (
        f"ntfy should fire exactly once per agent alert, got {ntfy_event.call_count} calls"
    )


@pytest.mark.asyncio
async def test_events_dispatch_skip_channels_includes_ntfy_when_rich_sent():
    """
    Narrower unit check: the skip_channels frozenset passed to dispatch()
    includes 'ntfy' whenever the rich alert succeeded.
    """
    from backend.api.algo import agent_engine
    from backend.api.algo.events import EvalResult

    agent = _make_agent()
    now = datetime(2026, 10, 2, 10, 0, 0, tzinfo=timezone.utc)
    result = EvalResult(triggered=True, condition_text="cash < 0", detail={})
    entry = {
        "agent": agent,
        "matches": [{"metric": "cash", "scope": "TOTAL", "value": -500.0}],
        "result": result,
        "sim_mode": False,
        "bypass_schedule": True,
    }

    dispatch_mock = AsyncMock()
    with (
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=True)),
        patch.object(agent_engine, "dispatch", new=dispatch_mock),
    ):
        await agent_engine._ae_dispatch_survivor_entry(entry, now, context={}, broadcast_fn=None)

    dispatch_mock.assert_awaited_once()
    _, kwargs = dispatch_mock.call_args
    skip = kwargs.get("skip_channels")
    assert skip is not None and "ntfy" in skip, (
        f"expected 'ntfy' in skip_channels when rich alert succeeded, got {skip!r}"
    )
    assert "telegram" in skip and "email" in skip


@pytest.mark.asyncio
async def test_events_dispatch_does_not_skip_ntfy_when_rich_alert_failed():
    """
    When the rich alert did NOT succeed (e.g. no rows, send failure), the
    plain dispatch() path is the only delivery path and must NOT skip ntfy.
    """
    from backend.api.algo import agent_engine
    from backend.api.algo.events import EvalResult

    agent = _make_agent()
    now = datetime(2026, 10, 2, 10, 0, 0, tzinfo=timezone.utc)
    result = EvalResult(triggered=True, condition_text="cash < 0", detail={})
    entry = {
        "agent": agent,
        "matches": [{"metric": "cash", "scope": "TOTAL", "value": -500.0}],
        "result": result,
        "sim_mode": False,
        "bypass_schedule": True,
    }

    dispatch_mock = AsyncMock()
    with (
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=False)),
        patch.object(agent_engine, "dispatch", new=dispatch_mock),
    ):
        await agent_engine._ae_dispatch_survivor_entry(entry, now, context={}, broadcast_fn=None)

    dispatch_mock.assert_awaited_once()
    _, kwargs = dispatch_mock.call_args
    skip = kwargs.get("skip_channels")
    assert not skip, f"expected empty skip_channels when rich alert failed, got {skip!r}"
