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
    """`_metric_pnl` migrated to grammar_tokens/metric_pnl.py (Wave 1,
    batch 1) — resolver function bodies now live under the per-token
    package, not inline in grammar.py's own source text."""
    from backend.api.algo.grammar_tokens.metric_pnl import _metric_pnl
    assert callable(_metric_pnl), "_metric_pnl resolver function must exist and be callable"


def test_metric_functions_take_ctx_and_row():
    """All metric resolver functions under grammar_tokens/ must take
    (ctx, row) arguments. Retargeted (Wave 1) from a grammar.py source
    scan — metric resolver bodies now live under the per-token package."""
    import re
    from pathlib import Path
    pkg_dir = Path("backend/api/algo/grammar_tokens")
    combined_src = "\n".join(
        p.read_text() for p in sorted(pkg_dir.glob("metric_*.py"))
    )
    metric_defs = re.findall(r"def (_metric_\w+)\s*\(ctx,\s*row\)", combined_src)
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
    """Window metrics (30m, 1h) must exist for rolling-average conditions.
    Retargeted (Wave 1, batch 2) — these fixed-window bindings migrated
    to grammar_tokens/metric_mean_pnl.py, alongside their factory."""
    from backend.api.algo.grammar_tokens.metric_mean_pnl import (
        _metric_mean_pnl_30m, _metric_mean_pnl_1h,
    )
    assert callable(_metric_mean_pnl_30m), "_metric_mean_pnl_30m must exist"
    assert callable(_metric_mean_pnl_1h), "_metric_mean_pnl_1h must exist"


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #3 — missing-vs-zero convention for raw-broker-column funds metrics
# ═══════════════════════════════════════════════════════════════════════════
#
# Resolver-level tests for avail_margin / used_margin / collateral moved to
# backend/tests/grammar_tokens/test_metric_avail_margin.py,
# test_metric_used_margin.py, test_metric_collateral.py (Wave 1, batch 1).


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #4 — live cash metric (was reading start-of-day opening_balance)
# ═══════════════════════════════════════════════════════════════════════════
#
# Resolver-level tests for cash / sod_cash moved to
# backend/tests/grammar_tokens/test_metric_cash.py, test_metric_sod_cash.py
# (Wave 1, batch 1). The catalog-registration test below stays here — it
# asserts on SYSTEM_TOKENS, not on either resolver function directly.

class TestLiveCashMetric:
    def test_sod_cash_token_registered(self):
        from backend.api.algo.grammar import SYSTEM_TOKENS
        tokens = {(t['grammar_kind'], t['token_kind'], t['token']) for t in SYSTEM_TOKENS}
        assert ('condition', 'metric', 'sod_cash') in tokens, (
            "sod_cash must be a registered system token"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Sprint 6 (agent CLI grammar) — ntfy registered as an authorable channel
# ═══════════════════════════════════════════════════════════════════════════

class TestNtfyChannelCatalogToken:
    """`ntfy` already dispatches in events.py/event_agents.py — this is just
    the catalog-loader registration so the agent CLI grammar's
    autocomplete/validation surfaces it like every other notify channel."""

    def test_ntfy_channel_token_registered(self):
        from backend.api.algo.grammar import SYSTEM_TOKENS
        tokens = {(t['grammar_kind'], t['token_kind'], t['token']) for t in SYSTEM_TOKENS}
        assert ('notify', 'channel', 'ntfy') in tokens

    def test_ntfy_channel_token_has_priority_params_schema(self):
        from backend.api.algo.grammar import SYSTEM_TOKENS
        by_key = {(t['grammar_kind'], t['token_kind'], t['token']): t for t in SYSTEM_TOKENS}
        tok = by_key[('notify', 'channel', 'ntfy')]
        assert tok['value_type'] == 'enum'
        assert tok['params_schema']['priority']['type'] == 'enum'
        assert tok['params_schema']['priority']['enum'] == ['low', 'default', 'high', 'urgent']
        assert tok['params_schema']['priority']['required'] is False


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #10 — day_pct metric prefers margin-based column over notional
# ═══════════════════════════════════════════════════════════════════════════
#
# Resolver-level tests moved to
# backend/tests/grammar_tokens/test_metric_day_pct.py (Wave 1, batch 1).


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #11 — worst-case scope key fix (was always returning [])
# ═══════════════════════════════════════════════════════════════════════════
#
# Resolver-level tests moved to backend/tests/grammar_tokens/
# test_scope_holdings_worst_acct.py (Wave 1, scope batch B of the DSL
# token-unification plan), and test_scope_positions_worst_acct.py /
# test_scope_positions_worst_symbol.py (Wave 1, scope batch C).
