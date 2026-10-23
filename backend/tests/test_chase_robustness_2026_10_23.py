"""
Tests for the 2026-10-23 chase-robustness pass (chase.py).

Covers:
  1. Cancel-unconfirmed / consecutive-errors aborts write CANCEL_FAILED
     (not REJECTED) — and a later real fill is still recordable, since
     CANCEL_FAILED is deliberately excluded from ALGO_ORDER_FINAL_STATUSES.
  2. Every abort path writes SOME terminal marker, even when
     current_order_id was never set (nothing was ever placed this cycle).
  3. `_chase_terminal_update_db`'s broker_order_id fallback lookup is
     skipped when the id is empty (never queries `WHERE
     broker_order_id == ''`).
  4. `_run`'s asyncio.wait_for timeout wrap; the stuck-detector's ceiling
     fires strictly before `_ch_exhaust_max_attempts` would; a
     `_place_order` timeout aborts without retrying.
  5. Recoverable errors keep retrying — never become terminal
     (explicit regression guard).
  6. `_ch_append_superseded_id` appends the old id, idempotently.
"""
from __future__ import annotations

import time as _time_mod
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.api.algo.chase as chase
from backend.api.algo.chase import (
    ChaseConfig,
    ChaseResult,
    ChaseStatus,
)


def _mock_session(row):
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


def _cfg(max_attempts=5, interval_seconds=1):
    return ChaseConfig(interval_seconds=interval_seconds, max_attempts=max_attempts,
                       exchange="NFO")


# ── Fix #1/#2 — CANCEL_FAILED, not REJECTED, for ambiguous aborts ──────

@pytest.mark.asyncio
async def test_cancel_unconfirmed_abort_writes_cancel_failed_not_rejected():
    result = ChaseResult()
    emit = MagicMock()
    mock_terminal = AsyncMock()
    with patch.object(chase, "_emit_chase_terminal", mock_terminal), \
         patch.object(chase, "_ch_mark_ambiguous_abort") as mock_mark:
        out = await chase._ch_build_cancel_unconfirmed_abort(
            result, "NIFTY25JULFUT", "ZG0790", "BUY", 50, 50, 2,
            "OID1", algo_order_id=7, emit=emit,
        )

    assert out.status == ChaseStatus.FAILED
    mock_terminal.assert_awaited_once()
    _, kwargs = mock_terminal.call_args
    args = mock_terminal.call_args.args
    # positional signature: (broker_order_id, outcome, symbol, side, qty, ...)
    assert args[1] == "chase_cancel_failed", (
        "cancel-unconfirmed abort must use the new ambiguous outcome, "
        "not 'chase_failed' (-> REJECTED, a final status that would "
        "permanently block a later real fill)."
    )
    mock_mark.assert_called_once_with(7)


@pytest.mark.asyncio
async def test_consecutive_errors_abort_writes_cancel_failed_and_marks_ambiguous():
    result = ChaseResult()
    emit = MagicMock()
    mock_terminal = AsyncMock()
    with patch.object(chase, "_ch_final_cancel_and_confirm",
                      AsyncMock(return_value=(True, 0, 0, 0.0))), \
         patch.object(chase, "_emit_chase_terminal", mock_terminal), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert, \
         patch.object(chase, "_ch_mark_ambiguous_abort") as mock_mark:
        out = await chase._chase_abort_on_consecutive_errors(
            consecutive_errors=3, attempt=3, last_error=ConnectionError("x"),
            account="ZG0790", symbol="NIFTY25JULFUT", transaction_type="BUY",
            quantity=50, current_order_id=None, cfg=_cfg(), result=result,
            emit=emit, algo_order_id=9,
        )

    assert out.status == ChaseStatus.FAILED
    mock_terminal.assert_awaited_once()
    args = mock_terminal.call_args.args
    assert args[1] == "chase_cancel_failed"
    # bypass_gates must be set — this is an abandonment-class alert
    # (fix #7), must never be swallowed by dedup/market-hours gate.
    assert mock_alert.call_args.kwargs.get("bypass_gates") is True
    mock_mark.assert_called_once_with(9)


