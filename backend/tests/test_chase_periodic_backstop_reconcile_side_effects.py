"""
Regression test for audit finding O5 (Wave 1, cluster 1F).

`_ch_periodic_broker_truth_check` and `_ch_backstop_reconcile_one` both
called `orders_helpers.py:apply_order_truth` directly — which
deliberately omits template-attach / ws-resubscribe side effects (see
its own docstring) — instead of the shared
`order_poll.py:reconcile_row_and_followup` wrapper every OTHER
automatic corrector (the open-order watchdog, the periodic order-poll
sweep, postback's follow-up verification) already uses. A fill caught
by either of chase's own checks therefore silently skipped
template-attach/auto-TP and ws-resubscribe — potentially leaving a
freshly-filled position with no SL/TP armed.

Fix: migrate both callers onto `reconcile_row_and_followup`.
`_ch_periodic_broker_truth_check` runs WHILE `chase_order()` has this
exact row marked active, so it must pass `owning_chase=True` (new
kwarg on the wrapper) to skip the wrapper's own `is_chase_active`
no-op gate — without this, the migration would silently turn the
periodic check into a permanent no-op. `_ch_backstop_reconcile_one`
runs AFTER chase has already cleared its active marker (in
`chase_order()`'s `finally`), so it needs no such bypass.

Also confirms no duplicate alert/attach fires when the row is already
FILLED by another writer before either check runs (idempotency via
`apply_order_truth`'s own no-op-on-no-change behavior).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo import chase
from backend.api.routes.orders_helpers import OrderTruth


def _db_session(row):
    """Mirrors test_order_poll.py's `_db_session` helper — supports both
    `.scalar_one_or_none()` (apply_order_truth's own fetch, and
    `_opo_postprocess`'s refetch) call shapes."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = row

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_periodic_check_fires_template_attach_on_a_caught_fill():
    """A fill caught by the periodic in-loop broker-truth check must
    fire template-attach + ws-resubscribe, matching every other
    automatic corrector."""
    fresh_row = SimpleNamespace(
        id=11, status="FILLED", broker_order_id="OID1",
        account="ZG0790", symbol="NIFTY25JULFUT", exchange="NFO",
        agent_id=None,
    )
    truth = OrderTruth(available=True, algo_status="FILLED",
                       average_price=105.0, raw_broker_status="COMPLETE")

    attach_calls: list = []
    subscribe_calls: list = []

    with patch(
        "backend.api.routes.orders_helpers.get_broker_order_truth",
        new=AsyncMock(return_value=truth),
    ), patch(
        "backend.api.routes.orders_helpers.apply_order_truth",
        new=AsyncMock(return_value=True),
    ), patch(
        "backend.api.database.async_session",
        return_value=_db_session(fresh_row),
    ), patch(
        "backend.api.routes.orders_place._maybe_fire_template_attach_for_reconcile",
        new=MagicMock(side_effect=lambda row: attach_calls.append(row)),
    ), patch(
        "backend.api.routes.orders._subscribe_filled_pairs",
        new=AsyncMock(side_effect=lambda pairs: subscribe_calls.append(pairs)),
    ), patch("backend.api.algo.order_poll.logger") as mock_log:
        result = await chase._ch_periodic_broker_truth_check(
            account="ZG0790", current_order_id="OID1", algo_order_id=11,
            attempt=3, symbol="NIFTY25JULFUT",
            cfg=chase.ChaseConfig(exchange="NFO"), agent_id=None,
        )

    assert result == "FILLED"
    assert attach_calls == [fresh_row], (
        "a fill caught by the periodic broker-truth check must fire "
        "template-attach, matching the watchdog/order-poll-sweep "
        "treatment every other automatic corrector gets."
    )
    assert subscribe_calls == [[("NIFTY25JULFUT", "NFO")]], (
        "a fill caught by the periodic broker-truth check must "
        "re-subscribe the ticker, matching every other automatic "
        "corrector."
    )
    mock_log.warning.assert_not_called(), (
        "a normal, self-resolving chase fill caught by the periodic "
        "check must NOT fire the generic order_correction alert — "
        "that would be a false alarm; chase's own in-loop fill/alert "
        "path is the correct notification channel for this outcome."
    )


@pytest.mark.asyncio
async def test_periodic_check_does_not_shortcircuit_into_a_permanent_noop():
    """Regression guard for the specific bug this migration could have
    introduced: the periodic check runs WHILE chase_order() has this
    row marked active via is_chase_active(). Without owning_chase=True
    threaded through correctly, reconcile_row_and_followup's own
    is_chase_active gate would make this check a permanent no-op even
    though a real correction is available."""
    fresh_row = SimpleNamespace(
        id=12, status="FILLED", broker_order_id="OID2",
        account="ZG0790", symbol="NIFTY25JULFUT", exchange="NFO",
        agent_id=None,
    )
    truth = OrderTruth(available=True, algo_status="FILLED",
                       average_price=50.0, raw_broker_status="COMPLETE")

    with patch(
        "backend.api.algo.chase.is_chase_active", return_value=True,
    ), patch(
        "backend.api.routes.orders_helpers.get_broker_order_truth",
        new=AsyncMock(return_value=truth),
    ), patch(
        "backend.api.routes.orders_helpers.apply_order_truth",
        new=AsyncMock(return_value=True),
    ), patch(
        "backend.api.database.async_session",
        return_value=_db_session(fresh_row),
    ), patch(
        "backend.api.routes.orders_place._maybe_fire_template_attach_for_reconcile",
    ), patch(
        "backend.api.routes.orders._subscribe_filled_pairs",
        new=AsyncMock(),
    ), patch("backend.api.algo.order_poll.logger"):
        result = await chase._ch_periodic_broker_truth_check(
            account="ZG0790", current_order_id="OID2", algo_order_id=12,
            attempt=3, symbol="NIFTY25JULFUT",
            cfg=chase.ChaseConfig(exchange="NFO"), agent_id=None,
        )

    assert result == "FILLED", (
        "is_chase_active()==True for THIS row must not block the "
        "periodic check's own correction — the row's active chase IS "
        "the caller; owning_chase=True must bypass that gate."
    )


@pytest.mark.asyncio
async def test_periodic_check_no_double_dispatch_when_already_filled_elsewhere():
    """If another writer already finalized the row to FILLED before
    this check runs, apply_order_truth's own no-op-on-no-change
    behavior must prevent a second attach/alert dispatch."""
    with patch(
        "backend.api.routes.orders_helpers.get_broker_order_truth",
        new=AsyncMock(return_value=OrderTruth(
            available=True, algo_status="FILLED",
            average_price=105.0, raw_broker_status="COMPLETE",
        )),
    ), patch(
        "backend.api.routes.orders_helpers.apply_order_truth",
        new=AsyncMock(return_value=False),  # already FILLED -> no-op
    ) as mock_apply, patch(
        "backend.api.routes.orders_place._maybe_fire_template_attach_for_reconcile",
    ) as attach_mock, patch(
        "backend.api.routes.orders._subscribe_filled_pairs",
        new=AsyncMock(),
    ) as subscribe_mock, patch("backend.api.algo.order_poll.logger"):
        result = await chase._ch_periodic_broker_truth_check(
            account="ZG0790", current_order_id="OID3", algo_order_id=13,
            attempt=3, symbol="NIFTY25JULFUT",
            cfg=chase.ChaseConfig(exchange="NFO"), agent_id=None,
        )

    assert result is None
    mock_apply.assert_awaited_once()
    attach_mock.assert_not_called()
    subscribe_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_backstop_reconcile_fires_template_attach_on_a_caught_fill():
    """A fill resolved by the ambiguous-abort backstop reconcile must
    also fire template-attach + ws-resubscribe."""
    stuck_row = SimpleNamespace(
        id=22, status="CANCEL_FAILED", broker_order_id="OID4",
        account="ZG0790", symbol="NIFTY25JULFUT", exchange="NFO",
        agent_id=None,
    )
    fresh_row = SimpleNamespace(
        id=22, status="FILLED", broker_order_id="OID4",
        account="ZG0790", symbol="NIFTY25JULFUT", exchange="NFO",
        agent_id=None,
    )
    truth = OrderTruth(available=True, algo_status="FILLED",
                       average_price=210.5, raw_broker_status="COMPLETE")

    attach_calls: list = []
    subscribe_calls: list = []

    with patch.object(chase, "_async_session", return_value=_db_session(stuck_row)), \
         patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
         ), patch(
            "backend.api.routes.orders_helpers.apply_order_truth",
            new=AsyncMock(return_value=True),
         ), patch(
            "backend.api.database.async_session",
            return_value=_db_session(fresh_row),
         ), patch(
            "backend.api.routes.orders_place._maybe_fire_template_attach_for_reconcile",
            new=MagicMock(side_effect=lambda row: attach_calls.append(row)),
         ), patch(
            "backend.api.routes.orders._subscribe_filled_pairs",
            new=AsyncMock(side_effect=lambda pairs: subscribe_calls.append(pairs)),
         ), patch.object(chase, "logger") as mock_logger:
        await chase._ch_backstop_reconcile_one(22)

    assert attach_calls == [fresh_row], (
        "a fill resolved by the ambiguous-abort backstop reconcile "
        "must fire template-attach, same as every other automatic "
        "corrector."
    )
    assert subscribe_calls == [[("NIFTY25JULFUT", "NFO")]]
    mock_logger.info.assert_called_once()
