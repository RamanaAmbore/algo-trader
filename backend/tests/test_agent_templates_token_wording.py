"""
Sprint 14 (2026-10, post-Sprint-12 audit) — operator-facing wording fix.

backend/api/routes/agent_templates.py's HTTPException `detail` strings
said "Fragment"/"fragment" — stale vocabulary from before Sprint 10
renamed the operator-facing concept to "Token" (display-layer only; the
underlying table/model/route path stay named Fragment/AgentTemplate
internally, per Sprint 10's documented convention). These strings
surface verbatim in TokensTab's toasts, so an operator deleting or
creating a token would still see "Fragment" in the error message.

This is a pure string reword — no logic, table name, or variable name
changed. These tests assert the NEW wording and the absence of the OLD
wording in every reachable HTTPException/success-detail string.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar.exceptions import HTTPException

from backend.api.routes import agent_templates as at


def _handler_fn(handler):
    return handler.fn


def _mock_session(existing=None):
    """Same mocking shape as test_agent_kind_tier_topic.py's helper —
    reimplemented locally so this file has no cross-test-module import
    dependency."""
    session = MagicMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    session.delete = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=existing)
    session.execute = AsyncMock(return_value=result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), session


class _FakeFragmentRow:
    def __init__(self, **kw):
        self.id = 7
        self.kind = "notify"
        self.name = "ops-alert"
        self.body = [{"channel": "telegram", "enabled": True}]
        self.description = ""
        self.is_system = False
        self.is_active = True
        self.created_at = None
        self.updated_at = None
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture(autouse=True)
def _no_reload(monkeypatch):
    """Short-circuit TemplateRegistry.reload() — not under test here."""
    async def _noop():
        return None
    monkeypatch.setattr(at, "_reload_registry", AsyncMock(side_effect=lambda: None))


@pytest.mark.asyncio
async def test_get_fragment_404_says_token_not_fragment():
    session_factory, _ = _mock_session(existing=None)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)

    with patch.object(at, "async_session", session_factory):
        with pytest.raises(HTTPException) as exc:
            await _handler_fn(at.AgentTemplateController.get_fragment)(controller, 99)

    assert exc.value.status_code == 404
    assert "Token 99 not found" == exc.value.detail
    assert "Fragment" not in exc.value.detail


@pytest.mark.asyncio
async def test_create_fragment_duplicate_says_token_not_fragment():
    existing_row = _FakeFragmentRow(kind="notify", name="ops-alert")
    session_factory, _ = _mock_session(existing=existing_row)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)
    data = at.FragmentCreate(kind="notify", name="ops-alert",
                              body=[{"channel": "telegram", "enabled": True}])

    with patch.object(at, "async_session", session_factory):
        with pytest.raises(HTTPException) as exc:
            await _handler_fn(at.AgentTemplateController.create_fragment)(controller, data)

    assert exc.value.status_code == 409
    assert exc.value.detail == "Token notify/ops-alert already exists"
    assert "Fragment" not in exc.value.detail


@pytest.mark.asyncio
async def test_update_fragment_404_says_token_not_fragment():
    session_factory, _ = _mock_session(existing=None)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)
    data = at.FragmentPatch(description="x")

    with patch.object(at, "async_session", session_factory):
        with pytest.raises(HTTPException) as exc:
            await _handler_fn(at.AgentTemplateController.update_fragment)(controller, 99, data)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Token 99 not found"
    assert "Fragment" not in exc.value.detail


@pytest.mark.asyncio
async def test_update_system_fragment_body_rejected_says_token_not_fragment():
    system_row = _FakeFragmentRow(is_system=True)
    session_factory, _ = _mock_session(existing=system_row)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)
    data = at.FragmentPatch(body=[{"channel": "log", "enabled": True}])

    with patch.object(at, "async_session", session_factory):
        with pytest.raises(HTTPException) as exc:
            await _handler_fn(at.AgentTemplateController.update_fragment)(controller, 7, data)

    assert exc.value.status_code == 400
    assert exc.value.detail.startswith("System tokens can only toggle is_active")
    assert "Fragment" not in exc.value.detail
    assert "fragment" not in exc.value.detail


@pytest.mark.asyncio
async def test_delete_fragment_404_says_token_not_fragment():
    session_factory, _ = _mock_session(existing=None)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)

    with patch.object(at, "async_session", session_factory):
        with pytest.raises(HTTPException) as exc:
            await _handler_fn(at.AgentTemplateController.delete_fragment)(controller, 99)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Token 99 not found"
    assert "Fragment" not in exc.value.detail


@pytest.mark.asyncio
async def test_delete_system_fragment_rejected_says_token_not_fragment():
    system_row = _FakeFragmentRow(is_system=True)
    session_factory, _ = _mock_session(existing=system_row)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)

    with patch.object(at, "async_session", session_factory):
        with pytest.raises(HTTPException) as exc:
            await _handler_fn(at.AgentTemplateController.delete_fragment)(controller, 7)

    assert exc.value.status_code == 400
    assert exc.value.detail == "Cannot delete system tokens. Toggle is_active off instead."
    assert "Fragment" not in exc.value.detail


@pytest.mark.asyncio
async def test_delete_fragment_success_detail_says_token_not_fragment():
    custom_row = _FakeFragmentRow(is_system=False)
    session_factory, _ = _mock_session(existing=custom_row)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)

    with patch.object(at, "async_session", session_factory):
        result = await _handler_fn(at.AgentTemplateController.delete_fragment)(controller, 7)

    assert result == {"detail": "Token 7 deleted"}
    assert "Fragment" not in result["detail"]


@pytest.mark.asyncio
async def test_reload_fragments_detail_says_token_not_fragment():
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)
    result = await _handler_fn(at.AgentTemplateController.reload_fragments)(controller)
    assert result == {"detail": "Token registry reloaded"}
    assert "Fragment" not in result["detail"]


@pytest.mark.asyncio
async def test_fragment_references_404_says_token_not_fragment(monkeypatch):
    async def _none(*a, **k):
        return None
    monkeypatch.setattr(at, "_load_fragment", _none)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)

    with pytest.raises(HTTPException) as exc:
        await _handler_fn(at.AgentTemplateController.fragment_references)(controller, 99)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Token 99 not found"
    assert "Fragment" not in exc.value.detail


def test_validate_body_shape_errors_say_token_not_fragment():
    with pytest.raises(HTTPException) as exc1:
        at._validate_body_shape("notify", {"not": "a list"})
    assert "token body must be a list" in exc1.value.detail
    assert "fragment" not in exc1.value.detail

    with pytest.raises(HTTPException) as exc2:
        at._validate_body_shape("notify", [{"no_channel_key": True}])
    assert "token body[0] missing 'channel'" in exc2.value.detail
    assert "fragment" not in exc2.value.detail

    with pytest.raises(HTTPException) as exc3:
        at._validate_body_shape("condition", ["not", "a", "dict"])
    assert "token body must be a dict" in exc3.value.detail
    assert "fragment" not in exc3.value.detail
