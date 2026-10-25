"""Tests for grammar_tokens/metric_minutes_until_mcx_open.py —
`minutes_until_mcx_open` metric.

Moved verbatim from test_market_cycle_tokens.py's Layer 2
(TestMinutesUntilOpen, mcx cases) — Wave 1, batch 4 of the DSL
token-unification plan.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_minutes_until_mcx_open import (
    _metric_minutes_until_mcx_open,
)


def _ctx(segment_flags: dict | None = None, now=None):
    from backend.api.algo.agent_evaluator import Context
    return Context(segment_flags=segment_flags or {}, now=now)


def test_minutes_until_mcx_open_passthrough():
    ctx = _ctx({"minutes_until_mcx_open": 10})
    assert _metric_minutes_until_mcx_open(ctx, {}) == 10


def test_minutes_until_mcx_open_zero_when_already_open():
    ctx = _ctx({"minutes_until_mcx_open": 0})
    assert _metric_minutes_until_mcx_open(ctx, {}) == 0
