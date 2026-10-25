"""Tests for grammar_tokens/scope_watchlist_markets.py —
`watchlist.markets` scope.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, scope batch D of the DSL token-
unification plan.
"""

from __future__ import annotations

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_watchlist_markets import (
    _scope_watchlist_markets,
)


def test_returns_only_markets_watchlist_rows():
    rows = [
        {'account': 'Default', 'tradingsymbol': 'RELIANCE'},
        {'account': 'Markets', 'tradingsymbol': 'NIFTY 50'},
    ]
    ctx = Context(watchlist_rows=rows)
    result = _scope_watchlist_markets(ctx)
    assert len(result) == 1
    assert result[0]['tradingsymbol'] == 'NIFTY 50'


def test_empty_when_no_markets_rows():
    rows = [{'account': 'Default', 'tradingsymbol': 'RELIANCE'}]
    ctx = Context(watchlist_rows=rows)
    assert _scope_watchlist_markets(ctx) == []


def test_empty_when_no_watchlist_rows():
    ctx = Context(watchlist_rows=[])
    assert _scope_watchlist_markets(ctx) == []
