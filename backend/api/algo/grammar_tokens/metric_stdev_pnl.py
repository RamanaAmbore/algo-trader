"""Metric resolver — `stdev_pnl` factory + its fixed-window bindings
(`stdev_pnl_30m`, `stdev_pnl_1h`). See metric_mean_pnl.py's module
docstring for the shared factory/binding design rationale.
"""

from __future__ import annotations

from backend.api.algo.grammar import _w_key_pos


def _metric_factory_stdev_pnl(minutes):
    return lambda ctx, row: ctx.window_stdev(_w_key_pos(row), minutes)


_metric_stdev_pnl_30m = _metric_factory_stdev_pnl(30)
_metric_stdev_pnl_1h = _metric_factory_stdev_pnl(60)
