"""Tests for `_run_nav_compute_once()`'s MCX-close-settled trigger time
(backend/api/background.py, 2026-09 fix).

Root cause fixed: the daily NAV snapshot cron previously fired at a fixed
`dtime(16, 0)` IST — hours before MCX's actual 23:30 IST close — so the
"end of day" NAV figure always shipped with that day's commodity P&L
still mid-session. The fix reuses the SAME effective-snapshot-time
mechanism `_sg_recover_mcx_snapshot` already uses for the MCX EOD
daily_book write (`exchange_clock._effective_gate_rows("MCX")` +
`_effective_snapshot_time(row)`), so both consumers move together.
"""

from __future__ import annotations

from datetime import date, time as dtime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.api.background import _run_nav_compute_once


def _row(close_time=None, snapshot_time=None, open_time=dtime(9, 0)):
    return SimpleNamespace(
        open_time=open_time, close_time=close_time, snapshot_time=snapshot_time,
    )


@pytest.mark.asyncio
async def test_does_not_fire_before_mcx_settled_time():
    """At 16:00 IST (the OLD trigger time), with MCX trading normally
    (close 23:30 → effective snapshot ≈23:45), the new gate must NOT
    fire — this is the exact regression the fix addresses."""
    state: dict = {}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 25), time=lambda: dtime(16, 0))
    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows",
        side_effect=lambda gate: [_row(close_time=dtime(23, 30))] if gate == "MCX" else [],
    ), patch(
        "backend.api.algo.nav.write_nav_snapshot", new=AsyncMock(),
    ) as mock_write:
        await _run_nav_compute_once(state)

    mock_write.assert_not_called()
    assert "nav_done" not in state


@pytest.mark.asyncio
async def test_fires_at_mcx_settled_time():
    """At 23:45 IST (MCX close 23:30 + 15 min default offset), the gate
    fires and writes the snapshot."""
    state: dict = {}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 25), time=lambda: dtime(23, 45))
    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows",
        side_effect=lambda gate: [_row(close_time=dtime(23, 30))] if gate == "MCX" else [],
    ), patch(
        "backend.api.algo.nav.write_nav_snapshot",
        new=AsyncMock(return_value={"nav": 1000.0, "cash_total": 0, "positions_mtm": 0, "holdings_mtm": 0}),
    ) as mock_write:
        await _run_nav_compute_once(state)

    mock_write.assert_awaited_once()
    assert state["nav_done"] == date(2026, 9, 25)


@pytest.mark.asyncio
async def test_skipped_write_pops_nav_done_latch_for_retry():
    """2026-09-27 council audit: write_nav_snapshot() can now return
    `{"skipped_write": True, ...}` (an understated leg — no last-known-
    good anywhere) WITHOUT raising. Pre-fix, `state["nav_done"]` (set
    BEFORE the write, to guard against a concurrent duplicate poll)
    would stay set even though no row was written — the day silently
    never gets a NAV row, no retry ever happens. Must pop the latch
    exactly like the existing exception-based retry path already does,
    so the next 30s poll tries again."""
    state: dict = {}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 25), time=lambda: dtime(23, 45))
    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows",
        side_effect=lambda gate: [_row(close_time=dtime(23, 30))] if gate == "MCX" else [],
    ), patch(
        "backend.api.algo.nav.write_nav_snapshot",
        new=AsyncMock(return_value={
            "nav": 1000.0, "cash_total": 0, "positions_mtm": 0, "holdings_mtm": 0,
            "errors": ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"],
            "understated": ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"],
            "skipped_write": True,
        }),
    ) as mock_write:
        await _run_nav_compute_once(state)

    mock_write.assert_awaited_once()
    mock_write.assert_awaited_once_with(target_date=date(2026, 9, 25), force=False)
    assert "nav_done" not in state, (
        "a skipped (understated) write must pop the latch so the next "
        "30s poll retries — otherwise the day permanently has no "
        "nav_daily row and nothing ever retries"
    )


