"""Metric resolver — `max_drawdown_pnl_pct` factory + its fixed-window
bindings (`max_drawdown_pnl_pct_30m`, `max_drawdown_pnl_pct_1h`). See
metric_mean_pnl.py's module docstring for the shared factory/binding
design rationale.
"""

from __future__ import annotations

from backend.api.algo.grammar import _w_key_pos


def _metric_factory_max_drawdown_pnl_pct(minutes):
    return lambda ctx, row: ctx.window_drawdown(_w_key_pos(row), minutes, field_idx=2)


_metric_max_drawdown_pnl_pct_30m = _metric_factory_max_drawdown_pnl_pct(30)
_metric_max_drawdown_pnl_pct_1h = _metric_factory_max_drawdown_pnl_pct(60)
