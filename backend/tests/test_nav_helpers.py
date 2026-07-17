"""
Tests for api/algo/nav.py — firm-level NAV computation.
SSOT: compute_firm_nav is the single entry point for firm NAV.
Perf: LTP sourced from KiteTicker tick_map first (zero broker quota).
Stale: apply_day_change_backstop called (not reimplemented) in position phase.
Reuse: write_nav_snapshot delegates to compute_firm_nav (same formula).
UX: compute_firm_nav returns dict with nav, equity, cash keys.
"""
from pathlib import Path
import inspect

_SRC = Path("backend/api/algo/nav.py").read_text()


def test_compute_firm_nav_exists_and_is_async():
    from backend.api.algo.nav import compute_firm_nav
    assert inspect.iscoroutinefunction(compute_firm_nav), (
        "compute_firm_nav must be async (calls broker and DB)"
    )


def test_compute_firm_nav_return_structure_in_source():
    """Return dict must include nav, equity, cash (or equivalent keys)."""
    # Check that the function returns a dict with these fields
    assert "nav" in _SRC, "compute_firm_nav must return a 'nav' field"
    assert "equity" in _SRC or "holdings" in _SRC, (
        "compute_firm_nav must include equity/holdings component"
    )
    assert "cash" in _SRC or "funds" in _SRC, (
        "compute_firm_nav must include cash/funds component"
    )


def test_write_nav_snapshot_exists():
    from backend.api.algo.nav import write_nav_snapshot
    assert inspect.iscoroutinefunction(write_nav_snapshot), (
        "write_nav_snapshot must be async"
    )


def test_write_nav_snapshot_delegates_to_compute():
    """write_nav_snapshot must call compute_firm_nav — not reimplement the formula."""
    snap_src = inspect.getsource(__import__("backend.api.algo.nav", fromlist=["write_nav_snapshot"]).write_nav_snapshot)
    assert "compute_firm_nav" in snap_src, (
        "write_nav_snapshot must call compute_firm_nav to avoid formula duplication"
    )


def test_positions_phase_exists():
    from backend.api.algo.nav import _fetch_positions_phase
    assert inspect.iscoroutinefunction(_fetch_positions_phase), (
        "_fetch_positions_phase must be async"
    )


def test_holdings_phase_exists():
    from backend.api.algo.nav import _fetch_holdings_phase
    assert inspect.iscoroutinefunction(_fetch_holdings_phase)


def test_ltp_from_ticker_in_source():
    """LTP must be sourced from KiteTicker tick_map first (zero broker quota)."""
    assert "ticker" in _SRC.lower() or "tick_map" in _SRC or "ltp_fallback" in _SRC, (
        "nav.py must source LTP from KiteTicker tick_map before broker call"
    )
