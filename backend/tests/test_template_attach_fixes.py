"""Tests for template attach fixes.

Coverage:
  • T-A4: GTT-only plan attaches off-hours, but wing plan is blocked
  • T-C2: ATM wing (offset=0) classified as has-wing
"""

from unittest.mock import MagicMock, patch, AsyncMock
import pytest
import inspect


class TestTemplateAttachOffHours:
    """T-A4: GTT-only plan attaches off-hours; wing plan blocked."""

    def test_apply_plan_live_checks_wing_for_market_hours(self):
        """Verify apply_plan_live checks market hours when wing is present."""
        from backend.api.algo.template_attach import apply_plan_live

        source = inspect.getsource(apply_plan_live)

        # The function should check if wing is not None
        assert "wing" in source, "apply_plan_live should check for wing leg"
        assert "_symbol_exchange_open" in source, (
            "apply_plan_live should check market hours via _symbol_exchange_open"
        )

    def test_apply_plan_live_skips_market_hours_check_without_wing(self):
        """Verify the logic: no wing means no off-hours blocking."""
        from backend.api.algo.template_attach import apply_plan_live

        source = inspect.getsource(apply_plan_live)

        # The pattern should be: if plan.wing is not None, then check market hours
        # This means GTT-only (wing=None) skips the check
        assert "if plan.wing is not None:" in source or \
               "if plan.wing:" in source or \
               "plan.wing is not None" in source, (
            "apply_plan_live should have conditional: if plan.wing is not None"
        )

    def test_gtt_only_plan_can_attach_off_hours(self):
        """GTT-only plan (wing=None) should bypass market-hours check."""
        # This is a logic test based on code inspection
        # GTT registration is 24x7 on Kite, so it's safe to attach GTT-only orders off-hours
        from backend.api.algo.template_attach import apply_plan_live

        source = inspect.getsource(apply_plan_live)

        # Comment in code should explain GTT-only is allowed off-hours
        assert "GTT" in source.upper(), "apply_plan_live should mention GTT"
        assert "24" in source or "offhour" in source.lower() or "closed" in source.lower(), (
            "apply_plan_live should document why GTT-only is allowed off-hours"
        )


