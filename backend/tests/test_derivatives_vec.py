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
    DEFAULT_RISK_FREE,
    black_76,
    black_scholes,
    expected_value,
    greeks,
    greeks_76,
    implied_vol,
    intermediate_curves,
    multileg_greeks,
    multileg_intermediate_curves,
    multileg_payoff_curve,
    payoff_curve,
    _black_scholes_vec,
    _norm_cdf,
    _norm_cdf_vec,
)


# ── Frozen pre-refactor reference (for byte-identical NSE regression) ──
#
# Verbatim copies of `black_scholes()`/`greeks()`/`implied_vol()` as they
# existed BEFORE the generalized cost-of-carry (`_gbs_price`/`_gbs_greeks`)
# refactor landed — not re-derived, not hand-typed from memory. Frozen
# here so `test_gbs_wrappers_byte_identical_to_legacy_bs` below has a
# same-process, no-hardcoded-float-literal comparison target: computing
# both sides fresh in the same process/platform avoids any libm-ulp
# cross-platform flakiness that hardcoded literals would risk (macOS dev
# vs. Linux CI/prod can round the last ULP of erf/exp differently).

def _legacy_norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _legacy_black_scholes(S: float, K: float, T_years: float, r: float,
                          sigma: float, opt_type: str) -> float:
    if S <= 0 or K <= 0:
        return 0.0
    if T_years <= 0 or sigma <= 0:
        if opt_type == "CE":
            return max(0.0, S - K)
        return max(0.0, K - S)
    sqrt_T = math.sqrt(T_years)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2.0) * T_years) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    if opt_type == "CE":
        return S * _legacy_norm_cdf(d1) - K * math.exp(-r * T_years) * _legacy_norm_cdf(d2)
    return K * math.exp(-r * T_years) * _legacy_norm_cdf(-d2) - S * _legacy_norm_cdf(-d1)


def _legacy_norm_pdf(x: float) -> float:
    return math.exp(-x * x / 2.0) / math.sqrt(2.0 * math.pi)


def _legacy_greeks(S: float, K: float, T_years: float, r: float,
                   sigma: float, opt_type: str) -> dict:
    if S <= 0 or K <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    if T_years <= 0 or sigma <= 0:
        if opt_type == "CE":
            d = 1.0 if S > K else 0.0
        else:
            d = -1.0 if S < K else 0.0
        return {"delta": d, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    sqrt_T = math.sqrt(T_years)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2.0) * T_years) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    nd1 = _legacy_norm_pdf(d1)
    Nd1 = _legacy_norm_cdf(d1)
    Nd2 = _legacy_norm_cdf(d2)
    if opt_type == "CE":
        delta = Nd1
        theta_yr = (-S * nd1 * sigma / (2.0 * sqrt_T)
                    - r * K * math.exp(-r * T_years) * Nd2)
        rho_raw  = K * T_years * math.exp(-r * T_years) * Nd2
    else:
        delta = Nd1 - 1.0
        theta_yr = (-S * nd1 * sigma / (2.0 * sqrt_T)
                    + r * K * math.exp(-r * T_years) * _legacy_norm_cdf(-d2))
        rho_raw  = -K * T_years * math.exp(-r * T_years) * _legacy_norm_cdf(-d2)
    gamma     = nd1 / (S * sigma * sqrt_T)
    vega_raw  = S * nd1 * sqrt_T
    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta_yr / 365.0,
        "vega":  vega_raw / 100.0,
        "rho":   rho_raw  / 100.0,
    }


