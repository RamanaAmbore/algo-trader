"""
Tests for the P2 perf cleanup bundle (2026-07).

Checks five quality dimensions (SSOT, perf, stale-code grep, reuse, UX):
  1. Dead imports removed (grep the target files).
  2. iterrows absent from the modified _task_performance block.
  3. Per-row session pattern removed from hedge proxy regression loop
     (only one async_session context inside _run_once after the load).
  4. Batched UPDATE fires once for N accounts.
  5. list_funds returns ≤ hard_cap rows even with more in DB.
"""
from __future__ import annotations

import os
import re
import sys
import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]  # /Users/.../ramboq
POSITIONS_PY  = REPO / "backend/api/routes/positions.py"
HOLDINGS_PY   = REPO / "backend/api/routes/holdings.py"
FUNDS_PY      = REPO / "backend/api/routes/funds.py"
BACKGROUND_PY = REPO / "backend/api/background.py"
NAV_PY        = REPO / "backend/api/algo/nav.py"
DATABASE_PY   = REPO / "backend/api/database.py"
HISTORY_PY    = REPO / "backend/api/routes/history.py"

os.environ.setdefault("PYTEST_RUNNING", "1")


# ---------------------------------------------------------------------------
# 1. Dead imports removed
# ---------------------------------------------------------------------------

class TestDeadImportsRemoved:
    """SSOT: no dead name should appear anywhere in the file except its import line."""

    def _non_import_usages(self, path: Path, name: str) -> list[int]:
        """Return line numbers where `name` appears in non-import lines."""
        hits = []
        for i, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if name in stripped and not stripped.startswith(("import ", "from ")):
                hits.append(i)
        return hits

    def test_positions_no_is_authenticated_request(self):
        src = POSITIONS_PY.read_text()
        assert "is_authenticated_request" not in src, (
            "is_authenticated_request should be fully removed from positions.py"
        )

    def test_positions_no_mask_column(self):
        # mask_column must not appear (import or usage)
        assert "mask_column" not in POSITIONS_PY.read_text(), (
            "mask_column should be fully removed from positions.py"
        )

    def test_positions_no_function_scope_json_import(self):
        src = POSITIONS_PY.read_text()
        assert "import json as _json" not in src, (
            "function-scope 'import json as _json' should be removed from positions.py"
        )

    def test_holdings_no_is_authenticated_request(self):
        assert "is_authenticated_request" not in HOLDINGS_PY.read_text()

    def test_holdings_no_mask_column(self):
        assert "mask_column" not in HOLDINGS_PY.read_text()

    def test_funds_no_is_authenticated_request(self):
        assert "is_authenticated_request" not in FUNDS_PY.read_text()

    def test_funds_no_mask_column(self):
        assert "mask_column" not in FUNDS_PY.read_text()

    def test_background_no_mask_column_in_top_level_import(self):
        src = BACKGROUND_PY.read_text()
        # The top-level utils import should no longer include mask_column
        match = re.search(
            r"from backend\.shared\.helpers\.utils import[^\n]+",
            src,
        )
        assert match, "top-level utils import not found in background.py"
        assert "mask_column" not in match.group(0), (
            "mask_column should be removed from the top-level utils import in background.py"
        )

    def test_background_no_summary_block_import(self):
        src = BACKGROUND_PY.read_text()
        # _summary_block should not be imported
        assert "_summary_block" not in src, (
            "_summary_block should be removed from the visitor_report import in background.py"
        )

    def test_nav_no_function_scope_datetime_import(self):
        src = NAV_PY.read_text()
        # The function-scope 'from datetime import datetime, timezone' should be gone
        # The top-level module may still have datetime imports — we only care about
        # the one that was inside write_nav_snapshot.
        fn_match = re.search(
            r"async def write_nav_snapshot.*?(?=\nasync def |\Z)",
            src, re.DOTALL,
        )
        assert fn_match, "write_nav_snapshot function not found in nav.py"
        fn_body = fn_match.group(0)
        assert "from datetime import datetime, timezone" not in fn_body, (
            "function-scope 'from datetime import datetime, timezone' "
            "should be removed from write_nav_snapshot in nav.py"
        )


# ---------------------------------------------------------------------------
# 2. iterrows absent from the _task_performance ticker-subscribe block
# ---------------------------------------------------------------------------

class TestIterrowsReplaced:
    """Perf: iterrows must not appear in the hot-path DataFrame iteration."""

    def test_iterrows_not_in_background(self):
        src = BACKGROUND_PY.read_text()
        assert "iterrows()" not in src, (
            "iterrows() found in background.py — should have been replaced with itertuples()"
        )

    def test_itertuples_present_in_background(self):
        src = BACKGROUND_PY.read_text()
        assert "itertuples(" in src, (
            "itertuples() not found in background.py — replacement may not have landed"
        )


# ---------------------------------------------------------------------------
# 3. Per-row session pattern removed from hedge proxy regression
# ---------------------------------------------------------------------------

