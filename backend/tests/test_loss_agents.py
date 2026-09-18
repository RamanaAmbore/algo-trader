"""Tests for BUILTIN_AGENTS loss agent definitions.

Verifies the Python-level structure of loss agents in agent_engine.py
before any database interaction. Tests cover:
  - Required loss agents exist (loss-positions-acct, loss-rate-acct, etc.)
  - Condition tree structure is correct
  - Tier/cooldown assignments match design
  - _LOSS_AGENT_NTFY dict has all loss agents mapped
  - Separation of rate conditions (critical) vs static conditions (high)
"""

import pytest
from backend.api.algo.agent_engine import BUILTIN_AGENTS, _LOSS_AGENT_NTFY


def _agent(slug):
    """Helper to look up a built-in agent by slug."""
    return next((a for a in BUILTIN_AGENTS if a["slug"] == slug), None)


class TestLossRateAcct:
    """Tests for loss-rate-acct (new burn-rate-only agent)."""

    def test_exists(self):
        """loss-rate-acct must be in BUILTIN_AGENTS."""
        a = _agent("loss-rate-acct")
        assert a is not None, "loss-rate-acct not found in BUILTIN_AGENTS"

    def test_tier_is_critical(self):
        """loss-rate-acct is critical-tier (short 10-min cooldown)."""
        a = _agent("loss-rate-acct")
        assert a["tier"] == "critical", f"Expected tier='critical', got {a['tier']}"

    def test_cooldown_is_10_minutes(self):
        """loss-rate-acct has 10-minute cooldown (vs 30 for static losses)."""
        a = _agent("loss-rate-acct")
        assert a["cooldown_minutes"] == 10, f"Expected 10, got {a['cooldown_minutes']}"

    def test_contains_only_rate_conditions(self):
        """loss-rate-acct must contain ONLY rate metrics (pnl_rate_abs/pct)."""
        a = _agent("loss-rate-acct")
        conds = a["conditions"]["all"]
        metrics = {c["metric"] for c in conds}
        expected = {"pnl_rate_abs", "pnl_rate_pct"}
        assert metrics == expected, (
            f"loss-rate-acct should have only rate metrics {expected}, "
            f"got {metrics}"
        )

    def test_no_static_conditions(self):
        """loss-rate-acct must NOT have static pnl or pnl_pct thresholds."""
        a = _agent("loss-rate-acct")
        conds = a["conditions"]["all"]
        static_metrics = {c["metric"] for c in conds if c["metric"] in {"pnl", "pnl_pct"}}
        assert static_metrics == set(), (
            f"loss-rate-acct should not have static conditions, "
            f"found {static_metrics}"
        )

    def test_abs_threshold_is_minus_10000(self):
        """loss-rate-acct absolute threshold must be -10000."""
        a = _agent("loss-rate-acct")
        conds = a["conditions"]["all"]
        abs_cond = next(c for c in conds if c["metric"] == "pnl_rate_abs")
        assert abs_cond["value"] == -10000, (
            f"Expected pnl_rate_abs threshold -10000, got {abs_cond['value']}"
        )

    def test_in_ntfy_dict_as_urgent(self):
        """loss-rate-acct must map to 'urgent' in _LOSS_AGENT_NTFY."""
        assert "loss-rate-acct" in _LOSS_AGENT_NTFY, (
            "loss-rate-acct missing from _LOSS_AGENT_NTFY"
        )
        assert _LOSS_AGENT_NTFY["loss-rate-acct"] == "urgent", (
            f"loss-rate-acct should be 'urgent', "
            f"got {_LOSS_AGENT_NTFY['loss-rate-acct']}"
        )

    def test_scope_is_any_acct(self):
        """loss-rate-acct applies to per-account scope (positions.any_acct)."""
        a = _agent("loss-rate-acct")
        conds = a["conditions"]["all"]
        scopes = {c["scope"] for c in conds}
        expected = {"positions.any_acct"}
        assert scopes == expected, f"Expected scopes {expected}, got {scopes}"

    def test_condition_is_all_not_any(self):
        """loss-rate-acct uses AND logic — both rate metrics must breach to fire."""
        a = _agent("loss-rate-acct")
        conds = a.get("conditions", {})
        assert "all" in conds, (
            "loss-rate-acct must use 'all' (AND) condition, not 'any' (OR)"
        )
        assert "any" not in conds, (
            "loss-rate-acct must not have an 'any' (OR) key; expected 'all' only"
        )

    def test_only_abs_breach_does_not_fire(self):
        """If ONLY abs breaches (-10001 abs, -0.10 pct) the agent must NOT fire (AND semantics)."""
        a = _agent("loss-rate-acct")
        conds = a["conditions"]["all"]
        # Simulate both conditions against mock values
        abs_val = -10001
        pct_val = -0.10
        results = []
        for c in conds:
            if c["metric"] == "pnl_rate_abs":
                results.append(abs_val <= c["value"])   # True: -10001 <= -10000
            elif c["metric"] == "pnl_rate_pct":
                results.append(pct_val <= c["value"])   # False: -0.10 > -0.25
        # AND: must all be True to fire — one False means no fire
        assert not all(results), (
            "Agent should NOT fire when only abs breaches (AND semantics)"
        )

    def test_only_pct_breach_does_not_fire(self):
        """If ONLY pct breaches (-2000 abs, -0.30 pct) the agent must NOT fire (AND semantics)."""
        a = _agent("loss-rate-acct")
        conds = a["conditions"]["all"]
        abs_val = -2000
        pct_val = -0.30
        results = []
        for c in conds:
            if c["metric"] == "pnl_rate_abs":
                results.append(abs_val <= c["value"])   # False: -2000 > -10000
            elif c["metric"] == "pnl_rate_pct":
                results.append(pct_val <= c["value"])   # True: -0.30 <= -0.25
        assert not all(results), (
            "Agent should NOT fire when only pct breaches (AND semantics)"
        )

    def test_both_breach_fires(self):
        """If BOTH abs and pct breach (-10001 abs, -0.30 pct) the agent DOES fire (AND semantics)."""
        a = _agent("loss-rate-acct")
        conds = a["conditions"]["all"]
        abs_val = -10001
        pct_val = -0.30
        results = []
        for c in conds:
            if c["metric"] == "pnl_rate_abs":
                results.append(abs_val <= c["value"])   # True: -10001 <= -10000
            elif c["metric"] == "pnl_rate_pct":
                results.append(pct_val <= c["value"])   # True: -0.30 <= -0.25
        assert all(results), (
            "Agent should fire when BOTH abs and pct breach (AND semantics)"
        )


