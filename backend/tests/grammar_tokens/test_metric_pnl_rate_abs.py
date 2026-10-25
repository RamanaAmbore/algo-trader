"""Tests for grammar_tokens/metric_pnl_rate_abs.py — `pnl_rate_abs` metric.

No dedicated resolver-level test existed before this migration (every
existing reference wires the resolver into REGISTRY.metrics as a
fixture for evaluator/engine integration tests) — fresh coverage,
written as part of Wave 1, batch 2 of the DSL token-unification plan.

`windowed_rate()` (agent_evaluator.py) requires at least 3 samples
spanning >= 80% of the effective rate window (default 10 minutes, or
wider if the observed sample cadence demands it) — the fixtures below
use 5-minute spacing across 3 samples (10-minute span) to satisfy that
guard, same convention as test_agent_latch.py's rate fixtures.
"""

from datetime import datetime, timedelta, timezone

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_pnl_rate_abs import _metric_pnl_rate_abs


def _ctx_with_history(key, samples, now=None):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    n = len(samples)
    history = [
        (now - timedelta(minutes=5 * (n - 1 - i)), pnl, pct)
        for i, (pnl, pct) in enumerate(samples)
    ]
    return Context(alert_state={'pnl_history': {key: history}}, now=now)


def test_pnl_rate_abs_computes_rate_for_account_scoped_row():
    ctx = _ctx_with_history(('positions', 'ZG0790'), [(100, None), (130, None), (160, None)])
    row = {'account': 'ZG0790'}
    result = _metric_pnl_rate_abs(ctx, row)
    assert result == 6.0, f"expected 6.0 ₹/min, got {result}"


def test_pnl_rate_abs_keys_off_underlying_namespace_when_no_account():
    """A row with `underlying` and NO real `account` (positions.any_underlying)
    must key off the ('positions_underlying', <underlying>) bucket, not
    ('positions', None)."""
    ctx = _ctx_with_history(('positions_underlying', 'NIFTY'), [(0, None), (250, None), (500, None)])
    row = {'underlying': 'NIFTY'}
    result = _metric_pnl_rate_abs(ctx, row)
    assert result == 50.0, f"expected 50.0 ₹/min, got {result}"


def test_pnl_rate_abs_none_when_baseline_not_live():
    ctx = _ctx_with_history(('positions', 'ZG0790'), [(100, None), (130, None), (160, None)])
    ctx.baseline_live = False
    row = {'account': 'ZG0790'}
    assert _metric_pnl_rate_abs(ctx, row) is None


def test_pnl_rate_abs_none_on_empty_history():
    ctx = Context(alert_state={}, now=datetime.now(timezone.utc))
    row = {'account': 'ZG0790'}
    assert _metric_pnl_rate_abs(ctx, row) is None