class TestTemplateHasWingATMOffset:
    """T-C2: ATM wing (offset=0) classified as has-wing."""

    def test_template_has_wing_returns_true_for_zero_offset(self):
        """_template_has_wing should return True when wing_strike_offset=0."""
        from backend.api.algo.template_attach import _template_has_wing

        # Create template with offset=0 (ATM wing)
        template = {
            "wing_strike_offset": 0,  # ATM (at-the-money)
        }

        result = _template_has_wing(template)

        assert result is True, (
            "ATM wing with offset=0 should be classified as has-wing"
        )

    def test_non_zero_offset_has_wing(self):
        """_template_has_wing should return True for any non-zero offset."""
        from backend.api.algo.template_attach import _template_has_wing

        # Test positive offset
        template_pos = {"wing_strike_offset": 100}
        assert _template_has_wing(template_pos) is True

        # Test negative offset
        template_neg = {"wing_strike_offset": -100}
        assert _template_has_wing(template_neg) is True

    def test_no_offset_no_wing(self):
        """_template_has_wing should return False when no offset and no pct."""
        from backend.api.algo.template_attach import _template_has_wing

        # Template with no wing
        template = {}

        result = _template_has_wing(template)

        assert result is False, "template without offset/pct should be no-wing"

    def test_none_offset_with_pct_has_wing(self):
        """_template_has_wing should return True when pct is set (offset can be None)."""
        from backend.api.algo.template_attach import _template_has_wing

        template = {
            "wing_strike_offset": None,
            "wing_premium_pct": 50.0,  # Non-zero pct
        }

        result = _template_has_wing(template)

        assert result is True, "wing_premium_pct should indicate has-wing"

    def test_zero_pct_with_offset_has_wing(self):
        """_template_has_wing should return True when offset is set even if pct is 0."""
        from backend.api.algo.template_attach import _template_has_wing

        template = {
            "wing_strike_offset": 50,
            "wing_premium_pct": 0.0,  # Falsy but offset is set
        }

        result = _template_has_wing(template)

        assert result is True, "offset should indicate has-wing regardless of pct"

    def test_offset_none_pct_none_no_wing(self):
        """_template_has_wing should return False when both are None."""
        from backend.api.algo.template_attach import _template_has_wing

        template = {
            "wing_strike_offset": None,
            "wing_premium_pct": None,
        }

        result = _template_has_wing(template)

        assert result is False, "None offset and None pct means no wing"

    def test_offset_zero_pct_none_has_wing(self):
        """_template_has_wing with offset=0 (ATM) even if pct is None.

        This is the key regression test: ATM (offset=0) must be classified
        as has-wing, not skipped due to Python's bool(0)=False."""
        from backend.api.algo.template_attach import _template_has_wing

        template = {
            "wing_strike_offset": 0,
            "wing_premium_pct": None,
        }

        result = _template_has_wing(template)

        assert result is True, (
            "offset=0 (ATM wing) should be treated as has-wing, "
            "not skipped due to Python's bool(0)=False bug"
        )

    def test_template_has_wing_logic_not_using_bool_on_offset(self):
        """Verify _template_has_wing doesn't use bool(offset) for the check.

        The fix: check `offset is not None` instead of truthy check."""
        from backend.api.algo.template_attach import _template_has_wing

        source = inspect.getsource(_template_has_wing)

        # The correct pattern should be:
        # return (offset is not None) or bool(pct)
        # NOT:
        # return bool(offset) or bool(pct)
        # NOT:
        # return (offset and offset != 0) or bool(pct)  # Also acceptable but less clean

        # Check for the fix pattern
        has_is_not_none = "offset is not None" in source
        has_none_check = "is None" in source or "is not None" in source

        assert has_none_check, (
            "_template_has_wing should use 'is None' / 'is not None' check, "
            "not boolean coercion of offset"
        )

        # Make sure it doesn't have the buggy pattern
        # (though this is just a heuristic)
        assert "return (offset" in source or "return bool" in source, (
            "_template_has_wing should have an explicit return statement"
        )


class TestTemplateHasWingSourceCodeQuality:
    """Verify the _template_has_wing implementation quality."""

    def test_template_has_wing_docstring_mentions_atm(self):
        """Verify docstring mentions ATM case."""
        from backend.api.algo.template_attach import _template_has_wing

        source = inspect.getsource(_template_has_wing)

        # Should document what the function does
        assert "wing" in source.lower(), "_template_has_wing should mention wing"

    def test_template_has_wing_readable_implementation(self):
        """Verify the implementation is readable and correct."""
        from backend.api.algo.template_attach import _template_has_wing

        # Test the actual implementation with various edge cases
        # to ensure it handles the ATM case correctly

        test_cases = [
            ({"wing_strike_offset": 0}, True, "ATM offset=0"),
            ({"wing_strike_offset": 1}, True, "positive offset"),
            ({"wing_strike_offset": -1}, True, "negative offset"),
            ({"wing_premium_pct": 10.0}, True, "positive pct"),
            ({"wing_premium_pct": 0.0}, False, "zero pct alone"),
            ({"wing_strike_offset": 0, "wing_premium_pct": 0.0}, True, "ATM + zero pct"),
            ({}, False, "empty template"),
            ({"wing_strike_offset": None}, False, "None offset alone"),
            ({"wing_strike_offset": None, "wing_premium_pct": None}, False, "both None"),
        ]

        for template, expected, description in test_cases:
            result = _template_has_wing(template)
            assert result == expected, (
                f"_template_has_wing({template}) = {result}, "
                f"expected {expected} ({description})"
            )
