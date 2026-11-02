"""Metric resolver factory — `sum(<column>)`: sum of an exposed numeric
column across the current scope snapshot's rows.

A ROW-SET AGGREGATE, same family as `count`/`max`/`avg` — see
`metric_count.py`'s module docstring for the shared `(ctx, rows,
scope_tok) -> value` contract and why this collapses a leaf to ONE
result regardless of scope suffix.

`<column>` must be a real NUMBER-typed exposed column on the leaf's OWN
scope (`grammar.exposed_columns_for_scope(scope_tok)`) — checked FRESH
on every evaluation, never assumed from the literal call string alone,
because the factory is bound (and cached forever, per-literal-string) by
`GrammarRegistry._resolve_call_token` with NO scope context at bind
time; the same `sum(qty)` call string could in principle be referenced
from a leaf scoped to a DIFFERENT scope token on a different agent."""

from __future__ import annotations

from backend.api.algo import grammar
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def rowset_numeric_values(rows, scope_tok, column):
    """Shared column-read helper for `sum`/`max`/`avg`.

    Returns `(values, ok)`. `ok=False` means `column` isn't a real
    NUMBER-typed exposed column on `scope_tok` (unknown column, or a
    string/other-typed one). A `None`/NaN cell on ANY row makes the
    WHOLE reduction unavailable (`ok=False`) rather than silently
    dropping just that one row — the same no-partial-reduction rule
    `grammar._underlying_theoretical_loss` already uses: a partial
    sum/max/avg computed over only the SOME of the rows that happened
    to have a value would understate or misrepresent the true
    aggregate if presented as if it covered every row."""
    schema = grammar.exposed_columns_for_scope(scope_tok)
    col = schema.get(column)
    if not col or col.get('type', 'number') != 'number':
        return None, False
    values = []
    for row in rows:
        v = grammar.read_exposed_value(row, col)
        if v is None:
            return None, False
        values.append(v)
    return values, True


def _metric_factory_sum(column):
    def _reduce(ctx, rows, scope_tok):
        values, ok = rowset_numeric_values(rows, scope_tok, column)
        if not ok:
            logger.warning(
                f"sum({column!r})@{scope_tok}: '{column}' is not a "
                f"number-typed exposed column on this scope, or a row's "
                f"value is missing/NaN — leaf skipped"
            )
            return None
        # sum([]) == 0.0 — a genuine, confirmed zero after filtering
        # (see metric_count.py's docstring for the "raw_rows empty"
        # vs "filtered-to-empty" distinction this relies on).
        return float(sum(values))
    _reduce.rowset_aggregate = True
    _reduce.column = column
    return _reduce
