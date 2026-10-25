"""Metric resolver — `ltp`: last-traded price, reused across watchlist/quote/position rows."""

from __future__ import annotations


def _metric_ltp(ctx, row):
    """Last-traded price for a watchlist row. Reused for any row that
    carries a `last_price` column — works on positions rows too."""
    return float(row.get('last_price', 0) or 0)
