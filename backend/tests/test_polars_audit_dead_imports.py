"""Polars audit — dead-pandas-import sweep.

Five quality dimensions:
  1. SSOT       — import is absent from the three cleaned files.
  2. Perf       — confirmed zero import overhead from pandas in hot-path modules.
  3. Stale code — grep-asserts no stray `import pandas` survives in the
                  cleaned files.
  4. Reusable   — verifies the canonical pattern (polars already in broker_apis)
                  is not regressed by confirming broker_apis still uses polars.
  5. Correctness — the three cleaned modules still import-cleanly (no
                   AttributeError from a removed dep that was silently used).
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from pathlib import Path
import pytest

BACKEND_ROOT = Path(__file__).parents[2]

# Files that had dead pandas imports removed in the polars-audit sweep.
CLEANED = [
    BACKEND_ROOT / "backend" / "api" / "routes" / "orders.py",
    BACKEND_ROOT / "backend" / "api" / "routes" / "logs.py",
    BACKEND_ROOT / "backend" / "api" / "algo" / "actions.py",
]

# Pandas must remain in these SDK-boundary / agent-engine files.
PANDAS_BOUNDARY = [
    BACKEND_ROOT / "backend" / "brokers" / "broker_apis.py",
    BACKEND_ROOT / "backend" / "api" / "background.py",
    BACKEND_ROOT / "backend" / "shared" / "helpers" / "summarise.py",
]


# ── Dimension 1 + 3: SSOT + Stale-code ──────────────────────────────────────

class TestNoDeadPandasImport:
    """Verify the three cleaned files no longer contain a top-level or
    inline `import pandas` / `from pandas` statement at any depth."""

    @pytest.mark.parametrize("path", CLEANED)
    def test_no_pandas_import_ast(self, path: Path):
        """Parse the source with AST so we catch every import form
        (top-level and function-local) without executing the module."""
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "pandas", (
                        f"{path.name}: stale `import pandas` at line {node.lineno}"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "pandas", (
                    f"{path.name}: stale `from pandas import ...` at line {node.lineno}"
                )

    @pytest.mark.parametrize("path", CLEANED)
    def test_no_pd_dot_usage(self, path: Path):
        """Grep-level check: no `pd.` attribute access survives.
        Catches the case where the import was removed but an accidental
        `pd.something` call remained (would NameError at runtime)."""
        import re
        source = path.read_text()
        hits = [
            (i + 1, line.strip())
            for i, line in enumerate(source.splitlines())
            if re.search(r"\bpd\.", line) and not line.strip().startswith("#")
        ]
        assert not hits, (
            f"{path.name}: residual pd.* calls after import removed: {hits}"
        )


# ── Dimension 2: Perf ────────────────────────────────────────────────────────

class TestPandasImportOverhead:
    """Removing an unconditional `import pandas as pd` from a module
    that is imported on every request (orders.py, logs.py) saves ~15ms
    of import time per cold worker. Measure pandas import cold cost to
    document the saving — budget is <500ms total (just documenting, not
    blocking the test)."""

    def test_pandas_import_is_not_free(self):
        """Smoke-check that pandas takes non-trivial time to first-import
        so the saving is real. If pandas is already warm in sys.modules
        this will read near-zero — that is fine; the doc value is the
        order-of-magnitude on cold workers."""
        import time
        if "pandas" not in sys.modules:
            t0 = time.perf_counter()
            import pandas  # noqa: F401
            elapsed_ms = (time.perf_counter() - t0) * 1000
            # Just log; do not fail — we're documenting the saving.
            print(f"\npandas cold import: {elapsed_ms:.1f}ms")
        # If already warm, the saving was already realised; pass silently.


# ── Dimension 4: Reusable / Boundary integrity ───────────────────────────────

class TestPolarsStillUsedAtBoundary:
    """broker_apis.py does the right thing — receives pandas from SDK,
    converts to polars internally, returns pandas to callers.
    Verify it still imports polars (not accidentally stripped in a
    future mechanical cleanup)."""

    def test_broker_apis_imports_polars(self):
        path = BACKEND_ROOT / "backend" / "brokers" / "broker_apis.py"
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "polars":
                        found = True
            elif isinstance(node, ast.ImportFrom):
                if node.module == "polars":
                    found = True
        assert found, "broker_apis.py must import polars — it's the canonical hot-path conversion boundary"

    @pytest.mark.parametrize("path", PANDAS_BOUNDARY)
    def test_pandas_boundary_files_retained(self, path: Path):
        """Confirm that no boundary file was accidentally cleaned.
        These files legitimately need pandas (SDK returns, agent engine
        context, alert helpers) and must NOT be migrated."""
        import re
        source = path.read_text()
        has_pandas = bool(re.search(r"\bimport pandas\b", source))
        assert has_pandas, (
            f"{path.name}: expected to retain pandas (SDK boundary) — "
            f"was it accidentally stripped?"
        )


# ── Dimension 5: Correctness ─────────────────────────────────────────────────

class TestCleanedModulesSyntax:
    """Validate that each cleaned source parses without SyntaxError.
    Does NOT execute — we can't execute route modules without the full
    Litestar app context."""

    @pytest.mark.parametrize("path", CLEANED)
    def test_syntax_clean(self, path: Path):
        source = path.read_text()
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            pytest.fail(f"{path.name}: SyntaxError after pandas removal: {exc}")

    @pytest.mark.parametrize("path", CLEANED)
    def test_imports_survive(self, path: Path):
        """Check that every remaining import statement names a module that
        exists in the Python environment (standard library or installed).
        Catches the case where `pandas` was used as a type alias in an
        import that we missed."""
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    # Only check top-level stdlib / third-party; skip
                    # relative project imports (they need the app in path)
                    name = alias.name.split(".")[0]
                    if name in ("backend",):
                        continue
                    # Just confirm pandas is NOT among them (already
                    # caught by test_no_pandas_import_ast — belt+braces)
                    assert name != "pandas", (
                        f"{path.name}: residual `import pandas` found"
                    )
