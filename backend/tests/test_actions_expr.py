"""
Tests for backend/api/algo/actions.py:resolve_action_params — the
action-param expression-resolution layer wired into execute().

Covers:
  - Zero-behavior-change invariant: a fully-literal-numeric params dict
    passes through byte-identical (same values), and the input dict is
    never mutated in place.
  - A legitimate expression (`qty: "(lots*2)+buffer"`) resolves correctly
    against sibling literal params.
  - Bool-into-numeric-field rejection (`qty: "a==b"`).
  - Float-into-integer-field (qty/new_qty) rejection, no silent truncation.
  - Division-by-zero and undefined-sibling-name propagate as ExprError.
  - string/enum-typed fields are never evaluated even if marked
    token_ref_ok (defensive — no such field exists today, but the
    activation-rule type check must hold regardless).

No DB / fixtures needed — pure unit tests against the static YAML-derived
params_schema catalog.
"""

import pytest

from backend.api.algo.actions import resolve_action_params
from backend.api.algo.expr_eval import ExprError


class TestZeroBehaviorChangeInvariant:
    def test_fully_literal_params_pass_through_byte_identical(self):
        params = {
            "account": "ZG0790",
            "symbol": "NIFTY25JULFUT",
            "exchange": "NFO",
            "side": "SELL",
            "qty": 5,
            "order_type": "LIMIT",
            "price": 24500.5,
            "trigger_price": 24490.0,
            "product": "NRML",
        }
        resolved = resolve_action_params("place_order", params, {})
        assert resolved == params
        assert resolved is not params  # new dict, never the same object

    def test_input_dict_never_mutated(self):
        params = {"account": "ZG0790", "symbol": "X", "side": "BUY", "qty": 1}
        original_id = id(params)
        snapshot = dict(params)
        resolve_action_params("place_order", params, {})
        assert id(params) == original_id
        assert params == snapshot

    def test_unknown_action_type_passes_through_unchanged(self):
        params = {"foo": "bar"}
        resolved = resolve_action_params("not_a_real_action", params, {})
        assert resolved == params

    def test_empty_params_passes_through(self):
        assert resolve_action_params("place_order", {}, {}) == {}

    def test_none_params_passes_through(self):
        assert resolve_action_params("place_order", None, {}) is None


class TestExpressionResolution:
    def test_qty_expression_resolves_against_siblings(self):
        params = {
            "account": "ZG0790",
            "symbol": "NIFTY25JULFUT",
            "side": "SELL",
            "qty": "(lots * 2) + buffer",
            "lots": 5,
            "buffer": 3,
        }
        resolved = resolve_action_params("place_order", params, {})
        assert resolved["qty"] == 13
        assert type(resolved["qty"]) is int

    def test_price_expression_resolves_to_float(self):
        params = {
            "account": "ZG0790",
            "symbol": "NIFTY25JULFUT",
            "side": "BUY",
            "qty": 1,
            "price": "entry_price * 1.05",
            "entry_price": 100,
        }
        resolved = resolve_action_params("place_order", params, {})
        assert resolved["price"] == pytest.approx(105.0)

    def test_set_flag_boolean_expression_resolves(self):
        params = {"name": "my_flag", "value": "a and b", "a": True, "b": True}
        resolved = resolve_action_params("set_flag", params, {})
        assert resolved["value"] is True

    def test_set_flag_boolean_expression_false(self):
        params = {"name": "my_flag", "value": "a and b", "a": True, "b": False}
        resolved = resolve_action_params("set_flag", params, {})
        assert resolved["value"] is False


class TestFailClosedCoercion:
    def test_bool_result_into_numeric_field_rejected(self):
        """qty: 'a==b' must never silently become qty=1 (bool is an int
        subclass in Python)."""
        params = {"account": "A", "symbol": "X", "side": "BUY", "qty": "a==b", "a": 1, "b": 1}
        with pytest.raises(ExprError):
            resolve_action_params("place_order", params, {})

    def test_non_integer_float_into_qty_rejected(self):
        params = {"account": "A", "symbol": "X", "side": "BUY", "qty": "a/b", "a": 5, "b": 2}
        with pytest.raises(ExprError):
            resolve_action_params("place_order", params, {})

    def test_integer_valued_float_into_qty_accepted_as_int(self):
        """2.5-free case: a/b = 10/2 = 5.0 — whole number, accepted and
        coerced to a real int (never left as a stray float on a qty
        field)."""
        params = {"account": "A", "symbol": "X", "side": "BUY", "qty": "a/b", "a": 10, "b": 2}
        resolved = resolve_action_params("place_order", params, {})
        assert resolved["qty"] == 5
        assert type(resolved["qty"]) is int

    def test_non_integer_float_into_new_qty_rejected(self):
        params = {"account": "A", "broker_order_id": "1", "new_qty": "a/b", "a": 5, "b": 2}
        with pytest.raises(ExprError):
            resolve_action_params("modify_order", params, {})

    def test_non_integer_price_is_fine_not_integer_only(self):
        """price is NOT in the integer-only set — a non-whole float is
        perfectly valid for a price field."""
        params = {"account": "A", "symbol": "X", "side": "BUY", "qty": 1, "price": "a/b", "a": 5, "b": 2}
        resolved = resolve_action_params("place_order", params, {})
        assert resolved["price"] == 2.5

    def test_division_by_zero_propagates_as_expr_error(self):
        params = {"account": "A", "symbol": "X", "side": "BUY", "qty": "a/b", "a": 5, "b": 0}
        with pytest.raises(ExprError, match="division by zero"):
            resolve_action_params("place_order", params, {})

    def test_undefined_sibling_name_propagates_as_expr_error(self):
        """No fallback to zero — an undefined sibling name must raise,
        never silently default."""
        params = {"account": "A", "symbol": "X", "side": "BUY", "qty": "lots * 2"}
        with pytest.raises(ExprError, match="undefined name"):
            resolve_action_params("place_order", params, {})

    def test_boolean_field_requires_strict_bool_result(self):
        """set_flag.value must be exactly bool — a numeric expression
        result is rejected even though 'number' and 'boolean' are the
        only two eligible types."""
        params = {"name": "f", "value": "a + b", "a": 1, "b": 1}
        with pytest.raises(ExprError):
            resolve_action_params("set_flag", params, {})


class TestActivationRuleBoundaries:
    def test_string_field_never_evaluated_even_if_expression_like(self):
        """`symbol` has no token_ref_ok and is type string — a value
        that merely LOOKS like an expression must pass through as a
        literal string, untouched."""
        params = {"account": "A", "symbol": "a+b", "side": "BUY", "qty": 1}
        resolved = resolve_action_params("place_order", params, {})
        assert resolved["symbol"] == "a+b"

    def test_native_numeric_value_skips_evaluation_entirely(self):
        """Already-native JSON number — must be left completely
        untouched, not re-evaluated as if it were a string."""
        params = {"account": "A", "symbol": "X", "side": "BUY", "qty": 5}
        resolved = resolve_action_params("place_order", params, {})
        assert resolved["qty"] == 5
        assert type(resolved["qty"]) is int

    def test_native_bool_value_skips_evaluation_entirely(self):
        params = {"name": "f", "value": True}
        resolved = resolve_action_params("set_flag", params, {})
        assert resolved["value"] is True
