"""
test_exchange_clock_mcx_winter_snapshot_gap.py — the MCX winter-window
Friday-night snapshot gap found during a pre-ship review of the "MCX
DST-aware non-agri close time" fix (see CLAUDE.md), and its root-cause
FIX in `exchange_clock.py`.

Root cause: MCX's winter non-agri close (23:55 IST) derives a
close-snapshot trigger (`close_time + 15 min`,
`exchange_clock._effective_snapshot_time()`) of 00:10 THE NEXT CALENDAR
DAY. On a Friday MCX close, that moment is a Saturday. The OLD
`sessions_with_snapshot_time_now()` resolved "today's" governing rows
via `_effective_gate_rows()`, whose weekday filter checked the REAL
clock weekday at that later (post-midnight) moment — Saturday — against
the row's Mon-Fri filter, and wrongly excluded it, so the close-snapshot
(which writes `daily_book.ltp`, the prev_close SSOT) would silently
never fire on any Friday night throughout the winter window. The OLD
code also compared bare `time(...)` values (both collapsed onto
`datetime.today()`), which cannot represent a window spanning midnight
at all.

THE FIX (2026-11-21): `sessions_with_snapshot_time_now()` (and
`is_market_active_for_prev_close()`, which had the identical pattern)
now check BOTH today's and yesterday's governing session, each resolved
against ITS OWN calendar date via the new `_effective_gate_rows_for_date()`
— never "now"'s real-clock date — and compare ABSOLUTE datetimes via
the new `_anchor_snapshot_datetime()`, which anchors a derived (not
explicit) snapshot time to the day AFTER its session's close_time only
when the `close_time + 15 min` arithmetic itself crosses midnight (see
`_effective_snapshot_day_offset()`). An explicit `snapshot_time` is
always treated as same-day, regardless of its numeric value relative to
close_time — this matters because a pre-close special-session snapshot
can legitimately be numerically "earlier" than its own close_time
without being a midnight wrap.

These tests exercise `exchange_clock`'s real cache + real functions
directly (not a hand-simulated analogue). The first test below used to
assert the BUG's broken behavior (an empty match) — see git history for
the pre-fix version — and now asserts the CORRECT, fixed behavior (the
row matches). `test_mcx_dst_close_time_sync.py`'s wiring tripwires were
flipped in the same change that landed this fix — see that file.
"""

from __future__ import annotations

import types
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from backend.api.helpers import exchange_clock

_IST = ZoneInfo("Asia/Kolkata")


def _mcx_row(close_time: time, snapshot_time=None, weekdays=(0, 1, 2, 3, 4)):
    return types.SimpleNamespace(
        gate="MCX",
        date=None,
        weekdays=list(weekdays),  # Mon-Fri by default — same as the real seeded row
        open_time=time(8, 0),
        close_time=close_time,
        snapshot_time=snapshot_time,
        snapshot_reset_time=time(8, 0),
        exchanges=["MCX"],
        is_open=True,
        session_name="regular",
        source="system",
    )


def _row(gate, close_time, open_time=time(8, 0), snapshot_time=None,
        weekdays=(0, 1, 2, 3, 4), exchanges=None):
    return types.SimpleNamespace(
        gate=gate,
        date=None,
        weekdays=list(weekdays),
        open_time=open_time,
        close_time=close_time,
        snapshot_time=snapshot_time,
        snapshot_reset_time=time(8, 0),
        exchanges=exchanges or [gate],
        is_open=True,
        session_name="regular",
        source="system",
    )


@pytest.fixture(autouse=True)
def _restore_cache():
    """exchange_clock._CACHE is module-level global state — isolate it
    per test so this file can't leak into any other test's cache."""
    original = exchange_clock._CACHE
    yield
    exchange_clock._CACHE = original


# ── The bug — now FIXED: the row correctly matches ─────────────────────


def test_friday_23_55_close_derived_snapshot_now_matches_on_saturday():
    """The real reported hazard, now fixed: close_time=23:55 (winter),
    derived snapshot_time=00:10 the next day. A 30s poll running AT that
    real 00:10 instant — which the real clock shows as a Saturday,
    because the Friday session's close pushed past midnight — must now
    find the MCX row correctly matched (it belongs to Friday's session,
    which resolves via `_effective_gate_rows_for_date` against Friday's
    OWN weekday, not Saturday's)."""
    row = _mcx_row(close_time=time(23, 55))
    exchange_clock._CACHE = [row]

    friday = date(2026, 11, 20)
    saturday_just_after_midnight = datetime(2026, 11, 21, 0, 10, tzinfo=_IST)
    assert friday.weekday() == 4   # sanity: Friday
    assert saturday_just_after_midnight.weekday() == 5  # sanity: Saturday

    with patch.object(exchange_clock, "_now_ist", return_value=saturday_just_after_midnight):
        matched = exchange_clock.sessions_with_snapshot_time_now()

    assert matched == [row], (
        "FIXED: the MCX row's derived snapshot_time (00:10) must now "
        "match — resolved against Friday's own session date, not "
        "Saturday's real-clock weekday — so the close-snapshot that "
        "writes daily_book.ltp fires correctly"
    )


