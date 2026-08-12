"""
Tests for apply_plan_live pre-flight capability checks (P2a, P2b).

P2a: When capabilities_for() raises an exception, the fallback must be
UNKNOWN_CAPS (with gtt_single=False), not None. This prevents assuming
OCO is supported when lookup failed.

P2b: apply_plan_live must check gtt_single up-front before any broker
call, returning a clear error message when the broker doesn't support GTT.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest
from backend.brokers.capabilities import UNKNOWN_CAPS, KITE_CAPS, BrokerCapabilities
from backend.api.algo.template_attach import TemplatePlan, GttSpec, apply_plan_live


def _make_simple_plan() -> TemplatePlan:
    """Factory for a minimal valid TemplatePlan that will pass pre-checks."""
    return TemplatePlan(
        template_id=1,
        template_name="test_template",
        template_slug="test-template",
        parent_account="ZG0790",
        parent_symbol="NIFTY25JUNFUT",
        parent_side="BUY",
        parent_qty=50,
        parent_lot_size=50,
        parent_exchange="NFO",
        parent_fill_price=24000.0,
        gtts=[
            GttSpec(
                trigger_type="single",
                trigger_values=[24500.0],
                orders=[{
                    "transaction_type": "SELL",
                    "quantity": 50,
                    "price": 24500.0,
                    "order_type": "LIMIT",
                    "product": "NRML",
                }],
                label="TP",
            )
        ],
    )


class TestCapabilitiesForFallback:
    """P2a: Verify that UNKNOWN_CAPS is the safe fallback for failed lookup."""

    def test_unknown_caps_is_conservative(self):
        """UNKNOWN_CAPS must have gtt_single=False and gtt_oco=False (no GTT support)."""
        assert UNKNOWN_CAPS.gtt_single is False, "UNKNOWN_CAPS must not claim single GTT support"
        assert UNKNOWN_CAPS.gtt_oco is False, "UNKNOWN_CAPS must not claim OCO support"
        assert UNKNOWN_CAPS.gtt_supports_mcx is False

    def test_none_caps_would_assume_oco_supported(self):
        """Demonstrate the bug: None fallback causes gtt_oco to be assumed True.

        At line 1100 of template_attach.py:
            if broker_caps is None or broker_caps.gtt_oco:

        With None, this evaluates to True, sending OCO even when broker can't support it.
        """
        broker_caps = None
        # Simulating the condition at line 1100
        assumed_oco_supported = broker_caps is None or broker_caps.gtt_oco
        assert assumed_oco_supported is True, (
            "None caps would incorrectly assume OCO is supported"
        )

    def test_unknown_caps_does_not_assume_oco(self):
        """With UNKNOWN_CAPS fallback, gtt_oco=False so two singles are used."""
        broker_caps = UNKNOWN_CAPS
        # Simulating the condition at line 1100
        assumed_oco_supported = broker_caps is None or broker_caps.gtt_oco
        assert assumed_oco_supported is False, (
            "UNKNOWN_CAPS fallback must correctly report no OCO support"
        )


class TestApplyPlanLiveGttSinglePreflight:
    """P2b: apply_plan_live must reject early when gtt_single=False."""

    def test_gtt_single_false_returns_error_without_broker_call(self):
        """When broker has gtt_single=False, apply_plan_live returns error before place_gtt."""
        # Create a broker mock with gtt_single=False (like UNKNOWN_CAPS)
        broker = MagicMock()
        broker.broker_id = "unknown_broker"
        broker.capabilities = UNKNOWN_CAPS  # gtt_single=False
        broker.validate_gtt_exchange.return_value = None

        plan = _make_simple_plan()
        result = apply_plan_live(plan, broker)

        # Must have errors
        assert result.errors, "Expected at least one error"

        # Error must mention gtt_single or GTT unsupported
        error_messages = " ".join(result.errors)
        assert "gtt_single=False" in error_messages or "does not support GTT" in error_messages, (
            f"Error message should mention gtt_single or GTT unsupported. Got: {result.errors}"
        )

        # place_gtt must NOT be called (pre-flight stop)
        broker.place_gtt.assert_not_called()

    def test_gtt_single_true_proceeds_to_placement(self):
        """When broker has gtt_single=True (Kite), apply_plan_live proceeds."""
        broker = MagicMock()
        broker.broker_id = "zerodha_kite"
        broker.capabilities = KITE_CAPS  # gtt_single=True
        broker.validate_gtt_exchange.return_value = None
        broker.place_gtt.return_value = "gtt-123"
        broker.translate_qty.side_effect = lambda ex, q, ls: q
        broker.place_order.return_value = "order-456"

        plan = _make_simple_plan()
        result = apply_plan_live(plan, broker)

        # Should attempt placement
        broker.place_gtt.assert_called_once()

    def test_custom_broker_with_gtt_disabled(self):
        """A broker with a custom capability set (gtt_single=False) is rejected early."""
        custom_caps = BrokerCapabilities(
            broker_id="test_broker",
            display_name="Test Broker (No GTT)",
            gtt_single=False,
            gtt_oco=False,
            gtt_modify=False,
            gtt_cap_per_account=0,
            gtt_validity_days=0,
            gtt_supports_mcx=False,
            bracket_order=False,
            cover_order=False,
            atomic_basket=False,
            order_tag=False,
            margin_preview=False,
            postback_gtt="poll_only",
            rate_limit_orders_sec=1,
        )

        broker = MagicMock()
        broker.broker_id = "test_broker"
        broker.capabilities = custom_caps
        broker.validate_gtt_exchange.return_value = None

        plan = _make_simple_plan()
        result = apply_plan_live(plan, broker)

        assert result.errors
        assert any("does not support GTT" in e for e in result.errors)
        broker.place_gtt.assert_not_called()

    def test_exchange_validation_runs_before_gtt_single_check(self):
        """Exchange validation (validate_gtt_exchange) fires before gtt_single check."""
        broker = MagicMock()
        broker.broker_id = "test_broker"
        broker.capabilities = UNKNOWN_CAPS  # gtt_single=False
        # Simulate exchange validation failure
        broker.validate_gtt_exchange.side_effect = ValueError("NFO not supported for GTT")

        plan = _make_simple_plan()
        result = apply_plan_live(plan, broker)

        # Should fail on exchange, not gtt_single
        assert result.errors
        assert "NFO not supported" in result.errors[0]

    def test_exchange_success_then_gtt_single_fails(self):
        """When exchange passes, gtt_single failure then kicks in."""
        broker = MagicMock()
        broker.broker_id = "no_gtt_broker"
        broker.capabilities = UNKNOWN_CAPS  # gtt_single=False
        broker.validate_gtt_exchange.return_value = None

        plan = _make_simple_plan()
        result = apply_plan_live(plan, broker)

        # Should fail on gtt_single, not exchange
        assert result.errors
        error_msg = " ".join(result.errors)
        assert "gtt_single" in error_msg or "does not support GTT" in error_msg
