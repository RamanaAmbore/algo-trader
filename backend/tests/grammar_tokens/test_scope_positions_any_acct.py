"""Tests for grammar_tokens/scope_positions_any_acct.py —
`positions.any_acct` scope.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, scope batch C of the DSL token-
unification plan.
"""

from __future__ import annotations

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_positions_any_acct import (
    _scope_positions_any_acct,
)


def test_returns_every_non_total_row():
    df = pd.DataFrame([
        {'account': 'ACCT1', 'pnl': 100.0},
        {'account': 'ACCT2', 'pnl': -50.0},
        {'account': 'TOTAL', 'pnl': 50.0},
    ])
    ctx = Context(sum_positions=df)
    result = _scope_positions_any_acct(ctx)
    assert {r['account'] for r in result} == {'ACCT1', 'ACCT2'}


def test_empty_when_only_total_row():
    df = pd.DataFrame([{'account': 'TOTAL', 'pnl': 50.0}])
    ctx = Context(sum_positions=df)
    assert _scope_positions_any_acct(ctx) == []


def test_empty_when_frame_is_none():
    ctx = Context(sum_positions=None)
    assert _scope_positions_any_acct(ctx) == []


def test_empty_when_frame_is_empty():
    ctx = Context(sum_positions=pd.DataFrame())
    assert _scope_positions_any_acct(ctx) == []
