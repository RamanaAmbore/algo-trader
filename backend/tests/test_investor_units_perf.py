"""Regression tests for the slice-M perf rewrites in
`backend.api.algo.investor_units`.

The math primitives didn't change; the running-pointer rewrite of
`compute_slice_history` (O(D × E) → O(D + E)) is the kind of edit
that's easy to break silently. These tests lock in the expected
output against a hand-built fixture.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from backend.api.algo.investor_units import compute_slice_history


def _ev(user_id: int, ed: date, units_delta: float, amount: float,
        event_type: str = "subscription"):
    """Build a minimal InvestorEvent-shaped object — same attrs the
    running-pointer code reads."""
    return SimpleNamespace(
        user_id=user_id,
        event_date=ed,
        units_delta=units_delta,
        amount=amount,
        event_type=event_type,
    )


def _nd(d: date, nav: float):
    return SimpleNamespace(as_of_date=d, nav=nav)


def test_compute_slice_history_basic_two_lps():
    """Two LPs, one bootstrap each on day-0, NAV grows 10% by day-2."""
    user_a = _ev(1, date(2026, 1, 1), units_delta=60.0,
                 amount=60_000.0, event_type="bootstrap")
    user_b = _ev(2, date(2026, 1, 1), units_delta=40.0,
                 amount=40_000.0, event_type="bootstrap")
    all_events = [user_a, user_b]
    # Compute LP-A's slice across three days.
    curve = [
        _nd(date(2026, 1, 1), 100_000.0),
        _nd(date(2026, 1, 2), 105_000.0),
        _nd(date(2026, 1, 3), 110_000.0),
    ]
    out = compute_slice_history([user_a], all_events, curve)
    assert len(out) == 3
    # Day 0: A holds 60 units out of 100, firm NAV 100k → 60k.
    assert out[0]["nav_share"] == 60_000.0
    assert out[0]["cost_basis"] == 60_000.0
    assert out[0]["pnl"] == 0.0
    # Day 2: firm NAV 110k → A's slice 66k (60/100 × 110k).
    assert out[2]["nav_share"] == 66_000.0
    assert out[2]["cost_basis"] == 60_000.0
    assert out[2]["pnl"] == 6_000.0


def test_compute_slice_history_mid_period_subscription():
    """LP-A bootstraps day-0; LP-B joins day-2 at the higher NAV/unit.
    The day-1 row should reflect ONLY A's units; day-2 onward both."""
    a_boot = _ev(1, date(2026, 1, 1), 100.0, 100_000.0, "bootstrap")
    # On day-2 firm_nav=110k with A's 100 units → npu = 1100. B buys
    # in for 22k at npu=1100 → 20 units.
    b_sub  = _ev(2, date(2026, 1, 2), 20.0, 22_000.0, "subscription")
    all_events = [a_boot, b_sub]
    curve = [
        _nd(date(2026, 1, 1), 100_000.0),
        _nd(date(2026, 1, 2), 132_000.0),  # 110k + 22k subscription
        _nd(date(2026, 1, 3), 144_000.0),
    ]
    out_a = compute_slice_history([a_boot], all_events, curve)
    out_b = compute_slice_history([b_sub],  all_events, curve)
    # Day 0: only A → 100% of firm_nav.
    assert out_a[0]["nav_share"] == 100_000.0
    assert out_b[0]["nav_share"] == 0.0
    # Day 2 + : A has 100 / 120 units = 83.33%; B has 20 / 120 = 16.66%.
    # Firm NAV day 2 = 132k → A=110k, B=22k.
    assert abs(out_a[1]["nav_share"] - 110_000.0) < 0.5
    assert abs(out_b[1]["nav_share"] -  22_000.0) < 0.5
    # Cost basis: A still 100k (no contribution since); B = 22k.
    assert out_a[1]["cost_basis"] == 100_000.0
    assert out_b[1]["cost_basis"] ==  22_000.0
    # B's day-2 pnl is 0 (just bought in).
    assert out_b[1]["pnl"] == 0.0


def test_compute_slice_history_redemption_subtracts_basis():
    """Redemption events subtract from cost_basis."""
    boot   = _ev(1, date(2026, 1, 1), 100.0, 100_000.0, "bootstrap")
    redeem = _ev(1, date(2026, 1, 2), -20.0, 20_000.0, "redemption")
    all_events = [boot, redeem]
    curve = [
        _nd(date(2026, 1, 1), 100_000.0),
        _nd(date(2026, 1, 2),  80_000.0),  # 100k − 20k payout
    ]
    out = compute_slice_history([boot, redeem], all_events, curve)
    # Day 1: cost basis dropped to 80k after the 20k payout.
    assert out[1]["cost_basis"] == 80_000.0
    # 80 units × (80k / 80 units) = 80k.
    assert out[1]["nav_share"] == 80_000.0


def test_compute_slice_history_unsorted_input_is_handled():
    """The caller may pass events / curve in any order; the function
    sorts internally."""
    boot = _ev(1, date(2026, 1, 1), 100.0, 100_000.0, "bootstrap")
    sub  = _ev(1, date(2026, 1, 3),  50.0,  60_000.0)
    # Unsorted on purpose.
    all_events = [sub, boot]
    curve = [
        _nd(date(2026, 1, 4), 160_000.0),
        _nd(date(2026, 1, 1), 100_000.0),
        _nd(date(2026, 1, 2), 100_000.0),
    ]
    out = compute_slice_history([boot, sub], all_events, curve)
    # Output is sorted ASC, so out[0] is day-1.
    assert out[0]["as_of_date"] == "2026-01-01"
    assert out[0]["nav_share"]  == 100_000.0
    assert out[1]["as_of_date"] == "2026-01-02"
    # Day 3 onward: 150 units total, firm_nav 160k.
    assert out[2]["as_of_date"] == "2026-01-04"
    # Each unit worth 160k/150 ≈ 1066.67; 150 × 1066.67 ≈ 160k.
    assert abs(out[2]["nav_share"] - 160_000.0) < 0.5


def test_compute_slice_history_empty_fund_returns_zero_npu():
    """No events, no units → npu = 0 (guards against div-by-zero)."""
    curve = [_nd(date(2026, 1, 1), 100_000.0)]
    out = compute_slice_history([], [], curve)
    assert out[0]["nav_share"] == 0.0
    assert out[0]["nav_per_unit"] == 0.0
