"""Tests for grammar_tokens/metric_max_drawdown_day.py —
`max_drawdown_day` factory + its fixed-window binding
(`max_drawdown_day_1h`).

No dedicated resolver-level test existed before this migration —
test_window_metrics.py's Phase 26 factory-comparison block covered
mean_pnl/mean_day/max_drawdown_pnl/max_drawdown_pnl_pct/stdev_pnl/
range_pnl but never max_drawdown_day. Fresh coverage, written as part
of Wave 1, batch 2 of the DSL token-unification plan.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_max_drawdown_day import (
    _metric_factory_max_drawdown_day, _metric_max_drawdown_day_1h,
)


def _ctx_with_history(samples, now=None):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    n = len(samples)
    history = [
        (now - timedelta(minutes=(n - 1 - i)), pnl, pct)
        for i, (pnl, pct) in enumerate(samples)
    ]
    return Context(alert_state={'pnl_history': {('holdings', 'ZG####'): history}}, now=now)


def test_max_drawdown_day_factory_matches_fixed_1h_resolver():
    # Holdings day P&L climbs to 200, then crashes to -50 → drawdown = -250.
    ctx = _ctx_with_history([(0, None), (100, None), (200, None), (50, None), (-50, None)])
    row = {'account': 'ZG####'}
    assert _metric_factory_max_drawdown_day(60)(ctx, row) == _metric_max_drawdown_day_1h(ctx, row)
    assert _metric_factory_max_drawdown_day(60)(ctx, row) == -250.0


def test_max_drawdown_day_monotonic_climb_is_zero():
    ctx = _ctx_with_history([(0, None), (50, None), (100, None)])
    row = {'account': 'ZG####'}
    assert _metric_factory_max_drawdown_day(60)(ctx, row) == 0.0
