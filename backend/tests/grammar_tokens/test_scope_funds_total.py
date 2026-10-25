"""Tests for grammar_tokens/scope_funds_total.py — `funds.total` scope.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, scope batch B of the DSL token-
unification plan.
"""

from __future__ import annotations

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_funds_total import (
    _scope_funds_total,
)


def test_returns_the_total_row_only():
    df = pd.DataFrame([
        {'account': 'ACCT1', 'cash': 1000.0},
        {'account': 'ACCT2', 'cash': 2000.0},
        {'account': 'TOTAL', 'cash': 3000.0},
    ])
    ctx = Context(df_margins=df)
    result = _scope_funds_total(ctx)
    assert len(result) == 1
    assert result[0]['account'] == 'TOTAL'
    assert result[0]['cash'] == 3000.0


def test_empty_when_no_total_row():
    df = pd.DataFrame([{'account': 'ACCT1', 'cash': 1000.0}])
    ctx = Context(df_margins=df)
    assert _scope_funds_total(ctx) == []


def test_empty_when_frame_is_none():
    ctx = Context(df_margins=None)
    assert _scope_funds_total(ctx) == []


def test_empty_when_frame_is_empty():
    ctx = Context(df_margins=pd.DataFrame())
    assert _scope_funds_total(ctx) == []
