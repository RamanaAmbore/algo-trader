"""Tests for grammar_tokens/metric_day_pct.py — `day_pct` metric.

Fix #10 — day_pct metric prefers margin-based column over notional.
Positions now carry day_change_pct_margin (margin-based) column; the
resolver prefers it, falling back to day_change_percentage for holdings.

Worked example: -25k realized + one small open leg.
Pre-fix: day_pct = -25k / 1k_notional = -2500% (absurd)
Post-fix: day_pct = -25k / 100k_margin = -25% (reasonable)

Moved verbatim from test_grammar_parsing.py's TestDayPctMarginDenominator
and test_alerts_e2e_regressions.py's TestDefect10NotionalVsMarginDayPct
(Wave 1, batch 1 of the DSL token-unification plan).
"""

from backend.api.algo.grammar_tokens.metric_day_pct import _metric_day_pct


def test_prefers_margin_column_when_present():
    row = {'day_change_percentage': -500.0, 'day_change_pct_margin': -5.2}
    result = _metric_day_pct(None, row)
    assert result == -5.2, (
        f"Expected the margin-based figure (-5.2), not the notional "
        f"figure (-500.0) that produced the worked-example blowup, got {result}"
    )


def test_none_when_margin_unavailable_no_notional_fallback():
    """When the margin column exists but is NaN (margin data
    unavailable for that account this tick), the leaf must skip —
    NEVER silently fall back to the broken notional figure."""
    row = {'day_change_percentage': -500.0, 'day_change_pct_margin': float('nan')}
    assert _metric_day_pct(None, row) is None


def test_holdings_row_without_margin_column_uses_notional():
    """Holdings rows never carry day_change_pct_margin — they keep
    the standard opening-value-denominator day_change_percentage."""
    row = {'day_change_percentage': -3.2}
    assert _metric_day_pct(None, row) == -3.2


def test_metric_day_pct_uses_margin_when_available():
    """Positions now carry day_change_pct_margin (margin-based) column.

    Post-fix: _metric_day_pct prefers day_change_pct_margin when present,
    falling back to day_change_percentage for holdings.
    """
    row_positions = {
        'account': 'A',
        'day_change_pct_margin': -25.0,  # -25% of margin (post-fix column)
        'day_change_percentage': -2500.0,  # -2500% notional (old bug value)
    }
    val = _metric_day_pct(None, row_positions)
    assert val == -25.0, (
        "Post-fix: day_pct should use margin-based percentage when available"
    )


def test_metric_day_pct_falls_back_for_holdings():
    """Holdings don't have day_change_pct_margin; use day_change_percentage."""
    row_holdings = {
        'account': 'A',
        'day_change_percentage': 5.0,  # Standard holdings percentage
    }
    val = _metric_day_pct(None, row_holdings)
    assert val == 5.0, (
        "Post-fix: day_pct should fall back to day_change_percentage for holdings"
    )
