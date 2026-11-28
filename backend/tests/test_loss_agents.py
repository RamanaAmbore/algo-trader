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


def _collect_metrics(cond: dict) -> set:
    """Recursively collect every leaf `metric` value in a condition tree.

    2026-11 noise-reduction redesign (loss-positions-total) nests its
    rate leaves inside an `all[any[...], any[...]]` subtree (gated on
    drawdown-from-peak) — flat `{c['metric'] for c in conds}` no longer
    reaches them, so tests that only care "is this metric present
    anywhere" walk the whole tree instead of assuming a flat shape."""
    out = set()
    if not isinstance(cond, dict):
        return out
    for key in ('all', 'any'):
        for child in (cond.get(key) or []):
            out |= _collect_metrics(child)
    if 'not' in cond:
        out |= _collect_metrics(cond['not'])
    if 'metric' in cond:
        out.add(cond['metric'])
    return out


class TestLossRateAcct:
    """Tests for loss-rate-acct (new burn-rate-only agent)."""

    def test_exists(self):
        """loss-rate-acct must be in BUILTIN_AGENTS."""
        a = _agent("loss-rate-acct")
        assert a is not None, "loss-rate-acct not found in BUILTIN_AGENTS"

    def test_status_is_inactive(self):
        """2026-09 alert-count reduction: loss-rate-acct ships inactive —
        redundant with the aggregate loss-positions-total agent now that
        rate/loss checks are aggregate-only."""
        a = _agent("loss-rate-acct")
        assert a.get("status") == "inactive", (
            f"Expected status='inactive', got {a.get('status')}"
        )

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

    def test_status_is_inactive(self):
        """2026-09 alert-count reduction: loss-positions-acct ships inactive —
        redundant with the aggregate loss-positions-total agent now that
        rate/loss checks are aggregate-only."""
        a = _agent("loss-positions-acct")
        assert a.get("status") == "inactive", (
            f"Expected status='inactive', got {a.get('status')}"
        )

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
    """Tests for loss-positions-total (critical, PURE F&O theoretical
    loss — redesigned 2026-11-28, see agent_engine.py's own comment on
    this agent and CLAUDE.md for the full rationale. No longer reads
    market day_change_val/day P&L at all; see
    test_loss_positions_total_theoretical_redesign.py for the dedicated
    scenario coverage (decay fires, equity-only never fires, rate-gating,
    leaf1-vs-leaf3 divergence, migration, etc.)."""

    def test_exists(self):
        """loss-positions-total must be in BUILTIN_AGENTS."""
        a = _agent("loss-positions-total")
        assert a is not None, "loss-positions-total not found in BUILTIN_AGENTS"

    def test_tier_is_critical(self):
        """loss-positions-total is critical-tier (book-wide loss signal)."""
        a = _agent("loss-positions-total")
        assert a["tier"] == "critical", f"Expected tier='critical', got {a['tier']}"

    def test_keeps_rate_conditions(self):
        """loss-positions-total still has rate conditions (unlike per-acct).

        2026-11-28 redesign: these now live nested inside an
        `all[theoretical_total_loss<=..., any[...]]` subtree (gated on
        leaf 1 itself crossing its own threshold), not flat under the
        top-level `any` — see `_collect_metrics`."""
        a = _agent("loss-positions-total")
        metrics = _collect_metrics(a["conditions"])
        rate_metrics = {m for m in metrics if "rate" in m}
        assert rate_metrics == {"theo_total_rate_abs", "theo_total_rate_pct"}, (
            f"loss-positions-total should keep rate conditions, "
            f"got {rate_metrics}"
        )

    def test_keeps_static_conditions(self):
        """loss-positions-total has theoretical_total_loss (leaf 1,
        since-ENTRY level) and theoretical_daily_loss (leaf 3,
        since-this-morning delta) static thresholds. The 2026-11-28
        redesign RETIRED day_val/day_pct (market P&L) entirely — this
        agent is now pure F&O theoretical loss, no equity leaf."""
        a = _agent("loss-positions-total")
        metrics = _collect_metrics(a["conditions"])
        static_metrics = {m for m in metrics
                          if m in {"theoretical_total_loss", "theoretical_daily_loss"}}
        assert static_metrics == {"theoretical_total_loss", "theoretical_daily_loss"}, (
            f"loss-positions-total should have theoretical_total_loss + "
            f"theoretical_daily_loss static conditions, got {static_metrics}"
        )
        assert "day_val" not in metrics and "day_pct" not in metrics, (
            f"the market-P&L leaves must be fully retired, got {metrics}"
        )

    def test_theoretical_total_loss_floor_is_100000(self):
        """Leaf 1 ("total") floor: -₹100,000 — the book's own
        lifetime-since-entry theoretical shortfall."""
        a = _agent("loss-positions-total")
        top = a["conditions"]["any"]
        leaf = next(c for c in top if c.get("metric") == "theoretical_total_loss")
        assert leaf["value"] == -100000

    def test_theoretical_daily_loss_floor_is_100000(self):
        """Leaf 3 ("daily") floor: -₹100,000 — today's own theoretical
        P&L move since the per-session frozen baseline."""
        a = _agent("loss-positions-total")
        top = a["conditions"]["any"]
        leaf = next(c for c in top if c.get("metric") == "theoretical_daily_loss")
        assert leaf["value"] == -100000

    def test_rate_leaves_gated_on_theoretical_total_loss_crossing(self):
        """2026-11-28 redesign — leaf 2 (the rate pair) is wrapped in
        all[theoretical_total_loss<=-100000, any[rate_abs, rate_pct]],
        directly implementing the operator's "only... when the total
        theoretical loss crosses the threshold" instruction. Replaces
        the retired drawdown-from-peak gate outright."""
        a = _agent("loss-positions-total")
        top = a["conditions"]["any"]
        gated = next((c for c in top if "all" in c), None)
        assert gated is not None, "expected a nested all[] branch gating the rate leaves"
        all_children = gated["all"]
        assert len(all_children) == 2
        gate_leaf, rate_branch = all_children
        assert gate_leaf["metric"] == "theoretical_total_loss", (
            f"leaf 2's gate must be leaf 1 itself, got {gate_leaf}"
        )
        assert gate_leaf["op"] == "<=" and gate_leaf["value"] == -100000
        rate_metrics = {c["metric"] for c in rate_branch["any"]}
        assert rate_metrics == {"theo_total_rate_abs", "theo_total_rate_pct"}

    def test_cooldown_unchanged_at_45_minutes(self):
        """Cooldown stays 45 minutes — unchanged by the 2026-11-28
        redesign (only the leaves themselves changed)."""
        a = _agent("loss-positions-total")
        assert a["cooldown_minutes"] == 45

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

    def test_status_is_active(self):
        """loss-margin-low shipped inactive from 2026-09 (cross-account
        false-positive source) — reactivated 2026-11-28 once its two root
        causes were fixed and verified: (1) `all[]` cross-account
        flattening (agent_evaluator.py `_eval_all`, 'Fix #2'), and (2) the
        Dhan/Groww funds adapters' missing-vs-zero coercion
        (`_dhan_num_or_none`/`_gf_or_none`, 'Fix #3'). Default status is
        'active' via `_LOSS_AGENT_DEFAULTS` — no explicit `status` key on
        this agent's own spec dict any more."""
        a = _agent("loss-margin-low")
        assert a.get("status") == "active", (
            f"Expected status='active', got {a.get('status')}"
        )

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

    def test_loss_agents_count_is_seven(self):
        """Exactly 7 loss agents — 6 after the rate/static split, plus
        loss-positions-underlying-divergence (theoretical-vs-market loss
        divergence gate, per underlying)."""
        loss_agents = [a for a in BUILTIN_AGENTS if a["slug"].startswith("loss-")]
        expected_count = 7
        assert len(loss_agents) == expected_count, (
            f"Expected {expected_count} loss agents, "
            f"found {len(loss_agents)}: {[a['slug'] for a in loss_agents]}"
        )

    def test_loss_agents_are_active_except_auto_close(self):
        """Alert-count reduction (2026-09): loss-rate-acct and
        loss-positions-acct are now redundant with the aggregate
        loss-positions-total agent and ship inactive by default, alongside
        the pre-existing inactive-by-default loss-pos-total-auto-close
        (destructive gate).

        loss-margin-low shipped inactive from 2026-09 (cross-account
        false-positive source) and was reactivated 2026-11-28 once its two
        root causes — `_eval_all`'s cross-account flattening ('Fix #2')
        and the Dhan/Groww funds adapters' missing-vs-zero coercion
        ('Fix #3') — were fixed, tested, and confirmed live on prod. It is
        now in the active group below.

        loss-positions-underlying-divergence shipped inactive on
        2026-10-22 pending a fix to a reference-point mismatch between its
        two gate sides (underlying_market_loss vs underlying_theoretical_
        loss — see grammar_tokens/metric_underlying_market_loss.py's `_metric_underlying_market_loss`
        docstring); fixed and activated the same day, so it is now in the
        active group below alongside loss-positions-total/loss-funds-
        negative."""
        inactive_slugs = (
            "loss-pos-total-auto-close",
            "loss-rate-acct",
            "loss-positions-acct",
        )
        for agent in BUILTIN_AGENTS:
            if not agent["slug"].startswith("loss-"):
                continue
            if agent["slug"] in inactive_slugs:
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
        """loss-positions-total conditions are a valid any: tree.

        2026-11-28 redesign: 3 top-level items (theoretical_total_loss
        [leaf 1], theoretical_daily_loss [leaf 3], and a nested all[]
        branch gating the 2 rate leaves [leaf 2] on leaf 1 itself
        crossing its own threshold) — same 3-item/1-nested-all[]
        STRUCTURE the prior drawdown-gated redesign had, just different
        leaf names/semantics. See TestLossPositionsTotal's
        test_rate_leaves_gated_on_theoretical_total_loss_crossing for
        the nested shape itself."""
        a = _agent("loss-positions-total")
        conds = a.get("conditions")
        assert "any" in conds, "loss-positions-total must have 'any' key"

        items = conds["any"]
        assert isinstance(items, list), "conditions['any'] must be a list"
        assert len(items) == 3, (
            f"loss-positions-total should have exactly 3 top-level "
            f"conditions (leaf1, leaf3, nested leaf2-gate branch), "
            f"got {len(items)}"
        )
        assert sum(1 for i in items if "all" in i) == 1, (
            "expected exactly one nested all[] branch")


