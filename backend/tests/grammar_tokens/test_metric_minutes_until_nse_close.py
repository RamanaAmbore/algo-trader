"""Tests for grammar_tokens/metric_minutes_until_nse_close.py —
`minutes_until_nse_close` metric.

Moved verbatim from test_market_cycle_tokens.py's Layer 2
(TestMinutesUntilCloseGating, nse case) — Wave 1, batch 4 of the DSL
token-unification plan.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_minutes_until_nse_close import (
    _metric_minutes_until_nse_close,
)


def _ctx(segment_flags: dict | None = None, now=None):
    from backend.api.algo.agent_evaluator import Context
    return Context(segment_flags=segment_flags or {}, now=now)


def test_minutes_until_nse_close_when_open():
    ctx = _ctx({"nse_open": True, "minutes_until_nse_close": 15})
    assert _metric_minutes_until_nse_close(ctx, {}) == 15


def test_minutes_until_nse_close_when_closed_is_none():
    ctx = _ctx({"nse_open": False, "minutes_until_nse_close": None})
    assert _metric_minutes_until_nse_close(ctx, {}) is None
