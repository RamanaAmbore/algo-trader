"""Metric resolver — `day_val`: holdings/positions day-change value in ₹."""

from __future__ import annotations


def _metric_day_val(ctx, row):
    """Holdings day-change value in ₹."""
    return float(row.get('day_change_val', 0) or 0)
