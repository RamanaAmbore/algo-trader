"""
Tests for backend/shared/helpers/settings.py — upsert_setting / set_bool /
set_string (the write side of the DB-backed settings table; previously
read-only via get_int/get_float/get_bool/get_string).

Covers:
  1. set_bool / set_string insert a missing row and return None (no
     prior value).
  2. set_bool / set_string update an existing row and return the
     previous value.
  3. upsert_setting updates the in-process cache synchronously (no
     session passed) so an immediate get_bool/get_string in the same
     call sees the new value without waiting on the async
     invalidate_cache() background task.
  4. upsert_setting(session=...) defers commit to the caller — lets a
     Setting write and an AuditLog insert land in one atomic
     transaction (used by PATCH /api/admin/global-switches).
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.shared.helpers import settings as settings_mod
from backend.shared.helpers.settings import (
    set_bool, set_int, set_string, upsert_setting, get_bool, get_int,
)


class _FakeSettingRow:
    def __init__(self, key, value, value_type="bool", schema=None):
        self.key = key
        self.value = value
        self.value_type = value_type
        self.schema = schema
        self.category = key.split(".", 1)[0]
        self.default_value = value
        self.description = ""


def _mock_session(existing=None):
    """MagicMock async_session() context manager; execute().scalar_one_or_none()
    returns `existing`."""
    session = MagicMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=existing)
    session.execute = AsyncMock(return_value=result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), session


@pytest.fixture(autouse=True)
def _clean_cache():
    settings_mod._CACHE.clear()
    yield
    settings_mod._CACHE.clear()


@pytest.mark.asyncio
async def test_set_bool_inserts_missing_row_and_returns_none():
    session_factory, session = _mock_session(existing=None)
    with patch("backend.api.database.async_session", session_factory):
        old = await set_bool("execution.some_new_flag", True)

    assert old is None
    session.add.assert_called_once()
    added = session.add.call_args[0][0]
    assert added.value == "true"
    assert added.value_type == "bool"
    assert added.category == "execution"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_bool_updates_existing_row_and_returns_old_value():
    row = _FakeSettingRow("execution.paper_trading_mode", "false", "bool")
    session_factory, session = _mock_session(existing=row)
    with patch("backend.api.database.async_session", session_factory):
        old = await set_bool("execution.paper_trading_mode", True)

    assert old is False
    assert row.value == "true"
    session.add.assert_not_called()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_int_inserts_missing_row_and_returns_none():
    session_factory, session = _mock_session(existing=None)
    with patch("backend.api.database.async_session", session_factory):
        old = await set_int("hold.lead_minutes_mcx", 45)

    assert old is None
    session.add.assert_called_once()
    added = session.add.call_args[0][0]
    assert added.value == "45"
    assert added.value_type == "int"
    assert added.category == "hold"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_int_updates_existing_row_and_returns_old_value():
    row = _FakeSettingRow("hold.lead_minutes_mcx", "30", "int")
    session_factory, session = _mock_session(existing=row)
    with patch("backend.api.database.async_session", session_factory):
        old = await set_int("hold.lead_minutes_mcx", 45)

    assert old == 30
    assert row.value == "45"
    session.add.assert_not_called()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_int_tolerates_stored_float_style_old_value():
    """A stored "30.0"-style string (legacy serialisation) must still
    coerce to int 30 for the returned previous value."""
    row = _FakeSettingRow("hold.lead_minutes_mcx", "30.0", "int")
    session_factory, _ = _mock_session(existing=row)
    with patch("backend.api.database.async_session", session_factory):
        old = await set_int("hold.lead_minutes_mcx", 45)

    assert old == 30
    assert row.value == "45"


@pytest.mark.asyncio
async def test_set_int_updates_in_process_cache_synchronously():
    row = _FakeSettingRow("hold.lead_minutes_mcx", "30", "int")
    session_factory, _ = _mock_session(existing=row)
    with patch("backend.api.database.async_session", session_factory):
        await set_int("hold.lead_minutes_mcx", 45)

    assert get_int("hold.lead_minutes_mcx", 0) == 45


@pytest.mark.asyncio
async def test_set_string_updates_existing_row_and_returns_old_value():
    row = _FakeSettingRow("execution.default_agent_trade_mode", "paper", "enum")
    session_factory, session = _mock_session(existing=row)
    with patch("backend.api.database.async_session", session_factory):
        old = await set_string("execution.default_agent_trade_mode", "live")

    assert old == "paper"
    assert row.value == "live"


@pytest.mark.asyncio
async def test_set_bool_updates_in_process_cache_synchronously():
    """No `session=` passed → upsert_setting must refresh _CACHE itself
    (not just schedule the async invalidate_cache() task) so an
    immediate get_bool() call in the same request sees the new value."""
    row = _FakeSettingRow("execution.paper_trading_mode", "false", "bool")
    session_factory, _ = _mock_session(existing=row)
    with patch("backend.api.database.async_session", session_factory):
        await set_bool("execution.paper_trading_mode", True)

    assert get_bool("execution.paper_trading_mode", False) is True


@pytest.mark.asyncio
async def test_upsert_setting_with_external_session_defers_commit():
    """When a caller passes its own session, upsert_setting must NOT
    commit — the caller owns the transaction (e.g. to fold a Setting
    write and an AuditLog insert into one atomic unit)."""
    row = _FakeSettingRow("execution.paper_trading_mode", "false", "bool")
    session = MagicMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=row)
    session.execute = AsyncMock(return_value=result)

    old = await upsert_setting("execution.paper_trading_mode", "true", "bool",
                                session=session)

    assert old == "false"
    assert row.value == "true"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_setting_with_external_session_inserts_without_commit():
    session = MagicMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=result)

    old = await upsert_setting("execution.brand_new_key", "live", "enum",
                                category="execution", session=session)

    assert old is None
    session.add.assert_called_once()
    session.commit.assert_not_awaited()
