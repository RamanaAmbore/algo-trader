"""Tests for grammar_tokens/metric_used_margin.py — `used_margin` metric.

Moved verbatim from test_grammar_parsing.py (Wave 1, batch 1).
"""

from backend.api.algo.grammar_tokens.metric_used_margin import _metric_used_margin


def test_used_margin_none_when_missing():
    assert _metric_used_margin(None, {}) is None


def test_used_margin_true_zero_passes_through():
    assert _metric_used_margin(None, {'util debits': 0.0}) == 0.0


def test_used_margin_real_value_passes_through():
    assert _metric_used_margin(None, {'util debits': 42000.0}) == 42000.0


def test_used_margin_none_when_nan():
    assert _metric_used_margin(None, {'util debits': float('nan')}) is None
