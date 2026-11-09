"""
Closed-window broker-call gate for `_task_order_poll` (2026-10-28).

`_task_trail_stop`/`_task_oco_pair_watcher` — the other two loops this
file used to cover — were removed entirely (Bracket/Template-GTT
removal, T3.2, 2026-11), along with their own closed-window-gate tests
below. `_task_order_poll` is driven for exactly ONE iteration by
patching `asyncio.sleep` so the second sleep call raises `_StopLoop`.
The gate is controlled by patching `is_any_segment_open_cached` (the
blanket "any segment open" check the loop calls after
`_resolve_market_status_today()`), so no holiday/DB work runs.

Patch target: `_task_order_poll` does a lazy
`from backend.api.background import ...` inside the function body, so
patching the attribute on `backend.api.background` is picked up on
every entry.

`_task_funds_offhours` — no broker call at all, asserted in
`test_closed_window_broker_gating.py` — is not covered here either.

order_poll's open path is asserted against the real broker-snapshot
call (`_broker_order_snapshot`) and the per-row reconcile.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
