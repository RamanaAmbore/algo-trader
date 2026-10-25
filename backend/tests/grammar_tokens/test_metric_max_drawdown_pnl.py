"""Tests for grammar_tokens/metric_max_drawdown_pnl.py — `max_drawdown_pnl`
factory + its fixed-window bindings (`max_drawdown_pnl_30m`,
`max_drawdown_pnl_1h`, `max_drawdown_pnl_4h`).

Moved verbatim from test_window_metrics.py (Wave 1, batch 2 of the DSL
token-unification plan).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_max_drawdown_pnl import (
    _metric_factory_max_drawdown_pnl, _metric_max_drawdown_pnl_1h,
    _metric_max_drawdown_pnl_30m, _metric_max_drawdown_pnl_4h,
)


def _ctx_with_history(samples, now=None):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    n = len(samples)
    history = [
        (now - timedelta(minutes=(n - 1 - i)), pnl, pct)
        for i, (pnl, pct) in enumerate(samples)
    ]
    return Context(alert_state={'pnl_history': {('positions', 'ZG####'): history}}, now=now)


def test_max_drawdown_pnl_factory_matches_fixed_1h_resolver():
    ctx = _ctx_with_history([(0, None), (100, None), (200, None), (50, None), (-50, None)])
    row = {'account': 'ZG####'}
    assert _metric_factory_max_drawdown_pnl(60)(ctx, row) == _metric_max_drawdown_pnl_1h(ctx, row)
    assert _metric_factory_max_drawdown_pnl(60)(ctx, row) == -250.0


def test_max_drawdown_pnl_30m_and_4h_bindings_match_factory():
    ctx = _ctx_with_history([(0, None), (100, None), (200, None), (50, None), (-50, None)])
    row = {'account': 'ZG####'}
    assert _metric_factory_max_drawdown_pnl(30)(ctx, row) == _metric_max_drawdown_pnl_30m(ctx, row)
    assert _metric_factory_max_drawdown_pnl(240)(ctx, row) == _metric_max_drawdown_pnl_4h(ctx, row)
