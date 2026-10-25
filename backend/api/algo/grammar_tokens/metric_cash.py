"""Metric resolver — `cash`: live available cash."""

from __future__ import annotations

from backend.api.algo.grammar import _num_or_none


def _metric_cash(ctx, row):
    """Live available cash (fix #4 — was reading start-of-day
    'avail opening_balance', which cannot move intraday, so
    loss-funds-negative's cash<0 leaf could never fire on a real
    intraday cash drop). 'avail cash' is the same column
    `routes/funds.py` maps to `live_cash` (Kite's `available.cash` /
    Dhan+Groww adapters' normalised `available.cash`, both None-safe —
    see `dhan.py:_dhan_margins_available` / `groww.py:_groww_margin_available`).
    Kept the token name `cash` (not renamed) so existing agent rows
    (prod's `loss-funds-negative`) get the corrected live-cash semantics
    without an operator having to re-save the agent — see `sod_cash`
    for the retained start-of-day reading."""
    return _num_or_none(row.get('avail cash'))
