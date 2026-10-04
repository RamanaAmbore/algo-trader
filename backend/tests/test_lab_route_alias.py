"""Legacy /api/research alias must serve the same handlers as /api/lab.

The alias (`LegacyResearchController` in backend/api/routes/lab.py) exists so
old MCP clients and saved links keep working after the rename. Anonymous
requests are demo-admitted for view_lab, so the read endpoint is reachable
without a token; the DB session is stubbed to an empty result set.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest


@asynccontextmanager
async def _empty_session():
    session = MagicMock()

    async def _execute(_q):
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    session.execute = _execute
    yield session


@pytest.mark.asyncio
async def test_research_alias_matches_lab_threads(async_client):
    with patch("backend.api.routes.lab.async_session", new=_empty_session):
        lab = await async_client.get("/api/lab/threads")
        legacy = await async_client.get("/api/research/threads")

    assert lab.status_code == 200, lab.text
    assert legacy.status_code == lab.status_code
    assert legacy.json() == lab.json() == []
