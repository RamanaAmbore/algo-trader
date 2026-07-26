"""
Tests for the GTT broker guard — validate_gtt_exchange and MCX fail-fast.

Covers five quality dimensions:
  SSOT        — validation happens once at the top of apply_plan_live, pre-attach MCX check in apply_template_to_order
  Correctness — ValueError raised for MCX/NCO on Dhan/Groww; Kite passes all exchanges
  Performance — early returns avoid wasting work (lot-size resolution, plan resolution)
  Reuse       — validation is called by both apply_plan_live (live path) and pre-attach guard (template layer)
  UX          — operator sees a clear error message when attach fails due to broker limitation

Scenario catalogue:
  1. Kite + MCX → no error; validation passes (validate_gtt_exchange does nothing)
  2. Dhan + MCX → ValueError raised; apply_plan_live catches and appends to errors
  3. Dhan + NCO → ValueError raised; apply_plan_live catches and appends to errors
  4. Dhan + NSE → no error; validation passes
  5. Groww + MCX → ValueError raised; apply_plan_live catches and appends to errors
  6. Groww + NCO → ValueError raised; apply_plan_live catches and appends to errors
  7. Groww + NSE → no error; validation passes
  8. Pre-attach MCX check in apply_template_to_order (Dhan/Groww) → AttachResult.guard_alert_fired=True + alert fired
  9. Pre-attach MCX check in apply_template_to_order (Kite) → proceeds past guard
  10. Off-hours GTT-only template has note in plan.notes
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from backend.brokers.adapters.kite import KiteBroker
from backend.brokers.adapters.dhan import DhanBroker
from backend.brokers.adapters.groww import GrowwBroker
from backend.api.algo.template_attach import (
    apply_plan_live,
    apply_template_to_order,
    TemplatePlan,
    GttSpec,
    AttachResult,
)


# ---------------------------------------------------------------------------
# Group 1: validate_gtt_exchange unit tests
# ---------------------------------------------------------------------------

class TestValidateGttExchange:
    """Test the validate_gtt_exchange method on each broker adapter."""

    def test_kite_mcx_passes(self):
        """Kite broker allows MCX GTT — no exception raised."""
        adapter = MagicMock(spec=KiteBroker)
        adapter.validate_gtt_exchange = KiteBroker.validate_gtt_exchange.__get__(adapter)
        # Should not raise
        adapter.validate_gtt_exchange("MCX")

    def test_kite_nco_passes(self):
        """Kite broker allows NCO GTT — no exception raised."""
        adapter = MagicMock(spec=KiteBroker)
        adapter.validate_gtt_exchange = KiteBroker.validate_gtt_exchange.__get__(adapter)
        # Should not raise
        adapter.validate_gtt_exchange("NCO")

    def test_kite_nse_passes(self):
        """Kite broker allows NSE GTT — no exception raised."""
        adapter = MagicMock(spec=KiteBroker)
        adapter.validate_gtt_exchange = KiteBroker.validate_gtt_exchange.__get__(adapter)
        # Should not raise
        adapter.validate_gtt_exchange("NSE")

    def test_dhan_mcx_raises(self):
        """Dhan broker rejects MCX GTT — ValueError raised."""
        adapter = DhanBroker.__new__(DhanBroker)
        with pytest.raises(ValueError, match="MCX"):
            adapter.validate_gtt_exchange("MCX")

    def test_dhan_nco_raises(self):
        """Dhan broker rejects NCO GTT — ValueError raised."""
        adapter = DhanBroker.__new__(DhanBroker)
        with pytest.raises(ValueError, match="NCO"):
            adapter.validate_gtt_exchange("NCO")

    def test_dhan_nse_passes(self):
        """Dhan broker allows NSE GTT — no exception raised."""
        adapter = MagicMock(spec=DhanBroker)
        adapter.validate_gtt_exchange = DhanBroker.validate_gtt_exchange.__get__(adapter)
        # Should not raise
        adapter.validate_gtt_exchange("NSE")

    def test_groww_mcx_raises(self):
        """Groww broker rejects MCX GTT — ValueError raised."""
        adapter = GrowwBroker.__new__(GrowwBroker)
        with pytest.raises(ValueError, match="MCX"):
            adapter.validate_gtt_exchange("MCX")

    def test_groww_nco_raises(self):
        """Groww broker rejects NCO GTT — ValueError raised."""
        adapter = GrowwBroker.__new__(GrowwBroker)
        with pytest.raises(ValueError, match="NCO"):
            adapter.validate_gtt_exchange("NCO")

    def test_groww_nse_passes(self):
        """Groww broker allows NSE GTT — no exception raised."""
        adapter = MagicMock(spec=GrowwBroker)
        adapter.validate_gtt_exchange = GrowwBroker.validate_gtt_exchange.__get__(adapter)
        # Should not raise
        adapter.validate_gtt_exchange("NSE")


# ---------------------------------------------------------------------------
# Group 2: apply_plan_live validation integration
# ---------------------------------------------------------------------------

class TestApplyPlanLiveValidation:
    """Test that apply_plan_live calls validate_gtt_exchange and handles ValueError."""

    def test_apply_plan_live_dhan_mcx_blocked(self):
        """MCX+Dhan → validate_gtt_exchange raises; errors appended; placement skipped."""
        plan = TemplatePlan(
            template_id=1,
            template_name="test",
            template_slug="test-tpl",
            parent_account="DH0001",
            parent_symbol="CRUDEOIL25AUGFUT",
            parent_side="BUY",
            parent_qty=1,
            parent_exchange="MCX",
            parent_fill_price=7500.0,
            parent_lot_size=100,
            gtts=[
                GttSpec(
                    trigger_type="single",
                    trigger_values=[7600.0],
                    orders=[{"order_type": "MARKET", "quantity": 1}],
                    label="TP",
                )
            ],
            wing=None,
        )

        broker = MagicMock()
        broker.validate_gtt_exchange.side_effect = ValueError(
            "Dhan does not support GTT on MCX"
        )

        result = apply_plan_live(plan, broker)

        assert result.errors, "Expected error to be appended"
        assert "MCX" in result.errors[0], f"Error should mention MCX: {result.errors[0]}"
        broker.place_gtt.assert_not_called()
        broker.place_order.assert_not_called()

    def test_apply_plan_live_groww_nco_blocked(self):
        """NCO+Groww → validate_gtt_exchange raises; errors appended; placement skipped."""
        plan = TemplatePlan(
            template_id=2,
            template_name="test",
            template_slug="test-tpl",
            parent_account="GR0001",
            parent_symbol="GOLDOCTFUT",
            parent_side="BUY",
            parent_qty=1,
            parent_exchange="NCO",
            parent_fill_price=50000.0,
            parent_lot_size=1,
            gtts=[
                GttSpec(
                    trigger_type="single",
                    trigger_values=[51000.0],
                    orders=[{"order_type": "MARKET", "quantity": 1}],
                    label="TP",
                )
            ],
            wing=None,
        )

        broker = MagicMock()
        broker.validate_gtt_exchange.side_effect = ValueError(
            "Groww Smart Order GTT is not supported on NCO"
        )

        result = apply_plan_live(plan, broker)

        assert result.errors, "Expected error to be appended"
        assert "NCO" in result.errors[0], f"Error should mention NCO: {result.errors[0]}"
        broker.place_gtt.assert_not_called()

    def test_apply_plan_live_kite_mcx_proceeds(self):
        """MCX+Kite → validate_gtt_exchange passes; proceeds to placement."""
        plan = TemplatePlan(
            template_id=3,
            template_name="test",
            template_slug="test-tpl",
            parent_account="ZG0790",
            parent_symbol="CRUDEOIL25AUGFUT",
            parent_side="BUY",
            parent_qty=1,
            parent_exchange="MCX",
            parent_fill_price=7500.0,
            parent_lot_size=100,
            gtts=[
                GttSpec(
                    trigger_type="single",
                    trigger_values=[7600.0],
                    orders=[{"order_type": "LIMIT", "quantity": 100, "price": 7600.0}],  # Must be multiple of lot_size
                    label="TP",
                )
            ],
            wing=None,
        )

        broker = MagicMock()
        broker.validate_gtt_exchange.return_value = None  # No error for Kite

        result = apply_plan_live(plan, broker)

        broker.validate_gtt_exchange.assert_called_once_with("MCX")
        # Kite proceeds past validation (placement would be attempted)
        assert not result.errors or len(result.errors) == 0, (
            f"Kite MCX should not error, but got: {result.errors}"
        )

    def test_apply_plan_live_dhan_nse_proceeds(self):
        """NSE+Dhan → validate_gtt_exchange passes; proceeds to placement."""
        plan = TemplatePlan(
            template_id=4,
            template_name="test",
            template_slug="test-tpl",
            parent_account="DH0001",
            parent_symbol="NIFTY25AUGFUT",
            parent_side="BUY",
            parent_qty=1,
            parent_exchange="NSE",
            parent_fill_price=24500.0,
            parent_lot_size=1,
            gtts=[
                GttSpec(
                    trigger_type="single",
                    trigger_values=[25000.0],
                    orders=[{"order_type": "LIMIT", "quantity": 1, "price": 25000.0}],
                    label="TP",
                )
            ],
            wing=None,
        )

        broker = MagicMock()
        broker.validate_gtt_exchange.return_value = None  # No error for NSE

        result = apply_plan_live(plan, broker)

        broker.validate_gtt_exchange.assert_called_once_with("NSE")
        assert not result.errors or len(result.errors) == 0, (
            f"Dhan NSE should not error, but got: {result.errors}"
        )


# ---------------------------------------------------------------------------
# Group 3: apply_template_to_order MCX pre-attach guard
# ---------------------------------------------------------------------------

class TestApplyTemplateToOrderPreAttachGuard:
    """Test the pre-attach MCX fail-fast in apply_template_to_order."""

    @pytest.mark.asyncio
    async def test_apply_template_to_order_dhan_mcx_fails_fast(self):
        """MCX+Dhan (gtt_supports_mcx=False) → early return with guard_alert_fired=True."""
        from backend.brokers.capabilities import BrokerCapabilities

        dhan_caps = BrokerCapabilities(
            broker_id="dhan",
            display_name="Dhan",
            gtt_single=True,
            gtt_oco=False,
            gtt_modify=False,
            gtt_cap_per_account=100,
            gtt_validity_days=365,
            gtt_supports_mcx=False,  # ← Key: MCX not supported
            bracket_order=True,
            cover_order=False,
            atomic_basket=True,
            order_tag=True,
            margin_preview=True,
            postback_gtt="partial",
            rate_limit_orders_sec=10,
        )

        with patch(
            "backend.brokers.capabilities.capabilities_for",
            return_value=dhan_caps,
        ), patch(
            "backend.api.algo.template_attach.load_template_for_slug_or_id",
            new_callable=AsyncMock,
            return_value={
                "id": 1,
                "name": "test",
                "slug": "test-tpl",
                "tp_pct": 2.0,
                "sl_pct": 1.0,
                "applies_to": "any",
            },
        ), patch(
            "backend.api.algo.template_attach._fire_attach_fail_alert"
        ) as mock_alert, patch(
            "backend.api.algo.template_attach.resolve_template_plan"
        ) as mock_resolve:

            result = await apply_template_to_order(
                template_id=1,
                template_slug=None,
                overrides={},
                parent_account="DH0001",
                parent_symbol="CRUDEOIL25AUGFUT",
                parent_side="BUY",
                parent_qty=1,
                parent_exchange="MCX",
                parent_fill_price=7500.0,
                apply_path="live",
            )

        assert result is not None, "Expected AttachResult to be returned"
        assert result.errors, "Expected errors in AttachResult"
        assert "MCX" in result.errors[0], f"Error should mention MCX: {result.errors[0]}"
        assert (
            result.guard_alert_fired is True
        ), "guard_alert_fired should be True for pre-attach failure"
        mock_alert.assert_called_once()
        mock_resolve.assert_not_called(), "resolve should not be called on pre-attach failure"

    @pytest.mark.asyncio
    async def test_apply_template_to_order_groww_mcx_fails_fast(self):
        """MCX+Groww (gtt_supports_mcx=False) → early return with guard_alert_fired=True."""
        from backend.brokers.capabilities import BrokerCapabilities

        groww_caps = BrokerCapabilities(
            broker_id="groww",
            display_name="Groww",
            gtt_single=True,
            gtt_oco=False,
            gtt_modify=False,
            gtt_cap_per_account=100,
            gtt_validity_days=365,
            gtt_supports_mcx=False,  # ← Key: MCX not supported
            bracket_order=False,
            cover_order=False,
            atomic_basket=False,
            order_tag=False,
            margin_preview=False,
            postback_gtt="poll_only",
            rate_limit_orders_sec=5,
        )

        with patch(
            "backend.brokers.capabilities.capabilities_for",
            return_value=groww_caps,
        ), patch(
            "backend.api.algo.template_attach.load_template_for_slug_or_id",
            new_callable=AsyncMock,
            return_value={
                "id": 2,
                "name": "test",
                "slug": "test-tpl",
                "tp_pct": 1.5,
                "sl_pct": 0.5,
                "applies_to": "any",
            },
        ), patch(
            "backend.api.algo.template_attach._fire_attach_fail_alert"
        ) as mock_alert, patch(
            "backend.api.algo.template_attach.resolve_template_plan"
        ) as mock_resolve:

            result = await apply_template_to_order(
                template_id=2,
                template_slug=None,
                overrides={},
                parent_account="GR0001",
                parent_symbol="CRUDEOIL25AUGFUT",
                parent_side="BUY",
                parent_qty=1,
                parent_exchange="MCX",
                parent_fill_price=7500.0,
                apply_path="live",
            )

        assert result is not None
        assert result.errors
        assert "MCX" in result.errors[0]
        assert result.guard_alert_fired is True
        mock_alert.assert_called_once()
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_template_to_order_kite_mcx_proceeds_past_guard(self):
        """MCX+Kite (gtt_supports_mcx=True) → passes pre-attach guard; proceeds to plan resolution."""
        from backend.brokers.capabilities import BrokerCapabilities

        kite_caps = BrokerCapabilities(
            broker_id="zerodha_kite",
            display_name="Zerodha Kite",
            gtt_single=True,
            gtt_oco=True,
            gtt_modify=True,
            gtt_cap_per_account=1000,
            gtt_validity_days=365,
            gtt_supports_mcx=True,  # ← Key: MCX IS supported
            bracket_order=False,
            cover_order=True,
            atomic_basket=False,
            order_tag=True,
            margin_preview=True,
            postback_gtt="reliable",
            rate_limit_orders_sec=10,
        )

        mock_plan = MagicMock(spec=TemplatePlan)
        mock_plan.gtts = []
        mock_plan.wing = None
        mock_plan.notes = []

        with patch(
            "backend.brokers.capabilities.capabilities_for",
            return_value=kite_caps,
        ), patch(
            "backend.api.algo.template_attach.load_template_for_slug_or_id",
            new_callable=AsyncMock,
            return_value={
                "id": 3,
                "name": "test",
                "slug": "test-tpl",
                "tp_pct": 2.0,
                "sl_pct": 1.0,
                "applies_to": "any",
            },
        ), patch(
            "backend.api.algo.template_attach._fire_attach_fail_alert"
        ) as mock_alert, patch(
            "backend.api.algo.template_attach._resolve_lot_size_for_order",
            new_callable=AsyncMock,
            return_value=(100, None),
        ), patch(
            "backend.api.algo.agent_engine._symbol_exchange_open",
            return_value=True,
        ), patch(
            "backend.api.algo.agent_engine._build_now_ctx",
            return_value={},
        ), patch(
            "backend.api.algo.template_attach.resolve_template_plan",
            return_value=mock_plan,
        ) as mock_resolve, patch(
            "backend.api.algo.template_attach._maybe_scan_wing_by_premium",
            new_callable=AsyncMock,
            return_value=({}, None),
        ), patch(
            "backend.api.algo.template_attach._route_apply_path",
            return_value=MagicMock(errors=[], guard_alert_fired=False),
        ):

            result = await apply_template_to_order(
                template_id=3,
                template_slug=None,
                overrides={},
                parent_account="ZG0790",
                parent_symbol="CRUDEOIL25AUGFUT",
                parent_side="BUY",
                parent_qty=1,
                parent_exchange="MCX",
                parent_fill_price=7500.0,
                apply_path="live",
            )

        # Pre-attach guard should NOT have fired
        mock_alert.assert_not_called()
        # resolve_template_plan SHOULD have been called (we got past the guard)
        mock_resolve.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_template_to_order_dhan_nse_proceeds(self):
        """NSE+Dhan (gtt_supports_mcx=False but NSE is allowed) → proceeds past guard."""
        from backend.brokers.capabilities import BrokerCapabilities

        dhan_caps = BrokerCapabilities(
            broker_id="dhan",
            display_name="Dhan",
            gtt_single=True,
            gtt_oco=False,
            gtt_modify=False,
            gtt_cap_per_account=100,
            gtt_validity_days=365,
            gtt_supports_mcx=False,  # MCX not supported, but NSE is
            bracket_order=True,
            cover_order=False,
            atomic_basket=True,
            order_tag=True,
            margin_preview=True,
            postback_gtt="partial",
            rate_limit_orders_sec=10,
        )

        mock_plan = MagicMock(spec=TemplatePlan)
        mock_plan.gtts = []
        mock_plan.wing = None
        mock_plan.notes = []

        with patch(
            "backend.brokers.capabilities.capabilities_for",
            return_value=dhan_caps,
        ), patch(
            "backend.api.algo.template_attach.load_template_for_slug_or_id",
            new_callable=AsyncMock,
            return_value={
                "id": 4,
                "name": "test",
                "slug": "test-tpl",
                "tp_pct": 2.0,
                "sl_pct": 1.0,
                "applies_to": "any",
            },
        ), patch(
            "backend.api.algo.template_attach._fire_attach_fail_alert"
        ) as mock_alert, patch(
            "backend.api.algo.template_attach._resolve_lot_size_for_order",
            new_callable=AsyncMock,
            return_value=(1, None),
        ), patch(
            "backend.api.algo.agent_engine._symbol_exchange_open",
            return_value=True,
        ), patch(
            "backend.api.algo.agent_engine._build_now_ctx",
            return_value={},
        ), patch(
            "backend.api.algo.template_attach.resolve_template_plan",
            return_value=mock_plan,
        ) as mock_resolve, patch(
            "backend.api.algo.template_attach._maybe_scan_wing_by_premium",
            new_callable=AsyncMock,
            return_value=({}, None),
        ), patch(
            "backend.api.algo.template_attach._route_apply_path",
            return_value=MagicMock(errors=[], guard_alert_fired=False),
        ):

            result = await apply_template_to_order(
                template_id=4,
                template_slug=None,
                overrides={},
                parent_account="DH0001",
                parent_symbol="NIFTY25AUGFUT",
                parent_side="BUY",
                parent_qty=1,
                parent_exchange="NSE",
                parent_fill_price=24500.0,
                apply_path="live",
            )

        # Pre-attach guard should NOT have fired (NSE is not MCX/NCO)
        mock_alert.assert_not_called()
        # resolve_template_plan SHOULD have been called
        mock_resolve.assert_called_once()


# ---------------------------------------------------------------------------
# Group 4: Off-hours GTT-only note
# ---------------------------------------------------------------------------

class TestOffHoursGttNote:
    """Test that off-hours GTT-only templates get a note in plan.notes."""

    @pytest.mark.asyncio
    async def test_apply_template_to_order_offhours_gtt_only_has_note(self):
        """GTT-only template (no wing) attached off-hours → note in plan.notes."""
        from backend.brokers.capabilities import BrokerCapabilities

        kite_caps = BrokerCapabilities(
            broker_id="zerodha_kite",
            display_name="Zerodha Kite",
            gtt_single=True,
            gtt_oco=True,
            gtt_modify=True,
            gtt_cap_per_account=1000,
            gtt_validity_days=365,
            gtt_supports_mcx=True,
            bracket_order=False,
            cover_order=True,
            atomic_basket=False,
            order_tag=True,
            margin_preview=True,
            postback_gtt="reliable",
            rate_limit_orders_sec=10,
        )

        mock_plan = MagicMock(spec=TemplatePlan)
        mock_plan.gtts = [
            GttSpec(
                trigger_type="single",
                trigger_values=[25000.0],
                orders=[{"order_type": "LIMIT", "quantity": 1, "price": 25000.0}],
                label="TP",
            )
        ]
        mock_plan.wing = None  # ← GTT-only, no wing
        mock_plan.notes = []  # Will be populated

        with patch(
            "backend.brokers.capabilities.capabilities_for",
            return_value=kite_caps,
        ), patch(
            "backend.api.algo.template_attach.load_template_for_slug_or_id",
            new_callable=AsyncMock,
            return_value={
                "id": 5,
                "name": "test",
                "slug": "test-tpl",
                "tp_pct": 2.0,
                "sl_pct": None,  # TP-only
                "applies_to": "any",
            },
        ), patch(
            "backend.api.algo.template_attach._resolve_lot_size_for_order",
            new_callable=AsyncMock,
            return_value=(1, None),
        ), patch(
            "backend.api.algo.agent_engine._symbol_exchange_open",
            return_value=False,  # ← Market is CLOSED
        ), patch(
            "backend.api.algo.agent_engine._build_now_ctx",
            return_value={},
        ), patch(
            "backend.api.algo.template_attach._template_has_wing",
            return_value=False,  # GTT-only
        ), patch(
            "backend.api.algo.template_attach.resolve_template_plan",
            return_value=mock_plan,
        ), patch(
            "backend.api.algo.template_attach._maybe_scan_wing_by_premium",
            new_callable=AsyncMock,
            return_value=({}, None),
        ), patch(
            "backend.api.algo.template_attach._route_apply_path",
            return_value=MagicMock(errors=[], guard_alert_fired=False),
        ):

            result = await apply_template_to_order(
                template_id=5,
                template_slug=None,
                overrides={},
                parent_account="ZG0790",
                parent_symbol="NIFTY25AUGFUT",
                parent_side="BUY",
                parent_qty=1,
                parent_exchange="NSE",
                parent_fill_price=24500.0,
                apply_path="live",
            )

        # Check that a note was added to the plan
        note_texts = " ".join(mock_plan.notes).lower()
        assert (
            "off-hours" in note_texts or "closed" in note_texts
        ), f"Expected off-hours/closed note in plan.notes, got: {mock_plan.notes}"
