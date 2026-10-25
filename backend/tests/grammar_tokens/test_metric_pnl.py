"""Tests for grammar_tokens/metric_pnl.py — `pnl` metric.

No dedicated resolver-level test existed before this migration (every
existing reference across the suite wires `_metric_pnl` into
`REGISTRY.metrics` as a fixture for evaluator/engine integration tests,
never asserts on the resolver's own return value directly) — fresh
coverage, written as part of Wave 1, batch 1 of the DSL token-
unification plan.
"""

from backend.api.algo.grammar_tokens.metric_pnl import _metric_pnl


def test_pnl_reads_real_value():
    row = {'account': 'ZG0790', 'pnl': -12345.0}
    assert _metric_pnl(None, row) == -12345.0


def test_pnl_defaults_to_zero_when_missing():
    """Unlike the raw-broker-column metrics (avail_margin/cash/...),
    `pnl` is a computed positions aggregate — always populated by the
    background summary builder's `.fillna(0)` — so a missing key here
    coerces to 0.0, not None (see grammar.py's missing-vs-zero
    convention module comment for the full boundary rationale)."""
    assert _metric_pnl(None, {}) == 0.0


def test_pnl_defaults_to_zero_when_none():
    assert _metric_pnl(None, {'pnl': None}) == 0.0


def test_pnl_handles_positive_value():
    assert _metric_pnl(None, {'pnl': 5000.0}) == 5000.0
