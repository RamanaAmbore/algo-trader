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
        # MCX commodity options trade until 23:30; everything else 15:30.
        close_time = (23, 30) if (row.get('exchange') or '').upper() == 'MCX' else (15, 30)
        return float(days_to_expiry(parsed['expiry'], ref=ctx.now, close_time=close_time))
    except Exception:
        return None
