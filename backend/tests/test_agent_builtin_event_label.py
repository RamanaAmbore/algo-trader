"""Automation page "Built-in" label source (2026-10).

`AgentInfo.is_builtin_event` is a read-only, computed-at-serialization field
— True only for one of the 23 code-seeded `kind='event'` agents in
`event_agents.SEEDED_AGENTS`. Deliberately NOT `Agent.is_system`: that flag
is set only for `kind='cycle'` BUILTIN_AGENTS / bracket / template rows —
confirmed live on dev, every one of the 23 seeded event-kind rows carries
`is_system=False` (the model default), since `seed_event_agents()`'s own
insert path never passes `is_system`. See `_is_builtin_seeded_event_agent`'s
docstring in `backend/api/routes/agents.py` for the full rationale.

Also covers the delete-gap follow-up (same session): `delete_agent()` reuses
`_is_builtin_seeded_event_agent()` directly (no duplicated slug-set check)
to refuse deleting any of the 23 seeded event agents with the SAME 403
response shape the existing `is_system` refusal already uses — these rows
would otherwise silently reappear on next process restart via
`seed_event_agents()`'s insert path, with the alert dark in the meantime.
A genuinely operator-created, non-seeded `kind='event'` agent must still be
deletable — regression guard against accidentally blocking ALL event-agent
deletes.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from litestar.exceptions import HTTPException

from backend.api.algo.event_agents import SEEDED_AGENTS
from backend.api.routes.agents import (
    AgentController, _agent_to_info, _is_builtin_seeded_event_agent,
)


# Same test-double convention as test_alert_routing_tree_activation.py's
# own `_Row` / `_make_row` — covers every attribute `_agent_to_info` reads
# directly (the rest are getattr-guarded with safe defaults).
class _Row:
    id = 1
    slug = "x"
    name = "x"
    long_name = None
    description = None
    conditions: dict = {}
    events: list = []
    actions: list = []
    scope = "total"
    schedule = None
    cooldown_minutes = 30
    fire_at_time = None
    status = "active"
    last_triggered_at = None
    trigger_count = 0
    last_error = None
    is_system = False
    given = None
    kind = "cycle"
    priority = None
    lifespan_type = "persistent"
    trade_mode = "paper"


def _make_row(**overrides) -> SimpleNamespace:
    base = {k: v for k, v in vars(_Row).items() if not k.startswith("__")}
    base.update(overrides)
    return SimpleNamespace(**base)


_SEEDED_SLUGS = [spec["slug"] for spec in SEEDED_AGENTS]


class TestIsBuiltinSeededEventAgent:
    def test_exactly_23_seeded_slugs(self):
        # Pins the count the operator explicitly asked about — a future
        # addition/removal from SEEDED_AGENTS should be a deliberate,
        # visible change to this test, not a silent drift.
        assert len(_SEEDED_SLUGS) == 23
        assert len(set(_SEEDED_SLUGS)) == 23  # no duplicate slugs

    @pytest.mark.parametrize("slug", _SEEDED_SLUGS)
    def test_true_for_every_seeded_event_agent_slug(self, slug):
        row = _make_row(kind="event", slug=slug)
        assert _is_builtin_seeded_event_agent(row) is True

    def test_false_for_non_seeded_event_agent(self):
        row = _make_row(kind="event", slug="operator-authored-alert")
        assert _is_builtin_seeded_event_agent(row) is False

    def test_false_for_cycle_agent_even_with_a_seeded_slug_string(self):
        # Defensive: the kind gate must win even if a slug string happened
        # to collide across kinds (not possible today — slugs are globally
        # unique — but the gate must not rely on that).
        row = _make_row(kind="cycle", slug=_SEEDED_SLUGS[0])
        assert _is_builtin_seeded_event_agent(row) is False

    def test_false_for_bracket_and_template_kinds(self):
        for kind in ("bracket", "template"):
            row = _make_row(kind=kind, slug=_SEEDED_SLUGS[0])
            assert _is_builtin_seeded_event_agent(row) is False


class TestAgentInfoIsBuiltinEventField:
    def test_agent_to_info_true_for_seeded_event_slug(self):
        row = _make_row(kind="event", slug=_SEEDED_SLUGS[0], events=[])
        info = _agent_to_info(row)
        assert info.is_builtin_event is True

    def test_agent_to_info_false_for_operator_authored_event_agent(self):
        row = _make_row(kind="event", slug="my-custom-alert", events=[])
        info = _agent_to_info(row)
        assert info.is_builtin_event is False

    def test_agent_to_info_false_for_cycle_builtin(self):
        # A kind='cycle' BUILTIN_AGENTS row (is_system=True in practice)
        # must NOT read as is_builtin_event — that flag is scoped to the
        # kind='event' seeded set only, a disjoint signal from is_system.
        row = _make_row(kind="cycle", slug="loss-funds-negative", is_system=True)
        info = _agent_to_info(row)
        assert info.is_builtin_event is False
        assert info.is_system is True

    def test_is_builtin_event_never_true_when_is_system_is_false_mismatch_case(self):
        # The exact case this field exists to fix: a real seeded event
        # agent with is_system=False (its true DB state) still reports
        # is_builtin_event=True.
        row = _make_row(kind="event", slug=_SEEDED_SLUGS[-1], events=[], is_system=False)
        info = _agent_to_info(row)
        assert info.is_system is False
        assert info.is_builtin_event is True


# ═══════════════════════════════════════════════════════════════════════
#  delete_agent() — reuses _is_builtin_seeded_event_agent() directly (no
#  duplicated slug-set check) to refuse deleting any of the 23 seeded
#  event agents with the SAME 403 response shape the existing is_system
#  refusal already uses.
# ═══════════════════════════════════════════════════════════════════════

def _handler_fn(handler):
    """Extract the raw coroutine from a Litestar-decorated method —
    same helper as test_event_agent_cli_contract.py."""
    return handler.fn


def _mock_session(existing=None):
    """Same MagicMock async_session() builder as
    test_event_agent_cli_contract.py's own helper."""
    session = MagicMock()
    session.commit = AsyncMock()
    session.delete = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=existing)
    session.execute = AsyncMock(return_value=result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), session


