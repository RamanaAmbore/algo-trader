"""Tests for grammar_tokens/scope_positions_any_underlying.py —
`positions.any_underlying` scope.

Moved out of test_underlying_loss_divergence_tokens.py's
TestScopePositionsAnyUnderlying (Wave 1, scope batch C of the DSL
token-unification plan) — pure resolver-level unit tests, no behavior
change.
"""

from __future__ import annotations

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_positions_any_underlying import (
    _scope_positions_any_underlying,
)


def test_groups_rows_by_underlying():
    rows = [
        {'underlying': 'NIFTY', 'account': 'ACC1', 'pnl': -100.0, 'day_change_val': -50.0},
        {'underlying': 'NIFTY', 'account': 'ACC2', 'pnl': -200.0, 'day_change_val': -75.0},
        {'underlying': 'CRUDEOIL', 'account': 'ACC1', 'pnl': 300.0, 'day_change_val': 10.0},
    ]
    ctx = Context(position_rows=rows)
    out = _scope_positions_any_underlying(ctx)
    by_u = {r['underlying']: r for r in out}
    assert set(by_u.keys()) == {'NIFTY', 'CRUDEOIL'}, f"got {by_u.keys()}"
    assert by_u['NIFTY']['pnl'] == -300.0
    assert by_u['NIFTY']['day_change_val'] == -125.0
    assert by_u['NIFTY']['accounts'] == ['ACC1', 'ACC2']
    assert by_u['CRUDEOIL']['pnl'] == 300.0


def test_rows_without_underlying_are_skipped():
    """Cash-equity / unparseable rows (no 'underlying') never produce
    a bucket — same skip rule `_scope_positions_expiring_today` uses."""
    rows = [
        {'underlying': None, 'account': 'ACC1', 'pnl': -999.0},
        {'account': 'ACC1', 'pnl': -999.0},  # key entirely absent
    ]
    ctx = Context(position_rows=rows)
    assert _scope_positions_any_underlying(ctx) == []


def test_empty_position_rows_returns_empty():
    assert _scope_positions_any_underlying(Context(position_rows=[])) == []
    assert _scope_positions_any_underlying(Context()) == []
