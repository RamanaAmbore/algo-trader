"""
Regression tests for the 2026-10-23 Kill / CANCEL_FAILED lifecycle fix
(.claude/PLAN.md, Wave 1 cluster 1A).

Three linked bugs:

1. Kill was keyed only by a `broker_order_id` snapshot read at click
   time. If chase has just placed a replacement order under a NEW
   broker_order_id in the 1-4s window around the Kill click, the
   broker cancel targets the dead old id, fails, and the row gets
   wrongly stamped CANCEL_FAILED while chase keeps chasing under the
   new id, unaware an operator tried to stop it.

   Fixed via `chase.mark_order_killed()` — an algo-order-id-keyed kill
   signal (survives a broker_order_id swap) that the chase loop checks
   every iteration (`_ch_loop_guards`) and immediately after every
   replacement (`_ch_post_replace_kill_check`), in addition to the
   pre-existing broker_order_id-keyed `is_killed()`.

2. `kill_chase`'s HTTP response always returned `ok: true`, even when
   the broker cancel genuinely failed or the row was already terminal
   at FILLED/REJECTED. Fixed: `ok` now reflects whether the kill
   actually succeeded.

3. A CANCEL_FAILED row could never be re-killed (`kill_chase` returned
   `already_terminal` for anything not OPEN). Fixed: the gate now also
   accepts CANCEL_FAILED, and a retry whose broker call ALSO fails
   force-resolves a row that predates the current trading session.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_kill_registries():
    """`chase._KILLED_ORDER_IDS` / `_KILLED_ALGO_ORDER_IDS` are
    module-level, process-lifetime dicts (by design — see their own
    docstrings). Every test below reuses the same AlgoOrder id (501),
    so a kill mark left behind by one test would silently leak into
    the next and misroute `_ch_loop_guards`'s kill check. Clear both
    before AND after every test in this file."""
    import backend.api.algo.chase as chase_mod
    with chase_mod._KILLED_LOCK:
        chase_mod._KILLED_ORDER_IDS.clear()
        chase_mod._KILLED_ALGO_ORDER_IDS.clear()
    yield
    with chase_mod._KILLED_LOCK:
        chase_mod._KILLED_ORDER_IDS.clear()
        chase_mod._KILLED_ALGO_ORDER_IDS.clear()


def _make_session_ctx(mock_row):
    """Async context-manager factory yielding a session whose execute()
    always resolves to `mock_row`. Patching `async_session` to this
    factory means EVERY `async with async_session() as s:` block in the
    route under test sees the same mocked row object — matching
    `kill_chase`'s real two-phase (gate-read, then re-read-and-dispatch)
    structure introduced by this fix.
    """
    mock_s = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=mock_row)
    mock_s.execute = AsyncMock(return_value=mock_result)
    mock_s.commit = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield mock_s

    return _ctx, mock_s


def _live_row(**overrides):
    row = MagicMock()
    row.id = 501
    row.mode = "live"
    row.status = "OPEN"
    row.account = "ZG0790"
    row.broker_order_id = "OLD_BROKER_ID"
    row.detail = ""
    row.created_at = datetime.now(timezone.utc)
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


async def _run_kill_chase(algo_order_id, session_ctx):
    from litestar import Request
    from backend.api.routes.orders import OrdersController

    mock_request = MagicMock(spec=Request)
    with patch("backend.api.database.async_session", session_ctx), \
         patch("backend.api.routes.orders.invalidate"), \
         patch("backend.api.algo.order_events.write_event", AsyncMock()):
        ctrl = OrdersController.__new__(OrdersController)
        return await OrdersController.kill_chase.fn(ctrl, algo_order_id, mock_request)


# ── Bug 1 — algo-order-id-keyed kill intent survives a broker_order_id swap ──

async def test_kill_marks_algo_order_id_before_resolving_broker_order_id():
    """`kill_chase` must call `chase.mark_order_killed(algo_order_id)`
    BEFORE dispatching the broker-level cancel, so a chase loop that has
    already cancelled-and-replaced under a NEW broker_order_id (one the
    Kill click never even read) still sees the kill intent on its very
    next guard check. Pre-fix, `kill_chase` only ever called
    `chase.mark_killed(broker_order_id)` — a signal keyed by the STALE
    id the click happened to read — so this assertion fails against the
    pre-fix code (no such call exists at all)."""
    import backend.api.algo.chase as chase_mod

    row = _live_row(broker_order_id="OLD_BROKER_ID")
    session_ctx, _ = _make_session_ctx(row)

    assert chase_mod.is_order_killed(row.id) is False

    with patch("backend.brokers.get_broker") as mock_get_broker:
        mock_broker = MagicMock()
        mock_broker.cancel_order = MagicMock(return_value=None)
        mock_get_broker.return_value = mock_broker
        with patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                    new=AsyncMock(side_effect=AssertionError("not reached for OPEN row"))):
            await _run_kill_chase(row.id, session_ctx)

    # The algo-order-id-level signal must now be set — this is the
    # signal that survives a broker_order_id swap chase might perform
    # concurrently with the click.
    assert chase_mod.is_order_killed(row.id) is True


async def test_chase_loop_honors_algo_order_kill_intent_under_a_new_broker_order_id():
    """Simulates the exact race: the operator's Kill click marks the
    algo-order-id-level intent (as `kill_chase` now does), but chase has
    ALREADY cancelled-and-replaced to a brand-new broker_order_id the
    click never read. `_ch_post_replace_kill_check` must still detect
    the kill via `is_order_killed(algo_order_id)` and terminate —
    pre-fix, this function only checked `is_killed(current_order_id)`
    (the broker_order_id-keyed signal), which can never match a
    broker_order_id it was never told about, so this would return
    False (no kill detected) against the pre-fix code."""
    from backend.api.algo.chase import (
        _ch_post_replace_kill_check, mark_order_killed, ChaseResult,
    )

    algo_order_id = 9001
    new_broker_order_id = "NEW_BROKER_ID_CHASE_JUST_PLACED"

    # Operator's kill click never read this id — only the algo-order-id
    # level signal is set, exactly as `kill_chase` now does first.
    mark_order_killed(algo_order_id)

    result = ChaseResult()
    cfg = MagicMock(variety="regular", exchange="NFO")

    with patch("backend.api.algo.chase._run", new=AsyncMock()) as mock_run, \
         patch("backend.api.algo.chase._emit_chase_terminal", new=AsyncMock()):
        detected = await _ch_post_replace_kill_check(
            "ZG0790", new_broker_order_id, cfg, result,
            "NIFTY25JULFUT", "BUY", 50, attempt=3,
            algo_order_id=algo_order_id, emit=lambda *a, **k: None,
        )

    assert detected is True, (
        "chase's post-replace kill check did not detect an operator "
        "kill issued against the algo-order id — it would keep "
        "chasing under the new broker_order_id, unaware"
    )
    assert result.status.value == "cancelled" or "cancel" in str(result.status).lower()
    # The new order must have actually been cancelled at the broker,
    # not just marked done locally.
    mock_run.assert_called_once()


# ── Bug 2 — `ok` reflects real success/failure ──────────────────────────

async def test_kill_chase_live_broker_cancel_failure_returns_ok_false():
    """A genuine broker cancel failure must surface as `ok: false` with
    the real error text — pre-fix, this endpoint always returned
    `ok: true` regardless, hiding the failure from the operator."""
    row = _live_row(broker_order_id="DEAD_ID")
    session_ctx, _ = _make_session_ctx(row)

    with patch("backend.brokers.get_broker") as mock_get_broker:
        mock_broker = MagicMock()
        mock_broker.cancel_order = MagicMock(
            side_effect=RuntimeError("order does not exist")
        )
        mock_get_broker.return_value = mock_broker
        result = await _run_kill_chase(row.id, session_ctx)

    assert result["ok"] is False, (
        "broker cancel genuinely failed but kill_chase reported ok=true "
        "— the exact false-success bug this fix closes"
    )
    assert result["err"] and "broker cancel failed" in result["err"]
    assert result["status"] == "CANCEL_FAILED"


async def test_kill_chase_already_filled_returns_ok_false_not_true():
    """A row that already reached FILLED cannot be killed — nothing
    was cancelled, so `ok` must be false, not a blanket true."""
    row = _live_row(status="FILLED")
    session_ctx, _ = _make_session_ctx(row)

    result = await _run_kill_chase(row.id, session_ctx)

    assert result["already_terminal"] is True
    assert result["status"] == "FILLED"
    assert result["ok"] is False


# ── Bug 3 — CANCEL_FAILED is re-killable ────────────────────────────────

async def test_cancel_failed_row_can_be_re_killed_not_already_terminal():
    """Pre-fix, `kill_chase` gated on `row.status != "OPEN"`, so a
    CANCEL_FAILED row always hit the `already_terminal` early return
    and NOTHING was ever retried — the broker was never called again.
    Post-fix, CANCEL_FAILED must reach the real dispatch path."""
    row = _live_row(status="CANCEL_FAILED", broker_order_id="STILL_RESTING_ID")
    session_ctx, _ = _make_session_ctx(row)

    with patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                new=AsyncMock(return_value=MagicMock(available=False, algo_status=None))), \
         patch("backend.brokers.get_broker") as mock_get_broker:
        mock_broker = MagicMock()
        mock_broker.cancel_order = MagicMock(return_value=None)
        mock_get_broker.return_value = mock_broker
        result = await _run_kill_chase(row.id, session_ctx)

    assert result.get("already_terminal") is not True, (
        "a CANCEL_FAILED row must be retried, not bounced as already_terminal"
    )
    mock_broker.cancel_order.assert_called_once()
    assert result["ok"] is True
    assert result["status"] == "CANCELLED"


async def test_cancel_failed_resolved_via_broker_truth_without_redundant_cancel():
    """When broker truth already shows the order genuinely CANCELLED
    (the earlier failed attempt actually landed, just never reflected
    onto the row), the retry must resolve from that truth directly
    instead of firing a second, redundant broker cancel call."""
    row = _live_row(status="CANCEL_FAILED", broker_order_id="ACTUALLY_GONE_ID")
    session_ctx, _ = _make_session_ctx(row)

    truth = MagicMock(available=True, algo_status="CANCELLED")
    with patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                new=AsyncMock(return_value=truth)), \
         patch("backend.brokers.get_broker") as mock_get_broker:
        result = await _run_kill_chase(row.id, session_ctx)

    mock_get_broker.assert_not_called()
    assert result["ok"] is True
    assert result["status"] == "CANCELLED"


async def test_cancel_failed_resolved_via_broker_truth_as_filled_is_not_ok():
    """When broker truth shows the order actually FILLED (operator's
    kill came too late), the response must say so honestly — ok=false,
    never a successful-kill claim."""
    row = _live_row(status="CANCEL_FAILED", broker_order_id="ACTUALLY_FILLED_ID")
    session_ctx, _ = _make_session_ctx(row)

    truth = MagicMock(available=True, algo_status="FILLED")
    with patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                new=AsyncMock(return_value=truth)):
        result = await _run_kill_chase(row.id, session_ctx)

    assert result["ok"] is False
    assert result["status"] == "FILLED"


async def test_stale_cancel_failed_force_resolves_when_retry_also_fails():
    """A CANCEL_FAILED row from a PRIOR trading session, whose retry
    cancel ALSO fails at the broker (the order has long since rolled
    off the broker's own day-order book), must force-resolve to
    CANCELLED rather than loop forever as an un-killable zombie row —
    the operator's only path to clear it, via the same Kill button."""
    stale_created = datetime.now(timezone.utc) - timedelta(days=2)
    row = _live_row(status="CANCEL_FAILED", broker_order_id="LONG_GONE_ID",
                     created_at=stale_created)
    session_ctx, _ = _make_session_ctx(row)

    with patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                new=AsyncMock(return_value=MagicMock(available=False, algo_status=None))), \
         patch("backend.brokers.get_broker") as mock_get_broker:
        mock_broker = MagicMock()
        mock_broker.cancel_order = MagicMock(
            side_effect=RuntimeError("order not found")
        )
        mock_get_broker.return_value = mock_broker
        result = await _run_kill_chase(row.id, session_ctx)

    assert result["ok"] is True, (
        "a stale CANCEL_FAILED row (prior trading session) with a "
        "failing retry must still resolve — it can never still be "
        "resting live from a previous day-order session"
    )
    assert result["status"] == "CANCELLED"
    assert "force-resolved" in (row.detail or "")