class _FakeAgentRow:
    """Plain settable object standing in for the Agent ORM row — same
    shape as test_event_agent_cli_contract.py's own _FakeAgentRow."""
    def __init__(self, **kw):
        self.id = 1
        self.slug = "evt-test"
        self.name = "Test Event"
        self.kind = "event"
        self.is_system = False
        for k, v in kw.items():
            setattr(self, k, v)


class TestDeleteAgentBlocksSeededEventAgents:
    # A sample, not all 23 — the membership check itself is already
    # parametrized over every slug in TestIsBuiltinSeededEventAgent above;
    # this class only needs to prove delete_agent() actually consults it.
    @pytest.mark.asyncio
    @pytest.mark.parametrize("slug", [_SEEDED_SLUGS[0], _SEEDED_SLUGS[5], _SEEDED_SLUGS[-1]])
    async def test_delete_refused_for_seeded_event_agent(self, slug):
        agent = _FakeAgentRow(slug=slug, kind="event", is_system=False)
        session_factory, session = _mock_session(existing=agent)
        controller = AgentController.__new__(AgentController)

        with patch("backend.api.routes.agents.async_session", session_factory):
            with pytest.raises(HTTPException) as exc_info:
                await _handler_fn(AgentController.delete_agent)(controller, slug)

        # Same response shape as the existing is_system refusal — not a
        # new error string.
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Cannot delete system agent"
        session.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_still_succeeds_for_operator_created_event_agent(self):
        # Regression guard: the guard must not become "no event agent is
        # ever deletable" — only the 23 seeded slugs are blocked.
        agent = _FakeAgentRow(slug="my-operator-alert", kind="event", is_system=False)
        session_factory, session = _mock_session(existing=agent)
        controller = AgentController.__new__(AgentController)

        with patch("backend.api.routes.agents.async_session", session_factory):
            result = await _handler_fn(AgentController.delete_agent)(controller, "my-operator-alert")

        assert result == {"detail": "Agent 'my-operator-alert' deleted"}
        session.delete.assert_called_once()
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_still_refused_for_is_system_row_unrelated_to_seeded_set(self):
        # Pre-existing behavior preserved: an is_system=True cycle/bracket/
        # template row is still refused, independent of the new check.
        agent = _FakeAgentRow(slug="loss-funds-negative", kind="cycle", is_system=True)
        session_factory, session = _mock_session(existing=agent)
        controller = AgentController.__new__(AgentController)

        with patch("backend.api.routes.agents.async_session", session_factory):
            with pytest.raises(HTTPException) as exc_info:
                await _handler_fn(AgentController.delete_agent)(controller, "loss-funds-negative")

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Cannot delete system agent"
        session.delete.assert_not_called()
