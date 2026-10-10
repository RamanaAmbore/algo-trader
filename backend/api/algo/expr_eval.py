"""
expr_eval.py — whitelist AST evaluator for agent action-parameter
expressions.

Wired into `backend/api/algo/actions.py`'s `resolve_action_params()` so an
action param like `qty: "(lots * 2) + buffer"` can be resolved against the
action's own sibling literal params before the action is dispatched to a
sim / paper / live broker handler. THIS CODE SITS DIRECTLY UPSTREAM OF
REAL ORDER PLACEMENT — every design decision here is deliberately
fail-closed, not fail-open.

`eval_expr(expr, namespace)` supports arithmetic (`+ - * / % **`),
relational (`< <= > >= == !=`), logical (`and or not`), grouping
parentheses, and (since the string-support extension below) `str`
constants + `str + str` concatenation, over a closed, caller-supplied
namespace. There is no `ast.Call` support of ANY kind, no dotted-path
import, no attribute or subscript access — nothing in this module's
design could ever be extended to call anything. If a construct isn't
explicitly whitelisted below, it is rejected.

String support is intentionally narrow: a `str` operand is ONLY ever
valid as a `Constant`, a `Name` lookup, or the `+` (concatenation)
`BinOp` operand, and ONLY when BOTH sides of `+` are strings — `str +
number` is a type error, never silently coerced via `str()`. Every
other `BinOp` operator (`- * / % **`) stays strictly numeric-only: a
string operand there is rejected outright, BEFORE the operator is ever
invoked — not caught after the fact — because leaning on a catch-all
would let `'a' * 999999999` through as a memory bomb (`Mult` has no
magnitude guard the way `Pow` does) or `'%s' % x` through as printf-style
formatting.

This is a SEPARATE, physically distinct trust boundary from
`backend/api/algo/grammar_registry.py`'s parameterized-token-call resolver
(which allows exactly one pre-vetted `ast.Call` shape — a bare `Name`
already present in the code-reviewed, `is_system=True` grammar catalog,
with literal-only args). The two modules share no code and must never be
merged — a future change that loosens one must never accidentally loosen
the other.
"""

from __future__ import annotations

import ast
import math
from typing import Any

# ── Limits ────────────────────────────────────────────────────────────────

_MAX_EXPR_LEN = 200
_MAX_DEPTH = 20
_MAX_POW_EXPONENT = 12
_MAX_POW_BASE = 1e6
# String-support extension: caps BOTH a literal string constant in the
# expression text AND the length of any string result (including one
# produced by `+` concatenation of two namespace-supplied strings, which
# wouldn't otherwise be bounded by _MAX_EXPR_LEN since namespace values
# never pass through the parser at all). Deliberately set BELOW
# _MAX_EXPR_LEN (not equal to it) — a literal string constant's quoted
# source form is always at least 2 chars longer than its own content, so
# if this cap were == _MAX_EXPR_LEN, the overall expression-length guard
# would always fire first and this constant-specific guard could never
# actually be exercised via a literal.
_MAX_STRING_LEN = 150

_ALLOWED_RESULT_TYPES = (int, float, bool, str)


class ExprError(ValueError):
    """Raised on ANY failure to parse or evaluate an expression.
    `eval_expr` never returns `None` and never raises anything else —
    callers only ever need to catch this one exception type."""


# ── Operator tables ──────────────────────────────────────────────────────

_BINOPS: dict[type, Any] = {
    ast.Add:  lambda a, b: a + b,
    ast.Sub:  lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div:  lambda a, b: a / b,
    ast.Mod:  lambda a, b: a % b,
    ast.Pow:  lambda a, b: a ** b,
}

_UNARYOPS: dict[type, Any] = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
    ast.Not:  lambda a: not a,
}

