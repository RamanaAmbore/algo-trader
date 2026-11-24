"""Sprint 3 (auto-close-short-option plan), piece 6 — the auto-release
background scan.

Covers: fires once past the deadline; respects the kill switch
(status=='inactive' blocks, but 'cooldown'/'completed' do NOT — see
`_task_agent_close_auto_release`'s own docstring for why this
deliberately deviates from a literal `== 'active'` check); never fires
before the deadline; dispatches through the REAL `get_release_handler`,
not a hand-rolled release call.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _held_row(**kw) -> SimpleNamespace:
    base = dict(id=1, status="HELD", agent_id=42, hold_json=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _agent_close_hold_json(auto_release_at: "str | None") -> str:
    import json
    out = {"category": "agent_close", "reason": "x", "price_policy": "CHASE_MED",
          "override": None, "held_at": datetime.now(timezone.utc).isoformat()}
    if auto_release_at is not None:
        out["auto_release_at"] = auto_release_at
    return json.dumps(out)


def _queued_session(results: list):
    """A session mock whose `.execute()` pops the next pre-built result
    object off *results* on every call, in order — one call for the
    initial HELD-rows select, then one additional call PER ROW that
    reaches the agent-status lookup."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    it = iter(results)

    async def _exec(*a, **k):
        return next(it)

    session.execute = _exec
    return session


def _held_rows_result(rows: list):
    r = MagicMock()
    r.scalars.return_value.all.return_value = rows
    return r


def _agent_result(agent):
    r = MagicMock()
    r.scalar_one_or_none.return_value = agent
    return r


async def _run_one_iteration():
    """Runs `_task_agent_close_auto_release`'s body for exactly one
    iteration by cancelling it right after its first `asyncio.sleep`
    (patched to return instantly) completes and the try block finishes.
    Uses a sentinel exception raised from inside the loop's own
    `get_int` patch on the SECOND call to stop the infinite `while
    True:` after exactly one pass."""
    from backend.api import background as bg

    call_count = {"n": 0}

    async def _sleep_once(_seconds):
        call_count["n"] += 1
        if call_count["n"] > 1:
            raise asyncio.CancelledError()

    with patch("asyncio.sleep", new=_sleep_once):
        try:
            await bg._task_agent_close_auto_release()
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_fires_once_past_deadline_via_real_release_handler():
    from backend.api import background as bg

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    row = _held_row(hold_json=_agent_close_hold_json(past))
    agent = SimpleNamespace(id=42, status="active")

    session = _queued_session([_held_rows_result([row]), _agent_result(agent)])
    mock_handler = AsyncMock(return_value={"ok": True, "status": "OPEN"})
    mock_get_handler = MagicMock(return_value=mock_handler)

    with patch("backend.api.database.async_session", return_value=session), \
         patch("backend.api.algo.order_release.get_release_handler", mock_get_handler):
        await _run_one_iteration()

    mock_get_handler.assert_called_once_with("agent_close")
    mock_handler.assert_called_once_with(1, actor="auto-release")


@pytest.mark.asyncio
async def test_does_not_fire_before_the_deadline():
    from backend.api import background as bg

    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    row = _held_row(hold_json=_agent_close_hold_json(future))

    session = _queued_session([_held_rows_result([row])])
    mock_handler = AsyncMock()
    mock_get_handler = MagicMock(return_value=mock_handler)

    with patch("backend.api.database.async_session", return_value=session), \
         patch("backend.api.algo.order_release.get_release_handler", mock_get_handler):
        await _run_one_iteration()

    mock_handler.assert_not_called()


@pytest.mark.asyncio
async def test_never_auto_releases_when_auto_release_at_is_absent():
    past_but_irrelevant = _held_row(hold_json=_agent_close_hold_json(None))
    session = _queued_session([_held_rows_result([past_but_irrelevant])])
    mock_handler = AsyncMock()
    mock_get_handler = MagicMock(return_value=mock_handler)

    with patch("backend.api.database.async_session", return_value=session), \
         patch("backend.api.algo.order_release.get_release_handler", mock_get_handler):
        await _run_one_iteration()

    mock_handler.assert_not_called()


