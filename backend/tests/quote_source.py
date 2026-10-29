"""Collection-time snapshot of backend/api/routes/quote.py for static SSOT tests.

Why this module exists
----------------------
Tests that assert on the *source text* of quote.py used to call
``inspect.getsource(handler_or_module)``. ``getsource`` re-reads the file
through linecache but locates the body by the ``co_firstlineno`` captured when
the module was imported. Any on-disk edit to quote.py after import (another
agent, a checkout, a pull) makes it return a different block, so the
assertions fail non-deterministically.

Here the file is read exactly once, when this module is first imported (during
collection, before any test runs). Functions and methods are then located by
NAME in the AST of that snapshot, so line drift cannot affect the result.

Test-only. The leading ``quote_`` name is not ``test_``-prefixed, so pytest
does not collect it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_QUOTE_PATH = Path(__file__).resolve().parents[1] / "api" / "routes" / "quote.py"
QUOTE_SRC: str = _QUOTE_PATH.read_text(encoding="utf-8")
_QUOTE_TREE: ast.Module = ast.parse(QUOTE_SRC)


def quote_module_source() -> str:
    """Full text of quote.py as captured at collection time."""
    return QUOTE_SRC


def quote_method_source(class_name: str, method_name: str) -> str:
    """Source text of ``class_name.method_name`` (async or sync) in quote.py."""
    for node in _QUOTE_TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.AsyncFunctionDef, ast.FunctionDef)) and item.name == method_name:
                    segment = ast.get_source_segment(QUOTE_SRC, item)
                    assert segment, f"could not extract source for {class_name}.{method_name}"
                    return segment
    raise AssertionError(f"{class_name}.{method_name} not found in quote.py")


def quote_function_source(function_name: str) -> str:
    """Source text of a module-level (async or sync) function in quote.py."""
    for node in _QUOTE_TREE.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == function_name:
            segment = ast.get_source_segment(QUOTE_SRC, node)
            assert segment, f"could not extract source for {function_name}"
            return segment
    raise AssertionError(f"module-level function {function_name} not found in quote.py")
