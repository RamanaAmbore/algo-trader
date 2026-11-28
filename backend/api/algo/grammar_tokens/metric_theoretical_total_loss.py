"""Metric resolver — `theoretical_total_loss`: lifetime-since-entry
theoretical shortfall across the whole F&O book (leaf 1, "total", of the
2026-11-28 `loss-positions-total` redesign)."""

from __future__ import annotations

from backend.api.algo.grammar import _portfolio_theoretical_loss_since_entry


def _metric_theoretical_total_loss(ctx, row):
    """Σ qty_base × (model_price_now − avg_cost) across every currently-
    held F&O leg, skipping (not zero-filling) any underlying whose own
    value is unavailable this tick — see `_portfolio_theoretical_loss_
    since_entry`'s own docstring (grammar.py) for the full formula and
    missing-data rules."""
    return _portfolio_theoretical_loss_since_entry(ctx)