@pytest.mark.asyncio
async def test_cancel_failed_status_does_not_block_a_later_real_fill():
    """CANCEL_FAILED is deliberately excluded from
    ALGO_ORDER_FINAL_STATUSES — a row already at CANCEL_FAILED must
    still accept a later terminal write (e.g. a real fill the broker
    eventually confirms)."""
    mock_row = MagicMock()
    mock_row.id = 42
    mock_row.status = "CANCEL_FAILED"
    mock_row.agent_id = None
    mock_row.broker_order_id = "OID1"
    mock_row.detail = ""

    mock_session = _mock_session(mock_row)
    with patch.object(chase, "_async_session", return_value=mock_session), \
         patch("backend.api.routes.orders_postback._pb_write_ledger_fills",
               new=AsyncMock()), \
         patch("backend.api.routes.orders._subscribe_filled_pairs", new=AsyncMock()):
        agent_id, row_snap = await chase._chase_terminal_update_db(
            algo_order_id=42, broker_order_id="OID1", outcome="chase_fill",
            attempts=3, final_price=105.0, error=None,
        )

    assert mock_row.status == "FILLED", (
        "a CANCEL_FAILED row must still accept a later real fill — "
        "it must never behave like a final status."
    )
    assert row_snap is not None


# ── Fix #2 — "write some marker" even when nothing was ever placed ─────

@pytest.mark.asyncio
async def test_exhaustion_writes_marker_when_nothing_ever_placed():
    """Every attempt hit `price<=0: continue` — no order was ever
    placed. Pre-fix, `if result.order_id:` was False and the row was
    left silently OPEN forever."""
    result = ChaseResult()
    result.order_id = ""  # never placed
    emit = MagicMock()
    mock_terminal = AsyncMock()
    with patch.object(chase, "_ch_final_cancel_and_confirm",
                      AsyncMock(return_value=(True, 0, 0, 0.0))), \
         patch.object(chase, "_ch_write_order_event", AsyncMock()), \
         patch.object(chase, "_emit_chase_terminal", mock_terminal), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert"):
        out = await chase._ch_exhaust_max_attempts(
            result, None, _cfg(max_attempts=5),
            "ZG0790", "NIFTY25JULFUT", "BUY", 50, algo_order_id=11, emit=emit,
        )

    assert out.status == ChaseStatus.FAILED
    mock_terminal.assert_awaited_once(), (
        "exhaustion with nothing ever placed must still write a "
        "terminal marker when an algo_order_id exists — leaving the "
        "row at OPEN risks resurrection by recover_live_chases()."
    )


@pytest.mark.asyncio
async def test_non_recoverable_error_writes_marker_when_current_order_id_falsy():
    result = ChaseResult()
    emit = MagicMock()
    mock_terminal = AsyncMock()
    with patch.object(chase, "_emit_chase_terminal", mock_terminal), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert"):
        abort, consecutive = await chase._ch_handle_attempt_error(
            chase._BrokerInputError("bad qty"), consecutive_errors=0, attempt=1,
            symbol="NIFTY25JULFUT", account="ZG0790", transaction_type="BUY",
            quantity=50, current_order_id=None, cfg=_cfg(), result=result,
            emit=emit, algo_order_id=13,
        )

    assert abort is not None
    mock_terminal.assert_awaited_once()


# ── Fix #3 — chase_terminal_update_db fallback-lookup guard ────────────

@pytest.mark.asyncio
async def test_chase_terminal_update_db_skips_empty_broker_order_id_fallback():
    """algo_order_id=None (no row found that way) + broker_order_id=""
    must never run the `WHERE broker_order_id == ''` fallback query —
    it could match an unrelated row or raise MultipleResultsFound."""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(side_effect=AssertionError(
        "must not query the DB at all when both lookup keys are empty"
    ))
    mock_session.commit = AsyncMock()

    with patch.object(chase, "_async_session", return_value=mock_session):
        agent_id, row_snap = await chase._chase_terminal_update_db(
            algo_order_id=None, broker_order_id="", outcome="chase_cancel_failed",
            attempts=1, final_price=None, error="x",
        )

    assert agent_id is None
    assert row_snap is None
    mock_session.execute.assert_not_called()


# ── Fix #3 — stuck/timeout detection ────────────────────────────────────

@pytest.mark.asyncio
async def test_run_raises_timeout_error_on_hung_broker_call(monkeypatch):
    monkeypatch.setattr(chase, "_CH_BROKER_CALL_TIMEOUT_S", 0.05)

    def _hang(*_a):
        _time_mod.sleep(0.3)
        return "done"

    with pytest.raises(TimeoutError):
        await chase._run(_hang)


