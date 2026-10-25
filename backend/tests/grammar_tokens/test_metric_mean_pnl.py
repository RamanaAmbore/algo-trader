"""Tests for grammar_tokens/metric_mean_pnl.py — `mean_pnl` factory +
its fixed-window bindings (`mean_pnl_30m`, `mean_pnl_1h`).

Moved verbatim from test_window_metrics.py (Wave 1, batch 2 of the DSL
token-unification plan) — proves the factory's math, on real
pnl_history, matches the pre-existing fixed-window binding exactly (the
fixed tokens are one-line bindings to this same factory, not a
separate implementation that happens to agree).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_mean_pnl import (
    _metric_factory_mean_pnl, _metric_mean_pnl_30m, _metric_mean_pnl_1h,
)


def _ctx_with_history(samples, now=None):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    n = len(samples)
    history = [
        (now - timedelta(minutes=(n - 1 - i)), pnl, pct)
        for i, (pnl, pct) in enumerate(samples)
    ]
    return Context(alert_state={'pnl_history': {('positions', 'ZG####'): history}}, now=now)


def test_mean_pnl_factory_matches_fixed_30m_resolver():
    ctx = _ctx_with_history([(100, None), (200, None), (300, None)])
    row = {'account': 'ZG####'}
    assert _metric_factory_mean_pnl(30)(ctx, row) == _metric_mean_pnl_30m(ctx, row)
    assert _metric_factory_mean_pnl(30)(ctx, row) == 200.0


def test_mean_pnl_1h_binding_matches_factory():
    ctx = _ctx_with_history([(0, None), (10, None), (20, None)])
    row = {'account': 'ZG####'}
    assert _metric_factory_mean_pnl(60)(ctx, row) == _metric_mean_pnl_1h(ctx, row)
