"""Tests for grammar_tokens/scope_positions_worst_acct.py —
`positions.worst_acct` scope.

Moved out of test_grammar_parsing.py's TestWorstScopeKeyFix (Wave 1,
scope batch C of the DSL token-unification plan) — pure resolver-level
unit test, no behavior change.
"""

from __future__ import annotations

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_positions_worst_acct import (
    _scope_positions_worst_acct,
)


def test_positions_worst_acct_keys_on_day_change_percentage():
    df = pd.DataFrame([
        {'account': 'ACCT1', 'day_change_percentage': -0.5},
        {'account': 'ACCT2', 'day_change_percentage': -3.1},
    ])
    ctx = Context(sum_positions=df)
    result = _scope_positions_worst_acct(ctx)
    assert len(result) == 1, f"Expected exactly 1 row, got {len(result)} — key fix regressed"
    assert result[0]['account'] == 'ACCT2'


def test_empty_when_no_account_has_the_column():
    df = pd.DataFrame([{'account': 'ACCT1'}])
    ctx = Context(sum_positions=df)
    assert _scope_positions_worst_acct(ctx) == []