class TestLossPositionsAcct:
    """Tests for loss-positions-acct (static thresholds only, after split)."""

    def test_exists(self):
        """loss-positions-acct must remain in BUILTIN_AGENTS."""
        a = _agent("loss-positions-acct")
        assert a is not None, "loss-positions-acct not found in BUILTIN_AGENTS"

    def test_tier_is_high(self):
        """loss-positions-acct is high-tier (vs critical for rate version)."""
        a = _agent("loss-positions-acct")
        assert a["tier"] == "high", f"Expected tier='high', got {a['tier']}"

    def test_cooldown_is_30_minutes(self):
        """loss-positions-acct uses default 30-minute cooldown."""
        a = _agent("loss-positions-acct")
        # If not explicitly set, inherits from _LOSS_AGENT_DEFAULTS (30 min)
        cooldown = a.get("cooldown_minutes", 30)
        assert cooldown == 30, f"Expected 30, got {cooldown}"

    def test_has_no_rate_conditions(self):
        """loss-positions-acct must NOT contain pnl_rate_abs or pnl_rate_pct."""
        a = _agent("loss-positions-acct")
        conds = a["conditions"]["any"]
        rate_metrics = {c["metric"] for c in conds if "rate" in c["metric"]}
        assert rate_metrics == set(), (
            f"loss-positions-acct should have no rate conditions "
            f"(moved to loss-rate-acct), found {rate_metrics}"
        )

    def test_has_static_conditions(self):
        """loss-positions-acct must have day_val and day_pct static thresholds (migrated from pnl/pnl_pct)."""
        a = _agent("loss-positions-acct")
        conds = a["conditions"]["any"]
        metrics = {c["metric"] for c in conds}
        expected = {"day_val", "day_pct"}
        assert metrics == expected, (
            f"loss-positions-acct should have static metrics {expected}, "
            f"got {metrics}"
        )

    def test_in_ntfy_dict_as_high(self):
        """loss-positions-acct must map to 'high' in _LOSS_AGENT_NTFY."""
        assert "loss-positions-acct" in _LOSS_AGENT_NTFY, (
            "loss-positions-acct missing from _LOSS_AGENT_NTFY"
        )
        assert _LOSS_AGENT_NTFY["loss-positions-acct"] == "high", (
            f"loss-positions-acct should be 'high', "
            f"got {_LOSS_AGENT_NTFY['loss-positions-acct']}"
        )


