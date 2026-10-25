"""Scope resolver — `holdings.worst_acct`: single per-account holdings
row with the worst day_change_percentage."""

from __future__ import annotations

from backend.api.algo.grammar import _row_with_min
from backend.api.algo.grammar_tokens.scope_holdings_any_acct import (
    _scope_holdings_any_acct,
)


def _scope_holdings_worst_acct(ctx):
    """Single per-account holdings row with the worst day_change_percentage.

    Fix #11 — was keying on 'day_pct', a column name the holdings summary
    frame never carries (it carries 'day_change_percentage' — see
    background._bg_holdings_add_pct), so this scope always returned []."""
    rows = _scope_holdings_any_acct(ctx)
    return _row_with_min(rows, 'day_change_percentage')
