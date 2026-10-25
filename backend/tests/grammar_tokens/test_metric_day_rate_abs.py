"""Tests for grammar_tokens/metric_day_rate_abs.py — `day_rate_abs` metric.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, batch 2 of the DSL token-
unification plan. See test_metric_pnl_rate_abs.py's module docstring
for the 5-minute-spacing fixture rationale.
"""

from datetime import datetime, timedelta, timezone

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_day_rate_abs import _metric_day_rate_abs


def _ctx_with_history(key, samples, now=None):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    n = len(samples)
    history = [
        (now - timedelta(minutes=5 * (n - 1 - i)), pnl, pct)
        for i, (pnl, pct) in enumerate(samples)
    ]
    return Context(alert_state={'pnl_history': {key: history}}, now=now)


def test_day_rate_abs_computes_rate_for_holdings_account():
    ctx = _ctx_with_history(('holdings', 'ZG0790'), [(1000, None), (1150, None), (1300, None)])
    row = {'account': 'ZG0790'}
    result = _metric_day_rate_abs(ctx, row)
    assert result == 30.0, f"expected 30.0 ₹/min, got {result}"


def test_day_rate_abs_none_when_baseline_not_live():
    ctx = _ctx_with_history(('holdings', 'ZG0790'), [(1000, None), (1150, None), (1300, None)])
    ctx.baseline_live = False
    row = {'account': 'ZG0790'}
    assert _metric_day_rate_abs(ctx, row) is None


def test_day_rate_abs_none_on_empty_history():
    ctx = Context(alert_state={}, now=datetime.now(timezone.utc))
    row = {'account': 'ZG0790'}
    assert _metric_day_rate_abs(ctx, row) is None
