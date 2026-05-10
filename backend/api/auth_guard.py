"""
Litestar guard that enforces JWT authentication on protected routes.
Sets request.state.token_payload with the decoded JWT on success.

Apply at the controller level:
    class OrdersController(Controller):
        guards = [jwt_guard]
"""

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.handlers.base import BaseRouteHandler


def jwt_guard(connection: ASGIConnection, handler: BaseRouteHandler) -> None:  # noqa: ARG001
    """Raise NotAuthorizedException if request carries no valid JWT."""
    auth_header = connection.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise NotAuthorizedException("Missing or invalid Authorization header")
    token = auth_header.removeprefix("Bearer ").strip()
    from backend.api.routes.auth import verify_token
    payload = verify_token(token)
    if not payload:
        raise NotAuthorizedException("Token invalid or expired")
    # Stash payload so route handlers can read user info without re-decoding
    connection.state.token_payload = payload


def admin_guard(connection: ASGIConnection, handler: BaseRouteHandler) -> None:  # noqa: ARG001
    """Require a valid JWT with role in ('admin', 'designated'). The
    designated tier inherits everything an admin can do."""
    jwt_guard(connection, handler)
    payload = getattr(connection.state, "token_payload", {})
    if payload.get("role") not in ("admin", "designated"):
        raise NotAuthorizedException("Admin access required")


def designated_guard(connection: ASGIConnection, handler: BaseRouteHandler) -> None:  # noqa: ARG001
    """Require a JWT with role='designated' — strictly above admin. Used
    for actions that touch other admins (terminate, change role, etc.)."""
    jwt_guard(connection, handler)
    payload = getattr(connection.state, "token_payload", {})
    if payload.get("role") != "designated":
        raise NotAuthorizedException("Designated-admin access required")


def is_admin_request(connection: ASGIConnection) -> bool:
    """Check if the request has a valid admin (or designated) JWT. Does
    NOT raise — returns False if not."""
    try:
        auth_header = connection.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header.removeprefix("Bearer ").strip()
        from backend.api.routes.auth import verify_token
        payload = verify_token(token)
        if not payload:
            return False
        return payload.get("role") in ("admin", "designated")
    except Exception:
        return False


def is_designated_request(connection: ASGIConnection) -> bool:
    """Check if the request has a valid designated-admin JWT. Does NOT raise."""
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


def auth_or_demo_guard(connection: ASGIConnection, handler: BaseRouteHandler) -> None:  # noqa: ARG001
    """
    Soft authentication guard for endpoints that serve both authenticated
    admins AND anonymous demo visitors (the algo UI's data endpoints).

    On prod (main):
      - admin JWT present → request flows like jwt_guard would have allowed
      - no admin JWT       → request is allowed but tagged as demo via
                            connection.state.is_demo = True

    On dev (non-main):
      - admin JWT required (mirrors the existing jwt_guard behaviour),
        because dev has no demo mode.

    Endpoints that share data between admin and demo paths read
    `connection.state.is_demo` to branch — typically gating off broker
    access via Connections._kite_for() (which raises in demo) and
    routing reads to demo fixtures.
    """
    from backend.shared.helpers.utils import is_prod_branch

    if not is_prod_branch():
        # No demo mode on dev — fall through to the strict guard.
        jwt_guard(connection, handler)
        connection.state.is_demo = False
        return

    if is_admin_request(connection):
        # Admin login on prod — populate token_payload so handlers can
        # read it (mirrors what jwt_guard would have set).
        auth_header = connection.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
        from backend.api.routes.auth import verify_token
        connection.state.token_payload = verify_token(token) or {}
        connection.state.is_demo = False
        return

    # Anonymous on prod → demo session.
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
