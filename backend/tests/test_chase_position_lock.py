"""
Tests for the 2026-11 per-(account, symbol, exchange) position-resolution
lock in `chase_order()` (chase.py).

Context: two fully independent call sites can each start a `chase_order()`
cycle for the SAME position with zero shared code between them —
`actions_live.py:_action_live_close_position` and
`order_release.py:release_held_order` / `_chase_released`. Each resolves
its own quantity against the broker/DB BEFORE calling `chase_order()`, so
if both fire for the same (account, symbol, exchange) in a narrow window,
nothing previously stopped BOTH from independently resolving the position
and placing their own full-size order.

This lock narrows that race for the ONE piece of it that lives inside
`chase_order()` itself: it is a try-lock (fail-closed, not a blocking
wait) acquired at the very top of `chase_order()`, released as soon as
`_chase_order_impl` has either placed+synced its first order or decided
there's nothing to place — well before the multi-minute cancel-and-replace
retry loop runs.

Covers:
  (a) two concurrent `chase_order()` calls for the SAME
      (account, symbol, exchange) — the second is refused while the
      first is still resolving (CRITICAL log + alert + a FAILED
      ChaseResult, and it never reaches `_chase_order_impl` at all).
  (b) two concurrent `chase_order()` calls for DIFFERENT keys proceed
      independently (no cross-talk).
  (c) a single normal call completes end-to-end through the REAL
      `_chase_order_impl`, releases the lock, and leaves it reusable —
      no deadlock, no change to the single-call behavior.
  (d) the lock is released EARLY — once the first order is placed, a
      second concurrent call for the SAME key is no longer refused,
      even though the "impl" standing in for the retry loop is still
      running. This is the narrow-scope claim itself.
  (e) small supporting-helper unit tests: key normalisation, registry
      lock reuse, and the refusal-result builder's shape.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.api.algo.chase as chase_mod
from backend.api.algo.chase import ChaseConfig, ChaseResult, ChaseStatus


# ─────────────────────────────────────────────────────────────────────────
# Helpers
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


# ─────────────────────────────────────────────────────────────────────────
# (e) supporting-helper unit tests
# ─────────────────────────────────────────────────────────────────────────

def test_lock_key_normalises_exchange_and_symbol_case():
    k1 = chase_mod._chase_position_lock_key("ACC1", "nfo", "niftyfut")
    k2 = chase_mod._chase_position_lock_key("ACC1", "NFO", "NIFTYFUT")
    assert k1 == k2


def test_lock_key_keeps_account_case_sensitive():
    k1 = chase_mod._chase_position_lock_key("acc1", "NFO", "SYM")
    k2 = chase_mod._chase_position_lock_key("ACC1", "NFO", "SYM")
    assert k1 != k2


@pytest.mark.asyncio
async def test_registry_returns_the_same_lock_object_for_the_same_key():
    key = ("ACC_REUSE", "NFO", "SYMREUSE")
    lk1 = await chase_mod._get_chase_position_lock(key)
    lk2 = await chase_mod._get_chase_position_lock(key)
    assert lk1 is lk2


@pytest.mark.asyncio
async def test_contended_result_shape_masks_account_and_fails_closed():
    with patch.object(chase_mod.logger, "critical") as mock_critical, \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert:
        result = await chase_mod._chase_position_lock_contended_result(
            "ZG0790", "NFO", "NIFTY24APR25000CE", "SELL", 50, None,
        )
    assert result.status == ChaseStatus.FAILED
    assert result.detail  # non-empty, operator-facing
    assert "0790" not in result.detail  # raw account must not leak unmasked
    mock_critical.assert_called_once()
    mock_alert.assert_called_once()
    _, kwargs = mock_alert.call_args
    assert kwargs["bypass_gates"] is True


# ─────────────────────────────────────────────────────────────────────────
# (a) two concurrent calls, SAME key — second fails closed
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_second_concurrent_call_same_key_fails_closed_while_first_resolves():
    entered = {"count": 0}
    release_event = asyncio.Event()

    async def _fake_impl(*args, **kwargs):
        entered["count"] += 1
        # Yield so a concurrently-launched second call gets a chance to
        # run and observe the lock as held, before we ever release it.
        await asyncio.sleep(0)
        await release_event.wait()
        release_cb = kwargs.get("_release_position_lock")
        if release_cb is not None:
            release_cb()
        return ChaseResult(status=ChaseStatus.FILLED)

    with patch.object(chase_mod, "_chase_order_impl", _fake_impl), \
         patch.object(chase_mod.logger, "critical") as mock_critical, \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert:

        cfg = ChaseConfig(exchange="NFO")
        task1 = asyncio.create_task(chase_mod.chase_order(
            account="ACC_RACE", symbol="SAMESYM", transaction_type="SELL",
            quantity=10, cfg=cfg,
        ))
        # Let task1 run far enough to acquire the lock and enter the
        # (fake) impl before task2 is even created.
        await asyncio.sleep(0)
        assert entered["count"] == 1, "task1 must have entered the impl already"

        task2 = asyncio.create_task(chase_mod.chase_order(
            account="ACC_RACE", symbol="SAMESYM", transaction_type="BUY",
            quantity=5, cfg=cfg,
        ))
        result2 = await task2

        release_event.set()
        result1 = await task1

    # The second call must NEVER have reached _chase_order_impl.
    assert entered["count"] == 1
    assert result2.status == ChaseStatus.FAILED
    assert "REFUSED" in result2.detail
    mock_critical.assert_called_once()
    mock_alert.assert_called_once()

    assert result1.status == ChaseStatus.FILLED

    # The lock must be free again — no leak, no deadlock.
    key = chase_mod._chase_position_lock_key("ACC_RACE", "NFO", "SAMESYM")
    lk = await chase_mod._get_chase_position_lock(key)
    assert lk.locked() is False


# ─────────────────────────────────────────────────────────────────────────
# (b) two concurrent calls, DIFFERENT keys — both proceed independently
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_calls_different_keys_both_proceed():
    entered = {"count": 0}

    async def _fake_impl(*args, **kwargs):
        entered["count"] += 1
        await asyncio.sleep(0.01)
        release_cb = kwargs.get("_release_position_lock")
        if release_cb is not None:
            release_cb()
        return ChaseResult(status=ChaseStatus.FILLED)

    with patch.object(chase_mod, "_chase_order_impl", _fake_impl), \
         patch.object(chase_mod.logger, "critical") as mock_critical, \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert:
        result1, result2 = await asyncio.gather(
            chase_mod.chase_order(
                account="ACC_A", symbol="SYM_A", transaction_type="SELL",
                quantity=10, cfg=ChaseConfig(exchange="NFO"),
            ),
            chase_mod.chase_order(
                account="ACC_B", symbol="SYM_B", transaction_type="BUY",
                quantity=10, cfg=ChaseConfig(exchange="MCX"),
            ),
        )

    assert entered["count"] == 2, "both independent keys must reach the impl"
    assert result1.status == ChaseStatus.FILLED
    assert result2.status == ChaseStatus.FILLED
    mock_critical.assert_not_called()
    mock_alert.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# (d) the lock is released EARLY — before the retry loop finishes
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lock_released_before_retry_loop_finishes_allows_a_later_concurrent_call():
    """Proves the narrow-scope claim: once the fake impl calls
    `_release_position_lock()` (standing in for "first order placed and
    synced"), a brand-new concurrent `chase_order()` call for the SAME
    key is NOT refused even though the first call's own impl is still
    running (standing in for the multi-minute retry loop)."""
    released_at = {"released": False}
    second_call_started_after_release = {"value": False}

    async def _fake_impl(*args, **kwargs):
        release_cb = kwargs.get("_release_position_lock")
        # Simulate: position resolved, first order placed+synced.
        if release_cb is not None:
            release_cb()
        released_at["released"] = True
        # Simulate the rest of the (long) retry loop still running.
        await asyncio.sleep(0.02)
        return ChaseResult(status=ChaseStatus.FILLED)

    with patch.object(chase_mod, "_chase_order_impl", _fake_impl):
        cfg = ChaseConfig(exchange="NFO")
        task1 = asyncio.create_task(chase_mod.chase_order(
            account="ACC_EARLY", symbol="EARLYSYM", transaction_type="SELL",
            quantity=10, cfg=cfg,
        ))
        # Give task1 enough of a head start to release the lock, while
        # its own impl call is still in flight (asleep).
        await asyncio.sleep(0.005)
        assert released_at["released"] is True
        assert task1.done() is False, "task1's impl must still be running"

        key = chase_mod._chase_position_lock_key("ACC_EARLY", "NFO", "EARLYSYM")
        lk = await chase_mod._get_chase_position_lock(key)
        assert lk.locked() is False, "lock must already be released"

        second_call_started_after_release["value"] = True
        result2 = await chase_mod.chase_order(
            account="ACC_EARLY", symbol="EARLYSYM", transaction_type="BUY",
            quantity=5, cfg=cfg,
        )
        result1 = await task1

    assert second_call_started_after_release["value"] is True
    assert result2.status == ChaseStatus.FILLED, (
        "a second call arriving AFTER the first released its lock must "
        "proceed normally, not be refused"
    )
    assert result1.status == ChaseStatus.FILLED


# ─────────────────────────────────────────────────────────────────────────
# (c) single normal call — real `_chase_order_impl`, no deadlock, reusable
# ─────────────────────────────────────────────────────────────────────────

def _e2e_row(algo_order_id: int) -> object:
    from types import SimpleNamespace
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


async def _run_single_real_chase(mock_row, algo_order_id: int):
    """Drive the REAL `chase_order()` -> `_chase_order_impl()` loop for
    a plain single-attempt fill (no reject/retry needed — that path is
    already covered end-to-end by test_chase_postback_active_gate.py).
    """
    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        return "order_1"

    async def _fake_run(fn, *args):
        if fn is chase_mod._get_depth:
            return {"buy": [{"price": 100.00, "quantity": 50}],
                    "sell": [{"price": 100.05, "quantity": 50}]}
        if fn is chase_mod._place_order:
            return _fake_place(*args)
        if fn is chase_mod._order_status:
            return {"status": "COMPLETE", "filled_quantity": 50, "average_price": 100.0}
        if fn is chase_mod._cancel_order:
            return None
        return None

    mock_session = _mock_session(mock_row)
    # Captured BEFORE `asyncio.sleep` is patched below — an AsyncMock
    # replacement resolves synchronously without yielding to the loop,
    # so the fire-and-forget terminal task (_emit_chase_terminal) would
    # otherwise never get a chance to actually run before we assert on
    # it. Same precedent as test_chase_postback_active_gate.py.
    _real_sleep = asyncio.sleep

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch.object(chase_mod, "_run", side_effect=_fake_run),
        patch.object(chase_mod, "_async_session", return_value=mock_session),
        patch.object(chase_mod, "_ch_seed_pre_fill_net_qty", new_callable=AsyncMock),
        patch.object(chase_mod, "_ch_post_replace_kill_check", return_value=False),
        patch.object(chase_mod, "_tick_size_sync", return_value=0.05),
        patch("backend.api.routes.orders_postback._pb_write_ledger_fills",
              new_callable=AsyncMock),
        patch("backend.api.routes.orders._subscribe_filled_pairs",
              new_callable=AsyncMock),
        patch("backend.api.algo.agent_engine.record_chase_terminal",
              new_callable=AsyncMock),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = chase_mod.ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=5)
        result = await chase_mod.chase_order(
            account="ZG0790", symbol="NIFTY24APR25000CE",
            transaction_type="BUY", quantity=50, cfg=cfg,
            algo_order_id=algo_order_id,
        )
        await _real_sleep(0)
        await _real_sleep(0)
    return result


@pytest.mark.asyncio
async def test_single_call_completes_end_to_end_and_lock_is_reusable():
    mock_row = _e2e_row(999960)

    result = await _run_single_real_chase(mock_row, 999960)

    assert result.status == ChaseStatus.FILLED
    assert mock_row.status == "FILLED"

    key = chase_mod._chase_position_lock_key("ZG0790", "NFO", "NIFTY24APR25000CE")
    lk = await chase_mod._get_chase_position_lock(key)
    assert lk.locked() is False, "lock must be released after a normal run"

    # Running it again for the exact same key must NOT be refused —
    # proves the lock isn't stuck "held" after a completed call.
    mock_row2 = _e2e_row(999961)
    result2 = await _run_single_real_chase(mock_row2, 999961)
    assert result2.status == ChaseStatus.FILLED
    assert "REFUSED" not in (result2.detail or "")


@pytest.mark.asyncio
async def test_single_call_lock_released_even_when_impl_raises():
    async def _raising_impl(*args, **kwargs):
        raise RuntimeError("boom")

    with patch.object(chase_mod, "_chase_order_impl", _raising_impl):
        with pytest.raises(RuntimeError):
            await chase_mod.chase_order(
                account="ACC_EXC", symbol="EXCSYM", transaction_type="BUY",
                quantity=10, cfg=ChaseConfig(exchange="NFO"),
            )

    key = chase_mod._chase_position_lock_key("ACC_EXC", "NFO", "EXCSYM")
    lk = await chase_mod._get_chase_position_lock(key)
    assert lk.locked() is False, "an exception inside the impl must not leak the lock"
