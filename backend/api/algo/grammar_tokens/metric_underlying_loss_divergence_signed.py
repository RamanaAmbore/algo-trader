"""Metric resolver — `underlying_loss_divergence_signed`: signed
(market − theoretical) day loss for one underlying.

2026-11-26 (operator: "if the rate of change towards exp max profit, that
is okay. if it is away, that needs to be alerted") — a SIGNED sibling of
`underlying_loss_divergence` (which stays unsigned, unchanged, in case a
future custom agent reads it). Negative means the underlying's actual day
P&L is UNDERPERFORMING its own Greeks/theta-model-predicted path (the
adverse, alert-worthy direction — moving away from the expected decay
trajectory). Positive means it is OUTPERFORMING the model (benign, never
the alert-worthy direction here, however large the gap).

Reuses `_underlying_theoretical_loss`/`_metric_underlying_market_loss`
unchanged — these already correctly account for elapsed-time decay via the
frozen-vol Black-Scholes/Black-76 reprice, so this is NOT blind to theta/
vega bleed the way a bare intrinsic-value-at-expiry metric would be (see
`~/.claude/plans/lovely-stirring-allen.md` for the 6-perspective council
review that ruled out that simpler-looking alternative).
"""

from __future__ import annotations

from backend.api.algo.grammar import _underlying_theoretical_loss
from backend.api.algo.grammar_tokens.metric_underlying_market_loss import (
    _metric_underlying_market_loss,
)


def _metric_underlying_loss_divergence_signed(ctx, row):
    """(market − theoretical) day loss for this underlying, signed. None
    when either side is unavailable — never a false 0, which would read
    as "no divergence" and silently fail to gate a downstream
    rate-of-change leaf closed."""
    underlying = row.get('underlying')
    theo = _underlying_theoretical_loss(ctx, underlying)
    if theo is None:
        return None
    market = _metric_underlying_market_loss(ctx, row)
    if market is None:
        return None
    return market - theo
