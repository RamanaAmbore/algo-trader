"""Tests for grammar_tokens/metric_is_mcx_open.py — `is_mcx_open` metric.

Moved verbatim from test_market_cycle_tokens.py's Layer 2
(TestIsOpenTokens, is_mcx_open cases) — Wave 1, batch 4 of the DSL
token-unification plan. Layer 1 (`_ae_segment_flags` boundary tests)
and Layer 3 (end-to-end REGISTRY/evaluator wiring) stay in
test_market_cycle_tokens.py — this file isolates the resolver's own
logic on a hand-built Context, per that file's own 3-layer design note.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_is_mcx_open import _metric_is_mcx_open


def _ctx(segment_flags: dict | None = None, now=None):
    from backend.api.algo.agent_evaluator import Context
    return Context(segment_flags=segment_flags or {}, now=now)


def test_is_mcx_open_true():
    assert _metric_is_mcx_open(_ctx({"mcx_open": True}), {}) == 1.0


def test_is_mcx_open_false():
    assert _metric_is_mcx_open(_ctx({"mcx_open": False}), {}) == 0.0


def test_is_mcx_open_missing_is_none():
    assert _metric_is_mcx_open(_ctx({}), {}) is None
