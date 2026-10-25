"""Tests for grammar_tokens/metric_underlying_market_loss.py —
`underlying_market_loss` metric.

2026-10-22 reference-point fix: this metric sums
(day_change_val now − dcv_base) per BASELINED leg, not raw
day_change_val — see the resolver's own docstring for the full
rationale (shares the theoretical side's reference point, so a pure
gap-day move doesn't read as a false divergence).

Moved verbatim from test_underlying_loss_divergence_tokens.py's
TestMetricUnderlyingMarketLoss (Wave 1, batch 3 of the DSL
token-unification plan).
"""

import pytest

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_underlying_market_loss import (
    _metric_underlying_market_loss,
)


def _fut_baseline(S_base, qty, is_mcx=False, dcv_base=0.0):
    return {'kind': 'fut', 'ltp_base': S_base, 'S_base': S_base,
            'is_mcx': is_mcx, 'qty': qty, 'dcv_base': dcv_base}


def test_sums_baseline_relative_delta_across_legs_of_one_underlying():
    rows = [
        {'underlying': 'NIFTY', 'tradingsymbol': 'LEG_A', 'day_change_val': -50.0},
        {'underlying': 'NIFTY', 'tradingsymbol': 'LEG_B', 'day_change_val': -75.0},
        {'underlying': 'CRUDEOIL', 'tradingsymbol': 'LEG_C', 'day_change_val': 10.0},
    ]
    alert_state = {'underlying_theo_baseline': {
        'LEG_A': _fut_baseline(100.0, 1, dcv_base=-10.0),
        'LEG_B': _fut_baseline(100.0, 1, dcv_base=-25.0),
        'LEG_C': _fut_baseline(100.0, 1, dcv_base=0.0),
    }}
    ctx = Context(position_rows=rows, alert_state=alert_state)
    val = _metric_underlying_market_loss(ctx, {'underlying': 'NIFTY'})
    # (-50 - (-10)) + (-75 - (-25)) = -40 + -50 = -90
    assert val == pytest.approx(-90.0), f"expected -90.0, got {val}"


def test_missing_underlying_on_row_is_none():
    ctx = Context(position_rows=[{'underlying': 'NIFTY', 'day_change_val': -50.0}])
    assert _metric_underlying_market_loss(ctx, {}) is None


def test_leg_without_baseline_does_not_contribute():
    """A leg not yet baselined is skipped entirely (not coerced to a
    0 delta) — but a sibling leg of the SAME underlying that IS
    baselined still contributes its own delta."""
    rows = [
        {'underlying': 'NIFTY', 'tradingsymbol': 'BASELINED', 'day_change_val': -50.0},
        {'underlying': 'NIFTY', 'tradingsymbol': 'NOT_YET_BASELINED', 'day_change_val': -999999.0},
    ]
    alert_state = {'underlying_theo_baseline': {
        'BASELINED': _fut_baseline(100.0, 1, dcv_base=-10.0),
    }}
    ctx = Context(position_rows=rows, alert_state=alert_state)
    val = _metric_underlying_market_loss(ctx, {'underlying': 'NIFTY'})
    assert val == pytest.approx(-40.0), (
        f"expected -40.0 (only the BASELINED leg's delta), got {val} — "
        f"the un-baselined leg's -999999.0 must not leak in"
    )


def test_no_legs_baselined_yet_is_none():
    """Missing-vs-zero convention: if NOT A SINGLE leg of this
    underlying has a baseline yet, the metric reports None, never a
    false 0 (which would silently look like 'no loss')."""
    rows = [{'underlying': 'NIFTY', 'tradingsymbol': 'SYM', 'day_change_val': -50.0}]
    ctx = Context(position_rows=rows, alert_state={})
    assert _metric_underlying_market_loss(ctx, {'underlying': 'NIFTY'}) is None
