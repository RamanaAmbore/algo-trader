"""
Tests for Part 2 of the RamboQL GIVEN upgrade — server-evaluated,
per-tick, per-agent GIVEN bindings (backend/api/algo/agent_engine.py's
`_cycle_evaluate_given_bindings` + helpers), wired into
`_cycle_evaluate_agent` before `v2_evaluate` runs.

Covers:
  - Bare `metric@scope` detection (`_given_binding_bare_metric_scope`).
  - Aggregate-scope resolution (one synthetic row) via the SAME
    scope-selector + metric-resolver machinery `_eval_leaf` uses.
  - Operator-expression resolution via `expr_eval.eval_expr`, with a
    namespace built from this agent's own earlier-resolved bindings.
  - Per-agent key isolation: two agents evaluated in the same tick
    (same shared `context` dict) never leak bindings into each other.
  - Exception isolation: a forced `ExprError` in one agent's GIVEN
    evaluation never propagates out of `_cycle_evaluate_given_bindings`
    — a sibling agent evaluated afterward in the same tick still
    resolves normally.
  - Phase-1 scope limitation: an aggregate scope (one row) resolves
    fine; a per-row scope (more than one row selected) logs a warning
    and skips that one binding (and anything depending on it) rather
    than crashing.
  - `agent.given` absent/empty is a true no-op (no `context["given"]`
    key created at all).

No DB / fixtures needed — pure unit tests against a minimal grammar
registry seed (mirrors test_agent_template_unification.py's
`_seed_grammar_minimal` pattern) and a plain Agent stand-in.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from backend.api.algo.agent_engine import (
    _cycle_evaluate_given_bindings,
    _given_binding_bare_metric_scope,
    _given_resolve_bare_metric_scope,
    _GIVEN_UNRESOLVED,
)
from backend.api.algo.agent_evaluator import Context as V2Context
from backend.api.algo.grammar_registry import REGISTRY


class _Agent:
    """Minimal Agent-row stand-in carrying only what
    `_cycle_evaluate_given_bindings` reads: id, slug, given."""
    def __init__(self, id, slug, given=None):
        self.id = id
        self.slug = slug
        self.given = given or []


@pytest.fixture(autouse=True)
def _seed_grammar_minimal():
    """Register just enough of the real grammar.py resolvers for
    'pnl@positions.total' / 'pnl@positions.any_acct' to resolve through
    the real REGISTRY.scope()/REGISTRY.metric() machinery, PLUS the real
    comparison operators (needed for the full `_cycle_evaluate_agent`
    integration test below, whose condition tree needs `REGISTRY.op('<=')`
    to resolve) — same pattern test_agent_template_unification.py already
    uses. Restores whatever was there before (defensive — REGISTRY is a
    module singleton)."""
    from backend.api.algo import grammar as _grammar
    from backend.api.algo.grammar_tokens.metric_pnl import _metric_pnl

    prior_metrics = dict(REGISTRY.metrics)
    prior_scopes = dict(REGISTRY.scopes)
    prior_operators = dict(REGISTRY.operators)
    REGISTRY.metrics["pnl"] = _metric_pnl
    REGISTRY.scopes["positions.total"] = _grammar._scope_positions_total
    REGISTRY.scopes["positions.any_acct"] = _grammar._scope_positions_any_acct
    for k, v in _grammar.OPERATORS.items():
        REGISTRY.operators[k] = v
    yield
    REGISTRY.metrics.clear(); REGISTRY.metrics.update(prior_metrics)
    REGISTRY.scopes.clear(); REGISTRY.scopes.update(prior_scopes)
    REGISTRY.operators.clear(); REGISTRY.operators.update(prior_operators)


def _ctx(rows: list[dict]) -> V2Context:
    df = pd.DataFrame(rows)
    return V2Context(sum_positions=df, now=datetime.now(timezone.utc))


# ═══════════════════════════════════════════════════════════════════════
#  Bare metric@scope detection
# ═══════════════════════════════════════════════════════════════════════

class TestBareMetricScopeDetection:
    def test_bare_reference_matches(self):
        assert _given_binding_bare_metric_scope("pnl@positions.total") is not None

    def test_bare_reference_with_surrounding_whitespace_matches(self):
        assert _given_binding_bare_metric_scope("  pnl@positions.total  ") is not None

    def test_operator_expression_does_not_match(self):
        assert _given_binding_bare_metric_scope("pnl@positions.total + 1") is None

    def test_string_concat_expression_does_not_match(self):
        assert _given_binding_bare_metric_scope('"Loss: " + p') is None

    def test_plain_name_reference_does_not_match_without_at(self):
        # No '@' at all — not a metric@scope shape, falls through to
        # eval_expr as a bare Name lookup instead.
        assert _given_binding_bare_metric_scope("p") is None


# ═══════════════════════════════════════════════════════════════════════
#  Aggregate-scope resolution (one synthetic row)
# ═══════════════════════════════════════════════════════════════════════

class TestAggregateScopeResolution:
    def test_resolves_real_value_from_total_row(self):
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        match = _given_binding_bare_metric_scope("pnl@positions.total")
        value = _given_resolve_bare_metric_scope(match, v2_ctx, "test-agent")
        assert value == -60000.0

    def test_zero_rows_resolves_to_none(self):
        v2_ctx = _ctx([])
        match = _given_binding_bare_metric_scope("pnl@positions.total")
        value = _given_resolve_bare_metric_scope(match, v2_ctx, "test-agent")
        assert value is None

    def test_unknown_metric_returns_unresolved_sentinel(self):
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -1.0}])
        match = _given_binding_bare_metric_scope("not_a_real_metric@positions.total")
        value = _given_resolve_bare_metric_scope(match, v2_ctx, "test-agent")
        assert value is _GIVEN_UNRESOLVED

    def test_unknown_scope_returns_unresolved_sentinel(self):
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -1.0}])
        match = _given_binding_bare_metric_scope("pnl@not_a_real_scope")
        value = _given_resolve_bare_metric_scope(match, v2_ctx, "test-agent")
        assert value is _GIVEN_UNRESOLVED


# ═══════════════════════════════════════════════════════════════════════
#  Full per-agent evaluation step — _cycle_evaluate_given_bindings
# ═══════════════════════════════════════════════════════════════════════

class TestFullBindingEvaluation:
    def test_bare_alias_binding_resolves_and_is_stored_per_agent(self):
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        agent = _Agent(id=1, slug="a1", given=[{"name": "p", "expr": "pnl@positions.total"}])
        context: dict = {}
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        assert context["given"][1] == {"p": -60000.0}

    def test_operator_expression_resolves_against_earlier_binding(self):
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        agent = _Agent(id=1, slug="a1", given=[
            {"name": "p", "expr": "pnl@positions.total"},
            {"name": "double_p", "expr": "p * 2"},
        ])
        context: dict = {}
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        assert context["given"][1] == {"p": -60000.0, "double_p": -120000.0}

    def test_no_given_list_is_true_noop(self):
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -1.0}])
        agent = _Agent(id=1, slug="a1", given=[])
        context: dict = {}
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        assert "given" not in context, "an agent with no GIVEN bindings must not create the 'given' key at all"

    def test_agent_with_no_given_attribute_at_all_is_true_noop(self):
        """Mirrors a real Agent ORM row today (column doesn't exist yet)
        — getattr(agent, 'given', None) must degrade cleanly."""
        class _BareAgent:
            def __init__(self):
                self.id = 9
                self.slug = "bare"
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -1.0}])
        context: dict = {}
        _cycle_evaluate_given_bindings(_BareAgent(), v2_ctx, context)
        assert "given" not in context


# ═══════════════════════════════════════════════════════════════════════
#  Per-agent key isolation (risk council's mandated fix)
# ═══════════════════════════════════════════════════════════════════════

class TestPerAgentIsolation:
    def test_two_agents_same_tick_do_not_leak_bindings(self):
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        context: dict = {}

        agent1 = _Agent(id=1, slug="agent-one", given=[
            {"name": "p", "expr": "pnl@positions.total"},
        ])
        agent2 = _Agent(id=2, slug="agent-two", given=[
            {"name": "p", "expr": "pnl@positions.total"},
            {"name": "q", "expr": "p * 2"},
        ])

        _cycle_evaluate_given_bindings(agent1, v2_ctx, context)
        _cycle_evaluate_given_bindings(agent2, v2_ctx, context)

        assert context["given"][1] == {"p": -60000.0}
        assert "q" not in context["given"][1], (
            "agent 2's 'q' binding must never leak into agent 1's bindings dict"
        )
        assert context["given"][2] == {"p": -60000.0, "q": -120000.0}

    def test_bindings_stored_under_agent_id_never_a_bare_name(self):
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": 5.0}])
        context: dict = {}
        agent = _Agent(id=42, slug="any-slug", given=[{"name": "p", "expr": "pnl@positions.total"}])
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        assert "p" not in context, "bindings must never be stored as a bare top-level context key"
        assert context["given"][42] == {"p": 5.0}


# ═══════════════════════════════════════════════════════════════════════
#  Exception isolation (risk council's mandated fix)
# ═══════════════════════════════════════════════════════════════════════

class TestExceptionIsolation:
    def test_division_by_zero_logs_and_does_not_raise(self):
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        context: dict = {}
        agent = _Agent(id=3, slug="bad-agent", given=[
            {"name": "p", "expr": "pnl@positions.total"},
            {"name": "bad", "expr": "p / 0"},
        ])
        # Must not raise.
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        # Only the binding before the failure survives.
        assert context["given"][3] == {"p": -60000.0}
        assert "bad" not in context["given"][3]

    def test_sibling_agent_still_evaluates_after_a_forced_exprerror(self):
        """A bad expression in agent 3 (above) must never prevent agent 4,
        evaluated afterward in the SAME tick/cycle, from resolving
        normally — this is the exact invariant run_cycle()'s own
        per-agent loop (no surrounding try/except) depends on."""
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        context: dict = {}

        bad_agent = _Agent(id=3, slug="bad-agent", given=[
            {"name": "p", "expr": "pnl@positions.total"},
            {"name": "bad", "expr": "p / 0"},
        ])
        good_agent = _Agent(id=4, slug="good-agent", given=[
            {"name": "p", "expr": "pnl@positions.total"},
        ])

        _cycle_evaluate_given_bindings(bad_agent, v2_ctx, context)
        _cycle_evaluate_given_bindings(good_agent, v2_ctx, context)

        assert context["given"][4] == {"p": -60000.0}, (
            "sibling agent must still evaluate normally after a forced "
            "ExprError in a different agent's GIVEN bindings this tick"
        )

    def test_undefined_name_reference_also_isolated(self):
        """A binding referencing a name that failed to resolve earlier
        (the per-row-scope case below) raises eval_expr's own 'undefined
        name' ExprError — still isolated, never raised out."""
        v2_ctx = _ctx([
            {"account": "ACC1", "pnl": 10.0},
            {"account": "ACC2", "pnl": 20.0},
        ])
        context: dict = {}
        agent = _Agent(id=5, slug="per-row-then-dependent", given=[
            {"name": "x", "expr": "pnl@positions.any_acct"},   # per-row scope -> unresolved
            {"name": "y", "expr": "x * 2"},                    # depends on x -> undefined name
        ])
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        assert context["given"][5] == {}


# ═══════════════════════════════════════════════════════════════════════
#  Phase-1 scope limitation — aggregate vs per-row
# ═══════════════════════════════════════════════════════════════════════

class TestAggregateVsPerRowScopeLimitation:
    def test_aggregate_scope_binding_resolves_fine(self):
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -1234.0}])
        context: dict = {}
        agent = _Agent(id=6, slug="aggregate-ok", given=[
            {"name": "p", "expr": "pnl@positions.total"},
        ])
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        assert context["given"][6] == {"p": -1234.0}

    def test_per_row_scope_binding_logs_warning_and_skips_without_crashing(self):
        from unittest.mock import patch
        from backend.api.algo import agent_engine as _ae_mod

        v2_ctx = _ctx([
            {"account": "ACC1", "pnl": 10.0},
            {"account": "ACC2", "pnl": 20.0},
        ])
        context: dict = {}
        agent = _Agent(id=7, slug="per-row-skip", given=[
            {"name": "x", "expr": "pnl@positions.any_acct"},
        ])
        warnings = []
        with patch.object(_ae_mod.logger, "warning", side_effect=lambda msg, *a, **k: warnings.append(msg)):
            _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        # No crash, and the per-row-scope binding was simply skipped —
        # left out of the bindings dict entirely (not stored as None).
        assert context["given"][7] == {}
        assert any("per-row scope" in msg for msg in warnings), (
            f"expected a warning naming the per-row-scope limitation, got {warnings!r}"
        )

    def test_per_row_scope_with_exactly_one_row_resolves_normally(self):
        """A per-row scope that happens to select exactly ONE row this
        tick is indistinguishable from an aggregate scope at runtime —
        this is intentional (the check is "more than one row", not a
        static scope-name whitelist)."""
        v2_ctx = _ctx([{"account": "ACC1", "pnl": 99.0}])
        context: dict = {}
        agent = _Agent(id=8, slug="per-row-single", given=[
            {"name": "x", "expr": "pnl@positions.any_acct"},
        ])
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        assert context["given"][8] == {"x": 99.0}


