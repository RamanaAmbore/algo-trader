"""Metric resolver — `is_nse_expiry_today`: 1.0/0.0 from whether any
currently-held NFO (NSE equity F&O) position is expiring today (or
already past expiry). Ignores `row`, reads `ctx` only — same convention
as metric_is_mcx_open.py's module docstring.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.scope_positions_expiring_today import (
    _scope_positions_expiring_today,
)


def _metric_is_nse_expiry_today(ctx, row):
    """1.0 when any held NFO F&O position is expiring today (or already
    past expiry), 0.0 otherwise. Never None — `_scope_positions_expiring_
    today` degrades to an empty list on a missing/empty `ctx.position_
    rows`, which reads as "no expiry today", not "unknown". Kite's
    `exchange` field is NFO for equity F&O contracts (NSE is cash
    equity), matching `scope_positions_expiring_today_nfo.py`'s own
    filter."""
    rows = _scope_positions_expiring_today(ctx)
    nfo = [r for r in rows if (r.get('exchange') or '').upper() == 'NFO']
    return 1.0 if nfo else 0.0