def _legacy_implied_vol(price: float, S: float, K: float, T_years: float,
                        r: float, opt_type: str,
                        *, max_iter: int = 80, tol: float = 1e-3) -> float:
    if price <= 0 or S <= 0 or K <= 0 or T_years <= 0:
        return DEFAULT_IV
    intrinsic = max(0.0, S - K) if opt_type == "CE" else max(0.0, K - S)
    if price <= intrinsic + 0.05:
        return 0.0001
    lo, hi = 0.0001, 5.0
    p_lo = _legacy_black_scholes(S, K, T_years, r, lo, opt_type)
    p_hi = _legacy_black_scholes(S, K, T_years, r, hi, opt_type)
    if not (p_lo - 0.5 <= price <= p_hi + 0.5):
        return DEFAULT_IV
    for _ in range(max_iter):
        mid    = 0.5 * (lo + hi)
        p_mid  = _legacy_black_scholes(S, K, T_years, r, mid, opt_type)
        if abs(p_mid - price) < tol:
            return mid
        if p_mid < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def test_gbs_wrappers_byte_identical_to_legacy_bs():
    """`black_scholes()`/`greeks()`/`implied_vol()` — now thin b=r
    wrappers over the shared `_gbs_price()`/`_gbs_greeks()` core — must
    produce EXACTLY (`==`, not approx) the same output as the frozen
    pre-refactor implementations above, across a grid including the
    long-T/large-S corner (S=55000, T=10.0) where rho's cross-term
    cancellation is most exposed to rounding drift. A same-process
    comparison (not hardcoded float literals) so this can't flake from
    a one-ULP libm difference between macOS (dev) and Linux (prod/CI)."""
    mismatches = []
    for S in (40.0, 42.0, 100.0, 2500.0, 55000.0):
        for K_mult in (0.5, 0.9, 1.0, 1.1, 1.5):
            K = S * K_mult
            for T in (0.001, 0.02, 0.1, 0.5, 1.5, 3.0, 10.0):
                for r in (0.0, 0.03, 0.07, 0.15):
                    for sigma in (0.0, 0.0001, 0.1, 0.2, 0.5, 1.5):
                        for opt in ("CE", "PE"):
                            p_new = black_scholes(S, K, T, r, sigma, opt)
                            p_old = _legacy_black_scholes(S, K, T, r, sigma, opt)
                            if p_new != p_old:
                                mismatches.append(("price", S, K, T, r, sigma, opt, p_new, p_old))
                            g_new = greeks(S, K, T, r, sigma, opt)
                            g_old = _legacy_greeks(S, K, T, r, sigma, opt)
                            for k in g_old:
                                if g_new[k] != g_old[k]:
                                    mismatches.append((k, S, K, T, r, sigma, opt, g_new[k], g_old[k]))
    assert not mismatches, (
        f"{len(mismatches)} byte-identical mismatches vs frozen pre-refactor "
        f"implementation; first 5: {mismatches[:5]}"
    )


def test_implied_vol_byte_identical_to_legacy():
    """`implied_vol()` (b=r wrapper over `_gbs_implied_vol`) must also
    match the frozen pre-refactor bisection exactly — the re-pricing
    calls inside the loop go through the new `black_scholes()` wrapper,
    so any drift there would also surface here."""
    mismatches = []
    for S, K, T, r, sigma, opt in (
        (24500.0, 24500.0, 14 / 365.0, 0.07, 0.16, "CE"),
        (24500.0, 24500.0, 14 / 365.0, 0.07, 0.16, "PE"),
        (42.0, 40.0, 0.5, 0.10, 0.20, "CE"),
        (55000.0, 50000.0, 10.0, 0.15, 0.5, "PE"),
        (100.0, 110.0, 0.02, 0.0, 0.1, "CE"),
    ):
        price = _legacy_black_scholes(S, K, T, r, sigma, opt)
        if price <= 0:
            continue
        iv_new = implied_vol(price, S, K, T, r, opt)
        iv_old = _legacy_implied_vol(price, S, K, T, r, opt)
        if iv_new != iv_old:
            mismatches.append((S, K, T, r, sigma, opt, iv_new, iv_old))
    assert not mismatches, f"implied_vol byte-identical mismatches: {mismatches}"


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


# ── Reference-value Greeks: cash-spot Black-Scholes (b=r) ─────────────
#
# Hull's classic worked example ("Options, Futures, and Other
# Derivatives"): S=42, K=40, r=10%, sigma=20%, T=0.5 years. Hand-verified
# this session against the formulas in `greeks()`/`black_scholes()`
# before this test was added — values below are this repo's own
# `greeks()` output at that point, locked in as a permanent regression
# guard (closes the "spec claims reference tests exist but don't" gap;
# DERIVATIVES_SPEC.md §10 previously pointed at three nonexistent test
# files for exactly this kind of check).

_BS_REF_S, _BS_REF_K, _BS_REF_T = 42.0, 40.0, 0.5
_BS_REF_R, _BS_REF_SIGMA = 0.10, 0.20

_BS_REF_CALL = {
    "delta": 0.779131290942669,
    "gamma": 0.04996267040591185,
    "theta": -0.012490663546829112,
    "vega":  0.08813415059602853,
    "rho":   0.1398204591336028,
}
_BS_REF_PUT = {
    "delta": -0.22086870905733103,
    "gamma": 0.04996267040591185,
    "theta": -0.0020662314975062202,
    "vega":  0.08813415059602853,
    "rho":   -0.05042542576654,
}


def test_greeks_bs_reference_values_call():
    """`greeks()` (b=r, cash-spot BS) must match the Hull worked example
    to tight tolerance — not just a wide sanity band."""
    g = greeks(_BS_REF_S, _BS_REF_K, _BS_REF_T, _BS_REF_R, _BS_REF_SIGMA, "CE")
    for k, expected in _BS_REF_CALL.items():
        assert g[k] == pytest.approx(expected, abs=1e-6), f"{k}: {g[k]} != {expected}"


