"""Tests for grammar_tokens/metric_cur_val.py — `cur_val` metric.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, batch 1 of the DSL token-
unification plan.
"""

from backend.api.algo.grammar_tokens.metric_cur_val import _metric_cur_val


def test_cur_val_reads_real_value():
    assert _metric_cur_val(None, {'cur_val': 95000.0}) == 95000.0


def test_cur_val_defaults_to_zero_when_missing():
    assert _metric_cur_val(None, {}) == 0.0


def test_cur_val_defaults_to_zero_when_none():
    assert _metric_cur_val(None, {'cur_val': None}) == 0.0
