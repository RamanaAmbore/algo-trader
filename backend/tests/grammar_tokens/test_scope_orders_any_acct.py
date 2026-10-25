"""Tests for grammar_tokens/scope_orders_any_acct.py — `orders.any_acct`
scope.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, scope batch D of the DSL token-
unification plan.
"""

from __future__ import annotations

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_orders_any_acct import (
    _scope_orders_any_acct,
)


def test_returns_every_open_order_row():
    rows = [
        {'account': 'ACCT1', 'tradingsymbol': 'RELIANCE', 'status': 'OPEN'},
        {'account': 'ACCT2', 'tradingsymbol': 'NIFTY25APR22000CE', 'status': 'OPEN'},
    ]
    ctx = Context(orders_rows=rows)
    assert _scope_orders_any_acct(ctx) == rows


def test_empty_when_no_orders_rows():
    ctx = Context(orders_rows=[])
    assert _scope_orders_any_acct(ctx) == []