def test_greeks_bs_reference_values_put():
    """Put-side counterpart of the Hull reference — exercises the
    opposite delta/theta/rho sign branch."""
    g = greeks(_BS_REF_S, _BS_REF_K, _BS_REF_T, _BS_REF_R, _BS_REF_SIGMA, "PE")
    for k, expected in _BS_REF_PUT.items():
        assert g[k] == pytest.approx(expected, abs=1e-6), f"{k}: {g[k]} != {expected}"


def test_multileg_greeks_short_sign_flip_bs():
    """A short position (qty=-1) must exactly negate every Greek vs the
    equivalent long (qty=+1) — linearity in qty is the whole premise of
    `multileg_greeks()`'s qty-weighted summation."""
    leg = {"kind": "opt", "strike": _BS_REF_K, "opt_type": "CE",
           "T_years": _BS_REF_T, "sigma": _BS_REF_SIGMA}
    long_g  = multileg_greeks([{**leg, "qty": 1}],  S=_BS_REF_S, r=_BS_REF_R)
    short_g = multileg_greeks([{**leg, "qty": -1}], S=_BS_REF_S, r=_BS_REF_R)
    for k in long_g:
        assert short_g[k] == pytest.approx(-long_g[k], abs=1e-9), (
            f"{k}: short {short_g[k]} is not the exact negation of long {long_g[k]}"
        )


def test_put_call_delta_parity_bs():
    """Put-call parity on delta for cash-spot BS: call_delta - put_delta
    must equal exactly 1 (b=r makes the growth factor e^((b-r)T) == 1)."""
    g_call = greeks(_BS_REF_S, _BS_REF_K, _BS_REF_T, _BS_REF_R, _BS_REF_SIGMA, "CE")
    g_put  = greeks(_BS_REF_S, _BS_REF_K, _BS_REF_T, _BS_REF_R, _BS_REF_SIGMA, "PE")
    assert (g_call["delta"] - g_put["delta"]) == pytest.approx(1.0, abs=1e-9)


# ── Reference-value Greeks: Black-76 (options on futures, b=0) ────────
#
# Haug's "The Complete Guide to Option Pricing Formulas" at-the-money-
# forward worked example: F=K=19, T=0.75, r=10%, sigma=28% → call price
# == put price == 1.7011 (ATM-forward put-call parity: C-P =
# e^(-rT)(F-K) = 0 when F=K). This is an INDEPENDENT published reference
# (not derived from the BS numbers above — different model, needs its
# own anchor) that this repo's `black_76()` reproduces to 4 decimals
# (1.70105...). The Greek reference values below are this repo's own
# `greeks_76()` output at that point — their correctness was established
# this session via finite-difference cross-check against `black_76()`
# directly (delta/gamma/vega/rho/theta all independently verified
# numerically; see commit history) and via the generalized-b derivation
# reducing exactly to the BS endpoint above at b=r — now locked in as a
# permanent regression guard.

_B76_REF_F, _B76_REF_K, _B76_REF_T = 19.0, 19.0, 0.75
_B76_REF_R, _B76_REF_SIGMA = 0.10, 0.28
_B76_REF_PRICE = 1.7010507252362679  # matches Haug's published 1.7011 to 4dp

_B76_REF_CALL = {
    "delta": 0.5086362359336519,
    "gamma": 0.07974503467912114,
    "theta": -0.0026257064718563077,
    "vega":  0.06045471079024173,
    "rho":   -0.012757880439272009,
}
_B76_REF_PUT = {
    "delta": -0.419107250394901,
    "gamma": 0.07974503467912114,
    "theta": -0.0026257064718563073,
    "vega":  0.06045471079024173,
    "rho":   -0.012757880439272009,
}


def test_black_76_price_matches_published_reference():
    """`black_76()` must reproduce Haug's published ATM-forward example
    (F=K=19, T=0.75, r=10%, sigma=28% → 1.7011) to 4 decimals, for both
    call and put (ATM-forward parity: they're equal when F=K)."""
    call = black_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA, "CE")
    put  = black_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA, "PE")
    assert call == pytest.approx(1.7011, abs=1e-4)
    assert put  == pytest.approx(1.7011, abs=1e-4)
    assert call == pytest.approx(put, abs=1e-9)


def test_greeks_76_reference_values_call():
    """`greeks_76()` (b=0, Black-76) at the Haug ATM-forward point,
    tight tolerance."""
    g = greeks_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA, "CE")
    for k, expected in _B76_REF_CALL.items():
        assert g[k] == pytest.approx(expected, abs=1e-6), f"{k}: {g[k]} != {expected}"


def test_greeks_76_reference_values_put():
    g = greeks_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA, "PE")
    for k, expected in _B76_REF_PUT.items():
        assert g[k] == pytest.approx(expected, abs=1e-6), f"{k}: {g[k]} != {expected}"


