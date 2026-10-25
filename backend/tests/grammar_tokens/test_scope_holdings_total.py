"""Tests for grammar_tokens/scope_holdings_total.py —
`holdings.total` scope.

No dedicated resolver-level test existed before this migration (every
existing reference across the suite wires `_scope_holdings_total` into
`REGISTRY.scopes` as a fixture for evaluator/engine integration tests,
never asserts on the resolver's own return value directly) — fresh
coverage, written as part of Wave 1, scope batch B of the DSL token-
unification plan.
"""

from __future__ import annotations

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_holdings_total import (
    _scope_holdings_total,
)


def test_returns_the_total_row_only():
    df = pd.DataFrame([
        {'account': 'ACCT1', 'pnl': 100.0},
        {'account': 'ACCT2', 'pnl': -50.0},
        {'account': 'TOTAL', 'pnl': 50.0},
    ])
    ctx = Context(sum_holdings=df)
    result = _scope_holdings_total(ctx)
    assert len(result) == 1
    assert result[0]['account'] == 'TOTAL'
    assert result[0]['pnl'] == 50.0


def test_empty_when_no_total_row():
    df = pd.DataFrame([{'account': 'ACCT1', 'pnl': 100.0}])
    ctx = Context(sum_holdings=df)
    assert _scope_holdings_total(ctx) == []


def test_empty_when_frame_is_none():
    ctx = Context(sum_holdings=None)
    assert _scope_holdings_total(ctx) == []


def test_empty_when_frame_is_empty():
    ctx = Context(sum_holdings=pd.DataFrame())
    assert _scope_holdings_total(ctx) == []