class TestLossPositionsTotal:
    """Tests for loss-positions-total (critical, mixed static + rate)."""

    def test_exists(self):
        """loss-positions-total must be in BUILTIN_AGENTS."""
        a = _agent("loss-positions-total")
        assert a is not None, "loss-positions-total not found in BUILTIN_AGENTS"

    def test_tier_is_critical(self):
        """loss-positions-total is critical-tier (book-wide loss signal)."""
        a = _agent("loss-positions-total")
        assert a["tier"] == "critical", f"Expected tier='critical', got {a['tier']}"

    def test_keeps_rate_conditions(self):
        """loss-positions-total still has rate conditions (unlike per-acct)."""
        a = _agent("loss-positions-total")
        conds = a["conditions"]["any"]
        metrics = {c["metric"] for c in conds}
        rate_metrics = {m for m in metrics if "rate" in m}
        assert rate_metrics == {"pnl_rate_abs", "pnl_rate_pct"}, (
            f"loss-positions-total should keep rate conditions, "
            f"got {rate_metrics}"
        )

    def test_keeps_static_conditions(self):
        """loss-positions-total has day_val and day_pct static thresholds (migrated from pnl/pnl_pct)."""
        a = _agent("loss-positions-total")
        conds = a["conditions"]["any"]
        metrics = {c["metric"] for c in conds}
        static_metrics = {m for m in metrics if m in {"day_val", "day_pct"}}
        assert static_metrics == {"day_val", "day_pct"}, (
            f"loss-positions-total should have day_val + day_pct static conditions, "
            f"got {static_metrics}"
        )

    def test_in_ntfy_dict_as_urgent(self):
        """loss-positions-total must map to 'urgent' in _LOSS_AGENT_NTFY."""
        assert "loss-positions-total" in _LOSS_AGENT_NTFY, (
            "loss-positions-total missing from _LOSS_AGENT_NTFY"
        )
        assert _LOSS_AGENT_NTFY["loss-positions-total"] == "urgent", (
            f"loss-positions-total should be 'urgent', "
            f"got {_LOSS_AGENT_NTFY['loss-positions-total']}"
        )


class TestLossMarginLow:
    """Tests for loss-margin-low (early-warning, not critical)."""

    def test_exists(self):
        """loss-margin-low must be in BUILTIN_AGENTS."""
        a = _agent("loss-margin-low")
        assert a is not None, "loss-margin-low not found in BUILTIN_AGENTS"

    def test_tier_is_high(self):
        """loss-margin-low is high-tier (warning, not critical)."""
        a = _agent("loss-margin-low")
        assert a["tier"] == "high", f"Expected tier='high', got {a['tier']}"

    def test_in_ntfy_dict_as_high(self):
        """loss-margin-low must map to 'high' in _LOSS_AGENT_NTFY."""
        assert "loss-margin-low" in _LOSS_AGENT_NTFY, (
            "loss-margin-low missing from _LOSS_AGENT_NTFY"
        )
        assert _LOSS_AGENT_NTFY["loss-margin-low"] == "high", (
            f"loss-margin-low should be 'high', "
            f"got {_LOSS_AGENT_NTFY['loss-margin-low']}"
        )


