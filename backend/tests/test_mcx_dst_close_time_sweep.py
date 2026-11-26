"""Regression tests for the 2026-11-26 sweep: five functions that
hardcoded a flat ``(23, 30)`` (MCX) / ``(15, 30)`` (everything else)
close-time literal, instead of routing through
`close_time_for_exchange()` (`date_time_utils.py`) — the market-calendar
SSOT. Found while investigating a live-reported "still getting ITM MCX
expiry alerts" concern; `scope_positions_expiring_today.py` was fixed
first (see `test_scope_positions_expiring_today.py`), then this sweep
found four more instances, all now routed through the same SSOT:

1. `derivatives.py:_close_time_for_row()` → `calibrate_iv_for_row()` /
   `reprice_row()`.
2. `agent_engine.py:_capture_opt_leg_baseline()`.
3. `grammar.py:_theo_reprice_opt_leg()`.
4. `actions_paper.py:_ap_dte()`.

Each test picks a winter-window (US standard time) instant where the
real MCX non-agri close is 23:55 IST, but the OLD flat hardcode would
have treated 23:30 as already-closed — 25 minutes early. 2026-11-27 is
confirmed winter (see `test_mcx_non_agri_close_time.py`).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

_WINTER_EXPIRY = date(2026, 11, 27)  # Friday — confirmed winter window
# 23:40 IST on expiry day = 18:10 UTC. Real close 23:55 IST → ~15 min
# of life left. The old flat 23:30 close would already read "expired".
_AT_2340_IST_UTC = datetime(2026, 11, 27, 18, 10, 0, tzinfo=timezone.utc)


def test_close_time_for_row_mcx_winter():
    from backend.api.algo.derivatives import _close_time_for_row

    row = {"exchange": "MCX"}
    assert _close_time_for_row(row, _WINTER_EXPIRY) == (23, 55)


def test_close_time_for_row_nfo_unaffected():
    from backend.api.algo.derivatives import _close_time_for_row

    row = {"exchange": "NFO"}
    assert _close_time_for_row(row, _WINTER_EXPIRY) == (15, 30)


def test_close_time_for_row_unknown_exchange_falls_back_to_1530():
    from backend.api.algo.derivatives import _close_time_for_row

    row = {"exchange": ""}
    assert _close_time_for_row(row, _WINTER_EXPIRY) == (15, 30)


def test_calibrate_iv_for_row_mcx_winter_still_has_time_value():
    # At 23:40 IST on the real winter-window expiry day, the real MCX
    # close (23:55) hasn't happened yet -- IV calibration must not
    # treat this as already-expired (T<=0 -> DEFAULT_IV fallback is
    # fine, but a genuine math domain error from a negative/zero T
    # feeding log() would be the symptom of the old flat-23:30 bug).
    from backend.api.algo.derivatives import calibrate_iv_for_row

    row = {"tradingsymbol": "GOLD26NOV123000PE"}
    sigma = calibrate_iv_for_row(
        row, spot=2400.0, ref_now=_AT_2340_IST_UTC,
    )
    assert sigma is not None and sigma > 0


def test_reprice_row_mcx_winter_still_has_time_value():
    from backend.api.algo.derivatives import reprice_row

    row = {"tradingsymbol": "GOLD26NOV123000PE"}
    price = reprice_row(
        row, spot=2400.0, sigma=0.2, ref_now=_AT_2340_IST_UTC,
    )
    assert price is not None and price >= 0


def test_capture_opt_leg_baseline_mcx_winter_not_treated_as_expired():
    from backend.api.algo.agent_engine import _capture_opt_leg_baseline
    from backend.api.algo.derivatives import black_76, days_to_expiry, DEFAULT_RISK_FREE

    strike, S_base = 123000.0, 123050.0
    # T_base computed against the REAL (fixed) close time, used to
    # forward-price a self-consistent `ltp_base` via black_76 at a
    # known sigma -- so the reconciliation check inside
    # `_capture_opt_leg_baseline` succeeds on its own terms, and this
    # test isolates the one thing it's meant to check: that the
    # function doesn't short-circuit on a degenerate T<=0.
    t_years = days_to_expiry(
        _WINTER_EXPIRY, ref=_AT_2340_IST_UTC, close_time=(23, 55),
    ) / 365.0
    assert t_years > 0, "sanity check on the test's own fixture"
    ltp_base = black_76(S_base, strike, t_years, DEFAULT_RISK_FREE, 0.25, "PE")

    parsed = {"strike": strike, "opt_type": "PE", "expiry": _WINTER_EXPIRY}
    baseline = _capture_opt_leg_baseline(
        parsed, S_base=S_base, ltp_base=ltp_base, qty=-100,
        is_mcx=True, now=_AT_2340_IST_UTC,
    )
    assert baseline is not None, (
        "A degenerate T<=0 (the old flat-23:30 bug) makes this return "
        "None 25 minutes before the real MCX winter close."
    )


def test_theo_reprice_opt_leg_mcx_winter_not_treated_as_expired():
    from backend.api.algo.grammar import _theo_reprice_opt_leg

    b = {
        "qty": -100, "K": 123000.0, "sigma_base": 0.2, "ltp_base": 50.0,
        "opt_type": "PE", "is_mcx": True, "expiry": _WINTER_EXPIRY,
    }
    price = _theo_reprice_opt_leg(b, S_now=2400.0, now=_AT_2340_IST_UTC)
    assert price is not None


def test_ap_dte_mcx_winter_close():
    from backend.api.algo.actions_paper import _ap_dte

    d = _ap_dte(_WINTER_EXPIRY, "MCX", _AT_2340_IST_UTC)
    assert d is not None and d > 0.0


def test_ap_dte_nfo_unaffected():
    from backend.api.algo.actions_paper import _ap_dte

    d = _ap_dte(_WINTER_EXPIRY, "NFO", _AT_2340_IST_UTC)
    assert d is not None and d == pytest.approx(0.0, abs=1e-6)
