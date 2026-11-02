"""
DSL dataframe-access gaps, item 3 — generic row-set aggregates
(`count`, `sum(column)`, `max(column)`, `avg(column)`), plus the
architectural consequences this requires in `agent_engine.py`:
`_ae_is_aggregate_match` must classify a row-set-aggregate match as
aggregate REGARDLESS of scope suffix, and `$row` must fail closed on it.

Covers (per PLAN.md "Done when" + the backend-test adversarial list):
  1. `count@positions.any_acct(...)`, `sum(qty)@...`, `max(pnl)@...`,
     `avg(pnl)@...` each evaluate to ONE aggregate number, never a
     fan-out, regardless of the underlying scope's own suffix.
  2. Unknown/wrong-type column -> None (leaf skipped), not a crash.
  3. raw_rows-empty (scope produced nothing) -> None/no-match; a
     where/scope_filter-narrowed-to-empty set IS a real, confirmed
     zero for count/sum (and None/undefined for max/avg).
  4. `$row` rejected (fails closed, does not crash) on a leaf using a
     row-set-aggregate metric.
  5. A row-set-aggregate metric paired with an already-aggregate
     `.total` scope produces a degenerate single result, never crashes.
  6. `_parse_call_token`'s number-arg path is unchanged — `mean_pnl(30)`
     keeps working, `mean_pnl(0)` and `mean_pnl(qty)` keep being
     rejected (pinned against the string-arg extension regressing the
     pre-existing number-only behaviour).
  7. Regression: every ordinary (non-aggregate) metric is completely
     unaffected.
  8. The new match shape (`row={}`, `metric="sum(qty)"`, `account=
     'TOTAL'`) safely flows through the ALERT-RENDER path
     (`_v2_match_to_alertrow` -> `_tg_alert_body`/`_email_alert_body`,
     and `_v2_build_evalresult`) without raising — this shape has never
     gone through that path before this feature.
  9. A row-set aggregate GIVEN binding (`x = count@positions.
     expiring_today`) resolves to a real value instead of always
     hitting the pre-existing "unsupported per-row scope" rejection —
     `agent_engine._given_resolve_bare_metric_scope` has a SEPARATE
     `(ctx, row)`-calling caller that needed its own fix.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from datetime import datetime, timezone

import pandas as pd
import pytest

from backend.api.algo import grammar
from backend.api.algo.agent_engine import _ae_is_aggregate_match, _ae_fanout_matches
from backend.api.algo.agent_evaluator import Context, evaluate, validate
from backend.api.algo.actions import resolve_row_ref_params
from backend.api.algo.grammar_tokens.metric_pnl import _metric_pnl
from backend.api.algo.grammar_tokens.metric_count import _metric_count
from backend.api.algo.grammar_tokens.metric_sum import _metric_factory_sum
from backend.api.algo.grammar_tokens.metric_max import _metric_factory_max
from backend.api.algo.grammar_tokens.metric_avg import _metric_factory_avg
from backend.api.algo.grammar_tokens.metric_mean_pnl import _metric_factory_mean_pnl
from backend.api.algo.grammar_tokens.scope_positions_expiring_today import (
    _scope_positions_expiring_today,
)
from backend.api.algo.grammar_tokens.scope_positions_total import (
    _scope_positions_total,
)


_NOW_EXPIRY_DAY = datetime(2025, 4, 24, 9, 0, tzinfo=timezone.utc)


def _rows():
    return [
        {"account": "A", "tradingsymbol": "CRUDEOIL25APR6000CE", "exchange": "MCX",
         "quantity": 5, "pnl": -100.0},
        {"account": "A", "tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO",
         "quantity": 50, "pnl": -6000.0},
        {"account": "B", "tradingsymbol": "NIFTY25APR23000PE", "exchange": "NFO",
         "quantity": -50, "pnl": -3000.0},
    ]


@pytest.fixture(autouse=True)
def _wire_registry(monkeypatch):
    from backend.api.algo.grammar_registry import REGISTRY
    monkeypatch.setattr(REGISTRY, 'metrics', {
        'pnl': _metric_pnl, 'count': _metric_count,
    })
    monkeypatch.setattr(REGISTRY, 'scopes', {
        'positions.expiring_today': _scope_positions_expiring_today,
        'positions.total': _scope_positions_total,
    })
    monkeypatch.setattr(REGISTRY, 'operators', dict(grammar.OPERATORS))
    monkeypatch.setattr(REGISTRY, 'metric_factories', {
        'sum': (_metric_factory_sum, {'column': {'type': 'string'}}),
        'max': (_metric_factory_max, {'column': {'type': 'string'}}),
        'avg': (_metric_factory_avg, {'column': {'type': 'string'}}),
        'mean_pnl': (_metric_factory_mean_pnl, {'minutes': {'type': 'number'}}),
    })
    monkeypatch.setattr(REGISTRY, '_metric_call_cache', {})
    monkeypatch.setattr(REGISTRY, '_scope_call_cache', {})


# ═══════════════════════════════════════════════════════════════════════
#  1. Collapse to ONE result, never a fan-out
# ═══════════════════════════════════════════════════════════════════════

class TestCollapsesToOneResult:
    def test_count_collapses_to_one_match_on_a_fanning_out_scope(self):
        """`positions.expiring_today` normally returns N rows and would
        fan out N times for an ordinary metric — `count` must still
        collapse to exactly ONE match."""
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "count", "scope": "positions.expiring_today", "op": ">", "value": 1}
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["value"] == 3.0
        assert result[0]["rowset_aggregate"] is True
        assert result[0]["account"] == "TOTAL"

    def test_sum_qty_collapses_to_one_match(self):
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "sum(qty)", "scope": "positions.expiring_today",
                "op": ">", "value": 0}
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["value"] == 5.0  # 5 + 50 - 50

    def test_max_pnl_collapses_to_one_match(self):
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "max(pnl)", "scope": "positions.expiring_today",
                "op": ">", "value": -200}
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["value"] == -100.0

    def test_avg_pnl_collapses_to_one_match(self):
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "avg(pnl)", "scope": "positions.expiring_today",
                "op": "<", "value": -1000}
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["value"] == pytest.approx((-100.0 - 6000.0 - 3000.0) / 3)

    def test_ae_fanout_matches_also_collapses_rowset_aggregate_to_one(self):
        """Even if the agent-engine's own fan-out helper receives the
        aggregate match (e.g. via an `any[]` combining it with other
        leaves), it must stay a single execution — exercises the SAME
        `_ae_is_aggregate_match` classifier `_eval_leaf` tags matches
        for."""
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "count", "scope": "positions.expiring_today", "op": ">", "value": 1}
        matches = evaluate(leaf, ctx)
        fanout = _ae_fanout_matches(matches)
        assert len(fanout) == 1


# ═══════════════════════════════════════════════════════════════════════
#  2. Unknown/wrong-type column
# ═══════════════════════════════════════════════════════════════════════

class TestUnknownOrWrongTypeColumn:
    def test_validate_rejects_unknown_aggregate_column_at_save_time(self):
        """Save-time coverage for the SAME check item 2's where-filter
        already gets — an unknown/wrong-type `column=` argument on an
        aggregate metric must be rejected by validate() too, not only
        surface as a runtime warning every tick on an MCP/API-authored
        agent that bypasses frontend compile-time checks."""
        cond = {
            "metric": "sum(bogus_col)", "scope": "positions.expiring_today",
            "op": ">", "value": -1,
        }
        errors = validate(cond)
        assert len(errors) == 1
        assert "bogus_col" in errors[0]

    def test_validate_rejects_string_typed_aggregate_column(self):
        cond = {
            "metric": "sum(symbol)", "scope": "positions.expiring_today",
            "op": ">", "value": -1,
        }
        errors = validate(cond)
        assert len(errors) == 1
        assert "symbol" in errors[0]

    def test_validate_accepts_valid_aggregate_column(self):
        cond = {
            "metric": "sum(qty)", "scope": "positions.expiring_today",
            "op": ">", "value": -1,
        }
        assert validate(cond) == []

    def test_unknown_column_returns_no_match_not_a_crash(self, monkeypatch):
        import backend.api.algo.grammar_tokens.metric_sum as sum_mod
        warnings = []
        monkeypatch.setattr(sum_mod.logger, 'warning', lambda msg: warnings.append(msg))
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "sum(bogus_col)", "scope": "positions.expiring_today",
                "op": ">", "value": -1}
        assert evaluate(leaf, ctx) == []
        assert any("bogus_col" in w for w in warnings)

    def test_string_typed_column_rejected_for_sum(self):
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "sum(symbol)", "scope": "positions.expiring_today",
                "op": ">", "value": -1}
        # 'symbol' is a real exposed column but type='string' — sum/max/avg
        # only accept type='number'.
        assert evaluate(leaf, ctx) == []


# ═══════════════════════════════════════════════════════════════════════
#  3. raw_rows-empty vs filtered-to-empty
# ═══════════════════════════════════════════════════════════════════════

class TestEmptyRowsSemantics:
    def test_scope_returns_nothing_this_tick_no_match(self):
        """No positions at all this tick -> no match, same as any other
        leaf's "nothing to iterate" default — never a false 0."""
        ctx = Context(position_rows=[], now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "count", "scope": "positions.expiring_today", "op": ">=", "value": 0}
        assert evaluate(leaf, ctx) == []

    def test_where_filter_narrows_everything_away_count_is_a_real_zero(self):
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "count", "scope": "positions.expiring_today",
            "op": "==", "value": 0,
            "where": {"column": "exchange", "op": "==", "value": "BSE"},  # matches nothing
        }
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["value"] == 0.0

    def test_where_filter_narrows_everything_away_max_is_none_no_match(self):
        """max()/avg() of a confirmed-empty filtered set is undefined —
        None, never a coerced 0 — so the leaf simply doesn't fire."""
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "max(pnl)", "scope": "positions.expiring_today",
            "op": ">=", "value": -999999,
            "where": {"column": "exchange", "op": "==", "value": "BSE"},
        }
        assert evaluate(leaf, ctx) == []


