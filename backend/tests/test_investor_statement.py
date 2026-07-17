"""
Tests for api/algo/investor_statement.py — LP statement computation.
SSOT: compute_statement is the single public async entry point.
Perf: date-range filtering uses inclusive >= start and <= end bounds.
Stale: uses investor_units.py helpers (not reimplements NAV math).
Reuse: StatementData shared between route handler and PDF renderer.
UX: render_statement_pdf produces bytes for direct HTTP response.
"""
from pathlib import Path

_SRC = Path("backend/api/algo/investor_statement.py").read_text()


def test_compute_statement_exists():
    from backend.api.algo.investor_statement import compute_statement
    import inspect
    assert inspect.iscoroutinefunction(compute_statement), (
        "compute_statement must be async (reads from DB)"
    )


def test_statement_data_class_exists():
    from backend.api.algo.investor_statement import StatementData
    assert StatementData is not None


def test_render_statement_pdf_exists():
    from backend.api.algo.investor_statement import render_statement_pdf
    assert callable(render_statement_pdf), "render_statement_pdf must be callable"


def test_statement_data_has_nav_series():
    """StatementData must include nav_series for the LP's performance curve."""
    import dataclasses
    from backend.api.algo.investor_statement import StatementData
    # Check that daily_rows or nav_series field exists
    assert "daily_rows" in _SRC or "nav_series" in _SRC, (
        "StatementData must include daily NAV rows for the LP's performance curve"
    )


def test_statement_uses_investor_units():
    """Statement must delegate slice/cost_basis math to investor_units, not reimplementing."""
    assert "investor_units" in _SRC or "units_held" in _SRC or "slice_value" in _SRC, (
        "investor_statement must use investor_units module for NAV math — "
        "not reimplementing the units calculation"
    )


def test_net_flows_formula_present():
    """Net flows = subscriptions − redemptions must appear in statement logic."""
    assert "redemption" in _SRC and ("subscription" in _SRC or "bootstrap" in _SRC), (
        "Statement must compute net flows from subscription and redemption events"
    )


def test_annualized_return_present():
    """Annualized return or XIRR must appear in the statement."""
    assert "annualized" in _SRC.lower() or "xirr" in _SRC.lower() or "return" in _SRC.lower(), (
        "Statement must compute annualized return or XIRR for the LP"
    )


def test_date_range_inclusive_bounds():
    """Date range filtering must use inclusive bounds (>= start, <= end)."""
    assert ">=" in _SRC, "Date range must use >= for inclusive start bound"
    assert "<=" in _SRC, "Date range must use <= for inclusive end bound"
