"""Metric resolver — `pnl_rate_pct`: rate of change of positions P&L in %/min."""

from __future__ import annotations

from backend.api.algo.grammar import _pnl_rate_key


def _metric_pnl_rate_pct(ctx, row):
    """See `_update_underlying_pnl_history`'s docstring — the pct slot is
    deliberately recorded as None for the `positions_underlying`
    namespace (no per-underlying margin denominator exists), so this
    always returns None for `positions.any_underlying` rows. Use
    `pnl_rate_abs` for underlying-scoped rate conditions."""
    return ctx.rate_pct(_pnl_rate_key(row))
