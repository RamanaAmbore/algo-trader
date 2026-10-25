"""Scope resolver — `holdings.total`: the single TOTAL row of the
holdings summary."""

from __future__ import annotations


def _scope_holdings_total(ctx):
    """The single account-level TOTAL row from `ctx.sum_holdings` —
    the per-account holdings summary frame the background performance
    poller rebuilds each cycle. Empty list when the frame is absent or
    empty (no TOTAL row ever observed this tick)."""
    df = ctx.sum_holdings
    if df is None or df.empty:
        return []
    mask = df['account'].astype(str) == 'TOTAL'
    return [r.to_dict() for _, r in df[mask].iterrows()]
