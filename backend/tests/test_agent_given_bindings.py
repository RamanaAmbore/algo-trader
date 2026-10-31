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

from datetime import datetime, timedelta, timezone

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
    from backend.api.algo.grammar_tokens.scope_positions_total import (
        _scope_positions_total,
    )
    from backend.api.algo.grammar_tokens.scope_positions_any_acct import (
        _scope_positions_any_acct,
    )

    prior_metrics = dict(REGISTRY.metrics)
    prior_scopes = dict(REGISTRY.scopes)
    prior_operators = dict(REGISTRY.operators)
    REGISTRY.metrics["pnl"] = _metric_pnl
    REGISTRY.scopes["positions.total"] = _scope_positions_total
    REGISTRY.scopes["positions.any_acct"] = _scope_positions_any_acct
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


# ═══════════════════════════════════════════════════════════════════════
#  Part 3 — WHEN leaves may reference a bound label by bare name
#
#  Wire shape: the leaf's `metric` (or `value`) field is the EXISTING
#  `{"$given": "<name>"}` marker (already used for a DO action param
#  referencing a bound label — see `actions.py:resolve_given_ref_params`)
#  in place of a plain string token. No new leaf JSON shape. A label is
#  bound by an ordinary assignment statement anywhere before `WHEN` — no
#  introducer keyword (the frontend grammar has none).
# ═══════════════════════════════════════════════════════════════════════