@pytest.mark.asyncio
async def test_force_true_after_last_ditch_grace_period():
    """2026-09-27 council audit, second-pass refinement: past
    `target + _NAV_FORCE_GRACE` (23:45 + 10min = 23:55 for this normal-
    day fixture), write_nav_snapshot() must be called with force=True so
    a degraded row is written rather than the day permanently losing its
    nav_daily row (no backfill path exists, and the retry window closes
    at IST midnight)."""
    state: dict = {}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 25), time=lambda: dtime(23, 56))
    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows",
        side_effect=lambda gate: [_row(close_time=dtime(23, 30))] if gate == "MCX" else [],
    ), patch(
        "backend.api.algo.nav.write_nav_snapshot",
        new=AsyncMock(return_value={"nav": 1000.0, "cash_total": 0, "positions_mtm": 0, "holdings_mtm": 0}),
    ) as mock_write:
        await _run_nav_compute_once(state)

    mock_write.assert_awaited_once_with(target_date=date(2026, 9, 25), force=True)


@pytest.mark.asyncio
async def test_force_false_before_last_ditch_grace_period():
    """Regression guard — the normal 23:45 fire (well before target +
    grace = 23:55) must NOT force the write, preserving the skip-and-
    retry protection for the common transient-degradation case."""
    state: dict = {}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 25), time=lambda: dtime(23, 45))
    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows",
        side_effect=lambda gate: [_row(close_time=dtime(23, 30))] if gate == "MCX" else [],
    ), patch(
        "backend.api.algo.nav.write_nav_snapshot",
        new=AsyncMock(return_value={"nav": 1000.0, "cash_total": 0, "positions_mtm": 0, "holdings_mtm": 0}),
    ) as mock_write:
        await _run_nav_compute_once(state)

    mock_write.assert_awaited_once_with(target_date=date(2026, 9, 25), force=False)


@pytest.mark.asyncio
async def test_force_relative_to_early_fallback_target_not_fixed_clock_time():
    """MCX-holiday / weekend fallback (`target` ≈ 15:45, per
    test_falls_back_to_non_mcx_snapshot_time_on_mcx_holiday) — a FIXED
    23:55 cutoff would mean ~8 hours of 30s-interval retries (a full
    broker fetch + a warning log line each) before ever forcing. The
    grace period must be relative to `target`, forcing shortly after
    15:45 instead."""
    state: dict = {}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 25), time=lambda: dtime(15, 56))

    def _gate_rows(gate):
        if gate == "MCX":
            return [_row(open_time=None, close_time=None)]  # holiday override row
        if gate == "NON-MCX":
            return [_row(close_time=dtime(15, 30))]  # effective ≈ 15:45
        return []

    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows", side_effect=_gate_rows,
    ), patch(
        "backend.api.algo.nav.write_nav_snapshot",
        new=AsyncMock(return_value={"nav": 1000.0, "cash_total": 0, "positions_mtm": 0, "holdings_mtm": 0}),
    ) as mock_write:
        await _run_nav_compute_once(state)

    mock_write.assert_awaited_once_with(target_date=date(2026, 9, 25), force=True)


@pytest.mark.asyncio
async def test_force_false_on_first_attempt_when_target_itself_is_late():
    """A retuned/late MCX close pushing `target` to 23:55 or later must
    NOT force-write on the VERY FIRST attempt — the skip-and-retry
    protection must still get at least its grace window (falls back to
    a 23:59 same-day cutoff per `_NAV_FORCE_GRACE`'s docstring when
    `target + grace` would roll past midnight)."""
    state: dict = {}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 25), time=lambda: dtime(23, 55))
    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows",
        side_effect=lambda gate: [_row(close_time=dtime(23, 40), snapshot_time=dtime(23, 55))]
        if gate == "MCX" else [],
    ), patch(
        "backend.api.algo.nav.write_nav_snapshot",
        new=AsyncMock(return_value={"nav": 1000.0, "cash_total": 0, "positions_mtm": 0, "holdings_mtm": 0}),
    ) as mock_write:
        await _run_nav_compute_once(state)

    mock_write.assert_awaited_once_with(target_date=date(2026, 9, 25), force=False)


@pytest.mark.asyncio
async def test_skipped_write_audit_event_fires_once_per_day_not_per_retry():
    """A permanently-broken account retries ~30x/hour after 23:45 — the
    audit event must fire once for the day, not on every retry."""
    state: dict = {}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 25), time=lambda: dtime(23, 45))
    skipped_snap = {
        "nav": 1000.0, "cash_total": 0, "positions_mtm": 0, "holdings_mtm": 0,
        "errors": ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"],
        "understated": ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"],
        "skipped_write": True,
    }
    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows",
        side_effect=lambda gate: [_row(close_time=dtime(23, 30))] if gate == "MCX" else [],
    ), patch(
        "backend.api.algo.nav.write_nav_snapshot",
        new=AsyncMock(return_value=skipped_snap),
    ), patch(
        "backend.api.audit.write_audit_event",
    ) as mock_audit:
        await _run_nav_compute_once(state)   # first retry attempt
        await _run_nav_compute_once(state)   # a later 30s-poll retry, same day

    assert mock_audit.call_count == 1, (
        f"expected exactly 1 audit event across 2 retries the same day, "
        f"got {mock_audit.call_count}"
    )
    assert mock_audit.call_args.kwargs["action"] == "NAV_SNAPSHOT_SKIPPED"


