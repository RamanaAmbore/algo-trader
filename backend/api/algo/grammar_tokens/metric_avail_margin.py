"""Metric resolver — `avail_margin`: available margin (Kite's `net`)."""

from __future__ import annotations

from backend.api.algo.grammar import _num_or_none


def _metric_avail_margin(ctx, row):
    return _num_or_none(row.get('net'))
