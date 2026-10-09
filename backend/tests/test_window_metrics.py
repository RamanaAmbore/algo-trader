"""
Phase 24 — rolling-window metric unit tests.

Covers the statistical aggregators added to V2Context:
  window_mean / window_stdev / window_range / window_drawdown

Each test feeds a deterministic pnl_history slice into a bare Context
and asserts the reducer returns the expected value. No DB, no broker —
pure math.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from backend.api.algo.agent_evaluator import Context


def _ctx_with_history(samples: list[tuple[float, float | None]],
                      now: datetime | None = None) -> Context:
    """Build a Context whose pnl_history holds the given (pnl, pct) samples
    at 1-minute spacing, ending at `now`."""
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    history = []
    n = len(samples)
    for i, (pnl, pct) in enumerate(samples):
        ts = now - timedelta(minutes=(n - 1 - i))
        history.append((ts, pnl, pct))
    return Context(
        alert_state={'pnl_history': {('positions', 'ZG####'): history}},
        now=now,
    )


def test_window_mean_basic():
    ctx = _ctx_with_history([(100, None), (200, None), (300, None)])
    assert ctx.window_mean(('positions', 'ZG####'), 60) == 200.0


def test_window_mean_returns_none_on_single_sample():
    ctx = _ctx_with_history([(100, None)])
    assert ctx.window_mean(('positions', 'ZG####'), 60) is None


def test_window_stdev_basic():
    ctx = _ctx_with_history([(0, None), (10, None), (20, None)])
    # Sample stdev of [0, 10, 20] = sqrt(100) = 10
    sigma = ctx.window_stdev(('positions', 'ZG####'), 60)
    assert sigma is not None
    assert math.isclose(sigma, 10.0, abs_tol=0.0001)


def test_window_range_basic():
    ctx = _ctx_with_history([(50, None), (-30, None), (10, None), (100, None)])
    r = ctx.window_range(('positions', 'ZG####'), 60)
    assert r == 130.0  # 100 - (-30)


def test_window_drawdown_peak_to_trough():
    # pnl climbs to 200, then crashes to -50 → drawdown = -250.
    ctx = _ctx_with_history(
        [(0, None), (100, None), (200, None), (50, None), (-50, None)]
    )
    dd = ctx.window_drawdown(('positions', 'ZG####'), 60)
    assert dd == -250.0


def test_window_drawdown_monotonic_climb_is_zero():
    ctx = _ctx_with_history([(0, None), (50, None), (100, None)])
    assert ctx.window_drawdown(('positions', 'ZG####'), 60) == 0.0


def test_window_drawdown_starts_with_drop():
    # Peak is the very first sample; trough is the second.
    ctx = _ctx_with_history([(100, None), (-30, None), (10, None)])
    assert ctx.window_drawdown(('positions', 'ZG####'), 60) == -130.0


def test_window_slice_respects_minutes():
    # 5 samples at 1-minute spacing; ask for last 2 minutes → 3 samples
    # (now, now-1m, now-2m).
    ctx = _ctx_with_history(
        [(1, None), (2, None), (3, None), (4, None), (5, None)]
    )
    assert ctx.window_mean(('positions', 'ZG####'), 2) == 4.0  # mean of 3,4,5


def test_pct_field_index():
    # field_idx=2 reads the pct slot, ignoring pnl ₹.
    ctx = _ctx_with_history([(0, 1.0), (0, 2.0), (0, 3.0)])
    assert ctx.window_mean(('positions', 'ZG####'), 60, field_idx=2) == 2.0


def test_missing_pct_samples_skipped():
    # field_idx=2 with None pct values → drops them. Mean of the two
    # remaining values [1.0, 3.0] = 2.0.
    ctx = _ctx_with_history([(0, 1.0), (0, None), (0, 3.0)])
    assert ctx.window_mean(('positions', 'ZG####'), 60, field_idx=2) == 2.0


def test_empty_history_returns_none():
    ctx = Context(alert_state={}, now=datetime.now(timezone.utc))
    assert ctx.window_mean(('positions', 'ZG####'), 60) is None
    assert ctx.window_stdev(('positions', 'ZG####'), 60) is None
    assert ctx.window_range(('positions', 'ZG####'), 60) is None
    assert ctx.window_drawdown(('positions', 'ZG####'), 60) is None


# ═══════════════════════════════════════════════════════════════════════════
#  Phase 26 (Sprint 2) — _metric_factory_* resolvers must compute the exact
#  same value as the pre-existing fixed-window metric functions. These
#  resolvers are what GrammarRegistry.metric("mean_pnl(30)") ultimately
#  returns (see test_grammar_registry.py for the registry-level call-shape
#  parsing); this file proves the underlying math, on real pnl_history, is
#  unchanged — the fixed tokens are now one-line bindings to these same
#  factories, not a separate implementation that happens to agree.
# ═══════════════════════════════════════════════════════════════════════════

def test_mean_pnl_factory_matches_fixed_30m_resolver():
    from backend.api.algo import grammar
    ctx = _ctx_with_history([(100, None), (200, None), (300, None)])
    row = {'account': 'ZG####'}
    assert grammar._metric_factory_mean_pnl(30)(ctx, row) == grammar._metric_mean_pnl_30m(ctx, row)
    assert grammar._metric_factory_mean_pnl(30)(ctx, row) == 200.0


def test_mean_day_factory_matches_fixed_30m_resolver():
    from backend.api.algo import grammar
    now = datetime.now(timezone.utc).replace(microsecond=0)
    history = [(now - timedelta(minutes=2), 10, None), (now - timedelta(minutes=1), 20, None), (now, 30, None)]
    ctx = Context(alert_state={'pnl_history': {('holdings', 'ZG####'): history}}, now=now)
    row = {'account': 'ZG####'}
    assert grammar._metric_factory_mean_day(30)(ctx, row) == grammar._metric_mean_day_30m(ctx, row)
    assert grammar._metric_factory_mean_day(30)(ctx, row) == 20.0


def test_max_drawdown_pnl_factory_matches_fixed_1h_resolver():
    from backend.api.algo import grammar
    ctx = _ctx_with_history([(0, None), (100, None), (200, None), (50, None), (-50, None)])
    row = {'account': 'ZG####'}
    assert grammar._metric_factory_max_drawdown_pnl(60)(ctx, row) == grammar._metric_max_drawdown_pnl_1h(ctx, row)
    assert grammar._metric_factory_max_drawdown_pnl(60)(ctx, row) == -250.0


def test_max_drawdown_pnl_pct_factory_matches_fixed_30m_resolver():
    from backend.api.algo import grammar
    ctx = _ctx_with_history([(0, 0.0), (0, 10.0), (0, 20.0), (0, 5.0), (0, -5.0)])
    row = {'account': 'ZG####'}
    assert grammar._metric_factory_max_drawdown_pnl_pct(30)(ctx, row) == \
        grammar._metric_max_drawdown_pnl_pct_30m(ctx, row)
    assert grammar._metric_factory_max_drawdown_pnl_pct(30)(ctx, row) == -25.0


def test_stdev_pnl_factory_matches_fixed_30m_resolver():
    from backend.api.algo import grammar
    ctx = _ctx_with_history([(0, None), (10, None), (20, None)])
    row = {'account': 'ZG####'}
    assert grammar._metric_factory_stdev_pnl(30)(ctx, row) == grammar._metric_stdev_pnl_30m(ctx, row)


def test_range_pnl_factory_matches_fixed_30m_resolver():
    from backend.api.algo import grammar
    ctx = _ctx_with_history([(50, None), (-30, None), (10, None), (100, None)])
    row = {'account': 'ZG####'}
    assert grammar._metric_factory_range_pnl(30)(ctx, row) == grammar._metric_range_pnl_30m(ctx, row)
    assert grammar._metric_factory_range_pnl(30)(ctx, row) == 130.0