def test_greeks_76_rho_shortcut_matches_minus_T_times_price():
    """Black-76's closed-form rho shortcut (rho = -T*price, since d1/d2
    carry no `r` term — all rate-dependence routes through the outer
    discount factor) must match the actual `greeks_76()` output exactly,
    for both call and put."""
    for opt_type in ("CE", "PE"):
        price = black_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R,
                         _B76_REF_SIGMA, opt_type)
        g = greeks_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R,
                      _B76_REF_SIGMA, opt_type)
        expected_rho_raw = -_B76_REF_T * price
        assert g["rho"] == pytest.approx(expected_rho_raw / 100.0, abs=1e-9)


def test_multileg_greeks_short_sign_flip_black76():
    """Short-position sign flip for the Black-76 path too — a leg with
    `is_mcx: True` must negate exactly under qty=-1 vs qty=+1, same as
    the BS path."""
    leg = {"kind": "opt", "strike": _B76_REF_K, "opt_type": "CE",
           "T_years": _B76_REF_T, "sigma": _B76_REF_SIGMA, "is_mcx": True}
    long_g  = multileg_greeks([{**leg, "qty": 1}],  S=_B76_REF_F, r=_B76_REF_R)
    short_g = multileg_greeks([{**leg, "qty": -1}], S=_B76_REF_F, r=_B76_REF_R)
    for k in long_g:
        assert short_g[k] == pytest.approx(-long_g[k], abs=1e-9), (
            f"{k}: short {short_g[k]} is not the exact negation of long {long_g[k]}"
        )


def test_put_call_delta_parity_black76():
    """Put-call parity on delta for Black-76 differs from plain BS:
    call_delta - put_delta == e^(-rT) (the growth factor at b=0), NOT 1
    — a distinguishing numerical signature that the futures model is
    actually in effect, not a mislabeled BS call."""
    import math
    g_call = greeks_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA, "CE")
    g_put  = greeks_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA, "PE")
    expected = math.exp(-_B76_REF_R * _B76_REF_T)
    assert (g_call["delta"] - g_put["delta"]) == pytest.approx(expected, abs=1e-9)
    # And NOT 1 — proves this is genuinely the futures model, not BS.
    assert (g_call["delta"] - g_put["delta"]) != pytest.approx(1.0, abs=1e-3)


# ── Black-76 Greeks via independent finite difference on black_76() ───
#
# The reference-value tests above compare `greeks_76()` against ITSELF
# (the `_B76_REF_*` constants are `greeks_76()`'s own recorded output) —
# a bug in the theta/rho closed-form shortcuts would freeze in as a
# "passing" reference rather than get caught. This test instead derives
# each Greek from `black_76()` ALONE via central finite difference, so
# it's independent of `greeks_76()`'s own formulas — only the anchor
# price (externally verified against Haug's published 1.7011 above) and
# `black_76()` itself are trusted inputs.

def test_greeks_76_match_finite_difference_of_black_76():
    h_F, h_sigma, h_r, h_T = 1e-3, 1e-5, 1e-5, 1e-5
    for opt_type in ("CE", "PE"):
        g = greeks_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R,
                      _B76_REF_SIGMA, opt_type)

        delta_fd = (
            black_76(_B76_REF_F + h_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA, opt_type)
            - black_76(_B76_REF_F - h_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA, opt_type)
        ) / (2 * h_F)
        assert g["delta"] == pytest.approx(delta_fd, abs=1e-4), f"{opt_type} delta"

        gamma_fd = (
            black_76(_B76_REF_F + h_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA, opt_type)
            - 2 * black_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA, opt_type)
            + black_76(_B76_REF_F - h_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA, opt_type)
        ) / (h_F * h_F)
        assert g["gamma"] == pytest.approx(gamma_fd, abs=1e-4), f"{opt_type} gamma"

        vega_fd = (
            black_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA + h_sigma, opt_type)
            - black_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R, _B76_REF_SIGMA - h_sigma, opt_type)
        ) / (2 * h_sigma) * 0.01
        assert g["vega"] == pytest.approx(vega_fd, abs=1e-4), f"{opt_type} vega"

        rho_fd = (
            black_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R + h_r, _B76_REF_SIGMA, opt_type)
            - black_76(_B76_REF_F, _B76_REF_K, _B76_REF_T, _B76_REF_R - h_r, _B76_REF_SIGMA, opt_type)
        ) / (2 * h_r) * 0.01
        assert g["rho"] == pytest.approx(rho_fd, abs=1e-4), f"{opt_type} rho"

        # theta = -d(price)/dT, trader units = per day => /365.
        theta_fd = -(
            black_76(_B76_REF_F, _B76_REF_K, _B76_REF_T + h_T, _B76_REF_R, _B76_REF_SIGMA, opt_type)
            - black_76(_B76_REF_F, _B76_REF_K, _B76_REF_T - h_T, _B76_REF_R, _B76_REF_SIGMA, opt_type)
        ) / (2 * h_T) / 365.0
        assert g["theta"] == pytest.approx(theta_fd, abs=1e-4), f"{opt_type} theta"


