"""
Tests for api/algo/events.py — agent event dispatcher.
SSOT: dispatch() routes to all enabled channels; _dispatch_channel handles routing.
Perf: event logging via async queue (not per-call DB INSERT).
Stale: agent_event_queue is a module-level EventQueue (not per-request).
Reuse: EvalResult dataclass shared between grammar engine and dispatch path.
UX: failed channel dispatch logs error but doesn't block other channels.
"""
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.fixture(autouse=True)
def _direct_channels(monkeypatch):
    """These tests cover the dispatch loop. Telegram, email, and ntfy are recorded and sent by the event path."""
    from backend.api.algo import events
    monkeypatch.setattr(events, "_RECORDED_CHANNELS", frozenset())

_SRC = Path("backend/api/algo/events.py").read_text()


def test_dispatch_function_exists():
    from backend.api.algo.events import dispatch
    import inspect
    assert inspect.iscoroutinefunction(dispatch), "dispatch must be async"


def test_eval_result_dataclass_exists():
    from backend.api.algo.events import EvalResult
    r = EvalResult(triggered=True, condition_text="pnl > 1000", detail={"pnl": 1500})
    assert r.triggered is True
    assert r.condition_text == "pnl > 1000"


def test_agent_event_queue_is_module_level():
    from backend.api.algo.events import agent_event_queue
    assert agent_event_queue is not None, "agent_event_queue must be a module-level EventQueue"


def test_dispatch_channel_function_exists():
    from backend.api.algo.events import _dispatch_channel
    import inspect
    assert inspect.iscoroutinefunction(_dispatch_channel), "_dispatch_channel must be async"


def test_dispatch_handles_channel_errors_gracefully():
    """Failed channel dispatch must log error and continue (not propagate)."""
    assert "except Exception" in _SRC or "except" in _SRC, (
        "dispatch must catch channel errors to ensure other channels still fire"
    )
    assert "logger.error" in _SRC, (
        "Failed dispatches must be logged as errors for observability"
    )


def test_log_event_is_async():
    from backend.api.algo.events import _log_event
    import inspect
    assert inspect.iscoroutinefunction(_log_event), "_log_event must be async"


def test_event_uses_queue_not_direct_db():
    """Events must use the queue (async, batched) rather than direct DB writes."""
    assert "agent_event_queue.enqueue" in _SRC, (
        "_log_event must use agent_event_queue.enqueue() for batched INSERTs, "
        "not a direct session.add() per event"
    )


# ═══════════════════════════════════════════════════════════════════════════
#  EvalResult Dataclass Tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_eval_result_construction():
    """EvalResult can be constructed with all fields."""
    from backend.api.algo.events import EvalResult
    result = EvalResult(
        triggered=True,
        condition_text="pnl > 5000",
        detail={"pnl": 5500, "account": "ACC123"}
    )
    assert result.triggered is True
    assert result.condition_text == "pnl > 5000"
    assert result.detail["pnl"] == 5500


@pytest.mark.asyncio
async def test_eval_result_false_triggered():
    """EvalResult with triggered=False."""
    from backend.api.algo.events import EvalResult
    result = EvalResult(triggered=False, condition_text="", detail={})
    assert result.triggered is False


# ═══════════════════════════════════════════════════════════════════════════
#  Dispatch Function Tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_dispatch_calls_all_enabled_channels():
    """dispatch() routes to each enabled channel."""
    from backend.api.algo.events import dispatch, EvalResult
    from backend.api.algo.template_registry import resolve_events

    # Mock agent
    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.events = [
        {"channel": "telegram", "enabled": True},
        {"channel": "email", "enabled": True},
        {"channel": "log", "enabled": False},  # disabled
    ]

    eval_result = EvalResult(
        triggered=True,
        condition_text="pnl > 1000",
        detail={"pnl": 1500}
    )

    # Mock _dispatch_channel to track calls
    dispatch_calls = []
    async def mock_dispatch_channel(ch, *args, **kwargs):
        dispatch_calls.append(ch.get("channel"))

    with patch('backend.api.algo.events._dispatch_channel', side_effect=mock_dispatch_channel), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock):
        await dispatch(agent, eval_result, sim_mode=False)

    # Should call enabled channels only
    assert "telegram" in dispatch_calls
    assert "email" in dispatch_calls
    assert "log" not in dispatch_calls


