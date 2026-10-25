"""Tests for grammar_tokens/metric_ltp.py — `ltp` metric.

No dedicated resolver-level test existed before this migration —
test_orders_quote_scopes.py::test_ltp_reuses_the_real_metric_token_passthrough
exercises the EXPOSED_COLUMNS fallback shape, not this resolver
function's own return value, and stays in place (out of scope — it is
an exposed-column/registry test, not a metric resolver test). Fresh
resolver-level coverage written as part of Wave 1, batch 1 of the DSL
token-unification plan.
"""

from backend.api.algo.grammar_tokens.metric_ltp import _metric_ltp


def test_ltp_reads_last_price():
    assert _metric_ltp(None, {'last_price': 1234.5}) == 1234.5


def test_ltp_defaults_to_zero_when_missing():
    assert _metric_ltp(None, {}) == 0.0


def test_ltp_defaults_to_zero_when_none():
    assert _metric_ltp(None, {'last_price': None}) == 0.0


def test_ltp_works_on_position_row_shape_too():
    """Reused verbatim for position rows that carry `last_price`."""
    row = {'tradingsymbol': 'NIFTY26OCTFUT', 'last_price': 22100.0}
    assert _metric_ltp(None, row) == 22100.0
