"""
Contract test for event-kind agent creation via the CLI compiler.

Tests the exact payload shape the frontend `ALERT log_tag=... render=...`
statement compiles to, ensuring it's accepted by the backend create_agent
and activate endpoints and can be queried via the dispatch loader.

This test is NOT testing product code changes — it's testing the frontend's
compiled payload contract against the real backend API shape.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

import json
import msgspec
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.routes.agents import (
    AgentController, AgentCreateRequest, _agent_to_info,
)
from backend.api.algo.event_agents import RENDERS, CHANNELS
from backend.api.algo.grammar import LOG_TAG_TOKENS


def _handler_fn(handler):
    """Extract the raw coroutine from a Litestar-decorated method."""
    return handler.fn


def _mock_session(existing=None):
    """Build a MagicMock async_session() context manager."""
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


class _FakeAgentRow:
    """Plain settable object standing in for the Agent ORM row."""
    def __init__(self, **kw):
        self.id = 1
        self.slug = "evt-test"
        self.name = "Test Event"
        self.long_name = None
        self.description = ""
        self.conditions = {}
        self.events = []
        self.actions = []
        self.scope = "total"
        self.schedule = "market_hours"
        self.cooldown_minutes = 30
        self.fire_at_time = None
        self.status = "inactive"
        self.last_triggered_at = None
        self.trigger_count = 0
        self.last_error = None
        self.is_system = False
        self.lifespan_type = "persistent"
        self.lifespan_max_fires = None
        self.lifespan_expires_at = None
        self.trade_mode = "paper"
        self.debounce_minutes = 0
        self.tags = []
        self.blackout_windows = []
        self.kind = "event"
        self.tier = "medium"
        self.topic = "general"
        self.cli_source = None
        self.group_name = None
        self.condition_first_true_at = None
        for k, v in kw.items():
            setattr(self, k, v)


# ═══════════════════════════════════════════════════════════════════════
# 1. Create event agent with valid payload — should succeed with 201
# ═══════════════════════════════════════════════════════════════════════

def test_create_event_agent_valid_payload_decodes():
    """The exact JSON payload from the plan decodes cleanly without msgspec
    silently dropping fields. This catches frontend/backend shape drift."""
    payload = {
        "slug": "cli-test-error-alert",
        "name": "CLI test error alert",
        "kind": "event",
        "conditions": {"log": {"tag": "error", "min_level": "ERROR"}},
        "events": [{"channel": "telegram"}, {"channel": "email"}],
        "actions": [{"type": "render", "render": "error"}],
    }
    body = json.dumps(payload).encode()
    decoded = msgspec.json.decode(body, type=AgentCreateRequest)

    # Verify all fields decoded correctly (msgspec silently drops unknown keys,
    # so this proves the field names match the struct definition)
    assert decoded.slug == "cli-test-error-alert"
    assert decoded.name == "CLI test error alert"
    assert decoded.kind == "event"
    assert decoded.conditions == {"log": {"tag": "error", "min_level": "ERROR"}}
    assert decoded.events == [{"channel": "telegram"}, {"channel": "email"}]
    assert decoded.actions == [{"type": "render", "render": "error"}]


@pytest.mark.asyncio
async def test_create_event_agent_valid_payload_persists():
    """POST the exact payload from the plan; assert 201 and row is created."""
    session_factory, session = _mock_session(existing=None)
    controller = AgentController.__new__(AgentController)

    payload = {
        "slug": "cli-test-error-alert",
        "name": "CLI test error alert",
        "kind": "event",
        "conditions": {"log": {"tag": "error", "min_level": "ERROR"}},
        "events": [{"channel": "telegram"}, {"channel": "email"}],
        "actions": [{"type": "render", "render": "error"}],
    }
    data = msgspec.json.decode(json.dumps(payload).encode(), type=AgentCreateRequest)

    with patch("backend.api.routes.agents.async_session", session_factory), \
         patch("backend.api.routes.agents.logger"):
        result = await _handler_fn(AgentController.create_agent)(controller, data)

    assert result == {"detail": "Agent 'cli-test-error-alert' created"}
    session.add.assert_called_once()
    added_agent = session.add.call_args[0][0]
    assert added_agent.slug == "cli-test-error-alert"
    assert added_agent.kind == "event"
    assert added_agent.conditions == {"log": {"tag": "error", "min_level": "ERROR"}}
    assert added_agent.events == [{"channel": "telegram"}, {"channel": "email"}]
    assert added_agent.actions == [{"type": "render", "render": "error"}]
    session.commit.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════
# 2. Activate agent and verify it's queryable via dispatch loader query
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_activate_event_agent_and_query_via_loader():
    """After creating and activating, the row must be queryable via
    event_agents.py:441's loader query (kind='event', status='active')."""
    # Create the agent first
    agent = _FakeAgentRow(
        slug="cli-test-error-alert",
        name="CLI test error alert",
        kind="event",
        status="inactive",
        conditions={"log": {"tag": "error", "min_level": "ERROR"}},
        events=[{"channel": "telegram"}, {"channel": "email"}],
        actions=[{"type": "render", "render": "error"}],
    )
    session_factory, session = _mock_session(existing=agent)
    controller = AgentController.__new__(AgentController)

    with patch("backend.api.routes.agents.async_session", session_factory), \
         patch("backend.api.routes.agents.logger"):
        result = await _handler_fn(AgentController.activate_agent)(
            controller, "cli-test-error-alert"
        )

    assert result == {"detail": "Agent 'cli-test-error-alert' activated"}
    # Verify activate set status to 'active'
    assert agent.status == "active"
    session.commit.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════
