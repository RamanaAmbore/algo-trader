"""
Tests for ORDER_LIFECYCLE gap fixes.

These tests verify structural properties of the implementation:
  1. agent_trigger is in VALID_KINDS
  2. OCO pair poll default is 5 seconds
  3. _task_open_order_watchdog is registered
  4. _opp_send_cancel_alert helper exists
  5. "placed" event is written in _opp_live_handle_success

No mocking required — pure structural verification via inspection.
"""

import inspect
import pytest


class TestOrderLifecycleGapFixes:
    """Structural verification suite for ORDER_LIFECYCLE fixes."""

    def test_agent_trigger_in_valid_kinds(self):
        """Test 1: Verify 'agent_trigger' is in VALID_KINDS."""
        from backend.api.algo.order_events import VALID_KINDS
        assert "agent_trigger" in VALID_KINDS, (
            "agent_trigger must be in VALID_KINDS for order lifecycle gap fix"
        )

    # `test_oco_pair_poll_default_is_5s` (Test 2: OCO pair watcher's
    # own poll-interval default-value bugfix) removed — Bracket/
    # Template-GTT removal (T3.2, 2026-11) deleted `_task_oco_pair_
    # watcher` entirely, along with every reader of
    # `templates.oco_pair_poll_seconds`. There is no remaining
    # default-value behaviour to verify.

    def test_open_order_watchdog_registered(self):
        """Test 3: Verify _task_open_order_watchdog exists and is registered."""
        import backend.api.background as bg

        # Check function exists
        assert hasattr(bg, "_task_open_order_watchdog"), (
            "_task_open_order_watchdog must be defined in background.py"
        )

        # Check it's registered in on_startup
        src = inspect.getsource(bg)
        assert "bg-open-order-watchdog" in src, (
            "watchdog must be registered with name 'bg-open-order-watchdog' in on_startup"
        )

    def test_cancel_alert_helper_exists(self):
        """Test 4: Verify _opp_send_cancel_alert helper exists in orders.py."""
        import backend.api.routes.orders as orders_mod

        assert hasattr(orders_mod, "_opp_send_cancel_alert"), (
            "_opp_send_cancel_alert must be defined in orders.py"
        )

    def test_live_placed_event_in_orders_place(self):
        """Test 5: Verify 'placed' event is written in _opp_live_handle_success."""
        from backend.api.routes import orders_place as op_mod

        src = inspect.getsource(op_mod._opp_live_handle_success)
        # Check that write_event is called with "placed" kind
        assert '"placed"' in src or "'placed'" in src, (
            '_opp_live_handle_success must call write_event with "placed" kind'
        )


class TestCancelAlertSendsWithoutTypeError:
    """2026-10 audit fix (historical) — `_opp_send_cancel_alert` called
    `send_ntfy_alert(msg)` with a single positional argument, raising
    `TypeError` on every real call, swallowed silently inside the
    fire-and-forget `create_task` this is scheduled from (orders.py's
    postback fan-out), losing every postback CANCELLED alert.

    2026-11-03 (Wave 3 of the alert-agent audit) — the direct
    `send_ntfy_alert` call this regression was about is gone entirely;
    `_opp_send_cancel_alert` now writes a log record instead. These
    tests now assert the record's content — the masked body text, and
    (moved to render time, see `event_agents.py:_render_order_
    cancelled`) the "Agent: manual" origin line for a call with no
    resolvable agent identity.
    """

    @pytest.mark.asyncio
    async def test_real_call_logs_masked_body_without_raising(self):
        import backend.api.routes.orders as orders_mod
        from unittest.mock import patch

        # No `order_id` passed -> both `_opp_cancel_is_own_chase_requote`
        # and `_opp_cancel_alert_identity` short-circuit on the falsy
        # input before ever touching the DB.
        with patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
            # No exception means the TypeError regression is fixed — a
            # bare call with the pre-fix single-argument signature
            # would have raised here.
            await orders_mod._opp_send_cancel_alert(
                "ZG0790", "NIFTY26OCTFUT", "SELL", 75, "Cancelled by user",
            )

        mock_logger.warning.assert_called_once()
        extra = mock_logger.warning.call_args.kwargs["extra"]
        assert extra["title"] == "Order cancelled: NIFTY26OCTFUT"
        assert "ZG0790" not in extra["body"], "account must be masked"
        assert "ZG####" in extra["body"]
        assert "SELL 75 NIFTY26OCTFUT" in extra["body"]
        assert "Cancelled by user" in extra["body"]

    def test_render_leads_with_the_shared_origin_header_when_no_agent_resolved(self):
        """No `order_id` / no resolvable AlgoOrder row -> `agent_id` is
        None -> manual trigger. Built at RENDER time (`_render_order_
        cancelled`), not by the producer, which no longer constructs
        any header text itself.

        2026-11-06 rebuild (operator SSOT instruction) — this renderer
        now always carries a process layer ("postback" — the producer,
        `orders.py:_opp_send_cancel_alert`, sets `source_type`/
        `source_name`; this fixture omits them deliberately to exercise
        the renderer's own literal fallback default), so the header
        reads "Source: postback triggered manually" instead of the old
        "Agent: manual".

        2026-11 channel-authoring retirement — the renderer no longer
        returns a dynamic `out[4]` channel-override list (channel
        selection now comes from `alert_routing.py`'s per-slug override
        for `order-cancelled-alert`); it is a plain 3-tuple, same shape
        as `_render_market_status`."""
        from backend.api.algo.event_agents import _render_order_cancelled

        rec = {"extra": {
            "title": "Order cancelled: NIFTY26OCTFUT",
            "body": "ZG#### SELL 75 NIFTY26OCTFUT — Cancelled by user",
            "agent_id": None, "agent_name": None,
        }}
        title, body, tg = _render_order_cancelled(rec)
        assert body.splitlines()[0] == "Source: postback triggered manually"


