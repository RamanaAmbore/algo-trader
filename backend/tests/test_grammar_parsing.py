"""
Tests for api/algo/grammar.py — condition metric resolvers.
SSOT: OPERATORS dict is the authoritative comparison operator map.
Perf: metric functions are pure Python (no DB/broker calls in resolvers).
Stale: no hardcoded month strings — month map computed from date math.
Reuse: all metric resolvers share (ctx, row) calling convention.
UX: metric resolvers return numeric values for consistent condition evaluation.
"""
from pathlib import Path

_SRC = Path("backend/api/algo/grammar.py").read_text()


def test_operators_dict_exists():
    from backend.api.algo.grammar import OPERATORS
    assert isinstance(OPERATORS, dict), "OPERATORS must be a dict"
    assert len(OPERATORS) > 0, "OPERATORS must have at least one operator"


def test_operators_has_gt_lt():
    from backend.api.algo.grammar import OPERATORS
    assert ">" in OPERATORS or "gt" in OPERATORS, "OPERATORS must include > (greater than)"
    assert "<" in OPERATORS or "lt" in OPERATORS, "OPERATORS must include < (less than)"


def test_operators_values_are_callable():
    from backend.api.algo.grammar import OPERATORS
    for op_name, fn in OPERATORS.items():
        assert callable(fn), f"OPERATORS['{op_name}'] must be a callable"


def test_metric_pnl_resolver_exists():
    assert "_metric_pnl" in _SRC, "_metric_pnl resolver function must exist in grammar.py"


def test_metric_functions_take_ctx_and_row():
    """All metric resolver functions must take (ctx, row) arguments."""
    import re
    # Find metric function definitions
    metric_defs = re.findall(r"def (_metric_\w+)\s*\(ctx,\s*row\)", _SRC)
    assert len(metric_defs) >= 3, (
        f"At least 3 _metric_* functions with (ctx, row) signature expected, "
        f"found: {metric_defs}"
    )


def test_no_hardcoded_month_strings():
    """Month strings (JAN, FEB, ...) must not be hardcoded in grammar.py.
    They come from the grammar token DB or computed dynamically."""
    import re
    # Check that month abbreviations in grammar.py are in computed structures
    # (not literals used for parsing) — the grammar module processes condition
    # trees, not order strings with months
    month_literals = re.findall(r'"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"', _SRC)
    # Allow zero or few month literals (for test/example values), not a full month map
    assert len(month_literals) < 12, (
        f"grammar.py must not hardcode all 12 month abbreviations as literals; "
        f"found {len(month_literals)} month strings"
    )


def test_window_metric_functions_exist():
    """Window metrics (30m, 1h) must exist for rolling-average conditions."""
    assert "_metric_mean_pnl_30m" in _SRC, "_metric_mean_pnl_30m must exist"
    assert "_metric_mean_pnl_1h" in _SRC, "_metric_mean_pnl_1h must exist"


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #3 — missing-vs-zero convention for raw-broker-column funds metrics
# ═══════════════════════════════════════════════════════════════════════════

class TestMissingVsZeroConvention:
    """A genuinely-absent/None/NaN broker column must resolve to None
    (leaf skipped), never a coerced 0.0 — the operator's explicit ask:
    "if margin or cash of 0 should not generate alerts [when the 0 is
    actually missing data]". A TRUE reported 0 must still pass through
    (real zero balances must still be able to alert)."""

    def test_avail_margin_none_when_column_absent(self):
        from backend.api.algo.grammar import _metric_avail_margin
        row = {'account': 'DH1234'}  # 'net' column never present
        assert _metric_avail_margin(None, row) is None, (
            "Missing 'net' column must resolve to None, not 0"
        )

    def test_avail_margin_true_zero_passes_through(self):
        from backend.api.algo.grammar import _metric_avail_margin
        row = {'account': 'ZD1234', 'net': 0.0}
        assert _metric_avail_margin(None, row) == 0.0, (
            "A REAL reported 0 must pass through unchanged, not be treated as missing"
        )

    def test_avail_margin_none_when_nan(self):
        import math
        from backend.api.algo.grammar import _metric_avail_margin
        row = {'account': 'GR1234', 'net': float('nan')}
        assert _metric_avail_margin(None, row) is None, (
            "NaN (pandas representation of a None from json_normalize) must resolve to None"
        )

    def test_used_margin_none_when_missing(self):
        from backend.api.algo.grammar import _metric_used_margin
        assert _metric_used_margin(None, {}) is None

    def test_collateral_none_when_missing(self):
        from backend.api.algo.grammar import _metric_collateral
        assert _metric_collateral(None, {}) is None

    def test_avail_margin_negative_real_value_passes_through(self):
        """Prod repro guard: a genuinely negative margin must still fire —
        only MISSING data is suppressed, not real risk events."""
        from backend.api.algo.grammar import _metric_avail_margin
        row = {'account': 'ZD1234', 'net': -1500.0}
        assert _metric_avail_margin(None, row) == -1500.0


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #4 — live cash metric (was reading start-of-day opening_balance)
# ═══════════════════════════════════════════════════════════════════════════

