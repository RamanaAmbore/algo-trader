"""Metric resolver — `is_expiry_preclose_close_window`: same shape as
`is_expiry_preclose_alert_window`, threshold 60 minutes instead of 65 —
5 minutes after the alert's own window. Retimed 2026-11-22 from 25/30 to
60/65 (operator: "I want the unnetted in the money positions to be
closed by the agents one hour before market close"). Drives the
order-placing agent's condition; the alert agent uses the sibling
65-minute token.

Produced by calling the SAME `_metric_factory_is_expiry_preclose_window
(lead_minutes)` factory the alert token uses, with a different minute
value — not a second copy of the exchange-loop logic (operator: "the
time should be a parameter in the function in the DSL grammar to make
it generic"). Both read the same underlying per-exchange facts via
`_expiry_preclose_within()`, so the two thresholds can never diverge
into separate exchange-loop logic.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
    _metric_factory_is_expiry_preclose_window,
)

CLOSE_LEAD_MINUTES = 60

_metric_is_expiry_preclose_close_window = _metric_factory_is_expiry_preclose_window(
    CLOSE_LEAD_MINUTES
)