@pytest.mark.asyncio
async def test_dispatch_includes_branch_tag():
    """dispatch() includes branch tag when not main."""
    from backend.api.algo.events import _build_dispatch_email_body

    html = _build_dispatch_email_body(
        agent_name="TestAgent", agent_id=1,
        sim_tag="",
        branch="dev",
        branch_tag=" [dev]",
        ist_display="2026-07-26 10:30:00",
        condition_text="pnl > 1000",
        sim_mode=False
    )
    assert "[dev]" in html


@pytest.mark.asyncio
async def test_dispatch_includes_simulator_banner():
    """dispatch() includes simulator warning when sim_mode=True."""
    from backend.api.algo.events import _build_dispatch_email_body

    html = _build_dispatch_email_body(
        agent_name="TestAgent", agent_id=1,
        sim_tag="SIMULATOR ",
        branch="main",
        branch_tag="",
        ist_display="2026-07-26 10:30:00",
        condition_text="pnl > 1000",
        sim_mode=True
    )
    assert "SIMULATOR" in html
    assert "#fde4e4" in html  # red background


@pytest.mark.asyncio
async def test_dispatch_handles_channel_exception():
    """dispatch() catches channel errors and logs, continues with others."""
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test"
    agent.slug = "test"
    agent.events = [
        {"channel": "telegram", "enabled": True},
        {"channel": "email", "enabled": True},
    ]

    eval_result = EvalResult(triggered=True, condition_text="test", detail={})

    call_order = []
    async def mock_dispatch_channel(ch, *args, **kwargs):
        channel = ch.get("channel")
        call_order.append(channel)
        if channel == "telegram":
            raise RuntimeError("Telegram API down")
        # email succeeds

    with patch('backend.api.algo.events._dispatch_channel', side_effect=mock_dispatch_channel), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock), \
         patch('backend.api.algo.events.logger') as mock_logger:
        await dispatch(agent, eval_result)

    # Both channels should have been attempted
    assert "telegram" in call_order
    assert "email" in call_order
    # Error should be logged
    assert mock_logger.error.called


# ═══════════════════════════════════════════════════════════════════════════
#  Email Body Builder Tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_build_dispatch_email_body_main_branch():
    """Email for main branch has no branch banner."""
    from backend.api.algo.events import _build_dispatch_email_body

    html = _build_dispatch_email_body(
        agent_name="MyAgent", agent_id=1,
        sim_tag="",
        branch="main",
        branch_tag="",
        ist_display="2026-07-26 10:00",
        condition_text="loss > 5000",
        sim_mode=False
    )
    assert "MyAgent" in html
    assert "loss > 5000" in html
    assert "[main]" not in html


@pytest.mark.asyncio
async def test_build_dispatch_email_body_dev_branch():
    """Email for dev branch includes branch banner."""
    from backend.api.algo.events import _build_dispatch_email_body

    html = _build_dispatch_email_body(
        agent_name="MyAgent", agent_id=1,
        sim_tag="",
        branch="dev",
        branch_tag=" [dev]",
        ist_display="2026-07-26 10:00",
        condition_text="loss > 5000",
        sim_mode=False
    )
    assert "[dev]" in html
    assert "#fef3c7" in html  # yellow background


# ═══════════════════════════════════════════════════════════════════════════
#  Dispatch Channel Function Tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_dispatch_channel_websocket():
    """Dispatch channel websocket via broadcast_fn."""
    from backend.api.algo.events import _dispatch_channel, EvalResult
    from unittest.mock import MagicMock

    ch = {"channel": "websocket", "enabled": True}
    agent = MagicMock()
    agent.name = "Test"
    agent.slug = "test-slug"

    broadcast_fn = MagicMock()
    eval_result = EvalResult(triggered=True, condition_text="test", detail={})

    await _dispatch_channel(
        ch, agent, "msg", "", "", "test", "2026-07-26", eval_result,
        broadcast_fn, False, "main", ""
    )
    broadcast_fn.assert_called_once_with("agent_alert", {
        "slug": "test-slug",
        "message": "msg",
        "condition": "test",
        "sim_mode": False,
    })


