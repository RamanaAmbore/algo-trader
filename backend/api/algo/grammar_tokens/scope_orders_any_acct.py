"""Scope resolver — `orders.any_acct`: every currently open/active order
(mode=live, not yet filled/cancelled/rejected), across every account."""

from __future__ import annotations


def _scope_orders_any_acct(ctx):
    """Every currently open/active order, across every account. Reads
    `ctx.orders_rows` — currently OPEN/active `AlgoOrder` rows (mode=
    'live', status not in ALGO_ORDER_TERMINAL_STATUSES), refreshed once
    per `_task_performance` cycle by `background._perf_fetch_orders_rows`.
    NOT a live broker call — same cadence/cost profile as
    positions/holdings/funds. Account-rooted; never exchange:/symbol: —
    those are instrument-rooted qualifiers, reserved for `quote.*`.
    No `orders.total` aggregate — unlike P&L, there is no single
    meaningful scalar to sum across open orders of mixed BUY/SELL
    direction and instrument, so this scope family only ever has a
    per-row variant."""
    return list(getattr(ctx, 'orders_rows', []) or [])