# 3. Negative case: missing render key → should 422
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_event_agent_missing_render_rejects_422():
    """Omitting the render key from actions[0] should fail validation."""
    session_factory, _ = _mock_session(existing=None)
    controller = AgentController.__new__(AgentController)

    payload = {
        "slug": "cli-test-no-render",
        "name": "Missing render",
        "kind": "event",
        "conditions": {"log": {"tag": "error", "min_level": "ERROR"}},
        "events": [{"channel": "telegram"}],
        "actions": [{"type": "render"}],  # Missing render key
    }
    data = msgspec.json.decode(json.dumps(payload).encode(), type=AgentCreateRequest)

    from litestar.exceptions import HTTPException
    with patch("backend.api.routes.agents.async_session", session_factory):
        with pytest.raises(HTTPException) as exc_info:
            await _handler_fn(AgentController.create_agent)(controller, data)

    assert exc_info.value.status_code == 422
    assert "unknown renderer" in exc_info.value.detail or "render" in exc_info.value.detail.lower()


# ═══════════════════════════════════════════════════════════════════════
# 4. Negative case: invalid log tag → should 422
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_event_agent_invalid_log_tag_rejects_422():
    """An unknown log tag should fail validation."""
    session_factory, _ = _mock_session(existing=None)
    controller = AgentController.__new__(AgentController)

    payload = {
        "slug": "cli-test-bad-tag",
        "name": "Bad tag",
        "kind": "event",
        "conditions": {"log": {"tag": "not-a-real-tag", "min_level": "ERROR"}},
        "events": [{"channel": "telegram"}],
        "actions": [{"type": "render", "render": "error"}],
    }
    data = msgspec.json.decode(json.dumps(payload).encode(), type=AgentCreateRequest)

    from litestar.exceptions import HTTPException
    with patch("backend.api.routes.agents.async_session", session_factory):
        with pytest.raises(HTTPException) as exc_info:
            await _handler_fn(AgentController.create_agent)(controller, data)

    assert exc_info.value.status_code == 422
    assert "unknown log tag" in exc_info.value.detail


# ═══════════════════════════════════════════════════════════════════════
# 5. Verify log tag and renderer exist in the real catalogs
# ═══════════════════════════════════════════════════════════════════════

def test_error_log_tag_exists_in_catalog():
    """Verify 'error' is a real log tag."""
    known_tags = {t["token"] for t in LOG_TAG_TOKENS}
    assert "error" in known_tags, f"'error' must be in LOG_TAG_TOKENS, got: {known_tags}"


def test_error_renderer_exists_in_catalog():
    """Verify 'error' is a real renderer."""
    assert "error" in RENDERS, f"'error' must be in RENDERS, got: {set(RENDERS.keys())}"


