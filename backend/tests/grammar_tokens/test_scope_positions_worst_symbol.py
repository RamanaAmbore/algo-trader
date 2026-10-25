"""Tests for grammar_tokens/scope_positions_worst_symbol.py —
`positions.worst_symbol` scope.

Moved out of test_grammar_parsing.py's TestWorstScopeKeyFix (Wave 1,
scope batch C of the DSL token-unification plan) — pure resolver-level
unit tests, no behavior change.
"""

from __future__ import annotations

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_positions_worst_symbol import (
    _scope_positions_worst_symbol,
)


def test_positions_worst_symbol_reads_position_rows_not_positions_rows():
    """Was reading ctx.positions_rows (doesn't exist); real field is
    ctx.position_rows (singular 'position')."""
    rows = [
        {'tradingsymbol': 'NIFTY26SEPFUT', 'pnl': -100.0},
        {'tradingsymbol': 'BANKNIFTY26SEPFUT', 'pnl': -9000.0},
    ]
    ctx = Context(position_rows=rows)
    result = _scope_positions_worst_symbol(ctx)
    assert len(result) == 1, f"Expected exactly 1 row, got {len(result)}"
    assert result[0]['tradingsymbol'] == 'BANKNIFTY26SEPFUT', (
        f"Expected the worst pnl row, got {result[0]['tradingsymbol']}"
    )


def test_positions_worst_symbol_falls_back_when_no_rows():
    df = pd.DataFrame([{'account': 'ACCT1', 'day_change_percentage': -2.0}])
    ctx = Context(sum_positions=df, position_rows=[])
    result = _scope_positions_worst_symbol(ctx)
    assert result == [{'account': 'ACCT1', 'day_change_percentage': -2.0}], (
        f"Expected fallback to worst_acct row, got {result}"
    )
