"""Tests for grammar_tokens/metric_avail_margin.py — `avail_margin` metric.

Fix #3 — missing-vs-zero convention for raw-broker-column funds metrics.
A genuinely-absent/None/NaN broker column must resolve to None (leaf
skipped), never a coerced 0.0 — the operator's explicit ask: "if margin
or cash of 0 should not generate alerts [when the 0 is actually missing
data]". A TRUE reported 0 must still pass through (real zero balances
must still be able to alert).

Moved verbatim from test_grammar_parsing.py (Wave 1, batch 1 of the DSL
token-unification plan) — pure relocation, zero behavior change.
"""

from backend.api.algo.grammar_tokens.metric_avail_margin import _metric_avail_margin


def test_avail_margin_none_when_column_absent():
    row = {'account': 'DH1234'}  # 'net' column never present
    assert _metric_avail_margin(None, row) is None, (
        "Missing 'net' column must resolve to None, not 0"
    )


def test_avail_margin_true_zero_passes_through():
    row = {'account': 'ZD1234', 'net': 0.0}
    assert _metric_avail_margin(None, row) == 0.0, (
        "A REAL reported 0 must pass through unchanged, not be treated as missing"
    )


def test_avail_margin_none_when_nan():
    row = {'account': 'GR1234', 'net': float('nan')}
    assert _metric_avail_margin(None, row) is None, (
        "NaN (pandas representation of a None from json_normalize) must resolve to None"
    )


def test_avail_margin_negative_real_value_passes_through():
    """Prod repro guard: a genuinely negative margin must still fire —
    only MISSING data is suppressed, not real risk events."""
    row = {'account': 'ZD1234', 'net': -1500.0}
    assert _metric_avail_margin(None, row) == -1500.0
