"""Scope resolver — `positions.expiring_today`: per-symbol position rows
whose F&O contract is expiring today (or already past expiry)."""

from __future__ import annotations

from backend.api.algo.grammar import _parsed_or_none


def _scope_positions_expiring_today(ctx):
    """Per-symbol position rows where the symbol parses to an F&O
    contract expiring TODAY (or already past expiry — days_to_expiry
    floors at 0). Cash-equity rows skip (no parseable expiry).

    Reads from ctx.position_rows — the raw per-symbol list the engine
    fetched. sum_positions is the per-account aggregate and carries
    no per-symbol expiry, so it's the wrong shape for this filter.

    Why ≤ 1.5 day floor rather than === 0: an option that "expires
    today" at the engine's 09:00 tick has ~6.5 hours of life left;
    days_to_expiry yields ~0.27. The 1.5 ceiling also catches "T-1
    intraday warning" if a future operator preference wants that —
    keeping a single scope rather than a wider scope-token set.
    """
    rows_src = getattr(ctx, 'position_rows', None) or []
    out = []
    for r in rows_src:
        sym = r.get('tradingsymbol') or ''
        parsed = _parsed_or_none(sym)
        if not parsed or not parsed.get('expiry'):
            continue
        try:
            from backend.api.algo.derivatives import days_to_expiry
            close_time = (23, 30) if (r.get('exchange') or '').upper() == 'MCX' else (15, 30)
            d = float(days_to_expiry(parsed['expiry'], ref=ctx.now, close_time=close_time))
        except Exception:
            continue
        if d <= 1.5:
            out.append(r)
    return out
