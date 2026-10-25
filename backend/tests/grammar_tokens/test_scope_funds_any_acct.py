"""Tests for grammar_tokens/scope_funds_any_acct.py — `funds.any_acct`
scope.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, scope batch B of the DSL token-
unification plan.
"""

from __future__ import annotations

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_funds_any_acct import (
    _scope_funds_any_acct,
)


def test_returns_every_non_total_row():
    df = pd.DataFrame([
        {'account': 'ACCT1', 'cash': 1000.0},
        {'account': 'ACCT2', 'cash': 2000.0},
        {'account': 'TOTAL', 'cash': 3000.0},
    ])
    ctx = Context(df_margins=df)
    result = _scope_funds_any_acct(ctx)
    assert {r['account'] for r in result} == {'ACCT1', 'ACCT2'}


def test_empty_when_only_total_row():
    df = pd.DataFrame([{'account': 'TOTAL', 'cash': 3000.0}])
    ctx = Context(df_margins=df)
    assert _scope_funds_any_acct(ctx) == []


def test_empty_when_frame_is_none():
    ctx = Context(df_margins=None)
    assert _scope_funds_any_acct(ctx) == []


def test_empty_when_frame_is_empty():
    ctx = Context(df_margins=pd.DataFrame())
    assert _scope_funds_any_acct(ctx) == []
