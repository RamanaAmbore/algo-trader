"""
Tests for Sprint 1b-i — chase.py event-queue observability wiring.

Covers:
  - `chase_modify` fires on cancel-and-replace attempts (attempt > 1
    only — the initial placement at attempt 1 is not a "replace")
  - `chase_cancel_confirmed` fires once a cancel is confirmed gone at
    the broker, BEFORE the replacement order is placed
  - `chase_exhausted` fires on max-attempts exhaustion
  - a `None` `algo_order_id` (recovery edge case) never calls
    `write_event` at all, for any of the three new kinds

Five quality dimensions:
  1. SSOT  — drives the real `chase_order()` / `_ch_exhaust_max_attempts()`
             functions, not reimplementations.
  2. Perf  — pure unit / mocked broker calls, no real network or DB
             (`_sync_algo_order_id` and `_emit_chase_terminal` mocked so
             no real session is opened).
  3. Stale — `write_event` is patched at its lazy-import source
             (`backend.api.algo.order_events.write_event`), matching
             exactly how chase.py imports it at call time (mirrors
             paper.py's own write_event pattern).
  4. Reuse — same fake-broker / patch scaffold as
             `test_chase_fill_accounting.py` /
             `test_chase_cancel_confirmation.py`.
  5. UX    — asserts exact kind + order_id + ordering per call, not
             just call count.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


# ── Unit-level: the three write-event helpers directly ─────────────────

@pytest.mark.asyncio
async def test_ch_write_order_event_noop_when_algo_order_id_none():
    """Generic helper — no AlgoOrder row to attach to → write_event is
    never called at all."""
    from backend.api.algo.chase import _ch_write_order_event

    with patch(
        "backend.api.algo.order_events.write_event", new_callable=AsyncMock,
    ) as mock_we:
        await _ch_write_order_event(None, "chase_modify", "msg", {"a": 1})

    mock_we.assert_not_called()


@pytest.mark.asyncio
async def test_ch_write_order_event_calls_write_event_when_id_present():
    from backend.api.algo.chase import _ch_write_order_event

    with patch(
        "backend.api.algo.order_events.write_event", new_callable=AsyncMock,
    ) as mock_we:
        await _ch_write_order_event(7, "chase_exhausted", "msg", {"a": 1})

    mock_we.assert_awaited_once_with(7, "chase_exhausted", "msg", {"a": 1})


@pytest.mark.asyncio
async def test_ch_write_chase_modify_event_skips_attempt_one():
    """Attempt 1 is the initial placement, not a cancel-and-replace —
    must NOT write a chase_modify event even with a real algo_order_id."""
    from backend.api.algo.chase import _ch_write_chase_modify_event

    with patch(
        "backend.api.algo.order_events.write_event", new_callable=AsyncMock,
    ) as mock_we:
        await _ch_write_chase_modify_event(7, 1, "BUY", 100, 105.0, "order_1")

    mock_we.assert_not_called()


@pytest.mark.asyncio
async def test_ch_write_chase_modify_event_fires_attempt_two_plus():
    from backend.api.algo.chase import _ch_write_chase_modify_event

    with patch(
        "backend.api.algo.order_events.write_event", new_callable=AsyncMock,
    ) as mock_we:
        await _ch_write_chase_modify_event(7, 2, "BUY", 60, 105.5, "order_2")

    mock_we.assert_awaited_once()
    args = mock_we.call_args.args
    assert args[0] == 7
    assert args[1] == "chase_modify"


@pytest.mark.asyncio
async def test_ch_write_cancel_confirmed_event_noop_without_algo_order_id():
    from backend.api.algo.chase import _ch_write_cancel_confirmed_event

    with patch(
        "backend.api.algo.order_events.write_event", new_callable=AsyncMock,
    ) as mock_we:
        await _ch_write_cancel_confirmed_event(None, "order_1", 2, 60)

    mock_we.assert_not_called()


@pytest.mark.asyncio
async def test_ch_write_cancel_confirmed_event_fires_with_algo_order_id():
    from backend.api.algo.chase import _ch_write_cancel_confirmed_event

    with patch(
        "backend.api.algo.order_events.write_event", new_callable=AsyncMock,
    ) as mock_we:
        await _ch_write_cancel_confirmed_event(7, "order_1", 2, 60)

    mock_we.assert_awaited_once()
    args = mock_we.call_args.args
    assert args[0] == 7
    assert args[1] == "chase_cancel_confirmed"


# ── Integration: full chase_order() loop ────────────────────────────────

class _FakeTwoAttemptBroker:
    """Attempt 1's order never fills (stays OPEN) until cancelled;
    attempt 2's replacement order fills immediately — forces exactly
    one cancel-and-replace cycle."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self._n = 0
        self.placed_qtys: list[int] = []
        self.cancel_calls = 0

    def normalise_qty(self, exchange, qty, lot_size):
        return qty

    def quote(self, keys):
        key = keys[0]
        return {key: {"depth": {
            "buy":  [{"price": 100.00, "quantity": 500}],
            "sell": [{"price": 100.10, "quantity": 500}],
        }}}

    def place_order(self, **kwargs):
        self._n += 1
        oid = f"order_{self._n}"
        placed_qty = int(kwargs["quantity"])
        self.placed_qtys.append(placed_qty)
        filled = placed_qty if self._n >= 2 else 0
        self.orders[oid] = {"placed_qty": placed_qty, "filled": filled, "cancelled": False}
        return oid

    def cancel_order(self, order_id, variety="regular", exchange=""):
        self.cancel_calls += 1
        o = self.orders.get(order_id)
        if o is not None:
            o["cancelled"] = True

    def order_status(self, order_id):
        o = self.orders.get(order_id)
        if o is None:
            return {}
        if o["cancelled"]:
            status = "CANCELLED"
        elif o["placed_qty"] > 0 and o["filled"] >= o["placed_qty"]:
            status = "COMPLETE"
        else:
            status = "OPEN"
        return {
            "status": status, "quantity": o["placed_qty"],
            "filled_quantity": o["filled"], "average_price": 100.05,
        }