# Relational operators mirror backend/api/algo/grammar.py:OPERATORS' symbol
# mapping (< <= > >= == !=) — same comparators, same symbols — but are
# hand-rolled here rather than imported, and deliberately DIVERGE on
# None-handling. `grammar.py:OPERATORS` fails OPEN on a None operand
# (`a is not None and a < b` → just evaluates to False — "this condition
# leaf didn't fire this tick", which is the right behaviour for a
# metric/threshold leaf that may legitimately have no data yet). An
# action-param expression, by contrast, is resolving a value that is about
# to size or price a REAL ORDER about to be PLACED on a real broker —
# treating a missing operand as "the comparison is just false, carry on"
# could silently let a wrong-but-not-erroring qty/price through instead of
# refusing the action outright. So here, either operand being `None`
# raises `ExprError` instead of evaluating to `False`. Because the
# semantics genuinely differ (fail-open vs. fail-closed), this table is
# NOT imported from or merged with `grammar.py:OPERATORS` despite the
# symbol overlap — a shared import would couple two call sites that must
# be free to diverge.
_COMPARES: dict[type, Any] = {
    ast.Lt:    lambda a, b: a < b,
    ast.LtE:   lambda a, b: a <= b,
    ast.Gt:    lambda a, b: a > b,
    ast.GtE:   lambda a, b: a >= b,
    ast.Eq:    lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}


def _check_finite(result: Any) -> None:
    if isinstance(result, float) and not math.isfinite(result):
        raise ExprError("expression produced a non-finite result")


def _check_paren_depth(expr: str) -> None:
    """Raw-text parenthesis-nesting guard — supplements the AST-node depth
    guard in `_eval_node`.

    Plain parentheses wrapping a single literal/name produce ZERO extra
    AST nodes: `ast.parse("(((1)))", mode="eval")` collapses straight down
    to a bare `Constant(1)` with no nesting at all. An AST-depth-only
    check therefore cannot catch pathological paren nesting by itself
    (e.g. `"(" * 10000 + "1" + ")" * 10000`) — only real nested operators
    (`1+1+1+...` or `-----1`) build actual nested AST nodes. This is a
    cheap O(len(expr)) linear character scan — no parsing, no recursion —
    run BEFORE `ast.parse` so a pathological input never reaches the
    parser at all.
    """
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
            if depth > _MAX_DEPTH:
                raise ExprError(f"expression nesting too deep (> {_MAX_DEPTH})")
        elif ch == ")":
            depth -= 1


def _eval_constant(node: ast.Constant) -> Any:
    # Explicit type-check rejection (not catching a later error) — only
    # int/float/bool/str literals are allowed. bytes/complex/None/Ellipsis
    # constants are all rejected here. `str` is checked via `type() is str`
    # (not `isinstance`) so a `bool`/`int` can never accidentally satisfy
    # this branch — `type()` equality, not `isinstance`, is also what keeps
    # the numeric branch below from ever matching a `str`.
    if type(node.value) is str:
        if len(node.value) > _MAX_STRING_LEN:
            raise ExprError(
                f"string constant too long ({len(node.value)} chars > "
                f"{_MAX_STRING_LEN} cap)"
            )
        return node.value
    if type(node.value) not in (int, float, bool):
        raise ExprError(f"disallowed constant type: {type(node.value).__name__}")
    if isinstance(node.value, float) and not math.isfinite(node.value):
        raise ExprError("expression contains a non-finite constant")
    return node.value


def _eval_name(node: ast.Name, namespace: dict) -> Any:
    try:
        return namespace[node.id]
    except KeyError:
        raise ExprError(f"undefined name: {node.id}") from None


def _eval_unaryop(node: ast.UnaryOp, namespace: dict, depth: int) -> Any:
    op_fn = _UNARYOPS.get(type(node.op))
    if op_fn is None:
        raise ExprError(f"disallowed syntax: {type(node.op).__name__}")
    operand = _eval_node(node.operand, namespace, depth + 1)
    result = op_fn(operand)
    _check_finite(result)
    return result


def _check_pow_magnitude(left: Any, right: Any) -> None:
    # Static magnitude guard — rejected BEFORE evaluating, so a
    # 10**10**10 / 2**1000-style CPU/memory bomb never actually gets
    # computed (not caught via a timeout after the fact).
    try:
        exponent_ok = abs(right) <= _MAX_POW_EXPONENT
        base_ok = abs(left) <= _MAX_POW_BASE
    except TypeError as e:
        raise ExprError(f"invalid operand for **: {e}") from e
    if not exponent_ok or not base_ok:
        raise ExprError(
            "exponent/base too large for ** (guard: "
            f"|exponent|<={_MAX_POW_EXPONENT}, |base|<={_MAX_POW_BASE})"
        )


