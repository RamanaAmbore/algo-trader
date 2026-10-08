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
  6. expiry_close_hold_enabled / template_exit_hold_enabled surface the
     existing `hold.expiry_close_released` / `hold.template_exit_released`
     settings — INVERTED (storage `released=True` means operator-facing
     `hold_enabled=False`) — on both GET and PATCH, in both directions.
  7. expiry_close_lead_minutes_mcx / _nfo surface `hold.lead_minutes_mcx`
     / `hold.lead_minutes_nfo`; negative values are rejected; a PATCH to
     one hold field does not touch the other existing switches; the
     write path and order_hold_gate.cutoff_for()'s read path agree.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.routes.admin import AdminController, GlobalSwitchesRequest
from backend.api.algo.order_hold_gate import (
    EXPIRY_CLOSE_RELEASED_KEY, TEMPLATE_EXIT_RELEASED_KEY,
)


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
         patch("backend.shared.helpers.settings.get_string", return_value="live"), \
         patch("backend.shared.helpers.settings.get_int", side_effect=lambda k, d=0: d):
        result = await _handler_fn(AdminController.get_global_switches)(controller)

    assert result.paper_trading_mode is True
    assert result.default_agent_trade_mode == "live"


# ---------------------------------------------------------------------------
# expiry_close_hold_enabled / template_exit_hold_enabled — inversion tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_global_switches_hold_fields_inverted_correctly():
    """storage released=True -> response hold_enabled=False (and the
    reverse), for BOTH expiry_close and template_exit — the one place a
    naming mismatch could silently flip the meaning."""
    controller = AdminController.__new__(AdminController)

    def _get_bool(key, default=False):
        if key == EXPIRY_CLOSE_RELEASED_KEY:
            return True    # released -> NOT held
        if key == TEMPLATE_EXIT_RELEASED_KEY:
            return False   # not released -> held
        return default

    with patch("backend.shared.helpers.settings.get_bool", side_effect=_get_bool), \
         patch("backend.shared.helpers.settings.get_string", return_value="paper"), \
         patch("backend.shared.helpers.settings.get_int", side_effect=lambda k, d=0: d):
        result = await _handler_fn(AdminController.get_global_switches)(controller)

    assert result.expiry_close_hold_enabled is False
    assert result.template_exit_hold_enabled is True
    assert result.expiry_close_lead_minutes_mcx == 30
    assert result.expiry_close_lead_minutes_nfo == 15


@pytest.mark.asyncio
async def test_patch_expiry_close_hold_enabled_true_writes_released_false():
    """Operator sets expiry_close_hold_enabled=True (wants closes held).
    Currently released=True (not held) -> must write released=False."""
    session_factory, session = _mock_session({})
    controller = AdminController.__new__(AdminController)
    data = GlobalSwitchesRequest(expiry_close_hold_enabled=True)
    request = _fake_request()

    def _get_bool(key, default=False):
        if key == EXPIRY_CLOSE_RELEASED_KEY:
            return True
        return default

    with patch("backend.api.routes.admin.async_session", session_factory), \
         patch("backend.shared.helpers.settings.get_bool", side_effect=_get_bool), \
         patch("backend.shared.helpers.settings.get_string", return_value="paper"), \
         patch("backend.shared.helpers.settings.get_int", side_effect=lambda k, d=0: d), \
         patch("backend.shared.helpers.settings.upsert_setting", new=AsyncMock()) as _up, \
         patch("backend.shared.helpers.settings.reload_cache", new=AsyncMock()):
        await _handler_fn(AdminController.update_global_switches)(controller, data, request)

    assert session.add.call_count == 1
    audit_row = session.add.call_args_list[0][0][0]
    assert audit_row.target_id == EXPIRY_CLOSE_RELEASED_KEY
    assert audit_row.summary == f"{EXPIRY_CLOSE_RELEASED_KEY}: 'true' -> 'false'"
    _up.assert_awaited_once()
    args, kwargs = _up.call_args
    assert args[0] == EXPIRY_CLOSE_RELEASED_KEY
    assert args[1] == "false"
    assert args[2] == "bool"
    assert kwargs.get("category") == "hold"


