"""
Tests for the `is_mcx` Black-76 dispatch in the single-leg
`/api/options/analytics` helper chain (`backend/api/routes/options.py`):
`_ltp_bs_estimate`, `_resolve_iv_for_analytics`, `_analytics_compute_metrics`.

Context: these three helpers priced every option via plain Black-Scholes
(`black_scholes`/`implied_vol`/`greeks`) even when the resolved underlying
is an MCX commodity whose "spot" is the matching futures contract price
(see `derivatives.py`'s cost-of-carry `b` comment block, and the sibling
fix already shipped for `_strategy_build_option_leg` — the multi-leg
analogue of this single-leg endpoint). Fixed by threading `is_mcx`
through to select `black_76`/`implied_vol_76`/`greeks_76`.

Five quality dimensions:
  1. SSOT       — exercises the real production helpers, not a reimplementation
  2. Correctness— MCX path numerically matches black_76()/greeks_76() and
                  differs materially from the plain-BS path
  3. Regression — spies on the module-level names `options.py` imports, so
                  reverting any branch to always-BS fails these tests
  4. Reusable   — mirrors test_derivatives_vec.py's existing dispatch pattern
  5. Back-compat— is_mcx defaults to False; NSE/index callers are unaffected
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.api.algo.derivatives import (
    DEFAULT_IV,
    DEFAULT_RISK_FREE,
    black_76,
    black_scholes,
    greeks,
    greeks_76,
)

_S = 5500.0   # futures/spot price
_K = 5500.0   # ATM strike
_T = 30 / 365.0
_OPT = "CE"


# ── _ltp_bs_estimate ───────────────────────────────────────────────────

def test_ltp_bs_estimate_mcx_uses_black_76():
    from backend.api.routes.options import _ltp_bs_estimate

    with patch("backend.api.routes.options.black_76", wraps=black_76) as spy_76, \
         patch("backend.api.routes.options.black_scholes", wraps=black_scholes) as spy_bs:
        hit = _ltp_bs_estimate({
            "spot": _S, "strike": _K, "T_years": _T, "opt_type": _OPT, "is_mcx": True,
        })

    assert spy_76.called, "is_mcx=True must dispatch through black_76"
    assert not spy_bs.called, "is_mcx=True must NOT fall through to plain black_scholes()"
    assert hit is not None
    price, source = hit
    assert source == "estimated"
    expected = black_76(_S, _K, _T, DEFAULT_RISK_FREE, DEFAULT_IV, _OPT)
    assert price == pytest.approx(expected)
    # Must differ materially from the plain-BS estimate at the same inputs.
    bs_price = black_scholes(_S, _K, _T, DEFAULT_RISK_FREE, DEFAULT_IV, _OPT)
    assert abs(price - bs_price) > 1e-6


def test_ltp_bs_estimate_default_stays_on_plain_bs():
    from backend.api.routes.options import _ltp_bs_estimate

    with patch("backend.api.routes.options.black_76", wraps=black_76) as spy_76, \
         patch("backend.api.routes.options.black_scholes", wraps=black_scholes) as spy_bs:
        hit = _ltp_bs_estimate({
            "spot": 2800.0, "strike": 2800.0, "T_years": _T, "opt_type": _OPT,
        })  # is_mcx absent -> defaults False

    assert spy_bs.called, "is_mcx absent must use plain black_scholes()"
    assert not spy_76.called
    assert hit is not None


# ── _resolve_iv_for_analytics ──────────────────────────────────────────

def test_resolve_iv_for_analytics_mcx_uses_implied_vol_76():
    from backend.api.routes.options import _resolve_iv_for_analytics
    from backend.api.algo.derivatives import implied_vol, implied_vol_76

    ltp = black_76(_S, _K, _T, DEFAULT_RISK_FREE, 0.22, _OPT)
    with patch("backend.api.routes.options.implied_vol_76", wraps=implied_vol_76) as spy_76, \
         patch("backend.api.routes.options.implied_vol", wraps=implied_vol) as spy_bs:
        sigma, src = _resolve_iv_for_analytics(
            None, ltp, "live", _S, _K, _T, _OPT, True,
        )

    assert spy_76.called, "is_mcx=True must calibrate via implied_vol_76"
    assert not spy_bs.called
    assert sigma == pytest.approx(0.22, abs=1e-3)
    assert src == "calibrated"


def test_resolve_iv_for_analytics_default_stays_on_plain_bs():
    from backend.api.routes.options import _resolve_iv_for_analytics
    from backend.api.algo.derivatives import implied_vol, implied_vol_76

    ltp = black_scholes(2800.0, 2800.0, _T, DEFAULT_RISK_FREE, 0.22, _OPT)
    with patch("backend.api.routes.options.implied_vol_76", wraps=implied_vol_76) as spy_76, \
         patch("backend.api.routes.options.implied_vol", wraps=implied_vol) as spy_bs:
        sigma, src = _resolve_iv_for_analytics(
            None, ltp, "live", 2800.0, 2800.0, _T, _OPT,
        )  # is_mcx defaults False

    assert spy_bs.called
    assert not spy_76.called
    assert sigma == pytest.approx(0.22, abs=1e-3)


# ── _analytics_compute_metrics ─────────────────────────────────────────

def test_analytics_compute_metrics_mcx_dispatches_black_76():
    from backend.api.routes.options import _analytics_compute_metrics

    parsed = {"strike": _K, "opt_type": _OPT}
    with patch("backend.api.routes.options.black_76", wraps=black_76) as spy_price76, \
         patch("backend.api.routes.options.black_scholes", wraps=black_scholes) as spy_price_bs, \
         patch("backend.api.routes.options.greeks_76", wraps=greeks_76) as spy_g76, \
         patch("backend.api.routes.options.greeks", wraps=greeks) as spy_gbs:
        (theo, disc, disc_pct, g_per, g_pos, entry, risk,
         span_pct_resolved, curve, slices, ev, ev_pct, rr) = _analytics_compute_metrics(
            _S, parsed, _T, 0.20, 250.0, 1, 0.0,
            None, 3.0, 51, 0,
            is_mcx=True,
        )

    assert spy_price76.called, "MCX analytics must price theo via black_76"
    assert not spy_price_bs.called, "MCX analytics must NOT fall through to plain black_scholes"
    assert spy_g76.called, "MCX analytics must compute Greeks via greeks_76"
    assert not spy_gbs.called, "MCX analytics must NOT fall through to plain greeks()"

    theo76 = black_76(_S, _K, _T, DEFAULT_RISK_FREE, 0.20, _OPT)
    assert theo == pytest.approx(theo76)
    theo_bs = black_scholes(_S, _K, _T, DEFAULT_RISK_FREE, 0.20, _OPT)
    assert abs(theo - theo_bs) > 1e-6
    g76 = greeks_76(_S, _K, _T, DEFAULT_RISK_FREE, 0.20, _OPT)
    for k in g_per:
        assert g_per[k] == pytest.approx(g76[k], abs=1e-9)


def test_analytics_compute_metrics_default_stays_on_plain_bs():
    from backend.api.routes.options import _analytics_compute_metrics

    parsed = {"strike": 2800.0, "opt_type": _OPT}
    with patch("backend.api.routes.options.black_76", wraps=black_76) as spy_price76, \
         patch("backend.api.routes.options.black_scholes", wraps=black_scholes) as spy_price_bs, \
         patch("backend.api.routes.options.greeks_76", wraps=greeks_76) as spy_g76, \
         patch("backend.api.routes.options.greeks", wraps=greeks) as spy_gbs:
        (theo, *_rest) = _analytics_compute_metrics(
            2800.0, parsed, _T, 0.20, 50.0, 1, 0.0,
            None, 3.0, 51, 0,
        )  # is_mcx defaults False

    assert spy_price_bs.called
    assert not spy_price76.called
    assert spy_gbs.called
    assert not spy_g76.called
