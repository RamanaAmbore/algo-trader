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

    # test_string_constant_rejected intentionally REMOVED/FLIPPED here —
    # see TestStringSupport.test_string_constant_accepted below. `str`
    # constants were disallowed pre-string-support-extension; they are
    # now a deliberately supported, narrow addition (see expr_eval.py's
    # module docstring). `bytes` stays rejected (next test, unchanged).

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


# ─────────────────────────────────────────────────────────────────────────
# String support (added — str constants + str + str concatenation)
# ─────────────────────────────────────────────────────────────────────────

from backend.api.algo.expr_eval import _MAX_STRING_LEN  # noqa: E402


class TestStringSupport:
    def test_string_constant_accepted(self):
        """Pre-extension this raised ExprError (see the removed
        test_string_constant_rejected note above in TestBannedNodeTypes).
        A bare str constant is now a valid, narrow addition."""
        result = eval_expr("'hello'", {})
        assert result == "hello"
        assert type(result) is str

    def test_string_constant_within_length_guard_accepted(self):
        s = "x" * _MAX_STRING_LEN
        result = eval_expr(f"'{s}'", {})
        assert result == s

    def test_string_constant_exceeding_length_guard_rejected(self):
        """_MAX_STRING_LEN is deliberately set below _MAX_EXPR_LEN (see
        expr_eval.py comment) specifically so this case is caught by the
        string-constant guard itself, not by the overall expression-
        length guard tripping first."""
        s = "x" * (_MAX_STRING_LEN + 1)
        expr = f"'{s}'"
        assert len(expr) <= 200, "test must stay under _MAX_EXPR_LEN to isolate the string guard"
        _assert_only_expr_error(expr)

    def test_namespace_string_exceeding_length_guard_rejected(self):
        """A bare Name lookup returning an oversized namespace-supplied
        string (no Constant, no BinOp) is caught by the centralized
        backstop at the end of eval_expr."""
        long_str = "x" * (_MAX_STRING_LEN + 1)
        _assert_only_expr_error("a", {"a": long_str})

    def test_string_plus_string_concatenation(self):
        result = eval_expr("a + b", {"a": "NFO", "b": "-close"})
        assert result == "NFO-close"
        assert type(result) is str

    def test_string_literal_plus_string_literal_concatenation(self):
        result = eval_expr("'foo' + 'bar'", {})
        assert result == "foobar"

    def test_string_plus_number_rejected_as_type_error(self):
        """str + number is a type error — never silently coerced via
        str()."""
        _assert_only_expr_error("a + b", {"a": "qty-", "b": 5})

    def test_number_plus_string_rejected_as_type_error(self):
        _assert_only_expr_error("a + b", {"a": 5, "b": "qty-"})

    def test_string_minus_rejected(self):
        """Every BinOp operator other than Add stays strictly
        numeric-only — a string operand on Sub is rejected before the
        operator is ever invoked."""
        _assert_only_expr_error("a - b", {"a": "x", "b": "y"})

    def test_string_mult_rejected_not_executed_as_repeat(self):
        """'a' * 999999999 would be a memory-exhaustion bomb in raw
        Python (str repetition) — must be rejected outright, not
        executed and then caught after the fact."""
        _assert_only_expr_error("a * b", {"a": "x", "b": 999999999})

    def test_string_percent_rejected_not_executed_as_format(self):
        """'%s' % x is printf-style string formatting in raw Python —
        must be rejected outright, never executed."""
        _assert_only_expr_error("a % b", {"a": "%s", "b": 5})

    def test_string_pow_rejected(self):
        _assert_only_expr_error("a ** b", {"a": "x", "b": 2})

    def test_string_div_rejected(self):
        _assert_only_expr_error("a / b", {"a": "x", "b": 2})

    def test_concatenated_string_exceeding_length_guard_rejected(self):
        a = "x" * _MAX_STRING_LEN
        b = "y" * _MAX_STRING_LEN
        _assert_only_expr_error("a + b", {"a": a, "b": b})

    def test_string_result_flows_through_allowed_result_types(self):
        """Top-level eval_expr result-type gate must accept a plain str
        result end to end (not just inside _eval_binop/_eval_constant)."""
        result = eval_expr("a", {"a": "hello"})
        assert result == "hello"

    def test_existing_numeric_behavior_unchanged_arithmetic(self):
        """Regression guard: ordinary numeric arithmetic must behave
        identically after the string-support extension."""
        result = eval_expr("(lots * 2) + buffer", {"lots": 5, "buffer": 3})
        assert result == 13

    def test_existing_numeric_behavior_unchanged_boolean(self):
        result = eval_expr("a and b", {"a": True, "b": True})
        assert result is True

    def test_bool_is_not_confused_with_string_operand(self):
        """bool is an int subclass in Python — confirm the new
        str-operand check never misfires on a bool operand."""
        result = eval_expr("a + b", {"a": True, "b": 1})
        assert result == 2
        assert type(result) is int
