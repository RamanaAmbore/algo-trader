"""Scope resolver — `positions.any_symbol`: every per-symbol position row
(fan-out — one row per held position; leaf is OR-combined)."""

from __future__ import annotations


def _scope_positions_any_symbol(ctx):
    """Every per-symbol position row from `ctx.position_rows` — one row
    per currently held position, INDEPENDENTLY (fan-out).

    Distinct from the other two "all positions" scope shapes already in
    this family:
      - `positions.worst_symbol` collapses to a SINGLE row (the worst by
        pnl) — see `scope_positions_worst_symbol._scope_positions_worst_
        symbol`.
      - `positions.any_underlying` collapses to ONE aggregate row PER
        UNDERLYING ROOT, grouped across every account holding it — see
        `scope_positions_any_underlying._scope_positions_any_underlying`.
      - `positions.any_symbol` (this scope) returns EVERY row untouched —
        no collapsing, no grouping. A leg-level agent (e.g. "close any
        short option at 90% of premium captured") needs each leg's own
        account/symbol/exchange to reach the per-leg action dispatch
        intact, which only a true fan-out scope can guarantee.

    Reads the SAME raw per-symbol list `positions.expiring_today` /
    `positions.worst_symbol` already read (`ctx.position_rows`, built
    once per tick from `df_positions.to_dict("records")` —
    `background.py`'s `_perf_run_agent_engine`) — no new row-building
    logic, no new ctx field.
    """
    return list(getattr(ctx, 'position_rows', None) or [])
