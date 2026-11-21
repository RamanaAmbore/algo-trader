"""
test_options_mcx_dst_close_time_wiring.py — covers Fix 2's wiring inside
`backend/api/routes/options.py`: both of the file's MCX close-time
branches must resolve via `mcx_non_agri_close_time()` instead of the
old hardcoded `(23, 30)` literal.

Two layers:
  1. Source-grep — no `(23, 30)` literal survives in this file, and
     both call sites reference `mcx_non_agri_close_time`.
  2. Behavioral — feeding the resolved close_time through the same
     helpers `_strategy_analytics_impl`/`analytics()` delegate to
     (`_strategy_option_T_range`, `_strategy_build_legs`) must NOT
     collapse a genuinely still-open MCX leg's T_years to 0 at 23:37 IST
     on the real 2026-11-17 winter-window date — the exact operator-
     reported incident.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import backend.api.routes.options as _options_mod
from backend.api.routes.options import (
    StrategyLeg,
    StrategyRequest,
    _strategy_build_legs,
    _strategy_option_T_range,
    mcx_non_agri_close_time,
)

_IST = ZoneInfo("Asia/Kolkata")


def _frozen_datetime(fixed_now: datetime):
    """Standard subclass-based "freeze time" shim — real `datetime`
    arithmetic/construction/`isinstance` checks all keep working
    (unlike replacing the module-level `datetime` name with a bare
    MagicMock, which breaks `isinstance(expiry, datetime)` inside
    `days_to_expiry` outright)."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)
    return _Frozen


# ── 1. Source-grep — no stray hardcoded tuple remains ──────────────────


def test_no_hardcoded_23_30_literal_remains_in_options_py():
    src = inspect.getsource(_options_mod)
    assert "(23, 30)" not in src and "(23,30)" not in src, (
        "options.py must resolve the MCX close time via "
        "mcx_non_agri_close_time(), never a hardcoded (23, 30) literal"
    )


def test_mcx_non_agri_close_time_imported_and_used():
    assert _options_mod.mcx_non_agri_close_time is mcx_non_agri_close_time
    # `analytics` is a Litestar `@get(...)`-decorated handler object, not
    # a plain function — the raw function lives on its `.fn` attribute.
    analytics_handler = _options_mod.OptionsController.__dict__["analytics"]
    analytics_src = inspect.getsource(analytics_handler.fn)
    assert "mcx_non_agri_close_time" in analytics_src, (
        "/analytics must resolve its MCX close_time via mcx_non_agri_close_time()"
    )
    impl_src = inspect.getsource(
        _options_mod.OptionsController.__dict__["_strategy_analytics_impl"]
    )
    assert "mcx_non_agri_close_time" in impl_src, (
        "/strategy-analytics must resolve its MCX close_time via "
        "mcx_non_agri_close_time()"
    )


# ── 2. Behavioral — the real reported bug, now fixed ───────────────────


def _crudeoil_leg(expiry_iso: str) -> StrategyLeg:
    return StrategyLeg(symbol="CRUDEOIL26NOV9200PE", qty=-1, avg_cost=10.0,
                       ltp=12.0, iv=0.35, expiry=expiry_iso)


def _mcx_parsed(expiry_iso: str) -> dict:
    return {
        "kind": "opt", "root": "CRUDEOIL", "strike": 9200.0,
        "opt_type": "PE", "expiry": date.fromisoformat(expiry_iso),
    }


def test_strategy_option_t_range_stays_positive_at_23_37_winter_close():
    """Mirrors exactly what `_strategy_analytics_impl` does: resolve
    close_time via mcx_non_agri_close_time(nearest_expiry), then feed it
    into `_strategy_option_T_range`."""
    expiry_iso = "2026-11-17"
    data = StrategyRequest(legs=[_crudeoil_leg(expiry_iso)])
    parsed_by_sym = {"CRUDEOIL26NOV9200PE": _mcx_parsed(expiry_iso)}

    ct = mcx_non_agri_close_time(date.fromisoformat(expiry_iso))
    close_time = (ct.hour, ct.minute)
    assert close_time == (23, 55), "sanity: must resolve the real winter close"

    fixed_now = datetime(2026, 11, 17, 23, 37, tzinfo=_IST)
    with patch("backend.api.algo.derivatives.datetime", _frozen_datetime(fixed_now)):
        eval_T, T_yrs_shared = _strategy_option_T_range(data, parsed_by_sym, close_time)

    assert eval_T > 0.0, (
        "at 23:37 IST on the real 2026-11-17 winter close, eval_T must "
        "still be positive (18 minutes remain before the real 23:55 "
        "close) — this is the exact bug the operator reported"
    )
    assert T_yrs_shared > 0.0


def test_strategy_build_legs_does_not_price_leg_as_already_expired():
    """`_strategy_build_legs` (option-leg builder) must compute a
    positive T_years for the same scenario, so the resolved_leg's
    expiry_value pricing uses real time value rather than collapsing to
    bare intrinsic 7 minutes early."""
    expiry_iso = "2026-11-17"
    sym = "CRUDEOIL26NOV9200PE"
    data = StrategyRequest(legs=[_crudeoil_leg(expiry_iso)])
    parsed_by_sym = {sym: _mcx_parsed(expiry_iso)}
    quote_resp: dict = {}
    ct = mcx_non_agri_close_time(date.fromisoformat(expiry_iso))
    close_time = (ct.hour, ct.minute)

    fixed_now = datetime(2026, 11, 17, 23, 37, tzinfo=_IST)
    with patch("backend.api.algo.derivatives.datetime", _frozen_datetime(fixed_now)):
        resolved_legs, leg_details, _num, _den = _strategy_build_legs(
            data, parsed_by_sym, quote_resp, S=9150.0,
            _is_commodity=True, _close_time=close_time,
            _leg_scale_ratios={},
        )

    assert len(resolved_legs) == 1
    assert resolved_legs[0]["T_years"] > 0.0, (
        "the leg must still carry positive T_years at 23:37 IST on the "
        "real winter-window close date"
    )
