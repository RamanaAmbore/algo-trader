"""Tests for grammar_tokens/metric_minutes_since_nse_open.py —
`minutes_since_nse_open` metric.

Moved verbatim from test_market_cycle_tokens.py's Layer 2
(TestMinutesSinceOpenGating, nse case) — Wave 1, batch 4 of the DSL
token-unification plan. See test_metric_minutes_since_mcx_open.py's
module docstring for the open-flag-gating rationale.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_minutes_since_nse_open import (
    _metric_minutes_since_nse_open,
)


def _ctx(segment_flags: dict | None = None, now=None):
    from backend.api.algo.agent_evaluator import Context
    return Context(segment_flags=segment_flags or {}, now=now)


def test_minutes_since_nse_open_when_open():
    ctx = _ctx({"nse_open": True, "minutes_since_nse_open": 5})
    assert _metric_minutes_since_nse_open(ctx, {}) == 5


def test_minutes_since_nse_open_when_closed_is_none_not_zero():
    ctx = _ctx({"nse_open": False, "minutes_since_nse_open": 0})
    assert _metric_minutes_since_nse_open(ctx, {}) is None
