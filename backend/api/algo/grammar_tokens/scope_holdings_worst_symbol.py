"""Scope resolver — `holdings.worst_symbol`: single per-symbol holdings
row with the worst day_change_percentage."""

from __future__ import annotations

from backend.api.algo.grammar import _row_with_min
from backend.api.algo.grammar_tokens.scope_holdings_worst_acct import (
    _scope_holdings_worst_acct,
)


def _scope_holdings_worst_symbol(ctx):
    """Single per-symbol holdings row with the worst day_change_percentage.
    Note: relies on the engine context populating per-symbol detail. When
    the live pipeline only carries per-account aggregates (current
    default), this falls back to the same row as worst_acct — operators
    get an honest drawdown signal either way."""
    df = getattr(ctx, 'holdings_rows', None)
    if df is None or (hasattr(df, 'empty') and df.empty):
        return _scope_holdings_worst_acct(ctx)
    rows = [r.to_dict() for _, r in df.iterrows()] if hasattr(df, 'iterrows') else list(df)
    return _row_with_min(rows, 'day_change_percentage')
