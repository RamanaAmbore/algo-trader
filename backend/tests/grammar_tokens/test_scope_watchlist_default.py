"""Tests for grammar_tokens/scope_watchlist_default.py —
`watchlist.default` scope.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, scope batch D of the DSL token-
unification plan.
"""

from __future__ import annotations

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_watchlist_default import (
    _scope_watchlist_default,
)


def test_returns_only_default_watchlist_rows():
    rows = [
        {'account': 'Default', 'tradingsymbol': 'RELIANCE'},
        {'account': 'Markets', 'tradingsymbol': 'NIFTY 50'},
    ]
    ctx = Context(watchlist_rows=rows)
    result = _scope_watchlist_default(ctx)
    assert len(result) == 1
    assert result[0]['tradingsymbol'] == 'RELIANCE'


def test_empty_when_no_default_rows():
    rows = [{'account': 'Markets', 'tradingsymbol': 'NIFTY 50'}]
    ctx = Context(watchlist_rows=rows)
    assert _scope_watchlist_default(ctx) == []


def test_empty_when_no_watchlist_rows():
    ctx = Context(watchlist_rows=[])
    assert _scope_watchlist_default(ctx) == []