class TestLossFundsNegative:
    """Tests for loss-funds-negative (critical operational health)."""

    def test_exists(self):
        """loss-funds-negative must be in BUILTIN_AGENTS."""
        a = _agent("loss-funds-negative")
        assert a is not None, "loss-funds-negative not found in BUILTIN_AGENTS"

    def test_tier_is_critical(self):
        """loss-funds-negative is critical-tier (operational health)."""
        a = _agent("loss-funds-negative")
        assert a["tier"] == "critical", f"Expected tier='critical', got {a['tier']}"

    def test_in_ntfy_dict_as_urgent(self):
        """loss-funds-negative must map to 'urgent' in _LOSS_AGENT_NTFY."""
        assert "loss-funds-negative" in _LOSS_AGENT_NTFY, (
            "loss-funds-negative missing from _LOSS_AGENT_NTFY"
        )
        assert _LOSS_AGENT_NTFY["loss-funds-negative"] == "urgent", (
            f"loss-funds-negative should be 'urgent', "
            f"got {_LOSS_AGENT_NTFY['loss-funds-negative']}"
        )


class TestLossPosAutoClose:
    """Tests for loss-pos-total-auto-close (destructive, ships inactive)."""

    def test_exists(self):
        """loss-pos-total-auto-close must be in BUILTIN_AGENTS."""
        a = _agent("loss-pos-total-auto-close")
        assert a is not None, "loss-pos-total-auto-close not found in BUILTIN_AGENTS"

    def test_tier_is_critical(self):
        """loss-pos-total-auto-close is critical-tier (destructive action)."""
        a = _agent("loss-pos-total-auto-close")
        assert a["tier"] == "critical", f"Expected tier='critical', got {a['tier']}"

    def test_status_is_inactive(self):
        """loss-pos-total-auto-close ships inactive (destructive gate)."""
        a = _agent("loss-pos-total-auto-close")
        assert a.get("status") == "inactive", (
            f"Expected status='inactive', got {a.get('status')}"
        )

    def test_has_chase_close_action(self):
        """loss-pos-total-auto-close carries the chase_close_positions action."""
        a = _agent("loss-pos-total-auto-close")
        actions = a.get("actions", [])
        chase_actions = [ac for ac in actions if ac.get("type") == "chase_close_positions"]
        assert len(chase_actions) > 0, "Missing chase_close_positions action"

    def test_in_ntfy_dict_as_urgent(self):
        """loss-pos-total-auto-close must map to 'urgent' in _LOSS_AGENT_NTFY."""
        assert "loss-pos-total-auto-close" in _LOSS_AGENT_NTFY, (
            "loss-pos-total-auto-close missing from _LOSS_AGENT_NTFY"
        )
        assert _LOSS_AGENT_NTFY["loss-pos-total-auto-close"] == "urgent", (
            f"loss-pos-total-auto-close should be 'urgent', "
            f"got {_LOSS_AGENT_NTFY['loss-pos-total-auto-close']}"
        )


