"""Metric resolver — `is_expiry_preclose_close_window`: same shape as
`is_expiry_preclose_alert_window`, threshold 25 minutes instead of 30 —
5 minutes after the alert's own window, per operator instruction ("alert
agent 30 mins before expiry market closure, close agents 25 mins
before"). Drives the order-placing agent's condition; the alert agent
uses the sibling 30-minute token. Both read the same underlying
per-exchange facts via `_expiry_preclose_within()`, so the two
thresholds can never diverge into separate exchange-loop logic.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
    _expiry_preclose_within,
)

CLOSE_LEAD_MINUTES = 25


def _metric_is_expiry_preclose_close_window(ctx, row):
    """1.0 when either exchange is expiring today and within 25 minutes
    of its own close, else 0.0. Never None."""
    return 1.0 if _expiry_preclose_within(ctx, row, CLOSE_LEAD_MINUTES) else 0.0
