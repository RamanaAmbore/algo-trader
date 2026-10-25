"""Scope resolver — `watchlist.all`: every row across every watchlist
the operator owns (leaf is OR-combined per row)."""

from __future__ import annotations


def _scope_watchlist_all(ctx):
    """Every row across every watchlist the user owns. Reads
    `ctx.watchlist_rows` — the `account` slot on each row carries the
    watchlist NAME, not a trading account (see the exposed-column
    collision guard in grammar.py's module comment)."""
    return list(getattr(ctx, 'watchlist_rows', []) or [])