@pytest.mark.asyncio
async def test_dispatch_channel_inapp():
    """Dispatch channel inapp via broadcast_fn."""
    from backend.api.algo.events import _dispatch_channel, EvalResult
    from unittest.mock import MagicMock

    ch = {"channel": "inapp", "enabled": True}
    agent = MagicMock()
    agent.name = "Test"
    agent.slug = "test"
    agent.tier = "warning"
    agent.topic = "risk"

    broadcast_fn = MagicMock()
    eval_result = EvalResult(triggered=True, condition_text="test", detail={"x": 1})

    await _dispatch_channel(
        ch, agent, "", "", "", "test", "2026-07-26", eval_result,
        broadcast_fn, False, "main", ""
    )
    call_args = broadcast_fn.call_args
    assert call_args[0][0] == "agent_inapp_notify"
    assert call_args[0][1]["slug"] == "test"
    assert call_args[0][1]["tier"] == "warning"


@pytest.mark.asyncio
async def test_dispatch_channel_log_is_now_a_noop():
    """'log' moved off _dispatch_channel's direct-send branches onto the
    _RECORDED_CHANNELS / event_agents.py CHANNELS table (Sprint 6) — calling
    _dispatch_channel directly with channel='log' must no longer write
    anything via events.logger. (Its old behaviour — slug/condition/[SIM]/
    branch_tag in the warning line — now lives in
    event_agents._send_log_channel; see test_event_agents.py.)"""
    from backend.api.algo.events import _dispatch_channel
    from unittest.mock import MagicMock, patch

    ch = {"channel": "log", "enabled": True}
    agent = MagicMock()
    agent.name = "Test"
    agent.slug = "test-slug"

    with patch('backend.api.algo.events.logger') as mock_logger:
        await _dispatch_channel(
            ch, agent, "", "", "", "test condition", "2026-07-26", None,
            None, False, "main", ""
        )
        mock_logger.warning.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
#  Log Event Tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_log_event_enqueues_via_queue():
    """_log_event enqueues to agent_event_queue."""
    from backend.api.algo.events import _log_event

    agent = MagicMock()
    agent.id = 123

    with patch('backend.api.algo.events.agent_event_queue') as mock_queue:
        mock_queue.enqueue = AsyncMock()
        await _log_event(agent, "triggered", "pnl > 1000", {"pnl": 1500})

        mock_queue.enqueue.assert_called_once()
        call_kwargs = mock_queue.enqueue.call_args[1]
        assert call_kwargs["agent_id"] == 123
        assert call_kwargs["event_type"] == "triggered"
        assert call_kwargs["trigger_condition"] == "pnl > 1000"


@pytest.mark.asyncio
async def test_log_event_handles_exception():
    """_log_event catches enqueue exceptions and logs."""
    from backend.api.algo.events import _log_event
    from unittest.mock import MagicMock, AsyncMock, patch

    agent = MagicMock()
    agent.id = 1

    with patch('backend.api.algo.events.agent_event_queue') as mock_queue, \
         patch('backend.api.algo.events.logger') as mock_logger:
        mock_queue.enqueue = AsyncMock(side_effect=RuntimeError("Queue full"))
        await _log_event(agent, "triggered", "test", {})

        mock_logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_log_event_json_serializes_detail():
    """_log_event serializes detail dict to JSON."""
    from backend.api.algo.events import _log_event
    from unittest.mock import MagicMock, AsyncMock, patch
    import json

    agent = MagicMock()
    agent.id = 1

    with patch('backend.api.algo.events.agent_event_queue') as mock_queue:
        mock_queue.enqueue = AsyncMock()
        detail = {"x": 1, "y": 2}
        await _log_event(agent, "triggered", "test", detail)

        call_kwargs = mock_queue.enqueue.call_args[1]
        # detail should be JSON-serialized
        assert isinstance(call_kwargs["detail"], str)
        assert json.loads(call_kwargs["detail"]) == detail


@pytest.mark.asyncio
async def test_log_event_none_detail():
    """_log_event with None detail → enqueue None."""
    from backend.api.algo.events import _log_event
    from unittest.mock import MagicMock, AsyncMock, patch

    agent = MagicMock()
    agent.id = 1

    with patch('backend.api.algo.events.agent_event_queue') as mock_queue:
        mock_queue.enqueue = AsyncMock()
        await _log_event(agent, "triggered", "test", None)

        call_kwargs = mock_queue.enqueue.call_args[1]
        assert call_kwargs["detail"] is None


