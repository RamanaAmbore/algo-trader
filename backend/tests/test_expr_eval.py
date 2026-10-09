"""
Adversarial safety suite for backend/api/algo/expr_eval.py.

This evaluator sits directly upstream of real order placement
(`actions.py:resolve_action_params`) — every one of these cases must
raise `ExprError` and NOTHING ELSE. A bare `except Exception` catch
anywhere downstream must only ever see `ExprError` instances from this
module; no `TypeError`/`ZeroDivisionError`/`RecursionError`/`SyntaxError`
may escape `eval_expr` itself.

Perf: pure in-memory AST parsing/walking, no DB/network/fixtures.
"""

import math

import pytest

from backend.api.algo.expr_eval import ExprError, eval_expr


def _assert_only_expr_error(expr: str, namespace: dict | None = None):
    """Call eval_expr and assert it raises ExprError — and, separately,
    that nothing else would leak past a bare `except Exception` catch."""
    ns = namespace if namespace is not None else {}
    with pytest.raises(ExprError):
        eval_expr(expr, ns)
    # Separately prove no OTHER exception type is observable: a generic
    # except Exception catch always resolves to ExprError here.
    try:
        eval_expr(expr, ns)
        raised = None
    except Exception as e:
        raised = e
    assert isinstance(raised, ExprError), (
        f"expected only ExprError to leak for {expr!r}, got {type(raised)}"
    )


# ─────────────────────────────────────────────────────────────────────────
# Banned node types — each a distinct AST node, tested individually
# ─────────────────────────────────────────────────────────────────────────

class TestBannedNodeTypes:
    def test_call_import_os_system_rejected(self):
        _assert_only_expr_error("__import__('os').system('x')")

    def test_attribute_access_rejected(self):
        _assert_only_expr_error("().__class__")

    def test_attribute_chain_rejected(self):
        _assert_only_expr_error("[].__class__.__bases__")

    def test_subscript_rejected(self):
        _assert_only_expr_error("a[0]", {"a": [1, 2, 3]})

    def test_lambda_rejected(self):
        _assert_only_expr_error("lambda x: x")

    def test_list_comprehension_rejected(self):
        _assert_only_expr_error("[x for x in range(3)]")

    def test_set_comprehension_rejected(self):
        _assert_only_expr_error("{x for x in range(3)}")

    def test_dict_comprehension_rejected(self):
        _assert_only_expr_error("{x: x for x in range(3)}")

    def test_generator_expression_rejected(self):
        _assert_only_expr_error("(x for x in range(3))")

    def test_fstring_rejected(self):
        _assert_only_expr_error('f"{1}"')

    def test_walrus_rejected(self):
        _assert_only_expr_error("(n := 5)")

    def test_ifexp_rejected(self):
        _assert_only_expr_error("1 if True else 2")

    def test_tuple_rejected(self):
        _assert_only_expr_error("(1, 2)")

    def test_list_literal_rejected(self):
        _assert_only_expr_error("[1, 2]")

    def test_dict_literal_rejected(self):
        _assert_only_expr_error("{1: 2}")

    def test_set_literal_rejected(self):
        _assert_only_expr_error("{1, 2}")

    def test_starred_rejected(self):
        _assert_only_expr_error("*a,", {"a": 1})

    def test_string_constant_rejected(self):
        _assert_only_expr_error("'hello'")

    def test_bytes_constant_rejected(self):
        _assert_only_expr_error("b'x'")

    def test_none_constant_rejected(self):
        _assert_only_expr_error("None")

    def test_ellipsis_constant_rejected(self):
        _assert_only_expr_error("...")

    def test_complex_constant_rejected(self):
        _assert_only_expr_error("1j")


# ─────────────────────────────────────────────────────────────────────────
# Runtime arithmetic failures
# ─────────────────────────────────────────────────────────────────────────

class TestArithmeticFailures:
    def test_division_by_zero(self):
        with pytest.raises(ExprError, match="division by zero"):
            eval_expr("1/0", {})

    def test_modulo_by_zero(self):
        with pytest.raises(ExprError, match="division by zero"):
            eval_expr("1%0", {})

    def test_pow_zero_negative_exponent_zero_division(self):
        """0 ** -1 raises ZeroDivisionError in raw Python, not Div/Mod —
        must still surface as ExprError, not a raw ZeroDivisionError."""
        _assert_only_expr_error("0**-1")

    def test_pow_magnitude_guard_large_exponent_chain(self):
        """10**10**10 — Pow is right-associative, so the inner 10**10
        computes fine (fast, no overflow — Python ints are unbounded) and
        the OUTER pow's exponent (1e10) trips the magnitude guard BEFORE
        ever attempting to compute it. Must reject fast, not hang."""
        _assert_only_expr_error("10**10**10")

    def test_pow_magnitude_guard_large_single_exponent(self):
        _assert_only_expr_error("2**1000")

    def test_pow_complex_result_rejected(self):
        """(-8) ** 0.5 produces a complex number in raw Python — not a
        modelled result type (int/float/bool) — must be rejected
        explicitly rather than leaking a complex value out."""
        _assert_only_expr_error("(-8)**0.5")

    def test_non_finite_float_constant_rejected(self):
        """1e309 parses to an `inf` float constant — rejected by the
        non-finite constant check."""
        _assert_only_expr_error("1e309")

    def test_non_finite_result_rejected(self):
        """Arithmetic that produces inf at runtime (not just a literal)
        is also rejected."""
        _assert_only_expr_error("1e308 * 10")


