"""Tests for grammar_tokens/metric_range_pnl.py — `range_pnl` factory +
its fixed-window bindings (`range_pnl_30m`, `range_pnl_1h`).

Moved verbatim from test_window_metrics.py (Wave 1, batch 2 of the DSL
token-unification plan).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_range_pnl import (
    _metric_factory_range_pnl, _metric_range_pnl_30m, _metric_range_pnl_1h,
)


def _ctx_with_history(samples, now=None):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    n = len(samples)
    history = [
        (now - timedelta(minutes=(n - 1 - i)), pnl, pct)
        for i, (pnl, pct) in enumerate(samples)
    ]
    return Context(alert_state={'pnl_history': {('positions', 'ZG####'): history}}, now=now)


def test_range_pnl_factory_matches_fixed_30m_resolver():
    ctx = _ctx_with_history([(50, None), (-30, None), (10, None), (100, None)])
    row = {'account': 'ZG####'}
    assert _metric_factory_range_pnl(30)(ctx, row) == _metric_range_pnl_30m(ctx, row)
    assert _metric_factory_range_pnl(30)(ctx, row) == 130.0


def test_range_pnl_1h_binding_matches_factory():
    ctx = _ctx_with_history([(50, None), (-30, None), (10, None), (100, None)])
    row = {'account': 'ZG####'}
    assert _metric_factory_range_pnl(60)(ctx, row) == _metric_range_pnl_1h(ctx, row)
