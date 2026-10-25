"""Scope resolver — `positions.expiring_today.nfo`: subset of
positions.expiring_today restricted to NFO (equity F&O)."""

from __future__ import annotations

from backend.api.algo.grammar_tokens.scope_positions_expiring_today import (
    _scope_positions_expiring_today,
)


def _scope_positions_expiring_today_nfo(ctx):
    """Subset of positions.expiring_today restricted to NFO (equity
    F&O). Used by the equity-only auto-close agent which fires at
    T-30min before the 15:30 IST equity close. Kite's `exchange`
    field is the source of truth — NSE for cash equity, NFO for
    equity F&O contracts. Returns NFO rows only.
    """
    rows = _scope_positions_expiring_today(ctx)
    return [r for r in rows if (r.get('exchange') or '').upper() == 'NFO']