def test_friday_23_55_close_is_market_active_now_true_on_saturday():
    """Same root cause, same fix, for `is_market_active_for_prev_close()`
    — it must now report the post-close snapshot window as active at
    Saturday 00:10 for a Friday 23:55 close."""
    row = _mcx_row(close_time=time(23, 55))
    exchange_clock._CACHE = [row]
    saturday_just_after_midnight = datetime(2026, 11, 21, 0, 10, tzinfo=_IST)

    with patch.object(exchange_clock, "_now_ist", return_value=saturday_just_after_midnight):
        result = exchange_clock.is_market_active_for_prev_close()

    assert result is True


def test_saturday_itself_does_not_spuriously_match_before_or_after_the_window():
    """One minute before/after the ±1-min tolerance window around the
    wrapped Saturday 00:10 trigger must NOT match — proves this isn't a
    blanket 'any Saturday moment matches' regression."""
    row = _mcx_row(close_time=time(23, 55))
    exchange_clock._CACHE = [row]

    too_early = datetime(2026, 11, 21, 0, 8, 30, tzinfo=_IST)   # >1min before 00:10
    too_late  = datetime(2026, 11, 21, 0, 11, 30, tzinfo=_IST)  # >1min after 00:10

    with patch.object(exchange_clock, "_now_ist", return_value=too_early):
        assert exchange_clock.sessions_with_snapshot_time_now() == []
    with patch.object(exchange_clock, "_now_ist", return_value=too_late):
        assert exchange_clock.sessions_with_snapshot_time_now() == []


def test_a_real_saturday_with_no_friday_session_never_matches():
    """A Saturday with NO adjoining Friday session in _CACHE (e.g. a
    gate that only ever trades Tue/Wed) must not spuriously match just
    because 'yesterday' happens to be checked too."""
    row = _mcx_row(close_time=time(23, 55), weekdays=(1, 2))  # Tue, Wed only
    exchange_clock._CACHE = [row]

    saturday_just_after_midnight = datetime(2026, 11, 21, 0, 10, tzinfo=_IST)
    with patch.object(exchange_clock, "_now_ist", return_value=saturday_just_after_midnight):
        assert exchange_clock.sessions_with_snapshot_time_now() == []


# ── Contrast: the old flat 23:30 value never had this problem ──────────


def test_old_23_30_close_does_not_have_this_problem():
    """Sanity / contrast case: the PRE-fix flat 23:30 value derives
    snapshot_time=23:45, same calendar day, no weekday-boundary issue —
    confirming the winter 23:55 value was the actual trigger for the
    bug, not some pre-existing defect that was coincidentally hidden."""
    row = _mcx_row(close_time=time(23, 30))
    exchange_clock._CACHE = [row]

    friday_evening = datetime(2026, 11, 20, 23, 45, tzinfo=_IST)
    assert friday_evening.weekday() == 4  # still Friday — no wrap

    with patch.object(exchange_clock, "_now_ist", return_value=friday_evening):
        matched = exchange_clock.sessions_with_snapshot_time_now()

    assert matched == [row]


def test_explicit_snapshot_time_override_also_works():
    """An explicit `snapshot_time` (not derived via close_time+15min)
    continues to match correctly — same-day, offset=0 always."""
    row = _mcx_row(close_time=time(23, 55), snapshot_time=time(23, 59))
    exchange_clock._CACHE = [row]

    friday_evening = datetime(2026, 11, 20, 23, 59, tzinfo=_IST)
    assert friday_evening.weekday() == 4

    with patch.object(exchange_clock, "_now_ist", return_value=friday_evening):
        matched = exchange_clock.sessions_with_snapshot_time_now()

    assert matched == [row]


def test_explicit_snapshot_time_earlier_than_close_is_not_misread_as_a_wrap():
    """An explicit snapshot_time that is NUMERICALLY earlier than
    close_time (a legitimate pre-close special-session snapshot) must
    NOT be misclassified as a midnight wrap — it stays same-day."""
    row = _row("NON-MCX", close_time=time(21, 30), snapshot_time=time(21, 15))
    exchange_clock._CACHE = [row]

    moment = datetime(2026, 11, 20, 21, 15, tzinfo=_IST)  # Friday
    with patch.object(exchange_clock, "_now_ist", return_value=moment):
        matched = exchange_clock.sessions_with_snapshot_time_now()

    assert matched == [row], (
        "an explicit snapshot_time earlier than close_time must still "
        "match on the SAME day — it is not a midnight-wrap case"
    )


# ── Comprehensive non-regression sweep — every non-wrapping combination ─