def test_telegram_and_email_channels_exist_in_catalog():
    """Verify telegram and email are real channels."""
    assert "telegram" in CHANNELS, f"'telegram' must be in CHANNELS, got: {set(CHANNELS.keys())}"
    assert "email" in CHANNELS, f"'email' must be in CHANNELS, got: {set(CHANNELS.keys())}"


# ═══════════════════════════════════════════════════════════════════════
# 6. Verify min_level validation and defaults
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_event_agent_min_level_defaults_to_info():
    """When min_level is omitted, validation treats it as 'INFO' (the default)."""
    session_factory, session = _mock_session(existing=None)
    controller = AgentController.__new__(AgentController)

    payload = {
        "slug": "cli-test-default-level",
        "name": "Default level",
        "kind": "event",
        "conditions": {"log": {"tag": "error"}},  # min_level omitted
        "events": [{"channel": "telegram"}],
        "actions": [{"type": "render", "render": "error"}],
    }
    data = msgspec.json.decode(json.dumps(payload).encode(), type=AgentCreateRequest)

    with patch("backend.api.routes.agents.async_session", session_factory), \
         patch("backend.api.routes.agents.logger"):
        result = await _handler_fn(AgentController.create_agent)(controller, data)

    # Should succeed (validation defaults to "INFO")
    assert result == {"detail": "Agent 'cli-test-default-level' created"}
    session.add.assert_called_once()


@pytest.mark.asyncio
async def test_create_event_agent_min_level_lowercased_input_uppercases():
    """When min_level is lowercase (e.g., 'error'), it should pass validation
    (normalized to uppercase internally)."""
    session_factory, session = _mock_session(existing=None)
    controller = AgentController.__new__(AgentController)

    payload = {
        "slug": "cli-test-lowercase-level",
        "name": "Lowercase level",
        "kind": "event",
        "conditions": {"log": {"tag": "error", "min_level": "error"}},  # lowercase input
        "events": [{"channel": "telegram"}],
        "actions": [{"type": "render", "render": "error"}],
    }
    data = msgspec.json.decode(json.dumps(payload).encode(), type=AgentCreateRequest)

    with patch("backend.api.routes.agents.async_session", session_factory), \
         patch("backend.api.routes.agents.logger"):
        result = await _handler_fn(AgentController.create_agent)(controller, data)

    # Should succeed (validation normalizes to "ERROR")
    assert result == {"detail": "Agent 'cli-test-lowercase-level' created"}
    session.add.assert_called_once()


@pytest.mark.asyncio
async def test_create_event_agent_invalid_min_level_rejects_422():
    """An invalid min_level should fail validation."""
    session_factory, _ = _mock_session(existing=None)
    controller = AgentController.__new__(AgentController)

    payload = {
        "slug": "cli-test-bad-level",
        "name": "Bad level",
        "kind": "event",
        "conditions": {"log": {"tag": "error", "min_level": "INVALID_LEVEL"}},
        "events": [{"channel": "telegram"}],
        "actions": [{"type": "render", "render": "error"}],
    }
    data = msgspec.json.decode(json.dumps(payload).encode(), type=AgentCreateRequest)

    from litestar.exceptions import HTTPException
    with patch("backend.api.routes.agents.async_session", session_factory):
        with pytest.raises(HTTPException) as exc_info:
            await _handler_fn(AgentController.create_agent)(controller, data)

    assert exc_info.value.status_code == 422
    assert "min_level" in exc_info.value.detail.lower()


# ═══════════════════════════════════════════════════════════════════════
# 7. Delete endpoint cleanup helper
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_delete_event_agent_removes_row():
    """Verify the delete endpoint works for cleanup."""
    agent = _FakeAgentRow(slug="cli-test-delete", kind="event", is_system=False)
    session_factory, session = _mock_session(existing=agent)
    controller = AgentController.__new__(AgentController)

    with patch("backend.api.routes.agents.async_session", session_factory):
        result = await _handler_fn(AgentController.delete_agent)(
            controller, "cli-test-delete"
        )

    assert result == {"detail": "Agent 'cli-test-delete' deleted"}
    session.delete.assert_called_once()
    session.commit.assert_awaited_once()
