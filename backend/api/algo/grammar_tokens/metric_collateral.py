"""Metric resolver — `collateral`: pledged collateral (Kite's `avail collateral`)."""

from __future__ import annotations

from backend.api.algo.grammar import _num_or_none


def _metric_collateral(ctx, row):
    return _num_or_none(row.get('avail collateral'))
