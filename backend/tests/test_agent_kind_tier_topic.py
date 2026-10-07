"""
Tests for backend/api/routes/agents.py — Phase: tier/topic save bug fix +
kind='event' create/update path.

Covers:
  1. REGRESSION (critical) — AgentCreateRequest / AgentUpdateRequest msgspec
     decode no longer silently drops tier/topic (the exact bug class: msgspec
     ignores unknown struct fields on decode, so a PATCH with tier/topic in
     the body previously returned 200 "saved" but never persisted).
  2. update_agent's per-field copy loop actually setattrs tier/topic onto
     the row.
  3. _agent_to_info surfaces kind/tier/topic on every read.
  4. kind normalisation: 'threshold' is accepted as a synonym for 'cycle'
     and NORMALISED to 'cycle' before it ever reaches the DB — storing the
     literal string 'threshold' would make agent_engine.run_cycle()'s
     `Agent.kind == "cycle"` query silently skip the agent forever.
  5. create_agent validates kind='event' agents via
     event_agents.validate_seed_spec() — unknown log tag / unknown
     min_level / unknown renderer / unknown channel each rejected 422.
  6. update_agent rejects any attempt to change kind after creation.
  7. update_agent re-validates the MERGED spec for an existing event
     agent (a partial update that only touches `actions` must still be
     checked against the row's existing conditions/events).
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

import json

import msgspec
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.routes.agents import (
    AgentController, AgentCreateRequest, AgentUpdateRequest, _agent_to_info,
)


def _handler_fn(handler):
    """Extract the raw coroutine from a Litestar-decorated method — same
    helper used in test_agents_interpret_auth_fix.py."""
    return handler.fn


def _mock_session(existing=None):
    """Build a MagicMock async_session() context manager whose
    execute().scalar_one_or_none() returns `existing` (None = no row)."""
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


class _FakeAgentRow:
    """Plain settable object standing in for the Agent ORM row — real
    attribute assignment (unlike a strict MagicMock spec), so the
    update_agent copy-loop's setattr() calls behave exactly as they
    would against the real model."""
    def __init__(self, **kw):
        self.id = 1
        self.slug = "t-agent"
        self.name = "Test"
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
        self.kind = "cycle"
        self.tier = "medium"
        self.topic = "general"
        self.condition_first_true_at = None
        for k, v in kw.items():
            setattr(self, k, v)


# ═══════════════════════════════════════════════════════════════════════
# 1. REGRESSION — msgspec no longer silently drops tier/topic/kind
# ═══════════════════════════════════════════════════════════════════════

def test_agent_update_request_decodes_tier_and_topic():
    """Pre-fix, AgentUpdateRequest had no tier/topic fields — msgspec's
    JSON decoder silently ignores unknown keys, so decoding this exact
    payload would have produced a struct with NO tier/topic attributes
    at all (AttributeError on access). Post-fix, both decode cleanly."""
    body = json.dumps({"tier": "critical", "topic": "risk-breach"}).encode()
    decoded = msgspec.json.decode(body, type=AgentUpdateRequest)
    assert decoded.tier == "critical"
    assert decoded.topic == "risk-breach"


def test_agent_create_request_decodes_tier_topic_and_kind():
    body = json.dumps({
        "slug": "x", "name": "X", "conditions": {}, "events": [],
        "tier": "high", "topic": "pnl", "kind": "event",
    }).encode()
    decoded = msgspec.json.decode(body, type=AgentCreateRequest)
    assert decoded.tier == "high"
    assert decoded.topic == "pnl"
    assert decoded.kind == "event"


def test_agent_update_request_tier_topic_default_to_none():
    """None = 'leave column unchanged' — matches every other optional
    field's convention on this struct."""
    decoded = msgspec.json.decode(b"{}", type=AgentUpdateRequest)
    assert decoded.tier is None
    assert decoded.topic is None
    assert decoded.kind is None


