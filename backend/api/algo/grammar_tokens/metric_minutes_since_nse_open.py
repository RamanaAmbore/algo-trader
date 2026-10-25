"""Metric resolver — `minutes_since_nse_open`. See metric_is_mcx_open.py's
module docstring for the segment_flags design."""

from __future__ import annotations


def _metric_minutes_since_nse_open(ctx, row):
    """Minutes since NSE's own session opened today. None while NSE is
    closed — see `_metric_minutes_since_mcx_open`'s docstring for why."""
    flags = ctx.segment_flags or {}
    if not flags.get('nse_open'):
        return None
    return flags.get('minutes_since_nse_open')
