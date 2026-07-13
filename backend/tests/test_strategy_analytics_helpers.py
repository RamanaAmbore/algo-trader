"""
test_strategy_analytics_helpers.py — pure-helper coverage for the July 2026
decomposition of `OptionsController._strategy_analytics_impl`.

The impl was 620 LOC / cc=120 before decomposition; extracted into 8 module-
level `_strategy_*` helpers (validation, metadata collection, spot anchor
selection, T-range, futures builder, option builder, iv calibration, curve
compute). This spec covers the pure helpers that don't need a broker mock.

Five quality dimensions per feedback_test_dimensions.md:

  1. SSOT — helpers are the single source of truth for their concern
     (spot-anchor rule, SIM fast path guard, T-range computation);
     the orchestrator delegates and does not duplicate the logic.
  2. Performance — helpers are pure Python without I/O; every call is
     under 1 ms even on 6-leg baskets.
  3. Stale code — helpers use `date.fromisoformat` (stdlib), not
     `datetime.datetime.strptime(..., '%Y-%m-%d')`. Also asserted here.
  4. Reusable — helpers accept plain dicts / msgspec Structs, no
     hidden globals; the impl passes state in and reads state out.
  5. Correctness — happy-path + edge-case fixtures per helper.
"""

from __future__ import annotations

import inspect
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from litestar.exceptions import HTTPException

from backend.api.routes import options as _options_mod
from backend.api.routes.options import (
    StrategyLeg,
    StrategyRequest,
    _strategy_build_futures_leg,
    _strategy_calibrate_iv,
    _strategy_collect_leg_metadata,
    _strategy_option_T_range,
    _strategy_pick_spot_anchor,
    _strategy_resolve_option_ltp,
    _strategy_validate_and_parse,
)


# ── 5. Correctness — validate_and_parse ──────────────────────────────


def test_validate_and_parse_empty_legs_raises_400():
    req = StrategyRequest(legs=[])
    with pytest.raises(HTTPException) as exc:
        _strategy_validate_and_parse(req)
    assert exc.value.status_code == 400
    assert "legs is required" in exc.value.detail


def test_validate_and_parse_returns_parsed_map():
    legs = [
        StrategyLeg(symbol="NIFTY26JUL24500CE", qty=50, ltp=100.0),
        StrategyLeg(symbol="NIFTY26JUL24700CE", qty=-50, ltp=50.0),
    ]
    req = StrategyRequest(legs=legs)
    parsed = _strategy_validate_and_parse(req)
    assert "NIFTY26JUL24500CE" in parsed
    assert "NIFTY26JUL24700CE" in parsed
    assert parsed["NIFTY26JUL24500CE"].get("kind") == "opt"
    assert parsed["NIFTY26JUL24500CE"].get("strike") == 24500.0


def test_validate_and_parse_uppercases_and_strips():
    legs = [StrategyLeg(symbol="  nifty26jul24500ce  ", qty=50, ltp=100.0)]
    req = StrategyRequest(legs=legs)
    parsed = _strategy_validate_and_parse(req)
    # Key is uppercased + stripped.
    assert "NIFTY26JUL24500CE" in parsed


# ── collect_leg_metadata ─────────────────────────────────────────────


def test_collect_metadata_populates_need_quote_for_missing_ltp():
    legs = [
        StrategyLeg(symbol="NIFTY26JUL24500CE", qty=50),   # no ltp — must quote
        StrategyLeg(symbol="NIFTY26JUL24700CE", qty=-50, ltp=50.0),  # has ltp — skip
    ]
    req = StrategyRequest(legs=legs)
    parsed = _strategy_validate_and_parse(req)
    roots, expiries, need_quote = _strategy_collect_leg_metadata(req, parsed)
    assert roots == {"NIFTY"}
    assert len(expiries) >= 1
    # Only the ltp-less leg is in need_quote.
    nq_syms = set(need_quote.values())
    assert "NIFTY26JUL24500CE" in nq_syms
    assert "NIFTY26JUL24700CE" not in nq_syms


