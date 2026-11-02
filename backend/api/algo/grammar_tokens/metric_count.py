"""Metric resolver — `count`: number of rows in the current scope
snapshot (after any `scope_filter`/`where` has already narrowed them).

A ROW-SET AGGREGATE (DSL dataframe-access gaps, item 3) — unlike every
other metric resolver in this package, its callable signature is
`(ctx, rows, scope_tok) -> value`, not `(ctx, row) -> value`: it reduces
ACROSS the whole scope snapshot once per leaf evaluation, never once per
row. `agent_evaluator._eval_leaf` detects this via the `rowset_aggregate`
attribute set below and branches to `_eval_leaf_rowset_aggregate` instead
of the ordinary per-row loop — collapsing to AT MOST one result,
regardless of the underlying scope's own suffix. See that module's
section comment for the full contract, and
`agent_engine._ae_is_aggregate_match`'s `rowset_aggregate` check, which
makes a `count`-metric leaf fail `$row` closed exactly like a
`.total`-scoped leaf.

`count` takes no arguments — called with no parentheses, e.g.
`count@positions.expiring_today(exchange == "MCX") > 5` (NOT
`positions.any_acct` — that scope is the account-SUMMARY frame and has
no `exchange`/`qty` columns to filter on at all). `exists(...)` is
deliberately NOT a separate primitive — it is `count(...) > 0`,
expressible with this one metric plus the where-filter (item 2)."""

from __future__ import annotations


def _metric_count(ctx, rows, scope_tok):
    """Number of rows in `rows`. `agent_evaluator._eval_leaf_rowset_
    aggregate` only calls this when the scope itself returned at least
    one row this tick (its own "raw_rows empty -> no match" short
    circuit) — so an empty `rows` HERE is always a genuine, confirmed
    zero after filtering, never an outage/no-data signal."""
    return float(len(rows or []))


_metric_count.rowset_aggregate = True
