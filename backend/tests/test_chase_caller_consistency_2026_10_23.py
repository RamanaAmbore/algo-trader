"""
Tests for the 2026-10-23 chase-robustness "caller consistency" item
(item 5):
  - `_action_live_close_position` (actions_live.py) no longer silently
    discards a FAILED/HELD ChaseResult — it raises (so `execute()`
    records action_failed) and alerts exactly once for the one
    uncovered no-internal-alert path (market-closed / non-prod-branch
    early return inside chase_order()).
  - `_start_live_chase`'s post-placement `on_event` handler
    (orders_helpers.py) no longer alerts on a recoverable "error" event
    that chase keeps retrying from — every genuinely terminal
    "chase_failed"/"chase_cancel_failed" outcome already alerts from
    inside chase.py itself.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo.chase import ChaseResult, ChaseStatus
from backend.tests.test_actions import _make_broker_stub, _make_conns_stub


# ── _action_live_close_position ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_position_market_closed_result_alerts_once_and_raises():
    from backend.api.algo.actions_live import _action_live_close_position

    broker = _make_broker_stub()
    conns = _make_conns_stub("ZG0790")
    agent = MagicMock()
    agent.slug = "test-close"
    agent.id = 1
    context: dict = {}
    params = {"account": "ZG0790", "symbol": "NIFTY25JULFUT",
              "exchange": "NFO", "quantity": 50, "side": "SELL"}

    market_closed_result = ChaseResult(
        status=ChaseStatus.FAILED, detail="NFO closed — chase not started",
    )
    mock_chase = AsyncMock(return_value=market_closed_result)
    mock_alert = MagicMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size", new=AsyncMock(return_value=1)), \
         patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=50.0)), \
         patch("backend.api.algo.chase.chase_order", mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.brokers.client.is_cutover_on", return_value=False), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert):
        with pytest.raises(RuntimeError):
            await _action_live_close_position(agent, context, params)

    mock_alert.assert_called_once(), (
        "the market-closed early return inside chase_order() fires NO "
        "internal alert at all — the caller must fill that one gap"
    )


@pytest.mark.asyncio
async def test_close_position_other_failed_result_raises_without_duplicate_alert():
    from backend.api.algo.actions_live import _action_live_close_position

    broker = _make_broker_stub()
    conns = _make_conns_stub("ZG0790")
    agent = MagicMock()
    agent.slug = "test-close"
    agent.id = 1
    context: dict = {}
    params = {"account": "ZG0790", "symbol": "NIFTY25JULFUT",
              "exchange": "NFO", "quantity": 50, "side": "SELL"}

    exhausted_result = ChaseResult(
        status=ChaseStatus.FAILED, detail="Failed after 20 attempts (0/50 filled)",
    )
    mock_chase = AsyncMock(return_value=exhausted_result)
    mock_alert = MagicMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size", new=AsyncMock(return_value=1)), \
         patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=50.0)), \
         patch("backend.api.algo.chase.chase_order", mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.brokers.client.is_cutover_on", return_value=False), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert):
        with pytest.raises(RuntimeError):
            await _action_live_close_position(agent, context, params)

    mock_alert.assert_not_called(), (
        "an exhaustion FAILED result already alerted from INSIDE "
        "chase.py's own _ch_exhaust_max_attempts — the caller must not "
        "fire a second, duplicate alert"
    )


@pytest.mark.asyncio
async def test_close_position_held_result_raises():
    from backend.api.algo.actions_live import _action_live_close_position

    broker = _make_broker_stub()
    conns = _make_conns_stub("ZG0790")
    agent = MagicMock()
    agent.slug = "test-close"
    agent.id = 1
    context: dict = {}
    params = {"account": "ZG0790", "symbol": "NIFTY25JULFUT",
              "exchange": "NFO", "quantity": 50, "side": "SELL"}

    held_result = ChaseResult(status=ChaseStatus.HELD, detail="held for review")
    mock_chase = AsyncMock(return_value=held_result)

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size", new=AsyncMock(return_value=1)), \
         patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=50.0)), \
         patch("backend.api.algo.chase.chase_order", mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.brokers.client.is_cutover_on", return_value=False):
        with pytest.raises(RuntimeError, match="HELD"):
            await _action_live_close_position(agent, context, params)


@pytest.mark.asyncio
async def test_close_position_filled_result_does_not_raise():
    from backend.api.algo.actions_live import _action_live_close_position

    broker = _make_broker_stub()
    conns = _make_conns_stub("ZG0790")
    agent = MagicMock()
    agent.slug = "test-close"
    agent.id = 1
    context: dict = {}
    params = {"account": "ZG0790", "symbol": "NIFTY25JULFUT",
              "exchange": "NFO", "quantity": 50, "side": "SELL"}

    filled_result = ChaseResult(status=ChaseStatus.FILLED, fill_price=100.0)
    mock_chase = AsyncMock(return_value=filled_result)

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size", new=AsyncMock(return_value=1)), \
         patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=50.0)), \
         patch("backend.api.algo.chase.chase_order", mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.brokers.client.is_cutover_on", return_value=False):
        await _action_live_close_position(agent, context, params)  # must not raise


# ── _start_live_chase on_event (ticket path double-alert fix) ──────────

@pytest.mark.asyncio
async def test_start_live_chase_post_placement_recoverable_error_does_not_alert():
    import backend.api.routes.orders_helpers as oh

    async def _fake_chase_order(account, symbol, transaction_type, quantity,
                                cfg, on_event=None, algo_order_id=None):
        on_event("order_placed", {"order_id": "B1"})
        on_event("error", {"error": "network blip"})
        return SimpleNamespace(status="filled")

    mock_alert = MagicMock()
    with patch("backend.api.algo.chase.chase_order", new=_fake_chase_order), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert):
        order_id = await oh._start_live_chase(
            account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
            transaction_type="SELL", quantity=50, aggressiveness="low",
        )

    assert order_id == "B1"
    mock_alert.assert_not_called(), (
        "a recoverable 'error' event mid-chase must never alert from "
        "this handler — chase keeps retrying from it, and alerting "
        "here is a guaranteed false alarm when the chase goes on to "
        "fill cleanly"
    )