# ═══════════════════════════════════════════════════════════════════════
#  Full _cycle_evaluate_agent wiring — proves the call inside
#  _cycle_evaluate_agent is real, not just the standalone helper
# ═══════════════════════════════════════════════════════════════════════
#
# Every test above calls `_cycle_evaluate_given_bindings` directly — none
# of them would catch a regression where the call were ever removed from
# INSIDE `_cycle_evaluate_agent` itself. This class drives the real
# entry point `run_cycle()`/`_cycle_process_agent` actually calls.

class TestCycleEvaluateAgentRealWiring:
    def test_sibling_agent_fires_normally_after_a_bad_given_binding_same_tick(self):
        """Two agents sharing one `context` dict, evaluated via the REAL
        `_cycle_evaluate_agent` entry point (not the standalone helper):
        agent A's GIVEN has a forced division-by-zero; agent B's GIVEN
        is clean. Neither agent's WHEN condition depends on GIVEN at all
        (pure pnl@positions.total threshold) — proving GIVEN evaluation
        failures never block ordinary condition evaluation/firing for
        ANY agent, sibling or not, and that both agents still get a
        correctly isolated `context["given"][id]` entry."""
        from backend.api.algo.agent_engine import _cycle_evaluate_agent, _v2_cfg

        now = datetime(2026, 10, 13, 10, 0, 0, tzinfo=timezone.utc)
        condition = {"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -1}
        context = {
            "sum_positions": pd.DataFrame([{"account": "TOTAL", "pnl": -50000.0}]),
            "sum_holdings": pd.DataFrame(),
            "df_margins": pd.DataFrame(),
        }
        cfg = _v2_cfg()

        bad_agent = _Agent(id=10, slug="bad-given-agent", given=[
            {"name": "p", "expr": "pnl@positions.total"},
            {"name": "bad", "expr": "p / 0"},
        ])
        bad_agent.conditions = condition

        good_agent = _Agent(id=11, slug="good-given-agent", given=[
            {"name": "p", "expr": "pnl@positions.total"},
        ])
        good_agent.conditions = condition

        matches_bad, _obs_bad = _cycle_evaluate_agent(
            bad_agent, context, cfg, now, alert_state={}, bypass_schedule=True,
        )
        matches_good, _obs_good = _cycle_evaluate_agent(
            good_agent, context, cfg, now, alert_state={}, bypass_schedule=True,
        )

        assert matches_bad, (
            "agent with a bad GIVEN binding must still fire on its own "
            "WHEN condition (which doesn't reference GIVEN at all)"
        )
        assert matches_good, (
            "sibling agent must fire normally after the bad agent's "
            "GIVEN failure in the same tick/context"
        )
        assert context["given"][10] == {"p": -50000.0}, "bad agent keeps its one successfully-resolved binding"
        assert "bad" not in context["given"][10]
        assert context["given"][11] == {"p": -50000.0}, "good agent resolves fully, unaffected by its sibling"