# ═══════════════════════════════════════════════════════════════════════
# 2. update_agent copy loop actually persists tier/topic
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_update_agent_copies_tier_and_topic_onto_row():
    agent = _FakeAgentRow(tier="medium", topic="general")
    session_factory, session = _mock_session(existing=agent)

    controller = AgentController.__new__(AgentController)
    data = AgentUpdateRequest(tier="critical", topic="loss-breach")

    with patch("backend.api.routes.agents.async_session", session_factory):
        result = await _handler_fn(AgentController.update_agent)(controller, "t-agent", data)

    assert result == {"detail": "Agent 't-agent' updated"}
    assert agent.tier == "critical", "tier update must persist onto the row"
    assert agent.topic == "loss-breach", "topic update must persist onto the row"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_agent_leaves_tier_topic_unchanged_when_omitted():
    agent = _FakeAgentRow(tier="medium", topic="general")
    session_factory, _ = _mock_session(existing=agent)
    controller = AgentController.__new__(AgentController)
    data = AgentUpdateRequest(name="renamed only")

    with patch("backend.api.routes.agents.async_session", session_factory):
        await _handler_fn(AgentController.update_agent)(controller, "t-agent", data)

    assert agent.tier == "medium"
    assert agent.topic == "general"
    assert agent.name == "renamed only"


# ═══════════════════════════════════════════════════════════════════════
# 3. _agent_to_info surfaces kind/tier/topic
# ═══════════════════════════════════════════════════════════════════════

def test_agent_to_info_surfaces_kind_tier_topic():
    agent = _FakeAgentRow(kind="event", tier="critical", topic="loss")
    info = _agent_to_info(agent)
    assert info.kind == "event"
    assert info.tier == "critical"
    assert info.topic == "loss"


def test_agent_to_info_defaults_when_missing_attrs():
    """A legacy row created before kind/tier/topic existed — getattr
    defaults must never crash and must match the model's own column
    defaults."""
    class _Bare:
        pass
    bare = _Bare()
    for field, val in [
        ("id", 1), ("slug", "s"), ("name", "n"), ("long_name", None),
        ("description", None), ("conditions", {}), ("events", []),
        ("actions", []), ("scope", "total"), ("schedule", "market_hours"),
        ("cooldown_minutes", 30), ("fire_at_time", None), ("status", "inactive"),
        ("last_triggered_at", None), ("trigger_count", 0), ("last_error", None),
        ("is_system", False),
    ]:
        setattr(bare, field, val)
    info = _agent_to_info(bare)
    assert info.kind == "cycle"
    assert info.tier == "medium"
    assert info.topic == "general"


# ═══════════════════════════════════════════════════════════════════════
# 4. kind normalisation — 'threshold' synonym never reaches the DB literally
# ═══════════════════════════════════════════════════════════════════════

def test_normalize_kind_threshold_maps_to_cycle():
    from backend.api.routes.agents import _age_normalize_kind
    assert _age_normalize_kind("threshold") == "cycle"
    assert _age_normalize_kind("cycle") == "cycle"
    assert _age_normalize_kind("event") == "event"
    assert _age_normalize_kind(None) == "cycle"


def test_normalize_kind_rejects_unknown():
    from backend.api.routes.agents import _age_normalize_kind
    from litestar.exceptions import HTTPException
    with pytest.raises(HTTPException):
        _age_normalize_kind("bogus")


@pytest.mark.asyncio
async def test_create_agent_threshold_kind_stores_cycle_literally():
    """The critical invariant: a client sending kind='threshold' must
    result in Agent(kind='cycle') being constructed — NOT kind='threshold',
    which run_cycle()'s query would silently never select."""
    session_factory, session = _mock_session(existing=None)
    controller = AgentController.__new__(AgentController)
    data = AgentCreateRequest(
        slug="thr-1", name="Threshold agent", conditions={}, events=[],
        kind="threshold",
    )

    with patch("backend.api.routes.agents.async_session", session_factory):
        await _handler_fn(AgentController.create_agent)(controller, data)

    session.add.assert_called_once()
    added_agent = session.add.call_args[0][0]
    assert added_agent.kind == "cycle", (
        "kind='threshold' must be normalised to the DB column's real "
        "vocabulary ('cycle') — storing 'threshold' literally would make "
        "agent_engine.run_cycle()'s `Agent.kind == 'cycle'` query silently "
        "skip this agent forever."
    )