def _check_no_string_operand(op_type: type, left: Any, right: Any) -> None:
    """Reject a `str` operand on any BinOp operator OTHER than `Add` —
    checked BEFORE the operator is ever invoked, not caught after the
    fact. Catching after the fact isn't enough: `'a' * 999999999` is a
    memory-exhaustion bomb (`Mult` has no magnitude guard the way `Pow`
    does) and `'%s' % x` is printf-style string formatting — both would
    actually execute and produce a result before any later type-check
    could reject them."""
    if type(left) is str or type(right) is str:
        raise ExprError(
            f"disallowed string operand for {op_type.__name__}"
        )


def _eval_string_add(left: Any, right: Any) -> str:
    """`+` between two strings is concatenation — but ONLY when BOTH
    operands are already strings. `str + number` is a type error here,
    never silently coerced via `str()`."""
    if type(left) is not str or type(right) is not str:
        raise ExprError(
            "'+' requires both operands to be strings (or both numeric) "
            "— no implicit str/number coercion"
        )
    result = left + right
    if len(result) > _MAX_STRING_LEN:
        raise ExprError(
            f"concatenated string too long ({len(result)} chars > "
            f"{_MAX_STRING_LEN} cap)"
        )
    return result


def _eval_binop(node: ast.BinOp, namespace: dict, depth: int) -> Any:
    op_fn = _BINOPS.get(type(node.op))
    if op_fn is None:
        raise ExprError(f"disallowed syntax: {type(node.op).__name__}")
    left = _eval_node(node.left, namespace, depth + 1)
    right = _eval_node(node.right, namespace, depth + 1)
    if isinstance(node.op, ast.Add) and (type(left) is str or type(right) is str):
        return _eval_string_add(left, right)
    _check_no_string_operand(type(node.op), left, right)
    if isinstance(node.op, ast.Pow):
        _check_pow_magnitude(left, right)
    try:
        result = op_fn(left, right)
    except ZeroDivisionError as e:
        raise ExprError("division by zero") from e
    if type(result) not in (int, float):
        # e.g. (-8) ** 0.5 → complex. Not a modelled result type —
        # reject explicitly rather than let a complex leak out.
        raise ExprError(
            f"expression produced an unsupported result type: {type(result).__name__}"
        )
    _check_finite(result)
    return result


def _eval_boolop(node: ast.BoolOp, namespace: dict, depth: int) -> Any:
    if type(node.op) not in (ast.And, ast.Or):
        raise ExprError(f"disallowed syntax: {type(node.op).__name__}")
    # Evaluate ALL operands (no short-circuit) and coerce the FINAL
    # result via explicit all()/any() — never return a raw operand
    # value. This prevents value-leakage: Python's native `0 or 5`
    # evaluates to `5` (the operand itself); here it must become
    # `True`, a strict bool, every time.
    values = [_eval_node(v, namespace, depth + 1) for v in node.values]
    if isinstance(node.op, ast.And):
        return all(bool(v) for v in values)
    return any(bool(v) for v in values)


def _eval_compare(node: ast.Compare, namespace: dict, depth: int) -> Any:
    # Chained comparisons (a < b < c) are NOT supported — Python's ast
    # represents them as one Compare node with multiple ops/comparators;
    # reject outright rather than silently evaluating only part of the
    # chain.
    if len(node.ops) != 1 or len(node.comparators) != 1:
        raise ExprError("chained comparisons not supported")
    op_fn = _COMPARES.get(type(node.ops[0]))
    if op_fn is None:
        raise ExprError(f"disallowed syntax: {type(node.ops[0]).__name__}")
    left = _eval_node(node.left, namespace, depth + 1)
    right = _eval_node(node.comparators[0], namespace, depth + 1)
    # Deliberate fail-CLOSED divergence from grammar.py:OPERATORS — see
    # the _COMPARES table comment above for the full rationale.
    if left is None or right is None:
        raise ExprError("comparison against undefined value")
    return bool(op_fn(left, right))