# ═══════════════════════════════════════════════════════════════════════
#  4. `$row` fails closed on a row-set aggregate match
# ═══════════════════════════════════════════════════════════════════════

class TestRowRefFailsClosedOnAggregate:
    def test_matched_row_is_none_for_rowset_aggregate_match(self):
        """Mirrors `agent_engine._ae_dispatch_actions`'s own
        `is_aggregate` gate: a row-set-aggregate match's `row` is never
        bound for a `$row` DO-action param."""
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "count", "scope": "positions.expiring_today", "op": ">", "value": 1}
        (match,) = evaluate(leaf, ctx)
        assert _ae_is_aggregate_match(match) is True

        action_ctx = {
            "_matched_row": None if _ae_is_aggregate_match(match) else match.get('row'),
            "_matched_row_scope": None if _ae_is_aggregate_match(match) else match.get('scope'),
        }
        params = {"symbol": {"$row": "symbol"}}
        resolved, error = resolve_row_ref_params(params, action_ctx)
        assert error is not None
        assert resolved == params  # unresolved — original params returned unchanged

    def test_ordinary_per_row_match_is_not_aggregate(self):
        """Sanity check: a normal per-row match from the SAME scope is
        NOT classified as aggregate, so $row resolution would proceed
        normally for it (regression guard on the classifier itself)."""
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "pnl", "scope": "positions.expiring_today", "op": "<=", "value": -1}
        matches = evaluate(leaf, ctx)
        assert matches
        assert all(_ae_is_aggregate_match(m) is False for m in matches)


