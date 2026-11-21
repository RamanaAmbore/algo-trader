"""
test_options_t0_expiry_cache_bypass.py — a leg at/past its own expiry
(`eval_T <= 0`) must never read or write the Phase-4 leg-curve cache
(`_LEG_CURVE_CACHE`).

Root cause (live-audit confirmed): `_strategy_compute_curves`'s leg-curve
cache stores `expiry_value` normalized by spot RATIO and remaps it onto a
new absolute spot on every cache hit. That remap is only a reasonable
approximation away from expiry, where extrinsic+intrinsic value scales
smoothly with spot. At `eval_T <= 0` (the nearest leg has reached its own
expiry), `expiry_value` collapses to per-leg intrinsic
(`max(S-K,0)` / `max(K-S,0)`), which is NOT ratio-invariant with absolute
spot. Worse, every cache-key input (T_years, sigma, span) also freezes at
T=0, so the cache key is identical on every poll and the entry's sliding
TTL never naturally expires — the Exp P&L dotted curve could silently
freeze at whatever spot was current the first time the leg crossed T=0.

Fix: `_strategy_compute_curves` now takes an `eval_T <= 0` branch that
bypasses the cache entirely (no get, no put) and always returns the
curve + slices + extremes computed fresh, at the CURRENT spot, on every
call.
"""

from __future__ import annotations

import backend.api.routes.options as _options_mod
from backend.api.routes.options import (
    _leg_curve_cache_get,
    _leg_curve_cache_key,
    _leg_curve_cache_put,
    _strategy_compute_curves,
)
from backend.api.algo.derivatives import multileg_payoff_curve


def _clear_leg_curve_cache() -> None:
    with _options_mod._LEG_CURVE_CACHE_LOCK:
        _options_mod._LEG_CURVE_CACHE.clear()


def _single_ce_leg(T_years: float, strike: float = 24500.0, qty: int = 50) -> list[dict]:
    return [
        {"kind": "opt", "strike": strike, "opt_type": "CE", "qty": qty,
         "entry_price": 0.0, "T_years": T_years, "sigma": 0.16,
         "scale_ratio": 1.0},
    ]


def _single_pe_leg(T_years: float, strike: float = 24500.0, qty: int = 50) -> list[dict]:
    return [
        {"kind": "opt", "strike": strike, "opt_type": "PE", "qty": qty,
         "entry_price": 0.0, "T_years": T_years, "sigma": 0.16,
         "scale_ratio": 1.0},
    ]


# ── Core bug fix: eval_T<=0 reads fresh, never the ratio-remapped stale cache ──


