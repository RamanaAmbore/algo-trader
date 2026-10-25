"""Metric resolver — `used_margin`: used margin (Kite's `util debits`)."""

from __future__ import annotations

from backend.api.algo.grammar import _num_or_none


def _metric_used_margin(ctx, row):
    return _num_or_none(row.get('util debits'))
