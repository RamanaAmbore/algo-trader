"""
Tests for api/algo/grammar.py — metric resolvers.
SSOT: _metric_pnl_pct uses Context.used_margin_for() with fallback logic.
Perf: all resolvers are sync (no DB calls).
Stale: used_margin_for now falls back to 'net' when 'util debits' is 0.
Reuse: Context dataclass shared with evaluator.
UX: resolvers return None when margin data is unavailable or zero.
"""

import pandas as pd
import pytest
from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar import _metric_pnl_pct


class TestUsedMarginForFallback:
    """Test Context.used_margin_for() fallback to net when util_debits=0."""

    def test_used_margin_for_falls_back_to_net(self):
        """When util_debits=0, used_margin_for should return net value."""
        df_margins = pd.DataFrame([{
            'account': 'ZG0790',
            'util debits': 0,
            'net': 75000,
        }])
        ctx = Context(df_margins=df_margins)
        result = ctx.used_margin_for('ZG0790')
        assert result == 75000.0, (
            f"expected used_margin_for to fall back to net (75000.0) "
            f"when util_debits is 0, got {result}"
        )

    def test_used_margin_for_uses_util_debits_when_nonzero(self):
        """When util_debits > 0, used_margin_for should use it (not net)."""
        df_margins = pd.DataFrame([{
            'account': 'ZG0790',
            'util debits': 50000,
            'net': 80000,
        }])
        ctx = Context(df_margins=df_margins)
        result = ctx.used_margin_for('ZG0790')
        assert result == 50000.0, (
            f"expected used_margin_for to use util_debits (50000.0) "
            f"when it is > 0, got {result}"
        )

    def test_used_margin_for_returns_none_when_both_zero(self):
        """When both util_debits and net are 0, should return None."""
        df_margins = pd.DataFrame([{
            'account': 'ZG0790',
            'util debits': 0,
            'net': 0,
        }])
        ctx = Context(df_margins=df_margins)
        result = ctx.used_margin_for('ZG0790')
        assert result is None, (
            f"expected used_margin_for to return None when both "
            f"util_debits and net are 0, got {result}"
        )

    def test_used_margin_for_returns_none_when_net_negative(self):
        """When net is negative, should return None (not a valid margin)."""
        df_margins = pd.DataFrame([{
            'account': 'ZG0790',
            'util debits': 0,
            'net': -5000,
        }])
        ctx = Context(df_margins=df_margins)
        result = ctx.used_margin_for('ZG0790')
        assert result is None, (
            f"expected used_margin_for to return None when net is negative, "
            f"got {result}"
        )

    def test_used_margin_for_handles_empty_df(self):
        """used_margin_for should return None when df_margins is empty."""
        ctx = Context(df_margins=pd.DataFrame())
        result = ctx.used_margin_for('ZG0790')
        assert result is None, "expected None for empty df_margins"

    def test_used_margin_for_handles_none_df(self):
        """used_margin_for should return None when df_margins is None."""
        ctx = Context(df_margins=None)
        result = ctx.used_margin_for('ZG0790')
        assert result is None, "expected None for None df_margins"

    def test_used_margin_for_handles_none_account(self):
        """used_margin_for should return None when account is None."""
        df_margins = pd.DataFrame([{
            'account': 'ZG0790',
            'util debits': 50000,
            'net': 80000,
        }])
        ctx = Context(df_margins=df_margins)
        result = ctx.used_margin_for(None)
        assert result is None, "expected None for None account"

    def test_used_margin_for_missing_account(self):
        """used_margin_for should return None when account not in df."""
        df_margins = pd.DataFrame([{
            'account': 'ZG0790',
            'util debits': 50000,
            'net': 80000,
        }])
        ctx = Context(df_margins=df_margins)
        result = ctx.used_margin_for('DOESNOTEXIST')
        assert result is None, "expected None for missing account"


class TestMetricPnlPct:
    """Test _metric_pnl_pct resolver using used_margin_for fallback."""

    def test_pnl_pct_uses_util_debits_when_nonzero(self):
        """_metric_pnl_pct should use util_debits when it's > 0."""
        df_margins = pd.DataFrame([{
            'account': 'ZG0790',
            'util debits': 50000,
            'net': 80000,
        }])
        ctx = Context(df_margins=df_margins)
        row = {'account': 'ZG0790', 'pnl': -1000}
        result = _metric_pnl_pct(ctx, row)
        expected = (-1000 / 50000) * 100.0  # -2.0
        assert result == pytest.approx(expected), (
            f"expected pnl_pct to use util_debits: {expected}, got {result}"
        )

    def test_pnl_pct_falls_back_to_net_when_util_debits_zero(self):
        """_metric_pnl_pct should fall back to net when util_debits=0."""
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

    def test_pnl_pct_returns_none_when_both_zero(self):
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

    def test_pnl_pct_returns_none_when_no_margin_data(self):
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

    def test_pnl_pct_handles_positive_pnl(self):
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

    def test_pnl_pct_handles_zero_pnl(self):
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

    def test_pnl_pct_handles_missing_pnl_in_row(self):
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

    def test_pnl_pct_handles_none_pnl_in_row(self):
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

    def test_pnl_pct_with_multiple_accounts(self):
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

        # Second account: uses util_debits
        row2 = {'account': 'ANOTHER', 'pnl': -1000}
        result2 = _metric_pnl_pct(ctx, row2)
        expected2 = (-1000 / 50000) * 100.0  # -2.0
        assert result2 == pytest.approx(expected2), (
            f"expected -2.0 for ANOTHER, got {result2}"
        )

    def test_pnl_pct_with_large_values(self):
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

    def test_pnl_pct_with_small_margin_large_loss(self):
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