class TestLeafReferencesBoundLabel:
    """Item (a) — a leaf referencing a valid, resolving bound label
    matches correctly, via the REAL `_cycle_evaluate_agent` entry point
    (not just the standalone `_eval_leaf`/`evaluate()` helpers)."""

    def test_metric_position_reference_fires(self):
        from backend.api.algo.agent_engine import _cycle_evaluate_agent, _v2_cfg

        now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        # p = pnl@positions.total ;  gap = p + 1000
        # WHEN gap < 0 DO ...   (gap resolves to -60000 + 1000 = -59000)
        condition = {
            "metric": {"$given": "gap"}, "scope": "preamble",
            "op": "<", "value": 0,
        }
        context = {
            "sum_positions": pd.DataFrame([{"account": "TOTAL", "pnl": -60000.0}]),
            "sum_holdings": pd.DataFrame(),
            "df_margins": pd.DataFrame(),
        }
        cfg = _v2_cfg()
        agent = _Agent(id=20, slug="gap-agent", given=[
            {"name": "p", "expr": "pnl@positions.total"},
            {"name": "gap", "expr": "p + 1000"},
        ])
        agent.conditions = condition

        matches, observations = _cycle_evaluate_agent(
            agent, context, cfg, now, alert_state={}, bypass_schedule=True,
        )

        assert len(matches) == 1
        m = matches[0]
        # The match's own 'metric' field is the bound NAME ITSELF — a
        # plain, hashable string — never the marker dict. This is what
        # `_latch_key`/`_V2_LATCH` hash on.
        assert m["metric"] == "gap"
        assert m["value"] == -59000.0
        assert m["threshold"] == 0
        assert m["op"] == "<"
        # Aggregate, not per-row — matches every other aggregate scope's
        # own convention (never a per-account/per-symbol identity).
        assert m["account"] == "TOTAL"
        assert observations and observations[0]["fired"] is True

    def test_value_position_reference_against_an_ordinary_metric(self):
        """`pnl@positions.total < gap` — metric is an ordinary registered
        token, value is the bound-label reference."""
        from backend.api.algo.agent_engine import _cycle_evaluate_agent, _v2_cfg

        now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        condition = {
            "metric": "pnl", "scope": "positions.total", "op": "<",
            "value": {"$given": "gap"},
        }
        context = {
            "sum_positions": pd.DataFrame([{"account": "TOTAL", "pnl": -60000.0}]),
            "sum_holdings": pd.DataFrame(),
            "df_margins": pd.DataFrame(),
        }
        cfg = _v2_cfg()
        agent = _Agent(id=21, slug="gap-threshold-agent", given=[
            {"name": "gap", "expr": "-1000"},
        ])
        agent.conditions = condition

        matches, _obs = _cycle_evaluate_agent(
            agent, context, cfg, now, alert_state={}, bypass_schedule=True,
        )

        assert len(matches) == 1
        # Here the ordinary metric leaf fires — 'metric' stays the real
        # registered token, 'threshold' is the RESOLVED number (-1000),
        # never the marker dict.
        assert matches[0]["metric"] == "pnl"
        assert matches[0]["threshold"] == -1000.0

    def test_metric_name_equal_to_a_real_catalog_token_is_not_shadowed(self):
        """A bound label literally named 'pnl' must never make an
        ORDINARY `metric: 'pnl'` leaf (a plain string, not a marker)
        resolve against the binding instead of the real registered
        metric — the marker shape is the only thing that activates the
        bound-label path. Proves the two token spaces are disjoint."""
        from backend.api.algo.agent_engine import _cycle_evaluate_agent, _v2_cfg

        now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        condition = {"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -1}
        context = {
            "sum_positions": pd.DataFrame([{"account": "TOTAL", "pnl": -60000.0}]),
            "sum_holdings": pd.DataFrame(),
            "df_margins": pd.DataFrame(),
        }
        cfg = _v2_cfg()
        # Binds a label named 'pnl' to something wildly different — if
        # the ordinary leaf ever consulted bindings for a plain string
        # metric, this would corrupt the real pnl@positions.total read.
        agent = _Agent(id=22, slug="shadow-agent", given=[
            {"name": "pnl", "expr": "999999"},
        ])
        agent.conditions = condition

        matches, _obs = _cycle_evaluate_agent(
            agent, context, cfg, now, alert_state={}, bypass_schedule=True,
        )

        assert len(matches) == 1
        assert matches[0]["value"] == -60000.0, (
            "the ordinary metric leaf must resolve the REAL pnl metric, "
            "not the unrelated label bound to the same bare name"
        )


class TestLeafFailsClosedOnUnresolvedBinding:
    """Item (b) — a leaf referencing a label whose binding failed to
    resolve (upstream metric missing/None, or never bound at all)
    produces a clean no-match. Never an exception, never a false
    match."""

    def test_metric_position_unresolved_binding_is_no_match(self):
        from backend.api.algo.agent_engine import _cycle_evaluate_agent, _v2_cfg

        now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        condition = {"metric": {"$given": "x"}, "scope": "preamble", "op": "<", "value": 0}
        context = {
            # Two accounts — pnl@positions.any_acct selects a per-row
            # scope with > 1 row this tick, the existing Phase-1
            # limitation that leaves the binding unresolved.
            "sum_positions": pd.DataFrame([
                {"account": "ACC1", "pnl": 10.0},
                {"account": "ACC2", "pnl": 20.0},
            ]),
            "sum_holdings": pd.DataFrame(),
            "df_margins": pd.DataFrame(),
        }
        cfg = _v2_cfg()
        agent = _Agent(id=23, slug="unresolved-metric-agent", given=[
            {"name": "x", "expr": "pnl@positions.any_acct"},
        ])
        agent.conditions = condition

        # Must not raise.
        matches, _obs = _cycle_evaluate_agent(
            agent, context, cfg, now, alert_state={}, bypass_schedule=True,
        )
        assert matches == []

    def test_value_position_unresolved_binding_is_no_match(self):
        from backend.api.algo.agent_engine import _cycle_evaluate_agent, _v2_cfg

        now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        condition = {
            "metric": "pnl", "scope": "positions.total", "op": "<",
            "value": {"$given": "gap"},
        }
        context = {
            "sum_positions": pd.DataFrame([{"account": "TOTAL", "pnl": -60000.0}]),
            "sum_holdings": pd.DataFrame(),
            "df_margins": pd.DataFrame(),
        }
        cfg = _v2_cfg()
        # 'gap' depends on a per-row scope that never resolves -> 'gap'
        # itself ends up absent from the bindings dict this tick.
        agent = _Agent(id=24, slug="unresolved-value-agent", given=[
            {"name": "x", "expr": "pnl@positions.any_acct"},
            {"name": "gap", "expr": "x - 1"},
        ])
        agent.conditions = condition

        matches, _obs = _cycle_evaluate_agent(
            agent, context, cfg, now, alert_state={}, bypass_schedule=True,
        )
        assert matches == []

    def test_reference_to_a_name_never_bound_at_all_is_no_match(self):
        """A leaf referencing a label this agent's statement never
        declares at all (e.g. a saved agent whose `given` was edited
        out from under a still-saved leaf) — `ctx.given_bindings` is
        simply empty, never raises."""
        from backend.api.algo.agent_evaluator import evaluate as v2_evaluate, Context

        v2_ctx = Context(given_bindings={}, now=datetime.now(timezone.utc))
        condition = {"metric": {"$given": "never_bound"}, "scope": "preamble",
                     "op": "<", "value": 0}
        assert v2_evaluate(condition, v2_ctx) == []

    def test_malformed_marker_logs_and_returns_no_match(self):
        from backend.api.algo.agent_evaluator import evaluate as v2_evaluate, Context

        v2_ctx = Context(given_bindings={"gap": -1.0}, now=datetime.now(timezone.utc))
        condition = {"metric": {"$given": ""}, "scope": "preamble", "op": "<", "value": 0}
        assert v2_evaluate(condition, v2_ctx) == []


class TestLegacyAgentRegressionUnaffected:
    """Item (c) — an agent with zero bound-label usage anywhere in its
    condition tree is byte-for-byte unaffected by this feature. Proves
    the change is purely additive, never a reinterpretation of the
    existing flat-leaf evaluation path."""

    def test_plain_leaf_matches_identically_with_default_empty_given_bindings(self):
        """`Context()` built with NO `given_bindings` kwarg at all (every
        pre-existing call site in this codebase) must behave exactly as
        it did before this feature — the new field defaults to `{}` and
        is never consulted by an ordinary string-metric leaf."""
        from backend.api.algo.agent_evaluator import evaluate as v2_evaluate, Context

        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        condition = {"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -1}
        matches = v2_evaluate(condition, v2_ctx)
        assert len(matches) == 1
        assert matches[0]["metric"] == "pnl"
        assert matches[0]["value"] == -60000.0
        assert matches[0]["account"] == "TOTAL"
        # given_bindings defaults to an empty dict, never None, never
        # consulted for a plain-string-metric leaf.
        assert v2_ctx.given_bindings == {}

    def test_agent_with_no_given_at_all_and_no_marker_leaves_fires_normally_via_real_entrypoint(self):
        from backend.api.algo.agent_engine import _cycle_evaluate_agent, _v2_cfg

        now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        condition = {"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -1}
        context = {
            "sum_positions": pd.DataFrame([{"account": "TOTAL", "pnl": -60000.0}]),
            "sum_holdings": pd.DataFrame(),
            "df_margins": pd.DataFrame(),
        }
        cfg = _v2_cfg()
        agent = _Agent(id=25, slug="no-given-agent", given=[])
        agent.conditions = condition

        matches, _obs = _cycle_evaluate_agent(
            agent, context, cfg, now, alert_state={}, bypass_schedule=True,
        )
        assert len(matches) == 1
        assert matches[0]["value"] == -60000.0
        assert "given" not in context, (
            "an agent with no bound labels must not create the 'given' "
            "key at all — unchanged from before this feature"
        )


# ═══════════════════════════════════════════════════════════════════════
#  Latch-key hashability and the real fire/cooldown/recovery pipeline
# ═══════════════════════════════════════════════════════════════════════

class TestLatchKeyIntegration:
    """A match whose 'metric' field were ever a dict (not the plain bound
    name) would make `_latch_key`'s returned tuple unhashable, crashing
    `_V2_LATCH[...] = ...` and aborting evaluation for every agent still
    queued in that tick's `run_cycle` loop (no surrounding try/except
    there). Drives the real fire/cooldown/recovery pipeline, not just
    `_eval_leaf`, to prove this never happens."""

    def test_given_match_latches_fires_once_then_cooldown_gates_a_repeat(self):
        from backend.api.algo.agent_engine import (
            _latch_key, _v2_apply_recovery, _v2_apply_escalation_gate,
        )

        agent = _Agent(id=26, slug="latch-gap-agent")
        store: dict = {}
        now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        match = {
            "metric": "gap", "scope": "preamble", "op": "<",
            "threshold": 0, "value": -59000.0, "row": {},
            "account": "TOTAL", "underlying": None,
        }

        # First tick — no latch yet, must fire. Must not raise on an
        # unhashable key.
        _v2_apply_recovery(agent, [{**match, "fired": True}], store=store)
        effective = _v2_apply_escalation_gate(agent, [match], now, cooldown_min=30, store=store)
        assert effective == [match]
        key = _latch_key(agent.slug, match)
        assert key in store, "latch key must be hashable and stored"
        assert key == ("latch-gap-agent", "gap", "preamble", "TOTAL")

        # Same tick's value again, 5 minutes later — within cooldown,
        # no escalation (zero-threshold leaf) -> gated, not re-fired.
        later = now + timedelta(minutes=5)
        effective_2 = _v2_apply_escalation_gate(agent, [match], later, cooldown_min=30, store=store)
        assert effective_2 == []

    def test_recovery_clears_the_latch_once_value_recovers_past_the_band(self):
        from backend.api.algo.agent_engine import (
            _v2_apply_recovery, _v2_apply_escalation_gate, _latch_key,
        )

        agent = _Agent(id=27, slug="latch-gap-recover-agent")
        store: dict = {}
        now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        breach = {
            "metric": "gap", "scope": "preamble", "op": "<",
            "threshold": -1000, "value": -5000.0, "row": {},
            "account": "TOTAL", "underlying": None,
        }
        _v2_apply_recovery(agent, [{**breach, "fired": True}], store=store)
        _v2_apply_escalation_gate(agent, [breach], now, cooldown_min=30, store=store)
        key = _latch_key(agent.slug, breach)
        assert key in store

        # Recovered observation: value now well above threshold, NOT
        # fired — recovery should clear the latch.
        recovered_obs = {**breach, "value": 500.0, "fired": False}
        _v2_apply_recovery(agent, [recovered_obs], store=store)
        assert key not in store


# ═══════════════════════════════════════════════════════════════════════
#  agent_evaluator.validate() — save-time dry-check for marker leaves
# ═══════════════════════════════════════════════════════════════════════

class TestValidateBoundLabelMarkerLeaves:
    def test_declared_label_in_metric_position_passes(self):
        from backend.api.algo.agent_evaluator import validate as v2_validate

        cond = {"metric": {"$given": "gap"}, "scope": "preamble", "op": "<", "value": 0}
        errors = v2_validate(cond, given_names={"gap"})
        assert errors == []

    def test_undeclared_label_in_metric_position_is_an_error(self):
        from backend.api.algo.agent_evaluator import validate as v2_validate

        cond = {"metric": {"$given": "typo_name"}, "scope": "preamble", "op": "<", "value": 0}
        errors = v2_validate(cond, given_names={"gap"})
        assert any("typo_name" in e for e in errors)

    def test_given_names_none_skips_existence_check(self):
        """The standalone /validate-condition endpoint has no agent/label
        context at all — given_names=None (the default) must not flag
        an otherwise-well-formed marker as an error."""
        from backend.api.algo.agent_evaluator import validate as v2_validate

        cond = {"metric": {"$given": "anything"}, "scope": "preamble", "op": "<", "value": 0}
        assert v2_validate(cond) == []

    def test_malformed_marker_is_an_error(self):
        from backend.api.algo.agent_evaluator import validate as v2_validate

        cond = {"metric": {"$given": ""}, "scope": "preamble", "op": "<", "value": 0}
        errors = v2_validate(cond, given_names={"gap"})
        assert any("malformed" in e for e in errors)

    def test_unknown_operator_on_a_marker_leaf_is_an_error(self):
        from backend.api.algo.agent_evaluator import validate as v2_validate

        cond = {"metric": {"$given": "gap"}, "scope": "preamble", "op": "???", "value": 0}
        errors = v2_validate(cond, given_names={"gap"})
        assert any("unknown operator" in e for e in errors)

    def test_marker_leaf_does_not_require_a_real_scope_token(self):
        """A marker-metric leaf's 'scope' field is unused by evaluation
        — validate() must not demand it resolve to a registered scope
        (or even be present at all)."""
        from backend.api.algo.agent_evaluator import validate as v2_validate

        cond = {"metric": {"$given": "gap"}, "op": "<", "value": 0}
        errors = v2_validate(cond, given_names={"gap"})
        assert errors == []

    def test_undeclared_label_in_value_position_against_an_ordinary_metric_is_an_error(self):
        from backend.api.algo.agent_evaluator import validate as v2_validate

        cond = {"metric": "pnl", "scope": "positions.total", "op": "<",
                "value": {"$given": "typo_name"}}
        errors = v2_validate(cond, given_names={"gap"})
        assert any("typo_name" in e for e in errors)

    def test_ordinary_leaf_with_literal_value_is_unaffected_by_given_names_param(self):
        """Regression — a totally ordinary leaf (no marker anywhere)
        must validate exactly as before, whether or not given_names is
        supplied."""
        from backend.api.algo.agent_evaluator import validate as v2_validate

        cond = {"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -1}
        assert v2_validate(cond) == []
        assert v2_validate(cond, given_names=set()) == []


# ═══════════════════════════════════════════════════════════════════════
#  Dry-run visibility — routes/agents.py's bound-label values sibling field
# ═══════════════════════════════════════════════════════════════════════

class TestDryRunBoundLabelVisibility:
    def test_bound_label_values_view_reports_resolved_and_unresolved(self):
        from backend.api.routes.agents import _dry_run_bound_label_values

        agent = _Agent(id=30, slug="view-agent", given=[
            {"name": "P", "expr": "pnl@positions.total"},
            {"name": "Gap", "expr": "P + 1000"},
            {"name": "Unresolved", "expr": "x * 2"},
        ])
        # Only 'p'/'gap' resolved this tick (lowercased keys, matching
        # the real storage convention) — 'unresolved' is absent.
        resolved_this_tick = {"p": -60000.0, "gap": -59000.0}
        view = _dry_run_bound_label_values(agent, resolved_this_tick)
        # Reported under the label's OWN original typed case.
        assert view == {"P": -60000.0, "Gap": -59000.0, "Unresolved": None}

    def test_no_given_declared_returns_empty_dict(self):
        from backend.api.routes.agents import _dry_run_bound_label_values

        agent = _Agent(id=31, slug="no-view-agent", given=[])
        assert _dry_run_bound_label_values(agent, {}) == {}

    def test_build_dry_run_response_includes_bindings_field_and_resolves_leaf(self):
        """End-to-end through the real `_build_dry_run_response` builder
        — a WHEN leaf referencing a bound label fires, and the response
        carries the resolved value under the new 'bindings' sibling
        field, addressing the dry-run visibility requirement."""
        from backend.api.routes.agents import _build_dry_run_response

        now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
        agent = _Agent(id=32, slug="dry-run-gap-agent", given=[
            {"name": "p", "expr": "pnl@positions.total"},
            {"name": "gap", "expr": "p + 1000"},
        ])
        agent.conditions = {"metric": {"$given": "gap"}, "scope": "preamble",
                             "op": "<", "value": 0}
        agent.status = "active"
        agent.schedule = "always"
        agent.fire_at_time = None
        agent.blackout_windows = []
        agent.debounce_minutes = 0
        ctx = {
            "sum_positions": pd.DataFrame([{"account": "TOTAL", "pnl": -60000.0}]),
            "sum_holdings": pd.DataFrame(),
            "df_margins": pd.DataFrame(),
            "segments": [],
            "any_market_open": True,
        }

        resp = _build_dry_run_response(agent, "dry-run-gap-agent", ctx, now)

        assert resp["would_fire"] is True
        assert resp["bindings"] == {"p": -60000.0, "gap": -59000.0}
        # `match_count`/the length of `matches` reflects the real match
        # list regardless of `_flatten_dry_run_matches`'s own per-field
        # extraction (a separate, pre-existing issue — see handback —
        # unrelated to and out of scope for this feature).
        assert resp["match_count"] == 1
        assert len(resp["matches"]) == 1


# ═══════════════════════════════════════════════════════════════════════
#  Save-time existence check — _age_given_names / _age_validate_threshold_
#  conditions (routes/agents.py). A typo'd label reference must be
#  caught at save time, not silently save clean and never fire.
# ═══════════════════════════════════════════════════════════════════════

class TestAgeGivenNamesAndSaveTimeValidation:
    def test_age_given_names_lowercases_and_skips_malformed_entries(self):
        from backend.api.routes.agents import _age_given_names

        names = _age_given_names([
            {"name": "Gap", "expr": "p + 1000"},
            {"name": "p", "expr": "pnl@positions.total"},
            {"not_a_name_key": "x"},
            "not-even-a-dict",
        ])
        assert names == {"gap", "p"}

    def test_age_given_names_none_and_empty_both_yield_empty_set(self):
        from backend.api.routes.agents import _age_given_names

        assert _age_given_names(None) == set()
        assert _age_given_names([]) == set()

    def test_save_time_rejects_leaf_referencing_an_undeclared_label_even_when_given_is_none(self):
        """`given=None` means 'this statement declares zero bindings', not
        'skip the check' — a marker leaf referencing anything is flagged,
        since nothing is declared. Only the real `/validate-condition`
        endpoint (no agent row at all) gets the fully-permissive
        `given_names=None` behaviour, and it never calls this wrapper."""
        from backend.api.routes.agents import _age_validate_threshold_conditions
        from litestar.exceptions import HTTPException

        cond = {"metric": {"$given": "gap"}, "scope": "preamble", "op": "<", "value": 0}
        with pytest.raises(HTTPException) as exc_info:
            _age_validate_threshold_conditions(cond, None)
        assert "gap" in str(exc_info.value.detail)

    def test_save_time_accepts_leaf_referencing_a_declared_label(self):
        from backend.api.routes.agents import _age_validate_threshold_conditions

        cond = {"metric": {"$given": "gap"}, "scope": "preamble", "op": "<", "value": 0}
        given = [
            {"name": "p", "expr": "pnl@positions.total"},
            {"name": "gap", "expr": "p + 1000"},
        ]
        # Must not raise.
        _age_validate_threshold_conditions(cond, given)