def test_stuck_ceiling_is_strictly_below_full_attempt_budget():
    cfg = _cfg(max_attempts=20, interval_seconds=20)  # MED tier, ~400s budget
    ceiling = chase._ch_stuck_ceiling_seconds(cfg)
    assert ceiling < cfg.max_attempts * cfg.interval_seconds
    # Floor applies for a fast/aggressive config.
    fast_cfg = _cfg(max_attempts=10, interval_seconds=10)
    assert chase._ch_stuck_ceiling_seconds(fast_cfg) >= 60.0


@pytest.mark.asyncio
async def test_loop_guards_aborts_stuck_at_a_low_attempt_count_before_exhaustion():
    """Proves the stuck-abort fires at an attempt count strictly LESS
    than cfg.max_attempts — i.e. before _ch_exhaust_max_attempts would
    ever run — not merely that the ceiling constant is smaller."""
    cfg = _cfg(max_attempts=100, interval_seconds=10)  # full budget = 1000s
    ceiling = chase._ch_stuck_ceiling_seconds(cfg)
    result = ChaseResult()
    emit = MagicMock()
    stale_mono = _time_mod.monotonic() - (ceiling + 5)

    with patch.object(chase, "_ch_check_external_final_status",
                      AsyncMock(return_value=None)), \
         patch.object(chase, "_ch_periodic_broker_truth_check",
                      AsyncMock(return_value=None)), \
         patch.object(chase, "_ch_final_cancel_and_confirm",
                      AsyncMock(return_value=(True, 0, 0, 0.0))), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert"), \
         patch.object(chase, "_emit_chase_terminal", AsyncMock()):
        out = await chase._ch_loop_guards(
            algo_order_id=None, attempt=5,   # attempt=5, NOT max_attempts=100
            last_progress_mono=stale_mono, cfg=cfg,
            account="ZG0790", symbol="NIFTY25JULFUT", transaction_type="BUY",
            quantity=50, current_order_id=None,
            cumulative_filled=0, current_order_filled=0,
            result=result, emit=emit,
        )

    assert out is not None
    assert out.status == ChaseStatus.FAILED
    assert "stuck" in out.detail.lower()


@pytest.mark.asyncio
async def test_place_order_timeout_aborts_without_a_second_placement_attempt(monkeypatch):
    """A `_place_order` timeout must hard-stop — never retry via the
    generic recoverable-error path (which would risk a SECOND live
    order on top of a possibly-already-landed first one)."""
    from backend.shared.helpers import utils as _utils_mod
    monkeypatch.setattr(_utils_mod, "is_prod_branch", lambda: True)

    place_calls = {"n": 0}

    async def _fake_run(fn, *args):
        if fn is chase._get_depth:
            return {"buy": [{"price": 100.0}], "sell": [{"price": 100.1}]}
        if fn is chase._place_order:
            place_calls["n"] += 1
            raise TimeoutError("broker call timed out")
        return None

    with patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
         patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}), \
         patch("backend.api.algo.agent_engine._prewarm_special_sessions_for_exchange",
               new=AsyncMock()), \
         patch.object(chase, "_run", side_effect=_fake_run), \
         patch.object(chase, "_ch_seed_pre_fill_net_qty", new=AsyncMock()), \
         patch.object(chase, "_ch_cancel_and_capture",
                      new=AsyncMock(return_value=(0, 0, 50, None))), \
         patch.object(chase, "_emit_chase_terminal", new=AsyncMock()), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert"), \
         patch("asyncio.sleep", new=AsyncMock()):
        result = await chase.chase_order(
            account="ZG0790", symbol="NIFTY25JULFUT", transaction_type="BUY",
            quantity=50, cfg=_cfg(max_attempts=5, interval_seconds=0),
        )

    assert place_calls["n"] == 1, "must not retry after a place-order timeout"
    assert result.status == ChaseStatus.FAILED


# ── Fix #5/#6 (regression guard, explicit per the plan) ────────────────