# Node type -> evaluator. Checked via isinstance in _eval_node (not a
# straight dict-by-type lookup) so ast.Expression's recursive unwrap stays
# inline; every OTHER node type dispatches through this table, keeping
# _eval_node itself a flat, low-complexity dispatcher — all the real
# per-node-type branching lives in the extracted functions above.
def _eval_node(node: ast.AST, namespace: dict, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        raise ExprError(f"expression nesting too deep (> {_MAX_DEPTH})")

    if isinstance(node, ast.Expression):
        return _eval_node(node.body, namespace, depth + 1)
    if isinstance(node, ast.Constant):
        return _eval_constant(node)
    if isinstance(node, ast.Name):
        return _eval_name(node, namespace)
    if isinstance(node, ast.UnaryOp):
        return _eval_unaryop(node, namespace, depth)
    if isinstance(node, ast.BinOp):
        return _eval_binop(node, namespace, depth)
    if isinstance(node, ast.BoolOp):
        return _eval_boolop(node, namespace, depth)
    if isinstance(node, ast.Compare):
        return _eval_compare(node, namespace, depth)

    # Everything else is explicitly rejected: Call, Attribute, Subscript,
    # Lambda, ListComp/SetComp/DictComp/GeneratorExp, IfExp, NamedExpr
    # (walrus), JoinedStr/FormattedValue (f-strings), Starred,
    # Tuple/List/Dict/Set, Import/ImportFrom, and anything else not
    # explicitly handled above. No silent pass-through of an unknown node
    # type — every unmatched node type lands here.
    raise ExprError(f"disallowed syntax: {type(node).__name__}")


def eval_expr(expr: str, namespace: dict) -> int | float | bool | str:
    """
    Evaluate a whitelisted arithmetic/relational/logical expression string
    against `namespace` and return an `int`, `float`, `bool`, or `str`.

    Raises `ExprError` on ANY failure — malformed syntax, a disallowed
    construct, an undefined name, a runtime arithmetic failure, a
    disallowed string operand (anything but `+` between two strings), or
    a non-finite/non-numeric/oversized result. Never returns `None`,
    never silently degrades. This is the ONLY exception type `eval_expr`
    ever raises — callers need catch nothing else.

    Callers resolving a NUMERIC action param (qty/price/etc — see
    `actions.py:resolve_action_params()`) must still explicitly check the
    returned type themselves: a `str`-typed expression (e.g. a `tag`
    param) is a perfectly valid `eval_expr` result now, but would be
    wrong flowing into a numeric field — this function's own contract
    doesn't know which param slot called it.
    """
    if not isinstance(expr, str):
        raise ExprError(f"expression must be a string, got {type(expr).__name__}")
    if len(expr) > _MAX_EXPR_LEN:
        raise ExprError(
            f"expression too long ({len(expr)} chars > {_MAX_EXPR_LEN} cap)"
        )

    # Cheap text-level guard, before any parsing — see _check_paren_depth's
    # own docstring for why this is needed IN ADDITION TO the AST-node
    # depth guard below.
    _check_paren_depth(expr)

    try:
        tree = ast.parse(expr, mode="eval")
    except (SyntaxError, RecursionError, MemoryError, ValueError) as e:
        raise ExprError(f"could not parse expression: {e}") from e

    try:
        result = _eval_node(tree, namespace, depth=0)
    except ExprError:
        raise
    except Exception as e:  # catch-all — eval_expr must never leak any
        # exception type other than ExprError to its caller.
        raise ExprError(f"error evaluating expression: {e}") from e

    if type(result) not in _ALLOWED_RESULT_TYPES:
        raise ExprError(
            f"expression produced an unsupported result type: {type(result).__name__}"
        )
    # Centralized backstop — covers a bare `Name` lookup returning a
    # namespace-supplied string directly (no Constant, no BinOp involved
    # at all, so neither of the two length guards above would ever see
    # it). Also redundantly re-covers the Constant/concatenation cases,
    # which is fine — the guards above raise first with a more specific
    # message; this is purely defense-in-depth for the one path they
    # don't reach.
    if type(result) is str and len(result) > _MAX_STRING_LEN:
        raise ExprError(
            f"string result too long ({len(result)} chars > "
            f"{_MAX_STRING_LEN} cap)"
        )
    _check_finite(result)
    return result
