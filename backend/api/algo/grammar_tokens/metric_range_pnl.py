"""Metric resolver — `range_pnl` factory + its fixed-window bindings
(`range_pnl_30m`, `range_pnl_1h`). See metric_mean_pnl.py's module
docstring for the shared factory/binding design rationale.
"""

from __future__ import annotations

from backend.api.algo.grammar import _w_key_pos


def _metric_factory_range_pnl(minutes):
    return lambda ctx, row: ctx.window_range(_w_key_pos(row), minutes)


_metric_range_pnl_30m = _metric_factory_range_pnl(30)
_metric_range_pnl_1h = _metric_factory_range_pnl(60)
