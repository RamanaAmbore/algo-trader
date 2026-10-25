"""Metric resolver — `mean_day` factory + its fixed-window bindings
(`mean_day_30m`, `mean_day_1h`). See metric_mean_pnl.py's module
docstring for the shared factory/binding design rationale.
"""

from __future__ import annotations

from backend.api.algo.grammar import _w_key_hold


def _metric_factory_mean_day(minutes):
    return lambda ctx, row: ctx.window_mean(_w_key_hold(row), minutes)


_metric_mean_day_30m = _metric_factory_mean_day(30)
_metric_mean_day_1h = _metric_factory_mean_day(60)
