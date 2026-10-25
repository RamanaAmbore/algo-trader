"""Tests for grammar_tokens/scope_quote_tracked.py — `quote.tracked`
scope.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, scope batch D of the DSL token-
unification plan.
"""

from __future__ import annotations

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_quote_tracked import (
    _scope_quote_tracked,
)


def test_returns_every_tracked_quote_row():
    rows = [
        {'tradingsymbol': 'RELIANCE', 'exchange': 'NSE', 'last_price': 2500.0},
        {'tradingsymbol': 'CRUDEOIL26OCTFUT', 'exchange': 'MCX', 'last_price': 5500.0},
    ]
    ctx = Context(quote_rows=rows)
    assert _scope_quote_tracked(ctx) == rows


def test_empty_when_no_quote_rows():
    ctx = Context(quote_rows=[])
    assert _scope_quote_tracked(ctx) == []
