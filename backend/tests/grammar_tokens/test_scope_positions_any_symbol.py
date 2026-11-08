"""Tests for grammar_tokens/scope_positions_any_symbol.py —
`positions.any_symbol` scope.

Sprint 1 foundation (auto-close-short-option-at-90%-profit plan). Fan-out
scope: EVERY per-symbol position row returned independently (one row per
held position), distinct from `positions.worst_symbol` (collapses to a
single worst row) and `positions.any_underlying` (collapses to one
aggregate row per underlying root).
"""

from __future__ import annotations

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_positions_any_symbol import (
    _scope_positions_any_symbol,
)


def test_returns_every_row_independently_no_collapsing():
    rows = [
        {'account': 'ZG0790', 'tradingsymbol': 'NIFTY26OCTFUT', 'pnl': -100.0},
        {'account': 'ZG0790', 'tradingsymbol': 'BANKNIFTY26OCTFUT', 'pnl': -9000.0},
        {'account': 'ZJ6294', 'tradingsymbol': 'CRUDEOIL26OCTFUT', 'pnl': 500.0},
    ]
    ctx = Context(position_rows=rows)
    result = _scope_positions_any_symbol(ctx)
    assert result == rows, f"expected every row returned unchanged, got {result}"
    assert len(result) == 3


def test_empty_position_rows_returns_empty_list():
    ctx = Context(position_rows=[])
    assert _scope_positions_any_symbol(ctx) == []


def test_none_position_rows_returns_empty_list():
    """`ctx.position_rows` is explicitly `None` (not the dataclass
    default empty list) — must not raise, must return []. Guards the
    `getattr(ctx, 'position_rows', None) or []` fallback."""
    ctx = Context(position_rows=None)
    assert _scope_positions_any_symbol(ctx) == []


def test_bare_context_default_returns_empty_list():
    """A bare `Context()` defaults `position_rows` to `[]` via
    dataclass `field(default_factory=list)` — must return []."""
    ctx = Context()
    assert _scope_positions_any_symbol(ctx) == []


def test_returns_a_new_list_not_the_same_object():
    """`list(...)` copy — a caller mutating the returned list must never
    corrupt ctx.position_rows itself."""
    rows = [{'account': 'ZG0790', 'tradingsymbol': 'X'}]
    ctx = Context(position_rows=rows)
    result = _scope_positions_any_symbol(ctx)
    assert result == rows
    assert result is not rows
    result.append({'account': 'ZJ6294', 'tradingsymbol': 'Y'})
    assert len(rows) == 1, "mutating the scope's return value must not mutate ctx.position_rows"


def test_does_not_collapse_same_account_different_symbol_rows():
    """The exact shape `positions.worst_symbol` collapses away (one row
    per account) — any_symbol must preserve BOTH rows for the same
    account when they are different symbols."""
    rows = [
        {'account': 'ZG0790', 'tradingsymbol': 'A26OCTFUT', 'pnl': -5000.0},
        {'account': 'ZG0790', 'tradingsymbol': 'B26OCTFUT', 'pnl': -50.0},
    ]
    ctx = Context(position_rows=rows)
    result = _scope_positions_any_symbol(ctx)
    assert len(result) == 2
    accounts = {r['account'] for r in result}
    symbols = {r['tradingsymbol'] for r in result}
    assert accounts == {'ZG0790'}
    assert symbols == {'A26OCTFUT', 'B26OCTFUT'}
