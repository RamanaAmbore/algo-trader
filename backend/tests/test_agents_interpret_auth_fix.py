"""Tests for the 2026-09-27 audit fixes to AgentController.interpret:

Fix 1 — /interpret's mutating sub-commands (config/activate/deactivate/
fire/ai) previously ran with only the class-level `view_agents_catalog`
guard, which demo/anonymous sessions hold — every other mutating route
on this controller requires `manage_own_agents`. Fixed via an imperative
`require_capability()` check gated on the parsed action.

Fix 2 — `agent activate`/`agent deactivate` via /interpret always threw
TypeError: `await self.activate_agent(slug)` awaits the Litestar
route-handler descriptor, not the underlying coroutine. Fixed by
extracting the shared body into `_age_set_status()`, called directly by
both the REST route handlers and the interpret dispatch.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.routes.agents import AgentController, InterpretRequest


def _handler_fn(handler):
    """Extract the raw coroutine from a Litestar-decorated method (the
    class attribute is a route-handler descriptor, not the plain
    function) — same pattern as test_postback_auth.py's _handler_fn."""
    return handler.fn


class _FakeAgent:
    def __init__(self, slug="demo-agent", status="inactive"):
        self.slug = slug
        self.status = status


def _mock_session_with_agent(agent):
    """Build a MagicMock async_session() context manager whose
    execute().scalar_one_or_none() returns `agent`."""
    session = MagicMock()
    session.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=agent)
    session.execute = AsyncMock(return_value=result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), session