# ═══════════════════════════════════════════════════════════════════════
# 5. create_agent validates kind='event' via validate_seed_spec()
# ═══════════════════════════════════════════════════════════════════════

_VALID_EVENT_CREATE_KW = dict(
    slug="evt-1", name="Evt", conditions={"log": {"tag": "orders", "min_level": "INFO"}},
    events=[{"channel": "telegram", "enabled": True}],
    actions=[{"type": "render", "render": "fill"}],
    kind="event",
)


@pytest.mark.asyncio
async def test_create_agent_event_kind_valid_spec_persists():
    session_factory, session = _mock_session(existing=None)
    controller = AgentController.__new__(AgentController)
    data = AgentCreateRequest(**_VALID_EVENT_CREATE_KW)

    with patch("backend.api.routes.agents.async_session", session_factory):
        result = await _handler_fn(AgentController.create_agent)(controller, data)

    assert result == {"detail": "Agent 'evt-1' created"}
    session.add.assert_called_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_agent_event_kind_rejects_unknown_log_tag():
    session_factory, _ = _mock_session(existing=None)
    controller = AgentController.__new__(AgentController)
    kw = dict(_VALID_EVENT_CREATE_KW)
    kw["conditions"] = {"log": {"tag": "not-a-real-tag", "min_level": "INFO"}}
    data = AgentCreateRequest(**kw)

    from litestar.exceptions import HTTPException
    with patch("backend.api.routes.agents.async_session", session_factory):
        with pytest.raises(HTTPException) as exc_info:
            await _handler_fn(AgentController.create_agent)(controller, data)

    assert exc_info.value.status_code == 422
    assert "unknown log tag" in exc_info.value.detail


@pytest.mark.asyncio
async def test_create_agent_event_kind_rejects_unknown_renderer():
    session_factory, _ = _mock_session(existing=None)
    controller = AgentController.__new__(AgentController)
    kw = dict(_VALID_EVENT_CREATE_KW)
    kw["actions"] = [{"type": "render", "render": "totally-made-up-renderer"}]
    data = AgentCreateRequest(**kw)

    from litestar.exceptions import HTTPException
    with patch("backend.api.routes.agents.async_session", session_factory):
        with pytest.raises(HTTPException) as exc_info:
            await _handler_fn(AgentController.create_agent)(controller, data)

    assert exc_info.value.status_code == 422
    assert "unknown renderer" in exc_info.value.detail


@pytest.mark.asyncio
async def test_create_agent_event_kind_rejects_unknown_channel():
    session_factory, _ = _mock_session(existing=None)
    controller = AgentController.__new__(AgentController)
    kw = dict(_VALID_EVENT_CREATE_KW)
    kw["events"] = [{"channel": "carrier-pigeon", "enabled": True}]
    data = AgentCreateRequest(**kw)

    from litestar.exceptions import HTTPException
    with patch("backend.api.routes.agents.async_session", session_factory):
        with pytest.raises(HTTPException) as exc_info:
            await _handler_fn(AgentController.create_agent)(controller, data)

    assert exc_info.value.status_code == 422
    assert "unknown channel" in exc_info.value.detail


