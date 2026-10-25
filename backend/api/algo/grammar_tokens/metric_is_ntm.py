"""Metric resolver — `is_ntm`: 1.0 when an option is near-the-money, else 0.0."""

from __future__ import annotations

from backend.api.algo.grammar import _option_moneyness


def _metric_is_ntm(ctx, row):
    """1.0 when the option is within ±1.5% of spot (near-the-money),
    0.0 otherwise. The 1.5% threshold matches the legacy ExpiryEngine
    default. None when row isn't an option or spot is unavailable."""
    kind, intrinsic = _option_moneyness(ctx, row)
    if kind is None:
        return None
    return 1.0 if abs(intrinsic) <= 0.015 else 0.0
