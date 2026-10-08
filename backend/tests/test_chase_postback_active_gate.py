"""
Tests for the 2026-10 postback/chase status-ownership fix: a postback
must never finalize AlgoOrder.status to a non-FILLED terminal value
(REJECTED/CANCELLED/UNFILLED) for a row currently under an active
chase_order() retry loop, or for a row already HELD.

Root cause (operator-confirmed timing fact): Kite's rejection postback
for an order arrives within a few seconds of the rejection — reliably,
not a rare race. `_ch_poll_handle_rejected`'s own price-shaped-
rejection retry ("rejected_continue") almost always loses that race
against the postback, so without this fix a ROUTINE price-rejected
retry (not an edge case) would get finalized to REJECTED by the
postback mid-retry, tripping `_sync_algo_order_id`'s separate
(still-correct) final-status guard and aborting the chase with a false
CRITICAL alert + operator alert on every ordinary retry.

Covers:
  - chase.py: `is_chase_active` / `_ch_mark_chase_active` /
    `_ch_mark_chase_inactive` — the in-process refcount registry.
  - chase.py: `chase_order()` wrapper marks/unmarks active around the
    real loop (`_chase_order_impl`).
  - orders_postback.py: `_pb_should_withhold_status` predicate.
  - orders_postback.py: `_pb_apply_status_to_row` (Kite path) and
    `_sync_apply_row_status` (Dhan/Groww path) wire the predicate in
    WITHOUT breaking the pre-existing FILLED-always-finalizes and
    non-chase REJECTED-finalizes-normally behavior.
  - orders.py: `_chase_process_live_row` (the `/chases/active` 3s-poll
    reconcile sweep) gets the identical withhold guard.
  - End-to-end: `chase_order()`'s main loop survives a REAL fast
    postback for the SAME row mid-retry and still ends FILLED, with NO
    false CRITICAL alert — plus a negative control proving the test
    actually exercises the collision (disable the gate → the same
    scenario reproduces the pre-fix false-abort).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────
# chase.py — in-process active-chase registry
# ─────────────────────────────────────────────────────────────────────────

def test_is_chase_active_false_when_never_marked():
    from backend.api.algo.chase import is_chase_active
    assert is_chase_active(999901) is False


def test_is_chase_active_none_always_false():
    from backend.api.algo.chase import is_chase_active
    assert is_chase_active(None) is False


def test_mark_active_then_inactive_round_trips():
    from backend.api.algo.chase import (
        _ch_mark_chase_active, _ch_mark_chase_inactive, is_chase_active,
    )
    _ch_mark_chase_active(999902)
    assert is_chase_active(999902) is True
    _ch_mark_chase_inactive(999902)
    assert is_chase_active(999902) is False


def test_mark_inactive_without_mark_active_is_a_safe_noop():
    from backend.api.algo.chase import _ch_mark_chase_inactive, is_chase_active
    _ch_mark_chase_inactive(999903)  # never marked — must not raise or go negative
    assert is_chase_active(999903) is False


def test_refcount_overlap_two_marks_require_two_unmarks():
    """A release-triggered resume and a recovery chase could overlap on
    the SAME algo_order_id — the first one to finish must not clear the
    marker out from under the other still-running one."""
    from backend.api.algo.chase import (
        _ch_mark_chase_active, _ch_mark_chase_inactive, is_chase_active,
    )
    _ch_mark_chase_active(999904)
    _ch_mark_chase_active(999904)
    _ch_mark_chase_inactive(999904)
    assert is_chase_active(999904) is True, "one of two marks still outstanding"
    _ch_mark_chase_inactive(999904)
    assert is_chase_active(999904) is False


@pytest.mark.asyncio
async def test_chase_order_wrapper_marks_and_unmarks_around_impl():
    """The public `chase_order()` must be active during the impl call
    and inactive again immediately after, on the normal return path."""
    from backend.api.algo import chase as m

    seen_active = {}

    async def _fake_impl(*args, **kwargs):
        seen_active["during"] = m.is_chase_active(999905)
        return m.ChaseResult(status=m.ChaseStatus.FILLED)

    with patch.object(m, "_chase_order_impl", _fake_impl):
        result = await m.chase_order(
            account="ACC1", symbol="SYM", transaction_type="BUY",
            quantity=10, algo_order_id=999905,
        )

    assert seen_active["during"] is True
    assert m.is_chase_active(999905) is False
    assert result.status == m.ChaseStatus.FILLED


@pytest.mark.asyncio
async def test_chase_order_wrapper_unmarks_even_on_exception():
    from backend.api.algo import chase as m

    async def _raising_impl(*args, **kwargs):
        raise RuntimeError("boom")

    with patch.object(m, "_chase_order_impl", _raising_impl):
        with pytest.raises(RuntimeError):
            await m.chase_order(
                account="ACC1", symbol="SYM", transaction_type="BUY",
                quantity=10, algo_order_id=999906,
            )

    assert m.is_chase_active(999906) is False


# ─────────────────────────────────────────────────────────────────────────
# orders_postback.py — _pb_should_withhold_status predicate
# ─────────────────────────────────────────────────────────────────────────

def test_withhold_predicate_never_withholds_filled():
    from backend.api.routes.orders_postback import _pb_should_withhold_status
    row = SimpleNamespace(id=1, status="HELD")
    assert _pb_should_withhold_status(row, "FILLED") is False


def test_withhold_predicate_true_for_held_row_nonfill_status():
    from backend.api.routes.orders_postback import _pb_should_withhold_status
    row = SimpleNamespace(id=1, status="HELD")
    for ns in ("REJECTED", "CANCELLED", "UNFILLED"):
        assert _pb_should_withhold_status(row, ns) is True


def test_withhold_predicate_true_for_active_chase_row():
    from backend.api.algo.chase import _ch_mark_chase_active, _ch_mark_chase_inactive
    from backend.api.routes.orders_postback import _pb_should_withhold_status
    row = SimpleNamespace(id=999910, status="OPEN")
    _ch_mark_chase_active(999910)
    try:
        assert _pb_should_withhold_status(row, "REJECTED") is True
        assert _pb_should_withhold_status(row, "CANCELLED") is True
    finally:
        _ch_mark_chase_inactive(999910)
    assert _pb_should_withhold_status(row, "REJECTED") is False


def test_withhold_predicate_false_for_plain_non_chased_row():
    from backend.api.routes.orders_postback import _pb_should_withhold_status
    row = SimpleNamespace(id=999911, status="OPEN")
    assert _pb_should_withhold_status(row, "REJECTED") is False


# ─────────────────────────────────────────────────────────────────────────
# orders_postback.py — _pb_apply_status_to_row (Kite path)
# ─────────────────────────────────────────────────────────────────────────

def _row(status: str, **extra) -> SimpleNamespace:
    base = dict(id=1, status=status, fill_price=None, filled_at=None,
                detail=None, created_at=None, quantity=100, filled_quantity=0)
    base.update(extra)
    return SimpleNamespace(**base)


class TestPbApplyStatusToRowActiveChaseGate:
    def test_rejected_withheld_for_active_chase_row(self):
        """The core collision: a postback REJECTED for a row under an
        active chase must NOT change row.status."""
        from backend.api.algo.chase import _ch_mark_chase_active, _ch_mark_chase_inactive
        from backend.api.routes.orders_postback import _pb_apply_status_to_row

        r = _row("OPEN", id=999920)
        _ch_mark_chase_active(999920)
        try:
            changed = _pb_apply_status_to_row(r, new_status="REJECTED", price=0)
        finally:
            _ch_mark_chase_inactive(999920)

        assert changed is False
        assert r.status == "OPEN", "row must stay OPEN — chase may still retry"

    def test_rejected_finalizes_normally_when_not_chased(self):
        """Regression guard: a plain one-shot ticket (no chase involved
        at all) must still finalize to REJECTED exactly as before."""
        from backend.api.routes.orders_postback import _pb_apply_status_to_row

        r = _row("OPEN", id=999921)
        changed = _pb_apply_status_to_row(r, new_status="REJECTED", price=0)

        assert changed is False  # REJECTED is not the FILLED transition
        assert r.status == "REJECTED"

    def test_filled_always_finalizes_even_under_active_chase(self):
        """Regression guard: a genuine fill must NEVER be withheld,
        chase-active or not — a fill is never ambiguous."""
        from backend.api.algo.chase import _ch_mark_chase_active, _ch_mark_chase_inactive
        from backend.api.routes.orders_postback import _pb_apply_status_to_row

        r = _row("OPEN", id=999922)
        _ch_mark_chase_active(999922)
        try:
            changed = _pb_apply_status_to_row(r, new_status="FILLED", price=101.5)
        finally:
            _ch_mark_chase_inactive(999922)

        assert changed is True
        assert r.status == "FILLED"
        assert r.fill_price == 101.5

    def test_held_row_never_overwritten_by_late_nonfill_postback(self):
        """A row already HELD (operator-review) must not be flipped
        back to REJECTED/CANCELLED by a late/duplicate postback for the
        stale broker_order_id still attached to it — only a release
        (which clears hold_json + sets OPEN) may move it on."""
        from backend.api.routes.orders_postback import _pb_apply_status_to_row

        r = _row("HELD", id=999923)
        changed = _pb_apply_status_to_row(r, new_status="CANCELLED", price=0)

        assert changed is False
        assert r.status == "HELD"

    def test_held_row_still_accepts_a_genuine_fill(self):
        from backend.api.routes.orders_postback import _pb_apply_status_to_row

        r = _row("HELD", id=999924)
        changed = _pb_apply_status_to_row(r, new_status="FILLED", price=100.0)

        assert changed is True
        assert r.status == "FILLED"


# ─────────────────────────────────────────────────────────────────────────
# orders_postback.py — _sync_apply_row_status (Dhan/Groww path)
# ─────────────────────────────────────────────────────────────────────────

class TestSyncApplyRowStatusActiveChaseGate:
    def test_rejected_withheld_for_active_chase_row(self):
        from backend.api.algo.chase import _ch_mark_chase_active, _ch_mark_chase_inactive
        from backend.api.routes.orders_postback import _sync_apply_row_status

        r = _row("OPEN", id=999930)
        _ch_mark_chase_active(999930)
        try:
            changed = _sync_apply_row_status(
                r, new_status="REJECTED", price=0, broker_id="dhan",
                status="REJECTED", status_message="price out of band",
            )
        finally:
            _ch_mark_chase_inactive(999930)

        assert changed is False
        assert r.status == "OPEN"

    def test_rejected_finalizes_normally_when_not_chased(self):
        from backend.api.routes.orders_postback import _sync_apply_row_status

        r = _row("OPEN", id=999931)
        changed = _sync_apply_row_status(
            r, new_status="REJECTED", price=0, broker_id="dhan",
            status="REJECTED", status_message="",
        )

        assert changed is False
        assert r.status == "REJECTED"

    def test_filled_always_finalizes_even_under_active_chase(self):
        from backend.api.algo.chase import _ch_mark_chase_active, _ch_mark_chase_inactive
        from backend.api.routes.orders_postback import _sync_apply_row_status

        r = _row("OPEN", id=999932)
        _ch_mark_chase_active(999932)
        try:
            changed = _sync_apply_row_status(
                r, new_status="FILLED", price=50.0, broker_id="dhan",
                status="TRADED", status_message="",
            )
        finally:
            _ch_mark_chase_inactive(999932)

        assert changed is True
        assert r.status == "FILLED"


# ─────────────────────────────────────────────────────────────────────────
# orders.py — _chase_process_live_row (the /chases/active 3s-poll sweep)
# ─────────────────────────────────────────────────────────────────────────

class TestChaseProcessLiveRowActiveChaseGate:
    def test_withholds_nonfill_terminal_write_for_active_chase_row(self):
        from backend.api.algo.chase import _ch_mark_chase_active, _ch_mark_chase_inactive
        from backend.api.routes.orders import _chase_process_live_row

        r = SimpleNamespace(id=999940, status="OPEN", broker_order_id="B1",
                            detail="", quantity=100, filled_quantity=0,
                            fill_price=None, filled_at=None)
        broker = {"B1": {"status": "REJECTED", "average_price": 0}}
        _ch_mark_chase_active(999940)
        try:
            drop, dd, rd = _chase_process_live_row(r, broker, [])
        finally:
            _ch_mark_chase_inactive(999940)

        assert drop is False
        assert r.status == "OPEN"

    def test_regression_nonfill_terminal_still_applies_when_not_chased(self):
        from backend.api.routes.orders import _chase_process_live_row

        r = SimpleNamespace(id=999941, status="OPEN", broker_order_id="B1",
                            detail="", quantity=100, filled_quantity=0,
                            fill_price=None, filled_at=None)
        broker = {"B1": {"status": "REJECTED", "average_price": 0}}
        drop, dd, rd = _chase_process_live_row(r, broker, [])

        assert drop is True
        assert r.status == "REJECTED"

    def test_fill_still_applies_even_under_active_chase(self):
        from backend.api.algo.chase import _ch_mark_chase_active, _ch_mark_chase_inactive
        from backend.api.routes.orders import _chase_process_live_row

        r = SimpleNamespace(id=999942, status="OPEN", broker_order_id="B1",
                            detail="", quantity=100, filled_quantity=0,
                            fill_price=None, filled_at=None)
        broker = {"B1": {"status": "COMPLETE", "average_price": 42.0}}
        q: list = []
        _ch_mark_chase_active(999942)
        try:
            drop, dd, rd = _chase_process_live_row(r, broker, q)
        finally:
            _ch_mark_chase_inactive(999942)

        assert drop is True
        assert r.status == "FILLED"
        assert q == [r]

    def test_missing_broker_order_id_withheld_for_active_chase_row(self):
        """Edge case: the tiny window between chase_order() entry
        (marked active) and its first successful _sync_algo_order_id
        write — broker_order_id is still empty but this is NOT a
        genuinely stuck/never-placed row."""
        from backend.api.algo.chase import _ch_mark_chase_active, _ch_mark_chase_inactive
        from backend.api.routes.orders import _chase_process_live_row

        r = SimpleNamespace(id=999943, status="OPEN", broker_order_id="",
                            detail="", quantity=100, filled_quantity=0,
                            fill_price=None, filled_at=None)
        _ch_mark_chase_active(999943)
        try:
            drop, dd, rd = _chase_process_live_row(r, {}, [])
        finally:
            _ch_mark_chase_inactive(999943)

        assert drop is False
        assert r.status == "OPEN"


# ─────────────────────────────────────────────────────────────────────────
# End-to-end: chase_order() survives a REAL fast postback mid-retry
# ─────────────────────────────────────────────────────────────────────────

def _make_depth(bid: float = 100.00, ask: float = 100.05) -> dict:
    return {
        "buy":  [{"price": bid, "quantity": 50}],
        "sell": [{"price": ask, "quantity": 50}],
    }


def _mock_session(row):
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


def _e2e_row(algo_order_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=algo_order_id, status="OPEN", broker_order_id="",
        current_limit=None, account="ZG0790", symbol="NIFTY24APR25000CE",
        exchange="NFO", transaction_type="BUY", product="NRML", mode="live",
        quantity=50, filled_quantity=0, fill_price=None, filled_at=None,
        detail="", created_at=None, attempts=0,
        target_pct=None, target_abs=None, parent_order_id=None,
        template_id=None, intent="", is_close_intent=False, agent_id=None,
        last_attempt_at=None, next_attempt_at=None, interval_seconds=None,
    )


async def _run_e2e_scenario(mock_row, algo_order_id: int):
    """Drive the real chase_order() loop: attempt 1 gets a price-shaped
    REJECTED (and — THE collision this test proves closed — a fast
    REAL postback write for the SAME row lands via _pb_apply_status_to_row
    at that exact moment); attempt 2 is placed and fills."""
    from backend.api.algo import chase as m
    from backend.api.routes.orders_postback import _pb_apply_status_to_row

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
                # THE confirmed timing fact: Kite's rejection postback
                # for THIS SAME order lands within seconds — simulated
                # here as a direct call against the SAME row object
                # chase's own _sync_algo_order_id reads.
                _pb_apply_status_to_row(mock_row, new_status="REJECTED", price=0)
                return {"status": "REJECTED",
                        "status_message": "price out of circuit limit",
                        "filled_quantity": 0, "average_price": 0}
            return {"status": "COMPLETE", "filled_quantity": 50, "average_price": 101.0}
        if fn is m._cancel_order:
            return None
        return None

    mock_session = _mock_session(mock_row)
    # Captured BEFORE `asyncio.sleep` is patched below, so we retain a
    # real checkpoint to let the fire-and-forget terminal task actually
    # run (an AsyncMock-replaced `asyncio.sleep` resolves synchronously
    # without yielding to the loop) — called while the patches below
    # (mock session, ledger-fill/subscribe/terminal-notify mocks) are
    # STILL active, so the real `_chase_terminal_update_db` write lands
    # on the SAME mock_row instead of silently failing against an
    # unpatched real DB session after the `with` block exits.
    _real_sleep = asyncio.sleep

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch.object(m, "_run", side_effect=_fake_run),
        patch.object(m, "_async_session", return_value=mock_session),
        patch.object(m, "_ch_seed_pre_fill_net_qty", new_callable=AsyncMock),
        patch.object(m, "_ch_post_replace_kill_check", return_value=False),
        patch.object(m, "_tick_size_sync", return_value=0.05),
        patch("backend.api.routes.orders_postback._pb_write_ledger_fills",
              new_callable=AsyncMock),
        patch("backend.api.routes.orders._subscribe_filled_pairs",
              new_callable=AsyncMock),
        patch("backend.api.algo.agent_engine.record_chase_terminal",
              new_callable=AsyncMock),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = m.ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=5)
        result = await m.chase_order(
            account="ZG0790", symbol="NIFTY24APR25000CE",
            transaction_type="BUY", quantity=50, cfg=cfg,
            algo_order_id=algo_order_id,
        )
        # Let any fire-and-forget terminal task run — using the REAL
        # sleep captured above, still inside the patch context.
        await _real_sleep(0)
        await _real_sleep(0)
    return result, placed_prices


@pytest.mark.asyncio
async def test_chase_survives_fast_postback_mid_retry_and_fills():
    """THE test that proves the collision is actually fixed: a fast
    postback REJECTED for the SAME row chase is mid-retrying on must
    NOT abort the chase. Exactly one retry is placed, the row ends
    FILLED, and no false CRITICAL alert / operator alert fires."""
    from backend.api.algo import chase as m

    algo_order_id = 999950
    mock_row = _e2e_row(algo_order_id)

    with patch.object(m.logger, "critical") as mock_critical, \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert:
        result, placed_prices = await _run_e2e_scenario(mock_row, algo_order_id)

    assert len(placed_prices) == 2, (
        f"expected exactly 2 orders placed (initial + 1 reprice retry), "
        f"got {len(placed_prices)}: {placed_prices}"
    )
    assert result.status == m.ChaseStatus.FILLED
    assert mock_row.status == "FILLED"
    assert mock_row.broker_order_id == "order_2"
    mock_critical.assert_not_called()
    mock_alert.assert_not_called()
    # The in-process marker must be clear again after the chase returns.
    assert m.is_chase_active(algo_order_id) is False


@pytest.mark.asyncio
async def test_negative_control_without_gate_reproduces_the_false_abort():
    """Negative control proving the test above actually exercises the
    fix: with `is_chase_active` forced to False (as if the gate didn't
    exist), the SAME fast postback prematurely finalizes the row to
    REJECTED, and the pre-existing `_sync_algo_order_id` final-status
    guard then (correctly, from ITS perspective) aborts the chase with
    a CRITICAL alert — the exact collision this fix closes."""
    from backend.api.algo import chase as m

    algo_order_id = 999951
    mock_row = _e2e_row(algo_order_id)

    with patch.object(m, "is_chase_active", return_value=False), \
         patch.object(m.logger, "critical") as mock_critical, \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert:
        result, placed_prices = await _run_e2e_scenario(mock_row, algo_order_id)

    assert mock_row.status == "REJECTED", (
        "without the gate the fast postback must still finalize the row"
    )
    assert result.status == m.ChaseStatus.FAILED, (
        "without the gate, _sync_algo_order_id's final-status guard "
        "must abort the chase — reproducing the pre-fix collision"
    )
    mock_critical.assert_called()
    mock_alert.assert_called()
