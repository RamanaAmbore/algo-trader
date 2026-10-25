"""Metric resolver — `is_nse_open`: 1.0/0.0 from ctx.segment_flags['nse_open'].
See metric_is_mcx_open.py's module docstring for the segment_flags design."""

from __future__ import annotations


def _metric_is_nse_open(ctx, row):
    """1.0/0.0 from `ctx.segment_flags['nse_open']`. None when unset."""
    v = (ctx.segment_flags or {}).get('nse_open')
    if v is None:
        return None
    return 1.0 if v else 0.0
