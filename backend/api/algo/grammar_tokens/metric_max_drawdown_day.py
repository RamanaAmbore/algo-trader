"""Metric resolver — `max_drawdown_day` factory + its fixed-window binding
(`max_drawdown_day_1h`). See metric_mean_pnl.py's module docstring for
the shared factory/binding design rationale.
"""

from __future__ import annotations

from backend.api.algo.grammar import _w_key_hold


def _metric_factory_max_drawdown_day(minutes):
    return lambda ctx, row: ctx.window_drawdown(_w_key_hold(row), minutes)


_metric_max_drawdown_day_1h = _metric_factory_max_drawdown_day(60)