@pytest.mark.parametrize("gate,close_time,expected_snap,weekday_date", [
    ("NON-MCX", time(15, 30), time(15, 45), date(2026, 11, 16)),   # Mon
    ("NON-MCX", time(15, 30), time(15, 45), date(2026, 11, 20)),   # Fri
    ("MCX",     time(23, 30), time(23, 45), date(2026, 11, 16)),   # Mon, pre-fix summer value
    ("MCX",     time(23, 30), time(23, 45), date(2026, 11, 20)),   # Fri, pre-fix summer value
])
def test_non_wrapping_combinations_match_on_their_own_day_only(
    gate, close_time, expected_snap, weekday_date,
):
    """Every close_time that does NOT cross midnight must match on ITS
    OWN day, at its derived snapshot instant — exactly the pre-fix
    behaviour, for both gates, on both a Monday and a Friday."""
    row = _row(gate, close_time=close_time)
    exchange_clock._CACHE = [row]

    moment = datetime.combine(weekday_date, expected_snap, tzinfo=_IST)
    with patch.object(exchange_clock, "_now_ist", return_value=moment):
        matched = exchange_clock.sessions_with_snapshot_time_now()
    assert matched == [row]

    # One minute before: no match.
    with patch.object(exchange_clock, "_now_ist",
                      return_value=moment - timedelta(minutes=5)):
        assert exchange_clock.sessions_with_snapshot_time_now() == []

    # The identical bare time, 7 days later (same weekday, so the row's
    # weekday filter still passes — this must still match, since it is a
    # genuinely new occurrence of the SAME weekly trigger, not a
    # same-value coincidence from a different session): confirms the fix
    # anchors to the date actually passed in, not a fixed reference date.
    with patch.object(exchange_clock, "_now_ist",
                      return_value=moment + timedelta(days=7)):
        assert exchange_clock.sessions_with_snapshot_time_now() == [row]


def test_weekend_default_row_still_excluded_entirely():
    """A default Mon-Fri row must still never match on a real Saturday
    or Sunday with no adjoining Friday-wrap session — confirms the new
    yesterday-branch doesn't loosen the weekend exclusion."""
    row = _row("NON-MCX", close_time=time(15, 30))  # no wrap at all
    exchange_clock._CACHE = [row]

    saturday = datetime(2026, 11, 21, 15, 45, tzinfo=_IST)
    with patch.object(exchange_clock, "_now_ist", return_value=saturday):
        assert exchange_clock.sessions_with_snapshot_time_now() == []

    sunday = datetime(2026, 11, 22, 15, 45, tzinfo=_IST)
    with patch.object(exchange_clock, "_now_ist", return_value=sunday):
        assert exchange_clock.sessions_with_snapshot_time_now() == []


def test_two_gates_today_and_yesterday_do_not_cross_contaminate():
    """NON-MCX (no wrap) and MCX (wrapping winter value) in the same
    cache, checked at the wrapped MCX instant — only the MCX row must
    match, never a spurious NON-MCX match."""
    non_mcx = _row("NON-MCX", close_time=time(15, 30))
    mcx = _mcx_row(close_time=time(23, 55))
    exchange_clock._CACHE = [non_mcx, mcx]

    saturday_just_after_midnight = datetime(2026, 11, 21, 0, 10, tzinfo=_IST)
    with patch.object(exchange_clock, "_now_ist", return_value=saturday_just_after_midnight):
        matched = exchange_clock.sessions_with_snapshot_time_now()

    assert matched == [mcx]


# ── _anchor_snapshot_datetime / _effective_snapshot_day_offset unit tests ─


def test_effective_snapshot_day_offset_derived_wrap():
    row = _mcx_row(close_time=time(23, 55))
    assert exchange_clock._effective_snapshot_day_offset(row) == 1


def test_effective_snapshot_day_offset_derived_no_wrap():
    row = _mcx_row(close_time=time(23, 30))
    assert exchange_clock._effective_snapshot_day_offset(row) == 0


def test_effective_snapshot_day_offset_explicit_always_zero_even_if_small():
    """An explicit snapshot_time of e.g. 00:05 (genuinely intended as
    same-day, e.g. an unusual very-late session) must still resolve
    offset=0 — only a DERIVED value can ever wrap, by design; an
    explicit value is the operator's own direct claim."""
    row = _mcx_row(close_time=time(23, 30), snapshot_time=time(0, 5))
    assert exchange_clock._effective_snapshot_day_offset(row) == 0


def test_anchor_snapshot_datetime_rolls_forward_on_wrap():
    row = _mcx_row(close_time=time(23, 55))
    session_date = date(2026, 11, 20)  # Friday
    anchored = exchange_clock._anchor_snapshot_datetime(session_date, row)
    assert anchored == datetime(2026, 11, 21, 0, 10, tzinfo=_IST)


def test_anchor_snapshot_datetime_stays_same_day_when_no_wrap():
    row = _mcx_row(close_time=time(23, 30))
    session_date = date(2026, 11, 20)
    anchored = exchange_clock._anchor_snapshot_datetime(session_date, row)
    assert anchored == datetime(2026, 11, 20, 23, 45, tzinfo=_IST)


def test_anchor_snapshot_datetime_none_when_no_close_or_snapshot_time():
    row = _mcx_row(close_time=None)
    row.close_time = None
    assert exchange_clock._anchor_snapshot_datetime(date(2026, 11, 20), row) is None