# ── divergence-agent cooldown reduction + restart-sync (2026-11-26) ──
# Operator: "even divergence alerts need to be reduced". Stock cooldown
# raised 10 -> 45 minutes, matching the precedent already set for the
# sibling loss-positions-total redesign. Needs its own value-gated
# restart migration (same contract as _ae_sync_expiry_awareness_cooldown)
# because cooldown_minutes is never force-synced on an ordinary restart.

class TestDivergenceAgentCooldownReduction:
    def test_spec_cooldown_is_0(self):
        """2026-11-26 — superseded from an earlier same-session 45-minute
        edit. 0 is correct: it fixes a confirmed cross-underlying
        cooldown-silencing bug (see
        test_underlying_loss_divergence_tokens.py::TestFixDCrossUnderlyingCooldownSilencing).
        A nonzero AGENT-level cooldown here would silence a genuinely
        DIFFERENT underlying's breach for the whole window once any one
        underlying fires — 45 would have made that window longer, not
        fixed it. Per-underlying dedup still applies via the per-leaf
        escalation gate's own fallback to `alerts.cooldown_minutes`."""
        a = _agent("loss-positions-underlying-divergence")
        assert a["cooldown_minutes"] == 0

    def test_sync_force_upgrades_a_stale_stock_row(self):
        from backend.api.algo.agent_engine import _ae_sync_divergence_cooldown

        class _Row:
            pass

        existing = _Row()
        existing.cooldown_minutes = 10
        agent_def = _agent("loss-positions-underlying-divergence")

        _ae_sync_divergence_cooldown(existing, agent_def)

        assert existing.cooldown_minutes == 0

    def test_sync_leaves_an_operator_customized_cooldown_alone(self):
        from backend.api.algo.agent_engine import _ae_sync_divergence_cooldown

        class _Row:
            pass

        existing = _Row()
        existing.cooldown_minutes = 5  # operator's own custom value
        agent_def = _agent("loss-positions-underlying-divergence")

        _ae_sync_divergence_cooldown(existing, agent_def)

        assert existing.cooldown_minutes == 5

    def test_sync_is_slug_gated_and_no_op_for_other_agents(self):
        from backend.api.algo.agent_engine import _ae_sync_divergence_cooldown

        class _Row:
            pass

        existing = _Row()
        existing.cooldown_minutes = 10
        agent_def = _agent("loss-rate-acct")  # also stock 10, different slug

        _ae_sync_divergence_cooldown(existing, agent_def)

        assert existing.cooldown_minutes == 10, (
            "a 10-min cooldown on an unrelated agent must never be "
            "force-upgraded by this divergence-only migration"
        )


