"""Tests for grammar_tokens/metric_inv_val.py — `inv_val` metric.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, batch 1 of the DSL token-
unification plan.
"""

from backend.api.algo.grammar_tokens.metric_inv_val import _metric_inv_val


def test_inv_val_reads_real_value():
    assert _metric_inv_val(None, {'inv_val': 100000.0}) == 100000.0


def test_inv_val_defaults_to_zero_when_missing():
    assert _metric_inv_val(None, {}) == 0.0


def test_inv_val_defaults_to_zero_when_none():
    assert _metric_inv_val(None, {'inv_val': None}) == 0.0
