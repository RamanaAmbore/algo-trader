"""Scope resolver — `holdings.any_acct`: every non-TOTAL account row of
the holdings summary (leaf is OR-combined)."""

from __future__ import annotations


def _scope_holdings_any_acct(ctx):
    """Every non-TOTAL account-level row from `ctx.sum_holdings` — one
    row per account currently holding something, excluding the
    aggregate TOTAL row `holdings.total` reads."""
    df = ctx.sum_holdings
    if df is None or df.empty:
        return []
    mask = df['account'].astype(str) != 'TOTAL'
    return [r.to_dict() for _, r in df[mask].iterrows()]
