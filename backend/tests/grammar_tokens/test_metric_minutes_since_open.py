"""Tests for grammar_tokens/metric_minutes_since_open.py —
`minutes_since_open` metric.

No dedicated resolver-level test existed before this migration (the
resolver is a pure passthrough of `Context.minutes_since_open()`,
which has its own coverage elsewhere, but the metric token itself was
untested) — fresh coverage, written as part of Wave 1, batch 4 of the
DSL token-unification plan.
"""

from unittest.mock import MagicMock

from backend.api.algo.grammar_tokens.metric_minutes_since_open import (
    _metric_minutes_since_open,
)


def test_minutes_since_open_delegates_to_context_method():
    ctx = MagicMock()
    ctx.minutes_since_open.return_value = 37.5
    assert _metric_minutes_since_open(ctx, {}) == 37.5
    ctx.minutes_since_open.assert_called_once_with()


def test_minutes_since_open_passes_through_none():
    ctx = MagicMock()
    ctx.minutes_since_open.return_value = None
    assert _metric_minutes_since_open(ctx, {}) is None
