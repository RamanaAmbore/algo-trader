"""Scope resolver — `quote.tracked`: pre-tracked ad-hoc market-data
row(s) for symbols referenced by at least one active agent's quote.*
condition leaf."""

from __future__ import annotations


def _scope_quote_tracked(ctx):
    """Every pre-tracked ad-hoc market-data row. Reads `ctx.quote_rows`
    — symbol + exchange + ltp/bid/ask/volume, for every symbol
    referenced by at least one active agent's `quote.*` leaf, refreshed
    once per `_task_performance` cycle by
    `background._perf_build_quote_rows` (reuses `routes/quote.py`'s
    `_fetch_ltp` directly — never a live per-condition broker
    round-trip). Instrument-rooted (qualified by `symbol:` — REQUIRED,
    see `agent_evaluator.validate()`'s quote.* guard — with `exchange:`
    as an OPTIONAL secondary qualifier); never `account:` — market data
    belongs to no account. Mirrors `watchlist.*`'s existing
    per-row-only pattern exactly — no aggregate."""
    return list(getattr(ctx, 'quote_rows', []) or [])