@pytest.mark.asyncio
async def test_log_event_wrapper():
    """log_event() convenience wrapper calls _log_event()."""
    from backend.api.algo.events import log_event
    from unittest.mock import MagicMock, AsyncMock, patch

    agent = MagicMock()
    agent.id = 1

    with patch('backend.api.algo.events._log_event', new_callable=AsyncMock) as mock_log:
        await log_event(agent, "triggered", "test cond", {"detail": 1})

        mock_log.assert_called_once_with(agent, "triggered", "test cond", {"detail": 1}, sim_mode=False)


# ═══════════════════════════════════════════════════════════════════════════
#  Event Resolution Tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_dispatch_resolves_events_from_agent():
    """dispatch() calls resolve_events() to expand fragments."""
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test"
    agent.slug = "test"
    agent.events = [{"$ref": "notify-trio"}]  # fragment

    eval_result = EvalResult(triggered=True, condition_text="test", detail={})

    resolved = [{"channel": "telegram", "enabled": True}]

    with patch('backend.api.algo.template_registry.resolve_events', return_value=resolved) as mock_resolve, \
         patch('backend.api.algo.events._dispatch_channel', new_callable=AsyncMock), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock):
        await dispatch(agent, eval_result)

        mock_resolve.assert_called_once_with([{"$ref": "notify-trio"}])


# ═══════════════════════════════════════════════════════════════════════════
#  Integration Tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_dispatch_full_path_sim_mode():
    """dispatch() in sim_mode passes sim_mode through to all surfaces."""
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test"
    agent.slug = "test"
    agent.events = [{"channel": "log", "enabled": True}]

    eval_result = EvalResult(triggered=True, condition_text="test", detail={})

    with patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._dispatch_channel', new_callable=AsyncMock) as mock_dispatch, \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock) as mock_log:
        await dispatch(agent, eval_result, sim_mode=True)

        # _dispatch_channel should receive sim_mode=True as positional arg (9th position)
        mock_dispatch.assert_called_once()
        call_args = mock_dispatch.call_args[0]
        # dispatch_channel signature: ch, agent, telegram_body, email_subject, email_body,
        #                             condition_text, ist_display, eval_result, broadcast_fn,
        #                             sim_mode (9th), branch, branch_tag
        assert call_args[9] is True  # sim_mode position

        # _log_event should receive sim_mode=True
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args[1]
        assert call_kwargs.get("sim_mode") is True


# ═══════════════════════════════════════════════════════════════════════════
#  _dispatch_channel Detailed Routing Tests (Missing Coverage)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_dispatch_channel_websocket_calls_broadcast_agent_alert():
    """websocket channel calls broadcast_fn("agent_alert", {...}) with slug, message, condition, sim_mode."""
    from backend.api.algo.events import _dispatch_channel, EvalResult

    ch = {"channel": "websocket", "enabled": True}
    agent = MagicMock()
    agent.id = 1
    agent.slug = "test-agent-slug"
    agent.name = "Test Agent"

    telegram_body = "Alert message"
    condition_text = "pnl > 1000"
    broadcast_fn = MagicMock()
    eval_result = EvalResult(triggered=True, condition_text=condition_text, detail={})

    await _dispatch_channel(
        ch, agent, telegram_body, "", "",
        condition_text, "2026-07-26", eval_result,
        broadcast_fn, False, "main", ""
    )

    broadcast_fn.assert_called_once()
    call_args = broadcast_fn.call_args[0]
    assert call_args[0] == "agent_alert"
    payload = call_args[1]
    assert payload["slug"] == "test-agent-slug"
    assert payload["message"] == telegram_body
    assert payload["condition"] == condition_text
    assert payload["sim_mode"] is False