@pytest.mark.asyncio
async def test_skips_non_agent_close_category():
    import json
    non_agent_close = json.dumps({
        "category": "expiry_close", "reason": "x", "price_policy": "CHASE_MED",
        "override": None, "held_at": datetime.now(timezone.utc).isoformat(),
        "auto_release_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    })
    row = _held_row(hold_json=non_agent_close)
    session = _queued_session([_held_rows_result([row])])
    mock_handler = AsyncMock()
    mock_get_handler = MagicMock(return_value=mock_handler)

    with patch("backend.api.database.async_session", return_value=session), \
         patch("backend.api.algo.order_release.get_release_handler", mock_get_handler):
        await _run_one_iteration()

    mock_handler.assert_not_called()


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_inactive_agent_blocks_release(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        row = _held_row(hold_json=_agent_close_hold_json(past))
        agent = SimpleNamespace(id=42, status="inactive")

        session = _queued_session([_held_rows_result([row]), _agent_result(agent)])
        mock_handler = AsyncMock()
        mock_get_handler = MagicMock(return_value=mock_handler)

        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.algo.order_release.get_release_handler", mock_get_handler):
            await _run_one_iteration()

        mock_handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_agent_does_not_block_release(self):
        """DELIBERATE deviation from a literal `== 'active'` check — see
        the function's own docstring. A persistent agent's status
        transitions to 'cooldown' immediately after every fire, so a
        literal active-only check would make auto-release essentially
        never fire for a normal persistent agent."""
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        row = _held_row(hold_json=_agent_close_hold_json(past))
        agent = SimpleNamespace(id=42, status="cooldown")

        session = _queued_session([_held_rows_result([row]), _agent_result(agent)])
        mock_handler = AsyncMock(return_value={"ok": True, "status": "OPEN"})
        mock_get_handler = MagicMock(return_value=mock_handler)

        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.algo.order_release.get_release_handler", mock_get_handler):
            await _run_one_iteration()

        mock_handler.assert_called_once_with(1, actor="auto-release")

    @pytest.mark.asyncio
    async def test_completed_agent_does_not_block_release(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        row = _held_row(hold_json=_agent_close_hold_json(past))
        agent = SimpleNamespace(id=42, status="completed")

        session = _queued_session([_held_rows_result([row]), _agent_result(agent)])
        mock_handler = AsyncMock(return_value={"ok": True, "status": "OPEN"})
        mock_get_handler = MagicMock(return_value=mock_handler)

        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.algo.order_release.get_release_handler", mock_get_handler):
            await _run_one_iteration()

        mock_handler.assert_called_once_with(1, actor="auto-release")

    @pytest.mark.asyncio
    async def test_missing_agent_row_blocks_release(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        row = _held_row(hold_json=_agent_close_hold_json(past))

        session = _queued_session([_held_rows_result([row]), _agent_result(None)])
        mock_handler = AsyncMock()
        mock_get_handler = MagicMock(return_value=mock_handler)

        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.algo.order_release.get_release_handler", mock_get_handler):
            await _run_one_iteration()

        mock_handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_agent_id_blocks_release(self):
        """No agent to check -> fail closed, never release."""
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        row = _held_row(agent_id=None, hold_json=_agent_close_hold_json(past))

        session = _queued_session([_held_rows_result([row])])
        mock_handler = AsyncMock()
        mock_get_handler = MagicMock(return_value=mock_handler)

        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.algo.order_release.get_release_handler", mock_get_handler):
            await _run_one_iteration()

        mock_handler.assert_not_called()


class TestSweepPerRowIsolation:
    """Audit finding #13 (2026-11-24): one row's exception must never
    abort the rest of the sweep tick."""

    @pytest.mark.asyncio
    async def test_one_bad_row_does_not_block_a_later_due_row(self):
        from backend.api import background as bg

        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        bad_row = _held_row(id=1, hold_json="not even json")
        good_row = _held_row(id=2, hold_json=_agent_close_hold_json(past))

        mock_handler = AsyncMock(return_value={"ok": True, "reason": "released"})

        session = _queued_session([
            _held_rows_result([bad_row, good_row]),
            _agent_result(SimpleNamespace(status="active")),
        ])
        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.algo.order_release.get_release_handler",
                   MagicMock(return_value=mock_handler)):
            released = await bg._agent_close_auto_release_sweep_once(
                now=datetime.now(timezone.utc))

        assert released == 1
        mock_handler.assert_called_once_with(2, actor="auto-release")

    @pytest.mark.asyncio
    async def test_handler_exception_on_one_row_does_not_block_another(self):
        from backend.api import background as bg

        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        row_a = _held_row(id=1, hold_json=_agent_close_hold_json(past))
        row_b = _held_row(id=2, hold_json=_agent_close_hold_json(past))

        async def _handler(order_id, actor):
            if order_id == 1:
                raise RuntimeError("boom")
            return {"ok": True, "reason": "released"}

        session = _queued_session([
            _held_rows_result([row_a, row_b]),
            _agent_result(SimpleNamespace(status="active")),
            _agent_result(SimpleNamespace(status="active")),
        ])
        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.algo.order_release.get_release_handler",
                   MagicMock(return_value=_handler)):
            released = await bg._agent_close_auto_release_sweep_once(
                now=datetime.now(timezone.utc))

        assert released == 1, "row 2 must still release despite row 1's exception"


@pytest.mark.asyncio
async def test_no_held_rows_is_a_clean_noop():
    session = _queued_session([_held_rows_result([])])
    with patch("backend.api.database.async_session", return_value=session):
        await _run_one_iteration()
    # No exception == pass.


@pytest.mark.asyncio
async def test_bad_sweep_iteration_is_caught_and_logged_not_raised():
    """A DB failure on the initial sweep query must not crash the
    supervised loop — matching every other periodic task's own
    try/except convention."""
    class _Boom:
        def __call__(self):
            raise RuntimeError("db down")

    with patch("backend.api.database.async_session", _Boom()):
        await _run_one_iteration()
    # No exception == pass.


# ═══════════════════════════════════════════════════════════════════════
# Audit finding #2 (2026-11-24) — stuck-OPEN recovery
# ═══════════════════════════════════════════════════════════════════════

def _stuck_open_row(**kw) -> SimpleNamespace:
    base = dict(
        id=201, status="OPEN", broker_order_id="", mode="live", agent_id=42,
        account="ZG0790", symbol="CRUDEOIL26NOV5500CE", exchange="MCX",
        transaction_type="SELL", quantity=1, product="NRML",
        detail="Pending: expiry close · released by auto-release at 2026-11-24T10:00:00+00:00",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _rows_session(rows: list):
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session.execute = AsyncMock(return_value=result)
    return session


class TestRecoverStuckOpenAgentCloseRows:
    def setup_method(self):
        from backend.api import background as bg
        bg._STUCK_OPEN_RETRIED.clear()
        bg._STUCK_OPEN_ALERTED.clear()

    @pytest.mark.asyncio
    async def test_active_chase_is_never_raced(self):
        """A real chase genuinely still working this row must never be
        retried a second time — the one trap this fix must avoid."""
        from backend.api import background as bg
        row = _stuck_open_row()
        session = _rows_session([row])

        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.algo.chase.is_chase_active", return_value=True), \
             patch("asyncio.create_task") as mock_create_task:
            retried = await bg._recover_stuck_open_agent_close_rows()

        assert retried == 0
        mock_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_detection_retries_the_chase_once(self):
        from backend.api import background as bg
        row = _stuck_open_row()
        session = _rows_session([row])

        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.algo.chase.is_chase_active", return_value=False), \
             patch("asyncio.create_task") as mock_create_task, \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert:
            retried = await bg._recover_stuck_open_agent_close_rows()

        assert retried == 1
        mock_create_task.assert_called_once()
        mock_create_task.call_args[0][0].close()  # avoid "never awaited" noise
        mock_alert.assert_not_called(), "no alert on the first, un-retried detection"
        assert row.id in bg._STUCK_OPEN_RETRIED

    @pytest.mark.asyncio
    async def test_still_stuck_after_retry_alerts_instead_of_retrying_again(self):
        from backend.api import background as bg
        row = _stuck_open_row()
        bg._STUCK_OPEN_RETRIED.add(row.id)  # already retried on a prior sweep
        session = _rows_session([row])

        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.algo.chase.is_chase_active", return_value=False), \
             patch("asyncio.create_task") as mock_create_task, \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert:
            retried = await bg._recover_stuck_open_agent_close_rows()

        assert retried == 0, "must not retry a second time — escalate instead"
        mock_create_task.assert_not_called()
        mock_alert.assert_called_once()
        assert row.id in bg._STUCK_OPEN_ALERTED

    @pytest.mark.asyncio
    async def test_alert_fires_only_once_per_row_for_process_lifetime(self):
        from backend.api import background as bg
        row = _stuck_open_row()
        bg._STUCK_OPEN_RETRIED.add(row.id)
        bg._STUCK_OPEN_ALERTED.add(row.id)  # already alerted on a prior sweep
        session = _rows_session([row])

        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.algo.chase.is_chase_active", return_value=False), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert:
            await bg._recover_stuck_open_agent_close_rows()

        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_matching_rows_is_a_clean_noop(self):
        from backend.api import background as bg
        session = _rows_session([])
        with patch("backend.api.database.async_session", return_value=session):
            retried = await bg._recover_stuck_open_agent_close_rows()
        assert retried == 0

    @pytest.mark.asyncio
    async def test_one_bad_row_does_not_block_the_rest(self):
        from backend.api import background as bg
        bad_row = _stuck_open_row(id=301)
        good_row = _stuck_open_row(id=302)
        session = _rows_session([bad_row, good_row])

        def _is_active(row_id):
            if row_id == 301:
                raise RuntimeError("boom")
            return False

        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.algo.chase.is_chase_active", side_effect=_is_active), \
             patch("asyncio.create_task") as mock_create_task:
            retried = await bg._recover_stuck_open_agent_close_rows()

        assert retried == 1
        mock_create_task.assert_called_once()
        mock_create_task.call_args[0][0].close()
