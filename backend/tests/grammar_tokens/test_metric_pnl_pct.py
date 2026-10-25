"""Tests for grammar_tokens/metric_pnl_pct.py — `pnl_pct` metric.

2026-10 audit fix: `_metric_pnl_pct` no longer uses
`Context.used_margin_for()` (an OR-fallback: util_debits if > 0, else
net). It now shares `pnl_math.account_margin_base()` — the SAME
used+available SUM denominator `day_pct` already uses (fix #10) — so
pnl_pct and day_pct agree on what "% of margin" means. The two
denominators coincide numerically whenever util_debits is 0 (SUM
degenerates to net, same as the old fallback), which is why several
single-field test cases below are numerically unchanged; cases with
BOTH util_debits and net nonzero are the ones that actually changed.

Moved verbatim from test_agent_grammar.py's TestMetricPnlPct (Wave 1,
batch 1 of the DSL token-unification plan) — pure relocation, zero
behavior change. `Context.used_margin_for()`'s own OR-fallback
behaviour (unrelated to this resolver any more) is still exercised by
test_agent_grammar.py::TestUsedMarginForFallback, which stays in place.

Perf: all resolvers are sync (no DB calls).
Reuse: Context dataclass shared with evaluator.
UX: resolvers return None when margin data is unavailable or zero.
"""

import pandas as pd
import pytest
from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_pnl_pct import _metric_pnl_pct


def test_pnl_pct_sums_util_debits_and_net_when_both_nonzero():
    """2026-10 fix: when BOTH util_debits and net are > 0, the
    denominator is their SUM (130000), not just util_debits (50000) —
    the old OR-fallback behaviour this replaces."""
    df_margins = pd.DataFrame([{
        'account': 'ZG0790',
        'util debits': 50000,
        'net': 80000,
    }])
    ctx = Context(df_margins=df_margins)
    row = {'account': 'ZG0790', 'pnl': -1000}
    result = _metric_pnl_pct(ctx, row)
    expected = (-1000 / 130000) * 100.0
    assert result == pytest.approx(expected), (
        f"expected pnl_pct to divide by used+available sum (130000): "
        f"{expected}, got {result}"
    )
    # Lock in the regression this fix closes: the OLD (OR-fallback)
    # denominator must NOT be what's used any more.
    old_denominator_result = (-1000 / 50000) * 100.0
    assert result != pytest.approx(old_denominator_result), (
        "pnl_pct must no longer match the old used_margin_for() "
        "OR-fallback denominator"
    )


def test_pnl_pct_matches_net_when_util_debits_zero():
    """When util_debits is a genuine 0, used+available SUM degenerates
    to `net` alone — numerically identical to the old OR-fallback for
    this specific (single-field-populated) case."""
    df_margins = pd.DataFrame([{
        'account': 'ZG0790',
        'util debits': 0,
        'net': 100000,
    }])
    ctx = Context(df_margins=df_margins)
    row = {'account': 'ZG0790', 'pnl': -3000}
    result = _metric_pnl_pct(ctx, row)
    expected = (-3000 / 100000) * 100.0  # -3.0
    assert result == pytest.approx(expected), (
        f"expected pnl_pct to fall back to net: {expected}, got {result}"
    )


def test_pnl_pct_returns_none_when_both_zero():
    """_metric_pnl_pct should return None when both util_debits and net are 0."""
    df_margins = pd.DataFrame([{
        'account': 'ZG0790',
        'util debits': 0,
        'net': 0,
    }])
    ctx = Context(df_margins=df_margins)
    row = {'account': 'ZG0790', 'pnl': -3000}
    result = _metric_pnl_pct(ctx, row)
    assert result is None, (
        f"expected None when both margins are 0, got {result}"
    )


def test_pnl_pct_returns_none_when_no_margin_data():
    """_metric_pnl_pct should return None when account has no margin data."""
    df_margins = pd.DataFrame([{
        'account': 'OTHER',
        'util debits': 50000,
        'net': 80000,
    }])
    ctx = Context(df_margins=df_margins)
    row = {'account': 'ZG0790', 'pnl': -3000}
    result = _metric_pnl_pct(ctx, row)
    assert result is None, (
        f"expected None when account has no margin data, got {result}"
    )


def test_pnl_pct_handles_positive_pnl():
    """_metric_pnl_pct should handle positive P&L correctly."""
    df_margins = pd.DataFrame([{
        'account': 'ZG0790',
        'util debits': 0,
        'net': 100000,
    }])
    ctx = Context(df_margins=df_margins)
    row = {'account': 'ZG0790', 'pnl': 5000}
    result = _metric_pnl_pct(ctx, row)
    expected = (5000 / 100000) * 100.0  # 5.0
    assert result == pytest.approx(expected), (
        f"expected 5.0, got {result}"
    )


def test_pnl_pct_handles_zero_pnl():
    """_metric_pnl_pct should return 0 when P&L is 0."""
    df_margins = pd.DataFrame([{
        'account': 'ZG0790',
        'util debits': 50000,
        'net': 80000,
    }])
    ctx = Context(df_margins=df_margins)
    row = {'account': 'ZG0790', 'pnl': 0}
    result = _metric_pnl_pct(ctx, row)
    assert result == pytest.approx(0.0), (
        f"expected 0.0 for zero pnl, got {result}"
    )


