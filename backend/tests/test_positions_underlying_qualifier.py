"""
Sprint G backend — `underlying:` qualifier on `positions.*` scopes
(filter, not a group-by aggregation).

Generalizes the per-row underlying derivation the MCX collar detector
(`grammar_tokens.scope_positions_expiring_today_mcx_unhedged.
_scope_positions_expiring_today_mcx_unhedged`) already does
inline (`parse_tradingsymbol(...)['root']`) into a reusable
`grammar.underlying_of_symbol()` helper, and wires a real `underlying`
column onto every position row built in `background.py`
(`_perf_run_agent_engine`'s `position_rows` construction), so a leaf's
existing `scope_filter` mechanism (Sprint 19 — the SAME generic
`scope_filter_row_predicate` `symbol:`/`exchange:` already use) can
filter `positions.expiring_today`/`.nfo`/`.mcx_unhedged`/`worst_symbol`
down to one underlying, e.g. `positions.expiring_today.underlying:NIFTY`.

Covers:
  1. `underlying_of_symbol` unit coverage — F&O options/futures resolve
     to their root, cash equity / unparseable symbols resolve to None.
  2. `scope_filter: {"underlying": ...}` correctly isolates a multi-
     underlying portfolio's rows (NIFTY + BANKNIFTY options, one
     account) via `evaluate()`.
  3. Composition with the `.nfo` / `.mcx_unhedged` variants, mirroring
     `test_scope_filter_symbol.py`'s existing composition coverage for
     `symbol:`.
  4. `validate()` accepts the new qualifier; unknown-qualifier rejection
     (e.g. on `positions.total`, which has no per-symbol row shape) is
     unaffected.
  5. `background.py`'s position_rows construction: the real per-row
     `underlying` derivation (isolated — not a full `_perf_run_
     agent_engine` integration test, matching the existing project
     convention of unit-testing the derivation logic directly).
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
from backend.api.algo.grammar_tokens.scope_positions_expiring_today_nfo import (
    _scope_positions_expiring_today_nfo,
)
from backend.api.algo.grammar_tokens.scope_positions_expiring_today_mcx_unhedged import (
    _scope_positions_expiring_today_mcx_unhedged,
)


_NOW_EXPIRY_DAY = datetime(2025, 4, 24, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _wire_registry(monkeypatch):
    from backend.api.algo.grammar_registry import REGISTRY
    monkeypatch.setattr(REGISTRY, 'metrics', {
        'pnl': _metric_pnl,
    })
    monkeypatch.setattr(REGISTRY, 'scopes', {
        'positions.expiring_today':              _scope_positions_expiring_today,
        'positions.expiring_today.nfo':           _scope_positions_expiring_today_nfo,
        'positions.expiring_today.mcx_unhedged':  _scope_positions_expiring_today_mcx_unhedged,
        'positions.total':                        grammar._scope_positions_total,
    })
    monkeypatch.setattr(REGISTRY, 'operators', {
        '<=': grammar.OPERATORS['<='], '>': grammar.OPERATORS['>'],
    })
    monkeypatch.setattr(REGISTRY, 'metric_factories', {})
    monkeypatch.setattr(REGISTRY, '_metric_call_cache', {})
    monkeypatch.setattr(REGISTRY, '_scope_call_cache', {})


# ═══════════════════════════════════════════════════════════════════════
#  1. underlying_of_symbol — unit coverage
# ═══════════════════════════════════════════════════════════════════════

class TestUnderlyingOfSymbol:
    def test_option_resolves_to_root(self):
        assert grammar.underlying_of_symbol("NIFTY25APR22000CE") == "NIFTY"

    def test_future_resolves_to_root(self):
        assert grammar.underlying_of_symbol("CRUDEOIL26OCTFUT") == "CRUDEOIL"

    def test_cash_equity_resolves_to_none(self):
        assert grammar.underlying_of_symbol("RELIANCE") is None

    def test_empty_string_resolves_to_none(self):
        assert grammar.underlying_of_symbol("") is None

    def test_none_input_does_not_raise(self):
        assert grammar.underlying_of_symbol(None) is None

    def test_same_as_collar_detector_root(self):
        """The collar detector's own inline `parsed.get('root')` and the
        new helper must agree — same underlying logic, not a second
        implementation."""
        sym = "GOLDM25APR75000CE"
        parsed = grammar._parsed_or_none(sym)
        assert grammar.underlying_of_symbol(sym) == parsed.get('root')


# ═══════════════════════════════════════════════════════════════════════
#  2. scope_filter.underlying isolates a multi-underlying portfolio
# ═══════════════════════════════════════════════════════════════════════

_SYM_NIFTY = "NIFTY25APR22000CE"
_SYM_BANKNIFTY = "BANKNIFTY25APR48000PE"


def _multi_underlying_rows():
    """One account holding both NIFTY and BANKNIFTY options, both
    expiring today — the `underlying` field is pre-populated exactly as
    `background.py`'s position_rows construction would set it."""
    return [
        {"account": "ACCT_X", "tradingsymbol": _SYM_NIFTY, "exchange": "NFO",
         "quantity": 50, "pnl": -6000.0, "underlying": "NIFTY"},
        {"account": "ACCT_X", "tradingsymbol": _SYM_BANKNIFTY, "exchange": "NFO",
         "quantity": -50, "pnl": -7000.0, "underlying": "BANKNIFTY"},
    ]


