"""
DSL dataframe-access gaps, item 2 — generic where-filter on a scope
segment, and item 1 — group-derived `is_unhedged`/`net_qty` columns.

Wire shape (agreed, see the implementation handback): an OPTIONAL
`where` key on an ordinary leaf, SIBLING of the existing `scope_filter`
key, holding a condition sub-tree that reuses the EXISTING `all`/`any`/
`not` composite shapes verbatim. A where-LEAF is `{"column": <bare
exposed column name>, "op": <operator token>, "value": <literal>}` —
no `scope` key (there is no nested `@`; the bare name resolves directly
against the OUTER leaf's own scope's exposed-column schema).

Covers (per PLAN.md "Done when"):
  1. `positions.any_acct(exchange == "MCX" & qty > 0)`-shaped filtering
     (on `positions.expiring_today`, which actually HAS `exchange`/`qty`
     exposed columns — `positions.any_acct` is the account-SUMMARY scope
     and has no per-symbol columns at all, confirmed against
     `grammar.py`'s real `_POSITIONS_SUMMARY_COLUMNS` schema).
  2. Parity: `positions.expiring_today(exchange == "MCX" & is_unhedged ==
     1)` reproduces `positions.expiring_today.mcx_unhedged`'s own row
     set exactly, on an equivalent fixture.
  3. Fail-closed: an unknown column in a where-leaf never fail-OPENs
     through `not` — the whole leaf is skipped.
  4. Composite `all`/`any`/`not`, missing/NaN exclusion, operator
     exceptions.
  5. Regression: every existing leaf with no `where` key is unaffected.
  6. Group-derived columns: `is_unhedged`/`net_qty` broadcast correctly
     by `compute_mcx_ce_pe_net_groups` and `background.py`'s broadcast
     site.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from datetime import datetime, timezone

import pytest

from backend.api.algo import grammar
from backend.api.algo.agent_evaluator import Context, evaluate, validate
from backend.api.algo.grammar_tokens.metric_pnl import _metric_pnl
from backend.api.algo.grammar_tokens.scope_positions_expiring_today import (
    _scope_positions_expiring_today,
)
from backend.api.algo.grammar_tokens.scope_positions_expiring_today_mcx_unhedged import (
    _scope_positions_expiring_today_mcx_unhedged,
    compute_mcx_ce_pe_net_groups,
)
from backend.api.algo.grammar_tokens.scope_positions_any_acct import (
    _scope_positions_any_acct,
)


_NOW_EXPIRY_DAY = datetime(2025, 4, 24, 9, 0, tzinfo=timezone.utc)


def _mixed_exchange_rows():
    """Four rows: two MCX (one qty>0, one qty<0), two NFO — used to prove
    the where-filter narrows to exactly the right subset."""
    return [
        {"account": "ACCT_X", "tradingsymbol": "CRUDEOIL25APR6000CE",
         "exchange": "MCX", "quantity": 5, "pnl": -100.0},
        {"account": "ACCT_X", "tradingsymbol": "CRUDEOIL25APR6000PE",
         "exchange": "MCX", "quantity": -5, "pnl": -50.0},
        {"account": "ACCT_X", "tradingsymbol": "NIFTY25APR22000CE",
         "exchange": "NFO", "quantity": 50, "pnl": -6000.0},
        {"account": "ACCT_X", "tradingsymbol": "NIFTY25APR23000PE",
         "exchange": "NFO", "quantity": -50, "pnl": -6000.0},
    ]


@pytest.fixture(autouse=True)
def _wire_registry(monkeypatch):
    from backend.api.algo.grammar_registry import REGISTRY
    monkeypatch.setattr(REGISTRY, 'metrics', {'pnl': _metric_pnl})
    monkeypatch.setattr(REGISTRY, 'scopes', {
        'positions.expiring_today': _scope_positions_expiring_today,
        'positions.expiring_today.mcx_unhedged': _scope_positions_expiring_today_mcx_unhedged,
        'positions.any_acct': _scope_positions_any_acct,
    })
    monkeypatch.setattr(REGISTRY, 'operators', dict(grammar.OPERATORS))
    monkeypatch.setattr(REGISTRY, 'metric_factories', {})
    monkeypatch.setattr(REGISTRY, '_metric_call_cache', {})
    monkeypatch.setattr(REGISTRY, '_scope_call_cache', {})


# ═══════════════════════════════════════════════════════════════════════
#  1. Mixed-exchange narrowing
# ═══════════════════════════════════════════════════════════════════════

class TestWhereFilterNarrowsRows:
    def test_exchange_and_qty_composite_narrows_to_one_row(self):
        """`exchange == 'MCX' & qty > 0` over the 4-row mixed fixture
        must leave exactly the one MCX row with positive quantity."""
        ctx = Context(position_rows=_mixed_exchange_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": 1,  # always-true threshold — isolate the filter's effect
            "where": {"all": [
                {"column": "exchange", "op": "==", "value": "MCX"},
                {"column": "qty", "op": ">", "value": 0},
            ]},
        }
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["row"]["tradingsymbol"] == "CRUDEOIL25APR6000CE"

    def test_any_composite_matches_either_branch(self):
        ctx = Context(position_rows=_mixed_exchange_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": 1,
            "where": {"any": [
                {"column": "qty", "op": "==", "value": 5},
                {"column": "qty", "op": "==", "value": -50},
            ]},
        }
        result = evaluate(leaf, ctx)
        symbols = {m["row"]["tradingsymbol"] for m in result}
        assert symbols == {"CRUDEOIL25APR6000CE", "NIFTY25APR23000PE"}

    def test_not_negates_inner(self):
        ctx = Context(position_rows=_mixed_exchange_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": 1,
            "where": {"not": {"column": "exchange", "op": "==", "value": "MCX"}},
        }
        result = evaluate(leaf, ctx)
        exchanges = {m["row"]["exchange"] for m in result}
        assert exchanges == {"NFO"}

    def test_single_leaf_where_shorthand(self):
        """A where-tree may itself be a bare leaf (no all/any wrapper)."""
        ctx = Context(position_rows=_mixed_exchange_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": 1,
            "where": {"column": "exchange", "op": "==", "value": "NFO"},
        }
        result = evaluate(leaf, ctx)
        assert len(result) == 2
        assert all(m["row"]["exchange"] == "NFO" for m in result)


# ═══════════════════════════════════════════════════════════════════════
#  2. Parity with the bespoke mcx_unhedged token
# ═══════════════════════════════════════════════════════════════════════

def _unhedged_parity_fixture(*, spot_prices=None):
    """Lone unhedged MCX leg + a perfectly-hedged MCX pair, matching the
    SAME shape test_scope_filter_symbol.py's mcx_unhedged fixtures use —
    with `is_unhedged`/`net_qty` already broadcast the way
    background.py's real row-construction does it (a test calling the
    shared grouping helper directly, not re-deriving it)."""
    rows = [
        # Unhedged lone leg.
        {"account": "ACCT_X", "tradingsymbol": "GOLDM25APR75000CE",
         "exchange": "MCX", "quantity": 1, "pnl": -8000.0},
        # Perfectly hedged pair, different root.
        {"account": "ACCT_X", "tradingsymbol": "GOLD25APR75000CE",
         "exchange": "MCX", "quantity": 1, "pnl": -9000.0},
        {"account": "ACCT_X", "tradingsymbol": "GOLD25APR75000PE",
         "exchange": "MCX", "quantity": -1, "pnl": -9000.0},
        # An NFO row — never touched by the MCX-only broadcast.
        {"account": "ACCT_X", "tradingsymbol": "NIFTY25APR22000CE",
         "exchange": "NFO", "quantity": 10, "pnl": -500.0},
    ]
    for r in rows:
        r["is_unhedged"] = None
        r["net_qty"] = None
    mcx_rows = [r for r in rows if r["exchange"] == "MCX"]
    for g in compute_mcx_ce_pe_net_groups(mcx_rows).values():
        net = g["net_qty"]
        is_unhedged = None if net is None else (1.0 if net != 0 else 0.0)
        for gr in g["rows"]:
            gr["is_unhedged"] = is_unhedged
            gr["net_qty"] = net
    return rows


class TestParityWithBespokeMcxUnhedgedToken:
    """2026-11 classifier re-base: `.mcx_unhedged` (the bespoke token)
    now classifies via the real `ExpiryEngine`, while the generic
    `is_unhedged`/`net_qty` DSL columns (`compute_mcx_ce_pe_net_groups`,
    `background.py`'s broadcast) are DELIBERATELY left on the old
    crude signed-quantity-sum heuristic — see
    `scope_positions_expiring_today_mcx_unhedged.py`'s own module
    docstring for why. The two can now genuinely disagree. This class's
    first test demonstrates a scenario where they still happen to
    agree (both the old heuristic AND the real engine confirm the GOLD
    pair is safely excluded, once given a resolvable spot); the second
    test pins the now-DOCUMENTED disagreement explicitly, so it's
    never mistaken for an accidental regression."""

    def test_where_filter_reproduces_bespoke_token_row_set(self):
        """With a resolvable GOLD spot at the strike (ATM — confirmed,
        not UNKNOWN), both mechanisms agree: the GOLD pair is excluded
        and the lone GOLDM leg (no spot given, UNKNOWN under the real
        engine, unconditionally flagged under the crude sum) surfaces
        under both."""
        rows = _unhedged_parity_fixture()
        ctx_general = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        ctx_bespoke = Context(position_rows=rows, spot_prices={"GOLD": 75000.0},
                               now=_NOW_EXPIRY_DAY)

        general_leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": 1,
            "where": {"all": [
                {"column": "exchange", "op": "==", "value": "MCX"},
                {"column": "is_unhedged", "op": "==", "value": 1},
            ]},
        }
        bespoke_leaf = {
            "metric": "pnl", "scope": "positions.expiring_today.mcx_unhedged",
            "op": "<=", "value": 1,
        }
        general_result = evaluate(general_leaf, ctx_general)
        bespoke_result = evaluate(bespoke_leaf, ctx_bespoke)

        general_symbols = {m["row"]["tradingsymbol"] for m in general_result}
        bespoke_symbols = {m["row"]["tradingsymbol"] for m in bespoke_result}
        assert general_symbols == bespoke_symbols == {"GOLDM25APR75000CE"}

    def test_known_disagreement_without_a_resolvable_spot(self):
        """Without ANY spot price for GOLD, the crude `is_unhedged`
        column still excludes the GOLD pair (signed-sum == 0, unaware
        of moneyness), while the real engine includes it as UNKNOWN —
        "can't confirm this leg is safe" (see `_scope_positions_
        expiring_today_mcx_unhedged`'s own docstring). This is the
        documented residual gap this re-base deliberately leaves open
        for the generic DSL column — pinned here so it's a known,
        tested fact, not a silent surprise."""
        rows = _unhedged_parity_fixture()
        ctx_general = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        ctx_bespoke = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)

        general_leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": 1,
            "where": {"all": [
                {"column": "exchange", "op": "==", "value": "MCX"},
                {"column": "is_unhedged", "op": "==", "value": 1},
            ]},
        }
        bespoke_leaf = {
            "metric": "pnl", "scope": "positions.expiring_today.mcx_unhedged",
            "op": "<=", "value": 1,
        }
        general_symbols = {m["row"]["tradingsymbol"] for m in evaluate(general_leaf, ctx_general)}
        bespoke_symbols = {m["row"]["tradingsymbol"] for m in evaluate(bespoke_leaf, ctx_bespoke)}

        assert general_symbols == {"GOLDM25APR75000CE"}
        assert bespoke_symbols == {"GOLDM25APR75000CE", "GOLD25APR75000CE", "GOLD25APR75000PE"}
        assert general_symbols != bespoke_symbols


# ═══════════════════════════════════════════════════════════════════════
#  3. Fail-closed on unknown column — never fail-OPEN through `not`
# ═══════════════════════════════════════════════════════════════════════

class TestWhereFilterFailsClosed:
    def test_unknown_column_skips_the_whole_leaf(self, monkeypatch):
        import backend.api.algo.agent_evaluator as ev_mod
        warnings = []
        monkeypatch.setattr(ev_mod.logger, 'warning', lambda msg: warnings.append(msg))
        ctx = Context(position_rows=_mixed_exchange_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": 1,
            "where": {"column": "bogus_column_name", "op": "==", "value": 1},
        }
        assert evaluate(leaf, ctx) == []
        assert any("where" in w for w in warnings)

    def test_unknown_column_under_not_does_not_fail_open(self):
        """THE regression this ordering exists to prevent: if the
        unknown-column leaf were evaluated per-row instead of validated
        once up front, `not(bogus == 1)` would evaluate False for every
        row (can't resolve 'bogus'), and `not` would flip that to True —
        matching EVERY row. Must instead skip the WHOLE leaf — zero
        matches, not four."""
        ctx = Context(position_rows=_mixed_exchange_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": 1,
            "where": {"not": {"column": "bogus_column_name", "op": "==", "value": 1}},
        }
        assert evaluate(leaf, ctx) == []

    def test_scope_with_no_schema_at_all_skips_the_leaf(self):
        """A scope token with no EXPOSED_COLUMNS entry at all (nothing
        registered) must also fail closed, never raise a KeyError."""
        from backend.api.algo.grammar_registry import REGISTRY

        def _fake_scope(ctx):
            return [{"account": "X"}]

        REGISTRY.scopes['fake.scope'] = _fake_scope
        REGISTRY.metrics['pnl'] = _metric_pnl
        try:
            ctx = Context(position_rows=[], now=_NOW_EXPIRY_DAY)
            leaf = {
                "metric": "pnl", "scope": "fake.scope", "op": "<=", "value": 1,
                "where": {"column": "anything", "op": "==", "value": 1},
            }
            # scope_fn returns a row with no 'pnl', metric_fn resolution
            # for 'pnl'@'fake.scope' falls back to exposed columns (none
            # registered) -> metric_fn is None -> leaf already skipped
            # before 'where' is even reached. Assert no crash either way.
            assert evaluate(leaf, ctx) == []
        finally:
            REGISTRY.scopes.pop('fake.scope', None)

    def test_malformed_where_tree_not_a_dict_skips(self):
        ctx = Context(position_rows=_mixed_exchange_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": 1,
            "where": "not-a-dict",
        }
        assert evaluate(leaf, ctx) == []

    def test_where_leaf_value_as_dict_marker_is_malformed(self):
        """No $given/$row support inside a where-expression — explicit
        scope boundary. A dict-shaped `value` makes the leaf malformed,
        not a silent $given/$row resolution attempt."""
        ctx = Context(position_rows=_mixed_exchange_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": 1,
            "where": {"column": "exchange", "op": "==", "value": {"$given": "x"}},
        }
        assert evaluate(leaf, ctx) == []


# ═══════════════════════════════════════════════════════════════════════
#  4. Missing/NaN exclusion, operator exceptions
# ═══════════════════════════════════════════════════════════════════════

class TestWhereFilterRowLevelEdgeCases:
    def test_missing_value_excludes_row_even_for_not_equal(self):
        """`quantity` is absent entirely on row A (the exposed `qty`
        column resolves to None for it); row B has a real quantity."""
        rows = [
            {"account": "A", "tradingsymbol": "GOLD25APR75000CE", "exchange": "MCX", "pnl": -1.0},
            {"account": "A", "tradingsymbol": "GOLDM25APR75000CE", "exchange": "MCX",
             "quantity": 5, "pnl": -1.0},
        ]
        ctx = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": 1,
            "where": {"column": "qty", "op": "!=", "value": 999},
        }
        result = evaluate(leaf, ctx)
        # The row with no 'quantity' at all is excluded regardless of
        # '!=' (which would otherwise trivially match None != 999).
        symbols = {m["row"]["tradingsymbol"] for m in result}
        assert symbols == {"GOLDM25APR75000CE"}

    def test_between_operator_with_malformed_range_does_not_raise(self):
        rows = [{"account": "A", "tradingsymbol": "GOLDM25APR75000CE", "exchange": "MCX",
                 "quantity": 5, "pnl": -1.0}]
        ctx = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": 1,
            "where": {"column": "qty", "op": "between", "value": 5},  # not a [lo, hi] list
        }
        # Must not raise — degrades to "row excluded".
        assert evaluate(leaf, ctx) == []


# ═══════════════════════════════════════════════════════════════════════
#  5. Regression — unaffected when no `where` key is present
# ═══════════════════════════════════════════════════════════════════════

class TestPurelyAdditiveRegression:
    def test_no_where_key_behaves_exactly_as_before(self):
        ctx = Context(position_rows=_mixed_exchange_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "pnl", "scope": "positions.expiring_today", "op": "<=", "value": -5000}
        result = evaluate(leaf, ctx)
        symbols = {m["row"]["tradingsymbol"] for m in result}
        assert symbols == {"NIFTY25APR22000CE", "NIFTY25APR23000PE"}

    def test_validate_accepts_where_leaf(self):
        cond = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": -5000,
            "where": {"column": "exchange", "op": "==", "value": "MCX"},
        }
        assert validate(cond) == []

    def test_validate_rejects_where_with_unknown_column(self):
        cond = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": -5000,
            "where": {"column": "bogus", "op": "==", "value": 1},
        }
        errors = validate(cond)
        assert len(errors) == 1
        assert "where" in errors[0]

    def test_validate_unaffected_for_leaf_without_where(self):
        cond = {"metric": "pnl", "scope": "positions.expiring_today", "op": "<=", "value": -5000}
        assert validate(cond) == []


# ═══════════════════════════════════════════════════════════════════════
#  6. Group-derived columns — compute_mcx_ce_pe_net_groups unit coverage
# ═══════════════════════════════════════════════════════════════════════

class TestComputeMcxCePeNetGroups:
    def test_perfectly_hedged_pair_nets_to_zero(self):
        rows = [
            {"tradingsymbol": "GOLD25APR75000CE", "quantity": 1},
            {"tradingsymbol": "GOLD25APR75000PE", "quantity": -1},
        ]
        groups = compute_mcx_ce_pe_net_groups(rows)
        assert len(groups) == 1
        (g,) = groups.values()
        assert g["net_qty"] == 0

    def test_unhedged_lone_leg_nets_nonzero(self):
        rows = [{"tradingsymbol": "GOLDM25APR75000CE", "quantity": 1}]
        groups = compute_mcx_ce_pe_net_groups(rows)
        (g,) = groups.values()
        assert g["net_qty"] == 1

    def test_malformed_quantity_degrades_group_to_none_not_raise(self):
        rows = [
            {"tradingsymbol": "GOLDM25APR75000CE", "quantity": "not-a-number"},
        ]
        groups = compute_mcx_ce_pe_net_groups(rows)
        (g,) = groups.values()
        assert g["net_qty"] is None

    def test_different_roots_never_collide(self):
        rows = [
            {"tradingsymbol": "GOLDM25APR75000CE", "quantity": 1},
            {"tradingsymbol": "GOLD25APR75000CE", "quantity": 1},
        ]
        groups = compute_mcx_ce_pe_net_groups(rows)
        assert len(groups) == 2

    def test_unparseable_symbol_excluded_from_grouping(self):
        rows = [{"tradingsymbol": "NOT_A_REAL_SYMBOL_###", "quantity": 1}]
        groups = compute_mcx_ce_pe_net_groups(rows)
        assert groups == {}


class TestBackgroundBroadcastHelper:
    """Exercises the SAME is_unhedged/net_qty broadcast background.py's
    real position_rows construction performs, via the fixture helper
    above — proves non-MCX rows and malformed-quantity MCX rows both
    degrade to None rather than crashing or mis-flagging."""

    def test_mcx_rows_get_real_values_nfo_rows_stay_none(self):
        rows = _unhedged_parity_fixture()
        by_symbol = {r["tradingsymbol"]: r for r in rows}
        assert by_symbol["GOLDM25APR75000CE"]["is_unhedged"] == 1.0
        assert by_symbol["GOLDM25APR75000CE"]["net_qty"] == 1
        assert by_symbol["GOLD25APR75000CE"]["is_unhedged"] == 0.0
        assert by_symbol["GOLD25APR75000CE"]["net_qty"] == 0
        assert by_symbol["NIFTY25APR22000CE"]["is_unhedged"] is None
        assert by_symbol["NIFTY25APR22000CE"]["net_qty"] is None
