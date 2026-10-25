"""Metric resolver — `day_rate_abs`: rate of change of holdings day-change in ₹/min."""

from __future__ import annotations


def _metric_day_rate_abs(ctx, row):
    return ctx.rate_abs(('holdings', row.get('account')))
