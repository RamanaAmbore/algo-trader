"""Tests for template GTT entry key consistency.

Coverage:
  • T-B5: Both GTT writers use the same key name for trail-stop low-price field
"""

from unittest.mock import MagicMock
import pytest


class TestTemplateGttKeyConsistency:
    """T-B5: Both GTT writers use the same key name."""

    def test_opp_and_retry_use_same_lowest_ltp_key(self):
        """Verify both _opp_build_attach_entries and _retry_build_gtt_entry
        use the same key name 'lowest_ltp' for the trail-stop low-price field."""

        from backend.api.routes.orders_place import _opp_build_attach_entries
        from backend.api.routes.orders import _retry_build_gtt_entry

        # Create minimal mock objects for _opp_build_attach_entries
        mock_result = MagicMock()
        mock_result.sibling_pairs = []
        mock_spec = MagicMock()
        mock_spec.placed_id = "gtt_456"
        mock_spec.label = "SL"
        mock_spec.sl_trail_pct = 2.5
        mock_spec.trigger_values = [100.0, 98.0]
        mock_result.plan = MagicMock()
        mock_result.plan.gtts = [mock_spec]

        fill_price = 102.5
        parent_side = "BUY"

        # Call _opp_build_attach_entries
        attached_opp = _opp_build_attach_entries(mock_result, fill_price, parent_side)

        # Verify the key exists and has correct name
        assert len(attached_opp) > 0, "attached list should not be empty"
        entry_opp = attached_opp[0]
        assert "lowest_ltp" in entry_opp, (
            "opp entry must have 'lowest_ltp' key for trail-stop low price"
        )

        # Create minimal mock objects for _retry_build_gtt_entry
        mock_spec_retry = MagicMock()
        mock_spec_retry.label = "SL"
        mock_spec_retry.trigger_values = [100.0, 98.0]
        mock_spec_retry.trigger_type = "two-leg"
        mock_spec_retry.sl_trail_pct = 2.5

        mock_plan = MagicMock()
        mock_plan.parent_fill_price = 102.5
        mock_plan.parent_side = "BUY"
        mock_plan.parent_symbol = "NIFTY24MAY24000CE"
        mock_plan.parent_exchange = "NFO"
        mock_plan.parent_account = "ZG0790"
        mock_plan.parent_qty = 5

        # Call _retry_build_gtt_entry
        entry_retry = _retry_build_gtt_entry(
            mock_spec_retry,
            "gtt_456",
            mock_plan,
            "NRML"
        )

        # Verify the key exists and has correct name
        assert "lowest_ltp" in entry_retry, (
            "retry entry must have 'lowest_ltp' key for trail-stop low price"
        )

        # Verify both use the same key
        assert "lowest_ltp" in entry_opp and "lowest_ltp" in entry_retry, (
            "Both functions must use 'lowest_ltp' for trail-stop low price"
        )

        # Verify the values match expectations
        assert entry_opp["lowest_ltp"] == fill_price
        assert entry_retry["lowest_ltp"] == mock_plan.parent_fill_price

    def test_retry_entry_has_all_required_trail_keys(self):
        """Verify _retry_build_gtt_entry includes all trail-stop keys."""

        from backend.api.routes.orders import _retry_build_gtt_entry

        mock_spec = MagicMock()
        mock_spec.label = "SL"
        mock_spec.trigger_values = [100.0, 98.0]
        mock_spec.trigger_type = "two-leg"
        mock_spec.sl_trail_pct = 2.5

        mock_plan = MagicMock()
        mock_plan.parent_fill_price = 102.5
        mock_plan.parent_side = "BUY"
        mock_plan.parent_symbol = "NIFTY24MAY24000CE"
        mock_plan.parent_exchange = "NFO"
        mock_plan.parent_account = "ZG0790"
        mock_plan.parent_qty = 5

        entry = _retry_build_gtt_entry(mock_spec, "gtt_789", mock_plan, "NRML")

        # Verify trail-stop keys
        assert "sl_trail_pct" in entry, "must have sl_trail_pct"
        assert "current_trigger" in entry, "must have current_trigger"
        assert "highest_ltp" in entry, "must have highest_ltp"
        assert "lowest_ltp" in entry, "must have lowest_ltp"
        assert entry["sl_trail_pct"] == 2.5
        assert entry["current_trigger"] == 98.0  # last trigger value
        assert entry["highest_ltp"] == 102.5
        assert entry["lowest_ltp"] == 102.5

    def test_opp_entry_includes_trail_keys_when_sl_trail_pct_set(self):
        """Verify _opp_build_attach_entries includes trail-stop keys when
        sl_trail_pct is not None."""

        from backend.api.routes.orders_place import _opp_build_attach_entries

        mock_result = MagicMock()
        mock_result.sibling_pairs = []
        mock_spec = MagicMock()
        mock_spec.placed_id = "gtt_999"
        mock_spec.label = "SL"
        mock_spec.sl_trail_pct = 3.0
        mock_spec.trigger_values = [99.5, 97.5]
        mock_result.plan = MagicMock()
        mock_result.plan.gtts = [mock_spec]

        fill_price = 101.0
        parent_side = "SELL"

        attached = _opp_build_attach_entries(mock_result, fill_price, parent_side)

        entry = attached[0]
        # When sl_trail_pct is set and trigger_values exist, these keys must be present
        assert "sl_trail_pct" in entry, "must have sl_trail_pct"
        assert "trigger_values" in entry, "must have trigger_values"
        assert "highest_ltp" in entry, "must have highest_ltp"
        assert "lowest_ltp" in entry, "must have lowest_ltp"
        assert "parent_side" in entry, "must have parent_side"
        assert entry["parent_side"] == parent_side