# ═══════════════════════════════════════════════════════════════════════
#  5. Aggregate metric + `.total` scope — degenerate, never crashes
# ═══════════════════════════════════════════════════════════════════════

class TestAggregateMetricOnTotalScope:
    def test_count_on_total_scope_is_degenerate_but_safe(self):
        """`positions.total` already returns exactly one (TOTAL-scoped)
        row per account-summary frame — `count@positions.total` then
        trivially reduces over that single-row list. Degenerate (always
        0 or 1), but must not crash, and must still classify as
        aggregate via the pre-existing `.total`-suffix check."""
        df = pd.DataFrame([{"account": "TOTAL", "pnl": -1000.0}])
        ctx = Context(sum_positions=df)
        leaf = {"metric": "count", "scope": "positions.total", "op": "==", "value": 1}
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["value"] == 1.0
        assert _ae_is_aggregate_match(result[0]) is True


# ═══════════════════════════════════════════════════════════════════════
#  6. `_parse_call_token` number-arg path unchanged
# ═══════════════════════════════════════════════════════════════════════

class TestParseCallTokenNumberArgUnchanged:
    def test_mean_pnl_30_still_resolves(self):
        from backend.api.algo.grammar_registry import REGISTRY
        fn = REGISTRY.metric("mean_pnl(30)")
        assert fn is not None
        assert not getattr(fn, 'rowset_aggregate', False)

    def test_mean_pnl_zero_still_rejected(self):
        from backend.api.algo.grammar_registry import REGISTRY
        assert REGISTRY.metric("mean_pnl(0)") is None

    def test_mean_pnl_bare_name_arg_still_rejected(self):
        """A bare identifier is now ACCEPTED for a 'string'-typed param
        (sum/max/avg's `column`) but must stay REJECTED for a
        'number'-typed param like mean_pnl's `minutes` — the two arg
        kinds are not interchangeable."""
        from backend.api.algo.grammar_registry import REGISTRY
        assert REGISTRY.metric("mean_pnl(qty)") is None

    def test_sum_bare_identifier_arg_resolves(self):
        from backend.api.algo.grammar_registry import REGISTRY
        fn = REGISTRY.metric("sum(qty)")
        assert fn is not None
        assert getattr(fn, 'rowset_aggregate', False) is True

    def test_sum_quoted_string_arg_resolves_identically(self):
        from backend.api.algo.grammar_registry import REGISTRY
        fn1 = REGISTRY.metric("sum(qty)")
        fn2 = REGISTRY.metric('sum("qty")')
        assert fn1 is not None and fn2 is not None
        rows = _rows()
        assert fn1(None, rows, "positions.expiring_today") == fn2(None, rows, "positions.expiring_today")

    def test_sum_with_keyword_form_not_yet_accepted(self):
        """Confirmed against the frontend's real compiler
        (`agents.js:_compileMetricCallString`): the DSL surface syntax
        `sum(column=qty)` is keyword-shaped for operator readability,
        but it compiles down to the bare POSITIONAL call string
        `"sum(qty)"` before it ever reaches the backend — the wire
        contract never carries a keyword. `node.keywords` is rejected
        outright by `_parse_call_token`, and that is correct: no caller
        anywhere in the real pipeline ever needs it to accept one."""
        from backend.api.algo.grammar_registry import REGISTRY
        assert REGISTRY.metric("sum(column=qty)") is None


