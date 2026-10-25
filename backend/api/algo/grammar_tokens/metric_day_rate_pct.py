"""Metric resolver — `day_rate_pct`: rate of change of holdings day-change in %/min."""

from __future__ import annotations


def _metric_day_rate_pct(ctx, row):
    return ctx.rate_pct(('holdings', row.get('account')))
