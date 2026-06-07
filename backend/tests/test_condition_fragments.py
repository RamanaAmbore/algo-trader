"""
Item 2 Stage 2 — condition fragment resolution tests.

Covers `evaluate()` with `{"$ref": <name>}` nodes:
  - happy path  — $ref resolves to a fragment body, body evaluates
  - cycle       — A → B → A is detected, returns [] (no crash)
  - missing ref — unknown name returns [] gracefully
  - nested      — $ref inside an `all`/`any` block resolves correctly
  - validate()  — surfaces missing refs + cycles as errors

The registry is patched directly (no DB) so each test is pure
function unit.
"""

from __future__ import annotations

import pytest
import pandas as pd
from datetime import datetime, timezone

from backend.api.algo.agent_evaluator import Context, evaluate, validate
from backend.api.algo.template_registry import REGISTRY
from backend.api.algo import grammar as _grammar
from backend.api.algo.grammar_registry import REGISTRY as GRAMMAR_REGISTRY


@pytest.fixture(autouse=True)
def _seed_grammar_minimal():
    """Inject just the metric/scope/op tokens our condition trees use.
    The real DB-driven registry isn't loaded in unit tests; without
    this fixture the leaf evaluator returns None and every test that
    actually fires a leaf would short-circuit to []."""
    GRAMMAR_REGISTRY.metrics["pnl"]          = _grammar._metric_pnl
    GRAMMAR_REGISTRY.metrics["pnl_pct"]      = _grammar._metric_pnl_pct
    GRAMMAR_REGISTRY.metrics["pnl_rate_abs"] = _grammar._metric_pnl_rate_abs
    GRAMMAR_REGISTRY.metrics["pnl_rate_pct"] = _grammar._metric_pnl_rate_pct
    GRAMMAR_REGISTRY.scopes["positions.total"]     = _grammar._scope_positions_total
    GRAMMAR_REGISTRY.scopes["positions.any_acct"]  = _grammar._scope_positions_any_acct
    # Operators are seeded at module import via OPERATORS dict; ensure
    # the common comparators are present in case earlier tests cleared
    # them.
    from backend.api.algo.grammar import OPERATORS
    for k, v in OPERATORS.items():
        GRAMMAR_REGISTRY.operators[k] = v
    yield


def _set_cache(condition: dict, notify: dict | None = None):
    REGISTRY._cache = {
        "notify": dict(notify or {}),
        "condition": dict(condition),
    }


def _ctx_pnl(pnl: float) -> Context:
    """Context where positions.total has the given pnl. used_margin set
    high enough that pnl_pct never trips on its own."""
    df = pd.DataFrame([{"account": "TOTAL", "pnl": pnl, "pnl_percentage": 0.0}])
    margins = pd.DataFrame([{"account": "TOTAL", "util debits": 1000000.0}])
    return Context(
        sum_positions=df,
        df_margins=margins,
        now=datetime.now(timezone.utc),
    )


# ── happy path ─────────────────────────────────────────────────────────

def test_ref_resolves_to_fragment_body_and_fires():
    _set_cache({
        "loss-total": {"metric": "pnl", "scope": "positions.total",
                       "op": "<=", "value": -50000},
    })
    ctx = _ctx_pnl(-60000)
    matches = evaluate({"$ref": "loss-total"}, ctx)
    assert len(matches) == 1
    assert matches[0]["metric"] == "pnl"
    assert matches[0]["value"] == -60000


def test_ref_resolves_to_fragment_body_and_does_not_fire_below_threshold():
    _set_cache({
        "loss-total": {"metric": "pnl", "scope": "positions.total",
                       "op": "<=", "value": -50000},
    })
    ctx = _ctx_pnl(-30000)   # not yet crossed
    assert evaluate({"$ref": "loss-total"}, ctx) == []


# ── cycle detection ────────────────────────────────────────────────────

