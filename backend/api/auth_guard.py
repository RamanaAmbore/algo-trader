"""
Litestar guard that enforces JWT authentication on protected routes.
Sets request.state.token_payload with the decoded JWT on success.

Apply at the controller level:
    class OrdersController(Controller):
        guards = [jwt_guard]
"""

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException, PermissionDeniedException
from litestar.handlers.base import BaseRouteHandler


def _reject_if_user_invalid(row, tv: int) -> None:
    """Raise NotAuthorizedException for any invalid user state condition.

    Checks (in order): existence, termination, suspension, inactivity, and
    token-version mismatch. Callers invoke this after a successful DB fetch
    of the user row; pass row=None when the user no longer exists.
    """
    if not row:
        raise NotAuthorizedException("User no longer exists")
    if row.terminated_at is not None:
        raise NotAuthorizedException("Account terminated")
    if row.suspended_at is not None:
        raise NotAuthorizedException("Account suspended")
    if not row.is_active:
        raise NotAuthorizedException("Account inactive")
    if (row.token_version or 1) != tv:
        raise NotAuthorizedException("Session invalidated; please sign in again")


async def jwt_guard(connection: ASGIConnection, handler: BaseRouteHandler) -> None:  # noqa: ARG001
    """Validate the bearer JWT signature + expiry, then validate the
    user's live state from the DB:

    - User must exist and not be terminated / suspended / inactive.
    - JWT's `tv` claim must match the user's current `token_version`.
    - The role written into request.state is read fresh from the DB so
      a demoted designated can't keep using designated rights.

    The DB hit is one indexed SELECT keyed on `username`; ~1 ms each
    request. Bumping `User.token_version` invalidates every JWT that
    carries the old `tv` on the very next request that hits a guard.
    """
    auth_header = connection.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise NotAuthorizedException("Missing or invalid Authorization header")
    token = auth_header.removeprefix("Bearer ").strip()

    from backend.api.routes.auth import verify_token
    payload = verify_token(token)
    if not payload:
        raise NotAuthorizedException("Token invalid or expired")

    # Live state check — defends against the "JWT is still valid for
    # 24h after the user was suspended/terminated/demoted" gap.
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import User

    sub = payload.get("sub", "")
    tv  = int(payload.get("tv", 1))
    async with async_session() as session:
        result = await session.execute(
            select(
                User.token_version, User.role,
                User.is_active, User.terminated_at, User.suspended_at,
                User.must_change_password,
            ).where(User.username == sub)
        )
        row = result.first()

    _reject_if_user_invalid(row, tv)

    # Force-password-change wall: when the must_change_password flag is
    # set (admin-issued reset), the user can ONLY hit the change-password
    # / me / logout paths. Every other route is rejected with a
    # specific 401 so the frontend redirects to /auth/change-password.
    if row.must_change_password:
        path = connection.scope.get("path", "") or ""
        ALLOWED = (
            "/api/auth/change-password",
            "/api/auth/me",
            "/api/auth/logout",
        )
        if path not in ALLOWED:
            raise NotAuthorizedException(
                "Password change required — complete it at /auth/change-password",
            )

    # Refresh role from DB so a demoted designated can't keep using
    # designated rights via a stale claim. The remaining payload fields
    # (sub, display_name, etc.) stay as-is since they're identity-only.
    payload["role"] = row.role
    connection.state.token_payload = payload

    # Impersonation forensic trail — when a write request comes in under
    # an impersonation JWT (imp_by claim present), emit a WARNING log
    # entry so the audit reconstruction has the actor's identity at
    # write time. The impersonation_events table tracks session start
    # / end; the log here links every mid-session write to the actor.
    imp_by = payload.get("imp_by")
    if imp_by:
        method = (connection.scope.get("method") or "").upper()
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            path = connection.scope.get("path", "") or ""
            # Allow the stop-impersonate hop itself to land quietly.
            if path != "/api/auth/stop-impersonate":
                from backend.shared.helpers.ramboq_logger import get_logger
                _logger = get_logger("backend.api.auth_guard")
                _logger.warning(
                    f"IMPERSONATE write: actor={imp_by!r} → target={sub!r} "
                    f"method={method} path={path}"
                )


async def admin_guard(connection: ASGIConnection, handler: BaseRouteHandler) -> None:  # noqa: ARG001
    """Require an admin-tier JWT — `designated` (firm owner) OR `admin`
    (operational support). Both tiers count as "admin" for routes that
    don't specifically need firm-owner authority. Routes that DO need
    firm-owner authority (terminate, manage_users, manage_settings,
    impersonate, ...) use `designated_guard` or `cap_guard("xxx")`
    where xxx admits only `designated`."""
    await jwt_guard(connection, handler)
    payload = getattr(connection.state, "token_payload", {})
    if payload.get("role") not in ("designated", "admin"):
        raise PermissionDeniedException("Admin access required")


