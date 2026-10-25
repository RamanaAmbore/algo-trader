"""Scope resolver — `watchlist.default`: rows in the operator's Default
watchlist."""

from __future__ import annotations


def _scope_watchlist_default(ctx):
    """Rows in the user's 'Default' watchlist. Reads `ctx.watchlist_rows`
    filtered on the `account` slot, which carries the watchlist NAME on
    this row shape (not a trading account)."""
    rows = getattr(ctx, 'watchlist_rows', []) or []
    return [r for r in rows if str(r.get('account', '')) == 'Default']
