"""
test_exchange_clock_mcx_winter_snapshot_gap.py — confirms, empirically,
the latent bug found during a pre-ship review of the "MCX DST-aware
non-agri close time" fix (see CLAUDE.md).

If `exchange_schedule`'s default MCX row's `close_time` were ever set to
23:55 (the correct winter-DST-window value — see
`backend.api.algo.derivatives.mcx_non_agri_close_time`), the DERIVED
close-snapshot trigger time (`close_time + 15 min`,
`exchange_clock._effective_snapshot_time()`) wraps to 00:10 THE NEXT
CALENDAR DAY. On a Friday MCX close, that moment is a Saturday.
`exchange_clock._row_matches_now()`'s weekday filter
(`weekdays=[0,1,2,3,4]`) checks the REAL weekday at the moment the 30s
poll runs — so the MCX close-snapshot (which writes `daily_book.ltp`,
the prev_close SSOT, per CLAUDE.md's "close_price / ltp invariant")
would silently never fire on any Friday night throughout the winter
window.

This is why `backend/brokers/broker_apis.py:_sync_mcx_dst_close_time_coro`
is implemented and tested but DELIBERATELY NOT wired into
`background.py:_task_holiday_refresh` yet — see
`test_mcx_dst_close_time_sync.py`'s tripwire tests for that side.

These tests exercise `exchange_clock`'s real cache + real functions
directly (not a hand-simulated analogue). They currently PASS, because
they assert the BUG's present behavior (e.g. "the row is wrongly
excluded on Saturday"). That is deliberate: a future fix to
`exchange_clock.py`'s weekday/midnight-wrap handling should make
`test_friday_23_55_close_derived_snapshot_never_matches_on_saturday`
START FAILING — that failure is the signal to flip its assertion (the
row should now correctly match) as part of that fix, and only then
re-enable the wiring in `_task_holiday_refresh` per
`test_mcx_dst_close_time_sync.py`'s tripwire tests.
"""

from __future__ import annotations

import types
from datetime import date, datetime, time
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from backend.api.helpers import exchange_clock

_IST = ZoneInfo("Asia/Kolkata")


def _mcx_row(close_time: time, snapshot_time=None):
    return types.SimpleNamespace(
        gate="MCX",
        date=None,
        weekdays=[0, 1, 2, 3, 4],  # Mon-Fri — same as the real seeded row
        open_time=time(8, 0),
        close_time=close_time,
        snapshot_time=snapshot_time,
        snapshot_reset_time=time(8, 0),
        exchanges=["MCX"],
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


# ── The bug, demonstrated directly ──────────────────────────────────────


def test_friday_23_55_close_derived_snapshot_never_matches_on_saturday():
    """The real reported hazard: close_time=23:55 (winter), derived
    snapshot_time=00:10 the next day. A 30s poll running AT that real
    00:10 instant — which the real clock will show as a Saturday,
    because the Friday session's close pushed past midnight — must
    find the MCX row absent from `sessions_with_snapshot_time_now()`.
    """
    row = _mcx_row(close_time=time(23, 55))
    exchange_clock._CACHE = [row]

    # The real moment: Saturday 2026-11-21 00:10 IST — the derived
    # snapshot instant for a Friday 2026-11-20 23:55 close.
    friday = date(2026, 11, 20)
    saturday_just_after_midnight = datetime(2026, 11, 21, 0, 10, tzinfo=_IST)
    assert friday.weekday() == 4   # sanity: Friday
    assert saturday_just_after_midnight.weekday() == 5  # sanity: Saturday

    with patch.object(exchange_clock, "_now_ist", return_value=saturday_just_after_midnight):
        matched = exchange_clock.sessions_with_snapshot_time_now()

    assert matched == [], (
        "THIS IS THE BUG: the MCX row's derived snapshot_time (00:10) "
        "correctly matches the real clock time, but the weekday filter "
        "(checked against the REAL Saturday, not the Friday trading "
        "session it belongs to) wrongly excludes it — the close-snapshot "
        "that writes daily_book.ltp would never fire"
    )


def test_old_23_30_close_does_not_have_this_problem():
    """Sanity / contrast case: the PRE-fix flat 23:30 value derives
    snapshot_time=23:45, same calendar day, no weekday-boundary issue —
    confirming this is a NEW hazard introduced specifically by the
    winter 23:55 value, not a pre-existing defect."""
    row = _mcx_row(close_time=time(23, 30))
    exchange_clock._CACHE = [row]

    friday_evening = datetime(2026, 11, 20, 23, 45, tzinfo=_IST)
    assert friday_evening.weekday() == 4  # still Friday — no wrap

    with patch.object(exchange_clock, "_now_ist", return_value=friday_evening):
        matched = exchange_clock.sessions_with_snapshot_time_now()

    assert len(matched) == 1, (
        "the original 23:30 close_time must still match correctly — "
        "confirming the Saturday-exclusion case above is a NEW "
        "regression risk from the winter close_time value, not an "
        "existing one"
    )


def test_explicit_snapshot_time_override_sidesteps_the_bug():
    """If a future fix sets `snapshot_time` EXPLICITLY (rather than
    relying on the close_time+15min derivation) to a value that does
    not cross midnight relative to the trading day's own weekday, the
    gap disappears. Documents the shape of one viable fix direction —
    not a claim that this is what must be done."""
    row = _mcx_row(close_time=time(23, 55), snapshot_time=time(23, 59))
    exchange_clock._CACHE = [row]

    friday_evening = datetime(2026, 11, 20, 23, 59, tzinfo=_IST)
    assert friday_evening.weekday() == 4

    with patch.object(exchange_clock, "_now_ist", return_value=friday_evening):
        matched = exchange_clock.sessions_with_snapshot_time_now()

    assert len(matched) == 1, (
        "an explicit same-day snapshot_time (not derived via "
        "close_time+15min) avoids the midnight-wrap/weekday-filter gap"
    )
