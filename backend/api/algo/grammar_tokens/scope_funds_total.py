"""Scope resolver — `funds.total`: the single TOTAL row of the
funds/margins dataframe."""

from __future__ import annotations


def _scope_funds_total(ctx):
    """The single account-level TOTAL row from `ctx.df_margins` — the
    per-account funds/margins frame the background performance poller
    rebuilds each cycle. Empty list when the frame is absent or empty."""
    df = ctx.df_margins
    if df is None or df.empty:
        return []
    mask = df['account'].astype(str) == 'TOTAL'
    return [r.to_dict() for _, r in df[mask].iterrows()]
