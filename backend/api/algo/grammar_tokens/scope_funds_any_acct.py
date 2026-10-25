"""Scope resolver — `funds.any_acct`: every non-TOTAL account row of
the funds dataframe (leaf is OR-combined)."""

from __future__ import annotations


def _scope_funds_any_acct(ctx):
    """Every non-TOTAL account-level row from `ctx.df_margins` — one
    row per account, excluding the aggregate TOTAL row `funds.total`
    reads."""
    df = ctx.df_margins
    if df is None or df.empty:
        return []
    mask = df['account'].astype(str) != 'TOTAL'
    return [r.to_dict() for _, r in df[mask].iterrows()]