@pytest.mark.asyncio
async def test_recoverable_errors_then_success_never_becomes_terminal(monkeypatch):
    """Two recoverable errors (attempts 1-2) followed by a clean fill on
    attempt 3 must NOT produce any terminal CANCEL_FAILED/REJECTED write
    and must NOT fire any operator alert — recoverable errors must keep
    retrying, never become terminal."""
    from backend.shared.helpers import utils as _utils_mod
    monkeypatch.setattr(_utils_mod, "is_prod_branch", lambda: True)

    attempts = {"n": 0}

    async def _fake_run(fn, *args):
        if fn is chase._get_depth:
            return {"buy": [{"price": 100.0}], "sell": [{"price": 100.1}]}
        if fn is chase._place_order:
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise ConnectionError("network blip")
            return "OID-FINAL"
        if fn is chase._order_status:
            return {"status": "COMPLETE", "filled_quantity": 50, "average_price": 100.05}
        return None

    mock_terminal = AsyncMock()
    mock_alert = MagicMock()
    with patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
         patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}), \
         patch("backend.api.algo.agent_engine._prewarm_special_sessions_for_exchange",
               new=AsyncMock()), \
         patch.object(chase, "_run", side_effect=_fake_run), \
         patch.object(chase, "_ch_seed_pre_fill_net_qty", new=AsyncMock()), \
         patch.object(chase, "_ch_cancel_previous", new=AsyncMock()), \
         patch.object(chase, "_ch_append_superseded_id", new=AsyncMock()), \
         patch.object(chase, "_ch_write_cancel_confirmed_event", new=AsyncMock()), \
         patch.object(chase, "_sync_algo_order_id", new=AsyncMock(return_value=True)), \
         patch.object(chase, "_ch_write_chase_modify_event", new=AsyncMock()), \
         patch.object(chase, "_ch_post_replace_kill_check", new=AsyncMock(return_value=False)), \
         patch.object(chase, "_emit_chase_terminal", mock_terminal), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert), \
         patch("asyncio.sleep", new=AsyncMock()):
        result = await chase.chase_order(
            account="ZG0790", symbol="NIFTY25JULFUT", transaction_type="BUY",
            quantity=50, cfg=_cfg(max_attempts=5, interval_seconds=0),
        )

    assert result.status == ChaseStatus.FILLED
    mock_alert.assert_not_called(), (
        "a transient recoverable error that resolves into a clean fill "
        "must never trigger an operator-failure alert"
    )
    # The fill's terminal write is scheduled via asyncio.create_task
    # (fire-and-forget, by design, so the caller gets the fill result
    # immediately) — yield a couple of loop ticks so it actually runs
    # before asserting on it.
    import asyncio as _asyncio_mod
    for _ in range(5):
        await _asyncio_mod.sleep(0)
    assert mock_terminal.await_count == 1
    assert mock_terminal.call_args.args[1] == "chase_fill"


# ── Fix #6 — superseded_broker_order_ids ────────────────────────────────

@pytest.mark.asyncio
async def test_append_superseded_id_adds_old_id_once():
    mock_row = MagicMock()
    mock_row.superseded_broker_order_ids_json = None
    mock_session = _mock_session(mock_row)

    with patch.object(chase, "_async_session", return_value=mock_session):
        await chase._ch_append_superseded_id(5, "OID-OLD")

    import json
    saved = json.loads(mock_row.superseded_broker_order_ids_json)
    assert saved == ["OID-OLD"]
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_append_superseded_id_is_idempotent():
    mock_row = MagicMock()
    mock_row.superseded_broker_order_ids_json = '["OID-OLD"]'
    mock_session = _mock_session(mock_row)

    with patch.object(chase, "_async_session", return_value=mock_session):
        await chase._ch_append_superseded_id(5, "OID-OLD")

    import json
    saved = json.loads(mock_row.superseded_broker_order_ids_json)
    assert saved == ["OID-OLD"]
    mock_session.commit.assert_not_awaited(), (
        "re-adding the same id must not write a redundant commit"
    )


@pytest.mark.asyncio
async def test_cancel_and_capture_appends_superseded_id_before_returning():
    """The superseded id must be recorded at cancel-confirm time —
    BEFORE the caller places the replacement order — closing the
    original duplicate-open-order-card race."""
    reads = [(0, 0, 50, 0.0, True)]  # confirmed on first read
    append_mock = AsyncMock()
    with patch.object(chase, "_ch_capture_late_fill", AsyncMock(side_effect=reads)), \
         patch.object(chase, "_ch_cancel_previous", AsyncMock(return_value=None)), \
         patch.object(chase, "_ch_write_cancel_confirmed_event", AsyncMock()), \
         patch.object(chase, "_ch_append_superseded_id", append_mock):
        cumulative_filled, current_order_filled, remaining_qty, early = (
            await chase._ch_cancel_and_capture(
                "ZG0790", "OID-OLD", _cfg(), "NIFTY25JULFUT", 2,
                MagicMock(), 50, 50, 0, 0, 5, ChaseResult(), "BUY",
            )
        )

    assert early is None
    append_mock.assert_awaited_once_with(5, "OID-OLD")
