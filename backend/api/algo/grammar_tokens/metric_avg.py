"""Metric resolver factory — `avg(<column>)`: mean value of an exposed
numeric column across the current scope snapshot's rows.

A ROW-SET AGGREGATE, same family as `count`/`sum`/`max` — see
`metric_count.py`'s module docstring for the shared contract.
`rowset_numeric_values` (the column-read + no-partial-reduction rule) is
shared verbatim from `metric_sum.py`, not duplicated."""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_sum import rowset_numeric_values
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def _metric_factory_avg(column):
    def _reduce(ctx, rows, scope_tok):
        values, ok = rowset_numeric_values(rows, scope_tok, column)
        if not ok:
            logger.warning(
                f"avg({column!r})@{scope_tok}: '{column}' is not a "
                f"number-typed exposed column on this scope, or a row's "
                f"value is missing/NaN — leaf skipped"
            )
            return None
        if not values:
            # Filtered to a confirmed-empty set — average of nothing is
            # undefined, never a coerced 0.
            return None
        return float(sum(values)) / len(values)
    _reduce.rowset_aggregate = True
    _reduce.column = column
    return _reduce