# ═══════════════════════════════════════════════════════════════════════
#  7. Regression — ordinary metrics unaffected
# ═══════════════════════════════════════════════════════════════════════

class TestOrdinaryMetricsUnaffected:
    def test_pnl_metric_still_fans_out_per_row(self):
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "pnl", "scope": "positions.expiring_today", "op": "<=", "value": -1}
        result = evaluate(leaf, ctx)
        assert len(result) == 3
        assert all('rowset_aggregate' not in m for m in result)


# ═══════════════════════════════════════════════════════════════════════
#  8. Aggregate match safely flows through the alert-render path
# ═══════════════════════════════════════════════════════════════════════

class TestAggregateMatchFlowsThroughAlertRender:
    def test_match_to_alertrow_and_bodies_do_not_raise(self):
        from backend.api.algo.agent_engine import _v2_match_to_alertrow
        from backend.shared.helpers.alert_utils import _tg_alert_body, _email_alert_body

        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "sum(qty)", "scope": "positions.expiring_today",
                "op": ">", "value": 0}
        (match,) = evaluate(leaf, ctx)
        assert match["row"] == {}
        assert match["account"] == "TOTAL"

        row = _v2_match_to_alertrow(match)
        assert row["scope"] == "TOTAL"
        assert row["section"] == "Positions"

        tg_text = _tg_alert_body([row])
        email_html = _email_alert_body([row])
        assert isinstance(tg_text, str) and tg_text
        assert isinstance(email_html, str) and email_html

    def test_build_evalresult_does_not_raise(self):
        from backend.api.algo.agent_engine import _v2_build_evalresult

        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "count", "scope": "positions.expiring_today", "op": ">", "value": 1}
        matches = evaluate(leaf, ctx)
        result = _v2_build_evalresult(matches, "Test Agent")
        assert result.triggered is True
        assert "count" in result.condition_text


