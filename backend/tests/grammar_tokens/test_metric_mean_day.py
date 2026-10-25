"""Tests for grammar_tokens/metric_mean_day.py — `mean_day` factory +
its fixed-window bindings (`mean_day_30m`, `mean_day_1h`).

Moved verbatim from test_window_metrics.py (Wave 1, batch 2 of the DSL
token-unification plan).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_mean_day import (
    _metric_factory_mean_day, _metric_mean_day_30m, _metric_mean_day_1h,
)


def test_mean_day_factory_matches_fixed_30m_resolver():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    history = [
        (now - timedelta(minutes=2), 10, None),
        (now - timedelta(minutes=1), 20, None),
        (now, 30, None),
    ]
    ctx = Context(alert_state={'pnl_history': {('holdings', 'ZG####'): history}}, now=now)
    row = {'account': 'ZG####'}
    assert _metric_factory_mean_day(30)(ctx, row) == _metric_mean_day_30m(ctx, row)
    assert _metric_factory_mean_day(30)(ctx, row) == 20.0


def test_mean_day_1h_binding_matches_factory():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    history = [
        (now - timedelta(minutes=2), 10, None),
        (now - timedelta(minutes=1), 20, None),
        (now, 30, None),
    ]
    ctx = Context(alert_state={'pnl_history': {('holdings', 'ZG####'): history}}, now=now)
    row = {'account': 'ZG####'}
    assert _metric_factory_mean_day(60)(ctx, row) == _metric_mean_day_1h(ctx, row)
