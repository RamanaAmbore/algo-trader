"""
Audit middleware — writes one `audit_log` row per successful mutating
HTTP request handled by the API.

Why a middleware (not a decorator per-route):
- Decorators have to be applied N times, easy to forget; middleware
  catches everything by default.
- The forensic trail SEBI Cat-III requires says "every mutating event",
  not "every event we remembered to decorate".
- Per-route decorators can ADD semantic detail (before/after JSON of a
  resource patch) on top of what middleware captures — those land in a
  later slice when specific routes need richer audit detail.

What's captured (v1):
- Actor (user_id + username + role, snapshotted from JWT payload)
- Action (HTTP method + path)
- Target type + id (best-effort from path parameters)
- HTTP status code + summary (response detail when present)
- Request correlation UUID, mirrored in `X-Request-ID` response header
- Client IP + user-agent

What's NOT captured:
- GETs (read traffic dwarfs writes; no forensic value)
- Request/response bodies (PII + cost; route-level decorators can add
  these for specific actions in a later slice)

Demo writes are ignored (demo can't actually mutate; a 403 from
cap_guard short-circuits before the response leaves the route).
Anonymous reads that somehow reach a mutating route get
actor_username='' / role='demo' / user_id=None — surfaces as "unknown
caller" in the audit UI.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from litestar.middleware import ASGIMiddleware
from litestar.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


# Methods that mutate state. GETs intentionally excluded.
_MUTATING = frozenset({"POST", "PATCH", "PUT", "DELETE"})

# Paths to suppress from the audit log. The /api/health endpoint is
# noisy + low-value; auth flows (/login, /refresh) have their own
# specialized audit via the auth controller itself.
_SUPPRESS_PREFIXES = (
    "/api/health",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/refresh",
    "/api/auth/whoami",
    "/api/auth/me",
)


def _resolve_target_from_path(path: str) -> tuple[Optional[str], Optional[str]]:
    """Best-effort target_type + target_id extraction. The convention
    /api/admin/<type>/<id>[/<verb>] lets us peel the {id} after the
    plural type. Returns (None, None) when no clean extraction is
    possible — the audit row still records `path` in that case so
    nothing's lost.
    """
    parts = path.strip("/").split("/")
    # /api/<area>/<type>/<id>... — find the first segment after "admin"
    # or after "/api/<area>/" that's followed by a non-verb token.
    try:
        idx = parts.index("admin")
    except ValueError:
        # Non-admin path. Try /api/<plural>/<id>.
        if len(parts) >= 3 and parts[0] == "api":
            type_seg = parts[1]
            id_seg = parts[2] if len(parts) > 2 else None
            return (type_seg, id_seg)
        return (None, None)
    if idx + 2 < len(parts):
        return (parts[idx + 1], parts[idx + 2])
    if idx + 1 < len(parts):
        return (parts[idx + 1], None)
    return (None, None)


def _action_label(method: str, path: str) -> str:
    """Compact action label — e.g. 'POST /api/admin/brokers'. Truncate
    long paths so the index stays small."""
    return f"{method} {path[:100]}"


async def _write_audit(
    actor_user_id: Optional[int],
    actor_username: str,
    actor_role: str,
    method: str,
    path: str,
    status_code: int,
    summary: Optional[str],
    request_id: str,
    client_ip: Optional[str],
    user_agent: Optional[str],
) -> None:
    """One-shot insert. Wrapped in try/except — an audit-write failure
    must never break the user's request response (the request already
    succeeded; we're writing a forensic row after the fact).
    """
    from backend.api.database import async_session
    from backend.api.models import AuditLog

    target_type, target_id = _resolve_target_from_path(path)
    try:
        async with async_session() as session:
            row = AuditLog(
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                actor_role=actor_role,
                action=_action_label(method, path),
                method=method,
                path=path[:255],
                target_type=target_type[:40] if target_type else None,
                target_id=target_id[:80] if target_id else None,
                status_code=status_code,
                summary=summary[:1000] if summary else None,
                request_id=request_id,
                client_ip=client_ip[:45] if client_ip else None,
                user_agent=user_agent[:255] if user_agent else None,
            )
            session.add(row)
            await session.commit()
    except Exception as exc:
        logger.warning(
            f"audit_log write failed (actor={actor_username!r} "
            f"action={method} {path} status={status_code}): {exc}"
        )


class AuditMiddleware(ASGIMiddleware):
    """ASGI middleware. Watches every response; for mutating methods
    that produced a 2xx/3xx status, writes an audit row out-of-band.

    The middleware does NOT block the response — the audit write is
    scheduled via asyncio.create_task so the request completes
    immediately and the row lands shortly after. If the audit-write
    coroutine raises, the warning is logged and dropped; we never let
    the audit pipeline break the user's request.
    """

    async def handle(self, scope: Scope, receive: Receive, send: Send,
                     next_app: ASGIApp) -> None:
        if scope.get("type") != "http":
            await next_app(scope, receive, send)
            return

        method = scope.get("method") or ""
        path = (scope.get("path") or "").rstrip("/") or "/"

        # Fast path — skip non-mutating + suppressed paths without any
        # extra work. The ~99% read traffic hits this branch.
        if method not in _MUTATING:
            await next_app(scope, receive, send)
            return
        if any(path.startswith(p) for p in _SUPPRESS_PREFIXES):
            await next_app(scope, receive, send)
            return

        # Generate the request id NOW (before the route handler runs)
        # so the route can pick it off scope.state and surface it in
        # response headers. Idempotent — if upstream middleware
        # already set one, reuse it.
        request_id = scope.get("state", {}).get("request_id")
        if not request_id:
            request_id = str(uuid.uuid4())
            scope.setdefault("state", {})["request_id"] = request_id

        # Wrap `send` so we can capture the response status code.
        # Buffer the response body for the summary (truncated to 1 KB
        # so we don't double the response memory cost).
        captured = {"status": 0, "body_chunks": [], "body_len": 0}

        async def _send_wrapper(message):
            if message.get("type") == "http.response.start":
                captured["status"] = int(message.get("status") or 0)
                # Inject the X-Request-ID header so the client can
                # correlate the request to a specific audit row.
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            elif message.get("type") == "http.response.body":
                body = message.get("body") or b""
                if captured["body_len"] < 1024:
                    captured["body_chunks"].append(body[:1024 - captured["body_len"]])
                    captured["body_len"] += len(body)
            await send(message)

        await next_app(scope, receive, _send_wrapper)

        status = captured["status"]
        if status < 200 or status >= 400:
            # Failed requests (4xx / 5xx) are out of scope for v1 audit.
            # A future slice can add a parallel `failed_actions` log if
            # SEBI requires it (cap-III generally doesn't audit 4xx).
            return

        # Resolve actor from the token payload stamped by jwt_guard /
        # auth_or_demo_guard. State scope is set by the guard during
        # request handling.
        state = scope.get("state") or {}
        payload = state.get("token_payload") or {}
        actor_username = str(payload.get("sub") or "")
        actor_role = str(payload.get("role") or "")
        actor_user_id = None
        try:
            actor_user_id = int(payload.get("user_id")) if payload.get("user_id") else None
        except (TypeError, ValueError):
            actor_user_id = None

        # Demo writes shouldn't reach this point (cap_guard 403s
        # before the handler runs) but if a route somehow lets demo
        # write something, we still capture the row — the empty
        # username + 'demo' role makes that visible in the UI.
        if actor_role == "demo":
            actor_username = "demo"

        # Client IP + user-agent from headers.
        headers = dict(scope.get("headers") or [])
        # Litestar passes headers as a list of (bytes, bytes) tuples; convert.
        if isinstance(scope.get("headers"), list):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1", errors="replace")
                       for k, v in scope.get("headers")}
        client_ip = headers.get("x-forwarded-for", "").split(",")[0].strip() \
                    or headers.get("x-real-ip", "") \
                    or (scope.get("client") or ("",))[0]
        user_agent = headers.get("user-agent", "")[:255]

        # Parse summary — most JSON responses include a `detail`
        # string for status messages. Best-effort: take the first 1 KB
        # of body, try to decode as JSON, pluck `detail` if present;
        # else fall back to the truncated text.
        body_bytes = b"".join(captured["body_chunks"])[:1024]
        summary: Optional[str] = None
        if body_bytes:
            try:
                import json as _json
                obj = _json.loads(body_bytes.decode("utf-8", errors="replace"))
                if isinstance(obj, dict):
                    summary = obj.get("detail") or obj.get("message") or None
            except Exception:
                pass
            if not summary:
                # Plain-text fallback — drop the JSON wrapping if it
                # didn't parse, take the first line so the audit UI
                # row is glanceable.
                summary = body_bytes.decode("utf-8", errors="replace").splitlines()[0][:200]

        # Schedule the write out-of-band. Don't await — the response
        # has already started leaving the server.
        try:
            asyncio.get_running_loop().create_task(_write_audit(
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                actor_role=actor_role,
                method=method,
                path=path,
                status_code=status,
                summary=summary,
                request_id=request_id,
                client_ip=client_ip or None,
                user_agent=user_agent or None,
            ))
        except RuntimeError:
            # No running loop (sync test harness) — skip silently.
            pass