# ═══════════════════════════════════════════════════════════════════════
#  9. Row-set aggregate GIVEN bindings (_given_resolve_bare_metric_scope)
# ═══════════════════════════════════════════════════════════════════════

class TestGivenBindingRowsetAggregate:
    def test_count_given_binding_resolves_over_multiple_rows(self):
        """Before the fix, ANY multi-row scope hit the pre-existing
        `len(rows) > 1` "unsupported per-row scope" rejection before
        `metric_fn` was ever called — a row-set aggregate must bypass
        that check (it is MEANT to reduce over several rows)."""
        from backend.api.algo.agent_engine import _given_resolve_bare_metric_scope, _given_binding_bare_metric_scope
        from backend.api.algo.grammar_registry import REGISTRY

        match_ = _given_binding_bare_metric_scope("count@positions.expiring_today")
        assert match_ is not None
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        val = _given_resolve_bare_metric_scope(match_, ctx, "test-agent")
        assert val == 3.0

    def test_call_syntax_aggregate_does_not_match_bare_alias_regex(self):
        """Pre-existing, unrelated-to-this-fix limitation: the bare-alias
        GIVEN regex (`_GIVEN_BARE_METRIC_SCOPE_RE`) only ever matched a
        plain identifier metric token, never call syntax — true for
        `mean_pnl(30)@positions.total` long before this feature existed,
        and equally true for `sum(qty)@...`. Not a regression this fix
        introduces; documented here so the `count` case right above
        (which DOES match, since `count` is parenthesis-free) isn't
        mistaken for "all aggregates work via this path"."""
        from backend.api.algo.agent_engine import _given_binding_bare_metric_scope
        assert _given_binding_bare_metric_scope("sum(qty)@positions.expiring_today") is None

    def test_aggregate_given_binding_with_no_rows_resolves_to_none(self):
        from backend.api.algo.agent_engine import _given_resolve_bare_metric_scope, _given_binding_bare_metric_scope

        match_ = _given_binding_bare_metric_scope("count@positions.expiring_today")
        ctx = Context(position_rows=[], now=_NOW_EXPIRY_DAY)
        val = _given_resolve_bare_metric_scope(match_, ctx, "test-agent")
        assert val is None

    def test_ordinary_metric_given_binding_on_multirow_scope_still_unsupported(self):
        """Regression — a NON-aggregate metric referencing a scope that
        selects >1 row must still hit the pre-existing rejection
        unchanged (this behaviour predates this feature)."""
        from backend.api.algo.agent_engine import (
            _given_resolve_bare_metric_scope, _given_binding_bare_metric_scope, _GIVEN_UNRESOLVED,
        )
        match_ = _given_binding_bare_metric_scope("pnl@positions.expiring_today")
        ctx = Context(position_rows=_rows(), now=_NOW_EXPIRY_DAY)
        val = _given_resolve_bare_metric_scope(match_, ctx, "test-agent")
        assert val is _GIVEN_UNRESOLVED
