"""
Tests for cancelling (abandoning) a held automated order —
`cancel_held_order` (order_release.py) and `POST /api/orders/held/{id}/cancel`
(orders_held.py).

New file rather than extending test_orders_retry_helpers.py (that file covers
the `/retry-template` helpers, a different surface) or
test_order_hold_repeated_rejection_release.py (that file is scoped to the
repeated-rejection RELEASE path specifically). Mirrors the exact mock/session
conventions established in test_order_hold_repeated_rejection_release.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo.order_hold import HoldCategory, hold_record


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
    return hold_record(
        HoldCategory.AGENT_ORDER, "repeated price rejection", "n/a", None,
        datetime.now(timezone.utc),
    )


def _expiry_close_hold_json() -> str:
    return hold_record(
        HoldCategory.EXPIRY_CLOSE, "expiry close", "CHASE_MED", None,
        datetime.now(timezone.utc),
    )


# ── cancel_held_order: success cases, two different hold categories ───────

@pytest.mark.asyncio
async def test_cancel_agent_order_hold_succeeds():
    """A chase row held after repeated rejection (status=HELD) is cancelled."""
    from backend.api.algo import order_release as m

    row = _held_row(hold_json=_agent_order_hold_json(), status="HELD")
    mock_session = _mock_session(row)
    mock_write_event = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.order_events.write_event", mock_write_event):
        result = await m.cancel_held_order(77, actor="operator")

    assert result == {"ok": True, "reason": "cancelled", "status": "CANCELLED"}
    assert row.status == "CANCELLED"
    assert row.hold_json is None
    assert "cancelled by operator" in row.detail
    mock_session.commit.assert_called_once()
    mock_write_event.assert_called_once()
    args, kwargs = mock_write_event.call_args
    assert args[0] == 77
    assert args[1] == "cancelled"
    assert "BUY" in args[2] and "NIFTY24APR25000CE" in args[2] and "50" in args[2]
    assert args[3] == {"actor": "operator"}


@pytest.mark.asyncio
async def test_cancel_expiry_close_hold_succeeds():
    """An expiry-close held order (status=HELD) is cancelled — same code path
    as agent_order, proving the HELD-status branch is category-agnostic."""
    from backend.api.algo import order_release as m

    row = _held_row(hold_json=_expiry_close_hold_json(), status="HELD",
                    symbol="CRUDEOIL26OCTFUT", exchange="MCX", transaction_type="SELL",
                    quantity=1)
    mock_session = _mock_session(row)

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock):
        result = await m.cancel_held_order(77, actor="operator")

    assert result == {"ok": True, "reason": "cancelled", "status": "CANCELLED"}
    assert row.status == "CANCELLED"
    assert row.hold_json is None


# test_cancel_template_exit_hold_succeeds_without_touching_filled_status
# removed (Bracket/Template-GTT removal, T3.1, 2026-11) — it covered
# `cancel_held_order`'s `category == "template_exit"` branch
# (order_release.py). That branch's code is left completely intact per
# the removal's own scope (only the 3 TEMPLATE_EXIT-specific functions
# and the enum member were removed, not this shared function) — it is
# now unreachable in practice, since nothing creates a `template_exit`
# hold anymore, but the brief does not ask for it to be stripped out.


# ── cancel_held_order: refusal cases ────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_refuses_when_row_missing():
    from backend.api.algo import order_release as m

    mock_session = _mock_session(None)

    with patch("backend.api.database.async_session", return_value=mock_session):
        result = await m.cancel_held_order(999, actor="operator")

    assert result == {"ok": False, "reason": "order not found", "status": ""}


@pytest.mark.asyncio
async def test_cancel_refuses_when_not_held_and_no_template_exit_hold():
    """A row that is neither status=HELD nor carrying a pending template_exit
    hold (e.g. a plain OPEN order with no hold_json at all) is refused —
    mirroring release_held_order's own analogous refusal shape."""
    from backend.api.algo import order_release as m

    row = _held_row(status="OPEN", hold_json=None)
    mock_session = _mock_session(row)

    with patch("backend.api.database.async_session", return_value=mock_session):
        result = await m.cancel_held_order(77, actor="operator")

    assert result == {"ok": False, "reason": "order is OPEN, not HELD", "status": "OPEN"}
    assert row.status == "OPEN"  # untouched


# ── Route: POST /api/orders/held/{id}/cancel ───────────────────────────────

@pytest.mark.asyncio
async def test_route_cancel_dispatches_and_returns_result():
    # Litestar route handlers are called via `Controller.method.fn(ctrl, ...)`
    # with `ctrl = Controller(owner=None)` — see
    # test_order_hold_repeated_rejection_release.py for the established
    # convention in this repo.
    from backend.api.routes.orders_held import HeldOrdersController

    mock_cancel = AsyncMock(
        return_value={"ok": True, "reason": "cancelled", "status": "CANCELLED"})
    ctrl = HeldOrdersController(owner=None)

    with patch("backend.api.algo.order_release.cancel_held_order", mock_cancel):
        result = await HeldOrdersController.cancel.fn(ctrl, order_id=77)

    assert result == {"ok": True, "reason": "cancelled", "status": "CANCELLED"}
    mock_cancel.assert_called_once_with(77, actor="operator")


@pytest.mark.asyncio
async def test_route_cancel_raises_409_with_refusal_reason():
    from litestar.exceptions import HTTPException
    from backend.api.routes.orders_held import HeldOrdersController

    mock_cancel = AsyncMock(
        return_value={"ok": False, "reason": "order is OPEN, not HELD", "status": "OPEN"})
    ctrl = HeldOrdersController(owner=None)

    with patch("backend.api.algo.order_release.cancel_held_order", mock_cancel):
        with pytest.raises(HTTPException) as exc_info:
            await HeldOrdersController.cancel.fn(ctrl, order_id=77)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "order is OPEN, not HELD"
