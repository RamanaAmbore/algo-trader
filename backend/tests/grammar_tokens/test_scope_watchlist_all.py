"""Tests for grammar_tokens/scope_watchlist_all.py — `watchlist.all`
scope.

No dedicated resolver-level test existed before this migration (every
existing reference across the suite wires `_scope_watchlist_all` into
`REGISTRY.scopes` as a fixture for evaluator/engine integration tests,
never asserts on the resolver's own return value directly) — fresh
coverage, written as part of Wave 1, scope batch D of the DSL token-
unification plan.
"""

from __future__ import annotations

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_watchlist_all import (
    _scope_watchlist_all,
)


def test_returns_every_watchlist_row():
    rows = [
        {'account': 'Default', 'tradingsymbol': 'RELIANCE'},
        {'account': 'Markets', 'tradingsymbol': 'NIFTY 50'},
    ]
    ctx = Context(watchlist_rows=rows)
    assert _scope_watchlist_all(ctx) == rows


def test_empty_when_no_watchlist_rows():
    ctx = Context(watchlist_rows=[])
    assert _scope_watchlist_all(ctx) == []
