"""
Tests for response compression — backend/api/app.py's `compression_config`.

2026-10 bug fix: GET /api/instruments (an ~8.2MB uncompressed JSON dump)
was never compressed, which — combined with the public Cloudflare-fronted
hostname — produced a reproducible hang for the frontend's symbol-search
autocomplete (SymbolSearchInput.svelte -> loadInstruments() ->
fetchInstruments()). Confirmed via direct nginx/backend access (bypassing
Cloudflare) that the origin itself was always fast (<70ms); only the
public, Cloudflare-fronted path for this large payload hung. Wired
Litestar's built-in CompressionConfig (gzip) in app-wide, excluding the
one Server-Sent Events endpoint (GET /api/quotes/stream) whose real-time
flush behavior compression middleware would otherwise interfere with.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

import pytest

from backend.api.app import compression_config


def test_compression_config_uses_gzip_backend():
    assert compression_config.backend == "gzip"


def test_compression_config_excludes_the_sse_stream_endpoint():
    """GET /api/quotes/stream must never be compressed — gzip's
    compression middleware buffers output to build compressed chunks,
    which would defeat (or outright break) the stream's whole point of
    flushing each tick immediately."""
    assert compression_config.exclude == "/api/quotes/stream"


@pytest.mark.asyncio
async def test_a_real_json_response_is_gzip_compressed_when_requested():
    """End-to-end: GET /api/agents/renderers (a real, already-tested route,
    ~1.8KB of JSON — comfortably above CompressionConfig's 500-byte
    minimum_size default) must come back gzip-encoded when the client
    advertises Accept-Encoding: gzip, proving the middleware actually
    fires for a real response, not just that the config object is set."""
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
            resp = await client.get(
                "/api/agents/renderers",
                headers={"Accept-Encoding": "gzip"},
            )

    assert resp.status_code == 200, resp.text
    assert resp.headers.get("content-encoding") == "gzip", (
        f"expected gzip Content-Encoding, got headers: {dict(resp.headers)}"
    )
