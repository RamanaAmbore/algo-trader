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

    def test_oco_pair_poll_default_is_5s(self):
        """Test 2: Verify OCO pair watcher uses 5 as default (not 15)."""
        import backend.api.background as bg

        src = inspect.getsource(bg)
        # The default must be changed from 15 to 5
        assert 'get_int("templates.oco_pair_poll_seconds", 5)' in src, (
            "OCO pair poll default must be 5 seconds, not 15"
        )

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
    """2026-10 audit fix — `_opp_send_cancel_alert` called
    `send_ntfy_alert(msg)` with a single positional argument. The real
    signature is `(title, message, priority=None)`, so `msg` bound to
    `title` and the required `message` parameter was missing —
    `TypeError` on every real call, swallowed silently inside the
    fire-and-forget `create_task` this is scheduled from (orders.py's
    postback fan-out), losing every postback CANCELLED alert.
    """

    @pytest.mark.asyncio
    async def test_real_call_no_longer_raises_typeerror(self, monkeypatch):
        import backend.api.routes.orders as orders_mod

        captured = {}

        def _fake_send(title, message, priority=None):
            captured["title"] = title
            captured["message"] = message

        monkeypatch.setattr(
            "backend.shared.helpers.alert_utils.send_ntfy_alert", _fake_send,
        )
        # No exception means the TypeError regression is fixed — a bare
        # call with the pre-fix single-argument signature would have
        # raised here.
        await orders_mod._opp_send_cancel_alert(
            "ZG0790", "NIFTY26OCTFUT", "SELL", 75, "Cancelled by user",
        )
        assert captured["title"] == "Order cancelled: NIFTY26OCTFUT"
        assert "ZG0790" not in captured["message"], "account must be masked"
        assert "ZG####" in captured["message"]
        assert "SELL 75 NIFTY26OCTFUT" in captured["message"]
        assert "Cancelled by user" in captured["message"]

    @pytest.mark.asyncio
    async def test_message_leads_with_the_shared_origin_header(self, monkeypatch):
        """This call site bypasses the event-agent pipeline entirely and
        carries no agent context — `format_notification_header(None, None)`
        degrades to "Agent: manual" as the message's first line."""
        import backend.api.routes.orders as orders_mod

        captured = {}
        monkeypatch.setattr(
            "backend.shared.helpers.alert_utils.send_ntfy_alert",
            lambda title, message, priority=None: captured.update(
                title=title, message=message,
            ),
        )
        await orders_mod._opp_send_cancel_alert(
            "ZG0790", "NIFTY26OCTFUT", "SELL", 75, "Cancelled by user",
        )
        assert captured["message"].splitlines()[0] == "Agent: manual"
