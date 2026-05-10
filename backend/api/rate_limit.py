"""
In-process rate limiter for auth-sensitive endpoints.

CLAUDE.md mandates a single uvicorn worker on prod, so per-process state
is sufficient — no Redis or cross-worker coordination needed. The
limiter is a sliding-window counter keyed on (client_ip, route), with
the IP resolved from Cloudflare's CF-Connecting-IP header (real client)
falling back to X-Forwarded-For and the request peer.

Used as a Litestar guard. Build one via `make_rate_limit_guard(limit,
window_seconds)` and pass it to the route's `guards=[...]` list.
"""

import time
from collections import defaultdict, deque
from threading import Lock

from litestar.connection import ASGIConnection
from litestar.exceptions import HTTPException
from litestar.handlers.base import BaseRouteHandler

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class _Limiter:
    """Sliding-window counter. `_buckets[(ip, route)]` holds a deque of
    monotonic timestamps for hits inside the active window; entries
    older than the window are purged on every hit. Memory cost is
    O(active_clients × limit) which stays small at our scale."""

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def hit(self, key: tuple[str, str], limit: int, window: float) -> tuple[bool, int]:
        """Record an attempt. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry = max(1, int(bucket[0] + window - now))
                return False, retry
            bucket.append(now)
            return True, 0


_limiter = _Limiter()


def _client_ip(connection: ASGIConnection) -> str:
    """Same fallback chain `_log_visitor` uses — CF header first, then
    XFF, then peer. Anonymous fallback shares one bucket across all
    unidentifiable clients which is the safest default."""
    headers = connection.headers
    return (
        headers.get("CF-Connecting-IP")
        or (headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        or (connection.client.host if connection.client else "")
        or "anon"
    )


def make_rate_limit_guard(limit: int, window_seconds: int):
    """Build a Litestar guard that throttles requests to `limit` per
    `window_seconds` per (client_ip, route_path). Raises 429 with a
    Retry-After header when the bucket overflows."""
    async def _guard(connection: ASGIConnection, handler: BaseRouteHandler) -> None:  # noqa: ARG001
        ip = _client_ip(connection)
        path = connection.scope.get("path", "")
        ok, retry = _limiter.hit((ip, path), limit, window_seconds)
        if not ok:
            logger.warning(
                f"RateLimit: blocked ip={ip} path={path} "
                f"limit={limit}/{window_seconds}s retry_after={retry}s"
            )
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down and try again shortly.",
                headers={"Retry-After": str(retry)},
            )
    return _guard
