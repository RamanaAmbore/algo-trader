"""Tests for grammar_tokens/metric_underlying_theoretical_loss.py —
`underlying_theoretical_loss` metric.

Baseline-reprice design: implied vol is calibrated ONCE per leg per
session and frozen in `alert_state['underlying_theo_baseline']`; only
spot and time-to-expiry move tick to tick thereafter.

Moved verbatim from test_underlying_loss_divergence_tokens.py's
TestMetricUnderlyingTheoreticalLossAndDivergence (Wave 1, batch 3 of
the DSL token-unification plan).
"""

from datetime import date, datetime

import pytest

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.derivatives import black_76
from backend.api.algo.grammar_tokens.metric_underlying_theoretical_loss import (
    _metric_underlying_theoretical_loss,
)

IST_NOW = datetime(2026, 9, 1, 10, 0, 0)


def _fut_baseline(S_base, qty, is_mcx=False, dcv_base=0.0):
    return {'kind': 'fut', 'ltp_base': S_base, 'S_base': S_base,
            'is_mcx': is_mcx, 'qty': qty, 'dcv_base': dcv_base}


def test_future_leg_linear_reprice():
    """theoretical_loss(underlying) = qty * (S_now - S_base) for a
    futures leg — no option Greeks repricing."""
    rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'CRUDEOIL26OCTFUT',
             'quantity': 10, 'day_change_val': 0.0}]
    alert_state = {'underlying_theo_baseline': {
        'CRUDEOIL26OCTFUT': _fut_baseline(S_base=6000.0, qty=10, is_mcx=True),
    }}
    ctx = Context(position_rows=rows, spot_prices={'CRUDEOIL': 6050.0},
                  alert_state=alert_state, now=IST_NOW)
    theo = _metric_underlying_theoretical_loss(ctx, {'underlying': 'CRUDEOIL'})
    assert theo == pytest.approx(10 * (6050.0 - 6000.0)), f"got {theo}"


def test_option_leg_bs_reprice_matches_black_76_formula():
    """A frozen-σ option leg reprices via the SAME black_76 primitive
    the metric uses internally — not a reimplementation."""
    from backend.api.algo.derivatives import days_to_expiry

    S_base, K, sigma_base = 5550.0, 5500.0, 0.22
    expiry = date(2026, 10, 30)  # real parse_tradingsymbol('CRUDEOIL26OCT5500CE') output
    # T_now the metric itself will compute at IST_NOW — reused here so
    # the fixture's own ltp_base is built at the EXACT same T as the
    # metric will reprice against (not an approximate hand-picked DTE).
    T_now = days_to_expiry(expiry, ref=IST_NOW, close_time=(23, 30)) / 365.0
    ltp_base = black_76(S_base, K, T_now, 0.07, sigma_base, 'CE')
    rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'CRUDEOIL26OCT5500CE',
             'quantity': 5, 'day_change_val': 0.0}]
    alert_state = {'underlying_theo_baseline': {
        'CRUDEOIL26OCT5500CE': {
            'kind': 'opt', 'ltp_base': ltp_base, 'S_base': S_base,
            'sigma_base': sigma_base, 'K': K, 'opt_type': 'CE',
            'expiry': expiry, 'is_mcx': True, 'qty': 5,
        },
    }}
    # Spot unchanged from baseline, same `now` as baseline capture —
    # reprice must reproduce ltp_base exactly (zero theoretical move).
    ctx = Context(position_rows=rows, spot_prices={'CRUDEOIL': S_base},
                  alert_state=alert_state, now=IST_NOW)
    theo = _metric_underlying_theoretical_loss(ctx, {'underlying': 'CRUDEOIL'})
    assert theo == pytest.approx(0.0, abs=1e-6), f"expected ~0 at baseline spot, got {theo}"

    # Spot moves up — theoretical P&L must track the BS/Black-76
    # reprice, not a linear delta-only approximation.
    ctx2 = Context(position_rows=rows, spot_prices={'CRUDEOIL': S_base + 100.0},
                   alert_state=alert_state, now=IST_NOW)
    theo2 = _metric_underlying_theoretical_loss(ctx2, {'underlying': 'CRUDEOIL'})
    expected_price = black_76(S_base + 100.0, K, T_now, 0.07, sigma_base, 'CE')
    assert theo2 == pytest.approx(5 * (expected_price - ltp_base)), f"got {theo2}"


def test_missing_spot_price_is_none():
    rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'SYM', 'quantity': 1}]
    alert_state = {'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 1)}}
    ctx = Context(position_rows=rows, spot_prices={}, alert_state=alert_state, now=IST_NOW)
    assert _metric_underlying_theoretical_loss(ctx, {'underlying': 'CRUDEOIL'}) is None


def test_missing_baseline_for_one_leg_makes_whole_underlying_none():
    """A partial sum would UNDERSTATE theoretical loss — the whole
    underlying must report unavailable, not silently drop the leg."""
    rows = [
        {'underlying': 'NIFTY', 'tradingsymbol': 'LEG_A', 'quantity': 1},
        {'underlying': 'NIFTY', 'tradingsymbol': 'LEG_B', 'quantity': 1},
    ]
    alert_state = {'underlying_theo_baseline': {'LEG_A': _fut_baseline(100.0, 1)}}
    ctx = Context(position_rows=rows, spot_prices={'NIFTY': 100.0},
                  alert_state=alert_state, now=IST_NOW)
    assert _metric_underlying_theoretical_loss(ctx, {'underlying': 'NIFTY'}) is None


def test_no_position_rows_for_underlying_is_none():
    ctx = Context(position_rows=[], spot_prices={'NIFTY': 100.0}, now=IST_NOW)
    assert _metric_underlying_theoretical_loss(ctx, {'underlying': 'NIFTY'}) is None
