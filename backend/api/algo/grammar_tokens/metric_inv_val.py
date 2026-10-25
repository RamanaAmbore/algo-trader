"""Metric resolver — `inv_val`: holdings invested value in ₹."""

from __future__ import annotations


def _metric_inv_val(ctx, row):
    return float(row.get('inv_val', 0) or 0)
