"""Metric resolver — `pnl_pct`: positions P&L as a % of the account's margin base."""

from __future__ import annotations


def _metric_pnl_pct(ctx, row):
    """Positions P&L as a % of the account's margin base.

    2026-10 audit fix: previously divided by `ctx.used_margin_for()`
    (an OR-fallback: `util debits` if > 0, else `net`) — a DIFFERENT
    and narrower denominator than `day_pct`'s own corrected basis
    (fix #10: used+available margin SUM, via `account_margin_base()`).
    The OR-fallback denominator swings every time a position opens or
    closes (util debits shrinks/grows) even with zero P&L change,
    reproducing the exact "closed legs drop out of the denominator"
    symptom fix #10 was meant to eliminate — just for pnl_pct instead
    of day_pct. Now shares the SAME denominator as day_pct.

    Returns None when the margin base is unavailable for this account
    (missing-vs-zero convention — a genuinely missing figure must skip
    the leaf, not silently fall back to a different denominator).
    """
    from backend.api.algo.pnl_math import account_margin_base
    base = account_margin_base(ctx.df_margins, row.get('account'))
    if base is None or base <= 0:
        return None
    return (float(row.get('pnl', 0) or 0) / base) * 100.0
