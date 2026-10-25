"""Metric resolver — `sod_cash`: start-of-day cash balance."""

from __future__ import annotations

from backend.api.algo.grammar import _num_or_none


def _metric_sod_cash(ctx, row):
    """Start-of-day cash (the value `cash` used to read before fix #4).
    Kept as its own token for agents that specifically want the SOD
    baseline rather than live intraday cash."""
    return _num_or_none(row.get('avail opening_balance'))
