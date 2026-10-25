"""Scope resolver — `positions.total`: the single TOTAL row of the
positions summary."""

from __future__ import annotations

from backend.api.algo.grammar import _log_partial_outage_suppression


def _scope_positions_total(ctx):
    """The single account-level TOTAL row from `ctx.sum_positions` —
    the per-account positions summary frame the background performance
    poller rebuilds each cycle.

    Partial-outage guard (2026-09 alerts audit item 5): when
    `background._fetch_positions_direct` detected that some (not all)
    accounts failed to fetch this tick, `df`'s TOTAL row silently omits
    the failed account(s)' contribution — pd.concat just drops what
    never arrived. Returning [] here means the evaluator records NO
    observation for any positions.total leaf this tick (fix #5's
    "absent from observations = untouched, never treated as
    recovered" contract), rather than firing/clearing latches off a
    P&L total that's silently missing money.
    """
    df = ctx.sum_positions
    if df is None or df.empty:
        return []
    accts = (getattr(df, 'attrs', {}) or {}).get('partial_outage')
    if accts:
        _log_partial_outage_suppression(accts)
        return []
    mask = df['account'].astype(str) == 'TOTAL'
    return [r.to_dict() for _, r in df[mask].iterrows()]