async def test_stale_cancel_failed_does_not_force_resolve_on_non_gone_error():
    """A stale (prior-session) CANCEL_FAILED row whose retry fails with
    an error that does NOT look like "order genuinely gone" (e.g. a
    parameter/variety mismatch — the exact shape of the pre-existing,
    separately-tracked AMO `variety="regular"` gap, see the deferred
    P3 list in .claude/PLAN.md) must NOT be force-resolved — forcing a
    row to CANCELLED on an ambiguous failure risks marking a still-live
    order as dead."""
    stale_created = datetime.now(timezone.utc) - timedelta(days=2)
    row = _live_row(status="CANCEL_FAILED", broker_order_id="MAYBE_STILL_LIVE_ID",
                     created_at=stale_created)
    session_ctx, _ = _make_session_ctx(row)

    with patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                new=AsyncMock(return_value=MagicMock(available=False, algo_status=None))), \
         patch("backend.brokers.get_broker") as mock_get_broker:
        mock_broker = MagicMock()
        mock_broker.cancel_order = MagicMock(
            side_effect=RuntimeError("invalid variety for this order")
        )
        mock_get_broker.return_value = mock_broker
        result = await _run_kill_chase(row.id, session_ctx)

    assert result["ok"] is False
    assert result["status"] == "CANCEL_FAILED"
    assert "force-resolved" not in (row.detail or "")