@pytest.mark.asyncio
async def test_dispatch_channel_inapp_calls_broadcast_agent_inapp_notify():
    """inapp channel calls broadcast_fn("agent_inapp_notify", {...}) with all required fields."""
    from backend.api.algo.events import _dispatch_channel, EvalResult

    ch = {"channel": "inapp", "enabled": True}
    agent = MagicMock()
    agent.id = 1
    agent.slug = "risk-agent"
    agent.name = "Risk Agent"
    agent.tier = "warning"
    agent.topic = "risk"

    condition_text = "loss > 5000"
    detail = {"loss": 5500, "account": "ACC123"}
    ist_display = "2026-07-26 10:30:00"
    broadcast_fn = MagicMock()
    eval_result = EvalResult(triggered=True, condition_text=condition_text, detail=detail)

    await _dispatch_channel(
        ch, agent, "", "", "",
        condition_text, ist_display, eval_result,
        broadcast_fn, False, "main", ""
    )

    broadcast_fn.assert_called_once()
    call_args = broadcast_fn.call_args[0]
    assert call_args[0] == "agent_inapp_notify"
    payload = call_args[1]
    assert payload["slug"] == "risk-agent"
    assert payload["name"] == "Risk Agent"
    assert payload["tier"] == "warning"
    assert payload["topic"] == "risk"
    assert payload["condition"] == condition_text
    assert payload["detail"] == detail
    assert payload["when"] == ist_display
    assert payload["sim_mode"] is False
    assert payload["branch"] == "main"


@pytest.mark.asyncio
async def test_dispatch_channel_inapp_with_missing_tier_defaults():
    """inapp channel uses getattr with defaults when agent lacks tier/topic."""
    from backend.api.algo.events import _dispatch_channel, EvalResult

    ch = {"channel": "inapp", "enabled": True}
    agent = MagicMock()
    agent.id = 1
    agent.slug = "basic-agent"
    agent.name = "Basic Agent"
    # Deliberately omit tier and topic via spec
    del agent.tier
    del agent.topic

    broadcast_fn = MagicMock()
    eval_result = EvalResult(triggered=True, condition_text="test", detail={})

    await _dispatch_channel(
        ch, agent, "", "", "",
        "test", "2026-07-26", eval_result,
        broadcast_fn, False, "main", ""
    )

    broadcast_fn.assert_called_once()
    payload = broadcast_fn.call_args[0][1]
    assert payload["tier"] == "info"  # default
    assert payload["topic"] is None  # default


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_dispatch_skips_disabled_channels():
    """dispatch() skips channels with enabled=False before calling _dispatch_channel."""
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test"
    agent.slug = "test"
    agent.events = [
        {"channel": "telegram", "enabled": True},
        {"channel": "email", "enabled": False},  # disabled
        {"channel": "log", "enabled": True},
    ]

    eval_result = EvalResult(triggered=True, condition_text="test", detail={})

    dispatch_calls = []
    async def mock_dispatch_channel(ch, *args, **kwargs):
        dispatch_calls.append(ch.get("channel"))

    with patch('backend.api.algo.events._dispatch_channel', side_effect=mock_dispatch_channel), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock):
        await dispatch(agent, eval_result)

    # email should NOT be in calls because it's disabled
    assert "telegram" in dispatch_calls
    assert "log" in dispatch_calls
    assert "email" not in dispatch_calls




# ═══════════════════════════════════════════════════════════════════════════
#  Sprint 6 — per-channel `message` grouping (events.py:_classify_recorded_
#  channel / _emit_breach_record). This module's autouse `_direct_channels`
#  fixture empties `_RECORDED_CHANNELS` for every OTHER test in this file
#  (so they can exercise `_dispatch_channel` directly) — these tests
#  explicitly restore the real set first, since the grouping logic only
#  runs for _RECORDED_CHANNELS members.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_dispatch_groups_messageless_channels_into_one_record():
    """No channel carries a `message` key — dispatch() must emit exactly
    ONE 'ALERT recorded' logger.info() call, in the same shape as before
    Sprint 6's grouping. This is the majority of production traffic
    (every existing threshold/loss-* agent) and must never regress."""
    from backend.api.algo import events
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.tier = "medium"
    agent.events = [
        {"channel": "telegram", "enabled": True},
        {"channel": "email", "enabled": True},
    ]
    eval_result = EvalResult(triggered=True, condition_text="pnl > 1000", detail={})

    info_calls = []
    with patch.object(events, "_RECORDED_CHANNELS", frozenset({"telegram", "email", "ntfy", "log"})), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock), \
         patch.object(events.logger, "info", side_effect=lambda msg, **kw: info_calls.append((msg, kw.get("extra")))):
        await dispatch(agent, eval_result, sim_mode=False)

    assert len(info_calls) == 1, "messageless channels must share exactly one record"
    msg, extra = info_calls[0]
    assert msg == "ALERT recorded [test-agent]: Test Agent"
    assert [c["channel"] for c in extra["channels"]] == ["telegram", "email"]
    assert "message" not in extra["channels"][0] and "message" not in extra["channels"][1]
    assert extra["condition_text"] == "pnl > 1000"
    assert extra["telegram_body"] == extra["ntfy_body"]
    assert "Condition: pnl > 1000" in extra["telegram_body"]
    # 2026-11-08 audit fix (item 6) — subject now matches the
    # "Agent: X | ts" shape and carries a real timestamp (not frozen
    # in this test file), so only the shape is checked.
    assert extra["email_subject"].startswith("Agent: Test Agent | ")
    assert "<p><b>Condition:</b> pnl > 1000</p>" in extra["email_body"]
    assert extra["sim_mode"] is False


