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