async def designated_guard(connection: ASGIConnection, handler: BaseRouteHandler) -> None:  # noqa: ARG001
    """Require a firm-owner JWT (terminate, promote, manage_admins,
    impersonate, manage_settings, manage_users). Strictly above the
    operational `admin` tier — operational admins don't pass."""
    await jwt_guard(connection, handler)
    payload = getattr(connection.state, "token_payload", {})
    if payload.get("role") != "designated":
        raise PermissionDeniedException("Firm-owner access required")


def is_authenticated_request(connection: ASGIConnection) -> bool:
    """True iff the request carries a valid JWT, regardless of role.
    Used by data-masking chokepoints: anonymous = masked; any
    authenticated user = unmasked. Signature/expiry only — does NOT
    validate token_version or live user state, mirroring
    is_admin_request."""
    try:
        auth_header = connection.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header.removeprefix("Bearer ").strip()
        from backend.api.routes.auth import verify_token
        return verify_token(token) is not None
    except Exception:
        return False


def is_admin_request(connection: ASGIConnection) -> bool:
    """Check if the request has a valid admin-tier JWT (`designated`
    firm-owner OR `admin` operational). Signature-and-expiry check
    only — does NOT validate token_version or live user state. Used
    by demo-mode chokepoints where the cost of a per-request DB hit
    isn't justified; treat the result as 'has a credible-looking JWT'
    rather than 'is currently authorised'."""
    try:
        auth_header = connection.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header.removeprefix("Bearer ").strip()
        from backend.api.routes.auth import verify_token
        payload = verify_token(token)
        if not payload:
            return False
        return payload.get("role") in ("designated", "admin")
    except Exception:
        return False


def is_designated_request(connection: ASGIConnection) -> bool:
    """Check if the request carries a firm-owner (`designated`) JWT.
    Strictly above operational admin. Same caveat as
    `is_admin_request`: signature/expiry only, no live-state check."""
    try:
        auth_header = connection.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header.removeprefix("Bearer ").strip()
        from backend.api.routes.auth import verify_token
        payload = verify_token(token)
        return bool(payload and payload.get("role") == "designated")
    except Exception:
        return False


# ── Demo session helpers ────────────────────────────────────────────────
#
# Demo mode == "an anonymous visitor on the prod (main) branch is browsing
# the algo pages". The chokepoint pattern: every code path that touches a
# real broker, account, or settings goes through one of these helpers.
# A scattered `if not is_admin: ...` in 30 endpoints is a recipe for one
# missing check exposing a real account; a single `is_demo_request()` call
# at the broker / order chokepoints means we either find the bug or we
# don't have one.
#
# Note: dev branches don't have demo mode — anyone who lands on a dev
# deployment without auth is a developer who hasn't logged in yet, not a
# recruiter. The check below explicitly returns False on non-prod so we
# don't accidentally let a dev session into the synthetic data lane.


def is_demo_request(connection: ASGIConnection) -> bool:
    """
    True when:
      - we're on the prod (main) branch
      - the request has no admin JWT (anonymous OR non-admin user)

    Demo sessions get the algo UI but every broker / account / settings
    touchpoint must reroute or refuse via this flag. Use with the
    `auth_or_demo_guard` for endpoints that should serve both
    authenticated admins AND anonymous visitors with separate behaviour.
    """
    from backend.shared.helpers.utils import is_prod_branch
    if not is_prod_branch():
        return False
    return not is_admin_request(connection)


async def auth_or_demo_guard(connection: ASGIConnection, handler: BaseRouteHandler) -> None:  # noqa: ARG001
    """
    Soft authentication guard for endpoints that serve both authenticated
    admins AND anonymous demo visitors (the algo UI's data endpoints).

    Authenticated (any valid JWT) → request flows like jwt_guard.
    Anonymous → request is allowed and tagged as demo via
                connection.state.is_demo = True.

    Works on both prod (main) and dev (non-main) branches.

    Endpoints that share data between admin and demo paths read
    `connection.state.is_demo` to branch — typically gating off broker
    access via Connections._kite_for() (which raises in demo) and
    routing reads to demo fixtures.
    """
    if is_authenticated_request(connection):
        # ANY valid JWT (designated / admin / trader / risk / partner)
        # → run the strict guard so live-state checks fire
        # (suspended/terminated rejection, role refresh). Pre-fix this
        # only accepted admin-tier JWTs which downgraded trader/risk
        # employees to demo sessions on prod (they couldn't place
        # orders or see real account codes).
        await jwt_guard(connection, handler)
        connection.state.is_demo = False
        return

    # Anonymous → demo session (prod and dev).
    connection.state.token_payload = {"role": "demo", "user": "demo"}
    connection.state.is_demo = True


class NotAllowedInDemo(Exception):
    """
    Raised by the broker / order / settings chokepoints when a demo
    request tries to reach a real-money surface. Callers should catch
    and surface the error verbatim (or re-raise as a 403 HTTPException
    in route handlers).
    """
    def __init__(self, what: str = "operation"):
        super().__init__(f"Demo: {what} not available.")
        self.what = what
