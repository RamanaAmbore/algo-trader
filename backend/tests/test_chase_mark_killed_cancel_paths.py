"""
Regression tests for the 2026-10 chase `mark_killed()` gap fix.

Defect (found by a 4-agent council investigation, see .claude/PLAN.md):
none of the three existing cancel paths -- the admin `cancel_order` REST
route (`backend/api/routes/orders.py`), `_action_live_cancel_order`, or
`_action_live_cancel_all_orders` / `_al_cancel_broker_orders`
(`backend/api/algo/actions_live.py`) -- called chase's `mark_killed()`.

Chase's poll loop only treats a CANCELLED status as a real operator kill
when `is_killed(broker_order_id)` reports True (previously set ONLY by
the dedicated `kill_chase` route's `_rco_kill_live_mode`). Without
`mark_killed()` on these three paths, cancelling a chase-managed order
through any of them got silently undone: chase saw CANCELLED,
`is_killed()==False`, took the `cancelled_continue` branch, and
re-placed the order at a fresh price.

Each test below exercises the REAL cancel path (broker + DB mocked at
the boundary only), then feeds the same `broker_order_id` through
chase's own `_ch_poll_handle_cancelled` to prove the poll loop now
takes the "killed" branch, not "cancelled_continue".
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar.exceptions import HTTPException

from backend.api.algo.chase import (
    ChaseResult,
    ChaseStatus,
    _ch_poll_handle_cancelled,
    is_killed,
)

pytestmark = pytest.mark.asyncio


async def _assert_poll_loop_sees_kill(order_id: str) -> None:
    """Feed `order_id` through chase's real poll-cancel handler and assert
    it takes the operator-kill branch, never `cancelled_continue`."""
    assert is_killed(order_id) is True, (
        f"{order_id} was not recorded as killed -- the cancel path under "
        "test did not call chase.mark_killed()"
    )

    result = ChaseResult(order_id=order_id)
    with patch("backend.api.algo.chase._emit_chase_terminal", new=AsyncMock()):
        signal, remaining = _ch_poll_handle_cancelled(
            result, {"status_message": ""}, attempt=1, remaining_qty=5,
            current_order_id=order_id, symbol="NIFTY", transaction_type="BUY",
            quantity=5, algo_order_id=None, emit=lambda *a, **k: None,
        )
        # Let the fire-and-forget asyncio.create_task(_emit_chase_terminal())
        # scheduled inside the killed branch actually run (it's mocked, so
        # this just drains the task cleanly instead of leaving it pending).
        await asyncio.sleep(0)

    assert signal == "killed", (
        f"expected poll loop to recognize operator kill, got {signal!r} "
        "instead of 'killed' -- chase would silently re-place this order "
        "at a fresh price"
    )
    assert result.status == ChaseStatus.CANCELLED
    assert remaining == 5


def _fake_request():
    return SimpleNamespace(state=SimpleNamespace(is_demo=False))


# ---------------------------------------------------------------------------
# Path 1: admin DELETE /api/orders/{order_id} route (orders.py:cancel_order)
# ---------------------------------------------------------------------------

async def test_admin_cancel_order_route_marks_killed():
    from backend.api.routes.orders import OrdersController

    order_id = "TESTADMINKILL0001"
    broker = MagicMock()
    broker.cancel_order = MagicMock()
    ctrl = OrdersController(owner=None)

    fake_db_result = MagicMock()
    fake_db_result.scalar_one_or_none = MagicMock(return_value=None)
    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(return_value=fake_db_result)

    with patch("backend.api.routes.orders.is_admin_request", return_value=True), \
         patch("backend.api.routes.orders._broker_for", return_value=broker), \
         patch("backend.api.routes.orders.invalidate"), \
         patch("backend.api.database.async_session", return_value=fake_session):
        resp = await OrdersController.cancel_order.fn(
            ctrl, order_id=order_id, request=_fake_request(),
            account="ZG0790", variety="regular",
        )

    broker.cancel_order.assert_called_once_with(order_id, variety="regular")
    assert resp.order_id == order_id
    await _assert_poll_loop_sees_kill(order_id)


async def test_admin_cancel_order_route_mark_killed_survives_db_lookup_failure():
    """mark_killed() must still fire even if the exchange-lookup DB query
    inside the route fails (that lookup is already wrapped in its own
    try/except — this just confirms the two concerns are independent)."""
    from backend.api.routes.orders import OrdersController

    order_id = "TESTADMINKILL0002"
    broker = MagicMock()
    broker.cancel_order = MagicMock()
    ctrl = OrdersController(owner=None)

    with patch("backend.api.routes.orders.is_admin_request", return_value=True), \
         patch("backend.api.routes.orders._broker_for", return_value=broker), \
         patch("backend.api.routes.orders.invalidate"), \
         patch("backend.api.database.async_session", side_effect=RuntimeError("db down")):
        resp = await OrdersController.cancel_order.fn(
            ctrl, order_id=order_id, request=_fake_request(),
            account="ZG0790", variety="regular",
        )

    assert resp.order_id == order_id
    await _assert_poll_loop_sees_kill(order_id)


# ---------------------------------------------------------------------------
# Path 2: agent `cancel_order` DO-action (_action_live_cancel_order)
# ---------------------------------------------------------------------------

async def test_action_live_cancel_order_marks_killed():
    from backend.api.algo.actions_live import _action_live_cancel_order

    order_id = "TESTACTIONKILL0003"
    broker = MagicMock()
    broker.cancel_order = MagicMock()
    agent = SimpleNamespace(slug="test-agent")

    fake_result = MagicMock()
    fake_result.scalars.return_value.all.return_value = []
    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(return_value=fake_result)
    fake_session.commit = AsyncMock()

    with patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.api.database.async_session", return_value=fake_session):
        await _action_live_cancel_order(
            agent, {}, {"account": "ZG0790", "broker_order_id": order_id},
        )

    broker.cancel_order.assert_called_once_with(order_id, variety="regular")
    await _assert_poll_loop_sees_kill(order_id)


async def test_action_live_cancel_order_missing_fields_raises_before_broker_call():
    """Sanity guard: the existing required-field validation is untouched
    by this fix -- no mark_killed() call on a request that never reaches
    the broker."""
    from backend.api.algo.actions_live import _action_live_cancel_order

    agent = SimpleNamespace(slug="test-agent")
    with pytest.raises(ValueError):
        await _action_live_cancel_order(agent, {}, {"account": "", "broker_order_id": ""})


async def test_action_live_cancel_order_marks_killed_before_broker_call_not_after():
    """Ordering guard: mark_killed() must run BEFORE broker.cancel_order(),
    not after -- the surrounding `await run_in_executor(...)` yields the
    event loop while the cancel is in flight, so a chase poll landing
    during that exact window must already see is_killed()==True. A
    sequential pass/fail assertion (as in the test above) can't catch a
    regression that moves the mark_killed() call to AFTER the await --
    this test observes ordering directly via a side_effect on the mock."""
    from backend.api.algo.actions_live import _action_live_cancel_order

    order_id = "TESTORDERING0007"
    agent = SimpleNamespace(slug="test-agent")
    seen = {}

    def _cancel(oid, variety="regular"):
        seen["killed_at_call_time"] = is_killed(order_id)

    broker = MagicMock()
    broker.cancel_order = MagicMock(side_effect=_cancel)

    fake_result = MagicMock()
    fake_result.scalars.return_value.all.return_value = []
    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.execute = AsyncMock(return_value=fake_result)
    fake_session.commit = AsyncMock()

    with patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.api.database.async_session", return_value=fake_session):
        await _action_live_cancel_order(
            agent, {}, {"account": "ZG0790", "broker_order_id": order_id},
        )

    assert seen.get("killed_at_call_time") is True, (
        "mark_killed() must fire BEFORE broker.cancel_order(), not after "
        "-- a regression moving it post-await would reopen the race"
    )


# ---------------------------------------------------------------------------
# Path 3: `cancel_all_orders` fleet-wide path (_al_cancel_broker_orders,
# shared by the agent DO-action and the new POST /api/orders/cancel-all
# route via cancel_all_orders_scoped).
# ---------------------------------------------------------------------------

async def test_al_cancel_broker_orders_marks_killed_for_every_attempted_order():
    from backend.api.algo.actions_live import _al_cancel_broker_orders

    good_id = "TESTFLEETKILL0004"
    bad_id = "TESTFLEETKILL0005"

    broker = MagicMock()
    broker.account = "ZG0790"
    broker.orders = MagicMock(return_value=[
        {"order_id": good_id, "status": "OPEN", "variety": "regular",
         "tradingsymbol": "NIFTY", "transaction_type": "BUY"},
        {"order_id": bad_id, "status": "OPEN", "variety": "regular",
         "tradingsymbol": "NIFTY", "transaction_type": "BUY"},
    ])

    def _cancel(oid, variety="regular"):
        if oid == bad_id:
            raise RuntimeError("broker rejected cancel")

    broker.cancel_order = MagicMock(side_effect=_cancel)

    loop = asyncio.get_running_loop()
    cancelled, errors = await _al_cancel_broker_orders(broker, "", loop)

    assert cancelled == 1
    assert errors == 1
    # Both orders get marked killed -- including the one whose broker
    # cancel call raised. This is deliberate: mark_killed() fires BEFORE
    # the run_in_executor cancel attempt (not after/on-success-only),
    # because the await inside run_in_executor yields the event loop and
    # a chase poll could land and read is_killed() during that exact
    # window. Every order here was explicitly selected by the operator's
    # cancel-all scope, so failing toward "chase won't re-place" matches
    # that intent even when the broker call for one specific order
    # failed to land.
    await _assert_poll_loop_sees_kill(good_id)
    await _assert_poll_loop_sees_kill(bad_id)


async def test_al_cancel_broker_orders_listing_failure_counts_as_error_not_zero_zero():
    """A `broker.orders()` listing failure must surface as an error, not
    silently collapse to `(0, 0)` -- which reads identically to "nothing
    to cancel" and is especially misleading now that the new
    POST /api/orders/cancel-all route surfaces these counts directly to
    the operator right after they asked for a destructive action."""
    from backend.api.algo.actions_live import _al_cancel_broker_orders

    broker = MagicMock()
    broker.account = "ZG0790"
    broker.orders = MagicMock(side_effect=RuntimeError("broker unreachable"))

    loop = asyncio.get_running_loop()
    cancelled, errors = await _al_cancel_broker_orders(broker, "", loop)

    assert cancelled == 0
    assert errors == 1, (
        "a listing failure must increment errors, not read as a clean "
        "'(0, 0) nothing to cancel' result"
    )


async def test_al_cancel_broker_orders_marks_killed_before_broker_call_not_after():
    """Ordering guard (mirrors the single-order test above): mark_killed()
    must run BEFORE broker.cancel_order() for each order, not after."""
    from backend.api.algo.actions_live import _al_cancel_broker_orders

    order_id = "TESTORDERING0008"
    seen = {}

    broker = MagicMock()
    broker.account = "ZG0790"
    broker.orders = MagicMock(return_value=[
        {"order_id": order_id, "status": "OPEN", "variety": "regular",
         "tradingsymbol": "NIFTY", "transaction_type": "BUY"},
    ])

    def _cancel(oid, variety="regular"):
        seen["killed_at_call_time"] = is_killed(order_id)

    broker.cancel_order = MagicMock(side_effect=_cancel)

    loop = asyncio.get_running_loop()
    await _al_cancel_broker_orders(broker, "", loop)

    assert seen.get("killed_at_call_time") is True, (
        "mark_killed() must fire BEFORE broker.cancel_order() for this "
        "order, not after -- a regression moving it post-await would "
        "reopen the race"
    )


async def test_action_live_cancel_all_orders_wrapper_marks_killed_via_helper():
    """The agent DO-action wrapper (`_action_live_cancel_all_orders`) must
    reach the same mark_killed()-calling helper as the shared
    `cancel_all_orders_scoped` used by the new REST route."""
    from backend.api.algo.actions_live import _action_live_cancel_all_orders

    order_id = "TESTFLEETKILL0006"
    broker = MagicMock()
    broker.account = "ZG0790"
    broker.orders = MagicMock(return_value=[
        {"order_id": order_id, "status": "OPEN", "variety": "regular",
         "tradingsymbol": "NIFTY", "transaction_type": "BUY"},
    ])
    broker.cancel_order = MagicMock()

    agent = SimpleNamespace(slug="test-agent")

    with patch("backend.brokers.registry.all_brokers", return_value=[broker]):
        await _action_live_cancel_all_orders(agent, {}, {"scope": "total"})

    broker.cancel_order.assert_called_once()
    await _assert_poll_loop_sees_kill(order_id)
