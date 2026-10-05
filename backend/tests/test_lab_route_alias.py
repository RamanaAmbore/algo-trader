"""Route naming after the lab → research/mcp rename.

Research workspace paths live under `/api/research/*`; MCP tool and audit
paths live under `/api/mcp/*`. The old `/api/lab/*` paths are legacy aliases
(`LegacyLabResearchController` / `LegacyLabMcpController` in
backend/api/routes/lab.py) that reuse the same handlers, so existing MCP
clients and saved links keep working. Anonymous requests are demo-admitted
for view_research, so the research read is reachable without a token; the
DB session is stubbed to an empty result set.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
from litestar.routes import HTTPRoute

NEW_RESEARCH = ("/api/research/threads", "/api/research/drafts", "/api/research/chat")
NEW_MCP = ("/api/mcp/confirm-token", "/api/mcp/place-order", "/api/mcp/audit")


@asynccontextmanager
async def _empty_session():
    session = MagicMock()

    async def _execute(_q):
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    session.execute = _execute
    yield session


def _handlers_by_path(app):
    """Map each HTTP route path to {method: (handler_name, function qualname)}.

    Keyed per method so a path served by both GET and POST handlers is still
    one entry per (path, method) pair.
    """
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for route in app.routes:
        if not isinstance(route, HTTPRoute):
            continue
        out.setdefault(route.path, {})
        for method, (h, _kwargs) in route.route_handler_map.items():
            if method == "OPTIONS":
                continue
            fn = getattr(h.fn, "value", h.fn)
            out[route.path][method] = (h.handler_name, fn.__qualname__)
    return out


def test_new_paths_are_registered(app):
    handlers = _handlers_by_path(app)
    routes = [r.path for r in app.routes if isinstance(r, HTTPRoute)]
    for path in (*NEW_RESEARCH, *NEW_MCP):
        assert path in handlers, f"{path} not registered"
        assert handlers[path], f"{path} has no method handlers"
        # One HTTPRoute per path; a duplicate controller would show up here.
        assert routes.count(path) == 1, f"{path} registered {routes.count(path)} times"


def test_legacy_lab_paths_share_handlers_with_new_paths(app):
    handlers = _handlers_by_path(app)
    pairs = [
        ("/api/lab/threads", "/api/research/threads"),
        ("/api/lab/drafts", "/api/research/drafts"),
        ("/api/lab/chat", "/api/research/chat"),
        ("/api/lab/confirm-token", "/api/mcp/confirm-token"),
        ("/api/lab/place-order", "/api/mcp/place-order"),
        ("/api/lab/audit", "/api/mcp/audit"),
    ]
    for legacy, new in pairs:
        assert legacy in handlers, f"legacy {legacy} not registered"
        assert handlers[legacy] == handlers[new], f"{legacy} != {new}"


@pytest.mark.asyncio
async def test_new_research_threads_path_resolves(async_client):
    with patch("backend.api.routes.lab.async_session", new=_empty_session):
        res = await async_client.get("/api/research/threads")
    assert res.status_code == 200, res.text
    assert res.json() == []


@pytest.mark.asyncio
async def test_legacy_lab_threads_matches_research_threads(async_client):
    with patch("backend.api.routes.lab.async_session", new=_empty_session):
        lab = await async_client.get("/api/lab/threads")
        research = await async_client.get("/api/research/threads")

    assert lab.status_code == 200, lab.text
    assert research.status_code == lab.status_code
    assert research.json() == lab.json() == []


@pytest.mark.asyncio
async def test_legacy_lab_mcp_path_behaves_like_new_mcp_path(async_client):
    # Unauthenticated mint must be rejected identically on both paths.
    body = {}
    legacy = await async_client.post("/api/lab/confirm-token", json=body)
    new = await async_client.post("/api/mcp/confirm-token", json=body)
    assert legacy.status_code == new.status_code
    assert legacy.status_code in (401, 403)