class _FakeNeverFillBroker:
    """Every order placed never fills; cancel always succeeds instantly."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self._n = 0
        self.placed_qtys: list[int] = []

    def normalise_qty(self, exchange, qty, lot_size):
        return qty

    def quote(self, keys):
        key = keys[0]
        return {key: {"depth": {
            "buy":  [{"price": 100.00, "quantity": 500}],
            "sell": [{"price": 100.10, "quantity": 500}],
        }}}

    def place_order(self, **kwargs):
        self._n += 1
        oid = f"order_{self._n}"
        placed_qty = int(kwargs["quantity"])
        self.placed_qtys.append(placed_qty)
        self.orders[oid] = {"placed_qty": placed_qty, "filled": 0, "cancelled": False}
        return oid

    def cancel_order(self, order_id, variety="regular", exchange=""):
        o = self.orders.get(order_id)
        if o is not None:
            o["cancelled"] = True

    def order_status(self, order_id):
        o = self.orders.get(order_id)
        if o is None:
            return {}
        status = "CANCELLED" if o["cancelled"] else "OPEN"
        return {
            "status": status, "quantity": o["placed_qty"],
            "filled_quantity": o["filled"], "average_price": 100.05,
        }


def _chase_patches(broker):
    return (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._get_broker_registry", return_value=broker),
        patch("backend.api.algo.chase._sync_algo_order_id", new_callable=AsyncMock),
        patch("backend.api.algo.chase._emit_chase_terminal", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
async def test_chase_modify_and_cancel_confirmed_fire_in_order_on_replace():
    """A real cancel-and-replace cycle (attempt 2) must write exactly one
    `chase_cancel_confirmed` event (before the replacement order) followed
    by exactly one `chase_modify` event (after it placed) — attempt 1
    writes neither."""
    from backend.api.algo.chase import chase_order, ChaseConfig, ChaseStatus

    broker = _FakeTwoAttemptBroker()
    cfg = ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=6)

    p1, p2, p3, p4, p5, p6 = _chase_patches(broker)
    with p1, p2, p3, p4, p5, p6, patch(
        "backend.api.algo.order_events.write_event", new_callable=AsyncMock,
    ) as mock_we:
        result = await chase_order(
            account="ACC1", symbol="NIFTY24DECFUT",
            transaction_type="BUY", quantity=100, cfg=cfg,
            algo_order_id=55,
        )

    assert broker.placed_qtys == [100, 100]
    assert result.status == ChaseStatus.FILLED

    kinds = [c.args[1] for c in mock_we.call_args_list]
    assert kinds == ["chase_cancel_confirmed", "chase_modify"], (
        f"expected cancel_confirmed (before replace) then modify (after "
        f"replace) for the single cancel-and-replace cycle, got {kinds}"
    )
    assert all(c.args[0] == 55 for c in mock_we.call_args_list)

    cancel_payload = mock_we.call_args_list[0].args[3]
    assert cancel_payload["order_id"] == "order_1"
    modify_payload = mock_we.call_args_list[1].args[3]
    assert modify_payload["order_id"] == "order_2"
    assert modify_payload["attempt"] == 2


@pytest.mark.asyncio
async def test_chase_none_order_id_fires_zero_events():
    """Recovery edge case: algo_order_id=None must result in ZERO
    write_event calls across the whole chase, even across a real
    cancel-and-replace cycle."""
    from backend.api.algo.chase import chase_order, ChaseConfig

    broker = _FakeTwoAttemptBroker()
    cfg = ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=6)

    p1, p2, p3, p4, p5, p6 = _chase_patches(broker)
    with p1, p2, p3, p4, p5, p6, patch(
        "backend.api.algo.order_events.write_event", new_callable=AsyncMock,
    ) as mock_we:
        await chase_order(
            account="ACC1", symbol="NIFTY24DECFUT",
            transaction_type="BUY", quantity=100, cfg=cfg,
            algo_order_id=None,
        )

    mock_we.assert_not_called()


@pytest.mark.asyncio
async def test_chase_exhausted_fires_on_max_attempts():
    """A chase that never fills and exhausts max_attempts must write
    exactly one `chase_exhausted` event, as the LAST event of the run."""
    from backend.api.algo.chase import chase_order, ChaseConfig, ChaseStatus

    broker = _FakeNeverFillBroker()
    cfg = ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=2)

    p1, p2, p3, p4, p5, p6 = _chase_patches(broker)
    with p1, p2, p3, p4, p5, p6, patch(
        "backend.api.algo.order_events.write_event", new_callable=AsyncMock,
    ) as mock_we:
        result = await chase_order(
            account="ACC1", symbol="NIFTY24DECFUT",
            transaction_type="BUY", quantity=100, cfg=cfg,
            algo_order_id=77,
        )

    assert result.status == ChaseStatus.FAILED
    kinds = [c.args[1] for c in mock_we.call_args_list]
    assert kinds[-1] == "chase_exhausted", (
        f"expected chase_exhausted as the final event, got {kinds}"
    )
    assert kinds.count("chase_exhausted") == 1
    exhausted_call = mock_we.call_args_list[-1]
    assert exhausted_call.args[0] == 77
    assert exhausted_call.args[3]["max_attempts"] == 2


@pytest.mark.asyncio
async def test_chase_exhausted_none_algo_order_id_no_event():
    """Exhaustion with no algo_order_id (legacy caller) must not write
    any event at all, including chase_exhausted."""
    from backend.api.algo.chase import chase_order, ChaseConfig, ChaseStatus

    broker = _FakeNeverFillBroker()
    cfg = ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=2)

    p1, p2, p3, p4, p5, p6 = _chase_patches(broker)
    with p1, p2, p3, p4, p5, p6, patch(
        "backend.api.algo.order_events.write_event", new_callable=AsyncMock,
    ) as mock_we:
        result = await chase_order(
            account="ACC1", symbol="NIFTY24DECFUT",
            transaction_type="BUY", quantity=100, cfg=cfg,
            algo_order_id=None,
        )

    assert result.status == ChaseStatus.FAILED
    mock_we.assert_not_called()