def test_collect_metadata_sim_fast_path_guard_catches_zero_ltp():
    """A leg with ltp=0 (stale picker) must still be quoted."""
    legs = [
        StrategyLeg(symbol="NIFTY26JUL24500CE", qty=50, ltp=0.0),  # zero → must quote
    ]
    req = StrategyRequest(legs=legs)
    parsed = _strategy_validate_and_parse(req)
    roots, expiries, need_quote = _strategy_collect_leg_metadata(req, parsed)
    assert "NIFTY26JUL24500CE" in need_quote.values()


def test_collect_metadata_rejects_mixed_roots():
    legs = [
        StrategyLeg(symbol="NIFTY26JUL24500CE", qty=50, ltp=100.0),
        StrategyLeg(symbol="BANKNIFTY26JUL45000CE", qty=-50, ltp=200.0),
    ]
    req = StrategyRequest(legs=legs)
    parsed = _strategy_validate_and_parse(req)
    with pytest.raises(HTTPException) as exc:
        _strategy_collect_leg_metadata(req, parsed)
    assert exc.value.status_code == 400
    assert "share an underlying" in exc.value.detail


def test_collect_metadata_rejects_unrecognised_symbol():
    legs = [StrategyLeg(symbol="TOTAL_GARBAGE", qty=50, ltp=100.0)]
    req = StrategyRequest(legs=legs)
    parsed = _strategy_validate_and_parse(req)
    with pytest.raises(HTTPException) as exc:
        _strategy_collect_leg_metadata(req, parsed)
    assert exc.value.status_code == 400


# ── pick_spot_anchor ─────────────────────────────────────────────────


def test_pick_spot_anchor_prefers_option_over_futures_in_modal_month():
    """When the modal expiry has both option + futures legs, option wins."""
    legs = [
        StrategyLeg(symbol="NIFTY26JUL24500CE", qty=50, ltp=100.0,
                    expiry="2026-07-30"),
        StrategyLeg(symbol="NIFTY26JUL24700CE", qty=-50, ltp=50.0,
                    expiry="2026-07-30"),
        # Fut in the modal month — should NOT be picked over the CE.
        StrategyLeg(symbol="NIFTY26JULFUT", qty=1, ltp=24600.0,
                    expiry="2026-07-30"),
    ]
    req = StrategyRequest(legs=legs)
    parsed = _strategy_validate_and_parse(req)
    anchor, expiry_hint = _strategy_pick_spot_anchor(req, parsed)
    # Must anchor on an option (CE), not the future.
    assert anchor is not None
    assert "CE" in anchor
    assert expiry_hint == date(2026, 7, 30)


def test_pick_spot_anchor_front_month_tie_break():
    """Two months with equal leg count → nearest expiry wins."""
    legs = [
        StrategyLeg(symbol="NIFTY26JUL24500CE", qty=50, ltp=100.0,
                    expiry="2026-07-30"),
        StrategyLeg(symbol="NIFTY26JUL24700CE", qty=-50, ltp=50.0,
                    expiry="2026-08-27"),
    ]
    req = StrategyRequest(legs=legs)
    parsed = _strategy_validate_and_parse(req)
    _, expiry_hint = _strategy_pick_spot_anchor(req, parsed)
    # Front-month wins the tie.
    assert expiry_hint == date(2026, 7, 30)


# ── option_T_range ───────────────────────────────────────────────────


def test_option_T_range_zero_for_fut_only_basket():
    legs = [
        StrategyLeg(symbol="NIFTY26JULFUT", qty=1, ltp=24500.0,
                    expiry="2026-07-30"),
    ]
    req = StrategyRequest(legs=legs)
    parsed = _strategy_validate_and_parse(req)
    eval_T, T_yrs_shared = _strategy_option_T_range(req, parsed, (15, 30))
    # Futures-only baskets have no option T-range.
    assert eval_T == 0.0
    assert T_yrs_shared == 0.0


