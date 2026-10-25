"""Metric resolver — `underlying_theoretical_loss`: baseline-reprice
theoretical P&L for every currently-held leg of one underlying."""

from __future__ import annotations

from backend.api.algo.grammar import _underlying_theoretical_loss


def _metric_underlying_theoretical_loss(ctx, row):
    """Baseline-reprice theoretical P&L for this underlying — see
    `_underlying_theoretical_loss`'s module-level docstring (grammar.py)
    for the full formula and missing-data rules."""
    return _underlying_theoretical_loss(ctx, row.get('underlying'))