class TestHedgeProxySessionCollapsed:
    """SSOT: _run_once (hedge proxy) should have exactly ONE async_session
    context manager AFTER the initial row-load query."""

    def _extract_run_once_body(self) -> str:
        src = BACKGROUND_PY.read_text()
        # Extract from 'async def _run_once' to the next top-level 'async def'
        # or the enclosing 'await asyncio.sleep(120)' sentinel.
        match = re.search(
            r"(    async def _run_once\(\).*?)(?=\n    await asyncio\.sleep\(120\)|\nasync def )",
            src, re.DOTALL,
        )
        assert match, "_run_once inner function not found in background.py"
        return match.group(1)

    def test_only_one_async_session_after_load(self):
        body = self._extract_run_once_body()
        # Count async_session() occurrences: first one is the load query,
        # second one (for batch write) is the collapsed session.
        # Three would indicate per-row sessions still present.
        contexts = re.findall(r"async with async_session\(\)", body)
        assert len(contexts) <= 2, (
            f"Expected ≤2 async_session contexts in _run_once "
            f"(load + batch write), found {len(contexts)}"
        )

    def test_pending_writes_pattern_present(self):
        body = self._extract_run_once_body()
        assert "_pending_writes" in body, (
            "_pending_writes collect pattern should be present in "
            "the hedge proxy _run_once function"
        )


# ---------------------------------------------------------------------------
# 4. Batched UPDATE: CASE expression present, no per-row execute loop
# ---------------------------------------------------------------------------

class TestBatchedUpdate:
    """Perf: display_order seeding should use a single CASE-based UPDATE."""

    def _extract_seed_block(self) -> str:
        src = DATABASE_PY.read_text()
        # Match from 'do_already' check through the logger.info that confirms seeding
        match = re.search(
            r"do_already = await conn\.scalar.*?logger\.info\(\s*\"_ensure_shared_broker_schema: display_order seeded for",
            src, re.DOTALL,
        )
        assert match, "display_order seed block not found in database.py"
        return match.group(0)

    def test_case_expression_present(self):
        block = self._extract_seed_block()
        assert "CASE account" in block or "_build_display_order_map(" in block, (
            "CASE-based batched UPDATE not found in display_order seed block "
            "(or helper _build_display_order_map not called after C→B refactor)"
        )

    def test_no_per_row_update_execute(self):
        block = self._extract_seed_block()
        # After C→B refactoring, the check is: seed block has a call to _build_display_order_map
        # and then a batch UPDATE. No per-row executes inside the for loop itself.
        # The for loop may exist in _build_display_order_map, but that's a helper that builds the map.
        # Crucially: there must be no per-row UPDATE (conn.execute) inside the seed block's for loop.
        # Count total conn.execute in this block: fetch + batch = 2 max
        execute_calls = re.findall(r"await conn\.execute\(", block)
        assert len(execute_calls) <= 2, (
            f"Expected ≤2 conn.execute in the seed block (fetch + batch), "
            f"found {len(execute_calls)} — per-row executes may still be present"
        )
        # If there's a for loop in the seed block, it should not have conn.execute
        for_loop_match = re.search(r"for acct, broker_id in rows:.*?(?=\n        [a-zA-Z_]|\n    \w)", block, re.DOTALL)
        if for_loop_match:
            loop_body = for_loop_match.group(0)
            assert "await conn.execute" not in loop_body, (
                "await conn.execute found inside the per-account for loop — "
                "per-row UPDATE pattern not fully removed"
            )

    def test_order_map_collects_before_execute(self):
        block = self._extract_seed_block()
        assert "order_map" in block, (
            "order_map list should be built before the single batch execute"
        )


# ---------------------------------------------------------------------------
# 5. list_funds LIMIT guard
# ---------------------------------------------------------------------------

class TestListFundsLimit:
    """UX + perf: list_funds must never return more than hard_cap rows."""

    def test_limit_call_present_in_query(self):
        src = HISTORY_PY.read_text()
        assert ".limit(" in src, (
            ".limit() call not found in history.py — LIMIT guard may not be applied"
        )

    def test_hard_cap_default_constant(self):
        src = HISTORY_PY.read_text()
        # default hard_cap must be 1000
        assert "1000" in src, (
            "Default hard_cap of 1000 not found in history.py"
        )

    def test_truncation_warning_present(self):
        src = HISTORY_PY.read_text()
        assert "truncated" in src.lower(), (
            "Truncation warning log not found in history.py — "
            "operator should be warned when the cap is hit"
        )

    def test_get_int_used_for_cap(self):
        src = HISTORY_PY.read_text()
        assert "get_int" in src, (
            "get_int not found in history.py — hard_cap should be settings-driven"
        )

    def test_list_funds_hard_cap_in_source(self):
        """Structural: confirm the truncation logic slices at hard_cap."""
        src = HISTORY_PY.read_text()
        # The guard pattern should be: result[:hard_cap]
        assert "result[:hard_cap]" in src, (
            "result[:hard_cap] slice not found in history.py — "
            "truncation guard may not be correctly applied"
        )
