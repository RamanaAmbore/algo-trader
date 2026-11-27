"""
2026-11 real-money audit fix — coverage for how each of the four real
call sites of `diagnose_live_failure` (backend/api/algo/actions_preflight.py)
passes the new `is_close` keyword.

`is_close` must reflect a SERVER-VERIFIED close, never a raw unverified
client claim (the "Verified-intent propagation" invariant). Per call
site:

  - `_place_order_on_failure` (actions_live.py, backs the `place_order`
    DSL action) — always opens a fresh position, never a close.
    `is_close` must default to False.
  - `_close_position_on_failure` (actions_live.py, backs the
    `close_position` DSL action) — `_al_close_resolve_position` already
    server-verifies sign/magnitude/product against the real broker
    position before `chase_order()` is ever called. `is_close` must be
    True.
  - `_al_diagnose_chase_result` (actions_live.py, backs
    `chase_close_positions`) — `side` is derived directly from the real
    held position's own sign. `is_close` must be True.
  - `_ticket_handle_live_place_error` (orders_place.py, backs the
    manual `/ticket` route) — `data.intent` has already been mutated
    in place to the server-verified value by
    `_ticket_enforce_lot_and_fat_finger` for every F&O order with a
    known lot size (called unconditionally before this path can be
    reached). `is_close` must mirror `data.intent == "close"`.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Static source checks — cheap, deterministic guard matching this
# project's existing convention for these exact functions
# (see test_event_loop_blocking.py).
# ---------------------------------------------------------------------------

class TestStaticIsCloseWiring:
    def test_place_order_on_failure_never_claims_is_close(self):
        import backend.api.algo.actions_live as m
        src = inspect.getsource(m._place_order_on_failure)
        assert "is_close=True" not in src, (
            "place_order always opens a fresh position; is_close must "
            "default to False here"
        )
        assert "diagnose_live_failure" in src

    def test_close_position_on_failure_passes_is_close_true(self):
        import backend.api.algo.actions_live as m
        src = inspect.getsource(m._close_position_on_failure)
        assert "is_close=True" in src

    def test_al_diagnose_chase_result_passes_is_close_true(self):
        import backend.api.algo.actions_live as m
        src = inspect.getsource(m._al_diagnose_chase_result)
        assert "is_close=True" in src

    def test_ticket_handle_live_place_error_mirrors_verified_data_intent(self):
        import backend.api.routes.orders_place as m
        src = inspect.getsource(m._ticket_handle_live_place_error)
        # Must gate on the SAME condition `_ticket_enforce_lot_and_fat_
        # finger` gates its own data.intent mutation on (F&O exchange),
        # not just the raw field -- otherwise an unverified equity claim
        # could be mislabeled "verified" in the returned message.
        assert "data.exchange in _FO_EXCHANGES" in src
        assert 'getattr(data, "intent", None) == "close"' in src


# ---------------------------------------------------------------------------
# Behavioural checks — actually invoke each failure handler and assert
# the real kwarg diagnose_live_failure was called with.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_place_order_on_failure_calls_diagnose_with_default_is_close():
    import backend.api.algo.actions_live as actions_live

    diag_mock = AsyncMock(return_value="diagnosis")
    broker = MagicMock()

    with patch("backend.brokers.get_broker", return_value=broker), \
         patch.object(actions_live, "diagnose_live_failure", diag_mock), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert"):
        await actions_live._place_order_on_failure(
            RuntimeError("Insufficient funds"), {"agent_slug": "test-agent"},
            "ZG0790", "NIFTY26OCTFUT", "NFO", "BUY", 50, 100.0, "NRML",
        )

    diag_mock.assert_awaited_once()
    _, kwargs = diag_mock.call_args
    assert kwargs.get("is_close", False) is False, (
        "place_order never closes an existing position — is_close must "
        "not be True here"
    )
    assert kwargs.get("account") == "ZG0790"


@pytest.mark.asyncio
async def test_close_position_on_failure_calls_diagnose_with_is_close_true():
    import backend.api.algo.actions_live as actions_live

    diag_mock = AsyncMock(return_value="diagnosis")
    broker = MagicMock()
    agent = MagicMock(slug="test-agent")

    with patch.object(actions_live, "diagnose_live_failure", diag_mock), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert"):
        await actions_live._close_position_on_failure(
            RuntimeError("Insufficient funds"), agent, broker,
            "ZG0790", "NIFTY26OCTFUT", "NFO", "SELL", 50, 100.0, "NRML",
        )

    diag_mock.assert_awaited_once()
    args, kwargs = diag_mock.call_args
    assert args[0] is broker
    assert kwargs.get("is_close") is True, (
        "close_position's failure handler is only ever reached after "
        "_al_close_resolve_position already server-verified this is a "
        "genuine close — is_close must be True"
    )


@pytest.mark.asyncio
async def test_al_diagnose_chase_result_calls_diagnose_with_is_close_true():
    import backend.api.algo.actions_live as actions_live

    diag_mock = AsyncMock(return_value="diagnosis")
    broker = MagicMock()
    diag_order = {
        "exchange": "NFO", "symbol": "NIFTY26OCTFUT", "side": "SELL",
        "qty": 50, "order_type": "LIMIT", "product": "NRML",
        "price": 100.0, "variety": "regular",
    }

    with patch("backend.brokers.get_broker", return_value=broker), \
         patch.object(actions_live, "diagnose_live_failure", diag_mock):
        result = await actions_live._al_diagnose_chase_result("ZG0790", diag_order)

    assert result == "diagnosis"
    diag_mock.assert_awaited_once()
    args, kwargs = diag_mock.call_args
    assert args[0] is broker
    assert kwargs.get("is_close") is True, (
        "chase_close_positions derives `side` directly from the real "
        "held position's own sign -- is_close must be True"
    )
    assert kwargs.get("account") == "ZG0790"


def _ticket_data_stub(intent=None, exchange="NFO"):
    return MagicMock(
        exchange=exchange, product="NRML", price=100.0, variety="regular",
        source="ticket", intent=intent,
    )


@pytest.mark.asyncio
async def test_ticket_handle_live_place_error_true_when_intent_verified_close():
    from backend.api.routes import orders_place as m

    diag_mock = AsyncMock(return_value="diagnosis")

    with patch("backend.api.algo.actions.diagnose_live_failure", diag_mock), \
         patch("backend.api.routes.orders_helpers._broker_for"), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert"), \
         patch("backend.api.algo.agent_engine.record_manual_event", new=AsyncMock()), \
         patch("backend.api.routes.orders_helpers._record_rejection"), \
         patch("backend.api.routes.orders_helpers._maybe_send_breaker_alert"):
        with pytest.raises(Exception):
            await m._ticket_handle_live_place_error(
                _ticket_data_stub(intent="close"), "ZG0790", "NIFTY26OCTFUT",
                "SELL", 50, "LIMIT", RuntimeError("Insufficient funds"),
                "ZG0790:NFO:NIFTY26OCTFUT",
            )

    diag_mock.assert_awaited_once()
    _, kwargs = diag_mock.call_args
    assert kwargs.get("is_close") is True, (
        "data.intent == 'close' here is the server-VERIFIED value "
        "_ticket_enforce_lot_and_fat_finger already mutated in place -- "
        "the ticket failure handler must thread it through"
    )
    assert kwargs.get("account") == "ZG0790"


@pytest.mark.asyncio
async def test_ticket_handle_live_place_error_false_when_intent_not_close():
    from backend.api.routes import orders_place as m

    diag_mock = AsyncMock(return_value="diagnosis")

    with patch("backend.api.algo.actions.diagnose_live_failure", diag_mock), \
         patch("backend.api.routes.orders_helpers._broker_for"), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert"), \
         patch("backend.api.algo.agent_engine.record_manual_event", new=AsyncMock()), \
         patch("backend.api.routes.orders_helpers._record_rejection"), \
         patch("backend.api.routes.orders_helpers._maybe_send_breaker_alert"):
        with pytest.raises(Exception):
            await m._ticket_handle_live_place_error(
                _ticket_data_stub(intent=None), "ZG0790", "NIFTY26OCTFUT",
                "BUY", 50, "LIMIT", RuntimeError("Insufficient funds"),
                "ZG0790:NFO:NIFTY26OCTFUT",
            )

    diag_mock.assert_awaited_once()
    _, kwargs = diag_mock.call_args
    assert kwargs.get("is_close", False) is False


@pytest.mark.asyncio
async def test_ticket_handle_live_place_error_false_for_equity_even_with_intent_close():
    """
    `_ticket_enforce_lot_and_fat_finger` only mutates `data.intent` to
    the server-VERIFIED value for an F&O exchange with a known lot
    size -- it returns early for equity, leaving `data.intent`
    carrying whatever the client raw-claimed. is_close must therefore
    be False here even when the (unverified) claim is "close", so the
    returned message never asserts "verified" for an unverified claim.
    """
    from backend.api.routes import orders_place as m

    diag_mock = AsyncMock(return_value="diagnosis")

    with patch("backend.api.algo.actions.diagnose_live_failure", diag_mock), \
         patch("backend.api.routes.orders_helpers._broker_for"), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert"), \
         patch("backend.api.algo.agent_engine.record_manual_event", new=AsyncMock()), \
         patch("backend.api.routes.orders_helpers._record_rejection"), \
         patch("backend.api.routes.orders_helpers._maybe_send_breaker_alert"):
        with pytest.raises(Exception):
            await m._ticket_handle_live_place_error(
                _ticket_data_stub(intent="close", exchange="NSE"),
                "ZG0790", "RELIANCE", "SELL", 10, "LIMIT",
                RuntimeError("Insufficient funds"), "ZG0790:NSE:RELIANCE",
            )

    diag_mock.assert_awaited_once()
    _, kwargs = diag_mock.call_args
    assert kwargs.get("is_close", False) is False, (
        "an equity order's data.intent is never server-verified by "
        "_ticket_enforce_lot_and_fat_finger -- is_close must stay False "
        "even when the raw, unverified claim is 'close'"
    )
