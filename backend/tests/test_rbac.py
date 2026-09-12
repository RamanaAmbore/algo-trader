"""
Tests for api/rbac.py — role-based access control matrix.

SSOT: `has_cap()` is the single gate for all role-capability checks.
Correctness: admin role includes "run_simulator" and "view_hedge_proxies" caps.
No stale-code regression: admin specifically excludes trading caps like "place_order".
"""

from __future__ import annotations

from backend.api.rbac import has_cap, CAPS, normalise_role


class TestRBACAdminCapabilities:
    """Admin role must include simulator and hedge-proxy read caps (no trading)."""

    def test_admin_has_run_simulator(self):
        """Admin must be able to run the simulator (non-destructive analysis tool)."""
        assert has_cap("admin", "run_simulator") is True, (
            "Admin role must have 'run_simulator' capability"
        )

    def test_admin_has_view_hedge_proxies(self):
        """Admin must be able to view hedge proxies (read-only risk analysis)."""
        assert has_cap("admin", "view_hedge_proxies") is True, (
            "Admin role must have 'view_hedge_proxies' capability"
        )

    def test_admin_lacks_place_order(self):
        """Admin should not have trading rights — regression guard against
        capability matrix drift."""
        assert has_cap("admin", "place_order") is False, (
            "Admin role must NOT have 'place_order' (trading restricted to trader/designated)"
        )

    def test_admin_lacks_modify_order(self):
        """Admin should not have order modification rights."""
        assert has_cap("admin", "modify_order") is False, (
            "Admin role must NOT have 'modify_order' (trading restricted)"
        )

    def test_admin_lacks_cancel_order(self):
        """Admin should not have order cancellation rights."""
        assert has_cap("admin", "cancel_order") is False, (
            "Admin role must NOT have 'cancel_order' (trading restricted)"
        )


class TestRBACNormalisation:
    """Role strings are normalised to lowercase; unknown roles fall back to 'partner'."""

    def test_normalise_admin_lowercase(self):
        """'admin' normalises to 'admin'."""
        assert normalise_role("admin") == "admin"

    def test_normalise_admin_uppercase(self):
        """'ADMIN' normalises to 'admin'."""
        assert normalise_role("ADMIN") == "admin"

    def test_normalise_none_to_partner(self):
        """None normalises to 'partner' (safest default)."""
        assert normalise_role(None) == "partner"

    def test_normalise_empty_string_to_partner(self):
        """'' normalises to 'partner'."""
        assert normalise_role("") == "partner"

    def test_normalise_unknown_role_to_partner(self):
        """Unknown role 'superuser' normalises to 'partner'."""
        assert normalise_role("superuser") == "partner"


class TestRBACMatrixStructure:
    """CAPS dict is properly structured with all required roles."""

    def test_admin_in_simulator_cap(self):
        """'admin' must be in the 'run_simulator' frozenset."""
        assert "admin" in CAPS["run_simulator"], (
            "CAPS['run_simulator'] must include 'admin'"
        )

    def test_admin_in_hedge_proxies_cap(self):
        """'admin' must be in the 'view_hedge_proxies' frozenset."""
        assert "admin" in CAPS["view_hedge_proxies"], (
            "CAPS['view_hedge_proxies'] must include 'admin'"
        )

    def test_simulator_is_frozenset(self):
        """All caps values must be immutable frozensets."""
        assert isinstance(CAPS["run_simulator"], frozenset), (
            "CAPS['run_simulator'] must be a frozenset, not a set"
        )

    def test_hedge_proxies_is_frozenset(self):
        """All caps values must be immutable frozensets."""
        assert isinstance(CAPS["view_hedge_proxies"], frozenset), (
            "CAPS['view_hedge_proxies'] must be a frozenset, not a set"
        )


class TestRBACDesignatedAndTrader:
    """Regression: 'designated' and 'trader' roles retain their caps."""

    def test_designated_has_run_simulator(self):
        """Designated (firm owner) must have simulator access."""
        assert has_cap("designated", "run_simulator") is True

    def test_designated_has_view_hedge_proxies(self):
        """Designated must have hedge-proxy view access."""
        assert has_cap("designated", "view_hedge_proxies") is True

    def test_trader_has_run_simulator(self):
        """Trader must have simulator access."""
        assert has_cap("trader", "run_simulator") is True

    def test_trader_has_view_hedge_proxies(self):
        """Trader must have hedge-proxy view access."""
        assert has_cap("trader", "view_hedge_proxies") is True

    def test_trader_has_place_order(self):
        """Trader must have order placement (trading rights)."""
        assert has_cap("trader", "place_order") is True


class TestRBACUnknownCapability:
    """Unknown capabilities return False (fail-closed)."""

    def test_unknown_cap_returns_false(self):
        """Typo in capability name returns False, not an error."""
        assert has_cap("admin", "nonexistent_cap") is False, (
            "Unknown capability must return False (fail-closed), not raise"
        )

    def test_unknown_cap_for_any_role(self):
        """Unknown caps are false for all roles."""
        for role in ["admin", "trader", "designated", "partner", "risk"]:
            assert has_cap(role, "typo_capability") is False
