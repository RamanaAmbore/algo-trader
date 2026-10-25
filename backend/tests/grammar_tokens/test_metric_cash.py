"""Tests for grammar_tokens/metric_cash.py — `cash` metric (live intraday cash).

Fix #4 — was reading start-of-day 'avail opening_balance', which cannot
move intraday, so loss-funds-negative's cash<0 leaf could never fire on a
real intraday cash drop.

Moved verbatim from test_grammar_parsing.py (Wave 1, batch 1).
"""

from backend.api.algo.grammar_tokens.metric_cash import _metric_cash


def test_cash_reads_avail_cash_not_opening_balance():
    """cash must move intraday — reads 'avail cash' (live), NOT
    'avail opening_balance' (frozen SOD figure the old resolver read)."""
    row = {'avail cash': -500.0, 'avail opening_balance': 200000.0}
    result = _metric_cash(None, row)
    assert result == -500.0, (
        f"Expected live 'avail cash' (-500.0), got {result} — "
        f"cash<0 must be able to fire on a real intraday drop even "
        f"when the SOD opening balance was healthy"
    )


def test_cash_none_when_missing():
    assert _metric_cash(None, {}) is None


def test_cash_none_when_nan():
    assert _metric_cash(None, {'avail cash': float('nan')}) is None
