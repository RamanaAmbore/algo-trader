"""Metric resolver — `is_itm`: 1.0 when an option is in-the-money, else 0.0."""

from __future__ import annotations

from backend.api.algo.grammar import _option_moneyness


def _metric_is_itm(ctx, row):
    """1.0 when the option is in-the-money at current spot, 0.0
    otherwise. None when row isn't an option or spot is unavailable."""
    kind, intrinsic = _option_moneyness(ctx, row)
    if kind is None:
        return None
    return 1.0 if intrinsic > 0 else 0.0
