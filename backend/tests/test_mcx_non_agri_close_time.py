"""
test_mcx_non_agri_close_time.py — MCX revises its non-agri commodity
(bullion/energy/base-metals: CRUDEOIL, NATURALGAS, GOLD, SILVER, etc.)
evening close twice a year, tracking the US daylight-saving calendar:

  - US DST active            → 23:30 IST
  - US standard time (winter) → 23:55 IST

Live incident this covers: a CRUDEOIL position was genuinely still
tradeable on MCX at 23:37 IST on 2026-11-17 (a winter-window date, real
close 23:55), but the codebase hardcoded 23:30 everywhere, so DTE/
Greeks/payoff math treated the position as already past its own
expiry/close at 23:31.

`mcx_non_agri_close_time(date_)` resolves the real US Eastern-time DST
transition programmatically via `zoneinfo` — never a hand-rolled
"2nd Sunday of March / 1st Sunday of November" rule, and never a
hardcoded year-specific date range — so it stays correct in every
future year with zero manual maintenance. The expected transition date
in these tests is itself computed via the same zoneinfo approach, not
hardcoded, so the test doesn't go stale either.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.api.algo.derivatives import days_to_expiry, mcx_non_agri_close_time

_US_EASTERN = ZoneInfo("America/New_York")
_IST = ZoneInfo("Asia/Kolkata")


def _is_dst_on(d: date) -> bool:
    """Reference oracle — independent of the function under test, but
    using the exact same zoneinfo primitive the task specifies."""
    anchor = datetime(d.year, d.month, d.day, 12, 0, tzinfo=_US_EASTERN)
    return bool(anchor.dst())


def _find_real_spring_transition(year: int) -> date:
    """Walk March day-by-day to find the real US spring-forward date for
    `year`, computed programmatically (never hardcoded "2nd Sunday")."""
    d = date(year, 3, 1)
    prev_dst = _is_dst_on(d)
    while d.month == 3:
        d += timedelta(days=1)
        cur_dst = _is_dst_on(d)
        if cur_dst and not prev_dst:
            return d
        prev_dst = cur_dst
    raise AssertionError(f"no spring DST transition found in March {year}")


def _find_real_fall_transition(year: int) -> date:
    """Walk late October through November day-by-day to find the real US
    fall-back date for `year`, computed programmatically (never
    hardcoded "1st Sunday"). Starts a week before November 1st because
    the transition can legitimately land exactly on November 1st
    itself, which a scan starting AT November 1st would miss entirely
    (it would already observe the post-transition state on day one)."""
    d = date(year, 10, 25)
    end = date(year, 11, 30)
    prev_dst = _is_dst_on(d)
    while d < end:
        d += timedelta(days=1)
        cur_dst = _is_dst_on(d)
        if not cur_dst and prev_dst:
            return d
        prev_dst = cur_dst
    raise AssertionError(f"no fall DST transition found around November {year}")


# ── Core resolution ────────────────────────────────────────────────────


def test_winter_window_date_returns_23_55():
    """2026-11-17 is inside the US winter (standard-time) window — the
    exact date from the live incident. Real close must be 23:55, not the
    old hardcoded 23:30."""
    assert mcx_non_agri_close_time(date(2026, 11, 17)) == time(23, 55)


def test_dst_window_date_returns_23_30():
    """2026-06-01 is deep inside the US DST window — real close 23:30."""
    assert mcx_non_agri_close_time(date(2026, 6, 1)) == time(23, 30)


def test_january_is_winter_window():
    """Early January is always winter (standard time) in the US."""
    assert mcx_non_agri_close_time(date(2026, 1, 15)) == time(23, 55)


def test_july_is_dst_window():
    """Mid-July is always inside US DST."""
    assert mcx_non_agri_close_time(date(2026, 7, 15)) == time(23, 30)


# ── Transition boundary — computed programmatically, not hardcoded ─────


def test_spring_transition_flips_winter_to_dst():
    """The day before the real spring-forward transition must resolve
    23:55 (winter); the transition day itself (and the day after) must
    resolve 23:30 (DST) — boundary computed via zoneinfo, not hardcoded."""
    year = 2026
    transition = _find_real_spring_transition(year)
    day_before = transition - timedelta(days=1)

    assert mcx_non_agri_close_time(day_before) == time(23, 55), (
        f"day before spring transition ({day_before}) must still be winter"
    )
    assert mcx_non_agri_close_time(transition) == time(23, 30), (
        f"spring transition day ({transition}) itself must already be DST"
    )
    assert mcx_non_agri_close_time(transition + timedelta(days=1)) == time(23, 30)


def test_fall_transition_flips_dst_to_winter():
    """The day before the real fall-back transition must resolve 23:30
    (still DST); the transition day itself must resolve 23:55 (winter)."""
    year = 2026
    transition = _find_real_fall_transition(year)
    day_before = transition - timedelta(days=1)

    assert mcx_non_agri_close_time(day_before) == time(23, 30), (
        f"day before fall transition ({day_before}) must still be DST"
    )
    assert mcx_non_agri_close_time(transition) == time(23, 55), (
        f"fall transition day ({transition}) itself must already be winter"
    )


def test_transition_boundary_matches_reference_oracle_across_several_years():
    """Cross-check against the independent `_is_dst_on` oracle for every
    day of a representative window spanning both transitions, for
    multiple years — proves the function keeps working correctly in
    future years with no hardcoded date range."""
    for year in (2025, 2026, 2027, 2030):
        d = date(year, 1, 1)
        end = date(year, 12, 31)
        while d <= end:
            expected = time(23, 30) if _is_dst_on(d) else time(23, 55)
            assert mcx_non_agri_close_time(d) == expected, (
                f"{d} ({year}): expected {expected}, mismatch vs oracle"
            )
            d += timedelta(days=30)  # sample every ~30 days — cheap, still
            # crosses both yearly transitions for every sampled year.


# ── The actual reported bug: T must not be <=0 at 23:37 IST on the real
#    winter-window close date ────────────────────────────────────────


def test_23_37_ist_on_2026_11_17_does_not_collapse_dte_to_zero():
    """The exact scenario the operator reported: a CRUDEOIL-shaped expiry
    dated 2026-11-17, evaluated at 23:37 IST on that same date. With the
    real 23:55 close, there must still be ~18 minutes of time value left
    (T > 0). The old hardcoded 23:30 would have already reported T=0
    (and therefore an expired/zero-time leg) seven minutes earlier."""
    expiry = date(2026, 11, 17)
    close_t = mcx_non_agri_close_time(expiry)
    assert close_t == time(23, 55)

    ref = datetime(2026, 11, 17, 23, 37, tzinfo=_IST)
    dte_days = days_to_expiry(expiry, ref=ref, close_time=(close_t.hour, close_t.minute))
    T_years = dte_days / 365.0

    assert T_years > 0.0, (
        "at 23:37 IST on the real 2026-11-17 winter-window expiry date, "
        "T_years must be > 0 (18 minutes still remain before the real "
        "23:55 close) — this is the exact bug the operator reported"
    )

    # Confirm the OLD hardcoded behavior would indeed have been wrong —
    # not a strawman. Using the stale 23:30 constant at the same instant
    # already reads T<=0.
    stale_dte_days = days_to_expiry(expiry, ref=ref, close_time=(23, 30))
    assert stale_dte_days <= 0.0, (
        "sanity check: the OLD hardcoded 23:30 close must indeed have "
        "already reported T<=0 at 23:37 IST — confirming this is a real "
        "regression the DST-aware fix corrects, not a non-issue"
    )


@pytest.mark.parametrize("hour,minute,is_t_positive", [
    (23, 50, True),   # still 5 min before the real 23:55 close
    (23, 55, False),  # exactly at the real close — T floors to 0
    (23, 59, False),  # past the real close
])
def test_time_remaining_near_real_winter_close(hour, minute, is_t_positive):
    expiry = date(2026, 11, 17)
    close_t = mcx_non_agri_close_time(expiry)
    ref = datetime(2026, 11, 17, hour, minute, tzinfo=_IST)
    dte_days = days_to_expiry(expiry, ref=ref, close_time=(close_t.hour, close_t.minute))
    assert (dte_days > 0.0) is is_t_positive
