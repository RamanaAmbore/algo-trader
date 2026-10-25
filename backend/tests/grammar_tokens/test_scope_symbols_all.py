"""Tests for grammar_tokens/scope_symbols_all.py — `symbols.all` scope.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, scope batch D of the DSL token-
unification plan.
"""

from __future__ import annotations

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_symbols_all import (
    _scope_symbols_all,
)


def test_returns_every_symbols_row_across_all_kinds():
    rows = [
        {'kind': 'watchlist', 'tradingsymbol': 'RELIANCE'},
        {'kind': 'gainers', 'tradingsymbol': 'TCS'},
        {'kind': 'losers', 'tradingsymbol': 'INFY'},
        {'kind': 'pinned', 'tradingsymbol': 'NIFTY 50'},
    ]
    ctx = Context(symbols_rows=rows)
    assert _scope_symbols_all(ctx) == rows


def test_empty_when_no_symbols_rows():
    ctx = Context(symbols_rows=[])
    assert _scope_symbols_all(ctx) == []
