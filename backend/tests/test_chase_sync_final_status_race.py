"""
Tests for chase.py's `_sync_algo_order_id()` final-status race guard
(2026-10 audit fix, additive on top of d5b12f6d / bdbe7fec).

Bug: `_sync_algo_order_id()` (writes broker_order_id/current_limit/timing
fields onto an AlgoOrder row after every chase cancel-and-replace) had NO
`ALGO_ORDER_FINAL_STATUSES` guard, unlike its sibling
`_chase_terminal_update_db()`. A racing postback could finalize the row
(e.g. to REJECTED) in the window between the chase loop deciding to retry
and this function writing the new broker order's id onto the row —
silently overwriting broker_order_id on an already-final row, orphaning
the freshly-placed (live, possibly later filled) broker order from any DB
tracking.

Fix: `_sync_algo_order_id()` now checks `row.status in
ALGO_ORDER_FINAL_STATUSES` before writing, returns `bool` (False = raced,
caller must abort; True = synced normally), logs CRITICAL, and fires
`send_order_failure_alert`. The main `chase_order()` loop checks the
return value and aborts immediately (FAILED) without placing another
order.

Home: new file (not appended to test_order_lifecycle_race_fixes.py or
test_chase_recoverable_errors.py) — this is a distinct, narrowly-scoped
race condition on a different function (`_sync_algo_order_id`, not
`_chase_terminal_update_db`) and a different fix (today's additive patch,
not the two prior landed fixes), so it gets its own file for discoverability
and to avoid conflating with those two already-shipped defects.
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_session(row):
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


# ─────────────────────────────────────────────────────────────────────────
# _sync_algo_order_id — FINAL-status guard behavior
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_algo_order_id_refuses_overwrite_on_final_status():
    """A row already finalized to REJECTED must NOT get its
    broker_order_id overwritten by a later chase retry's new order id.
    Returns False, logs CRITICAL, fires the operator alert exactly once.

    Note: chase.py's logger has `propagate=False` (see
    `ramboq_logger.get_logger`), so pytest's `caplog` fixture (which
    hooks the root logger) never sees its records — asserting on the
    module's own `logger.critical` mock directly instead.
    """
    from backend.api.algo import chase as m

    mock_row = MagicMock()
    mock_row.id = 101
    mock_row.status = "REJECTED"
    mock_row.broker_order_id = "bo-old"
    mock_row.current_limit = 100.0
    mock_row.account = "ZG0790"
    mock_row.symbol = "NIFTY24APR25000CE"
    mock_row.exchange = "NFO"
    mock_row.transaction_type = "BUY"
    mock_row.quantity = 50

    mock_session = _mock_session(mock_row)
    mock_alert = MagicMock()

    with patch.object(m, "_async_session", return_value=mock_session), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert), \
         patch.object(m, "logger", wraps=m.logger) as mock_logger:
        synced = await m._sync_algo_order_id(
            101, "bo-new", current_limit=105.0, interval_seconds=20,
        )

    assert synced is False
    # Must NOT have overwritten broker_order_id or current_limit.
    assert mock_row.broker_order_id == "bo-old"
    assert mock_row.current_limit == 100.0
    mock_session.commit.assert_not_called()
    mock_alert.assert_called_once()
    mock_logger.critical.assert_called_once()
    # Row-lock check (race-closing fix): the SELECT must use
    # .with_for_update() — a plain unlocked SELECT would still race a
    # concurrent in-flight (uncommitted) postback status flip under
    # READ COMMITTED.
    _stmt = mock_session.execute.call_args[0][0]
    assert _stmt._for_update_arg is not None, (
        "_sync_algo_order_id's SELECT must use .with_for_update() to "
        "close the race, matching _chase_terminal_update_db's pattern"
    )


@pytest.mark.asyncio
async def test_sync_algo_order_id_writes_on_non_final_status():
    """Regression check — the normal/common case (row exists, status is
    NOT final) must keep working exactly as before this fix: returns
    True and DOES write the new broker_order_id + current_limit."""
    from backend.api.algo import chase as m

    mock_row = MagicMock()
    mock_row.id = 202
    mock_row.status = "OPEN"
    mock_row.broker_order_id = "bo-old"
    mock_row.current_limit = 100.0

    mock_session = _mock_session(mock_row)
    mock_alert = MagicMock()

    with patch.object(m, "_async_session", return_value=mock_session), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert):
        synced = await m._sync_algo_order_id(
            202, "bo-new", current_limit=105.0, interval_seconds=20,
        )

    assert synced is True
    assert mock_row.broker_order_id == "bo-new"
    assert mock_row.current_limit == 105.0
    mock_session.commit.assert_called_once()
    mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_sync_algo_order_id_returns_true_when_no_row_found():
    """No matching row (e.g. deleted/legacy) — nothing to guard against,
    returns True so the chase loop proceeds normally (unchanged pre-fix
    behavior: best-effort, never aborts the chase just because the row
    vanished)."""
    from backend.api.algo import chase as m

    mock_session = _mock_session(None)

    with patch.object(m, "_async_session", return_value=mock_session):
        synced = await m._sync_algo_order_id(303, "bo-new")

    assert synced is True


@pytest.mark.asyncio
async def test_sync_algo_order_id_returns_true_when_algo_order_id_none():
    """No algo_order_id at all — short-circuits before touching the DB,
    returns True (unchanged pre-fix behavior)."""
    from backend.api.algo import chase as m

    synced = await m._sync_algo_order_id(None, "bo-new")
    assert synced is True


# ─────────────────────────────────────────────────────────────────────────
# chase_order() main loop — aborts on final-status race, no further orders
# ─────────────────────────────────────────────────────────────────────────

def _make_depth(bid: float = 100.00, ask: float = 100.05) -> dict:
    return {
        "buy":  [{"price": bid, "quantity": 50}],
        "sell": [{"price": ask, "quantity": 50}],
    }


@pytest.mark.asyncio
async def test_chase_loop_aborts_immediately_when_sync_detects_race():
    """Integration path (mirrors test_chase_recoverable_errors.py's
    _ch_handle_attempt_error integration test and
    test_chase_min_tick_progression.py's full chase_order() mocking
    style): drive chase_order()'s main loop through a mocked scenario
    where `_sync_algo_order_id` returns False on the FIRST attempt.
    Asserts the loop returns a FAILED result immediately WITHOUT placing
    a second order."""
    from backend.api.algo.chase import chase_order, ChaseConfig, ChaseStatus

    placed_prices: list[float] = []
    cancelled_ids: list[str] = []

    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return f"order_{len(placed_prices)}"

    async def _fake_run(fn, *args):
        from backend.api.algo.chase import (
            _get_depth, _place_order, _order_status, _cancel_order,
        )
        if fn is _get_depth:
            return _make_depth()
        if fn is _place_order:
            return _fake_place(*args)
        if fn is _order_status:
            return {"status": "CANCELLED", "filled_quantity": 0, "average_price": 0}
        if fn is _cancel_order:
            cancelled_ids.append(args[1])  # (account, order_id, variety, exchange)
            return None
        return None

    mock_terminal = AsyncMock()
    mock_write_event = AsyncMock()

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.api.algo.chase._ch_cancel_previous", new_callable=AsyncMock),
        patch("backend.api.algo.chase._ch_seed_pre_fill_net_qty", new_callable=AsyncMock),
        # The fix under test: simulate the race — sync detects an
        # already-finalized row and tells the loop to abort.
        patch("backend.api.algo.chase._sync_algo_order_id",
              new=AsyncMock(return_value=False)),
        patch("backend.api.algo.chase._ch_post_replace_kill_check", return_value=False),
        patch("backend.api.algo.chase._tick_size_sync", return_value=0.05),
        patch("backend.api.algo.chase._emit_chase_terminal", mock_terminal),
        patch("backend.api.algo.chase._ch_write_order_event", mock_write_event),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=5)
        result = await chase_order(
            account="ACC1",
            symbol="NIFTY25CE",
            transaction_type="BUY",
            quantity=50,
            cfg=cfg,
            algo_order_id=42,
        )
    # The abort path schedules _emit_chase_terminal via
    # asyncio.create_task — give the loop one tick (using the REAL,
    # un-mocked asyncio.sleep, now that we've exited the `with` block
    # above) so the task actually runs before we assert on it.
    await asyncio.sleep(0)

    # Exactly ONE order placed — the loop must abort right after
    # detecting the race, never attempting a second cancel-and-replace.
    assert len(placed_prices) == 1, (
        f"Expected exactly 1 place_order call, got {len(placed_prices)}: {placed_prices}"
    )
    assert result.status == ChaseStatus.FAILED
    assert "untracked" in result.detail.lower() or "racing" in result.detail.lower()
    # The now-untracked order must still get a best-effort cancel attempt
    # (mirrors _ch_exhaust_max_attempts's shape) so it doesn't sit resting
    # live with nothing watching it.
    assert cancelled_ids == ["order_1"], (
        f"Expected best-effort cancel of the untracked order, got: {cancelled_ids}"
    )
    # The abort path must still fire the same terminal bookkeeping every
    # other abort path in this module fires (pops _CH_PRE_FILL_NET_QTY,
    # notifies record_chase_terminal) — without this the chase's pre-fill
    # snapshot would leak forever for this algo_order_id.
    mock_write_event.assert_called_once()
    assert mock_write_event.call_args[0][0] == 42
    mock_terminal.assert_called_once()
    _terminal_kwargs = mock_terminal.call_args
    assert _terminal_kwargs.kwargs.get("algo_order_id") == 42
    assert _terminal_kwargs.args[1] == "chase_failed"


@pytest.mark.asyncio
async def test_chase_loop_sync_race_reports_when_cancel_also_fails():
    """Variant of the above: the best-effort cancel of the now-untracked
    order ALSO fails (e.g. broker already processed it). The chase must
    still report FAILED with an explicit "may have ALSO failed" note in
    `result.detail` (mirrors `_ch_exhaust_max_attempts`'s own
    cancel-failed message), and the terminal bookkeeping must still
    fire exactly as in the clean-cancel case."""
    from backend.api.algo.chase import chase_order, ChaseConfig, ChaseStatus

    placed_prices: list[float] = []

    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return f"order_{len(placed_prices)}"

    async def _fake_run(fn, *args):
        from backend.api.algo.chase import (
            _get_depth, _place_order, _order_status, _cancel_order,
        )
        if fn is _get_depth:
            return _make_depth()
        if fn is _place_order:
            return _fake_place(*args)
        if fn is _order_status:
            return {"status": "CANCELLED", "filled_quantity": 0, "average_price": 0}
        if fn is _cancel_order:
            raise RuntimeError("broker: order already in terminal state")
        return None

    mock_terminal = AsyncMock()
    mock_write_event = AsyncMock()

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.api.algo.chase._ch_cancel_previous", new_callable=AsyncMock),
        patch("backend.api.algo.chase._ch_seed_pre_fill_net_qty", new_callable=AsyncMock),
        patch("backend.api.algo.chase._sync_algo_order_id",
              new=AsyncMock(return_value=False)),
        patch("backend.api.algo.chase._ch_post_replace_kill_check", return_value=False),
        patch("backend.api.algo.chase._tick_size_sync", return_value=0.05),
        patch("backend.api.algo.chase._emit_chase_terminal", mock_terminal),
        patch("backend.api.algo.chase._ch_write_order_event", mock_write_event),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=5)
        result = await chase_order(
            account="ACC1",
            symbol="NIFTY25CE",
            transaction_type="BUY",
            quantity=50,
            cfg=cfg,
            algo_order_id=42,
        )
    await asyncio.sleep(0)

    assert len(placed_prices) == 1
    assert result.status == ChaseStatus.FAILED
    assert "may have also failed" in result.detail.lower()
    mock_terminal.assert_called_once()
    mock_write_event.assert_called_once()
    assert mock_write_event.call_args[0][2]  # message text present (non-empty)