# ── Dispatch-race retry (advisor-flagged gap) — a cancel failing while ──
# chase is STILL ACTIVELY running the row must re-resolve the fresh
# broker_order_id and retry, instead of writing a false CANCEL_FAILED
# that the operator sees as "KILL FAILED" when chase just swapped ids.

async def test_dispatch_race_retries_after_refresh_reveals_new_broker_order_id():
    """Chase concurrently cancels-and-replaces mid-dispatch: the broker
    cancel for the STALE id the operator's read captured fails, but a
    session refresh reveals chase has already committed a NEW
    broker_order_id for this row, and chase is still actively running
    it. The retry must target the FRESH id — giving up here (the
    pre-fix behavior) would stamp CANCEL_FAILED while chase keeps
    chasing under the new id, unaware."""
    row = _live_row(status="OPEN", broker_order_id="OLD_ID")
    session_ctx, mock_s = _make_session_ctx(row)

    async def _refresh_side_effect(obj):
        obj.broker_order_id = "NEW_ID"
    mock_s.refresh = AsyncMock(side_effect=_refresh_side_effect)

    call_log = []

    def _cancel_side_effect(order_id, variety="regular"):
        call_log.append(order_id)
        if order_id == "OLD_ID":
            raise RuntimeError("order does not exist")
        return None

    with patch("backend.api.algo.chase.is_chase_active", return_value=True), \
         patch("backend.brokers.get_broker") as mock_get_broker:
        mock_broker = MagicMock()
        mock_broker.cancel_order = MagicMock(side_effect=_cancel_side_effect)
        mock_get_broker.return_value = mock_broker
        result = await _run_kill_chase(row.id, session_ctx)

    assert call_log == ["OLD_ID", "NEW_ID"], (
        f"must retry against the FRESH broker_order_id after a failed "
        f"cancel while chase is active, got calls: {call_log!r}"
    )
    assert result["ok"] is True
    assert result["status"] == "CANCELLED"