def test_t0_bypasses_stale_cache_and_returns_fresh_intrinsic_at_new_spot():
    """
    A leg at its own expiry (eval_T=0.0) must price expiry_value fresh at
    whatever spot is current on EVERY call — even when a stale cache entry
    already exists under the exact key `_strategy_compute_curves` would
    otherwise look up.
    """
    _clear_leg_curve_cache()
    legs = _single_ce_leg(T_years=0.0, strike=24500.0, qty=50)
    span, pts, n_slices, span_sigmas = 0.10, 21, 0, 3.0

    S1 = 24500.0
    S2 = 24900.0   # genuinely different spot — would move intrinsic materially

    # Manually seed a stale cache entry at S1, under the SAME key the
    # pre-fix code would have computed and looked up.
    stale_key = _leg_curve_cache_key(legs, span, span_sigmas, pts, n_slices)
    curve_s1 = multileg_payoff_curve(legs, S=S1, span_pct=span, points=pts, eval_T=0.0)
    stale_payload = {
        "expiry_values_norm": [pt["expiry_value"] for pt in curve_s1],
        "x_ratios":           [round(pt["spot"] / S1, 10) for pt in curve_s1],
        "slices_norm":        [],
        "max_profit":         999999.0,   # deliberately wrong sentinel
        "max_loss":           -999999.0,  # deliberately wrong sentinel
        "rr_ratio":           None,
    }
    _leg_curve_cache_put(stale_key, stale_payload)
    assert _leg_curve_cache_get(stale_key) is not None, "stale entry must be seeded"

    # Call the real function at S2, eval_T=0.0 (at/past expiry).
    curve, slices, max_p, max_l, agg_rr = _strategy_compute_curves(
        legs, S2, span, pts, n_slices, 0.0, span_sigmas,
    )

    # Expected: fresh intrinsic computed directly at S2 — NOT the S1-era
    # cached (and deliberately-sentinel-poisoned) values.
    fresh_curve_s2 = multileg_payoff_curve(legs, S=S2, span_pct=span, points=pts, eval_T=0.0)
    fresh_expiry = [pt["expiry_value"] for pt in fresh_curve_s2]
    got_expiry = [pt["expiry_value"] for pt in curve]

    assert got_expiry == fresh_expiry, (
        "eval_T<=0 must recompute expiry_value fresh at the current spot, "
        "not reuse a ratio-remapped stale cache entry"
    )
    # Prove it's genuinely different from the (poisoned) stale cache values,
    # not coincidentally identical.
    assert got_expiry != stale_payload["expiry_values_norm"], (
        "fresh T=0 expiry values must differ from the stale S1-era cached values"
    )
    # max_profit/max_loss must also be freshly derived, not the sentinel
    # values planted in the stale cache entry.
    assert max_p != 999999.0 and max_l != -999999.0, (
        "max_profit/max_loss must be recomputed fresh, not read from the "
        "stale cache entry, when eval_T<=0"
    )


def test_t0_bypass_also_applies_to_put_leg():
    """Same bypass for a put leg — intrinsic max(K-S,0) is not ratio-invariant
    either."""
    _clear_leg_curve_cache()
    legs = _single_pe_leg(T_years=0.0, strike=24500.0, qty=50)
    span, pts, n_slices, span_sigmas = 0.10, 21, 0, 3.0

    S1, S2 = 24500.0, 24100.0

    stale_key = _leg_curve_cache_key(legs, span, span_sigmas, pts, n_slices)
    curve_s1 = multileg_payoff_curve(legs, S=S1, span_pct=span, points=pts, eval_T=0.0)
    _leg_curve_cache_put(stale_key, {
        "expiry_values_norm": [pt["expiry_value"] for pt in curve_s1],
        "x_ratios":           [round(pt["spot"] / S1, 10) for pt in curve_s1],
        "slices_norm":        [],
        "max_profit":         0.0,
        "max_loss":           0.0,
        "rr_ratio":           None,
    })

    curve, _slices, _mp, _ml, _rr = _strategy_compute_curves(
        legs, S2, span, pts, n_slices, 0.0, span_sigmas,
    )
    fresh_curve_s2 = multileg_payoff_curve(legs, S=S2, span_pct=span, points=pts, eval_T=0.0)
    assert [p["expiry_value"] for p in curve] == [p["expiry_value"] for p in fresh_curve_s2]


def test_t0_never_writes_to_cache():
    """eval_T<=0 must never create a cache entry — no get, no put — so the
    sliding TTL described in the bug report can never be pushed forward by
    this path."""
    _clear_leg_curve_cache()
    legs = _single_ce_leg(T_years=0.0)
    span, pts, n_slices, span_sigmas = 0.10, 21, 0, 3.0

    with _options_mod._LEG_CURVE_CACHE_LOCK:
        assert len(_options_mod._LEG_CURVE_CACHE) == 0

    _strategy_compute_curves(legs, 24500.0, span, pts, n_slices, 0.0, span_sigmas)

    with _options_mod._LEG_CURVE_CACHE_LOCK:
        assert len(_options_mod._LEG_CURVE_CACHE) == 0, (
            "eval_T<=0 must not write any entry into _LEG_CURVE_CACHE"
        )


