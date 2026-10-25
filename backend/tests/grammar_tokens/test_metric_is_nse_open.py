"""Tests for grammar_tokens/metric_is_nse_open.py — `is_nse_open` metric.

Moved verbatim from test_market_cycle_tokens.py's Layer 2
(TestIsOpenTokens, is_nse_open cases) — Wave 1, batch 4 of the DSL
token-unification plan. See test_metric_is_mcx_open.py's module
docstring for the 3-layer design note.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_is_nse_open import _metric_is_nse_open


def _ctx(segment_flags: dict | None = None, now=None):
    from backend.api.algo.agent_evaluator import Context
    return Context(segment_flags=segment_flags or {}, now=now)


def test_is_nse_open_true():
    assert _metric_is_nse_open(_ctx({"nse_open": True}), {}) == 1.0


def test_is_nse_open_false():
    assert _metric_is_nse_open(_ctx({"nse_open": False}), {}) == 0.0


def test_is_nse_open_missing_is_none():
    assert _metric_is_nse_open(_ctx({}), {}) is None