# ─────────────────────────────────────────────────────────────────────────
# Depth / length guards
# ─────────────────────────────────────────────────────────────────────────

class TestDepthAndLengthGuards:
    def test_deeply_nested_parens_depth_25_rejected(self):
        """Plain parens around a single literal create ZERO extra AST
        nodes — this is caught by the raw-text paren-depth guard, not
        the AST-node depth guard."""
        expr = "(" * 25 + "1" + ")" * 25
        _assert_only_expr_error(expr)

    def test_long_expression_string_rejected_fast(self):
        """A 10,000-char expression is rejected by the length cap, before
        any real parsing work."""
        expr = "1+" * 5000 + "1"  # 10001 chars
        assert len(expr) > 200
        _assert_only_expr_error(expr)

    def test_deeply_nested_unary_operators_rejected(self):
        """Real nested UnaryOp nodes (not just parens) — caught by the
        AST-node depth guard."""
        expr = "-" * 25 + "1"
        _assert_only_expr_error(expr)


# ─────────────────────────────────────────────────────────────────────────
# Correct, positive results
# ─────────────────────────────────────────────────────────────────────────

class TestCorrectPositiveResults:
    def test_arithmetic_with_parens_and_namespace(self):
        result = eval_expr("(lots * 2) + buffer", {"lots": 5, "buffer": 3})
        assert result == 13
        assert not isinstance(result, bool)

    def test_float_multiplication(self):
        result = eval_expr("entry_price * 1.05", {"entry_price": 100})
        assert result == pytest.approx(105.0)

    def test_logical_and_strict_bool(self):
        result = eval_expr("a and b", {"a": True, "b": True})
        assert result is True

    def test_logical_and_false_strict_bool(self):
        result = eval_expr("a and b", {"a": True, "b": False})
        assert result is False

    def test_logical_or_strict_bool(self):
        result = eval_expr("a or b", {"a": False, "b": True})
        assert result is True

    def test_logical_or_no_value_leakage(self):
        """Python's native `0 or 5` evaluates to 5 (the operand itself).
        Here it must always coerce to a strict bool, never leak a raw
        operand value."""
        result = eval_expr("a or b", {"a": 0, "b": 5})
        assert result is True
        assert type(result) is bool

    def test_logical_not(self):
        result = eval_expr("not a", {"a": False})
        assert result is True
        assert type(result) is bool

    def test_equality_true(self):
        result = eval_expr("a == b", {"a": 5, "b": 5})
        assert result is True

    def test_equality_false(self):
        result = eval_expr("a == b", {"a": 5, "b": 6})
        assert result is False


# ─────────────────────────────────────────────────────────────────────────
# Comparisons — chained, undefined-in-namespace, undefined-entirely
# ─────────────────────────────────────────────────────────────────────────

class TestComparisonEdgeCases:
    def test_chained_comparison_rejected(self):
        with pytest.raises(ExprError, match="chained comparisons not supported"):
            eval_expr("a < b < c", {"a": 1, "b": 2, "c": 3})

    def test_comparison_against_none_fails_closed(self):
        """Deliberate divergence from grammar.py:OPERATORS (which fails
        OPEN / treats None as False) — here a None operand in a
        Compare must raise, not silently evaluate to False."""
        with pytest.raises(ExprError, match="comparison against undefined value"):
            eval_expr("a < b", {"a": None, "b": 5})

    def test_undefined_name_not_in_namespace(self):
        with pytest.raises(ExprError, match="undefined name"):
            eval_expr("a + 1", {})


# ─────────────────────────────────────────────────────────────────────────
# Non-ExprError inputs are still handled safely
# ─────────────────────────────────────────────────────────────────────────

class TestMiscSafety:
    def test_non_string_expr_rejected(self):
        with pytest.raises(ExprError):
            eval_expr(123, {})  # type: ignore[arg-type]

    def test_syntax_error_rejected(self):
        _assert_only_expr_error("1 +")

    def test_result_is_never_none(self):
        """Every successful eval_expr call returns int/float/bool —
        never None — by construction (no node type can produce None as
        an allowed Constant, and the final gate would reject it anyway)."""
        result = eval_expr("1 + 1", {})
        assert result is not None
        assert math.isfinite(result)
