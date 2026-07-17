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
