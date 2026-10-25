"""Tests for grammar_tokens/metric_collateral.py — `collateral` metric.

Moved verbatim from test_grammar_parsing.py (Wave 1, batch 1).
"""

from backend.api.algo.grammar_tokens.metric_collateral import _metric_collateral


def test_collateral_none_when_missing():
    assert _metric_collateral(None, {}) is None


def test_collateral_true_zero_passes_through():
    assert _metric_collateral(None, {'avail collateral': 0.0}) == 0.0


def test_collateral_real_value_passes_through():
    assert _metric_collateral(None, {'avail collateral': 75000.0}) == 75000.0


def test_collateral_none_when_nan():
    assert _metric_collateral(None, {'avail collateral': float('nan')}) is None
