"""Tests for grammar_tokens/metric_max_drawdown_pnl_pct.py —
`max_drawdown_pnl_pct` factory + its fixed-window bindings
(`max_drawdown_pnl_pct_30m`, `max_drawdown_pnl_pct_1h`).

Moved verbatim from test_window_metrics.py (Wave 1, batch 2 of the DSL
token-unification plan).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_max_drawdown_pnl_pct import (
    _metric_factory_max_drawdown_pnl_pct, _metric_max_drawdown_pnl_pct_30m,
    _metric_max_drawdown_pnl_pct_1h,
)


def _ctx_with_history(samples, now=None):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    n = len(samples)
    history = [
        (now - timedelta(minutes=(n - 1 - i)), pnl, pct)
        for i, (pnl, pct) in enumerate(samples)
    ]
    return Context(alert_state={'pnl_history': {('positions', 'ZG####'): history}}, now=now)


def test_max_drawdown_pnl_pct_factory_matches_fixed_30m_resolver():
    ctx = _ctx_with_history([(0, 0.0), (0, 10.0), (0, 20.0), (0, 5.0), (0, -5.0)])
    row = {'account': 'ZG####'}
    assert _metric_factory_max_drawdown_pnl_pct(30)(ctx, row) == \
        _metric_max_drawdown_pnl_pct_30m(ctx, row)
    assert _metric_factory_max_drawdown_pnl_pct(30)(ctx, row) == -25.0


def test_max_drawdown_pnl_pct_1h_binding_matches_factory():
    ctx = _ctx_with_history([(0, 0.0), (0, 10.0), (0, 20.0), (0, 5.0), (0, -5.0)])
    row = {'account': 'ZG####'}
    assert _metric_factory_max_drawdown_pnl_pct(60)(ctx, row) == \
        _metric_max_drawdown_pnl_pct_1h(ctx, row)