def test_pnl_pct_handles_missing_pnl_in_row():
    """_metric_pnl_pct should treat missing pnl as 0."""
    df_margins = pd.DataFrame([{
        'account': 'ZG0790',
        'util debits': 50000,
        'net': 80000,
    }])
    ctx = Context(df_margins=df_margins)
    row = {'account': 'ZG0790'}  # No 'pnl' key
    result = _metric_pnl_pct(ctx, row)
    # Should compute (0 / 50000) * 100 = 0
    assert result == pytest.approx(0.0), (
        f"expected 0.0 for missing pnl, got {result}"
    )


def test_pnl_pct_handles_none_pnl_in_row():
    """_metric_pnl_pct should treat None pnl as 0."""
    df_margins = pd.DataFrame([{
        'account': 'ZG0790',
        'util debits': 50000,
        'net': 80000,
    }])
    ctx = Context(df_margins=df_margins)
    row = {'account': 'ZG0790', 'pnl': None}
    result = _metric_pnl_pct(ctx, row)
    # Should compute (0 / 50000) * 100 = 0
    assert result == pytest.approx(0.0), (
        f"expected 0.0 for None pnl, got {result}"
    )


def test_pnl_pct_with_multiple_accounts():
    """_metric_pnl_pct should correctly resolve per-account margins."""
    df_margins = pd.DataFrame([
        {'account': 'ZG0790', 'util debits': 0, 'net': 100000},
        {'account': 'ANOTHER', 'util debits': 50000, 'net': 80000},
    ])
    ctx = Context(df_margins=df_margins)

    # First account: uses net
    row1 = {'account': 'ZG0790', 'pnl': -2000}
    result1 = _metric_pnl_pct(ctx, row1)
    expected1 = (-2000 / 100000) * 100.0  # -2.0
    assert result1 == pytest.approx(expected1), (
        f"expected -2.0 for ZG0790, got {result1}"
    )

    # Second account: both fields nonzero → used+available sum (130000)
    row2 = {'account': 'ANOTHER', 'pnl': -1000}
    result2 = _metric_pnl_pct(ctx, row2)
    expected2 = (-1000 / 130000) * 100.0
    assert result2 == pytest.approx(expected2), (
        f"expected {expected2} for ANOTHER, got {result2}"
    )


def test_pnl_pct_with_large_values():
    """_metric_pnl_pct should handle large margin and P&L values."""
    df_margins = pd.DataFrame([{
        'account': 'ZG0790',
        'util debits': 0,
        'net': 5000000,  # 50 lakhs
    }])
    ctx = Context(df_margins=df_margins)
    row = {'account': 'ZG0790', 'pnl': -250000}  # 2.5 lakh loss
    result = _metric_pnl_pct(ctx, row)
    expected = (-250000 / 5000000) * 100.0  # -5.0
    assert result == pytest.approx(expected), (
        f"expected -5.0%, got {result}%"
    )


def test_pnl_pct_with_small_margin_large_loss():
    """_metric_pnl_pct should compute correct % with small margin and large loss."""
    df_margins = pd.DataFrame([{
        'account': 'ZG0790',
        'util debits': 0,
        'net': 10000,  # 10k margin
    }])
    ctx = Context(df_margins=df_margins)
    row = {'account': 'ZG0790', 'pnl': -2000}
    result = _metric_pnl_pct(ctx, row)
    expected = (-2000 / 10000) * 100.0  # -20.0
    assert result == pytest.approx(expected), (
        f"expected -20.0%, got {result}%"
    )


def test_pnl_pct_does_not_call_used_margin_for():
    """Regression lock for the 2026-10 fix: _metric_pnl_pct must not
    consult Context.used_margin_for() at all any more — it should go
    straight to pnl_math.account_margin_base() instead. Patching
    used_margin_for to raise proves the resolver never calls it."""
    df_margins = pd.DataFrame([{
        'account': 'ZG0790',
        'util debits': 50000,
        'net': 80000,
    }])
    ctx = Context(df_margins=df_margins)

    def _boom(account):
        raise AssertionError("used_margin_for must not be called by _metric_pnl_pct")

    ctx.used_margin_for = _boom
    row = {'account': 'ZG0790', 'pnl': -1000}
    result = _metric_pnl_pct(ctx, row)
    assert result == pytest.approx((-1000 / 130000) * 100.0)


def test_pnl_pct_shares_denominator_with_day_pct():
    """pnl_pct and day_pct must agree on the margin-base denominator —
    both now route through pnl_math.account_margin_base()."""
    from backend.api.algo.pnl_math import account_margin_base
    df_margins = pd.DataFrame([{
        'account': 'ZG0790',
        'util debits': 50000,
        'net': 80000,
    }])
    ctx = Context(df_margins=df_margins)
    row = {'account': 'ZG0790', 'pnl': -1000}

    pnl_pct_result = _metric_pnl_pct(ctx, row)
    base = account_margin_base(df_margins, 'ZG0790')
    day_pct_equivalent = (-1000 / base) * 100.0
    assert pnl_pct_result == pytest.approx(day_pct_equivalent)