class TestLossAgentsConsistency:
    """Cross-cutting tests for loss agent consistency."""

    def test_all_loss_agents_in_ntfy_dict(self):
        """Every loss-* agent must have an entry in _LOSS_AGENT_NTFY."""
        loss_agents = [a for a in BUILTIN_AGENTS if a["slug"].startswith("loss-")]
        loss_slugs = {a["slug"] for a in loss_agents}

        missing = loss_slugs - set(_LOSS_AGENT_NTFY.keys())
        assert missing == set(), (
            f"Loss agents without _LOSS_AGENT_NTFY entry: {missing}"
        )

    def test_all_ntfy_entries_are_loss_agents(self):
        """Every entry in _LOSS_AGENT_NTFY must correspond to a loss agent."""
        loss_agents = [a for a in BUILTIN_AGENTS if a["slug"].startswith("loss-")]
        loss_slugs = {a["slug"] for a in loss_agents}

        extra = set(_LOSS_AGENT_NTFY.keys()) - loss_slugs
        assert extra == set(), (
            f"_LOSS_AGENT_NTFY entries with no corresponding agent: {extra}"
        )

    def test_ntfy_priority_is_valid(self):
        """All ntfy priorities must be either 'high' or 'urgent'."""
        for slug, priority in _LOSS_AGENT_NTFY.items():
            assert priority in {"high", "urgent"}, (
                f"{slug} has invalid priority {priority}; "
                f"must be 'high' or 'urgent'"
            )

    def test_loss_agents_have_required_fields(self):
        """Every loss agent must have slug, tier, topic, conditions."""
        loss_agents = [a for a in BUILTIN_AGENTS if a["slug"].startswith("loss-")]
        required_fields = {"slug", "tier", "topic", "conditions"}

        for agent in loss_agents:
            missing = required_fields - set(agent.keys())
            assert missing == set(), (
                f"Agent {agent.get('slug')} missing fields: {missing}"
            )

    def test_rate_conditions_only_in_critical_acct_agents(self):
        """
        Rate conditions (pnl_rate_*) should only appear in:
          - loss-rate-acct (new critical burn-rate-only agent)
          - loss-positions-total (mixed, already critical)

        They must NOT appear in per-account static-only agents:
          - loss-positions-acct
          - loss-margin-low
          - loss-funds-negative
        """
        static_only_slugs = {
            "loss-positions-acct",
            "loss-margin-low",
            "loss-funds-negative",
        }

        for agent in BUILTIN_AGENTS:
            if agent["slug"] not in static_only_slugs:
                continue

            conds = agent.get("conditions", {})
            # Handle both single-metric and any:/all: trees
            if isinstance(conds, dict):
                if "any" in conds:
                    metrics = {c.get("metric") for c in conds["any"]}
                elif "all" in conds:
                    metrics = {c.get("metric") for c in conds["all"]}
                else:
                    metrics = {conds.get("metric")}
            else:
                metrics = set()

            rate_metrics = {m for m in metrics if "rate" in str(m)}
            assert rate_metrics == set(), (
                f"Agent {agent['slug']} should not have rate conditions, "
                f"found {rate_metrics}"
            )

    def test_loss_agents_count_is_six(self):
        """Exactly 6 loss agents expected after rate/static split."""
        loss_agents = [a for a in BUILTIN_AGENTS if a["slug"].startswith("loss-")]
        expected_count = 6
        assert len(loss_agents) == expected_count, (
            f"Expected {expected_count} loss agents, "
            f"found {len(loss_agents)}: {[a['slug'] for a in loss_agents]}"
        )

    def test_loss_agents_are_active_except_auto_close(self):
        """All loss agents except loss-pos-total-auto-close ship active."""
        for agent in BUILTIN_AGENTS:
            if not agent["slug"].startswith("loss-"):
                continue
            if agent["slug"] in ("loss-pos-total-auto-close", "loss-margin-low"):
                assert agent.get("status") == "inactive", (
                    f"{agent['slug']} should ship inactive"
                )
            else:
                status = agent.get("status", "active")
                assert status == "active", (
                    f"Agent {agent['slug']} should ship active, "
                    f"got status={status}"
                )


class TestConditionTreeStructure:
    """Tests for condition tree format and structure."""

    def test_loss_rate_acct_condition_tree(self):
        """loss-rate-acct conditions are a valid all: tree (AND semantics)."""
        a = _agent("loss-rate-acct")
        conds = a.get("conditions")
        assert "all" in conds, "loss-rate-acct must have 'all' key (AND condition)"
        assert "any" not in conds, "loss-rate-acct must not have 'any' key"

        items = conds["all"]
        assert isinstance(items, list), "conditions['all'] must be a list"
        assert len(items) >= 2, "loss-rate-acct should have at least 2 conditions"

        # Each item should have metric, scope, op, value
        for item in items:
            assert "metric" in item, f"Missing metric in {item}"
            assert "scope" in item, f"Missing scope in {item}"
            assert "op" in item, f"Missing op in {item}"
            assert "value" in item, f"Missing value in {item}"

    def test_loss_positions_acct_condition_tree(self):
        """loss-positions-acct conditions are a valid any: tree."""
        a = _agent("loss-positions-acct")
        conds = a.get("conditions")
        assert "any" in conds, "loss-positions-acct must have 'any' key"

        items = conds["any"]
        assert isinstance(items, list), "conditions['any'] must be a list"
        assert len(items) >= 2, "loss-positions-acct should have at least 2 conditions"

    def test_loss_positions_total_condition_tree(self):
        """loss-positions-total conditions are a valid any: tree with 4 items (2 static + 2 rate)."""
        a = _agent("loss-positions-total")
        conds = a.get("conditions")
        assert "any" in conds, "loss-positions-total must have 'any' key"

        items = conds["any"]
        assert isinstance(items, list), "conditions['any'] must be a list"
        assert len(items) == 4, (
            f"loss-positions-total should have exactly 4 conditions "
            f"(2 static day_val/day_pct + 2 rate), got {len(items)}"
        )
