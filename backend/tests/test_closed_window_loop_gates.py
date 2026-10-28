"""
Closed-window broker-call gates for the four background loops that used to
poll the broker around the clock (2026-10-28).

Each loop is driven for exactly ONE iteration by patching `asyncio.sleep` so
the second sleep call raises `_StopLoop`. The gate is controlled by patching
`is_any_segment_open_cached` (the blanket "any segment open" check the loops
call after `_resolve_market_status_today()`), so no holiday/DB work runs.

Patch targets (resolved at each loop's own import site):
  - `_task_trail_stop`, `_task_oco_pair_watcher`: module-level name in
    `backend.api.background` (looked up at call time).
  - `_task_order_poll`: lazy `from backend.api.background import ...` inside
    the function body, so patching the attribute on `backend.api.background`
    is picked up on every entry.
  - `_task_funds_offhours`: no broker call at all, asserted in
    `test_closed_window_broker_gating.py`.

The DB sweep is the first thing the open-path touches in trail/OCO, so
"async_session entered" is the observable proof that the gate let the loop
proceed. order_poll's open path is asserted against the real broker-snapshot
call (`_broker_order_snapshot`) and the per-row reconcile.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


class _StopLoop(BaseException):
    """Raised from the patched sleep to end an infinite task loop after one pass."""


def _stop_on_second_sleep():
    calls = {"n": 0}

    async def _fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise _StopLoop()

    return _fake_sleep


class _FakeResult:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, counter):
        self._counter = counter

    async def __aenter__(self):
        self._counter["entered"] += 1
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_a, **_k):
        self._counter["executed"] += 1
        return _FakeResult([])

    async def commit(self):
        return None


def _session_factory(counter):
    def _factory(*_a, **_k):
        return _FakeSession(counter)
    return _factory


# ---------------------------------------------------------------------------
# _task_trail_stop
# ---------------------------------------------------------------------------

async def test_trail_stop_skips_db_and_broker_when_all_segments_closed():
    import backend.api.background as bg

    counter = {"entered": 0, "executed": 0}
    resolve = AsyncMock(return_value={})
    ltp = AsyncMock(side_effect=AssertionError("broker.ltp must not be called while closed"))

    with patch("backend.api.background._resolve_market_status_today", resolve), \
         patch("backend.api.background.is_any_segment_open_cached", return_value=False), \
         patch("backend.api.database.async_session", new=_session_factory(counter)), \
         patch("asyncio.sleep", new=_stop_on_second_sleep()):
        with pytest.raises(_StopLoop):
            await bg._task_trail_stop()

    assert resolve.await_count >= 1, "trail-stop must consult the once-per-day status cache"
    assert counter["entered"] == 0, "trail-stop must not open a DB session while closed"
    assert ltp.await_count == 0


async def test_trail_stop_proceeds_to_db_sweep_when_any_segment_open():
    import backend.api.background as bg

    counter = {"entered": 0, "executed": 0}
    with patch("backend.api.background._resolve_market_status_today", AsyncMock(return_value={})), \
         patch("backend.api.background.is_any_segment_open_cached", return_value=True), \
         patch("backend.api.database.async_session", new=_session_factory(counter)), \
         patch("asyncio.sleep", new=_stop_on_second_sleep()):
        with pytest.raises(_StopLoop):
            await bg._task_trail_stop()

    assert counter["entered"] >= 1, "trail-stop must run its DB sweep when a segment is open"


# ---------------------------------------------------------------------------
# _task_oco_pair_watcher
# ---------------------------------------------------------------------------

async def test_oco_watcher_skips_db_and_broker_when_all_segments_closed():
    import backend.api.background as bg

    counter = {"entered": 0, "executed": 0}
    gtts_map = AsyncMock(side_effect=AssertionError("get_gtts must not be called while closed"))

    with patch("backend.api.background._resolve_market_status_today", AsyncMock(return_value={})), \
         patch("backend.api.background.is_any_segment_open_cached", return_value=False), \
         patch("backend.api.background._oco_build_gtts_map", gtts_map), \
         patch("backend.api.database.async_session", new=_session_factory(counter)), \
         patch("asyncio.sleep", new=_stop_on_second_sleep()):
        with pytest.raises(_StopLoop):
            await bg._task_oco_pair_watcher()

    assert counter["entered"] == 0, "OCO watcher must not open a DB session while closed"
    assert gtts_map.await_count == 0, "OCO watcher must not fetch broker GTTs while closed"


async def test_oco_watcher_proceeds_to_db_sweep_when_any_segment_open():
    import backend.api.background as bg

    counter = {"entered": 0, "executed": 0}
    with patch("backend.api.background._resolve_market_status_today", AsyncMock(return_value={})), \
         patch("backend.api.background.is_any_segment_open_cached", return_value=True), \
         patch("backend.api.database.async_session", new=_session_factory(counter)), \
         patch("asyncio.sleep", new=_stop_on_second_sleep()):
        with pytest.raises(_StopLoop):
            await bg._task_oco_pair_watcher()

    assert counter["entered"] >= 1, "OCO watcher must run its DB sweep when a segment is open"


# ---------------------------------------------------------------------------
# _task_order_poll  (lazy import of the gate from backend.api.background)
# ---------------------------------------------------------------------------

def _order_row(**kw):
    kw.setdefault("id", 7)
    kw.setdefault("status", "OPEN")
    kw.setdefault("broker_order_id", "BO7")
    kw.setdefault("account", "ZG0790")
    return SimpleNamespace(**kw)


async def test_order_poll_skips_broker_snapshot_when_all_segments_closed():
    import backend.api.algo.order_poll as op

    candidates = AsyncMock(return_value=[_order_row()])
    snapshot = AsyncMock(side_effect=AssertionError("broker snapshot must not be fetched while closed"))
    reconcile = AsyncMock(return_value=None)

    with patch("backend.shared.helpers.utils.is_engine_idle", return_value=False), \
         patch("backend.api.background._resolve_market_status_today", AsyncMock(return_value={})), \
         patch("backend.api.background.is_any_segment_open_cached", return_value=False), \
         patch.object(op, "_order_poll_candidate_rows", candidates), \
         patch("backend.api.routes.orders_helpers._broker_order_snapshot", snapshot), \
         patch.object(op, "reconcile_row_and_followup", reconcile), \
         patch.object(op, "_order_poll_interval_seconds", return_value=1), \
         patch("asyncio.sleep", new=_stop_on_second_sleep()):
        with pytest.raises(_StopLoop):
            await op._task_order_poll()

    assert candidates.await_count == 0, "order-poll must not scan candidate rows while closed"
    assert snapshot.await_count == 0, "order-poll must not fetch the broker order book while closed"
    assert reconcile.await_count == 0


async def test_order_poll_fetches_broker_book_and_reconciles_when_open():
    import backend.api.algo.order_poll as op

    row = _order_row()
    candidates = AsyncMock(return_value=[row])
    snapshot = AsyncMock(return_value={"BO7": "FILLED"})
    reconcile = AsyncMock(return_value=None)

    with patch("backend.shared.helpers.utils.is_engine_idle", return_value=False), \
         patch("backend.api.background._resolve_market_status_today", AsyncMock(return_value={})), \
         patch("backend.api.background.is_any_segment_open_cached", return_value=True), \
         patch.object(op, "_order_poll_candidate_rows", candidates), \
         patch("backend.api.routes.orders_helpers._broker_order_snapshot", snapshot), \
         patch.object(op, "reconcile_row_and_followup", reconcile), \
         patch.object(op, "_order_poll_interval_seconds", return_value=1), \
         patch("asyncio.sleep", new=_stop_on_second_sleep()):
        with pytest.raises(_StopLoop):
            await op._task_order_poll()

    assert candidates.await_count == 1, "order-poll must scan candidates when a segment is open"
    assert snapshot.await_count == 1, "order-poll must fetch the broker book when a segment is open"
    assert reconcile.await_count == 1, "order-poll must reconcile the candidate row when open"