# ═══════════════════════════════════════════════════════════════════════
# 6 & 7. update_agent: kind is immutable; merged-spec re-validation
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_update_agent_rejects_kind_change():
    agent = _FakeAgentRow(kind="cycle")
    session_factory, _ = _mock_session(existing=agent)
    controller = AgentController.__new__(AgentController)
    data = AgentUpdateRequest(kind="event")

    from litestar.exceptions import HTTPException
    with patch("backend.api.routes.agents.async_session", session_factory):
        with pytest.raises(HTTPException) as exc_info:
            await _handler_fn(AgentController.update_agent)(controller, "t-agent", data)

    assert exc_info.value.status_code == 400
    assert "kind cannot be changed" in exc_info.value.detail


@pytest.mark.asyncio
async def test_update_agent_kind_resubmitted_unchanged_is_allowed():
    """Sending the SAME kind back (common for a client that always
    round-trips its full form state) must not be treated as a change."""
    agent = _FakeAgentRow(kind="event",
                           conditions=_VALID_EVENT_CREATE_KW["conditions"],
                           events=_VALID_EVENT_CREATE_KW["events"],
                           actions=_VALID_EVENT_CREATE_KW["actions"])
    session_factory, session = _mock_session(existing=agent)
    controller = AgentController.__new__(AgentController)
    data = AgentUpdateRequest(kind="event", name="renamed")

    with patch("backend.api.routes.agents.async_session", session_factory):
        result = await _handler_fn(AgentController.update_agent)(controller, "t-agent", data)

    assert result == {"detail": "Agent 't-agent' updated"}
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_agent_event_kind_validates_merged_spec_rejects_bad_actions():
    """A partial update that only supplies `actions` must still be
    checked against the row's EXISTING (valid) conditions/events — the
    merged spec, not just the delta."""
    agent = _FakeAgentRow(
        kind="event",
        conditions=_VALID_EVENT_CREATE_KW["conditions"],
        events=_VALID_EVENT_CREATE_KW["events"],
        actions=_VALID_EVENT_CREATE_KW["actions"],
    )
    session_factory, session = _mock_session(existing=agent)
    controller = AgentController.__new__(AgentController)
    data = AgentUpdateRequest(actions=[{"type": "render", "render": "nonexistent"}])

    from litestar.exceptions import HTTPException
    with patch("backend.api.routes.agents.async_session", session_factory):
        with pytest.raises(HTTPException) as exc_info:
            await _handler_fn(AgentController.update_agent)(controller, "t-agent", data)

    assert exc_info.value.status_code == 422
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_agent_event_kind_accepts_valid_merged_spec():
    agent = _FakeAgentRow(
        kind="event",
        conditions=_VALID_EVENT_CREATE_KW["conditions"],
        events=_VALID_EVENT_CREATE_KW["events"],
        actions=_VALID_EVENT_CREATE_KW["actions"],
    )
    session_factory, session = _mock_session(existing=agent)
    controller = AgentController.__new__(AgentController)
    data = AgentUpdateRequest(name="renamed event agent")

    with patch("backend.api.routes.agents.async_session", session_factory):
        result = await _handler_fn(AgentController.update_agent)(controller, "t-agent", data)

    assert result == {"detail": "Agent 't-agent' updated"}
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_agent_cycle_kind_is_not_spec_validated():
    """Ordinary threshold ('cycle') agents must keep their existing
    (lack of) save-time validation path — this fix only adds a
    PARALLEL branch for kind='event', it must not touch the cycle path."""
    agent = _FakeAgentRow(kind="cycle", conditions={"anything": "goes"}, events=[], actions=[])
    session_factory, session = _mock_session(existing=agent)
    controller = AgentController.__new__(AgentController)
    # This would fail validate_seed_spec (missing slug key etc) if it were
    # ever run against a cycle agent — it must not be.
    data = AgentUpdateRequest(conditions={"still": "anything goes"})

    with patch("backend.api.routes.agents.async_session", session_factory):
        result = await _handler_fn(AgentController.update_agent)(controller, "t-agent", data)

    assert result == {"detail": "Agent 't-agent' updated"}
    session.commit.assert_awaited_once()
