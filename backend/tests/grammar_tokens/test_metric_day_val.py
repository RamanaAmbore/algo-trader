"""Tests for grammar_tokens/metric_day_val.py — `day_val` metric.

No dedicated resolver-level test existed before this migration (the
similarly-named test_day_val_metric.py covers a DIFFERENT function —
agent_engine.py's `_v2_extract_pnl_fields`, not this grammar.py
resolver) — fresh coverage, written as part of Wave 1, batch 1 of the
DSL token-unification plan.
"""

from backend.api.algo.grammar_tokens.metric_day_val import _metric_day_val


def test_day_val_reads_real_value():
    row = {'account': 'ZG0790', 'day_change_val': -2500.0}
    assert _metric_day_val(None, row) == -2500.0


def test_day_val_defaults_to_zero_when_missing():
    """Computed positions/holdings aggregate — always populated by the
    background summary builder's `.fillna(0)` — so a missing key
    coerces to 0.0, not None."""
    assert _metric_day_val(None, {}) == 0.0


def test_day_val_defaults_to_zero_when_none():
    assert _metric_day_val(None, {'day_change_val': None}) == 0.0


def test_day_val_handles_positive_value():
    assert _metric_day_val(None, {'day_change_val': 1800.0}) == 1800.0
