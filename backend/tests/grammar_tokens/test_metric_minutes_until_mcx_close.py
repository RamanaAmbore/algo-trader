"""Tests for grammar_tokens/metric_minutes_until_mcx_close.py —
`minutes_until_mcx_close` metric.

Moved verbatim from test_market_cycle_tokens.py's Layer 2
(TestMinutesUntilCloseGating, mcx case) — Wave 1, batch 4 of the DSL
token-unification plan.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_minutes_until_mcx_close import (
    _metric_minutes_until_mcx_close,
)


def _ctx(segment_flags: dict | None = None, now=None):
    from backend.api.algo.agent_evaluator import Context
    return Context(segment_flags=segment_flags or {}, now=now)


def test_minutes_until_mcx_close_when_open():
    ctx = _ctx({"mcx_open": True, "minutes_until_mcx_close": 120})
    assert _metric_minutes_until_mcx_close(ctx, {}) == 120


def test_minutes_until_mcx_close_when_closed_is_none():
    ctx = _ctx({"mcx_open": False, "minutes_until_mcx_close": None})
    assert _metric_minutes_until_mcx_close(ctx, {}) is None
