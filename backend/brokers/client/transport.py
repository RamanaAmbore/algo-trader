"""httpx-over-UDS transport — single shared AsyncClient.

One AsyncClient per process, pinned to /tmp/ramboq_conn.sock via
httpx.AsyncHTTPTransport(uds=...). httpx pools the keep-alive
connections internally, so callers don't need to manage anything;
they just `await get_client().get("/internal/...")`.

The base URL is `http://conn` — the host is ignored by the UDS
transport but httpx requires SOMETHING, and "conn" beats "localhost"
in logs and error messages.

Lifecycle: the client is created lazily on first use and stays alive
for the process lifetime. There's no shutdown hook because Litestar
already closes httpx clients on app shutdown via the standard
asyncio cancellation chain — and even if it didn't, OS cleanup on
exit handles the dangling socket.
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

# Override via env for local dev or for the dev instance pointing
# at the same prod UDS — operator asked: "can dev domain use the
# same broker connection". Default matches the systemd unit.
CONN_SOCK = os.environ.get("RAMBOQ_CONN_SOCK", "/tmp/ramboq_conn.sock")

# Generous timeout — fetch_holdings/positions/margins each round-
# trip to 3-5 broker APIs in parallel inside conn_service via
# @for_all_accounts, and a slow account (Dhan rate-limit cool-off,
# Groww first-call) can take 8-12s before the inner ThreadPoolExecutor
# completes. 30s gives headroom; conn_service's own logic times out
# the underlying broker calls before this fires.
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient. Cheap to call repeatedly."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=CONN_SOCK),
            base_url="http://conn",
            timeout=_TIMEOUT,
        )
    return _client


async def aclose() -> None:
    """Close the shared client. Called from main API shutdown hook."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
