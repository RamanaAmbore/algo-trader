"""Metric resolver — `theoretical_daily_loss`: today's theoretical P&L
move across the whole F&O book (leaf 3, "daily", of the 2026-11-28
`loss-positions-total` redesign)."""

from __future__ import annotations

from backend.api.algo.grammar import _portfolio_theoretical_loss_daily


def _metric_theoretical_daily_loss(ctx, row):
    """Σ of each F&O underlying's own delta-since-this-morning
    theoretical P&L move, skipping (not zero-filling) any underlying
    whose own value is unavailable this tick — see `_portfolio_
    theoretical_loss_daily`'s own docstring (grammar.py) for the full
    formula and missing-data rules."""
    return _portfolio_theoretical_loss_daily(ctx)