def test_cycle_a_refs_b_refs_a_returns_empty_without_crash():
    _set_cache({
        "frag-a": {"$ref": "frag-b"},
        "frag-b": {"$ref": "frag-a"},
    })
    ctx = _ctx_pnl(-60000)
    # Must not raise RecursionError; must return [].
    assert evaluate({"$ref": "frag-a"}, ctx) == []


def test_self_referencing_fragment_returns_empty():
    _set_cache({
        "frag-self": {"$ref": "frag-self"},
    })
    assert evaluate({"$ref": "frag-self"}, _ctx_pnl(0)) == []


# ── missing ref ────────────────────────────────────────────────────────

def test_missing_ref_returns_empty():
    _set_cache({})
    assert evaluate({"$ref": "doesnt-exist"}, _ctx_pnl(-99999)) == []


def test_missing_ref_malformed_node():
    _set_cache({})
    # $ref must be a non-empty string.
    assert evaluate({"$ref": None}, _ctx_pnl(0)) == []
    assert evaluate({"$ref": ""}, _ctx_pnl(0)) == []
    assert evaluate({"$ref": 42}, _ctx_pnl(0)) == []


def test_fragment_body_not_a_dict_returns_empty():
    _set_cache({
        "frag-bad-body": [1, 2, 3],   # list instead of condition dict
    })
    assert evaluate({"$ref": "frag-bad-body"}, _ctx_pnl(-99999)) == []


# ── nested refs ────────────────────────────────────────────────────────

def test_ref_inside_any_block():
    _set_cache({
        "leg-a": {"metric": "pnl", "scope": "positions.total",
                  "op": "<=", "value": -50000},
        "leg-b": {"metric": "pnl", "scope": "positions.total",
                  "op": "<=", "value": -100000},
    })
    # any: leg-a (fires at -50k) OR inline leaf (fires at -200k)
    cond = {"any": [
        {"$ref": "leg-a"},
        {"metric": "pnl", "scope": "positions.total",
         "op": "<=", "value": -200000},
    ]}
    matches = evaluate(cond, _ctx_pnl(-60000))
    assert len(matches) == 1
    assert matches[0]["threshold"] == -50000   # leg-a fired


def test_ref_inside_all_block():
    _set_cache({
        "is-bleeding": {"metric": "pnl", "scope": "positions.total",
                        "op": "<=", "value": -10000},
    })
    cond = {"all": [
        {"$ref": "is-bleeding"},
        {"metric": "pnl", "scope": "positions.total",
         "op": "<=", "value": -5000},      # easier — also fires
    ]}
    matches = evaluate(cond, _ctx_pnl(-30000))
    assert len(matches) == 2   # both legs match → both in matches list


def test_nested_ref_chain_a_refs_b_refs_leaf():
    _set_cache({
        "outer": {"$ref": "inner"},
        "inner": {"metric": "pnl", "scope": "positions.total",
                  "op": "<=", "value": -50000},
    })
    matches = evaluate({"$ref": "outer"}, _ctx_pnl(-60000))
    assert len(matches) == 1


# ── validate() ─────────────────────────────────────────────────────────

def test_validate_passes_for_resolvable_ref():
    _set_cache({
        "good": {"metric": "pnl", "scope": "positions.total",
                 "op": "<=", "value": -10000},
    })
    errors = validate({"$ref": "good"})
    assert errors == []


def test_validate_surfaces_missing_ref():
    _set_cache({})
    errors = validate({"$ref": "nope"})
    assert any("nope" in e for e in errors)


def test_validate_detects_cycle():
    _set_cache({
        "a": {"$ref": "b"},
        "b": {"$ref": "a"},
    })
    errors = validate({"$ref": "a"})
    assert any("cycle" in e for e in errors)


def test_validate_drills_into_fragment_body_for_token_errors():
    """A typo'd metric inside a fragment should surface — validate
    must recurse into the resolved body."""
    _set_cache({
        "typo": {"metric": "p_n_l_typo", "scope": "positions.total",
                 "op": "<=", "value": 0},
    })
    errors = validate({"$ref": "typo"})
    assert any("p_n_l_typo" in e for e in errors)
