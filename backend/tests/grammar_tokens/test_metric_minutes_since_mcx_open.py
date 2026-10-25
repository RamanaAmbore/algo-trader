"""Tests for grammar_tokens/metric_minutes_since_mcx_open.py —
`minutes_since_mcx_open` metric.

These resolvers must gate on the open flag rather than trusting the
raw `minutes_since_mcx_open` field, which `_ae_segment_flags` coerces
to 0 while closed (a diagnostic convention for its other callers) —
ungated, that 0 would read as "just opened" all night.

Moved verbatim from test_market_cycle_tokens.py's Layer 2
(TestMinutesSinceOpenGating, mcx cases) — Wave 1, batch 4 of the DSL
token-unification plan.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_minutes_since_mcx_open import (
    _metric_minutes_since_mcx_open,
)


def _ctx(segment_flags: dict | None = None, now=None):
    from backend.api.algo.agent_evaluator import Context
    return Context(segment_flags=segment_flags or {}, now=now)


def test_minutes_since_mcx_open_when_open():
    ctx = _ctx({"mcx_open": True, "minutes_since_mcx_open": 42})
    assert _metric_minutes_since_mcx_open(ctx, {}) == 42


def test_minutes_since_mcx_open_when_closed_is_none_not_zero():
    ctx = _ctx({"mcx_open": False, "minutes_since_mcx_open": 0})
    assert _metric_minutes_since_mcx_open(ctx, {}) is None