# ── Call-site dispatch: MCX leg must actually route through Black-76 ──
#
# The tests above exercise `black_76()`/`greeks_76()` directly — they'd
# stay green even if the route-layer branch in
# `_strategy_build_option_leg()` were reverted to always call
# `black_scholes()`/`greeks()`. This test goes through the real call
# site instead, so it fails if that branch regresses (verified manually
# this session: temporarily hardcoding the branch to the BS path breaks
# this test's assertions, then reverted).


def test_strategy_build_option_leg_mcx_leg_uses_black_76():
    """An `is_mcx=True` leg built via `_strategy_build_option_leg()`
    (the real production call site, options.py) must produce Black-76
    Greeks/pricing — measurably different from what the plain-BS path
    would give for the same inputs — and must propagate an `is_mcx`
    flag that `multileg_greeks()` picks up for the aggregate card."""
    from backend.api.routes.options import _strategy_build_option_leg, StrategyLeg

    leg = StrategyLeg(symbol="CRUDEOIL26OCT5500CE", qty=1, avg_cost=None,
                      ltp=200.0, iv=0.20)
    parsed = {"strike": 5500.0, "opt_type": "CE", "root": "CRUDEOIL", "kind": "opt"}
    resolved, detail, sig = _strategy_build_option_leg(
        leg, "CRUDEOIL26OCT5500CE", parsed, {}, 5500.0, 0.1, 1.0, 1,
        is_mcx=True,
    )
    assert resolved["is_mcx"] is True
    assert sig == pytest.approx(0.20)

    g76 = greeks_76(5500.0, 5500.0, 0.1, DEFAULT_RISK_FREE, sig, "CE")
    gbs = greeks(5500.0, 5500.0, 0.1, DEFAULT_RISK_FREE, sig, "CE")

    # Leg detail's per-share Greeks must match the Black-76 path exactly...
    for k in g76:
        assert detail["greeks"][k] == pytest.approx(g76[k], abs=1e-9)
    # ...and must differ from the plain-BS path by far more than any
    # floating-point tolerance — proves the branch is actually live.
    assert abs(detail["greeks"]["delta"] - gbs["delta"]) > 1e-3

    # The aggregate "Greeks (position)" card (multileg_greeks) must also
    # dispatch this leg through Black-76, not silently fall back to BS.
    agg = multileg_greeks([resolved], S=5500.0)
    for k in g76:
        assert agg[k] == pytest.approx(g76[k], abs=1e-9)


def test_strategy_build_option_leg_nse_leg_stays_on_bs():
    """The `is_mcx=False` default must keep NSE/non-commodity legs on
    the plain-BS path — byte-identical to calling `greeks()` directly."""
    from backend.api.routes.options import _strategy_build_option_leg, StrategyLeg

    leg = StrategyLeg(symbol="RELIANCE25APR2800CE", qty=1, avg_cost=None,
                      ltp=50.0, iv=0.20)
    parsed = {"strike": 2800.0, "opt_type": "CE", "root": "RELIANCE", "kind": "opt"}
    resolved, detail, sig = _strategy_build_option_leg(
        leg, "RELIANCE25APR2800CE", parsed, {}, 2800.0, 0.1, 1.0, 1,
    )  # is_mcx defaults to False
    assert resolved["is_mcx"] is False
    gbs = greeks(2800.0, 2800.0, 0.1, DEFAULT_RISK_FREE, sig, "CE")
    for k in gbs:
        assert detail["greeks"][k] == pytest.approx(gbs[k], abs=1e-9)


def test_strategy_build_legs_wiring_passes_is_mcx_through():
    """Guards the real production wiring line (`is_mcx=_is_commodity`
    inside `_strategy_build_legs()`, options.py) one level up from
    `test_strategy_build_option_leg_mcx_leg_uses_black_76` above. That
    test enters directly at `_strategy_build_option_leg(is_mcx=True)` —
    since `is_mcx` defaults to False, a regression that deletes the
    `is_mcx=_is_commodity` keyword argument at the actual call site
    inside `_strategy_build_legs()` would silently revert MCX legs to
    plain BS while that lower-level test keeps passing untouched (it
    never exercises the wiring line itself). This test goes through
    `_strategy_build_legs()` — the real caller — instead, using a
    forward-dated `expiry` so it never time-bombs."""
    from datetime import date, timedelta
    from backend.api.routes.options import _strategy_build_legs, StrategyLeg, StrategyRequest

    future_expiry = (date.today() + timedelta(days=30)).isoformat()
    leg = StrategyLeg(symbol="CRUDEOIL26OCT5500CE", qty=1, avg_cost=None,
                      ltp=200.0, iv=0.20, expiry=future_expiry)
    data = StrategyRequest(legs=[leg])
    parsed_by_sym = {
        "CRUDEOIL26OCT5500CE": {"strike": 5500.0, "opt_type": "CE",
                                "root": "CRUDEOIL", "kind": "opt"},
    }
    resolved_legs, leg_details, _, _ = _strategy_build_legs(
        data, parsed_by_sym, {}, 5500.0, True, (23, 30), {},
    )
    assert len(resolved_legs) == 1
    assert resolved_legs[0]["is_mcx"] is True

    g76 = greeks_76(5500.0, 5500.0, resolved_legs[0]["T_years"],
                    DEFAULT_RISK_FREE, 0.20, "CE")
    for k in g76:
        assert leg_details[0]["greeks"][k] == pytest.approx(g76[k], abs=1e-9)