class TestRenderMarketStatusTriggerKnownFix:
    """2026-11-08 fix — `_render_market_status` always sets
    `source_type`/`source_name` (its one real producer,
    `actions_live._action_live_send_market_status`, carries it on every
    call), so `has_process_identity` was always True and the old
    `trigger_known=not has_process_identity` was always False. That
    silently dropped a real `agent_id`/`agent_name` the producer DID
    attach — exactly the gap the sibling agent found once their own fix
    (threading a real agent identity into the record for an
    agent-invoked `send_market_status()` call) went inert on arrival
    here. Fixed to `trigger_known=_order_scope_trigger_known(x)` — the
    same key-presence check every other order/process-scoped renderer
    in this file already uses."""

    def test_scheduled_cron_call_shows_no_trigger(self):
        """No `agent_id`/`agent_name` keys at all (the real 05:35 IST
        cron shape) — must still show no trigger, never a fabricated
        one."""
        from backend.api.algo.event_agents import _render_market_status

        rec = {"extra": {
            "title": "Market Status",
            "body": "NSE/Equity: OPEN 09:15–15:30 IST\nMCX/Commodity: OPEN 09:00–23:30 IST",
            "source_type": "process", "source_name": "Market status report",
        }}
        title, body, tg = _render_market_status(rec)
        assert body.splitlines()[0] == "Source: Market status report"

    def test_agent_invoked_call_shows_the_real_agent(self):
        """An agent-invoked `send_market_status()` call carries a real
        `agent_id`/`agent_name` pair alongside the process identity —
        this must now surface as "triggered by {agent}", not be
        silently dropped."""
        from backend.api.algo.event_agents import _render_market_status

        rec = {"extra": {
            "title": "Market Status",
            "body": "NSE/Equity: OPEN 09:15–15:30 IST\nMCX/Commodity: OPEN 09:00–23:30 IST",
            "source_type": "process", "source_name": "Market status report",
            "agent_id": 42, "agent_name": "custom-agent",
        }}
        title, body, tg = _render_market_status(rec)
        assert body.splitlines()[0] == "Source: Market status report triggered by custom-agent"

    def test_agent_invoked_manual_sentinel_shows_triggered_manually(self):
        """A caller that explicitly resolved "manual" (agent_id/agent_name
        keys present, values None) still shows "triggered manually",
        not a dropped/absent trigger."""
        from backend.api.algo.event_agents import _render_market_status

        rec = {"extra": {
            "title": "Market Status",
            "body": "NSE/Equity: OPEN 09:15–15:30 IST",
            "source_type": "process", "source_name": "Market status report",
            "agent_id": None, "agent_name": None,
        }}
        title, body, tg = _render_market_status(rec)
        assert body.splitlines()[0] == "Source: Market status report triggered manually"


class TestRenderDeploySyncTriggerKnownFix:
    """2026-11-08 audit fix (item 4) — `_render_deploy_sync` had the
    exact same hardcoded `trigger_known=not has_process_identity` bug
    `_render_market_status` was already fixed for above. Every real
    producer of this renderer (gtt_asymmetric, oco_double_fire,
    ticker_recovered, ticker_degraded, visitor_report, deploy_out_of_sync,
    user_registered, the unrouted catch-all, app_message) always sets
    `source_type`/`source_name` and never a real `agent_id`/`agent_name`,
    so this bug was latent — but a hardcoded `False` would silently drop
    a real trigger for any future agent-invoked `generic_notice`-shaped
    call. Fixed to the same `_order_scope_trigger_known(x)` key-presence
    check."""

    def test_scheduled_process_call_shows_no_trigger(self):
        """No `agent_id`/`agent_name` keys at all (every real producer's
        actual shape today) — must still show no trigger."""
        from backend.api.algo.event_agents import _render_deploy_sync

        rec = {"extra": {
            "title": "Deploy out of sync",
            "body": "Local HEAD != origin HEAD",
            "source_type": "process", "source_name": "Deploy sync",
        }}
        title, body, tg = _render_deploy_sync(rec)
        assert body.splitlines()[0] == "Source: Deploy sync"

    def test_agent_invoked_call_shows_the_real_agent(self):
        """A hypothetical future agent-invoked generic_notice call that
        carries a real `agent_id`/`agent_name` pair alongside the process
        identity must surface "triggered by {agent}", not be silently
        dropped the way the pre-fix hardcoded `trigger_known=False`
        would have."""
        from backend.api.algo.event_agents import _render_deploy_sync

        rec = {"extra": {
            "title": "Deploy out of sync",
            "body": "Local HEAD != origin HEAD",
            "source_type": "process", "source_name": "Deploy sync",
            "agent_id": 42, "agent_name": "custom-agent",
        }}
        title, body, tg = _render_deploy_sync(rec)
        assert body.splitlines()[0] == "Source: Deploy sync triggered by custom-agent"

    def test_agent_invoked_manual_sentinel_shows_triggered_manually(self):
        from backend.api.algo.event_agents import _render_deploy_sync

        rec = {"extra": {
            "title": "Deploy out of sync",
            "body": "Local HEAD != origin HEAD",
            "source_type": "process", "source_name": "Deploy sync",
            "agent_id": None, "agent_name": None,
        }}
        title, body, tg = _render_deploy_sync(rec)
        assert body.splitlines()[0] == "Source: Deploy sync triggered manually"
