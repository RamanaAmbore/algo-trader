"""Metric resolver — `minutes_since_mcx_open`. See metric_is_mcx_open.py's
module docstring for the segment_flags design."""

from __future__ import annotations


def _metric_minutes_since_mcx_open(ctx, row):
    """Minutes since MCX's own session opened today. None while MCX is
    closed — `_ae_segment_flags` coerces its `minutes_since_mcx_open`
    field to 0 while closed (a diagnostic convention for that function's
    other callers), which would otherwise read as "just opened" all
    night; gate on the open flag here instead of trusting the raw 0."""
    flags = ctx.segment_flags or {}
    if not flags.get('mcx_open'):
        return None
    return flags.get('minutes_since_mcx_open')