@pytest.mark.asyncio
async def test_dispatch_treats_empty_string_message_as_no_message():
    """An explicit `message: ""` (or `message: None`) must normalize to
    'no custom message' and join the shared group — never change the
    record's shape vs. the messageless default."""
    from backend.api.algo import events
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.tier = "medium"
    agent.events = [
        {"channel": "telegram", "enabled": True, "message": ""},
        {"channel": "email", "enabled": True, "message": None},
    ]
    eval_result = EvalResult(triggered=True, condition_text="pnl > 1000", detail={})

    info_calls = []
    with patch.object(events, "_RECORDED_CHANNELS", frozenset({"telegram", "email", "ntfy", "log"})), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock), \
         patch.object(events.logger, "info", side_effect=lambda msg, **kw: info_calls.append((msg, kw.get("extra")))):
        await dispatch(agent, eval_result, sim_mode=False)

    assert len(info_calls) == 1
    _, extra = info_calls[0]
    assert len(extra["channels"]) == 2
    for c in extra["channels"]:
        assert "message" not in c


@pytest.mark.asyncio
async def test_dispatch_splits_custom_message_channels_into_their_own_records():
    """A channel with a real `message` override gets its OWN separate
    'ALERT recorded' record (rendered from that message, not
    condition_text) — every messageless channel still shares one shared
    record."""
    from backend.api.algo import events
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.tier = "medium"
    agent.events = [
        {"channel": "telegram", "enabled": True},
        {"channel": "email", "enabled": True},
        {"channel": "ntfy", "enabled": True, "message": "custom ntfy text"},
    ]
    eval_result = EvalResult(triggered=True, condition_text="pnl > 1000", detail={})

    info_calls = []
    with patch.object(events, "_RECORDED_CHANNELS", frozenset({"telegram", "email", "ntfy", "log"})), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock), \
         patch.object(events.logger, "info", side_effect=lambda msg, **kw: info_calls.append((msg, kw.get("extra")))):
        await dispatch(agent, eval_result, sim_mode=False)

    assert len(info_calls) == 2
    shared = next(extra for _, extra in info_calls if len(extra["channels"]) == 2)
    custom = next(extra for _, extra in info_calls if len(extra["channels"]) == 1)

    assert {c["channel"] for c in shared["channels"]} == {"telegram", "email"}
    assert "Condition: pnl > 1000" in shared["telegram_body"]

    assert custom["channels"][0]["channel"] == "ntfy"
    assert custom["channels"][0]["message"] == "custom ntfy text"
    assert "Condition: custom ntfy text" in custom["ntfy_body"]
    assert "pnl > 1000" not in custom["ntfy_body"]
    # condition_text on the top-level extra always reflects the agent's
    # real firing condition, regardless of which group rendered the body.
    assert custom["condition_text"] == "pnl > 1000"
    # email_subject never depends on the message override (only the body does).
    # 2026-11-08 audit fix (item 6) — same shape change as above.
    assert custom["email_subject"] == shared["email_subject"]
    assert shared["email_subject"].startswith("Agent: Test Agent | ")


