"""Tests for grammar_tokens/scope_holdings_worst_symbol.py —
`holdings.worst_symbol` scope.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, scope batch B of the DSL token-
unification plan.
"""

from __future__ import annotations

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_holdings_worst_symbol import (
    _scope_holdings_worst_symbol,
)


def test_falls_back_to_worst_acct_when_no_holdings_rows():
    """Context carries no `holdings_rows` field at all (see grammar.py's
    own module comment above EXPOSED_COLUMNS' holdings-summary entry) —
    the real-world path is always this fallback, by design."""
    df = pd.DataFrame([
        {'account': 'ACCT1', 'day_change_percentage': -1.0},
        {'account': 'ACCT2', 'day_change_percentage': -4.5},
    ])
    ctx = Context(sum_holdings=df)
    result = _scope_holdings_worst_symbol(ctx)
    assert len(result) == 1
    assert result[0]['account'] == 'ACCT2'


def test_uses_per_symbol_rows_when_holdings_rows_populated():
    """A future caller that DOES populate `ctx.holdings_rows` (per-symbol
    detail) gets a real per-symbol worst-row answer, not the per-account
    fallback — exercised via a plain stub object since the real Context
    dataclass has no such field today."""
    rows_df = pd.DataFrame([
        {'tradingsymbol': 'RELIANCE', 'day_change_percentage': -0.5},
        {'tradingsymbol': 'TCS', 'day_change_percentage': -6.0},
    ])
    stub_ctx = type('Ctx', (), {'holdings_rows': rows_df, 'sum_holdings': None})()
    result = _scope_holdings_worst_symbol(stub_ctx)
    assert len(result) == 1
    assert result[0]['tradingsymbol'] == 'TCS'


def test_falls_back_when_holdings_rows_is_empty_dataframe():
    df = pd.DataFrame([{'account': 'ACCT1', 'day_change_percentage': -2.0}])
    stub_ctx = type('Ctx', (), {'holdings_rows': pd.DataFrame(), 'sum_holdings': df})()
    result = _scope_holdings_worst_symbol(stub_ctx)
    assert result == [{'account': 'ACCT1', 'day_change_percentage': -2.0}]
