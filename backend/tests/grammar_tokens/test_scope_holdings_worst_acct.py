"""Tests for grammar_tokens/scope_holdings_worst_acct.py —
`holdings.worst_acct` scope.

Moved out of test_grammar_parsing.py's TestWorstScopeKeyFix (Wave 1,
scope batch B of the DSL token-unification plan) — pure resolver-level
unit test, no behavior change.
"""

from __future__ import annotations

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_holdings_worst_acct import (
    _scope_holdings_worst_acct,
)


def test_holdings_worst_acct_keys_on_day_change_percentage():
    df = pd.DataFrame([
        {'account': 'ACCT1', 'day_change_percentage': -1.0},
        {'account': 'ACCT2', 'day_change_percentage': -4.5},
    ])
    ctx = Context(sum_holdings=df)
    result = _scope_holdings_worst_acct(ctx)
    assert len(result) == 1, f"Expected exactly 1 row, got {len(result)} — key fix regressed"
    assert result[0]['account'] == 'ACCT2', (
        f"Expected the worst account (ACCT2, -4.5%), got {result[0]['account']}"
    )


def test_empty_when_no_account_has_the_column():
    df = pd.DataFrame([{'account': 'ACCT1'}])
    ctx = Context(sum_holdings=df)
    assert _scope_holdings_worst_acct(ctx) == []
