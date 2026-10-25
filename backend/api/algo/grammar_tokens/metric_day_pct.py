"""Metric resolver — `day_pct`: day-change percentage (margin-based for positions,
notional/opening-value-based for holdings)."""

from __future__ import annotations

from backend.api.algo.grammar import _num_or_none


def _metric_day_pct(ctx, row):
    """Day-change percentage.

    Fix #10 — for POSITIONS rows, prefer `day_change_pct_margin` (added by
    `background._apply_positions_margin_pct`: day_change_val / account
    margin base, NOT notional) when present on the row. Notional
    (Σ|prev_close × quantity| over CURRENT rows) drops closed (qty=0)
    legs from the denominator while the numerator still carries their
    P&L, so a mostly-closed book could show -500% on a real -2%-of-margin
    move. HOLDINGS rows never carry that column — they keep the existing
    `day_change_percentage` (opening-value denominator), which is the
    standard, uncontested definition for a day's % move on a holding.
    Returns None (not 0) when the value itself is missing — real 0% is
    still a valid value and must pass through.
    """
    if 'day_change_pct_margin' in row:
        return _num_or_none(row.get('day_change_pct_margin'))
    val = row.get('day_change_percentage')
    return _num_or_none(val) if val is not None else 0.0
