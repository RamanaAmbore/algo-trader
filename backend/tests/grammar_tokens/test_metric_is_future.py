"""Tests for grammar_tokens/metric_is_future.py — `is_future` metric.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, batch 3 of the DSL token-
unification plan.
"""

from backend.api.algo.grammar_tokens.metric_is_future import _metric_is_future


def test_is_future_true_for_futures_contract():
    row = {"tradingsymbol": "NIFTY25APRFUT", "exchange": "NFO"}
    assert _metric_is_future(None, row) == 1.0


def test_is_future_false_for_option():
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    assert _metric_is_future(None, row) == 0.0


def test_is_future_none_for_equity():
    row = {"tradingsymbol": "RELIANCE", "exchange": "NSE"}
    assert _metric_is_future(None, row) is None


def test_is_future_none_for_missing_symbol():
    assert _metric_is_future(None, {}) is None
