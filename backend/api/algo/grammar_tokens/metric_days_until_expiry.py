"""Metric resolver — `days_until_expiry`: days until an F&O position's
option/future expires."""

from __future__ import annotations

from backend.api.algo.grammar import _parsed_or_none


def _metric_days_until_expiry(ctx, row):
    """Days until this position's option/future expires. None for
    cash equity holdings (non-parseable tradingsymbol)."""
    sym = row.get('tradingsymbol') or ''
    parsed = _parsed_or_none(sym)
    if not parsed or not parsed.get('expiry'):
        return None
    try:
        from backend.api.algo.derivatives import days_to_expiry
        from backend.shared.helpers.date_time_utils import close_time_for_exchange
        # Market-calendar SSOT, not a flat (23, 30)/(15, 30) literal —
        # resolves MCX's real DST-aware close (23:30 in-DST, 23:55 in
        # the US-winter window) instead of a year-round-flat assumption
        # (2026-11-26 fix; this is a directly operator-facing DSL
        # metric, so a miscalibrated close time here can make a custom
        # agent's own days_until_expiry condition fire up to ~25
        # minutes early during the winter window).
        close_time = close_time_for_exchange(row.get('exchange'), parsed['expiry']) or (15, 30)
        return float(days_to_expiry(parsed['expiry'], ref=ctx.now, close_time=close_time))
    except Exception:
        return None
