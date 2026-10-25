"""Tests for grammar_tokens/metric_minutes_until_nse_open.py —
`minutes_until_nse_open` metric.

Moved verbatim from test_market_cycle_tokens.py's Layer 2
(TestMinutesUntilOpen, nse case) — Wave 1, batch 4 of the DSL
token-unification plan.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_minutes_until_nse_open import (
    _metric_minutes_until_nse_open,
)


def _ctx(segment_flags: dict | None = None, now=None):
    from backend.api.algo.agent_evaluator import Context
    return Context(segment_flags=segment_flags or {}, now=now)


def test_minutes_until_nse_open_none_when_no_session_today():
    ctx = _ctx({"minutes_until_nse_open": None})
    assert _metric_minutes_until_nse_open(ctx, {}) is None


def test_minutes_until_nse_open_zero_when_already_open():
    ctx = _ctx({"minutes_until_nse_open": 0})
    assert _metric_minutes_until_nse_open(ctx, {}) == 0
