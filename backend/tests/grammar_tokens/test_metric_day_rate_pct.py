"""Tests for grammar_tokens/metric_day_rate_pct.py — `day_rate_pct` metric.

No dedicated resolver-level test existed before this migration — fresh
coverage, written as part of Wave 1, batch 2 of the DSL token-
unification plan. See test_metric_pnl_rate_abs.py's module docstring
for the 5-minute-spacing fixture rationale.
"""

from datetime import datetime, timedelta, timezone

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_day_rate_pct import _metric_day_rate_pct


def _ctx_with_history(key, samples, now=None):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    n = len(samples)
    history = [
        (now - timedelta(minutes=5 * (n - 1 - i)), pnl, pct)
        for i, (pnl, pct) in enumerate(samples)
    ]
    return Context(alert_state={'pnl_history': {key: history}}, now=now)


def test_day_rate_pct_computes_rate_for_holdings_account():
    ctx = _ctx_with_history(('holdings', 'ZG0790'), [(0, 1.0), (0, 2.5), (0, 4.0)])
    row = {'account': 'ZG0790'}
    result = _metric_day_rate_pct(ctx, row)
    assert result == 0.3, f"expected 0.3 %/min, got {result}"


def test_day_rate_pct_none_when_baseline_not_live():
    ctx = _ctx_with_history(('holdings', 'ZG0790'), [(0, 1.0), (0, 2.5), (0, 4.0)])
    ctx.baseline_live = False
    row = {'account': 'ZG0790'}
    assert _metric_day_rate_pct(ctx, row) is None