class TestLiveCashMetric:
    def test_cash_reads_avail_cash_not_opening_balance(self):
        """cash must move intraday — reads 'avail cash' (live), NOT
        'avail opening_balance' (frozen SOD figure the old resolver read)."""
        from backend.api.algo.grammar import _metric_cash
        row = {'avail cash': -500.0, 'avail opening_balance': 200000.0}
        result = _metric_cash(None, row)
        assert result == -500.0, (
            f"Expected live 'avail cash' (-500.0), got {result} — "
            f"cash<0 must be able to fire on a real intraday drop even "
            f"when the SOD opening balance was healthy"
        )

    def test_cash_none_when_missing(self):
        from backend.api.algo.grammar import _metric_cash
        assert _metric_cash(None, {}) is None

    def test_sod_cash_reads_opening_balance(self):
        """sod_cash (new token) preserves the OLD `cash` behaviour for
        agents that specifically want the start-of-day baseline."""
        from backend.api.algo.grammar import _metric_sod_cash
        row = {'avail cash': -500.0, 'avail opening_balance': 200000.0}
        assert _metric_sod_cash(None, row) == 200000.0

    def test_sod_cash_token_registered(self):
        from backend.api.algo.grammar import SYSTEM_TOKENS
        tokens = {(t['grammar_kind'], t['token_kind'], t['token']) for t in SYSTEM_TOKENS}
        assert ('condition', 'metric', 'sod_cash') in tokens, (
            "sod_cash must be a registered system token"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #10 — day_pct metric prefers margin-based column over notional
# ═══════════════════════════════════════════════════════════════════════════

class TestDayPctMarginDenominator:
    def test_prefers_margin_column_when_present(self):
        from backend.api.algo.grammar import _metric_day_pct
        row = {'day_change_percentage': -500.0, 'day_change_pct_margin': -5.2}
        result = _metric_day_pct(None, row)
        assert result == -5.2, (
            f"Expected the margin-based figure (-5.2), not the notional "
            f"figure (-500.0) that produced the worked-example blowup, got {result}"
        )

    def test_none_when_margin_unavailable_no_notional_fallback(self):
        """When the margin column exists but is NaN (margin data
        unavailable for that account this tick), the leaf must skip —
        NEVER silently fall back to the broken notional figure."""
        import math
        from backend.api.algo.grammar import _metric_day_pct
        row = {'day_change_percentage': -500.0, 'day_change_pct_margin': float('nan')}
        assert _metric_day_pct(None, row) is None

    def test_holdings_row_without_margin_column_uses_notional(self):
        """Holdings rows never carry day_change_pct_margin — they keep
        the standard opening-value-denominator day_change_percentage."""
        from backend.api.algo.grammar import _metric_day_pct
        row = {'day_change_percentage': -3.2}
        assert _metric_day_pct(None, row) == -3.2


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #11 — worst-case scope key fix (was always returning [])
# ═══════════════════════════════════════════════════════════════════════════

class TestWorstScopeKeyFix:
    def test_holdings_worst_acct_keys_on_day_change_percentage(self):
        from backend.api.algo.grammar import _scope_holdings_worst_acct
        from backend.api.algo.agent_evaluator import Context
        import pandas as pd
        df = pd.DataFrame([
            {'account': 'ACCT1', 'day_change_percentage': -1.0},
            {'account': 'ACCT2', 'day_change_percentage': -4.5},
        ])
        ctx = Context(sum_holdings=df)
        result = _scope_holdings_worst_acct(ctx)
        assert len(result) == 1, f"Expected exactly 1 row, got {len(result)} — key fix regressed"
        assert result[0]['account'] == 'ACCT2', (
            f"Expected the worst account (ACCT2, -4.5%), got {result[0]['account']}"
        )

    def test_positions_worst_acct_keys_on_day_change_percentage(self):
        from backend.api.algo.grammar import _scope_positions_worst_acct
        from backend.api.algo.agent_evaluator import Context
        import pandas as pd
        df = pd.DataFrame([
            {'account': 'ACCT1', 'day_change_percentage': -0.5},
            {'account': 'ACCT2', 'day_change_percentage': -3.1},
        ])
        ctx = Context(sum_positions=df)
        result = _scope_positions_worst_acct(ctx)
        assert len(result) == 1, f"Expected exactly 1 row, got {len(result)} — key fix regressed"
        assert result[0]['account'] == 'ACCT2'

    def test_positions_worst_symbol_reads_position_rows_not_positions_rows(self):
        """Was reading ctx.positions_rows (doesn't exist); real field is
        ctx.position_rows (singular 'position')."""
        from backend.api.algo.grammar import _scope_positions_worst_symbol
        from backend.api.algo.agent_evaluator import Context
        rows = [
            {'tradingsymbol': 'NIFTY26SEPFUT', 'pnl': -100.0},
            {'tradingsymbol': 'BANKNIFTY26SEPFUT', 'pnl': -9000.0},
        ]
        ctx = Context(position_rows=rows)
        result = _scope_positions_worst_symbol(ctx)
        assert len(result) == 1, f"Expected exactly 1 row, got {len(result)}"
        assert result[0]['tradingsymbol'] == 'BANKNIFTY26SEPFUT', (
            f"Expected the worst pnl row, got {result[0]['tradingsymbol']}"
        )

    def test_positions_worst_symbol_falls_back_when_no_rows(self):
        from backend.api.algo.grammar import _scope_positions_worst_symbol
        from backend.api.algo.agent_evaluator import Context
        import pandas as pd
        df = pd.DataFrame([{'account': 'ACCT1', 'day_change_percentage': -2.0}])
        ctx = Context(sum_positions=df, position_rows=[])
        result = _scope_positions_worst_symbol(ctx)
        assert result == [{'account': 'ACCT1', 'day_change_percentage': -2.0}], (
            f"Expected fallback to worst_acct row, got {result}"
        )
