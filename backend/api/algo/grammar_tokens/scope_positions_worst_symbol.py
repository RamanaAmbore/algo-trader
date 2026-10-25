"""Scope resolver — `positions.worst_symbol`: single per-symbol position
row with the worst pnl."""

from __future__ import annotations

from backend.api.algo.grammar import _row_with_min
from backend.api.algo.grammar_tokens.scope_positions_worst_acct import (
    _scope_positions_worst_acct,
)


def _scope_positions_worst_symbol(ctx):
    """Single per-symbol positions row with the worst pnl.

    Fix #11 — was reading `ctx.positions_rows`, an attribute that does not
    exist on Context (the real field is `position_rows`, singular), so
    this scope always fell through to the worst_acct fallback."""
    rows = getattr(ctx, 'position_rows', None) or []
    if not rows:
        return _scope_positions_worst_acct(ctx)
    return _row_with_min(rows, 'pnl')