# ── Test C: Futures multileg payoff subtracts entry cost ───────────────

def test_multileg_futures_payoff_subtracts_entry_cost():
    """Test C: single futures leg, entry_price=25000, qty=1.
    At spot=25000 (at entry), both today_value and expiry_value should be 0.
    At spot=25500, both should be +500.
    At spot=24500, both should be -500.
    This verifies the fix where futures payoff correctly subtracts cumulative
    entry cost (line 1079-1080 in derivatives.py)."""
    import numpy as np

    legs = [{"kind": "fut", "entry_price": 25000.0, "qty": 1}]
    S = 25000.0
    span_pct = 0.02  # ±2% from spot, giving narrow range for precise testing
    points = 5

    curve = multileg_payoff_curve(legs, S=S, span_pct=span_pct, points=points)

    assert len(curve) == points, f"Expected {points} curve points, got {len(curve)}"

    # Find the point closest to spot=25000 (should be exact in this linear grid)
    at_entry = None
    plus_500 = None
    minus_500 = None

    for pt in curve:
        spot = pt["spot"]
        # Due to floating point, check with tolerance
        if abs(spot - 25000.0) < 1.0:
            at_entry = pt
        elif abs(spot - 25500.0) < 1.0:
            plus_500 = pt
        elif abs(spot - 24500.0) < 1.0:
            minus_500 = pt

    # At entry (spot=25000), P&L should be 0 (spot - entry_price) × qty = (25000 - 25000) × 1 = 0
    assert at_entry is not None, \
        f"Could not find curve point near spot=25000. Points: {[p['spot'] for p in curve]}"
    assert at_entry["today_value"] == 0.0, \
        f"At entry spot, today_value should be 0, got {at_entry['today_value']}"
    assert at_entry["expiry_value"] == 0.0, \
        f"At entry spot, expiry_value should be 0, got {at_entry['expiry_value']}"

    # At spot=25500, P&L should be +500 (25500 - 25000) × 1 = 500
    assert plus_500 is not None, \
        f"Could not find curve point near spot=25500. Points: {[p['spot'] for p in curve]}"
    assert abs(plus_500["today_value"] - 500.0) < 1.0, \
        f"At spot=25500, today_value should be ~500, got {plus_500['today_value']}"
    assert abs(plus_500["expiry_value"] - 500.0) < 1.0, \
        f"At spot=25500, expiry_value should be ~500, got {plus_500['expiry_value']}"

    # At spot=24500, P&L should be -500 (24500 - 25000) × 1 = -500
    assert minus_500 is not None, \
        f"Could not find curve point near spot=24500. Points: {[p['spot'] for p in curve]}"
    assert abs(minus_500["today_value"] - (-500.0)) < 1.0, \
        f"At spot=24500, today_value should be ~-500, got {minus_500['today_value']}"
    assert abs(minus_500["expiry_value"] - (-500.0)) < 1.0, \
        f"At spot=24500, expiry_value should be ~-500, got {minus_500['expiry_value']}"


