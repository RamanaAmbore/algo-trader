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