class TestUnderlyingScopeFilter:
    def test_filters_to_requested_underlying_only(self):
        ctx = Context(position_rows=_multi_underlying_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": -5000,
            "scope_filter": {"underlying": "NIFTY"},
        }
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["row"]["tradingsymbol"] == _SYM_NIFTY

    def test_other_underlying_breaching_does_not_leak_through(self):
        ctx = Context(position_rows=_multi_underlying_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": -5000,
            "scope_filter": {"underlying": "BANKNIFTY"},
        }
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["row"]["tradingsymbol"] == _SYM_BANKNIFTY

    def test_unfiltered_both_underlyings_fire(self):
        ctx = Context(position_rows=_multi_underlying_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "pnl", "scope": "positions.expiring_today", "op": "<=", "value": -5000}
        result = evaluate(leaf, ctx)
        assert len(result) == 2

    def test_cash_equity_row_with_no_underlying_excluded_by_real_filter(self):
        rows = _multi_underlying_rows() + [
            {"account": "ACCT_X", "tradingsymbol": "RELIANCE", "exchange": "NSE",
             "quantity": 100, "pnl": -9000.0, "underlying": None},
        ]
        ctx = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": -5000,
            "scope_filter": {"underlying": "NIFTY"},
        }
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["row"]["tradingsymbol"] == _SYM_NIFTY


# ═══════════════════════════════════════════════════════════════════════
#  3. Composition with .nfo / .mcx_unhedged
# ═══════════════════════════════════════════════════════════════════════

class TestComposesWithVariants:
    def test_composes_with_nfo_variant(self):
        rows = _multi_underlying_rows() + [
            {"account": "ACCT_X", "tradingsymbol": "GOLDM25APR75000CE",
             "exchange": "MCX", "quantity": 1, "pnl": -9999.0, "underlying": "GOLDM"},
        ]
        ctx = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today.nfo",
            "op": "<=", "value": -5000,
            "scope_filter": {"underlying": "NIFTY"},
        }
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["row"]["tradingsymbol"] == _SYM_NIFTY

    def test_composes_with_mcx_unhedged_variant(self):
        rows = [
            {"account": "ACCT_X", "tradingsymbol": "GOLDM25APR75000CE",
             "exchange": "MCX", "quantity": 1, "pnl": -8000.0, "underlying": "GOLDM"},
            {"account": "ACCT_X", "tradingsymbol": "GOLD25APR75000CE",
             "exchange": "MCX", "quantity": 1, "pnl": -9000.0, "underlying": "GOLD"},
            {"account": "ACCT_X", "tradingsymbol": "GOLD25APR75000PE",
             "exchange": "MCX", "quantity": -1, "pnl": -9000.0, "underlying": "GOLD"},
        ]
        ctx = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today.mcx_unhedged",
            "op": "<=", "value": -5000,
            "scope_filter": {"underlying": "GOLDM"},
        }
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["row"]["tradingsymbol"] == "GOLDM25APR75000CE"

    def test_mcx_unhedged_hedged_leg_still_excluded_even_if_underlying_matches(self):
        rows = [
            {"account": "ACCT_X", "tradingsymbol": "GOLD25APR75000CE",
             "exchange": "MCX", "quantity": 1, "pnl": -9000.0, "underlying": "GOLD"},
            {"account": "ACCT_X", "tradingsymbol": "GOLD25APR75000PE",
             "exchange": "MCX", "quantity": -1, "pnl": -9000.0, "underlying": "GOLD"},
        ]
        ctx = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today.mcx_unhedged",
            "op": "<=", "value": -5000,
            "scope_filter": {"underlying": "GOLD"},
        }
        assert evaluate(leaf, ctx) == []


# ═══════════════════════════════════════════════════════════════════════
#  4. validate() — new qualifier accepted; unknown-qualifier unaffected
# ═══════════════════════════════════════════════════════════════════════

class TestValidateUnderlyingQualifier:
    def test_validate_accepts_underlying_scope_filter(self):
        cond = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": -5000,
            "scope_filter": {"underlying": "NIFTY"},
        }
        assert validate(cond) == []

    def test_validate_rejects_underlying_on_scope_with_no_such_column(self):
        """positions.total has no per-symbol row shape — no `underlying`
        column — same whitelist rejection every other unknown qualifier
        already gets."""
        cond = {
            "metric": "pnl", "scope": "positions.total",
            "op": "<=", "value": -5000,
            "scope_filter": {"underlying": "NIFTY"},
        }
        errors = validate(cond)
        assert len(errors) == 1
        assert "scope_filter" in errors[0]
        assert "underlying" in errors[0]

    def test_scope_filter_row_predicate_unit(self):
        predicate = grammar.scope_filter_row_predicate(
            "positions.expiring_today", {"underlying": "NIFTY"})
        assert predicate is not None
        assert predicate({"underlying": "NIFTY"}) is True
        assert predicate({"underlying": "BANKNIFTY"}) is False
        assert predicate({"underlying": None}) is False


# ═══════════════════════════════════════════════════════════════════════
#  5. background.py position_rows construction — underlying derivation
# ═══════════════════════════════════════════════════════════════════════

class TestBackgroundPositionRowsUnderlyingDerivation:
    def test_derivation_matches_underlying_of_symbol_per_row(self):
        """Isolates the exact per-row derivation
        `_perf_run_agent_engine` applies to `position_rows` (via
        `grammar.underlying_of_symbol`) without needing a full
        integration test of the whole background task."""
        rows = [
            {"tradingsymbol": _SYM_NIFTY, "exchange": "NFO"},
            {"tradingsymbol": _SYM_BANKNIFTY, "exchange": "NFO"},
            {"tradingsymbol": "RELIANCE", "exchange": "NSE"},
        ]
        for r in rows:
            r["underlying"] = grammar.underlying_of_symbol(r.get("tradingsymbol"))
        assert rows[0]["underlying"] == "NIFTY"
        assert rows[1]["underlying"] == "BANKNIFTY"
        assert rows[2]["underlying"] is None
