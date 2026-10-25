"""Metric resolver — `minutes_until_nse_open`. See metric_is_mcx_open.py's
module docstring for the segment_flags design."""

from __future__ import annotations


def _metric_minutes_until_nse_open(ctx, row):
    """Minutes until NSE's own session opens today — 0 while already
    open; None after today's close or on a day with no session at all
    (see `_ae_segment_flags`'s own computation)."""
    return (ctx.segment_flags or {}).get('minutes_until_nse_open')
