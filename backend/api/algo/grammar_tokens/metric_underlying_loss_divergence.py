"""Metric resolver — `underlying_loss_divergence`: abs(theoretical −
market) day loss for one underlying."""

from __future__ import annotations

from backend.api.algo.grammar import _underlying_theoretical_loss
from backend.api.algo.grammar_tokens.metric_underlying_market_loss import (
    _metric_underlying_market_loss,
)


def _metric_underlying_loss_divergence(ctx, row):
    """abs(theoretical − market) day loss for this underlying. None when
    either side is unavailable — never a false 0 divergence that could
    silently fail to gate a downstream rate-of-change leaf closed."""
    underlying = row.get('underlying')
    theo = _underlying_theoretical_loss(ctx, underlying)
    if theo is None:
        return None
    market = _metric_underlying_market_loss(ctx, row)
    if market is None:
        return None
    return abs(theo - market)
