"""
test_session_guard_mcx_winter_snapshot.py — `_sg_recover_mcx_snapshot()`
(SessionGuard's startup-recovery MCX EOD `daily_book` write) had the
identical cross-midnight bare-time-comparison bug as the
`exchange_clock.py` functions fixed alongside this one (see
`test_exchange_clock_mcx_winter_snapshot_gap.py` and CLAUDE.md's
"MCX DST-aware non-agri close time").

Confirmed empirically before this fix: with MCX's winter non-agri close
(23:55 IST, derived snapshot trigger 00:10 the next calendar day), the
old `now_t >= snap_t or now_t < dtime(2, 0)` heuristic was true at
almost ANY time of day — e.g. a server restart at 10:00 AM would
wrongly fire a mid-session "EOD" `daily_book` write for MCX.

Fix: the function now takes the FULL real `datetime` (was previously
only its bare `.time()`), and resolves the trigger via
`exchange_clock._anchor_snapshot_datetime(today, mcx_rows[0])` — an
absolute datetime anchored to the session's own date (the `sess_today`
the caller already passes), compared directly against `now`. This also
subsumes the old `< dtime(2, 0)` carve-out for a restart shortly after
a NORMAL close, with no separate heuristic needed.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

import backend.api.background as bg

_IST = ZoneInfo("Asia/Kolkata")


def _mcx_row(close_time, snapshot_time=None):
    return SimpleNamespace(close_time=close_time, snapshot_time=snapshot_time)


@pytest.fixture(autouse=True)
def _clear_sentinel():
    bg._snapshot_fired_today.pop("MCX", None)
    yield
    bg._snapshot_fired_today.pop("MCX", None)


# ── The bug, now fixed ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_winter_restart_at_10am_same_day_does_not_fire():
    """THIS WAS THE BUG: a restart at 10:00 AM on the SAME calendar date
    as the session being recovered — clearly mid-session, hours before
    the real 23:55 close — must NOT fire the EOD write."""
    mcx_rows = [_mcx_row(close_time=dtime(23, 55))]
    sess_today = date(2026, 11, 20)
    now = datetime(2026, 11, 20, 10, 0, tzinfo=_IST)

    with patch("backend.api.background._snapshot_fire", new=AsyncMock()) as mock_fire:
        await bg._sg_recover_mcx_snapshot(mcx_rows, now, sess_today)

    mock_fire.assert_not_awaited()
    assert bg._snapshot_fired_today.get("MCX") is None


@pytest.mark.asyncio
async def test_winter_restart_just_after_the_real_wrapped_trigger_fires():
    """A restart at 00:15 the NEXT calendar day — just after the real
    00:10 wrapped trigger for Friday's session — must fire, and must
    seed the sentinel with the SESSION's own date (Friday), not the
    real-clock date (Saturday)."""
    mcx_rows = [_mcx_row(close_time=dtime(23, 55))]
    sess_today = date(2026, 11, 20)  # Friday — resolved via session_date_of() by the caller
    now = datetime(2026, 11, 21, 0, 15, tzinfo=_IST)  # real clock: Saturday 00:15

    with patch("backend.api.background._snapshot_fire", new=AsyncMock()) as mock_fire:
        await bg._sg_recover_mcx_snapshot(mcx_rows, now, sess_today)

    mock_fire.assert_awaited_once()
    assert bg._snapshot_fired_today["MCX"] == date(2026, 11, 20)


@pytest.mark.asyncio
async def test_winter_restart_just_before_the_real_wrapped_trigger_does_not_fire():
    """A restart at 00:05 the next day — 5 minutes BEFORE the real
    00:10 trigger — must not fire yet."""
    mcx_rows = [_mcx_row(close_time=dtime(23, 55))]
    sess_today = date(2026, 11, 20)
    now = datetime(2026, 11, 21, 0, 5, tzinfo=_IST)

    with patch("backend.api.background._snapshot_fire", new=AsyncMock()) as mock_fire:
        await bg._sg_recover_mcx_snapshot(mcx_rows, now, sess_today)

    mock_fire.assert_not_awaited()


# ── Contrast / regression: the normal (non-wrapping) restart case ──────


@pytest.mark.asyncio
async def test_normal_close_restart_shortly_after_midnight_still_fires():
    """The pre-existing, intentional behaviour this fix must preserve:
    a restart shortly after midnight for a NORMAL (non-wrapping, e.g.
    23:30 summer) close must still recognise that close as already
    past — this used to be the explicit `now_t < dtime(2, 0)` carve-out,
    now subsumed by anchoring to the session's own date."""
    mcx_rows = [_mcx_row(close_time=dtime(23, 30))]  # pre-fix summer value, snapshot derives 23:45
    sess_today = date(2026, 9, 1)  # session_date_of() already resolved this to "yesterday"
    now = datetime(2026, 9, 2, 1, 0, tzinfo=_IST)  # real restart at 01:00 the next day

    with patch("backend.api.background._snapshot_fire", new=AsyncMock()) as mock_fire:
        await bg._sg_recover_mcx_snapshot(mcx_rows, now, sess_today)

    mock_fire.assert_awaited_once()
    assert bg._snapshot_fired_today["MCX"] == date(2026, 9, 1)


