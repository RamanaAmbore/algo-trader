"""
Closed-window broker gating: regression guards that do NOT depend on the
per-segment cached helper (`is_segment_open_cached`), so they are valid
against both the pre-change and post-change code shapes.

Covers:
  - `_task_funds_offhours`: a true no-op during the closed window (zero
    broker calls, zero cache invalidation). This is a deliberate REVERSAL
    of the prior behaviour (it used to poll broker because it was closed);
    the test fails loudly if anyone restores the old off-hours fetch.
  - `_snapshot_stop_ticker()` at the 23:31 tick clears the connected flag
    in `_TICKER_CALENDAR_STATE` immediately, not at the midnight rollover.
  - Closed-hours display independence: `closed_hours_or_broker()` keeps
    returning the last snapshot, and never calls the broker, regardless of
    what the background loops do. The new gate and the display gate must
    not share state or import each other.

Test conventions follow `test_ticker_calendar_gate.py` (clock injection via
`patch("backend.api.background.timestamp_indian", ...)`, `_TICKER_CALENDAR_STATE`
reset fixture).
"""
from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_IST = ZoneInfo("Asia/Kolkata")
_REPO = Path(__file__).resolve().parents[2]
_SNAPSHOT_GATE = _REPO / "backend" / "api" / "helpers" / "snapshot_gate.py"


class _StopLoop(BaseException):
    """Raised from the patched sleep to break out of an infinite task loop."""


def _reset_ticker_state():
    import backend.api.background as bg
    bg._TICKER_CALENDAR_STATE = {
        "date": None, "connected": False, "idle": False, "reason": "", "checked_at": 0.0,
    }
    bg._ticker_restart_done_global = None


@pytest.fixture(autouse=True)
def _clean_ticker_state():
    _reset_ticker_state()
    yield
    _reset_ticker_state()


def _sleep_once_then_stop():
    """Patched asyncio.sleep: the first call (startup settle) returns, the
    second (the 30-min cadence at the bottom of the loop) raises _StopLoop so
    exactly one loop iteration runs."""
    calls = {"n": 0}

    async def _fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise _StopLoop()

    return _fake_sleep


# ---------------------------------------------------------------------------
# _task_funds_offhours — reversal: no broker call while closed
# ---------------------------------------------------------------------------

async def test_funds_offhours_makes_no_broker_call_when_all_segments_closed():
    """Closed window (Saturday 02:00 IST, every segment shut): the task must
    not call the margins broker fetch and must not invalidate the funds cache.

    Old behaviour (pre-reversal) fetched margins here — this assertion is the
    tripwire against that regression."""
    import backend.api.background as bg

    saturday_0200 = datetime(2026, 10, 24, 2, 0, tzinfo=_IST)
    fetch = MagicMock(name="_fetch_margins_direct")
    invalidate = MagicMock(name="invalidate")

    with patch("backend.api.background.timestamp_indian", return_value=saturday_0200), \
         patch("backend.shared.helpers.date_time_utils.is_any_segment_open", return_value=False), \
         patch("backend.api.background._fetch_margins_direct", fetch), \
         patch("backend.api.cache.invalidate", invalidate), \
         patch("asyncio.sleep", new=_sleep_once_then_stop()):
        with pytest.raises(_StopLoop):
            await bg._task_funds_offhours()

    assert fetch.call_count == 0, (
        f"_task_funds_offhours must make ZERO broker calls while closed, "
        f"but _fetch_margins_direct was called {fetch.call_count} time(s)"
    )
    assert invalidate.call_count == 0, (
        f"_task_funds_offhours must not invalidate the funds cache while closed, "
        f"but invalidate() was called {invalidate.call_count} time(s)"
    )


async def test_funds_offhours_no_broker_call_even_at_midnight_closed_window():
    """Boundary inside the closed window (23:45 IST, MCX already closed)."""
    import backend.api.background as bg

    late_night = datetime(2026, 10, 23, 23, 45, tzinfo=_IST)
    fetch = MagicMock(name="_fetch_margins_direct")

    with patch("backend.api.background.timestamp_indian", return_value=late_night), \
         patch("backend.shared.helpers.date_time_utils.is_any_segment_open", return_value=False), \
         patch("backend.api.background._fetch_margins_direct", fetch), \
         patch("asyncio.sleep", new=_sleep_once_then_stop()):
        with pytest.raises(_StopLoop):
            await bg._task_funds_offhours()

    assert fetch.call_count == 0, "funds off-hours task must not poll the broker at 23:45 IST"


# ---------------------------------------------------------------------------
# _snapshot_stop_ticker — explicit idle-clear at the 23:31 tick
# ---------------------------------------------------------------------------

