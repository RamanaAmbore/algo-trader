"""Metric resolver — `max_drawdown_pnl` factory + its fixed-window bindings
(`max_drawdown_pnl_30m`, `max_drawdown_pnl_1h`, `max_drawdown_pnl_4h`).
See metric_mean_pnl.py's module docstring for the shared factory/binding
design rationale.
"""

from __future__ import annotations

from backend.api.algo.grammar import _w_key_pos


def _metric_factory_max_drawdown_pnl(minutes):
    return lambda ctx, row: ctx.window_drawdown(_w_key_pos(row), minutes)


_metric_max_drawdown_pnl_30m = _metric_factory_max_drawdown_pnl(30)
_metric_max_drawdown_pnl_1h = _metric_factory_max_drawdown_pnl(60)
_metric_max_drawdown_pnl_4h = _metric_factory_max_drawdown_pnl(240)
