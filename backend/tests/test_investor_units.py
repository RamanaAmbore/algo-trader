"""
Tests for api/algo/investor_units.py — units-based NAV math.
SSOT: units_held, cost_basis, slice_value are the pure math primitives.
Perf: compute_slice_history uses O(D+E) running pointer (not O(D×E)).
Stale: auto-bootstrap is idempotent (safe to call multiple times).
Reuse: same slice_value used by portal, statement, and history routes.
UX: slice returns 0.0 when total_units=0 (empty fund — no division by zero).
"""
from datetime import date
from unittest.mock import MagicMock

import pytest


def _event(event_type, amount, units_delta, event_date=None):
    e = MagicMock()
    e.event_type = event_type
    e.amount = amount
    e.units_delta = units_delta
    e.event_date = event_date or date(2026, 1, 1)
    return e


def test_units_held_sums_units_delta():
    from backend.api.algo.investor_units import units_held
    events = [
        _event("bootstrap", 100000, 10.0, date(2026, 1, 1)),
        _event("subscription", 50000, 4.0, date(2026, 3, 1)),
    ]
    total = units_held(events)
    assert total == pytest.approx(14.0, rel=1e-6), f"units_held must sum all delta; got {total}"


def test_units_held_as_of_filter():
    from backend.api.algo.investor_units import units_held
    events = [
        _event("bootstrap", 100000, 10.0, date(2026, 1, 1)),
        _event("subscription", 50000, 4.0, date(2026, 6, 1)),
    ]
    # as_of before the second event
    total = units_held(events, as_of=date(2026, 3, 1))
    assert total == pytest.approx(10.0, rel=1e-6), (
        f"units_held as_of must exclude future events; got {total}"
    )


def test_cost_basis_subscription_adds():
    from backend.api.algo.investor_units import cost_basis
    events = [
        _event("bootstrap", 100000, 10.0, date(2026, 1, 1)),
        _event("subscription", 50000, 4.0, date(2026, 3, 1)),
    ]
    basis = cost_basis(events)
    assert basis == pytest.approx(150000.0, rel=1e-6), (
        f"cost_basis must sum subscriptions + bootstrap; got {basis}"
    )


def test_cost_basis_redemption_subtracts():
    from backend.api.algo.investor_units import cost_basis
    events = [
        _event("bootstrap", 100000, 10.0, date(2026, 1, 1)),
        _event("redemption", 30000, -3.0, date(2026, 4, 1)),
    ]
    basis = cost_basis(events)
    assert basis == pytest.approx(70000.0, rel=1e-6), (
        f"Redemption must subtract from cost_basis; got {basis}"
    )


def test_slice_value_zero_when_no_units():
    from backend.api.algo.investor_units import slice_value
    val, npu = slice_value([], [], firm_nav=1000000.0)
    assert val == pytest.approx(0.0), "slice_value must return 0.0 when total_units=0"
    assert npu == pytest.approx(0.0), "nav_per_unit must return 0.0 when total_units=0"


def test_slice_value_proportional():
    from backend.api.algo.investor_units import slice_value
    all_ev = [
        _event("bootstrap", 100000, 10.0, date(2026, 1, 1)),  # LP A
        _event("bootstrap", 50000, 5.0, date(2026, 1, 1)),   # LP B
    ]
    user_ev = [all_ev[0]]  # LP A holds 10 out of 15 total units
    firm_nav = 300000.0
    val, npu = slice_value(user_ev, all_ev, firm_nav)
    expected_npu = 300000.0 / 15.0  # = 20000
    assert npu == pytest.approx(expected_npu, rel=1e-6), f"nav_per_unit wrong: {npu}"
    assert val == pytest.approx(10.0 * expected_npu, rel=1e-6), f"slice_value wrong: {val}"
