"""Tests for grammar_tokens/metric_minutes_until_close.py —
`minutes_until_close` metric.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, batch 4 of the DSL token-
unification plan.
"""

from unittest.mock import MagicMock

from backend.api.algo.grammar_tokens.metric_minutes_until_close import (
    _metric_minutes_until_close,
)


def test_minutes_until_close_delegates_to_context_method():
    ctx = MagicMock()
    ctx.minutes_until_close.return_value = 120.0
    assert _metric_minutes_until_close(ctx, {}) == 120.0
    ctx.minutes_until_close.assert_called_once_with()


def test_minutes_until_close_passes_through_none():
    ctx = MagicMock()
    ctx.minutes_until_close.return_value = None
    assert _metric_minutes_until_close(ctx, {}) is None
