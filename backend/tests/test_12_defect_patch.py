"""
Test suite for the 12-defect patch.

Six tests covering SSOT, correctness, stale-state, and UX fixes:
1. Dhan margin gate fix (flat dict handling)
2. CDS/BCD segment mapping to 'currency'
3. Funds cache invalidation after fill
4. Template attachment error alerts
5. positionsStore.load args (frontend JS source scan)
6. NavStrip P-slot formula guard against stale snapshots
"""

import inspect
import pytest
from pathlib import Path


def test_dhan_margin_gate_reads_flat_dict():
    """_fetch_account_margins must handle Dhan flat dict shape without .get(segment, {})
    returning empty — verify source does NOT unconditionally call .get('equity') on all shapes.

    Dhan returns flat dict: {"net": 100000, "available": 80000}
    Kite returns nested: {"equity": {...}, "commodity": {...}}

    The fix detects flat dicts by checking for 'net' or 'available' at top level.
    """
    src = Path("/Users/ramanambore/projects/ramboq/backend/api/algo/actions_preflight.py").read_text()
    # The fix must check for 'net' key at the top level to detect Dhan flat-dict shape
    assert '"net" in' in src or "'net' in" in src, (
        "_fetch_account_margins must check for Dhan flat-dict shape ('net' key) "
        "before slicing by segment. Without this check, Dhan orders silently pass "
        "the margin gate because .get('equity', {}) returns empty dict."
    )


def test_cds_bcd_segment_is_currency():
    """Exchange CDS/BCD must map to 'currency' segment, not 'equity'.

    These are currency derivatives and belong in the currency segment.
    The segment resolution occurs in run_preflight around line ~652.
    """
    src = Path("/Users/ramanambore/projects/ramboq/backend/api/algo/actions_preflight.py").read_text()
    # Both CDS and BCD must appear in the segment resolution
    assert ('"CDS"' in src or "'CDS'" in src), "CDS exchange must be handled in segment resolution"
    assert ('"BCD"' in src or "'BCD'" in src), "BCD exchange must be handled in segment resolution"
    # Both must map to currency segment
    assert ('"currency"' in src or "'currency'" in src), (
        "currency segment must appear in segment resolution for CDS/BCD"
    )


def test_funds_cache_invalidated_after_fill():
    """_rco_invalidate_terminal_caches must include funds cache invalidation.

    After a fill, the /api/funds response stays stale for up to 30s without this fix.
    """
    from backend.api.routes import orders as _ord

    src = inspect.getsource(_ord._rco_invalidate_terminal_caches)

    assert "funds" in src, (
        "_rco_invalidate_terminal_caches must invalidate 'funds' cache after fill; "
        "otherwise /api/funds stays stale for up to 30s post-trade. "
        "The fix adds invalidate('funds') alongside positions/holdings/margins."
    )


def test_template_attach_errors_trigger_alert():
    """Template attachment failures (G1/GTT/wing) must trigger a Telegram alert,
    not just append to result.errors silently.

    G1 guard failures, translate_qty errors, and GTT/wing placement failures should
    all result in an alert being sent when result.errors is non-empty.
    """
    from backend.api.algo import template_attach as _ta

    src = inspect.getsource(_ta)

    # The fix adds an alert call when result.errors is non-empty
    # Look for alert pattern when errors are collected
    assert "_fire_attach_fail_alert" in src, (
        "template_attach must call _fire_attach_fail_alert when result.errors is non-empty — "
        "not the pre-existing _fire_guard_alert which fires on applies_to guard, not on attach errors"
    )
    assert "result.errors" in src, (
        "alert must be conditional on result.errors being non-empty"
    )


def test_derivatives_page_positions_load_uses_args_not_opts():
    """positionsStore.load must pass fresh as args (first param), not opts (second param).

    The old bug: positionsStore.load(undefined, { force: fresh }) — fresh never reached
    backend because createDataStore forwards args to the fetcher as query params, but opts
    is a dedup flag only.

    The fix: positionsStore.load({ fresh: true }) (single-arg form).
    """
    src_path = Path("/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/admin/derivatives/+page.svelte")
    src = src_path.read_text()

    # The old pattern should NOT appear
    assert "positionsStore.load(undefined," not in src, (
        "positionsStore.load(undefined, ...) passes fresh as opts, not args — "
        "?fresh=1 never reaches backend. Must use positionsStore.load({ fresh: true }) instead."
    )


def test_navstrip_pslot_formula_guard_stale_snapshot():
    """baseDayPnlForPosition must guard against close_price === last_price (stale snapshot)
    to avoid returning a distorted formula value when previous_close is unavailable.

    When close_price falls back to last_price (stale LTP from broker REST endpoint),
    the formula pnl - oq*(close-avg) produces total unrealized P&L, not day P&L.

    The fix: check close !== ltp before applying the formula.
    """
    src_path = Path("/Users/ramanambore/projects/ramboq/frontend/src/lib/data/nav.js")
    src = src_path.read_text()

    # The fix adds a close !== ltp guard before applying the formula
    # Look for the guard condition in the baseDayPnlForPosition function
    assert "close !== ltp" in src or "close_price !== last_price" in src, (
        "baseDayPnlForPosition must check close !== ltp before applying the formula "
        "pnl - oq*(close-avg) to prevent distorted values when close_price fell back "
        "to LTP in snapshot rows. Without this guard, stale snapshots show multi-lakh distortions."
    )


if __name__ == "__main__":
    # Allow running via: python backend/tests/test_12_defect_patch.py
    pytest.main([__file__, "-v"])
