"""Scope resolver — `symbols.all`: every row across the unified
symbols.* kinds (watchlist/pinned/gainers/losers)."""

from __future__ import annotations


def _scope_symbols_all(ctx):
    """Every row across every `symbols.*` kind (watchlist/pinned/gainers/
    losers) — always narrowed by a `scope_filter.kind` qualifier in
    practice. Reads `ctx.symbols_rows` — one dict per row, UNIONING four
    previously separate concepts behind a single `kind` qualifier.
    Populated once per `_task_performance` cycle by
    `background._perf_build_symbols_rows` (gated on whether any active
    agent actually references `symbols.*`, same cost profile as
    `watchlist.*`/`quote.*` — never a live per-condition broker
    round-trip). Instrument-rooted like `watchlist.*`/`quote.*` — no
    account dimension. REQUIRES a non-wildcard `scope_filter.kind`
    (enforced in `agent_evaluator.validate()`, mirroring the existing
    `quote.*` required-symbol guard) since the four kinds carry
    materially different row populations and mixing them unfiltered
    under one OR-combined leaf would be meaningless."""
    return list(getattr(ctx, 'symbols_rows', []) or [])