class TestInterpretGatesCapabilityByAction:
    """The core auth-bypass fix: mutating actions must call
    require_capability("manage_own_agents"); read-only ones must not."""

    @pytest.mark.asyncio
    async def test_config_action_requires_manage_own_agents(self):
        controller = AgentController.__new__(AgentController)
        data = InterpretRequest(command="agent config foo threshold=5")
        fake_request = MagicMock()

        with patch("backend.api.routes.agents.require_capability", new=AsyncMock()) as _rc, \
             patch.object(controller, "_age_interpret_dispatch", new=AsyncMock(
                 return_value=None)):
            await _handler_fn(AgentController.interpret)(controller, data, fake_request)

        _rc.assert_awaited_once_with(fake_request, "manage_own_agents")

    @pytest.mark.asyncio
    async def test_activate_action_requires_manage_own_agents(self):
        controller = AgentController.__new__(AgentController)
        data = InterpretRequest(command="agent activate foo")
        fake_request = MagicMock()

        with patch("backend.api.routes.agents.require_capability", new=AsyncMock()) as _rc, \
             patch.object(controller, "_age_interpret_dispatch", new=AsyncMock(
                 return_value=None)):
            await _handler_fn(AgentController.interpret)(controller, data, fake_request)

        _rc.assert_awaited_once_with(fake_request, "manage_own_agents")

    @pytest.mark.asyncio
    async def test_fire_action_requires_manage_own_agents(self):
        controller = AgentController.__new__(AgentController)
        data = InterpretRequest(command="agent fire foo")
        fake_request = MagicMock()

        with patch("backend.api.routes.agents.require_capability", new=AsyncMock()) as _rc, \
             patch.object(controller, "_age_interpret_dispatch", new=AsyncMock(
                 return_value=None)):
            await _handler_fn(AgentController.interpret)(controller, data, fake_request)

        _rc.assert_awaited_once_with(fake_request, "manage_own_agents")

    @pytest.mark.asyncio
    async def test_ai_action_requires_manage_own_agents(self):
        """Covers both `agent ai create` and `agent ai refine` — both
        dispatch through the single "ai" top-level action."""
        controller = AgentController.__new__(AgentController)
        data = InterpretRequest(command='agent ai create "some prompt"')
        fake_request = MagicMock()

        with patch("backend.api.routes.agents.require_capability", new=AsyncMock()) as _rc, \
             patch.object(controller, "_age_interpret_dispatch", new=AsyncMock(
                 return_value=None)):
            await _handler_fn(AgentController.interpret)(controller, data, fake_request)

        _rc.assert_awaited_once_with(fake_request, "manage_own_agents")

    @pytest.mark.asyncio
    async def test_list_action_does_not_require_capability(self):
        """Read-only commands stay available under the class-level
        view_agents_catalog guard (demo-eligible) — no extra check."""
        controller = AgentController.__new__(AgentController)
        data = InterpretRequest(command="agent list")
        fake_request = MagicMock()

        with patch("backend.api.routes.agents.require_capability", new=AsyncMock()) as _rc, \
             patch.object(controller, "_age_interpret_dispatch", new=AsyncMock(
                 return_value=None)):
            await _handler_fn(AgentController.interpret)(controller, data, fake_request)

        _rc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_status_action_does_not_require_capability(self):
        controller = AgentController.__new__(AgentController)
        data = InterpretRequest(command="agent status foo")
        fake_request = MagicMock()

        with patch("backend.api.routes.agents.require_capability", new=AsyncMock()) as _rc, \
             patch.object(controller, "_age_interpret_dispatch", new=AsyncMock(
                 return_value=None)):
            await _handler_fn(AgentController.interpret)(controller, data, fake_request)

        _rc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_help_action_does_not_require_capability(self):
        controller = AgentController.__new__(AgentController)
        data = InterpretRequest(command="agent help")
        fake_request = MagicMock()

        with patch("backend.api.routes.agents.require_capability", new=AsyncMock()) as _rc, \
             patch.object(controller, "_age_interpret_dispatch", new=AsyncMock(
                 return_value=None)):
            await _handler_fn(AgentController.interpret)(controller, data, fake_request)

        _rc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_demo_session_rejected_before_any_mutation_runs(self):
        """End-to-end: a demo/anonymous request attempting a mutating
        command must be rejected by require_capability's real logic
        (PermissionDeniedException) BEFORE _age_interpret_dispatch ever
        runs — proving the gate actually blocks the dispatch, not just
        that it's called."""
        from litestar.exceptions import PermissionDeniedException, NotAuthorizedException

        controller = AgentController.__new__(AgentController)
        data = InterpretRequest(command="agent config foo threshold=5")
        fake_request = MagicMock()
        fake_request.headers = {}  # no Authorization header — anonymous

        dispatch_spy = AsyncMock(return_value=None)
        with patch.object(controller, "_age_interpret_dispatch", new=dispatch_spy):
            with pytest.raises((PermissionDeniedException, NotAuthorizedException)):
                await _handler_fn(AgentController.interpret)(controller, data, fake_request)

        dispatch_spy.assert_not_awaited()