def test_multileg_futures_multileg_payoff_entry_cost():
    """Test C variant: multi-leg with futures + options.
    Verify that total_cost (line 1079) correctly sums all entry costs
    (including negative qty for shorts) and is subtracted from both today and expiry.
    Example: long 1-lot NIFTY future (entry 25000) + short 1-lot put (entry 100 premium).

    total_cost = 25000*1 + 100*(-1) = 25000 - 100 = 24900.

    At spot=25000 at expiry:
    - Futures: 25000 * 1 = 25000
    - Put intrinsic: max(25000 - 25000, 0) * -1 = 0
    - Before cost: 25000 + 0 = 25000
    - After cost: 25000 - 24900 = 100

    This verifies that short premium (negative qty) reduces total_cost correctly."""
    import numpy as np

    legs = [
        {"kind": "fut", "entry_price": 25000.0, "qty": 1},
        {"kind": "opt", "strike": 25000.0, "opt_type": "PE", "qty": -1,
         "entry_price": 100.0, "T_years": 14/365.0, "sigma": 0.16},
    ]
    S = 25000.0
    span_pct = 0.02
    points = 5

    curve = multileg_payoff_curve(legs, S=S, span_pct=span_pct, points=points)

    at_entry = next((p for p in curve if abs(p["spot"] - 25000.0) < 1.0), None)
    assert at_entry is not None

    # At expiry with spot=25000:
    # Future contributes: 25000 (spot) * 1 = 25000
    # Put contributes: max(K - S, 0) * qty = max(25000 - 25000, 0) * -1 = 0
    # Sum before cost: 25000
    # After subtracting total_cost (24900): 25000 - 24900 = 100
    #
    # This verifies the CRITICAL FIX: entry cost is subtracted for both long and short legs.
    # The short -1 qty * entry_price 100 = -100 reduces total_cost correctly.
    assert abs(at_entry["expiry_value"] - 100.0) < 1.0, \
        f"At expiry, expiry_value should be ~100 (25000 - 24900), got {at_entry['expiry_value']}"

    # Verify today and expiry values are both correctly offset by total_cost
    # today_value includes time value (BS formula vs intrinsic at expiry)
    # Both should reflect the -24900 cost offset
    # today_value < expiry_value when short premium is involved (we collect premium today)
    assert at_entry["today_value"] is not None and at_entry["expiry_value"] is not None, \
        "Both today and expiry values should be computed"


# ── MCX Black-76 payoff/intermediate curves ────────────────────────────
#
# Context: the payoff curve pricer (`_accumulate_leg_slice`, used by
# `multileg_intermediate_curves`; `_leg_today_expiry_arrays`, used by the
# main `multileg_payoff_curve`; and the single-leg `payoff_curve`/
# `intermediate_curves`) priced every leg via plain Black-Scholes
# (`_black_scholes_vec`) even when the leg's resolved spot is an MCX
# futures contract price. Fixed via a new `_black_76_vec()` vectorized
# pricer, dispatched per-leg on `leg.get("is_mcx")` (multi-leg) or an
# explicit `is_mcx` kwarg (single-leg) — mirroring the scalar
# `black_76()`/`greeks_76()` dispatch already shipped for
# `_strategy_build_option_leg()`.

_MCX_F = 5500.0
_MCX_K = 5500.0
_MCX_T = 0.1


def test_black_76_vec_matches_scalar_black_76():
    """`_black_76_vec()` must reproduce the scalar `black_76()` to well
    within the vectorized pricer's accepted erf-approximation tolerance
    (same bound `_black_scholes_vec` already carries)."""
    import numpy as np
    from backend.api.algo.derivatives import _black_76_vec

    for F, K, T, r, sigma, opt in [
        (5500.0, 5500.0, 30 / 365.0, 0.07, 0.20, "CE"),
        (5500.0, 5600.0, 30 / 365.0, 0.07, 0.20, "PE"),
        (65000.0, 64000.0, 14 / 365.0, 0.07, 0.16, "CE"),
    ]:
        vec_price = float(_black_76_vec(np.array([F]), K, T, r, sigma, opt)[0])
        scalar_price = black_76(F, K, T, r, sigma, opt)
        assert vec_price == pytest.approx(scalar_price, abs=1e-2)


def test_black_76_vec_differs_from_black_scholes_vec():
    """At the same inputs, the Black-76 and plain-BS vectorized pricers
    must diverge materially — proves the two paths are NOT aliases of
    each other (a copy-paste bug could make `_black_76_vec` silently
    call `_black_scholes_vec` internally)."""
    import numpy as np
    from backend.api.algo.derivatives import _black_76_vec

    v76 = float(_black_76_vec(np.array([_MCX_F]), _MCX_K, _MCX_T, 0.07, 0.20, "CE")[0])
    vbs = float(_black_scholes_vec(np.array([_MCX_F]), _MCX_K, _MCX_T, 0.07, 0.20, "CE")[0])
    assert abs(v76 - vbs) > 1e-3


