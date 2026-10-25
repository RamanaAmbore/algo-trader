"""Scope resolver — `positions.any_acct`: every non-TOTAL account row of
the positions summary (leaf is OR-combined)."""

from __future__ import annotations


def _scope_positions_any_acct(ctx):
    """Every non-TOTAL account-level row from `ctx.sum_positions` — one
    row per account, excluding the aggregate TOTAL row `positions.total`
    reads."""
    df = ctx.sum_positions
    if df is None or df.empty:
        return []
    mask = df['account'].astype(str) != 'TOTAL'
    return [r.to_dict() for _, r in df[mask].iterrows()]
