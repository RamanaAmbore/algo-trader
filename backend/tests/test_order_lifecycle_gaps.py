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