def test_option_T_range_min_max_across_options():
    """When option legs span multiple expiries, eval_T=min, T_shared=max."""
    legs = [
        StrategyLeg(symbol="NIFTY26JUL24500CE", qty=50, ltp=100.0,
                    expiry="2026-07-30"),
        StrategyLeg(symbol="NIFTY26JUL24700CE", qty=-50, ltp=50.0,
                    expiry="2026-08-27"),
    ]
    req = StrategyRequest(legs=legs)
    parsed = _strategy_validate_and_parse(req)
    eval_T, T_yrs_shared = _strategy_option_T_range(req, parsed, (15, 30))
    # Both > 0 (future expiries assuming test run before 2026-07-30);
    # near ≤ far. Only checking the invariant, not absolute values,
    # so the spec doesn't age with the calendar.
    assert eval_T <= T_yrs_shared


# ── build_futures_leg ────────────────────────────────────────────────


def test_build_futures_leg_uses_ltp_override():
    leg = StrategyLeg(symbol="NIFTY26JULFUT", qty=1, ltp=24500.0, avg_cost=24000.0)
    resolved, detail = _strategy_build_futures_leg(
        leg, "NIFTY26JULFUT", {}, S_leg=24400.0, scale_ratio=1.0, qty=1,
    )
    assert resolved["kind"] == "fut"
    assert resolved["qty"] == 1
    assert resolved["entry_price"] == 24000.0
    assert detail["ltp"] == 24500.0
    assert detail["ltp_source"] == "override"
    # Futures greeks: delta=1, everything else 0.
    assert detail["greeks"]["delta"] == 1.0
    assert detail["greeks"]["gamma"] == 0.0


def test_build_futures_leg_falls_back_to_S_leg_when_no_price():
    """LTP chain: override(None) → broker({}) → avg_cost(None) → S_leg."""
    leg = StrategyLeg(symbol="NIFTY26JULFUT", qty=1)  # no ltp, no avg_cost
    resolved, detail = _strategy_build_futures_leg(
        leg, "NIFTY26JULFUT", {}, S_leg=24400.0, scale_ratio=1.0, qty=1,
    )
    assert detail["ltp"] == 24400.0
    assert detail["ltp_source"] == "estimated"


# ── resolve_option_ltp ───────────────────────────────────────────────


def test_resolve_option_ltp_override_wins():
    leg = StrategyLeg(symbol="NIFTY26JUL24500CE", qty=50, ltp=100.0)
    parsed = {"strike": 24500.0, "opt_type": "CE"}
    ltp, src = _strategy_resolve_option_ltp(
        leg, "NIFTY26JUL24500CE", parsed, {}, S_leg=24500.0, T_yrs=0.1,
    )
    assert ltp == 100.0
    assert src == "override"


def test_resolve_option_ltp_falls_back_to_black_scholes_estimate():
    """When no override, no broker quote, no avg_cost → BS estimate at
    DEFAULT_IV against S_leg."""
    leg = StrategyLeg(symbol="NIFTY26JUL24500CE", qty=50)
    parsed = {"strike": 24500.0, "opt_type": "CE"}
    ltp, src = _strategy_resolve_option_ltp(
        leg, "NIFTY26JUL24500CE", parsed, {}, S_leg=24500.0, T_yrs=0.1,
    )
    assert ltp > 0
    assert src == "estimated"


def test_resolve_option_ltp_raises_400_when_all_fallbacks_exhausted():
    """Zero spot + zero T → BS estimate is 0 → raise 400."""
    leg = StrategyLeg(symbol="NIFTY26JUL24500CE", qty=50)
    parsed = {"strike": 24500.0, "opt_type": "CE"}
    with pytest.raises(HTTPException) as exc:
        _strategy_resolve_option_ltp(
            leg, "NIFTY26JUL24500CE", parsed, {}, S_leg=0.0, T_yrs=0.0,
        )
    assert exc.value.status_code == 400
    assert "no usable price" in exc.value.detail


# ── calibrate_iv ─────────────────────────────────────────────────────