@pytest.mark.asyncio
async def test_normal_close_restart_mid_session_does_not_fire():
    """A normal (non-wrapping) close, restart mid-session (14:00) —
    must not fire. Confirms the fix did not loosen the normal case."""
    mcx_rows = [_mcx_row(close_time=dtime(23, 30))]
    sess_today = date(2026, 9, 1)
    now = datetime(2026, 9, 1, 14, 0, tzinfo=_IST)

    with patch("backend.api.background._snapshot_fire", new=AsyncMock()) as mock_fire:
        await bg._sg_recover_mcx_snapshot(mcx_rows, now, sess_today)

    mock_fire.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_fired_sentinel_short_circuits():
    """Dedup: if the sentinel already carries the session's own date,
    never re-fire, even if the trigger would otherwise match."""
    mcx_rows = [_mcx_row(close_time=dtime(23, 55))]
    sess_today = date(2026, 11, 20)
    bg._snapshot_fired_today["MCX"] = sess_today
    now = datetime(2026, 11, 21, 0, 15, tzinfo=_IST)

    with patch("backend.api.background._snapshot_fire", new=AsyncMock()) as mock_fire:
        await bg._sg_recover_mcx_snapshot(mcx_rows, now, sess_today)

    mock_fire.assert_not_awaited()


# ── Upper-bound regression — a late restart must not retroactively fire ─


@pytest.mark.asyncio
async def test_winter_restart_hours_later_does_not_retroactively_fire():
    """A Tuesday 05:00 restart recovering a never-seeded Monday sentinel
    must NOT fire — the old heuristic's implicit upper bound
    (`now_t < dtime(2, 0)`) capped the whole catch-up window at
    02:00 the day after the session; a bare `now >= anchor_dt` with no
    upper bound would silently widen that to unbounded, which the old
    code never did for ANY close_time, wrapping or not."""
    mcx_rows = [_mcx_row(close_time=dtime(23, 55))]  # winter value
    sess_today = date(2026, 11, 16)  # Monday's session
    now = datetime(2026, 11, 17, 5, 0, tzinfo=_IST)  # Tuesday 05:00 — hours later

    with patch("backend.api.background._snapshot_fire", new=AsyncMock()) as mock_fire:
        await bg._sg_recover_mcx_snapshot(mcx_rows, now, sess_today)

    mock_fire.assert_not_awaited(), (
        "a restart hours after the catch-up window closed must not "
        "retroactively fire an ancient EOD write"
    )


@pytest.mark.asyncio
async def test_normal_close_restart_hours_later_does_not_retroactively_fire():
    """Same upper-bound check for a NORMAL (non-wrapping) close — this
    is the exact scenario the old `now_t >= snap_t or now_t < dtime(2,0)`
    heuristic already protected against, and this fix must not regress
    it for the common (non-winter) case, which is what runs in
    production today."""
    mcx_rows = [_mcx_row(close_time=dtime(23, 30))]  # pre-fix summer value
    sess_today = date(2026, 9, 1)
    now = datetime(2026, 9, 2, 5, 0, tzinfo=_IST)  # Tuesday 05:00

    with patch("backend.api.background._snapshot_fire", new=AsyncMock()) as mock_fire:
        await bg._sg_recover_mcx_snapshot(mcx_rows, now, sess_today)

    mock_fire.assert_not_awaited()


@pytest.mark.asyncio
async def test_winter_restart_just_before_the_02_00_deadline_still_fires():
    """Just inside the upper bound (01:59) must still fire — confirms
    the deadline is exclusive at 02:00, not an off-by-one that closes
    the window too early."""
    mcx_rows = [_mcx_row(close_time=dtime(23, 55))]
    sess_today = date(2026, 11, 20)
    now = datetime(2026, 11, 21, 1, 59, tzinfo=_IST)

    with patch("backend.api.background._snapshot_fire", new=AsyncMock()) as mock_fire:
        await bg._sg_recover_mcx_snapshot(mcx_rows, now, sess_today)

    mock_fire.assert_awaited_once()


@pytest.mark.asyncio
async def test_winter_restart_at_exactly_02_00_does_not_fire():
    """At exactly 02:00 the deadline has passed (exclusive upper
    bound)."""
    mcx_rows = [_mcx_row(close_time=dtime(23, 55))]
    sess_today = date(2026, 11, 20)
    now = datetime(2026, 11, 21, 2, 0, tzinfo=_IST)

    with patch("backend.api.background._snapshot_fire", new=AsyncMock()) as mock_fire:
        await bg._sg_recover_mcx_snapshot(mcx_rows, now, sess_today)

    mock_fire.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_mcx_rows_is_a_noop():
    with patch("backend.api.background._snapshot_fire", new=AsyncMock()) as mock_fire:
        await bg._sg_recover_mcx_snapshot([], datetime(2026, 11, 21, 0, 15, tzinfo=_IST),
                                          date(2026, 11, 20))
    mock_fire.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_close_time_is_a_noop():
    mcx_rows = [_mcx_row(close_time=None)]
    with patch("backend.api.background._snapshot_fire", new=AsyncMock()) as mock_fire:
        await bg._sg_recover_mcx_snapshot(mcx_rows, datetime(2026, 11, 21, 0, 15, tzinfo=_IST),
                                          date(2026, 11, 20))
    mock_fire.assert_not_awaited()
