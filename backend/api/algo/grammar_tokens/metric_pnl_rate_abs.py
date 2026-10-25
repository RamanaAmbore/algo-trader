"""Metric resolver — `pnl_rate_abs`: rate of change of positions P&L in ₹/min."""

from __future__ import annotations

from backend.api.algo.grammar import _pnl_rate_key


def _metric_pnl_rate_abs(ctx, row):
    return ctx.rate_abs(_pnl_rate_key(row))
