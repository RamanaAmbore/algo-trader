"""
Tests for api/algo/grammar.py — metric resolvers.

2026-10 audit fix: `_metric_pnl_pct` no longer uses
`Context.used_margin_for()` (an OR-fallback: util_debits if > 0, else
net). It now shares `pnl_math.account_margin_base()` — the SAME
used+available SUM denominator `day_pct` already uses (fix #10) — so
pnl_pct and day_pct agree on what "% of margin" means. The two
denominators coincide numerically whenever util_debits is 0 (SUM
degenerates to net, same as the old fallback), which is why several
single-field test cases below are numerically unchanged; cases with
BOTH util_debits and net nonzero are the ones that actually changed.

`Context.used_margin_for()` itself is untested-for-removal here — its
own behaviour (OR-fallback) is unchanged and still exercised by
`TestUsedMarginForFallback` below; it is simply no longer the
denominator `_metric_pnl_pct` reads.

Resolver-level tests for `_metric_pnl_pct` itself moved to
backend/tests/grammar_tokens/test_metric_pnl_pct.py (Wave 1, batch 1 of
the DSL token-unification plan).

Perf: all resolvers are sync (no DB calls).
Reuse: Context dataclass shared with evaluator.
UX: resolvers return None when margin data is unavailable or zero.
"""

import pandas as pd
import pytest
from backend.api.algo.agent_evaluator import Context


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

