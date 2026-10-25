"""Scope resolver — `watchlist.markets`: rows in the auto-seeded Markets
watchlist (indices + MCX commodities)."""

from __future__ import annotations


def _scope_watchlist_markets(ctx):
    """Rows in the auto-seeded 'Markets' watchlist (indices + commodities).
    Reads `ctx.watchlist_rows` filtered on the `account` slot, which
    carries the watchlist NAME on this row shape (not a trading account)."""
    rows = getattr(ctx, 'watchlist_rows', []) or []
    return [r for r in rows if str(r.get('account', '')) == 'Markets']
