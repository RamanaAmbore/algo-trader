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
relational (`< <= > >= == !=`), logical (`and or not`), and grouping
parentheses over a closed, caller-supplied namespace. There is no
`ast.Call` support of ANY kind, no dotted-path import, no attribute or
subscript access — nothing in this module's design could ever be extended
to call anything. If a construct isn't explicitly whitelisted below, it is
rejected.

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

_ALLOWED_RESULT_TYPES = (int, float, bool)


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
    # int/float/bool literals are allowed. str/bytes/complex/None/Ellipsis
    # constants are all rejected here.
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


def _eval_binop(node: ast.BinOp, namespace: dict, depth: int) -> Any:
    op_fn = _BINOPS.get(type(node.op))
    if op_fn is None:
        raise ExprError(f"disallowed syntax: {type(node.op).__name__}")
    left = _eval_node(node.left, namespace, depth + 1)
    right = _eval_node(node.right, namespace, depth + 1)
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


def eval_expr(expr: str, namespace: dict) -> int | float | bool:
    """
    Evaluate a whitelisted arithmetic/relational/logical expression string
    against `namespace` and return an `int`, `float`, or `bool`.

    Raises `ExprError` on ANY failure — malformed syntax, a disallowed
    construct, an undefined name, a runtime arithmetic failure, or a
    non-finite/non-numeric result. Never returns `None`, never silently
    degrades. This is the ONLY exception type `eval_expr` ever raises —
    callers need catch nothing else.
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
    _check_finite(result)
    return result
