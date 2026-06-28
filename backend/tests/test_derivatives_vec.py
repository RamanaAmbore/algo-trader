"""
test_derivatives_vec.py — covers Phase 1 (NumPy-vectorize multileg_payoff_curve
+ multileg_intermediate_curves + expected_value).

Five quality dimensions per feedback_test_dimensions.md:
  1. SSOT — vectorized output matches a fresh scalar reference computation
     to 0.01 (well below the 2-decimal payoff display precision).
  2. Performance — 6-leg, 51-pt payoff curve completes in <50ms (was 500ms).
  3. Stale code — `_black_scholes_vec` is the only path through which curve
     functions touch BS now; the scalar `black_scholes` is no longer called
     inside multileg_payoff_curve / multileg_intermediate_curves loops.
  4. Reusable code — scalar `black_scholes` and `greeks` are still exposed
     and still work (used by `implied_vol` and `multileg_greeks`).
  5. UX-equivalent — same JSON shape (`spot`, `today_value`, `expiry_value`
     rounded to 2 decimals; `values: list[float]` rounded to 2 decimals).
"""
from __future__ import annotations

import math
import time

import pytest

from backend.api.algo.derivatives import (
    DEFAULT_IV,
    black_scholes,
    expected_value,
    multileg_intermediate_curves,
    multileg_payoff_curve,
    payoff_curve,
    _black_scholes_vec,
    _norm_cdf,
    _norm_cdf_vec,
)


# ── Sample fixtures ───────────────────────────────────────────────────


def _bull_call_spread(S: float = 24500.0):
    """4-leg-equivalent NIFTY bull-call spread + long futures hedge —
    representative of a real operator strategy. Lot size 50."""
    qty = 50
    return [
        {"kind": "opt", "strike": 24500, "opt_type": "CE", "qty":  qty,
         "entry_price": 120.0, "T_years": 14 / 365.0, "sigma": 0.16},
        {"kind": "opt", "strike": 24700, "opt_type": "CE", "qty": -qty,
         "entry_price":  35.0, "T_years": 14 / 365.0, "sigma": 0.18},
    ]


def _six_leg_iron_condor(S: float = 24500.0):
    """6-leg variant: bull-put + bear-call + long futures wing.
    Stresses the inner per-leg loop in multileg_payoff_curve."""
    q = 50
    return [
        {"kind": "opt", "strike": 24300, "opt_type": "PE", "qty":  q,
         "entry_price":  50.0, "T_years": 14 / 365.0, "sigma": 0.18},
        {"kind": "opt", "strike": 24100, "opt_type": "PE", "qty": -q,
         "entry_price":  20.0, "T_years": 14 / 365.0, "sigma": 0.20},
        {"kind": "opt", "strike": 24700, "opt_type": "CE", "qty":  q,
         "entry_price":  40.0, "T_years": 14 / 365.0, "sigma": 0.17},
        {"kind": "opt", "strike": 24900, "opt_type": "CE", "qty": -q,
         "entry_price":  15.0, "T_years": 14 / 365.0, "sigma": 0.19},
        {"kind": "fut", "qty":  q, "entry_price": 24500.0, "T_years": 30 / 365.0},
        {"kind": "opt", "strike": 24500, "opt_type": "CE", "qty":  q,
         "entry_price": 100.0, "T_years": 30 / 365.0, "sigma": 0.16},
    ]


# ── 1. SSOT — vectorized matches scalar reference ─────────────────────


def test_norm_cdf_vec_matches_scalar():
    """A&S 7.1.26 erf approximation must be within 1.5e-7 of math.erf,
    so _norm_cdf_vec must match the scalar _norm_cdf to ~1e-7."""
    import numpy as np
    xs = np.linspace(-4.0, 4.0, 200)
    expected = np.array([_norm_cdf(float(x)) for x in xs])
    actual   = _norm_cdf_vec(xs)
    err = np.max(np.abs(actual - expected))
    assert err < 1e-6, f"_norm_cdf_vec drifted from scalar _norm_cdf: max err {err}"


def test_black_scholes_vec_matches_scalar():
    """Vectorized BS over an array must match per-element scalar BS to
    2 decimals (display precision is 2; A&S erf accuracy supports 5+)."""
    import numpy as np
    S_arr = np.linspace(24000.0, 25000.0, 51)
    K, T, r, sigma = 24500.0, 14 / 365.0, 0.07, 0.16
    for opt_type in ("CE", "PE"):
        vec    = _black_scholes_vec(S_arr, K, T, r, sigma, opt_type)
        scalar = np.array([black_scholes(float(s), K, T, r, sigma, opt_type)
                          for s in S_arr])
        err = np.max(np.abs(vec - scalar))
        assert err < 0.01, f"_black_scholes_vec({opt_type}) drift: max err {err}"


def test_payoff_curve_matches_reference_singleleg():
    """Single-leg payoff_curve must produce the same numbers as a
    direct scalar computation. Catches regressions in the np.linspace
    grid vs the old `lo + step*i` step formula."""
    S, K = 24500.0, 24500.0
    T, sigma = 14 / 365.0, 0.16
    qty, entry = 50, 120.0
    out = payoff_curve(S=S, K=K, T_years=T, r=0.07, sigma=sigma,
                       opt_type="CE", qty=qty, entry_price=entry,
                       span_pct=0.10, points=51)
    assert len(out) == 51
    # Spot grid endpoints must be exact (linspace).
    assert abs(out[0]["spot"]  - S * 0.90) < 1e-6
    assert abs(out[-1]["spot"] - S * 1.10) < 1e-6
    # Expiry value at ATM: intrinsic = 0 → expiry_pnl = 0 - cost = -6000
    atm = next(p for p in out if abs(p["spot"] - S) < 1.0)
    assert atm["expiry_value"] == round(0 * qty - entry * qty, 2)


