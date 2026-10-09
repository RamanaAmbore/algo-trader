"""
Coverage tests for backend/api/algo/actions.py

Covers:
  - _resolve_mode: sim/replay/paper/live/noop routing
  - _action_target_exchanges: extract target exchanges from action params
  - _exchange_gate_passes: market open checks
  - execute: action sequencing and error handling
  - _maybe_attach_template_from_action: template override resolution
  - _write_live_order: AlgoOrder persistence
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_mode tests
# ─────────────────────────────────────────────────────────────────────────────

def test_resolve_mode_sim_precedence():
    """Sim mode takes precedence over all others."""
    from backend.api.algo.actions import _resolve_mode

    agent = MagicMock()
    agent.trade_mode = "live"  # even if agent is live

    mode = _resolve_mode("place_order", agent, {"sim_mode": True})
    assert mode == "sim"


def test_resolve_mode_replay_precedence():
    """Replay mode has next highest precedence after sim."""
    from backend.api.algo.actions import _resolve_mode

    agent = MagicMock()
    agent.trade_mode = "live"

    mode = _resolve_mode("place_order", agent, {"replay_mode": True})
    assert mode == "replay"


def test_resolve_mode_noop_non_broker_action():
    """Non-broker actions resolve to 'noop' regardless of mode."""
    from backend.api.algo.actions import _resolve_mode

    agent = MagicMock()
    agent.trade_mode = "live"

    mode = _resolve_mode("send_summary", agent, {})
    assert mode == "noop"

    mode = _resolve_mode("emit_log", agent, {})
    assert mode == "noop"

    mode = _resolve_mode("monitor_order", agent, {})
    assert mode == "noop"


def test_resolve_mode_dev_branch_paper():
    """Dev branch (not prod) always maps to paper."""
    from backend.api.algo.actions import _resolve_mode

    agent = MagicMock()
    agent.trade_mode = "live"

    with patch("backend.shared.helpers.utils.is_prod_branch", return_value=False):
        mode = _resolve_mode("place_order", agent, {})
        assert mode == "paper"


def test_resolve_mode_paper_trading_master_killswitch():
    """Master paper_trading_mode wins over agent.trade_mode."""
    from backend.api.algo.actions import _resolve_mode

    agent = MagicMock()
    agent.trade_mode = "live"

    with patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
         patch("backend.shared.helpers.settings.get_bool") as mock_get_bool:
        # paper_trading_mode = True
        mock_get_bool.side_effect = lambda key, default: key == "execution.paper_trading_mode"
        mode = _resolve_mode("place_order", agent, {})
        assert mode == "paper"


def test_resolve_mode_force_paper_context():
    """force_paper in context overrides agent.trade_mode."""
    from backend.api.algo.actions import _resolve_mode

    agent = MagicMock()
    agent.trade_mode = "live"

    with patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
         patch("backend.shared.helpers.settings.get_bool", return_value=False):
        mode = _resolve_mode("place_order", agent, {"force_paper": True})
        assert mode == "paper"


def test_resolve_mode_agent_trade_mode_live():
    """When all conditions pass, agent.trade_mode='live' → live."""
    from backend.api.algo.actions import _resolve_mode

    agent = MagicMock()
    agent.trade_mode = "live"

    with patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
         patch("backend.shared.helpers.settings.get_bool", return_value=False):
        mode = _resolve_mode("place_order", agent, {})
        assert mode == "live"


def test_resolve_mode_agent_trade_mode_paper_default():
    """When agent.trade_mode is not 'live', defaults to paper."""
    from backend.api.algo.actions import _resolve_mode

    agent = MagicMock()
    agent.trade_mode = "paper"

    with patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
         patch("backend.shared.helpers.settings.get_bool", return_value=False):
        mode = _resolve_mode("place_order", agent, {})
        assert mode == "paper"


# ─────────────────────────────────────────────────────────────────────────────
# _action_target_exchanges tests
# ─────────────────────────────────────────────────────────────────────────────

def test_action_target_exchanges_place_order_default_nfo():
    """place_order with no exchange defaults to NFO."""
    from backend.api.algo.actions import _action_target_exchanges

    targets = _action_target_exchanges("place_order", {}, {})
    assert targets == ["NFO"]


def test_action_target_exchanges_place_order_mcx():
    """place_order with explicit exchange extracts it."""
    from backend.api.algo.actions import _action_target_exchanges

    targets = _action_target_exchanges("place_order", {"exchange": "mcx"}, {})
    assert targets == ["MCX"]


def test_action_target_exchanges_close_position_nfo():
    """close_position extracts exchange."""
    from backend.api.algo.actions import _action_target_exchanges

    targets = _action_target_exchanges("close_position", {"exchange": "NFO"}, {})
    assert targets == ["NFO"]


def test_action_target_exchanges_chase_close_empty():
    """chase_close_positions returns [] (handled per position)."""
    from backend.api.algo.actions import _action_target_exchanges

    targets = _action_target_exchanges("chase_close_positions", {}, {})
    assert targets == []


def test_action_target_exchanges_chase_close_empty():
    """chase_close returns [] (handled per position)."""
    from backend.api.algo.actions import _action_target_exchanges

    targets = _action_target_exchanges("chase_close", {}, {})
    assert targets == []


def test_action_target_exchanges_cancel_order():
    """cancel_order extracts exchange."""
    from backend.api.algo.actions import _action_target_exchanges

    targets = _action_target_exchanges("cancel_order", {"exchange": "NSE"}, {})
    assert targets == ["NSE"]


def test_action_target_exchanges_modify_order():
    """modify_order extracts exchange."""
    from backend.api.algo.actions import _action_target_exchanges

    targets = _action_target_exchanges("modify_order", {"exchange": "BSE"}, {})
    assert targets == ["BSE"]


def test_action_target_exchanges_expiry_auto_close_no_exchange():
    """expiry_auto_close with no exchange returns []."""
    from backend.api.algo.actions import _action_target_exchanges

    targets = _action_target_exchanges("expiry_auto_close", {}, {})
    assert targets == []


def test_action_target_exchanges_expiry_auto_close_with_exchange():
    """expiry_auto_close with exchange extracts it."""
    from backend.api.algo.actions import _action_target_exchanges

    targets = _action_target_exchanges("expiry_auto_close", {"exchange": "MCX"}, {})
    assert targets == ["MCX"]


# ─────────────────────────────────────────────────────────────────────────────
# _exchange_gate_passes tests
# ─────────────────────────────────────────────────────────────────────────────

def test_exchange_gate_passes_sim_mode_bypass():
    """Sim mode bypasses exchange gate."""
    from backend.api.algo.actions import _exchange_gate_passes

    allowed, reason = _exchange_gate_passes("place_order", {"exchange": "NFO"}, {"sim_mode": True})
    assert allowed is True
    assert reason == ""


def test_exchange_gate_passes_replay_mode_bypass():
    """Replay mode bypasses exchange gate."""
    from backend.api.algo.actions import _exchange_gate_passes

    allowed, reason = _exchange_gate_passes("place_order", {"exchange": "MCX"}, {"replay_mode": True})
    assert allowed is True
    assert reason == ""


def test_exchange_gate_passes_noop_action_allowed():
    """Non-broker actions are always allowed."""
    from backend.api.algo.actions import _exchange_gate_passes

    allowed, reason = _exchange_gate_passes("send_summary", {}, {})
    assert allowed is True
    assert reason == ""


def test_exchange_gate_passes_no_targets_allowed():
    """Actions with no target exchanges are always allowed."""
    from backend.api.algo.actions import _exchange_gate_passes

    allowed, reason = _exchange_gate_passes("chase_close_positions", {}, {})
    assert allowed is True
    assert reason == ""


def test_exchange_gate_passes_open_exchange_allowed():
    """Exchange open → allowed."""
    from backend.api.algo.actions import _exchange_gate_passes

    context = {"nse_open": True, "mcx_open": False}
    with patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True):
        allowed, reason = _exchange_gate_passes("place_order", {"exchange": "NSE"}, context)
        assert allowed is True
        assert reason == ""


def test_exchange_gate_passes_closed_exchange_blocked():
    """Exchange closed → blocked."""
    from backend.api.algo.actions import _exchange_gate_passes

    context = {"nse_open": False, "mcx_open": True}
    with patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=False):
        allowed, reason = _exchange_gate_passes("place_order", {"exchange": "NSE"}, context)
        assert allowed is False
        assert "closed" in reason.lower()
        assert "NSE" in reason


# ─────────────────────────────────────────────────────────────────────────────
# _build_template_overrides tests
# ─────────────────────────────────────────────────────────────────────────────

def test_build_template_overrides_all_fields():
    """Template override params → dict."""
    from backend.api.algo.actions import _build_template_overrides

    params = {
        "tp_pct_override": 5.0,
        "sl_pct_override": 2.0,
        "wing_premium_pct_override": 1.5,
        "wing_strike_offset_override": 2,
    }
    overrides = _build_template_overrides(params)
    assert overrides["tp_pct"] == 5.0
    assert overrides["sl_pct"] == 2.0
    assert overrides["wing_premium_pct"] == 1.5
    assert overrides["wing_strike_offset"] == 2


def test_build_template_overrides_legacy_target_pct():
    """Legacy target_pct (fractional) → tp_pct (% units)."""
    from backend.api.algo.actions import _build_template_overrides

    params = {"target_pct": 0.05}  # 5% in fractional form
    overrides = _build_template_overrides(params)
    assert overrides["tp_pct"] == 5.0  # converted to % units


def test_build_template_overrides_empty():
    """Empty params → all None."""
    from backend.api.algo.actions import _build_template_overrides

    overrides = _build_template_overrides({})
    assert all(v is None for v in overrides.values())


# ─────────────────────────────────────────────────────────────────────────────
# _maybe_attach_template_from_action tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_maybe_attach_template_no_algo_order_id():
    """When algo_order_id is None, return None (template attach skipped)."""
    from backend.api.algo.actions import _maybe_attach_template_from_action

    result = await _maybe_attach_template_from_action(
        MagicMock(),
        "place_order",
        {},
        algo_order_id=None,
        parent_account="ZG0790",
        parent_symbol="NIFTY25JULFUT",
        parent_side="BUY",
        parent_qty=1,
        parent_exchange="NFO",
        parent_price=24500.0,
    )
    assert result is None


@pytest.mark.asyncio
async def test_maybe_attach_template_no_template_no_override():
    """When neither template nor override supplied, return None."""
    from backend.api.algo.actions import _maybe_attach_template_from_action

    result = await _maybe_attach_template_from_action(
        MagicMock(),
        "place_order",
        {},  # no params
        algo_order_id=42,
        parent_account="ZG0790",
        parent_symbol="NIFTY25JULFUT",
        parent_side="BUY",
        parent_qty=1,
        parent_exchange="NFO",
        parent_price=24500.0,
    )
    assert result is None


@pytest.mark.asyncio
async def test_maybe_attach_template_with_tp_pct_override():
    """When tp_pct_override supplied, call _al_apply_template."""
    from backend.api.algo.actions import _maybe_attach_template_from_action

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    params = {"tp_pct_override": 5.0}

    mock_result = MagicMock()
    mock_result.to_dict.return_value = {"status": "attached"}

    with patch("backend.api.algo.actions._al_apply_template", new=AsyncMock(return_value={"status": "attached"})):
        result = await _maybe_attach_template_from_action(
            agent,
            "place_order",
            params,
            algo_order_id=42,
            parent_account="ZG0790",
            parent_symbol="NIFTY25JULFUT",
            parent_side="BUY",
            parent_qty=1,
            parent_exchange="NFO",
            parent_price=24500.0,
        )
    assert result == {"status": "attached"}


# ─────────────────────────────────────────────────────────────────────────────
# _write_live_order tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_live_order_success():
    """_write_live_order persists AlgoOrder and returns id."""
    from backend.api.algo.actions import _write_live_order

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 123

    resolved = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "side": "SELL",
        "qty": 1,
        "price": 24500.0,
        "exchange": "NFO",
    }

    # Mock the database session
    mock_row = MagicMock()
    mock_row.id = 999

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = MagicMock()
    mock_session.__aenter__.return_value.add = MagicMock()
    mock_session.__aenter__.return_value.commit = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.models.AlgoOrder") as mock_order_class:
        mock_order_class.return_value = mock_row
        result = await _write_live_order(agent, "place_order", resolved)

    assert result == 999


@pytest.mark.asyncio
async def test_write_live_order_market_order():
    """Market order (no price) writes correctly."""
    from backend.api.algo.actions import _write_live_order

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 123

    resolved = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "side": "BUY",
        "qty": 2,
        # no price
        "exchange": "NFO",
    }

    mock_row = MagicMock()
    mock_row.id = 888

    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = MagicMock()
    mock_session.__aenter__.return_value.add = MagicMock()
    mock_session.__aenter__.return_value.commit = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.models.AlgoOrder") as mock_order_class:
        mock_order_class.return_value = mock_row
        result = await _write_live_order(agent, "close_position", resolved)

    assert result == 888


@pytest.mark.asyncio
async def test_write_live_order_db_error_returns_none():
    """DB error during write returns None."""
    from backend.api.algo.actions import _write_live_order

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 123

    resolved = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "side": "SELL",
        "qty": 1,
        "price": 24500.0,
        "exchange": "NFO",
    }

    mock_session = AsyncMock()
    mock_session.__aenter__.side_effect = RuntimeError("DB error")

    with patch("backend.api.database.async_session", return_value=mock_session):
        result = await _write_live_order(agent, "place_order", resolved)

    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# execute tests — action sequencing
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_empty_actions():
    """Empty action list → no-op."""
    from backend.api.algo.actions import execute

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    context = {}
    await execute(agent, [], context)
    # No exception, no side effects


@pytest.mark.asyncio
async def test_execute_noop_action():
    """Non-broker action runs through _dispatch_noop_action."""
    from backend.api.algo.actions import execute

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    actions = [
        {"type": "emit_log", "params": {"message": "test"}}
    ]
    context = {}

    with patch("backend.api.algo.actions._dispatch_noop_action", new=AsyncMock(return_value=True)), \
         patch("backend.api.algo.actions._log_action_success", new=AsyncMock()):
        await execute(agent, actions, context)


@pytest.mark.asyncio
async def test_execute_broker_action_paper_mode():
    """Broker action in paper mode routes through _al_dispatch_by_mode."""
    from backend.api.algo.actions import execute

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1
    agent.trade_mode = "paper"

    actions = [
        {"type": "place_order", "params": {"symbol": "NIFTY25JULFUT"}}
    ]
    context = {}

    with patch("backend.api.algo.actions._resolve_mode", return_value="paper"), \
         patch("backend.api.algo.actions._exchange_gate_passes", return_value=(True, "")), \
         patch("backend.api.algo.actions._al_dispatch_by_mode", new=AsyncMock(return_value=True)), \
         patch("backend.api.algo.actions._log_action_success", new=AsyncMock()):
        await execute(agent, actions, context)


@pytest.mark.asyncio
async def test_execute_exchange_gate_blocks_action():
    """Exchange gate blocks action → skip without logging success."""
    from backend.api.algo.actions import execute

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    actions = [
        {"type": "place_order", "params": {"exchange": "NSE"}}
    ]
    context = {}

    with patch("backend.api.algo.actions._resolve_mode", return_value="paper"), \
         patch("backend.api.algo.actions._exchange_gate_passes", return_value=(False, "NSE closed")), \
         patch("backend.api.algo.actions._al_dispatch_by_mode", new=AsyncMock()) as mock_dispatch:
        with patch("backend.api.algo.events.log_event", new=AsyncMock()):
            await execute(agent, actions, context)

    # _al_dispatch_by_mode should NOT be called
    mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_execute_action_failure_audit_logged():
    """Action exception → audit logged."""
    from backend.api.algo.actions import execute

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1

    actions = [
        {"type": "place_order", "params": {"symbol": "NIFTY25JULFUT"}}
    ]
    context = {}

    mock_error = RuntimeError("broker error")

    with patch("backend.api.algo.actions._resolve_mode", return_value="paper"), \
         patch("backend.api.algo.actions._exchange_gate_passes", return_value=(True, "")), \
         patch("backend.api.algo.actions._al_dispatch_by_mode", side_effect=mock_error), \
         patch("backend.api.algo.actions._al_action_failed_audit", new=AsyncMock()) as mock_audit:
        await execute(agent, actions, context)

    mock_audit.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Grammar token action handlers
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_log_with_level():
    """emit_log handler respects log level."""
    from backend.api.algo.actions import emit_log

    result = await emit_log({}, {"level": "warning", "message": "test warning"})
    assert result["level"] == "warning"
    assert result["message"] == "test warning"


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3 — registry-driven dispatch (_LIVE_ACTION_HANDLERS / _NOOP_ACTION_HANDLERS)
#
# These tests prove the dict-lookup registries added in actions.py route to
# the IDENTICAL handler, with IDENTICAL error-handling semantics, as the
# hardcoded if/elif chains they replaced.
# ═══════════════════════════════════════════════════════════════════════════

def _mk_agent(slug="test-agent", id_=1):
    agent = MagicMock()
    agent.slug = slug
    agent.id = id_
    return agent


# ── Live dispatch — every registered action_type reaches its handler ──────

@pytest.mark.asyncio
async def test_dispatch_live_action_place_order():
    from backend.api.algo.actions import _dispatch_live_action

    agent = _mk_agent()
    mock = AsyncMock()
    with patch("backend.api.algo.actions_live._action_place_order", new=mock):
        await _dispatch_live_action(agent, "place_order", {"a": 1}, {"c": 2})
    mock.assert_called_once_with(agent, {"c": 2}, {"a": 1})


@pytest.mark.asyncio
async def test_dispatch_live_action_close_position():
    from backend.api.algo.actions import _dispatch_live_action

    agent = _mk_agent()
    mock = AsyncMock()
    with patch("backend.api.algo.actions_live._action_live_close_position", new=mock):
        await _dispatch_live_action(agent, "close_position", {"a": 1}, {"c": 2})
    mock.assert_called_once_with(agent, {"c": 2}, {"a": 1})


@pytest.mark.asyncio
async def test_dispatch_live_action_modify_order():
    from backend.api.algo.actions import _dispatch_live_action

    agent = _mk_agent()
    mock = AsyncMock()
    with patch("backend.api.algo.actions_live._action_live_modify_order", new=mock):
        await _dispatch_live_action(agent, "modify_order", {"a": 1}, {"c": 2})
    mock.assert_called_once_with(agent, {"c": 2}, {"a": 1})


@pytest.mark.asyncio
async def test_dispatch_live_action_cancel_order():
    from backend.api.algo.actions import _dispatch_live_action

    agent = _mk_agent()
    mock = AsyncMock()
    with patch("backend.api.algo.actions_live._action_live_cancel_order", new=mock):
        await _dispatch_live_action(agent, "cancel_order", {"a": 1}, {"c": 2})
    mock.assert_called_once_with(agent, {"c": 2}, {"a": 1})


@pytest.mark.asyncio
async def test_dispatch_live_action_cancel_all_orders():
    from backend.api.algo.actions import _dispatch_live_action

    agent = _mk_agent()
    mock = AsyncMock()
    with patch("backend.api.algo.actions_live._action_live_cancel_all_orders", new=mock):
        await _dispatch_live_action(agent, "cancel_all_orders", {"a": 1}, {"c": 2})
    mock.assert_called_once_with(agent, {"c": 2}, {"a": 1})


@pytest.mark.asyncio
async def test_dispatch_live_action_expiry_auto_close():
    from backend.api.algo.actions import _dispatch_live_action

    agent = _mk_agent()
    mock = AsyncMock()
    with patch("backend.api.algo.actions_live._action_live_expiry_auto_close", new=mock):
        await _dispatch_live_action(agent, "expiry_auto_close", {"a": 1}, {"c": 2})
    mock.assert_called_once_with(agent, {"c": 2}, {"a": 1})


@pytest.mark.asyncio
async def test_dispatch_live_action_chase_close_alias():
    """chase_close and chase_close_positions both route to the SAME handler."""
    from backend.api.algo.actions import _dispatch_live_action

    agent = _mk_agent()
    mock = AsyncMock()
    with patch("backend.api.algo.actions_live._action_live_chase_close_positions", new=mock):
        await _dispatch_live_action(agent, "chase_close", {"a": 1}, {"c": 2})
        await _dispatch_live_action(agent, "chase_close_positions", {"a": 1}, {"c": 2})
    assert mock.await_count == 2
    mock.assert_any_call(agent, {"c": 2}, {"a": 1})


@pytest.mark.asyncio
async def test_dispatch_live_action_unregistered_type_warns_no_handler_call():
    """Unregistered action_type: no handler invoked, warning logged, no raise."""
    from backend.api.algo.actions import _dispatch_live_action

    agent = _mk_agent(slug="warn-agent")
    with patch("backend.api.algo.actions.logger") as mock_logger:
        await _dispatch_live_action(agent, "totally_bogus_type", {}, {})
    mock_logger.warning.assert_called_once_with(
        "Agent [warn-agent]: live action 'totally_bogus_type' has no wired handler"
    )


# ── Noop dispatch — every registered action_type reaches its handler ──────

@pytest.mark.asyncio
async def test_al_run_noop_handler_monitor_order():
    from backend.api.algo.actions import _al_run_noop_handler

    agent = _mk_agent()
    mock = AsyncMock()
    with patch("backend.api.algo.actions.monitor_order", new=mock):
        ok = await _al_run_noop_handler(agent, "monitor_order", {"a": 1}, {"c": 2})
    assert ok is True
    mock.assert_called_once_with({"c": 2}, {"a": 1})


@pytest.mark.asyncio
async def test_al_run_noop_handler_deactivate_agent():
    from backend.api.algo.actions import _al_run_noop_handler

    agent = _mk_agent()
    mock = AsyncMock()
    with patch("backend.api.algo.actions.deactivate_agent", new=mock):
        ok = await _al_run_noop_handler(agent, "deactivate_agent", {"a": 1}, {"c": 2})
    assert ok is True
    mock.assert_called_once_with({"c": 2}, {"a": 1})


@pytest.mark.asyncio
async def test_al_run_noop_handler_set_flag():
    from backend.api.algo.actions import _al_run_noop_handler

    agent = _mk_agent()
    mock = AsyncMock()
    with patch("backend.api.algo.actions.set_flag", new=mock):
        ok = await _al_run_noop_handler(agent, "set_flag", {"a": 1}, {"c": 2})
    assert ok is True
    mock.assert_called_once_with({"c": 2}, {"a": 1})


@pytest.mark.asyncio
async def test_al_run_noop_handler_emit_log():
    from backend.api.algo.actions import _al_run_noop_handler

    agent = _mk_agent()
    mock = AsyncMock()
    with patch("backend.api.algo.actions.emit_log", new=mock):
        ok = await _al_run_noop_handler(agent, "emit_log", {"a": 1}, {"c": 2})
    assert ok is True
    mock.assert_called_once_with({"c": 2}, {"a": 1})


@pytest.mark.asyncio
async def test_al_run_noop_handler_send_summary():
    from backend.api.algo.actions import _al_run_noop_handler

    agent = _mk_agent()
    mock = AsyncMock()
    with patch("backend.api.algo.actions_live._action_send_summary", new=mock):
        ok = await _al_run_noop_handler(agent, "send_summary", {"a": 1}, {"c": 2})
    assert ok is True
    mock.assert_called_once_with({"c": 2}, {"a": 1})


@pytest.mark.asyncio
async def test_al_run_noop_handler_unregistered_type_warns_and_returns_false():
    """Unregistered action_type: no handler invoked, warning logged, returns False."""
    from backend.api.algo.actions import _al_run_noop_handler

    agent = _mk_agent(slug="warn-agent")
    with patch("backend.api.algo.actions.logger") as mock_logger:
        ok = await _al_run_noop_handler(agent, "totally_bogus_type", {}, {})
    assert ok is False
    mock_logger.warning.assert_called_once_with(
        "Agent [warn-agent]: unknown action type 'totally_bogus_type'"
    )


# ── Asymmetric exception-handling: send_summary NEVER wrapped, the other
#    four handlers always are ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_al_run_noop_handler_send_summary_exception_propagates():
    """send_summary raising must propagate (NOT be swallowed) — matches
    the original un-wrapped early-return behavior."""
    from backend.api.algo.actions import _al_run_noop_handler

    agent = _mk_agent()
    boom = RuntimeError("summary boom")
    with patch("backend.api.algo.actions_live._action_send_summary",
               new=AsyncMock(side_effect=boom)):
        with pytest.raises(RuntimeError, match="summary boom"):
            await _al_run_noop_handler(agent, "send_summary", {}, {})


@pytest.mark.asyncio
async def test_al_run_noop_handler_emit_log_exception_swallowed():
    """emit_log (a `_raising`-dict handler) raising must be swallowed and
    logged, returning False — matches the original wrapped try/except."""
    from backend.api.algo.actions import _al_run_noop_handler

    agent = _mk_agent()
    boom = RuntimeError("emit_log boom")
    with patch("backend.api.algo.actions.emit_log", new=AsyncMock(side_effect=boom)):
        ok = await _al_run_noop_handler(agent, "emit_log", {}, {})
    assert ok is False


@pytest.mark.asyncio
async def test_al_run_noop_handler_monitor_order_exception_swallowed():
    """monitor_order raising must be swallowed and logged, returning False."""
    from backend.api.algo.actions import _al_run_noop_handler

    agent = _mk_agent()
    boom = RuntimeError("monitor boom")
    with patch("backend.api.algo.actions.monitor_order", new=AsyncMock(side_effect=boom)):
        ok = await _al_run_noop_handler(agent, "monitor_order", {}, {})
    assert ok is False


# ── End-to-end through execute(): the asymmetry is visible to the caller ──

@pytest.mark.asyncio
async def test_execute_send_summary_failure_triggers_action_failed_audit():
    """Through execute(): a send_summary exception must bubble all the way
    out to the outer try/except → _al_action_failed_audit (NOT swallowed
    inside the noop handler, unlike emit_log/monitor_order/etc.)."""
    from backend.api.algo.actions import execute

    agent = _mk_agent()
    actions = [{"type": "send_summary", "params": {}}]
    context = {}

    with patch("backend.api.algo.actions._resolve_mode", return_value="noop"), \
         patch("backend.api.algo.actions_live._action_send_summary",
               new=AsyncMock(side_effect=RuntimeError("boom"))), \
         patch("backend.api.algo.actions._log_action_success", new=AsyncMock()) as mock_success, \
         patch("backend.api.algo.actions._al_action_failed_audit", new=AsyncMock()) as mock_audit:
        await execute(agent, actions, context)

    mock_audit.assert_called_once()
    mock_success.assert_not_called()


@pytest.mark.asyncio
async def test_execute_emit_log_failure_does_not_trigger_action_failed_audit():
    """Through execute(): an emit_log exception is swallowed inside the
    noop handler (returns False) — execute() `continue`s without calling
    _log_action_success OR _al_action_failed_audit."""
    from backend.api.algo.actions import execute

    agent = _mk_agent()
    actions = [{"type": "emit_log", "params": {"message": "x"}}]
    context = {}

    with patch("backend.api.algo.actions._resolve_mode", return_value="noop"), \
         patch("backend.api.algo.actions.emit_log",
               new=AsyncMock(side_effect=RuntimeError("boom"))), \
         patch("backend.api.algo.actions._log_action_success", new=AsyncMock()) as mock_success, \
         patch("backend.api.algo.actions._al_action_failed_audit", new=AsyncMock()) as mock_audit:
        await execute(agent, actions, context)

    mock_success.assert_not_called()
    mock_audit.assert_not_called()
