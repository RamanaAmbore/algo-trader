"""
Tests for releasing a chase row held after repeated price rejection
(`_ch_hold_on_repeated_rejection`, chase.py).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_session(row):
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


def _held_row(**extra) -> SimpleNamespace:
    base = dict(
        id=77, status="HELD", hold_json=None, detail="",
        account="ZG0790", symbol="NIFTY24APR25000CE", exchange="NFO",
        transaction_type="BUY", quantity=50, product="NRML",
        intent=None, filled_quantity=0,
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _agent_order_hold_json() -> str:
    from datetime import datetime, timezone
    from backend.api.algo.order_hold import HoldCategory, hold_record
    return hold_record(
        HoldCategory.AGENT_ORDER, "repeated price rejection", "n/a", None,
        datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_release_resumes_chase_with_rows_own_fields():
    from backend.api.algo import order_release as m

    row = _held_row(hold_json=_agent_order_hold_json(), intent=None)
    mock_session = _mock_session(row)
    mock_chase_order = AsyncMock()

    # NOTE: order_release.py does `from backend.api.database import
    # async_session` INSIDE each function body — patch the SOURCE
    # (backend.api.database.async_session), NOT order_release.async_session
    # (which doesn't exist as a module attribute and would raise
    # AttributeError on patch.object).
    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.chase.chase_order", mock_chase_order), \
         patch("backend.api.algo.chase._ch_mark_chase_active"), \
         patch("backend.api.algo.chase._ch_mark_chase_inactive"):
        result = await m.release_repeated_rejection_hold(77, actor="operator")

        assert result["ok"] is True
        assert row.status == "OPEN"
        assert row.hold_json is None
        mock_session.commit.assert_called_once()

        # The patches above must still be active when the fire-and-forget
        # resume task actually runs, or it calls the REAL chase_order
        # instead of the mock — asyncio.sleep(0) must stay inside this
        # `with` block, not after it.
        await asyncio.sleep(0)

        mock_chase_order.assert_called_once()
        kw = mock_chase_order.call_args.kwargs
        assert kw["account"] == "ZG0790"
        assert kw["symbol"] == "NIFTY24APR25000CE"
        assert kw["transaction_type"] == "BUY"
        assert kw["quantity"] == 50
        assert kw["algo_order_id"] == 77
        assert kw["already_filled"] == 0
        assert kw["cfg"].intent is None  # plain OPEN order — not forced to "close"


@pytest.mark.asyncio
async def test_release_resumes_a_close_intent_chase_with_close_preserved():
    from backend.api.algo import order_release as m

    row = _held_row(hold_json=_agent_order_hold_json(), intent="close",
                    transaction_type="SELL", filled_quantity=20, quantity=50)
    mock_session = _mock_session(row)
    mock_chase_order = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.chase.chase_order", mock_chase_order), \
         patch("backend.api.algo.chase._ch_mark_chase_active"), \
         patch("backend.api.algo.chase._ch_mark_chase_inactive"):
        result = await m.release_repeated_rejection_hold(77, actor="operator")

        assert result["ok"] is True
        await asyncio.sleep(0)

        kw = mock_chase_order.call_args.kwargs
        assert kw["cfg"].intent == "close"
        assert kw["transaction_type"] == "SELL"
        assert kw["already_filled"] == 20


@pytest.mark.asyncio
async def test_release_marks_chase_active_before_committing_open():
    """Regression test for the release-path race: _ch_mark_chase_active
    must be called BEFORE row.status is committed as OPEN, so the
    ~3s /chases/active reconcile sweep never sees an OPEN row with a
    stale rejected broker_order_id and no active-chase protection."""
    from backend.api.algo import order_release as m

    row = _held_row(hold_json=_agent_order_hold_json())
    mock_session = _mock_session(row)
    call_order = []

    def _mark_active(order_id):
        call_order.append("mark_active")

    async def _commit():
        call_order.append("commit")

    mock_session.commit = _commit

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.chase.chase_order", new_callable=AsyncMock), \
         patch("backend.api.algo.chase._ch_mark_chase_active", side_effect=_mark_active), \
         patch("backend.api.algo.chase._ch_mark_chase_inactive"):
        result = await m.release_repeated_rejection_hold(77, actor="operator")

    assert result["ok"] is True
    assert call_order == ["mark_active", "commit"]


@pytest.mark.asyncio
async def test_resume_chase_after_hold_unmarks_active_on_success_and_failure():
    from backend.api.algo import order_release as m

    with patch("backend.api.algo.chase.chase_order", new_callable=AsyncMock), \
         patch("backend.api.algo.chase._ch_mark_chase_inactive") as mock_inactive:
        await m._resume_chase_after_hold(
            77, "ZG0790", "NIFTY24APR25000CE", "NFO", "BUY", 50, "NRML", None, 0,
        )
    mock_inactive.assert_called_once_with(77)

    with patch("backend.api.algo.chase.chase_order",
               new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
         patch("backend.api.algo.chase._ch_mark_chase_inactive") as mock_inactive_fail:
        await m._resume_chase_after_hold(
            77, "ZG0790", "NIFTY24APR25000CE", "NFO", "BUY", 50, "NRML", None, 0,
        )
    mock_inactive_fail.assert_called_once_with(77)


@pytest.mark.asyncio
async def test_release_refuses_non_agent_order_category():
    from backend.api.algo import order_release as m
    from datetime import datetime, timezone
    from backend.api.algo.order_hold import HoldCategory, hold_record

    row = _held_row(hold_json=hold_record(
        HoldCategory.EXPIRY_CLOSE, "expiry close", "CHASE_MED", None,
        datetime.now(timezone.utc),
    ))
    mock_session = _mock_session(row)

    with patch("backend.api.database.async_session", return_value=mock_session):
        result = await m.release_repeated_rejection_hold(77, actor="operator")

    assert result["ok"] is False
    assert "not a repeated-rejection hold" in result["reason"]
    assert row.status == "HELD"


@pytest.mark.asyncio
async def test_release_refuses_when_not_held():
    from backend.api.algo import order_release as m

    row = _held_row(status="OPEN", hold_json=_agent_order_hold_json())
    mock_session = _mock_session(row)

    with patch("backend.api.database.async_session", return_value=mock_session):
        result = await m.release_repeated_rejection_hold(77, actor="operator")

    assert result["ok"] is False
    assert "not HELD" in result["reason"]


@pytest.mark.asyncio
async def test_release_refuses_when_row_missing():
    from backend.api.algo import order_release as m

    mock_session = _mock_session(None)

    with patch("backend.api.database.async_session", return_value=mock_session):
        result = await m.release_repeated_rejection_hold(999, actor="operator")

    assert result["ok"] is False
    assert "not found" in result["reason"]


@pytest.mark.asyncio
async def test_route_dispatches_agent_order_category_to_repeated_rejection_release():
    # Litestar route handlers are called via `Controller.method.fn(ctrl, ...)`
    # with `ctrl = Controller(owner=None)` — see
    # backend/tests/test_mcp_template_audit_events.py's
    # test_get_order_events_matches_rest_handler_shape_for_known_order for
    # the established convention in this repo.
    from backend.api.routes.orders_held import HeldOrdersController

    row = _held_row(hold_json=_agent_order_hold_json())
    mock_session = _mock_session(row)
    mock_release_repeated = AsyncMock(
        return_value={"ok": True, "reason": "released; chasing resumed", "status": "OPEN"})
    mock_release_held = AsyncMock()
    mock_release_template = AsyncMock()

    ctrl = HeldOrdersController(owner=None)

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.order_release.release_repeated_rejection_hold",
               mock_release_repeated), \
         patch("backend.api.algo.order_release.release_held_order", mock_release_held), \
         patch("backend.api.algo.order_release.release_template_exit", mock_release_template):
        result = await HeldOrdersController.release.fn(ctrl, order_id=77)

    assert result["ok"] is True
    mock_release_repeated.assert_called_once_with(77, actor="operator")
    mock_release_held.assert_not_called()
    mock_release_template.assert_not_called()


@pytest.mark.asyncio
async def test_route_dispatches_other_categories_to_generic_release_held_order():
    from backend.api.routes.orders_held import HeldOrdersController
    from datetime import datetime, timezone
    from backend.api.algo.order_hold import HoldCategory, hold_record

    row = _held_row(hold_json=hold_record(
        HoldCategory.EXPIRY_CLOSE, "expiry close", "CHASE_MED", None,
        datetime.now(timezone.utc),
    ))
    mock_session = _mock_session(row)
    mock_release_repeated = AsyncMock()
    mock_release_held = AsyncMock(
        return_value={"ok": True, "reason": "released; chasing", "status": "OPEN"})
    mock_release_template = AsyncMock()

    ctrl = HeldOrdersController(owner=None)

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.order_release.release_repeated_rejection_hold",
               mock_release_repeated), \
         patch("backend.api.algo.order_release.release_held_order", mock_release_held), \
         patch("backend.api.algo.order_release.release_template_exit", mock_release_template):
        result = await HeldOrdersController.release.fn(ctrl, order_id=77)

    assert result["ok"] is True
    mock_release_held.assert_called_once_with(77, actor="operator")
    mock_release_repeated.assert_not_called()
    mock_release_template.assert_not_called()