# ── signed-divergence condition-shape restart migration (2026-11-26) ─
# `_ae_apply_content_matched_cond_migration` (table-driven, keyed by slug
# in `_AE_CONTENT_MATCHED_COND_MIGRATIONS`) must force-upgrade an
# already-seeded `loss-positions-underlying-divergence` row still at the
# OLD unsigned abs() condition shape to the NEW signed shape — otherwise
# shipping the signed metric in code alone is inert against a live row.

class TestDivergenceAgentSignedConditionMigration:
    def test_stale_unsigned_abs_row_is_force_upgraded(self):
        from backend.api.algo.agent_engine import _ae_apply_content_matched_cond_migration

        class _Row:
            pass

        existing = _Row()
        existing.conditions = {"all": [
            {"metric": "underlying_loss_divergence", "scope": "positions.any_underlying",
             "op": ">", "value": 100000},
            {"any": [
                {"metric": "pnl_rate_abs", "scope": "positions.any_underlying", "op": "<=", "value": -10000},
                {"metric": "pnl_rate_pct", "scope": "positions.any_underlying", "op": "<=", "value": -0.25},
            ]},
        ]}
        agent_def = _agent("loss-positions-underlying-divergence")

        _ae_apply_content_matched_cond_migration(existing, agent_def)

        assert existing.conditions == agent_def["conditions"], (
            "a stock unsigned-abs row must force-upgrade to the new "
            "signed condition tree on restart"
        )
        assert existing.conditions["all"][0]["metric"] == "underlying_loss_divergence_signed"
        assert existing.conditions["all"][0]["op"] == "<="
        assert existing.conditions["all"][0]["value"] == -100000

    def test_operator_customized_row_is_left_alone(self):
        from backend.api.algo.agent_engine import _ae_apply_content_matched_cond_migration

        class _Row:
            pass

        existing = _Row()
        custom = {"all": [
            {"metric": "underlying_loss_divergence", "scope": "positions.any_underlying",
             "op": ">", "value": 250000},  # operator raised the threshold
            {"any": [
                {"metric": "pnl_rate_abs", "scope": "positions.any_underlying", "op": "<=", "value": -10000},
                {"metric": "pnl_rate_pct", "scope": "positions.any_underlying", "op": "<=", "value": -0.25},
            ]},
        ]}
        existing.conditions = custom
        agent_def = _agent("loss-positions-underlying-divergence")

        _ae_apply_content_matched_cond_migration(existing, agent_def)

        assert existing.conditions == custom, (
            "a row that doesn't byte-for-byte match the old stock shape "
            "(e.g. an operator-raised threshold) must never be overwritten"
        )

    def test_already_migrated_row_is_a_no_op(self):
        from backend.api.algo.agent_engine import _ae_apply_content_matched_cond_migration

        class _Row:
            pass

        existing = _Row()
        agent_def = _agent("loss-positions-underlying-divergence")
        existing.conditions = agent_def["conditions"]

        _ae_apply_content_matched_cond_migration(existing, agent_def)

        assert existing.conditions == agent_def["conditions"]