@pytest.mark.asyncio
async def test_patch_expiry_close_hold_enabled_false_writes_released_true():
    """Reverse direction: operator sets hold_enabled=False (wants closes
    released). Currently released=False (held) -> must write released=True."""
    session_factory, session = _mock_session({})
    controller = AdminController.__new__(AdminController)
    data = GlobalSwitchesRequest(expiry_close_hold_enabled=False)
    request = _fake_request()

    def _get_bool(key, default=False):
        if key == EXPIRY_CLOSE_RELEASED_KEY:
            return False
        return default

    with patch("backend.api.routes.admin.async_session", session_factory), \
         patch("backend.shared.helpers.settings.get_bool", side_effect=_get_bool), \
         patch("backend.shared.helpers.settings.get_string", return_value="paper"), \
         patch("backend.shared.helpers.settings.get_int", side_effect=lambda k, d=0: d), \
         patch("backend.shared.helpers.settings.upsert_setting", new=AsyncMock()) as _up, \
         patch("backend.shared.helpers.settings.reload_cache", new=AsyncMock()):
        await _handler_fn(AdminController.update_global_switches)(controller, data, request)

    assert session.add.call_count == 1
    audit_row = session.add.call_args_list[0][0][0]
    assert audit_row.target_id == EXPIRY_CLOSE_RELEASED_KEY
    assert audit_row.summary == f"{EXPIRY_CLOSE_RELEASED_KEY}: 'false' -> 'true'"
    args, kwargs = _up.call_args
    assert args[1] == "true"


@pytest.mark.asyncio
async def test_patch_expiry_close_hold_enabled_noop_when_already_matching():
    """hold_enabled=False with released already True (i.e. already not
    held) is a no-op — no write, no audit row."""
    session_factory, session = _mock_session({})
    controller = AdminController.__new__(AdminController)
    data = GlobalSwitchesRequest(expiry_close_hold_enabled=False)
    request = _fake_request()

    def _get_bool(key, default=False):
        if key == EXPIRY_CLOSE_RELEASED_KEY:
            return True
        return default

    with patch("backend.api.routes.admin.async_session", session_factory), \
         patch("backend.shared.helpers.settings.get_bool", side_effect=_get_bool), \
         patch("backend.shared.helpers.settings.get_string", return_value="paper"), \
         patch("backend.shared.helpers.settings.get_int", side_effect=lambda k, d=0: d), \
         patch("backend.shared.helpers.settings.upsert_setting", new=AsyncMock()) as _up, \
         patch("backend.shared.helpers.settings.reload_cache", new=AsyncMock()) as _rc:
        await _handler_fn(AdminController.update_global_switches)(controller, data, request)

    session.add.assert_not_called()
    _up.assert_not_awaited()
    _rc.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_template_exit_hold_enabled_true_writes_released_false():
    """Same inversion contract as expiry_close, applied to the sibling
    template-exit hold switch."""
    session_factory, session = _mock_session({})
    controller = AdminController.__new__(AdminController)
    data = GlobalSwitchesRequest(template_exit_hold_enabled=True)
    request = _fake_request()

    def _get_bool(key, default=False):
        if key == TEMPLATE_EXIT_RELEASED_KEY:
            return True
        return default

    with patch("backend.api.routes.admin.async_session", session_factory), \
         patch("backend.shared.helpers.settings.get_bool", side_effect=_get_bool), \
         patch("backend.shared.helpers.settings.get_string", return_value="paper"), \
         patch("backend.shared.helpers.settings.get_int", side_effect=lambda k, d=0: d), \
         patch("backend.shared.helpers.settings.upsert_setting", new=AsyncMock()) as _up, \
         patch("backend.shared.helpers.settings.reload_cache", new=AsyncMock()):
        await _handler_fn(AdminController.update_global_switches)(controller, data, request)

    assert session.add.call_count == 1
    audit_row = session.add.call_args_list[0][0][0]
    assert audit_row.target_id == TEMPLATE_EXIT_RELEASED_KEY
    assert audit_row.summary == f"{TEMPLATE_EXIT_RELEASED_KEY}: 'true' -> 'false'"
    args, kwargs = _up.call_args
    assert args[1] == "false"
    assert kwargs.get("category") == "hold"


# ---------------------------------------------------------------------------
# expiry_close_lead_minutes_mcx / _nfo
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_lead_minutes_mcx_only_does_not_affect_other_fields():
    """PATCH touching only a hold-lead field must leave paper_trading_mode
    and default_agent_trade_mode untouched — regression against cross-
    field interference."""
    session_factory, session = _mock_session({})
    controller = AdminController.__new__(AdminController)
    data = GlobalSwitchesRequest(expiry_close_lead_minutes_mcx=45)
    request = _fake_request()

    with patch("backend.api.routes.admin.async_session", session_factory), \
         patch("backend.shared.helpers.settings.get_bool", return_value=False), \
         patch("backend.shared.helpers.settings.get_string", return_value="paper"), \
         patch("backend.shared.helpers.settings.get_int",
               side_effect=lambda k, d=0: 30 if k == "hold.lead_minutes_mcx" else d), \
         patch("backend.shared.helpers.settings.upsert_setting", new=AsyncMock()) as _up, \
         patch("backend.shared.helpers.settings.reload_cache", new=AsyncMock()):
        await _handler_fn(AdminController.update_global_switches)(controller, data, request)

    # Exactly one change, for the MCX lead-minutes key only.
    assert session.add.call_count == 1
    audit_row = session.add.call_args_list[0][0][0]
    assert audit_row.target_id == "hold.lead_minutes_mcx"
    assert audit_row.summary == "hold.lead_minutes_mcx: '30' -> '45'"
    _up.assert_awaited_once()
    args, kwargs = _up.call_args
    assert args[0] == "hold.lead_minutes_mcx"
    assert args[1] == "45"
    assert args[2] == "int"
    assert kwargs.get("category") == "hold"


