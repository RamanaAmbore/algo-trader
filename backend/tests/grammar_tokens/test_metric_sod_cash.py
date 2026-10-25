"""Tests for grammar_tokens/metric_sod_cash.py — `sod_cash` metric
(start-of-day cash baseline).

Moved verbatim from test_grammar_parsing.py (Wave 1, batch 1). The
catalog-registration test (`sod_cash` is a registered system token)
stays in test_grammar_parsing.py — it asserts on SYSTEM_TOKENS, not
on this resolver function directly.
"""

from backend.api.algo.grammar_tokens.metric_sod_cash import _metric_sod_cash


def test_sod_cash_reads_opening_balance():
    """sod_cash (new token) preserves the OLD `cash` behaviour for
    agents that specifically want the start-of-day baseline."""
    row = {'avail cash': -500.0, 'avail opening_balance': 200000.0}
    assert _metric_sod_cash(None, row) == 200000.0


def test_sod_cash_none_when_missing():
    assert _metric_sod_cash(None, {}) is None
