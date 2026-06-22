"""
Role-Based Access Control — the single source of truth for which roles
can perform which capabilities.

Design rationale
----------------
The codebase historically gated routes via scattered `admin_guard` /
`designated_guard` / `is_admin_request` checks. That works for a binary
admin/not-admin world but fragments badly when the firm grows past one
person — a 5-person boutique needs trader / risk / ops / observer roles
with overlapping-but-distinct permission sets, and updating that across
30+ route files is a recipe for silent permission leaks.

This module centralises the matrix. Each route asks "can role X do Y?"
via `has_cap(role, cap)`; the matrix decides yes/no. Adding a new role
or new capability is one entry here, not 30 grep-and-replace edits.

Roles
-----
Six effective roles. Three are NEW (trader, risk, ops, observer) and
match the operator's 5-role boutique-fund design. Three are LEGACY
(partner, admin, designated) — kept so existing JWTs / DB rows continue
to work without a destructive migration. Mapping:

    LEGACY        NEW EQUIVALENT      RATIONALE
    partner    →  observer            both = read-only, no exec rights
    admin      →  admin (kept)        unchanged
    designated →  admin (super-set)   designated was admin-of-admins;
                                      the capability matrix collapses
                                      the two — they have identical caps
                                      except `manage_admins` which only
                                      designated retains.

DEMO is implicit — any request that resolves to `role == 'demo'` (set by
`auth_or_demo_guard` for anonymous prod visitors) gets the demo cap set:
broad read-only access for showcase / portfolio purposes.

Adding a new capability
-----------------------
1. Add the cap name to `CAPS` with the set of roles that hold it.
2. In the route, replace `guards = [admin_guard]` with
   `guards = [cap_guard('your_cap_name')]`.
3. Mirror the cap in `frontend/src/lib/rbac.js` if the frontend gates
   UI on it.
"""

from __future__ import annotations

from typing import Iterable


# ── Role catalog ─────────────────────────────────────────────────────────

#: Roles a new user can be assigned via /admin/users.  Excludes 'demo'
#: (implicit, never stored) and 'partner' (legacy alias for observer —
#: we still accept it on read for back-compat but UI surfaces show
#: 'observer' as the canonical label).
ASSIGNABLE_ROLES = ("admin", "trader", "risk", "ops", "observer")

#: Every role value that may appear on `User.role` or in a JWT payload,
#: including legacy aliases. Used for validation when reading from DB
#: or accepting a role update.
VALID_ROLES = (*ASSIGNABLE_ROLES, "partner", "designated", "demo")


def normalise_role(role: str | None) -> str:
    """Map any legacy alias to its canonical form. Unknown / None / empty
    values collapse to 'observer' (the safest default — read-only,
    aggregate-only). Called by the capability resolver so the matrix
    only has to enumerate canonical roles.
    """
    if not role:
        return "observer"
    r = str(role).strip().lower()
    if r == "partner":
        return "observer"          # legacy: investor/LP → observer
    if r == "designated":
        return "admin"             # legacy: super-admin → admin
    if r in VALID_ROLES:
        return r
    return "observer"              # unknown role → safest default


# ── Capability matrix ────────────────────────────────────────────────────
#
# Capabilities are short imperative strings (verb + target). When you add
# a new route, pick a cap name from this matrix or add a new entry; the
# route's guard then asks `has_cap(request.state.role, 'your_cap')`.
#
# The matrix is intentionally NOT loaded from DB. Hot-path lookup needs
# to be in-process; the cost of a DB hit per route gate is too high. If
# the matrix ever needs operator-configurability, lift it into a DB
# table at that point — until then, code is fine.

