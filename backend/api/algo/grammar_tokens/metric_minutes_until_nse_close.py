"""Metric resolver — `minutes_until_nse_close`. See metric_is_mcx_open.py's
module docstring for the segment_flags design."""

from __future__ import annotations


def _metric_minutes_until_nse_close(ctx, row):
    """Minutes until NSE's own session closes today. None while NSE is
    closed (nothing to count down to)."""
    flags = ctx.segment_flags or {}
    if not flags.get('nse_open'):
        return None
    return flags.get('minutes_until_nse_close')