@pytest.mark.asyncio
async def test_log_channel_now_flows_through_recorded_channels_not_dispatch_channel():
    """'log' moved into _RECORDED_CHANNELS (Sprint 6) — dispatch() must
    route it through the grouped-record path, never call _dispatch_channel
    for it."""
    from backend.api.algo import events
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.tier = "medium"
    agent.events = [{"channel": "log", "enabled": True}]
    eval_result = EvalResult(triggered=True, condition_text="pnl > 1000", detail={})

    dispatch_channel_calls = []
    async def mock_dispatch_channel(ch, *a, **k):
        dispatch_channel_calls.append(ch.get("channel"))

    info_calls = []
    with patch.object(events, "_RECORDED_CHANNELS", frozenset({"telegram", "email", "ntfy", "log"})), \
         patch('backend.api.algo.events._dispatch_channel', side_effect=mock_dispatch_channel), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock), \
         patch.object(events.logger, "info", side_effect=lambda msg, **kw: info_calls.append(kw.get("extra"))):
        await dispatch(agent, eval_result, sim_mode=False)

    assert dispatch_channel_calls == []
    assert len(info_calls) == 1
    extra = info_calls[0]
    assert extra["channels"][0]["channel"] == "log"
    assert extra["channels"][0]["log_line"] == "ALERT [test-agent]: Test Agent — pnl > 1000"
    assert extra["channels"][0]["log_agent_slug"] == "test-agent"
    assert extra["channels"][0]["log_sim_mode"] is False


@pytest.mark.asyncio
async def test_websocket_and_inapp_channels_still_dispatch_directly():
    """Explicit scope-discipline regression guard (Sprint 6 narrowed scope
    to telegram/email/ntfy/log only) — websocket and inapp must still be
    routed through _dispatch_channel exactly as before, unaffected by the
    grouping refactor. See test_event_agents.py for the companion
    assertion that they're absent from _RECORDED_CHANNELS."""
    from backend.api.algo import events
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.tier = "medium"
    agent.events = [
        {"channel": "websocket", "enabled": True},
        {"channel": "inapp", "enabled": True},
    ]
    eval_result = EvalResult(triggered=True, condition_text="pnl > 1000", detail={})

    dispatch_channel_calls = []
    async def mock_dispatch_channel(ch, *a, **k):
        dispatch_channel_calls.append(ch.get("channel"))

    with patch.object(events, "_RECORDED_CHANNELS", frozenset({"telegram", "email", "ntfy", "log"})), \
         patch('backend.api.algo.events._dispatch_channel', side_effect=mock_dispatch_channel), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock):
        await dispatch(agent, eval_result, sim_mode=False)

    assert set(dispatch_channel_calls) == {"websocket", "inapp"}


# ═══════════════════════════════════════════════════════════════════════════
#  Part A (2026-10-12) — action-aware default ALERT message, no new grammar.
#  See .claude/plans/purrfect-marinating-pixel.md "Alert/notify messaging"
#  section. Only the no-`msg=` default path changes; any `msg=` configured
#  anywhere in the clause must remain byte-identical.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_dispatch_default_message_is_action_aware_for_order_action():
    """No msg= anywhere -> the default ALERT body describes what the
    agent's own DO action actually is (side/qty/symbol), not just the
    generic condition-breach text."""
    from backend.api.algo import events
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.tier = "medium"
    agent.events = [{"channel": "telegram", "enabled": True}]
    agent.actions = [
        {"type": "place_order", "params": {
            "account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "SELL",
            "qty": 1, "price": 100,
        }},
    ]
    eval_result = EvalResult(triggered=True, condition_text="pnl > 1000", detail={})

    info_calls = []
    with patch.object(events, "_RECORDED_CHANNELS", frozenset({"telegram", "email", "ntfy", "log"})), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock), \
         patch.object(events.logger, "info", side_effect=lambda msg, **kw: info_calls.append(kw.get("extra"))):
        await dispatch(agent, eval_result, sim_mode=False)

    assert len(info_calls) == 1
    extra = info_calls[0]
    # The real condition-breach text is still present...
    assert "pnl > 1000" in extra["telegram_body"]
    # ...and now the default body also describes the DO action.
    assert "SELL" in extra["telegram_body"]
    assert "NIFTY25JULFUT" in extra["telegram_body"]
    assert "(order)" in extra["telegram_body"]
    # ntfy/email share the same default-message construction.
    assert "SELL" in extra["ntfy_body"]
    assert "SELL" in extra["email_body"]
    # The persisted/raw condition_text field is untouched — only the
    # rendered body text grows the action description.
    assert extra["condition_text"] == "pnl > 1000"