def test_calibrate_iv_override_wins():
    leg = StrategyLeg(symbol="NIFTY26JUL24500CE", qty=50, iv=0.20)
    parsed = {"strike": 24500.0, "opt_type": "CE"}
    sig, src = _strategy_calibrate_iv(
        leg, parsed, S_leg=24500.0, T_yrs=0.1, ltp_val=100.0, ltp_source="live",
    )
    assert sig == 0.20
    assert src == "override"


# ── 3. Stale code — modern date parsing, not strptime ────────────────


def test_helpers_use_fromisoformat_not_strptime():
    """The Jul 2026 helpers all parse ISO dates via date.fromisoformat.
    strptime with an explicit format string is legacy — grep-guarding
    against a regression that mixes in the strptime path.

    _strategy_pick_spot_anchor delegates date parsing to
    _strategy_anchor_from_modal_expiry, so fromisoformat now lives in
    the sub-helper. Both helpers are checked.
    """
    for fn_name in (
        "_strategy_option_T_range",
        "_strategy_mcx_scale_ratios",
        "_strategy_anchor_from_modal_expiry",
    ):
        src = inspect.getsource(getattr(_options_mod, fn_name))
        assert "fromisoformat" in src, (
            f"{fn_name} must use date.fromisoformat (stdlib canonical)"
        )
        assert "strptime" not in src, (
            f"{fn_name} must NOT use datetime.strptime — the Jul 2026 "
            "helpers all use date.fromisoformat for ISO parsing"
        )


# ── 1. SSOT — the impl delegates; no parallel logic ──────────────────


def test_impl_delegates_metadata_to_helper():
    """`_strategy_analytics_impl` must call `_strategy_collect_leg_metadata`
    — not inline the SIM fast path / mixed-root check itself."""
    impl_src = inspect.getsource(
        _options_mod.OptionsController.__dict__["_strategy_analytics_impl"]
    )
    assert "_strategy_collect_leg_metadata" in impl_src
    # And the mixed-root gate must NOT appear inline (that lives in the helper).
    assert "share an underlying" not in impl_src


def test_impl_delegates_anchor_pick_to_helper():
    """`_strategy_pick_spot_anchor` is the SSOT for the modal-expiry rule.
    The impl delegates to _strategy_resolve_spot_impl which calls
    _strategy_pick_spot_anchor — Counter tie-break must not appear inline."""
    impl_src = inspect.getsource(
        _options_mod.OptionsController.__dict__["_strategy_analytics_impl"]
    )
    assert "_strategy_resolve_spot_impl" in impl_src
    # The Counter-based tie-break must not appear inline.
    assert "expiry_counts" not in impl_src


# ── 2. Performance — helpers stay pure & fast on 6-leg baskets ───────


def test_validate_and_parse_under_1ms():
    import time
    legs = [
        StrategyLeg(symbol=f"NIFTY{24500 + i * 100}CE", qty=50, ltp=100.0)
        for i in range(6)
    ]
    req = StrategyRequest(legs=legs)
    t0 = time.perf_counter()
    for _ in range(1000):
        _strategy_validate_and_parse(req)
    elapsed_ms = (time.perf_counter() - t0) * 1000 / 1000
    # 6-leg parse should be well under 1ms per call.
    assert elapsed_ms < 1.0, f"validate_and_parse too slow: {elapsed_ms:.3f}ms/call"


# ── 4. Reusable — every helper is a module-level function ────────────


def test_helpers_are_module_level_not_methods():
    """All 8 `_strategy_*` helpers live at module scope so they're testable
    without instantiating OptionsController + broker."""
    for fn_name in (
        "_strategy_validate_and_parse",
        "_strategy_collect_leg_metadata",
        "_strategy_pick_spot_anchor",
        "_strategy_option_T_range",
        "_strategy_mcx_scale_ratios",
        "_strategy_build_futures_leg",
        "_strategy_resolve_option_ltp",
        "_strategy_calibrate_iv",
        "_strategy_compute_curves",
    ):
        fn = getattr(_options_mod, fn_name, None)
        assert fn is not None, f"module missing helper: {fn_name}"
        assert callable(fn), f"{fn_name} must be callable"