def test_snapshot_stop_ticker_clears_connected_flag_at_2331_not_at_midnight():
    """A connected mark from the session must NOT survive the 23:31 stop.

    Before the fix, `ticker_connected_today()` stayed True until the IST date
    rolled over at midnight (~29 min of false 'connected'). After the fix the
    23:31 stop itself clears the flag, so the watchdog stops inspecting a
    socket that no longer exists. One patched IST clock (23:31, same date)
    spans mark, check and stop, so no date rollover is involved."""
    import backend.api.background as bg

    stop_tick = datetime(2026, 10, 19, 23, 31, tzinfo=_IST)
    with patch("backend.api.background.timestamp_indian", return_value=stop_tick), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=MagicMock(name="ticker")), \
         patch("backend.api.background.is_cutover_on", return_value=False):
        bg._ticker_calendar_mark(stop_tick.date(), connected=True, idle=False, reason="connected")
        assert bg.ticker_connected_today() is True, "precondition: marked connected before the stop"

        bg._snapshot_stop_ticker()

        assert bg.ticker_connected_today() is False, (
            "after the 23:31 stop, ticker_connected_today() must be False immediately; "
            f"state is still {bg._TICKER_CALENDAR_STATE!r}"
        )


def test_snapshot_stop_ticker_clear_is_immediate_not_midnight_dependent():
    """The connected flag must read False at 23:31 on the SAME IST date as the
    mark, i.e. the clear must not depend on a date rollover."""
    import backend.api.background as bg

    stop_tick = datetime(2026, 10, 19, 23, 31, tzinfo=_IST)
    with patch("backend.api.background.timestamp_indian", return_value=stop_tick), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=MagicMock()), \
         patch("backend.api.background.is_cutover_on", return_value=False):
        bg._ticker_calendar_mark(stop_tick.date(), connected=True, idle=False, reason="connected")
        bg._snapshot_stop_ticker()

    assert bg._TICKER_CALENDAR_STATE.get("date") == stop_tick.date(), "precondition: same IST date"
    assert bg._TICKER_CALENDAR_STATE.get("connected") is False, (
        "23:31 stop must set connected=False on the same IST date; "
        f"got {bg._TICKER_CALENDAR_STATE!r}"
    )


# ---------------------------------------------------------------------------
# Closed-hours display independence (regression guard for the "never blank" invariant)
# ---------------------------------------------------------------------------

async def test_closed_hours_route_serves_snapshot_without_calling_broker():
    """With every segment closed, `closed_hours_or_broker()` returns the DB
    snapshot unchanged and never invokes the broker callable. This is the
    'never blank during closed hours' invariant the background-loop gating
    must not disturb."""
    from backend.api.helpers import snapshot_gate

    snapshot = [{"symbol": "RELIANCE", "qty": 10, "ltp": 2950.0}]
    snapshot_fn = AsyncMock(return_value=snapshot)
    broker_fn = AsyncMock(side_effect=AssertionError("broker must not be called while closed"))

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False):
        data, source = await snapshot_gate.closed_hours_or_broker(
            "NSE", snapshot_fn, broker_fn, route_key="positions",
        )

    assert data == snapshot, f"closed-hours route must return the snapshot unchanged, got {data!r}"
    assert source == "snapshot", f"expected source 'snapshot' while closed, got {source!r}"
    assert broker_fn.await_count == 0, "broker_fn must never be awaited while the market is closed"


async def test_closed_hours_snapshot_survives_background_loops_being_gated():
    """Gating the four background loops must not touch the cached snapshot the
    display route reads. Simulate the loop-side skip (no broker call) and
    confirm the display route still returns the identical snapshot object."""
    from backend.api.helpers import snapshot_gate

    snapshot = [{"symbol": "NIFTY", "qty": 1, "ltp": 24000.0}]
    cache = {"positions": list(snapshot)}  # stand-in for the DB/LKG read path

    async def _snapshot_fn():
        return cache["positions"]

    async def _broker_fn():
        raise AssertionError("broker must not be called while closed")

    # The four loops are skipped while closed; they must not write the display cache.
    # (No code path below mutates `cache`; this asserts the display read is pure.)
    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False):
        before, _ = await snapshot_gate.closed_hours_or_broker(
            "NSE", _snapshot_fn, _broker_fn, route_key="positions",
        )
        after, source = await snapshot_gate.closed_hours_or_broker(
            "NSE", _snapshot_fn, _broker_fn, route_key="positions",
        )

    assert before == after == snapshot, "display snapshot must be identical before and after the loop skip"
    assert source == "snapshot"


def test_snapshot_gate_does_not_import_or_reference_closed_window_loop_gate():
    """Structural guard: the display gate module must not depend on the new
    per-segment cached helper or on background.py. If it did, a change to the
    background loops could reach the closed-hours display path."""
    tree = ast.parse(_SNAPSHOT_GATE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    forbidden_modules = {"backend.api.background", "backend.api.algo.order_poll"}
    leaked = sorted(m for m in imported if m in forbidden_modules)
    assert not leaked, f"snapshot_gate.py must not import background-loop modules, found: {leaked}"

    source = _SNAPSHOT_GATE.read_text(encoding="utf-8")
    assert "is_segment_open_cached" not in source, (
        "snapshot_gate.py must not reference the background loop-gate helper; "
        "the closed-hours display path has its own gate (_any_segment_open)"
    )
