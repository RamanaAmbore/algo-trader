"""Metric resolver — `theo_total_rate_abs`: rate of change of leaf 1's
own theoretical-total-loss value, in ₹/min (leaf 2 of the 2026-11-28
`loss-positions-total` redesign)."""

from __future__ import annotations

from backend.api.algo.grammar import PORTFOLIO_THEO_TOTAL_RATE_KEY


def _metric_theo_total_rate_abs(ctx, row):
    return ctx.rate_abs(PORTFOLIO_THEO_TOTAL_RATE_KEY)