@pytest.mark.asyncio
async def test_falls_back_to_non_mcx_snapshot_time_on_mcx_holiday():
    """MCX-specific holiday (its gate rows have close_time=None) — falls
    back to NON-MCX's own effective snapshot time (~15:45) so a same-day
    NAV row still lands for an equity-only session."""
    state: dict = {}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 25), time=lambda: dtime(15, 45))

    def _gate_rows(gate):
        if gate == "MCX":
            return [_row(open_time=None, close_time=None)]  # holiday override row
        if gate == "NON-MCX":
            return [_row(close_time=dtime(15, 30))]
        return []

    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows", side_effect=_gate_rows,
    ), patch(
        "backend.api.algo.nav.write_nav_snapshot",
        new=AsyncMock(return_value={"nav": 500.0, "cash_total": 0, "positions_mtm": 0, "holdings_mtm": 0}),
    ) as mock_write:
        await _run_nav_compute_once(state)

    mock_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_falls_back_to_1600_when_both_gates_fully_closed():
    """Full non-trading day (weekend/firm holiday) — both MCX and
    NON-MCX gate lookups return nothing usable; falls back to the old
    fixed 16:00 IST as a last resort rather than never firing."""
    state: dict = {}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 27), time=lambda: dtime(16, 0))
    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows", return_value=[],
    ), patch(
        "backend.api.algo.nav.write_nav_snapshot",
        new=AsyncMock(return_value={"nav": 0.0, "cash_total": 0, "positions_mtm": 0, "holdings_mtm": 0}),
    ) as mock_write:
        await _run_nav_compute_once(state)

    mock_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_once_per_day_latch_still_prevents_refire():
    """state['nav_done'] == today short-circuits before any gate lookup —
    the once-per-day invariant is unchanged by this fix."""
    state = {"nav_done": date(2026, 9, 25)}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 25), time=lambda: dtime(23, 50))
    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows",
    ) as mock_rows, patch(
        "backend.api.algo.nav.write_nav_snapshot", new=AsyncMock(),
    ) as mock_write:
        await _run_nav_compute_once(state)

    mock_write.assert_not_called()
    mock_rows.assert_not_called()


@pytest.mark.asyncio
async def test_latch_resets_on_next_trading_day():
    """A stale latch from a PRIOR date does not block today's fire —
    the comparison is state['nav_done'] == today, so date rollover
    naturally re-arms the gate."""
    state = {"nav_done": date(2026, 9, 24)}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 25), time=lambda: dtime(23, 45))
    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows",
        side_effect=lambda gate: [_row(close_time=dtime(23, 30))] if gate == "MCX" else [],
    ), patch(
        "backend.api.algo.nav.write_nav_snapshot",
        new=AsyncMock(return_value={"nav": 1000.0, "cash_total": 0, "positions_mtm": 0, "holdings_mtm": 0}),
    ) as mock_write:
        await _run_nav_compute_once(state)

    mock_write.assert_awaited_once()
    assert state["nav_done"] == date(2026, 9, 25)


@pytest.mark.asyncio
async def test_pops_latch_on_write_failure_for_retry():
    """A failed write must NOT poison the once-per-day latch — the next
    30s poll should retry (pre-existing behaviour, unaffected by this
    fix's gate-time change)."""
    state: dict = {}
    fake_now = SimpleNamespace(date=lambda: date(2026, 9, 25), time=lambda: dtime(23, 45))
    with patch(
        "backend.api.background.timestamp_indian", return_value=fake_now,
    ), patch(
        "backend.api.helpers.exchange_clock._effective_gate_rows",
        side_effect=lambda gate: [_row(close_time=dtime(23, 30))] if gate == "MCX" else [],
    ), patch(
        "backend.api.algo.nav.write_nav_snapshot",
        new=AsyncMock(side_effect=RuntimeError("broker down")),
    ):
        await _run_nav_compute_once(state)

    assert "nav_done" not in state
