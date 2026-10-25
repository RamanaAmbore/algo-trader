"""Metric resolver — `cur_val`: holdings current value in ₹."""

from __future__ import annotations


def _metric_cur_val(ctx, row):
    return float(row.get('cur_val', 0) or 0)
