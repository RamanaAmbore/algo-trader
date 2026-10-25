"""Metric resolver — `is_future`: 1.0 for a futures contract, 0.0 for an option."""

from __future__ import annotations

from backend.api.algo.grammar import _parsed_or_none


def _metric_is_future(ctx, row):
    """1.0 when the position is a futures contract (kind == 'fut'),
    0.0 when it is an option, None for equity or unrecognised symbols."""
    sym = row.get('tradingsymbol') or ''
    parsed = _parsed_or_none(sym)
    if not parsed:
        return None
    kind = parsed.get('kind')
    if kind == 'fut':
        return 1.0
    if kind == 'opt':
        return 0.0
    return None
