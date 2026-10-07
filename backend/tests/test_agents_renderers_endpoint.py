"""
Tests for GET /api/agents/renderers — backend/api/routes/agents.py.

Lets the frontend's event-agent renderer picker read the live catalogue
from event_agents.RENDERS instead of hardcoding the list.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

import pytest

from backend.api.routes.agents import AgentController, RendererInfo
from backend.api.algo.event_agents import RENDERS


def _handler_fn(handler):
    return handler.fn


@pytest.mark.asyncio
async def test_list_renderers_returns_every_render_key():
    controller = AgentController.__new__(AgentController)
    out = await _handler_fn(AgentController.list_renderers)(controller)

    assert isinstance(out, list)
    assert len(out) == len(RENDERS)
    keys = {r.key for r in out}
    assert keys == set(RENDERS.keys())


@pytest.mark.asyncio
async def test_list_renderers_entries_are_renderer_info_with_nonempty_label():
    controller = AgentController.__new__(AgentController)
    out = await _handler_fn(AgentController.list_renderers)(controller)

    for item in out:
        assert isinstance(item, RendererInfo)
        assert item.key
        assert item.label, f"label must be non-empty for {item.key!r}"
        assert isinstance(item.description, str)


@pytest.mark.asyncio
async def test_list_renderers_sorted_by_key():
    controller = AgentController.__new__(AgentController)
    out = await _handler_fn(AgentController.list_renderers)(controller)
    keys = [r.key for r in out]
    assert keys == sorted(keys)


@pytest.mark.asyncio
async def test_list_renderers_fill_entry_shape():
    controller = AgentController.__new__(AgentController)
    out = await _handler_fn(AgentController.list_renderers)(controller)
    fill = next(r for r in out if r.key == "fill")
    assert fill.label == "Fill"
    assert fill.description  # has a docstring-derived description now


@pytest.mark.asyncio
async def test_renderers_route_is_registered_as_static_path_not_slug_lookup():
    """Regression guard: GET /api/agents/renderers must resolve to
    list_renderers, not get_agent(slug='renderers') (which would 404
    with 'Agent 'renderers' not found' since no such agent exists)."""
    from unittest.mock import patch, AsyncMock
    from backend.api.app import app

    with patch("backend.api.app.init_db", new=AsyncMock()), \
         patch("backend.api.app._rebuild_broker_connections", new=AsyncMock()), \
         patch("backend.api.app.bg_startup", new=AsyncMock()), \
         patch("backend.api.app.bg_shutdown", new=AsyncMock()):
        app.on_startup = []
        app.on_shutdown = []
        from litestar.testing import AsyncTestClient
        async with AsyncTestClient(app=app) as client:
            resp = await client.get("/api/agents/renderers")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == len(RENDERS)
