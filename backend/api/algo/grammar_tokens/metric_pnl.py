"""Metric resolver — `pnl`: positions mark-to-market P&L in ₹."""

from __future__ import annotations


def _metric_pnl(ctx, row):
    """Positions P&L in ₹ (mark-to-market)."""
    return float(row.get('pnl', 0) or 0)