class TestActivateDeactivateNoLongerThrows:
    """Fix 2: activate/deactivate via /interpret must not raise TypeError
    from awaiting the decorated route-handler descriptor."""

    @pytest.mark.asyncio
    async def test_age_set_status_activates(self):
        controller = AgentController.__new__(AgentController)
        agent = _FakeAgent(slug="foo", status="inactive")
        session_factory, session = _mock_session_with_agent(agent)

        with patch("backend.api.routes.agents.async_session", session_factory):
            result = await controller._age_set_status("foo", "active")

        assert agent.status == "active"
        session.commit.assert_awaited_once()
        assert "activated" in result["detail"]

    @pytest.mark.asyncio
    async def test_age_set_status_deactivates(self):
        controller = AgentController.__new__(AgentController)
        agent = _FakeAgent(slug="foo", status="active")
        session_factory, session = _mock_session_with_agent(agent)

        with patch("backend.api.routes.agents.async_session", session_factory):
            result = await controller._age_set_status("foo", "inactive")

        assert agent.status == "inactive"
        session.commit.assert_awaited_once()
        assert "deactivated" in result["detail"]

    @pytest.mark.asyncio
    async def test_age_set_status_404_when_agent_missing(self):
        from litestar.exceptions import HTTPException

        controller = AgentController.__new__(AgentController)
        session_factory, session = _mock_session_with_agent(None)

        with patch("backend.api.routes.agents.async_session", session_factory):
            with pytest.raises(HTTPException) as exc_info:
                await controller._age_set_status("ghost", "active")
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_interpret_dispatch_activate_does_not_raise_typeerror(self):
        """THE regression this fixes: previously `_age_dispatch_slug_cmd`
        called `await self.activate_agent(slug)`, which raised
        `TypeError: 'put' object can't be awaited`. Now it must call
        `_age_set_status` directly and succeed."""
        controller = AgentController.__new__(AgentController)
        agent = _FakeAgent(slug="foo", status="inactive")
        session_factory, session = _mock_session_with_agent(agent)

        with patch("backend.api.routes.agents.async_session", session_factory):
            resp = await controller._age_dispatch_slug_cmd(
                "activate", ["agent", "activate", "foo"],
            )

        assert resp.success is True
        assert "activated" in resp.output
        assert agent.status == "active"

    @pytest.mark.asyncio
    async def test_interpret_dispatch_deactivate_does_not_raise_typeerror(self):
        controller = AgentController.__new__(AgentController)
        agent = _FakeAgent(slug="foo", status="active")
        session_factory, session = _mock_session_with_agent(agent)

        with patch("backend.api.routes.agents.async_session", session_factory):
            resp = await controller._age_dispatch_slug_cmd(
                "deactivate", ["agent", "deactivate", "foo"],
            )

        assert resp.success is True
        assert "deactivated" in resp.output
        assert agent.status == "inactive"

    @pytest.mark.asyncio
    async def test_rest_activate_route_still_works(self):
        """The REST route (PUT /{slug}/activate) must still work
        unchanged after extracting the shared body."""
        controller = AgentController.__new__(AgentController)
        agent = _FakeAgent(slug="foo", status="inactive")
        session_factory, session = _mock_session_with_agent(agent)

        with patch("backend.api.routes.agents.async_session", session_factory):
            result = await _handler_fn(AgentController.activate_agent)(controller, "foo")

        assert agent.status == "active"
        assert "activated" in result["detail"]


class TestRequireCapability:
    """Direct unit coverage for the new backend.api.rbac.require_capability
    helper — the shared logic behind both cap_guard() (route-level) and
    the imperative /interpret call site."""

    @pytest.mark.asyncio
    async def test_demo_role_rejected_for_non_demo_cap(self):
        from backend.api.rbac import require_capability
        from litestar.exceptions import PermissionDeniedException

        fake_connection = MagicMock()
        fake_connection.state.token_payload = None  # unauthenticated → demo

        with patch("backend.api.auth_guard.jwt_guard", new=AsyncMock()), \
             patch("backend.api.auth_guard.auth_or_demo_guard", new=AsyncMock()):
            with pytest.raises(PermissionDeniedException):
                await require_capability(fake_connection, "manage_own_agents")

    @pytest.mark.asyncio
    async def test_trader_role_allowed_for_manage_own_agents(self):
        from backend.api.rbac import require_capability

        fake_connection = MagicMock()
        fake_connection.state.token_payload = {"role": "trader"}

        with patch("backend.api.auth_guard.jwt_guard", new=AsyncMock()), \
             patch("backend.api.auth_guard.auth_or_demo_guard", new=AsyncMock()):
            await require_capability(fake_connection, "manage_own_agents")  # must not raise

    @pytest.mark.asyncio
    async def test_unknown_capability_raises_value_error(self):
        from backend.api.rbac import require_capability

        fake_connection = MagicMock()
        with pytest.raises(ValueError):
            await require_capability(fake_connection, "not_a_real_capability")

    def test_cap_guard_still_fails_fast_on_unknown_capability(self):
        """cap_guard() itself must still validate at decoration time
        (import time), not defer to the first request — this behavior
        must survive the require_capability extraction."""
        from backend.api.rbac import cap_guard

        with pytest.raises(ValueError):
            cap_guard("not_a_real_capability")