@pytest.mark.asyncio
async def test_patch_lead_minutes_negative_rejected():
    from litestar.exceptions import HTTPException
    session_factory, session = _mock_session({})
    controller = AdminController.__new__(AdminController)
    data = GlobalSwitchesRequest(expiry_close_lead_minutes_nfo=-5)
    request = _fake_request()

    with patch("backend.api.routes.admin.async_session", session_factory), \
         patch("backend.shared.helpers.settings.get_bool", return_value=False), \
         patch("backend.shared.helpers.settings.get_string", return_value="paper"), \
         patch("backend.shared.helpers.settings.get_int", side_effect=lambda k, d=0: d):
        with pytest.raises(HTTPException) as exc_info:
            await _handler_fn(AdminController.update_global_switches)(controller, data, request)

    assert exc_info.value.status_code == 400
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_lead_minutes_mcx_integration_with_cutoff_for():
    """The write path (this route, via upsert_setting) and the read path
    (order_hold_gate.cutoff_for, via a module-level `get_int` bound at
    gate-module import time) agree on the post-PATCH value — proving the
    PATCH actually reaches the gate's own cache read, not just the
    mocked settings surface."""
    from backend.shared.helpers import settings as settings_mod
    from backend.api.algo import order_hold_gate as gate
    from datetime import datetime
    from zoneinfo import ZoneInfo

    session_factory, session = _mock_session({})
    controller = AdminController.__new__(AdminController)
    data = GlobalSwitchesRequest(expiry_close_lead_minutes_mcx=45)
    request = _fake_request()

    async def _fake_upsert(key, value_str, value_type, *, category=None,
                            description=None, session=None):
        settings_mod._CACHE[key] = value_str
        return None

    had_key = "hold.lead_minutes_mcx" in settings_mod._CACHE
    prev_val = settings_mod._CACHE.get("hold.lead_minutes_mcx")
    try:
        with patch("backend.api.routes.admin.async_session", session_factory), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.shared.helpers.settings.get_string", return_value="paper"), \
             patch("backend.shared.helpers.settings.upsert_setting",
                   new=AsyncMock(side_effect=_fake_upsert)), \
             patch("backend.shared.helpers.settings.reload_cache", new=AsyncMock()):
            await _handler_fn(AdminController.update_global_switches)(controller, data, request)

        now = datetime(2026, 10, 15, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        # MCX close 23:30, lead 45 min -> cutoff 22:45.
        assert gate.cutoff_for("MCX", now).strftime("%H:%M") == "22:45"
    finally:
        if had_key:
            settings_mod._CACHE["hold.lead_minutes_mcx"] = prev_val
        else:
            settings_mod._CACHE.pop("hold.lead_minutes_mcx", None)


@pytest.mark.asyncio
async def test_patch_lead_minutes_nfo_integration_with_cutoff_for():
    from backend.shared.helpers import settings as settings_mod
    from backend.api.algo import order_hold_gate as gate
    from datetime import datetime
    from zoneinfo import ZoneInfo

    session_factory, session = _mock_session({})
    controller = AdminController.__new__(AdminController)
    data = GlobalSwitchesRequest(expiry_close_lead_minutes_nfo=20)
    request = _fake_request()

    async def _fake_upsert(key, value_str, value_type, *, category=None,
                            description=None, session=None):
        settings_mod._CACHE[key] = value_str
        return None

    had_key = "hold.lead_minutes_nfo" in settings_mod._CACHE
    prev_val = settings_mod._CACHE.get("hold.lead_minutes_nfo")
    try:
        with patch("backend.api.routes.admin.async_session", session_factory), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.shared.helpers.settings.get_string", return_value="paper"), \
             patch("backend.shared.helpers.settings.upsert_setting",
                   new=AsyncMock(side_effect=_fake_upsert)), \
             patch("backend.shared.helpers.settings.reload_cache", new=AsyncMock()):
            await _handler_fn(AdminController.update_global_switches)(controller, data, request)

        now = datetime(2026, 10, 15, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        # NFO close 15:30, lead 20 min -> cutoff 15:10.
        assert gate.cutoff_for("NFO", now).strftime("%H:%M") == "15:10"
    finally:
        if had_key:
            settings_mod._CACHE["hold.lead_minutes_nfo"] = prev_val
        else:
            settings_mod._CACHE.pop("hold.lead_minutes_nfo", None)