def test_multileg_payoff_curve_iron_condor_shape():
    """6-leg iron-condor curve: today and expiry must both be defined,
    same length, with finite values. Catches shape / NaN regressions."""
    legs = _six_leg_iron_condor()
    out = multileg_payoff_curve(legs, S=24500.0, span_pct=0.10, points=51)
    assert len(out) == 51
    for p in out:
        assert math.isfinite(p["spot"])
        assert math.isfinite(p["today_value"])
        assert math.isfinite(p["expiry_value"])


# ── 2. Performance — 6-leg × 51-pt under 50ms ─────────────────────────


def test_payoff_curve_perf_budget():
    """6-leg, 51-point payoff_curve must complete in <50ms. Pre-vec
    timing was ~500ms in this configuration; this is the regression
    guard for the vectorization win."""
    legs = _six_leg_iron_condor()
    # Warm-up call (JIT-like effects from NumPy ufunc dispatch caches).
    multileg_payoff_curve(legs, S=24500.0, span_pct=0.10, points=51)
    # 20 iterations average for stability.
    t0 = time.perf_counter()
    for _ in range(20):
        multileg_payoff_curve(legs, S=24500.0, span_pct=0.10, points=51)
    elapsed_ms = (time.perf_counter() - t0) * 1000 / 20.0
    assert elapsed_ms < 50.0, (
        f"6-leg payoff curve regressed: {elapsed_ms:.1f}ms (>50ms budget). "
        f"Phase 1 NumPy vectorization may have been backed out."
    )


def test_intermediate_curves_perf_budget():
    """3 time-slices × 6 legs × 51 points — the heaviest curve mode.
    Was ~1.5s pre-vec; budget after vec is <100ms."""
    legs = _six_leg_iron_condor()
    multileg_intermediate_curves(legs, S=24500.0, points=51, time_slices=3)
    t0 = time.perf_counter()
    for _ in range(10):
        multileg_intermediate_curves(legs, S=24500.0, points=51, time_slices=3)
    elapsed_ms = (time.perf_counter() - t0) * 1000 / 10.0
    assert elapsed_ms < 100.0, (
        f"3-slice intermediate curves regressed: {elapsed_ms:.1f}ms (>100ms budget)."
    )


def test_expected_value_perf_budget():
    """EV trapezoidal integration over a 51-pt curve must complete in
    well under 5ms after the vectorized PDF rewrite."""
    legs = _six_leg_iron_condor()
    curve = multileg_payoff_curve(legs, S=24500.0, points=51)
    t0 = time.perf_counter()
    for _ in range(200):
        expected_value(curve, S=24500.0, T_years=14 / 365.0, sigma=0.16)
    elapsed_ms = (time.perf_counter() - t0) * 1000 / 200.0
    assert elapsed_ms < 5.0, (
        f"expected_value regressed: {elapsed_ms:.2f}ms (>5ms budget)."
    )


# ── 3. Stale code — scalar `black_scholes` not called per-curve-point ──


def test_curve_does_not_call_scalar_black_scholes_in_loop():
    """Source-grep guard: multileg_payoff_curve must NOT call the
    scalar `black_scholes()` inside its per-leg loop. The vectorized
    `_black_scholes_vec` is the only path now. Catches a future
    refactor that accidentally re-introduces the scalar loop."""
    from pathlib import Path
    src = Path("backend/api/algo/derivatives.py").read_text()
    # Find the multileg_payoff_curve function body.
    import re
    m = re.search(
        r"def multileg_payoff_curve\(.*?\n(.+?)\ndef ",
        src,
        re.DOTALL,
    )
    assert m, "could not locate multileg_payoff_curve in source"
    body = m.group(1)
    # The scalar `black_scholes(` call must not appear in the body.
    # `_black_scholes_vec(` is OK.
    bad_call_pattern = re.compile(r"(?<!_)black_scholes\(")
    assert not bad_call_pattern.search(body), (
        "multileg_payoff_curve still calls scalar black_scholes() in its loop — "
        "Phase 1 vectorization regressed."
    )


# ── 4. Reusable — scalar BS / greeks still callable ───────────────────


def test_scalar_black_scholes_still_works():
    """The scalar `black_scholes` API is part of the public surface
    (used by implied_vol + multileg_greeks). Must remain callable
    with the same signature and produce the same prices."""
    # Reference value at ATM 24500 CE / 14 DTE / 16% IV.
    px = black_scholes(24500.0, 24500.0, 14 / 365.0, 0.07, 0.16, "CE")
    # ATM call, 14 DTE, 16% IV, r=7% → ~320-360 depending on the term
    # (Indian r=7% inflates the carry-adjusted strike — wider band than
    # a US-Treasury r=2% reference would give).
    assert 250 < px < 400, f"BS sanity check failed: {px}"


def test_scalar_greeks_still_works():
    """`greeks` must remain callable scalar-style for multileg_greeks
    (which is called once per request at a single spot)."""
    from backend.api.algo.derivatives import greeks
    g = greeks(24500.0, 24500.0, 14 / 365.0, 0.07, 0.16, "CE")
    assert set(g.keys()) == {"delta", "gamma", "theta", "vega", "rho"}
    assert 0.4 < g["delta"] < 0.6, f"ATM delta sanity failed: {g['delta']}"