def test_t0_does_not_disturb_an_unrelated_preexisting_entry():
    """A stale entry seeded under the T=0 key must be left completely
    untouched (no refresh of its sliding-TTL timestamp) by a bypass call —
    proven by confirming the exact same payload object round-trips."""
    _clear_leg_curve_cache()
    legs = _single_ce_leg(T_years=0.0)
    span, pts, n_slices, span_sigmas = 0.10, 21, 0, 3.0
    key = _leg_curve_cache_key(legs, span, span_sigmas, pts, n_slices)
    payload = {
        "expiry_values_norm": [1.0, 2.0],
        "x_ratios":           [0.9, 1.1],
        "slices_norm":        [],
        "max_profit":         42.0,
        "max_loss":           -42.0,
        "rr_ratio":           1.0,
    }
    _leg_curve_cache_put(key, payload)

    _strategy_compute_curves(legs, 24500.0, span, pts, n_slices, 0.0, span_sigmas)

    with _options_mod._LEG_CURVE_CACHE_LOCK:
        stored = _options_mod._LEG_CURVE_CACHE[key]
    assert stored[1] == payload, (
        "an eval_T<=0 call must not mutate an existing cache entry under its key"
    )


# ── Regression: T>0 cache-hit behavior stays exactly as before ────────────


def test_t_positive_cache_hit_still_remaps_expiry_values():
    """For a leg still away from expiry (eval_T > 0), a cache hit must still
    reuse the stale, ratio-remapped expiry_value — unchanged pre-existing
    behavior (and not a regression introduced by the T=0 bypass branch)."""
    _clear_leg_curve_cache()
    T = 14 / 365.0
    legs = [
        {"kind": "opt", "strike": 24500, "opt_type": "CE", "qty": 50,
         "entry_price": 120.0, "T_years": T, "sigma": 0.16, "scale_ratio": 1.0},
        {"kind": "opt", "strike": 24700, "opt_type": "CE", "qty": -50,
         "entry_price": 35.0, "T_years": T, "sigma": 0.18, "scale_ratio": 1.0},
    ]
    span, pts, n_slices, span_sigmas = 0.10, 51, 0, 3.0
    S1, S2 = 24500.0, 24750.0

    from backend.api.algo.derivatives import (
        multileg_extremes,
        multileg_intermediate_curves,
        risk_reward_ratio,
    )

    curve_s1 = multileg_payoff_curve(legs, S=S1, span_pct=span, points=pts)
    slices_s1 = multileg_intermediate_curves(
        legs, S=S1, span_pct=span, points=pts, time_slices=n_slices,
    )
    max_p_s1, max_l_s1 = multileg_extremes(curve_s1)
    rr_s1 = risk_reward_ratio(max_p_s1, max_l_s1)
    key = _leg_curve_cache_key(legs, span, span_sigmas, pts, n_slices)
    _leg_curve_cache_put(key, {
        "expiry_values_norm": [pt["expiry_value"] for pt in curve_s1],
        "x_ratios":           [round(pt["spot"] / S1, 10) for pt in curve_s1],
        "slices_norm":        slices_s1,
        "max_profit":         max_p_s1,
        "max_loss":           max_l_s1,
        "rr_ratio":           rr_s1,
    })

    # eval_T here is T itself (both legs share the same expiry, > 0).
    curve, slices, max_p, max_l, agg_rr = _strategy_compute_curves(
        legs, S2, span, pts, n_slices, T, span_sigmas,
    )

    # The cache HIT must win: returned expiry_value equals the S1-era
    # cached values, not a fresh S2 compute.
    assert [p["expiry_value"] for p in curve] == stale_values_of(curve_s1)
    assert max_p == max_p_s1
    assert max_l == max_l_s1
    assert agg_rr == rr_s1


def stale_values_of(curve_s1: list[dict]) -> list[float]:
    return [pt["expiry_value"] for pt in curve_s1]
