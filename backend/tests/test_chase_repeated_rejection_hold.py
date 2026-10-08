"""
Tests for the 2026-10 "repeated price rejection → HELD" fix.

Operator design input: "If the rejection repeats, hold the order and
let the operator take action (cancel or otherwise intervene) instead
of continuing to auto-retry." Concretely: a chase's own price-shaped
REJECTED retry (`_ch_poll_handle_rejected`'s `rejected_continue` path,
unchanged by this fix — see `test_chase_recoverable_errors.py`) is now
bounded to ONE retry: if the SAME chase gets a SECOND consecutive
price-shaped REJECTED, it stops auto-retrying and reuses the EXISTING
held-order mechanism (`AlgoOrder.status == "HELD"` + `hold_json`,
surfaced by `GET /api/orders/held`, released via
`POST /api/orders/held/{id}/release`) instead of inventing a new status
or UI surface.

Covers:
  - `_ch_handle_poll_signal`'s counter-based escalation: first
    'rejected_continue' retries normally; a SECOND one in a row holds
    instead (`done=True`, counter reset); any non-reject signal resets
    the counter so the "consecutive" chain can't straddle unrelated
    events.
  - `_ch_hold_on_repeated_rejection`: writes `status='HELD'` +
    `hold_json` (category `agent_order`), fires the standard
    `send_order_failure_alert`, writes a `held` timeline event, and
    pops `_CH_PRE_FILL_NET_QTY` so nothing leaks.
  - End-to-end `chase_order()` loop: two consecutive price-shaped
    REJECTED polls → row HELD, exactly 2 orders placed (never a 3rd),
    alert fires.
  - Regression: ONE rejection followed by a fill on the retry still
    ends FILLED (not held) — proves the counter doesn't over-trigger.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _cfg(max_attempts=5):
    from backend.api.algo.chase import ChaseConfig
    return ChaseConfig(interval_seconds=0, max_attempts=max_attempts, exchange="NFO")


# ─────────────────────────────────────────────────────────────────────────
# _ch_handle_poll_signal — consecutive-rejection counter + escalation
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_rejected_continue_retries_normally():
    from backend.api.algo.chase import ChaseResult, _ch_handle_poll_signal

    result = ChaseResult()
    emit = MagicMock()
    with patch("asyncio.sleep", new_callable=AsyncMock):
        done, new_id, new_count = await _ch_handle_poll_signal(
            "rejected_continue", "OID1", _cfg(), "NIFTY25CE",
            consecutive_price_rejections=0,
            account="ZG0790", transaction_type="BUY", quantity=50,
            algo_order_id=None, result=result, emit=emit,
        )

    assert done is False
    assert new_id is None  # reset so the loop places a fresh order
    assert new_count == 1
    assert result.status != "held"


@pytest.mark.asyncio
async def test_second_consecutive_rejected_continue_holds_instead_of_retrying():
    from backend.api.algo.chase import ChaseResult, ChaseStatus, _ch_handle_poll_signal

    result = ChaseResult()
    emit = MagicMock()
    with patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
               MagicMock()):
        done, new_id, new_count = await _ch_handle_poll_signal(
            "rejected_continue", "OID2", _cfg(), "NIFTY25CE",
            consecutive_price_rejections=1,  # already rejected once in a row
            account="ZG0790", transaction_type="BUY", quantity=50,
            algo_order_id=None, result=result, emit=emit,
        )

    assert done is True, "second consecutive rejection must end the loop (hold)"
    assert new_count == 0
    assert result.status == ChaseStatus.HELD
    emit.assert_called_once()
    assert emit.call_args[0][0] == "chase_held"


@pytest.mark.asyncio
async def test_non_reject_signal_resets_the_consecutive_counter():
    """A benign poll (signal=None, e.g. a partial fill still OPEN)
    between two otherwise-unrelated rejections must reset the counter
    — only BACK-TO-BACK rejections count as 'consecutive'."""
    from backend.api.algo.chase import ChaseResult, _ch_handle_poll_signal

    result = ChaseResult()
    emit = MagicMock()
    done, new_id, new_count = await _ch_handle_poll_signal(
        None, "OID3", _cfg(), "NIFTY25CE",
        consecutive_price_rejections=1,
        account="ZG0790", transaction_type="BUY", quantity=50,
        algo_order_id=None, result=result, emit=emit,
    )
    assert done is False
    assert new_count == 0


@pytest.mark.asyncio
async def test_cancelled_continue_resets_the_consecutive_counter():
    """A broker/external cancel is not a rejection — must not count
    toward, and must reset, the consecutive-rejection chain."""
    from backend.api.algo.chase import ChaseResult, _ch_handle_poll_signal

    result = ChaseResult()
    emit = MagicMock()
    with patch("asyncio.sleep", new_callable=AsyncMock):
        done, new_id, new_count = await _ch_handle_poll_signal(
            "cancelled_continue", "OID4", _cfg(), "NIFTY25CE",
            consecutive_price_rejections=1,
            account="ZG0790", transaction_type="BUY", quantity=50,
            algo_order_id=None, result=result, emit=emit,
        )
    assert done is False
    assert new_id is None
    assert new_count == 0


@pytest.mark.asyncio
async def test_filled_killed_rejected_always_done_with_zero_count():
    from backend.api.algo.chase import ChaseResult, _ch_handle_poll_signal

    for sig in ("filled", "killed", "rejected"):
        result = ChaseResult()
        done, new_id, new_count = await _ch_handle_poll_signal(
            sig, "OID5", _cfg(), "NIFTY25CE",
            consecutive_price_rejections=1,
            account="ZG0790", transaction_type="BUY", quantity=50,
            algo_order_id=None, result=result, emit=MagicMock(),
        )
        assert done is True
        assert new_count == 0


# ─────────────────────────────────────────────────────────────────────────
# _ch_hold_on_repeated_rejection — DB write + alert + timeline event
# ─────────────────────────────────────────────────────────────────────────

def _mock_session(row):
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


@pytest.mark.asyncio
async def test_hold_writes_held_status_and_agent_order_category():
    from backend.api.algo import chase as m
    from backend.api.algo.order_hold import parse_hold_record

    mock_row = SimpleNamespace(id=42, status="OPEN", detail="", hold_json=None)
    mock_session = _mock_session(mock_row)
    result = m.ChaseResult()
    mock_alert = MagicMock()
    mock_event = AsyncMock()

    with patch.object(m, "_async_session", return_value=mock_session), \
         patch.object(m, "_ch_write_order_event", mock_event), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert):
        out = await m._ch_hold_on_repeated_rejection(
            result, "ZG0790", "NIFTY25CE", "BUY", 50,
            "OID9", _cfg(), 42, MagicMock(),
        )

    assert out.status == m.ChaseStatus.HELD
    assert mock_row.status == "HELD"
    rec = parse_hold_record(mock_row.hold_json)
    assert rec is not None
    assert rec["category"] == "agent_order"
    mock_session.commit.assert_called_once()
    mock_alert.assert_called_once()
    assert "repeated price rejection" in mock_alert.call_args.kwargs["error"].lower()
    mock_event.assert_called_once()
    assert mock_event.call_args[0][1] == "held"


@pytest.mark.asyncio
async def test_hold_never_overwrites_an_already_final_row():
    """Defensive guard: if the row somehow already finalized to
    FILLED/REJECTED before this fires (race), the hold write must not
    clobber it."""
    from backend.api.algo import chase as m

    mock_row = SimpleNamespace(id=43, status="FILLED", detail="", hold_json=None)
    mock_session = _mock_session(mock_row)
    result = m.ChaseResult()

    with patch.object(m, "_async_session", return_value=mock_session), \
         patch.object(m, "_ch_write_order_event", new_callable=AsyncMock), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", MagicMock()):
        await m._ch_hold_on_repeated_rejection(
            result, "ZG0790", "NIFTY25CE", "BUY", 50,
            "OID10", _cfg(), 43, MagicMock(),
        )

    assert mock_row.status == "FILLED"
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_hold_pops_pre_fill_net_qty_leak():
    from backend.api.algo import chase as m

    m._CH_PRE_FILL_NET_QTY[44] = 25.0
    mock_row = SimpleNamespace(id=44, status="OPEN", detail="", hold_json=None)
    mock_session = _mock_session(mock_row)
    result = m.ChaseResult()

    with patch.object(m, "_async_session", return_value=mock_session), \
         patch.object(m, "_ch_write_order_event", new_callable=AsyncMock), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", MagicMock()):
        await m._ch_hold_on_repeated_rejection(
            result, "ZG0790", "NIFTY25CE", "BUY", 50,
            "OID11", _cfg(), 44, MagicMock(),
        )

    assert 44 not in m._CH_PRE_FILL_NET_QTY


# ─────────────────────────────────────────────────────────────────────────
# End-to-end: chase_order() holds on the second consecutive rejection
# ─────────────────────────────────────────────────────────────────────────

def _make_depth(bid: float = 100.00, ask: float = 100.05) -> dict:
    return {"buy": [{"price": bid, "quantity": 50}],
            "sell": [{"price": ask, "quantity": 50}]}


@pytest.mark.asyncio
async def test_two_consecutive_rejections_hold_no_third_order():
    from backend.api.algo import chase as m

    algo_order_id = 998801
    mock_row = SimpleNamespace(
        id=algo_order_id, status="OPEN", hold_json=None,
        account="ZG0790", symbol="NIFTY24APR25000CE", exchange="NFO",
        transaction_type="BUY", detail="",
    )
    placed_prices: list[float] = []

    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return f"order_{len(placed_prices)}"

    async def _fake_run(fn, *args):
        if fn is m._get_depth:
            return _make_depth()
        if fn is m._place_order:
            return _fake_place(*args)
        if fn is m._order_status:
            return {"status": "REJECTED",
                    "status_message": "price out of circuit limit",
                    "filled_quantity": 0, "average_price": 0}
        if fn is m._cancel_order:
            return None
        return None

    mock_session = MagicMock()
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = mock_row
    mock_async_session = AsyncMock()
    mock_async_session.__aenter__ = AsyncMock(return_value=mock_async_session)
    mock_async_session.__aexit__ = AsyncMock(return_value=False)
    mock_async_session.execute = AsyncMock(return_value=_result)
    mock_async_session.commit = AsyncMock()

    mock_alert = MagicMock()
    mock_event = AsyncMock()

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch.object(m, "_run", side_effect=_fake_run),
        patch.object(m, "_async_session", return_value=mock_async_session),
        patch.object(m, "_ch_seed_pre_fill_net_qty", new_callable=AsyncMock),
        patch.object(m, "_ch_post_replace_kill_check", return_value=False),
        patch.object(m, "_tick_size_sync", return_value=0.05),
        patch.object(m, "_ch_write_order_event", mock_event),
        patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = m.ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=10)
        result = await m.chase_order(
            account="ZG0790", symbol="NIFTY24APR25000CE",
            transaction_type="BUY", quantity=50, cfg=cfg,
            algo_order_id=algo_order_id,
        )

    assert len(placed_prices) == 2, (
        f"expected exactly 2 orders placed (initial + ONE reprice retry, "
        f"then hold — never a 3rd), got {len(placed_prices)}: {placed_prices}"
    )
    assert result.status == m.ChaseStatus.HELD
    assert mock_row.status == "HELD"
    mock_alert.assert_called_once()
    assert m.is_chase_active(algo_order_id) is False


@pytest.mark.asyncio
async def test_single_rejection_then_fill_does_not_hold():
    """Regression: the counter must not over-trigger — exactly ONE
    price-shaped rejection followed by a genuine fill on the retry must
    still end FILLED, not HELD."""
    from backend.api.algo import chase as m

    algo_order_id = 998802
    mock_row = SimpleNamespace(
        id=algo_order_id, status="OPEN", broker_order_id="",
        current_limit=None, account="ZG0790", symbol="NIFTY24APR25000CE",
        exchange="NFO", transaction_type="BUY", product="NRML", mode="live",
        quantity=50, filled_quantity=0, fill_price=None, filled_at=None,
        detail="", created_at=None, attempts=0,
        target_pct=None, target_abs=None, parent_order_id=None,
        template_id=None, intent="", is_close_intent=False, agent_id=None,
        last_attempt_at=None, next_attempt_at=None, interval_seconds=None,
    )
    placed_prices: list[float] = []
    poll_calls = {"n": 0}

    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return f"order_{len(placed_prices)}"

    async def _fake_run(fn, *args):
        if fn is m._get_depth:
            return _make_depth()
        if fn is m._place_order:
            return _fake_place(*args)
        if fn is m._order_status:
            poll_calls["n"] += 1
            if poll_calls["n"] == 1:
                return {"status": "REJECTED",
                        "status_message": "price out of circuit limit",
                        "filled_quantity": 0, "average_price": 0}
            return {"status": "COMPLETE", "filled_quantity": 50, "average_price": 101.0}
        if fn is m._cancel_order:
            return None
        return None

    _result = MagicMock()
    _result.scalar_one_or_none.return_value = mock_row
    mock_async_session = AsyncMock()
    mock_async_session.__aenter__ = AsyncMock(return_value=mock_async_session)
    mock_async_session.__aexit__ = AsyncMock(return_value=False)
    mock_async_session.execute = AsyncMock(return_value=_result)
    mock_async_session.commit = AsyncMock()

    _real_sleep = asyncio.sleep

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch.object(m, "_run", side_effect=_fake_run),
        patch.object(m, "_async_session", return_value=mock_async_session),
        patch.object(m, "_ch_seed_pre_fill_net_qty", new_callable=AsyncMock),
        patch.object(m, "_ch_post_replace_kill_check", return_value=False),
        patch.object(m, "_tick_size_sync", return_value=0.05),
        patch("backend.api.routes.orders_postback._pb_write_ledger_fills",
              new_callable=AsyncMock),
        patch("backend.api.routes.orders._subscribe_filled_pairs",
              new_callable=AsyncMock),
        patch("backend.api.algo.agent_engine.record_chase_terminal",
              new_callable=AsyncMock),
        patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
              MagicMock()) as mock_alert,
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = m.ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=10)
        result = await m.chase_order(
            account="ZG0790", symbol="NIFTY24APR25000CE",
            transaction_type="BUY", quantity=50, cfg=cfg,
            algo_order_id=algo_order_id,
        )
        await _real_sleep(0)
        await _real_sleep(0)

    assert len(placed_prices) == 2
    assert result.status == m.ChaseStatus.FILLED
    assert mock_row.status == "FILLED"
    mock_alert.assert_not_called()
