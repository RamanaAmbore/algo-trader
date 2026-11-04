"""
Part B (2026-10-12) — `notify=[...]` on action tokens.
2026-11-03 — converted from direct per-channel sends to a single
log+event-agent record (alert-agent audit Wave 2; see
`actions.py:_al_maybe_notify_action`'s own module-header comment for
the full chain).

An action of any kind may declare its own `notify` SIBLING key (never
inside `params`):
  {"type": "place_order", "params": {...},
   "notify": [{"channel": "telegram"},
              {"channel": "ntfy", "params": {"priority": "urgent"}}]}

Covers the mandatory safety invariants from the plan file's "Part B,
revised and approved" section — all preserved unchanged by the
2026-11-03 conversion:
  1. Mode-gating — only `mode == "live"` ever writes a log record;
     sim/paper/replay/shadow never do.
  2. Masking — an account identifier in an action param never reaches
     the logged message text unmasked.
  3. Success-only firing — a failed action never fires notify (no
     double-report alongside the existing failure-alert path).
  4. Throttle — the 11th notify-triggering action for the same agent in
     one `execute()` call is skipped; the first 10 write a record.
  5. `_action_place_order`'s real-outcome capture (Part B.1,
     actions_live.py) — `context["_action_result_detail"]` reflects the
     real `ChaseResult.status`/`.detail` after `chase_order()` returns.
  6. Notify-record exceptions are caught/logged, never propagate out of
     `execute()`'s loop, and never mark the action as failed.
  7. NEW — the logged record carries `alert_event="action_notify"`,
     the resolved `channels` list (telegram/ntfy/email entries with
     their own `params` flattened in), and the real firing agent's
     `agent_id`/`agent_name`.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def _make_agent(slug: str = "test-agent", id_: int = 1):
    agent = MagicMock()
    agent.slug = slug
    agent.id = id_
    agent.name = "Test Agent"
    return agent


# ---------------------------------------------------------------------------
# 1. Mode-gating — live-only, no default
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sim", "paper", "replay", "shadow"])
async def test_notify_never_logs_in_non_live_modes(mode):
    from backend.api.algo.actions import _al_maybe_notify_action

    agent = _make_agent()
    action = {
        "type": "place_order",
        "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "BUY", "qty": 2},
        "notify": [{"channel": "telegram"}],
    }
    context: dict = {}

    with patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
        await _al_maybe_notify_action(
            agent, action, "place_order", action["params"], mode, context,
        )

    mock_logger.info.assert_not_called()


@pytest.mark.asyncio
async def test_notify_logs_one_record_in_live_mode():
    from backend.api.algo.actions import _al_maybe_notify_action

    agent = _make_agent()
    action = {
        "type": "place_order",
        "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "BUY", "qty": 2},
        "notify": [{"channel": "telegram"}, {"channel": "ntfy", "params": {"priority": "urgent"}}],
    }
    context: dict = {}

    with patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
        await _al_maybe_notify_action(
            agent, action, "place_order", action["params"], "live", context,
        )

    mock_logger.info.assert_called_once()
    extra = mock_logger.info.call_args.kwargs["extra"]
    assert extra["alert_event"] == "action_notify"
    assert extra["agent_id"] == agent.id
    assert extra["agent_name"] == agent.name
    channels = {c["channel"]: c for c in extra["channels"]}
    assert channels["telegram"]["enabled"] is True
    assert channels["ntfy"]["enabled"] is True
    assert channels["ntfy"]["priority"] == "urgent"


@pytest.mark.asyncio
async def test_notify_noop_when_no_notify_key():
    """Zero overhead when action.get('notify') is absent — the ~100% case
    for existing agents."""
    from backend.api.algo.actions import _al_maybe_notify_action

    agent = _make_agent()
    action = {"type": "place_order", "params": {"account": "ZG0790"}}
    context: dict = {}

    with patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
        await _al_maybe_notify_action(
            agent, action, "place_order", action["params"], "live", context,
        )

    mock_logger.info.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Masking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_masks_account_identifier_in_logged_message():
    from backend.api.algo.actions import _al_maybe_notify_action
    from backend.shared.helpers.utils import mask_account_in_text

    agent = _make_agent()
    action = {
        "type": "place_order",
        "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "SELL", "qty": 1},
        "notify": [{"channel": "telegram"}],
    }
    context: dict = {}

    with patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
        await _al_maybe_notify_action(
            agent, action, "place_order", action["params"], "live", context,
        )

    mock_logger.info.assert_called_once()
    body = mock_logger.info.call_args.kwargs["extra"]["body"]
    assert "ZG0790" not in body
    assert body == mask_account_in_text(body)


# ---------------------------------------------------------------------------
# 3. Success-only firing — wired via execute()'s loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_failed_action_never_fires_notify():
    """A failed action (handler raises) must route to
    `_al_action_failed_audit` and must NEVER reach `_al_maybe_notify_
    action` — no double-report alongside the existing failure-alert
    path."""
    from backend.api.algo.actions import execute

    agent = _make_agent()
    actions = [
        {
            "type": "place_order",
            "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "SELL", "qty": 1},
            "notify": [{"channel": "telegram"}],
        },
    ]
    context: dict = {}

    mock_dispatch = AsyncMock(side_effect=RuntimeError("broker rejected"))
    mock_audit = AsyncMock()
    mock_notify = AsyncMock()

    with patch("backend.api.algo.actions._resolve_mode", return_value="live"), \
         patch("backend.api.algo.actions._exchange_gate_passes", return_value=(True, "")), \
         patch("backend.api.algo.actions._al_dispatch_by_mode", new=mock_dispatch), \
         patch("backend.api.algo.actions._al_action_failed_audit", new=mock_audit), \
         patch("backend.api.algo.actions._al_maybe_notify_action", new=mock_notify), \
         patch("backend.api.algo.actions._log_action_success", new=AsyncMock()):
        await execute(agent, actions, context)

    mock_audit.assert_called_once()
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_execute_successful_action_fires_notify_after_success_log():
    """A successful action (ok=True) reaches `_al_maybe_notify_action`
    exactly once, and never `_al_action_failed_audit`."""
    from backend.api.algo.actions import execute

    agent = _make_agent()
    actions = [
        {
            "type": "place_order",
            "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "SELL", "qty": 1},
            "notify": [{"channel": "telegram"}],
        },
    ]
    context: dict = {}

    mock_dispatch = AsyncMock(return_value=True)
    mock_audit = AsyncMock()
    mock_notify = AsyncMock()

    with patch("backend.api.algo.actions._resolve_mode", return_value="live"), \
         patch("backend.api.algo.actions._exchange_gate_passes", return_value=(True, "")), \
         patch("backend.api.algo.actions._al_dispatch_by_mode", new=mock_dispatch), \
         patch("backend.api.algo.actions._al_action_failed_audit", new=mock_audit), \
         patch("backend.api.algo.actions._al_maybe_notify_action", new=mock_notify), \
         patch("backend.api.algo.actions._log_action_success", new=AsyncMock()):
        await execute(agent, actions, context)

    mock_notify.assert_called_once()
    mock_audit.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Throttle — cap at _MAX_NOTIFY_PER_AGENT_PER_TICK (10) per agent per tick
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_throttle_caps_at_ten_per_agent_per_tick():
    from backend.api.algo.actions import execute, _MAX_NOTIFY_PER_AGENT_PER_TICK

    assert _MAX_NOTIFY_PER_AGENT_PER_TICK == 10

    agent = _make_agent()
    actions = [
        {
            "type": "place_order",
            "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "SELL", "qty": 1},
            "notify": [{"channel": "telegram"}],
        }
        for _ in range(11)
    ]
    context: dict = {}

    with patch("backend.api.algo.actions._resolve_mode", return_value="live"), \
         patch("backend.api.algo.actions._exchange_gate_passes", return_value=(True, "")), \
         patch("backend.api.algo.actions._al_dispatch_by_mode", new=AsyncMock(return_value=True)), \
         patch("backend.api.algo.actions._log_action_success", new=AsyncMock()), \
         patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
        await execute(agent, actions, context)

    assert mock_logger.info.call_count == 10, (
        f"expected exactly 10 log records (11th throttled), got {mock_logger.info.call_count}"
    )


# ---------------------------------------------------------------------------
# 5. _action_place_order's real-outcome capture (Part B.1)
# ---------------------------------------------------------------------------

def _make_conns_stub(account: str) -> MagicMock:
    c = MagicMock()
    c.conn = {account: object()}
    return c


def _make_broker_stub(*, ltp_value: float = 23500.0) -> MagicMock:
    broker = MagicMock()
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "MCX", "BSE", "CDS"]}
    broker.instruments.return_value = []
    broker.basket_order_margins.return_value = [{"initial": {"total": 5_000.0}}]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": 500_000.0},
        "commodity": {"enabled": True, "net": 500_000.0},
    }
    broker.ltp.return_value = {"NFO:NIFTY25JULFUT": {"last_price": ltp_value}}
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


@pytest.mark.asyncio
@pytest.mark.parametrize("status_name,detail_text", [
    ("FAILED", "Failed after 20 attempts"),
    ("PARTIAL", "partial fill 50/100"),
    ("CANCELLED", "operator cancelled"),
])
async def test_action_place_order_captures_real_chase_outcome(status_name, detail_text):
    """mock chase_order returning a terminal non-FILLED ChaseResult —
    context['_action_result_detail'] must reflect that real status/detail
    after _action_place_order returns, independent of the notify wrapper."""
    from backend.api.algo.actions import _action_place_order
    from backend.api.algo.chase import ChaseResult, ChaseStatus

    broker = _make_broker_stub()
    conns = _make_conns_stub("ZG0790")
    agent = _make_agent()
    context: dict = {}
    params = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "exchange": "NFO",
        "transaction_type": "SELL",
        "quantity": 50,
    }

    result = ChaseResult(status=ChaseStatus[status_name], detail=detail_text)
    mock_chase = AsyncMock(return_value=result)

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size", new=AsyncMock(return_value=50)), \
         patch("backend.api.algo.chase.chase_order", new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order", new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.actions_live._place_order_set_product_template", new=AsyncMock()), \
         patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.brokers.client.is_cutover_on", return_value=False):
        await _action_place_order(agent, context, params)

    mock_chase.assert_called_once()
    assert context["_action_result_detail"] == {
        "status": status_name.lower(),
        "detail": detail_text,
    }


# ---------------------------------------------------------------------------
# 6. Notify-send failure isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_log_record_exception_is_caught_and_logged_not_propagated():
    """A log-record failure (simulated — the logger itself raising) must
    be caught inside _al_maybe_notify_action and never propagate."""
    from backend.api.algo.actions import _al_maybe_notify_action

    agent = _make_agent()
    action = {
        "type": "place_order",
        "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "SELL", "qty": 1},
        "notify": [{"channel": "telegram"}],
    }
    context: dict = {}

    mock_logger = MagicMock()
    mock_logger.info.side_effect = RuntimeError("logging subsystem blip")

    with patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER", mock_logger):
        # Must not raise.
        await _al_maybe_notify_action(
            agent, action, "place_order", action["params"], "live", context,
        )


@pytest.mark.asyncio
async def test_execute_notify_log_record_exception_never_marks_action_failed():
    """A notify log-record failure inside execute()'s loop must never
    route to `_al_action_failed_audit` — it's caught at the wiring call
    site too."""
    from backend.api.algo.actions import execute

    agent = _make_agent()
    actions = [
        {
            "type": "place_order",
            "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "SELL", "qty": 1},
            "notify": [{"channel": "telegram"}],
        },
    ]
    context: dict = {}

    mock_audit = AsyncMock()
    mock_logger = MagicMock()
    mock_logger.info.side_effect = RuntimeError("logging subsystem blip")

    with patch("backend.api.algo.actions._resolve_mode", return_value="live"), \
         patch("backend.api.algo.actions._exchange_gate_passes", return_value=(True, "")), \
         patch("backend.api.algo.actions._al_dispatch_by_mode", new=AsyncMock(return_value=True)), \
         patch("backend.api.algo.actions._al_action_failed_audit", new=mock_audit), \
         patch("backend.api.algo.actions._log_action_success", new=AsyncMock()), \
         patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER", mock_logger):
        # Must not raise out of execute()'s loop.
        await execute(agent, actions, context)

    mock_audit.assert_not_called()


# ---------------------------------------------------------------------------
# 8. End-to-end: a real LogStoreHandler, not a mocked dispatch — same
#    precedent as test_alert_header_identity_2026_11_03.py /
#    test_log_store.py::test_order_failure_and_summary_records_reach_the_queue.
#    Confirms the record genuinely survives the real logger pipeline
#    (never dropped by `_SKIP_PREFIXES`) and is picked up by
#    `_render_action_notify`.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_record_reaches_a_real_log_handler_and_renders():
    import logging
    from backend.shared.helpers import log_store
    from backend.api.algo.actions import _al_maybe_notify_action
    from backend.api.algo.event_agents import _render_action_notify

    agent = _make_agent()
    action = {
        "type": "place_order",
        "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "BUY", "qty": 2},
        "notify": [{"channel": "telegram"}, {"channel": "ntfy", "params": {"priority": "urgent"}}],
    }
    context: dict = {}

    h = log_store.LogStoreHandler()
    dispatch_logger = logging.getLogger("backend.api.algo.events")
    dispatch_logger.addHandler(h)
    dispatch_logger.setLevel(logging.INFO)
    try:
        await _al_maybe_notify_action(
            agent, action, "place_order", action["params"], "live", context,
        )
    finally:
        dispatch_logger.removeHandler(h)

    rows = h.drain(10)
    notify_rows = [r for r in rows if r["extra"].get("alert_event") == "action_notify"]
    assert notify_rows, "action_notify record never reached the queue"
    rec = notify_rows[0]
    assert rec["extra"]["agent_id"] == agent.id
    assert rec["extra"]["agent_name"] == agent.name

    title, body, tg, email_payload, channels = _render_action_notify(rec)
    # 2026-11-04 (operator alert-format audit): the bespoke
    # "Agent [{agent_key}] action: ..." string is gone — the body now
    # leads with the same `format_notification_header()` origin line
    # every other renderer in the system uses ("Agent: {name} (#{id})"),
    # followed by "action: {description}".
    assert body.startswith(f"Agent: {agent.name} (#{agent.id})\n")
    assert "action: " in body
    assert "BUY 2 NIFTY25JULFUT" in body
    assert email_payload is None
    channel_names = {c["channel"] for c in channels}
    assert {"telegram", "ntfy"} <= channel_names