async def test_dispatch_race_pending_when_retry_also_fails_and_chase_still_active():
    """When even the retry against the fresh broker_order_id fails, and
    chase is STILL actively running this row, `kill_chase` must not
    write ANY terminal status — chase's own loop (now aware of the
    algo-order-level kill intent) owns finalizing it. A blind write
    here could stomp chase's own correct terminal write with a stale
    derived guess."""
    row = _live_row(status="OPEN", broker_order_id="OLD_ID")
    session_ctx, mock_s = _make_session_ctx(row)

    async def _refresh_side_effect(obj):
        obj.broker_order_id = "NEW_ID"
    mock_s.refresh = AsyncMock(side_effect=_refresh_side_effect)

    with patch("backend.api.algo.chase.is_chase_active", return_value=True), \
         patch("backend.brokers.get_broker") as mock_get_broker:
        mock_broker = MagicMock()
        mock_broker.cancel_order = MagicMock(
            side_effect=RuntimeError("still resting, cancel rejected")
        )
        mock_get_broker.return_value = mock_broker
        result = await _run_kill_chase(row.id, session_ctx)

    assert result.get("pending") is True
    assert result["ok"] is False
    assert row.status == "OPEN", (
        "must not write a terminal status while chase is still "
        "actively resolving this row"
    )
    mock_s.commit.assert_not_called()