CAPS: dict[str, frozenset[str]] = {
    # ── Reads ─────────────────────────────────────────────────────────
    "view_aggregate":           frozenset({"admin", "trader", "risk", "ops", "observer", "demo"}),
    "view_all_books":           frozenset({"admin", "trader", "risk", "ops", "demo"}),  # demo with masked accts
    "view_derivatives":         frozenset({"admin", "trader", "risk", "demo"}),
    "view_strategies_catalog":  frozenset({"admin", "trader", "risk", "observer", "demo"}),
    "view_agents_catalog":      frozenset({"admin", "trader", "risk", "demo"}),
    "view_settings_readonly":   frozenset({"admin", "risk", "ops", "demo"}),
    "view_audit":               frozenset({"admin", "risk", "ops"}),
    "view_users":               frozenset({"admin"}),
    "view_brokers":             frozenset({"admin", "ops", "risk", "demo"}),  # demo with masked secrets
    "view_lab":                 frozenset({"admin", "trader", "risk", "demo"}),
    "view_pulse":               frozenset({"admin", "trader", "risk", "ops", "demo"}),
    "view_charts":              frozenset({"admin", "trader", "risk", "ops", "demo"}),
    "view_market_summary":      frozenset({"admin", "trader", "risk", "ops", "observer", "demo"}),

    # ── Trading ───────────────────────────────────────────────────────
    "place_order":              frozenset({"admin", "trader"}),
    "modify_order":             frozenset({"admin", "trader"}),
    "cancel_order":             frozenset({"admin", "trader"}),

    # ── Strategies ────────────────────────────────────────────────────
    "view_strategies":          frozenset({"admin", "trader", "risk", "ops", "observer", "demo"}),
    "manage_own_strategies":    frozenset({"admin", "trader"}),
    "reassign_strategies":      frozenset({"admin"}),

    # ── Agents ────────────────────────────────────────────────────────
    "manage_own_agents":        frozenset({"admin", "trader"}),
    "disable_any_agent":        frozenset({"admin", "risk"}),
    "manage_grammar_tokens":    frozenset({"admin"}),

    # ── Risk / settings ───────────────────────────────────────────────
    "adjust_risk_floors":       frozenset({"admin", "risk"}),
    "manage_settings":          frozenset({"admin"}),
    "view_hedge_proxies":       frozenset({"admin", "trader", "risk", "demo"}),
    "manage_hedge_proxies":     frozenset({"admin", "trader"}),

    # ── Brokers ───────────────────────────────────────────────────────
    "manage_brokers":           frozenset({"admin", "ops"}),
    "test_broker_connection":   frozenset({"admin", "ops"}),

    # ── Users ─────────────────────────────────────────────────────────
    "manage_users":             frozenset({"admin"}),
    "approve_users":            frozenset({"admin"}),
    "manage_admins":            frozenset({"admin"}),  # legacy: was designated-only
    "impersonate":              frozenset({"admin"}),

    # ── Sim / replay / lab ────────────────────────────────────────────
    "run_simulator":            frozenset({"admin", "trader", "risk", "demo"}),  # demo session-only
    "run_replay":               frozenset({"admin", "trader", "risk", "demo"}),
    "manage_lab_threads":       frozenset({"admin", "trader"}),
    "mint_mcp_token":           frozenset({"admin"}),

    # ── Reports / export ──────────────────────────────────────────────
    "export_reports":           frozenset({"admin", "trader", "risk", "ops", "observer"}),
    "view_nav":                 frozenset({"admin", "trader", "risk", "ops", "observer", "demo"}),
    "trigger_nav_compute":      frozenset({"admin", "ops"}),

    # ── Lab / MCP ────────────────────────────────────────────────────
    "use_mcp_tools":            frozenset({"admin", "trader"}),
}


def has_cap(role: str | None, cap: str) -> bool:
    """Return True iff `role` (after normalisation) holds `cap`.

    Unknown caps return False — fail-closed. A typo in a route's guard
    silently locks everyone out, which is loud + obvious during dev,
    much safer than a typo silently letting everyone IN.
    """
    normalised = normalise_role(role)
    allowed = CAPS.get(cap)
    if allowed is None:
        return False
    return normalised in allowed


def caps_for_role(role: str | None) -> list[str]:
    """All capabilities a role holds. Useful for the auth /me endpoint —
    the frontend reads this and can hide nav items / disable buttons
    without re-implementing the matrix client-side."""
    normalised = normalise_role(role)
    return sorted(cap for cap, roles in CAPS.items() if normalised in roles)


def resolve_role_from_connection(connection) -> str:
    """Resolver — read the role off a request connection (Litestar
    ASGIConnection). Returns the canonical role string. Defaults to
    'demo' when nothing is set (e.g. an anonymous request that didn't
    go through `auth_or_demo_guard`).
    """
    payload = getattr(connection.state, "token_payload", None) or {}
    return normalise_role(payload.get("role"))


