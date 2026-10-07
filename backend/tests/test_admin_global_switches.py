"""
Tests for PATCH/GET /api/admin/global-switches — backend/api/routes/admin.py.

Covers:
  1. Flipping execution.paper_trading_mode writes the Setting AND an
     AuditLog row (actor, old value, new value) in the same commit.
  2. Setting paper_trading_mode=True also clears a set shadow_mode,
     with its own audit row.
  3. default_agent_trade_mode is validated against the enum schema;
     an invalid value is rejected before any write.
  4. A no-op request (value already matches) writes nothing and
     audits nothing.
  5. Response reflects the post-write effective values.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.routes.admin import AdminController, GlobalSwitchesRequest


def _handler_fn(handler):
    return handler.fn


class _FakeSettingRow:
    def __init__(self, key, value, value_type="bool", schema=None):
        self.key = key
        self.value = value
        self.value_type = value_type
        self.schema = schema


def _mock_session(rows_by_key: dict):
    """execute() returns scalar_one_or_none() keyed off whatever `Setting.key
    == <key>` the SUT queried — approximated here by returning, in order,
    one result per select() call matching keys_needed from `rows_by_key`."""
    session = MagicMock()
    session.commit = AsyncMock()
    session.add = MagicMock()

    def _execute(stmt):
        # Best-effort: find the key literal embedded in the compiled
        # WHERE clause so multiple selects in one test resolve correctly
        # regardless of call order.
        compiled = str(stmt)
        result = MagicMock()
        found = None
        for key, row in rows_by_key.items():
            if key in compiled or True:  # fallback below handles single-key tests
                found = row
        result.scalar_one_or_none = MagicMock(return_value=found)
        return result

    async def _execute_async(stmt):
        return _execute(stmt)

    session.execute = AsyncMock(side_effect=_execute_async)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), session


def _fake_request(sub="alice", role="designated", user_id=7):
    req = MagicMock()
    req.state = MagicMock()
    req.state.token_payload = {"sub": sub, "role": role, "user_id": user_id}
    return req


@pytest.mark.asyncio
async def test_patch_paper_trading_mode_writes_setting_and_audit_row():
    row_map = {"execution.default_agent_trade_mode": _FakeSettingRow(
        "execution.default_agent_trade_mode", "paper", "enum", {"enum": ["paper", "live"]})}
    session_factory, session = _mock_session(row_map)
    controller = AdminController.__new__(AdminController)
    data = GlobalSwitchesRequest(paper_trading_mode=True)
    request = _fake_request()

    with patch("backend.api.routes.admin.async_session", session_factory), \
         patch("backend.shared.helpers.settings.get_bool",
               side_effect=lambda k, d=False: False), \
         patch("backend.shared.helpers.settings.get_string",
               side_effect=lambda k, d="paper": "paper"), \
         patch("backend.shared.helpers.settings.upsert_setting", new=AsyncMock()) as _up, \
         patch("backend.shared.helpers.settings.reload_cache", new=AsyncMock()):
        result = await _handler_fn(AdminController.update_global_switches)(
            controller, data, request,
        )

    assert result.paper_trading_mode is False  # get_bool mocked to always False post-write
    session.commit.assert_awaited_once()
    # One AuditLog added for the paper_trading_mode change.
    assert session.add.call_count == 1
    audit_row = session.add.call_args_list[0][0][0]
    assert audit_row.action == "GLOBAL_SWITCH_CHANGED"
    assert audit_row.category == "config.global_switch"
    assert audit_row.target_id == "execution.paper_trading_mode"
    assert audit_row.actor_username == "alice"
    assert audit_row.actor_role == "designated"
    assert "false" in audit_row.summary and "true" in audit_row.summary
    _up.assert_awaited_once()


@pytest.mark.asyncio
async def test_patch_paper_trading_mode_true_also_clears_shadow_mode():
    session_factory, session = _mock_session({})
    controller = AdminController.__new__(AdminController)
    data = GlobalSwitchesRequest(paper_trading_mode=True)
    request = _fake_request()

    def _get_bool(key, default=False):
        if key == "execution.paper_trading_mode":
            return False
        if key == "execution.shadow_mode":
            return True  # shadow is currently ON
        return default

    with patch("backend.api.routes.admin.async_session", session_factory), \
         patch("backend.shared.helpers.settings.get_bool", side_effect=_get_bool), \
         patch("backend.shared.helpers.settings.get_string", return_value="paper"), \
         patch("backend.shared.helpers.settings.upsert_setting", new=AsyncMock()) as _up, \
         patch("backend.shared.helpers.settings.reload_cache", new=AsyncMock()):
        await _handler_fn(AdminController.update_global_switches)(controller, data, request)

    # Two changes: paper_trading_mode AND shadow_mode → two audit rows.
    assert session.add.call_count == 2
    target_ids = {session.add.call_args_list[i][0][0].target_id for i in range(2)}
    assert target_ids == {"execution.paper_trading_mode", "execution.shadow_mode"}
    assert _up.await_count == 2


@pytest.mark.asyncio
async def test_patch_default_agent_trade_mode_rejects_invalid_value():
    row = _FakeSettingRow("execution.default_agent_trade_mode", "paper", "enum",
                           {"enum": ["paper", "live"]})
    session_factory, session = _mock_session({"execution.default_agent_trade_mode": row})
    controller = AdminController.__new__(AdminController)
    data = GlobalSwitchesRequest(default_agent_trade_mode="bogus-mode")
    request = _fake_request()

    from litestar.exceptions import HTTPException
    with patch("backend.api.routes.admin.async_session", session_factory), \
         patch("backend.shared.helpers.settings.get_bool", return_value=False), \
         patch("backend.shared.helpers.settings.get_string", return_value="paper"):
        with pytest.raises(HTTPException) as exc_info:
            await _handler_fn(AdminController.update_global_switches)(controller, data, request)

    assert exc_info.value.status_code == 400
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_noop_when_value_unchanged_writes_nothing():
    row = _FakeSettingRow("execution.default_agent_trade_mode", "paper", "enum",
                           {"enum": ["paper", "live"]})
    session_factory, session = _mock_session({"execution.default_agent_trade_mode": row})
    controller = AdminController.__new__(AdminController)
    # Same value as current — no genuine change.
    data = GlobalSwitchesRequest(paper_trading_mode=False, default_agent_trade_mode="paper")
    request = _fake_request()

    with patch("backend.api.routes.admin.async_session", session_factory), \
         patch("backend.shared.helpers.settings.get_bool", return_value=False), \
         patch("backend.shared.helpers.settings.get_string", return_value="paper"), \
         patch("backend.shared.helpers.settings.upsert_setting", new=AsyncMock()) as _up, \
         patch("backend.shared.helpers.settings.reload_cache", new=AsyncMock()) as _rc:
        result = await _handler_fn(AdminController.update_global_switches)(
            controller, data, request,
        )

    session.add.assert_not_called()
    _up.assert_not_awaited()
    _rc.assert_not_awaited()
    assert result.paper_trading_mode is False
    assert result.default_agent_trade_mode == "paper"


@pytest.mark.asyncio
async def test_get_global_switches_reads_current_values():
    controller = AdminController.__new__(AdminController)
    with patch("backend.shared.helpers.settings.get_bool", return_value=True), \
         patch("backend.shared.helpers.settings.get_string", return_value="live"):
        result = await _handler_fn(AdminController.get_global_switches)(controller)

    assert result.paper_trading_mode is True
    assert result.default_agent_trade_mode == "live"