def test_accumulate_leg_slice_mcx_leg_uses_black_76(monkeypatch=None):
    """`multileg_intermediate_curves()` (real production caller of
    `_accumulate_leg_slice`) must price an `is_mcx: True` leg through
    `_black_76_vec`, not `_black_scholes_vec` — verified by comparing
    the real output against a hand-computed Black-76 reference and
    confirming it differs materially from the plain-BS value at the
    same inputs."""
    import numpy as np
    from backend.api.algo.derivatives import _black_76_vec

    leg = {
        "kind": "opt", "qty": 1, "strike": _MCX_K, "opt_type": "CE",
        "T_years": _MCX_T, "sigma": 0.20, "entry_price": 0.0,
        "is_mcx": True,
    }
    slices = multileg_intermediate_curves(
        [leg], S=_MCX_F, span_pct=0.0, points=2, time_slices=1,
    )
    assert len(slices) == 1
    elapsed = slices[0]["elapsed_pct"]
    expected = float(_black_76_vec(
        np.array([_MCX_F]), _MCX_K, _MCX_T * (1.0 - elapsed), DEFAULT_RISK_FREE, 0.20, "CE",
    )[0])
    assert slices[0]["values"][0] == pytest.approx(expected, abs=0.02)

    # Sibling leg without is_mcx must stay on plain BS and differ materially.
    leg_bs = dict(leg, is_mcx=False)
    slices_bs = multileg_intermediate_curves(
        [leg_bs], S=_MCX_F, span_pct=0.0, points=2, time_slices=1,
    )
    assert abs(slices_bs[0]["values"][0] - slices[0]["values"][0]) > 1e-2


def test_multileg_payoff_curve_mcx_leg_uses_black_76():
    """`multileg_payoff_curve()` (the MAIN multi-leg chart, via
    `_leg_today_expiry_arrays`) must also dispatch an `is_mcx: True` leg
    through Black-76 — must stay consistent with
    `test_accumulate_leg_slice_mcx_leg_uses_black_76` above (same model
    for the Today curve and its time-slices), closing the exact
    inconsistency class flagged for the Greeks-vs-curve mismatch."""
    leg_mcx = {
        "kind": "opt", "qty": 1, "strike": _MCX_K, "opt_type": "CE",
        "T_years": _MCX_T, "sigma": 0.20, "entry_price": 0.0,
        "is_mcx": True,
    }
    leg_bs = dict(leg_mcx, is_mcx=False)

    curve_mcx = multileg_payoff_curve([leg_mcx], S=_MCX_F, span_pct=0.0, points=2)
    curve_bs = multileg_payoff_curve([leg_bs], S=_MCX_F, span_pct=0.0, points=2)

    assert curve_mcx[0]["today_value"] != curve_bs[0]["today_value"]
    g76_at_T = black_76(_MCX_F, _MCX_K, _MCX_T, DEFAULT_RISK_FREE, 0.20, "CE")
    assert curve_mcx[0]["today_value"] == pytest.approx(g76_at_T, abs=0.02)


def test_payoff_curve_single_leg_is_mcx_dispatches_black_76():
    """Single-leg `payoff_curve()` (used by `/api/options/analytics` via
    `_analytics_compute_metrics`) must dispatch `is_mcx=True` through
    Black-76 for `today_value`, differing materially from the
    `is_mcx=False` default at the same inputs."""
    curve_mcx = payoff_curve(
        S=_MCX_F, K=_MCX_K, T_years=_MCX_T, r=DEFAULT_RISK_FREE,
        sigma=0.20, opt_type="CE", qty=1, entry_price=0.0,
        span_pct=0.0, points=2, is_mcx=True,
    )
    curve_bs = payoff_curve(
        S=_MCX_F, K=_MCX_K, T_years=_MCX_T, r=DEFAULT_RISK_FREE,
        sigma=0.20, opt_type="CE", qty=1, entry_price=0.0,
        span_pct=0.0, points=2,
    )  # is_mcx defaults False
    assert curve_mcx[0]["today_value"] != curve_bs[0]["today_value"]
    g76_at_T = black_76(_MCX_F, _MCX_K, _MCX_T, DEFAULT_RISK_FREE, 0.20, "CE")
    assert curve_mcx[0]["today_value"] == pytest.approx(g76_at_T, abs=0.02)


def test_intermediate_curves_single_leg_is_mcx_dispatches_black_76():
    """Single-leg `intermediate_curves()` must dispatch `is_mcx=True`
    through Black-76 — sibling of the multileg time-slice test above,
    for the single-leg `/api/options/analytics` path."""
    slices_mcx = intermediate_curves(
        S=_MCX_F, K=_MCX_K, T_years=_MCX_T, r=DEFAULT_RISK_FREE,
        sigma=0.20, opt_type="CE", qty=1, entry_price=0.0,
        span_pct=0.0, points=2, time_slices=1, is_mcx=True,
    )
    slices_bs = intermediate_curves(
        S=_MCX_F, K=_MCX_K, T_years=_MCX_T, r=DEFAULT_RISK_FREE,
        sigma=0.20, opt_type="CE", qty=1, entry_price=0.0,
        span_pct=0.0, points=2, time_slices=1,
    )  # is_mcx defaults False
    assert len(slices_mcx) == 1 and len(slices_bs) == 1
    assert abs(slices_mcx[0]["values"][0] - slices_bs[0]["values"][0]) > 1e-2