# ── Horizontal scoping (slice 5) ─────────────────────────────────────────
#
# The capability matrix above is VERTICAL — it answers "can role X do
# action Y?". Horizontal scoping answers "on which accounts / strategies
# can role X act?". The two compose: cap_guard rejects before the route
# runs; account_scope_filter narrows the result set.
#
# Default policy per role (slice 5):
#   admin / risk / ops / observer / demo   → ALL (firm-wide visibility)
#   trader                                  → only User.assigned_accounts
#                                             (empty = NONE — fail-safe)
#
# A trader with an empty assigned_accounts list sees zero positions /
# holdings / funds. That's the intended initial state for a newly-
# onboarded trader; admin grants accounts explicitly via /admin/users.

#: Roles whose horizontal scope is firm-wide regardless of assigned
#: list contents. Trader is the only role that respects assigned_*;
#: all others see everything (subject to the cap matrix's vertical
#: gates).
_FIRM_WIDE_ROLES = frozenset({"admin", "risk", "ops", "observer", "demo"})


def accounts_in_scope(role: str | None, assigned: list[str] | None,
                       all_accounts: list[str]) -> list[str]:
    """Effective broker-account scope for the current user.

    - Firm-wide roles → `all_accounts` (the live broker registry).
    - Trader → `assigned` (the per-user list). Empty list = empty
      result (the trader explicitly has no accounts assigned).
    - Unknown role → empty list (fail-closed).

    Callers pass `all_accounts` because it depends on the live
    Connections registry which is request-scope-irrelevant — let the
    caller fetch it once.
    """
    r = normalise_role(role)
    if r in _FIRM_WIDE_ROLES:
        return list(all_accounts)
    if r == "trader":
        return [a for a in (assigned or []) if a in all_accounts]
    return []


async def user_scope_for_connection(connection) -> tuple[list[str], list[int]]:
    """Resolve `(accounts, strategies)` scope for the request's actor.

    Reads the JWT-stamped username off `connection.state.token_payload`,
    looks up the user row, returns the assigned lists. Returns empty
    tuples for anonymous demo / observer / etc. — the firm-wide
    helpers don't read these.

    Wrapped in try/except: an audit row write failure must never
    break the user's request, so a scope lookup hiccup falls back to
    empty scope (the safest default — trader sees nothing, firm-wide
    roles ignore the list anyway).
    """
    payload = getattr(connection.state, "token_payload", None) or {}
    username = str(payload.get("sub") or "").strip()
    if not username:
        return ([], [])
    try:
        from sqlalchemy import select
        from backend.api.database import async_session
        from backend.api.models import User
        async with async_session() as session:
            row = (await session.execute(
                select(User.assigned_accounts, User.assigned_strategies)
                  .where(User.username == username)
            )).first()
            if not row:
                return ([], [])
            accts = list(row[0] or [])
            strats = list(row[1] or [])
            return (accts, strats)
    except Exception:
        return ([], [])


# ── Guard factory ────────────────────────────────────────────────────────


def cap_guard(cap: str):
    """Return a Litestar guard function that requires `cap`. Use as:

        @get("/admin/something", guards=[cap_guard("manage_brokers")])
        async def handler(...): ...

    Internally chains through `jwt_guard` (or `auth_or_demo_guard`
    depending on the cap — caps that include 'demo' in their role set
    are demo-eligible, all others require a real JWT).
    """
    from litestar.connection import ASGIConnection
    from litestar.exceptions import PermissionDeniedException
    from litestar.handlers.base import BaseRouteHandler
    from backend.api.auth_guard import jwt_guard, auth_or_demo_guard

    allowed = CAPS.get(cap)
    if allowed is None:
        raise ValueError(f"cap_guard: unknown capability {cap!r}")
    demo_eligible = "demo" in allowed

    async def _guard(connection: ASGIConnection, handler: BaseRouteHandler) -> None:  # noqa: ARG001
        # Demo-eligible caps go through the soft guard (anonymous OK on
        # prod); strict caps require a valid JWT.
        if demo_eligible:
            await auth_or_demo_guard(connection, handler)
        else:
            await jwt_guard(connection, handler)
        role = resolve_role_from_connection(connection)
        if role not in allowed:
            raise PermissionDeniedException(f"Capability '{cap}' required")

    _guard.__name__ = f"cap_guard__{cap}"
    return _guard


# ── Convenience: capability sets exported for the frontend ──────────────

def export_role_to_caps() -> dict[str, list[str]]:
    """Build a `{role: [caps]}` map for the frontend bootstrap. Called
    once from the /auth/me endpoint so the SPA doesn't have to mirror
    the matrix by hand."""
    return {role: caps_for_role(role)
            for role in (*ASSIGNABLE_ROLES, "demo", "partner", "designated")}