@pytest.mark.asyncio
async def test_dispatch_with_explicit_msg_is_byte_identical_to_no_action_aware_text():
    """An agent whose channel sets an explicit msg= must render EXACTLY
    that text, even though its DO clause has a real place_order action —
    the action-aware default must never leak into a msg=-configured
    channel's body."""
    from backend.api.algo import events
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.tier = "medium"
    agent.events = [
        {"channel": "ntfy", "enabled": True, "message": "Stop loss hit"},
    ]
    agent.actions = [
        {"type": "place_order", "params": {
            "account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "SELL", "qty": 1,
        }},
    ]
    eval_result = EvalResult(triggered=True, condition_text="pnl > 1000", detail={})

    info_calls = []
    with patch.object(events, "_RECORDED_CHANNELS", frozenset({"telegram", "email", "ntfy", "log"})), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock), \
         patch.object(events.logger, "info", side_effect=lambda msg, **kw: info_calls.append(kw.get("extra"))):
        await dispatch(agent, eval_result, sim_mode=False)

    assert len(info_calls) == 1
    extra = info_calls[0]
    assert "Condition: Stop loss hit" in extra["ntfy_body"]
    # Neither the action type nor its params leak into a msg=-configured
    # channel's body — byte-identical to the pre-change custom-message path.
    assert "SELL" not in extra["ntfy_body"]
    assert "(order)" not in extra["ntfy_body"]
    assert "pnl > 1000" not in extra["ntfy_body"]


@pytest.mark.asyncio
async def test_describe_actions_masks_account_identifiers():
    """An action param carrying a raw account code (e.g. order(...)'s
    account=) must never reach the rendered default message unmasked —
    reuses the same mask_account_in_text() chokepoint event_agents.py's
    _render_error/_gate_passes already use."""
    from backend.api.algo.events import _describe_actions
    from backend.shared.helpers.utils import mask_account_in_text

    actions = [
        {"type": "place_order", "params": {
            "account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "BUY", "qty": 2,
        }},
    ]
    desc = _describe_actions(actions)
    assert "ZG0790" not in desc
    assert desc == mask_account_in_text(desc)


@pytest.mark.asyncio
async def test_describe_actions_empty_for_no_actions():
    """An agent with no DO clause (empty/absent actions list) must
    degrade to "" so the default message stays exactly the condition-
    breach text — no appended '()' noise."""
    from backend.api.algo.events import _describe_actions

    assert _describe_actions(None) == ""
    assert _describe_actions([]) == ""


@pytest.mark.asyncio
async def test_dispatch_default_message_unaffected_when_agent_has_no_actions():
    """An ALERT-only / event-kind agent with no DO actions at all must
    produce EXACTLY today's condition-breach-only default body — no
    appended action description, no stray separator."""
    from backend.api.algo import events
    from backend.api.algo.events import dispatch, EvalResult

    agent = MagicMock()
    agent.id = 1
    agent.name = "Test Agent"
    agent.slug = "test-agent"
    agent.tier = "medium"
    agent.events = [{"channel": "telegram", "enabled": True}]
    agent.actions = []
    eval_result = EvalResult(triggered=True, condition_text="pnl > 1000", detail={})

    info_calls = []
    with patch.object(events, "_RECORDED_CHANNELS", frozenset({"telegram", "email", "ntfy", "log"})), \
         patch('backend.api.algo.template_registry.resolve_events', return_value=agent.events), \
         patch('backend.api.algo.events._log_event', new_callable=AsyncMock), \
         patch.object(events.logger, "info", side_effect=lambda msg, **kw: info_calls.append(kw.get("extra"))):
        await dispatch(agent, eval_result, sim_mode=False)

    assert len(info_calls) == 1
    extra = info_calls[0]
    assert extra["telegram_body"].strip().endswith("Condition: pnl > 1000")
    condition_line = [
        line for line in extra["telegram_body"].splitlines() if line.startswith("Condition:")
    ][0]
    assert condition_line == "Condition: pnl > 1000"
