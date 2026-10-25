"""Scope resolver — `positions.worst_acct`: single per-account positions
row with the worst day_change_percentage."""

from __future__ import annotations

from backend.api.algo.grammar import _row_with_min
from backend.api.algo.grammar_tokens.scope_positions_any_acct import (
    _scope_positions_any_acct,
)


def _scope_positions_worst_acct(ctx):
    """Single per-account positions row with the worst day_change_percentage.

    Fix #11 — was keying on 'pnl_pct', a column the positions summary
    frame never carries at all (only 'day_change_val'/'day_change_percentage'
    — see background._fetch_positions_direct / _rebuild_positions_summary),
    so this scope always returned []."""
    rows = _scope_positions_any_acct(ctx)
    return _row_with_min(rows, 'day_change_percentage')
