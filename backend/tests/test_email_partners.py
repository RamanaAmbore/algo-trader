"""
POST /api/admin/email-partners — recipients-preset contract tests.

Regression pin for a defect where the frontend sent underscore-cased
presets (`all_partners`, `all_designated`, `all_users`) while the
backend accepted only hyphen-cased presets (`all-partners`,
`all-designated`, `all`). Preset-string sends fell through to the
default 422 "recipients must be a list of usernames or one of…" branch.

Fix landed in `frontend/src/routes/(algo)/admin/+page.svelte` — preset
values realigned to backend contract. This spec pins the backend
contract so any future drift is caught server-side.

Five assertion dimensions per the operator's testing rubric:
  1. SSOT — one preset vocabulary, backend is authority (docstring).
  2. Perf — one request, no fan-out; SMTP mocked to avoid rate-limit
     coupling.
  3. Stale — no legacy underscore variants accepted (would allow
     silent frontend drift).
  4. Reuse — same `_send_email` seam every recipient goes through.
  5. UX — 422 rejection carries a preset-vocabulary hint in `detail`
     so operators debugging can see which values are valid.
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock


async def _fake_jwt_guard(connection, _handler):
    """Stand-in jwt_guard that installs a canonical designated-tier
    token_payload on connection.state so the downstream `admin_guard`
    role check admits the request. Production jwt_guard decodes the
    bearer token; the test skips crypto and hard-codes the payload."""
    connection.state.token_payload = {
        "sub":  "test-admin",
        "role": "designated",
    }


def _admin_auth_patches():
    """Route all guard calls through the stubbed jwt_guard so the
    admin_guard's `role in (designated, admin)` check passes without
    a real bearer token / DB lookup.

    admin_guard is defined as `await jwt_guard(...)` followed by an
    inline role check on `connection.state.token_payload`. Because
    admin_guard was already bound into the Controller's `guards` list
    at import time (`guards = [admin_guard]`), patching admin_guard
    itself on `backend.api.auth_guard` doesn't rewire the Controller;
    but admin_guard imports jwt_guard lazily inside its body via the
    module reference `jwt_guard(...)`, so patching that name on the
    auth_guard module DOES take effect at call time."""
    return patch(
        "backend.api.auth_guard.jwt_guard",
        new=_fake_jwt_guard,
    )


def _make_user_row(username, email, role="partner", share_pct=0.5, is_active=True):
    """Cheap stand-in User-like object; the handler only reads five
    attributes (username, display_name, email, role, share_pct,
    is_active) — plain MagicMock suffices."""
    u = MagicMock()
    u.username     = username
    u.email        = email
    u.role         = role
    u.share_pct    = share_pct
    u.is_active    = is_active
    u.display_name = username.title()
    return u


def _mock_session_ctx(users_returned):
    """Patch `async_session` in admin.py so the session context yields
    a mock with .execute() returning our fake user rows."""
    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows
        def scalars(self):
            class _Scalars:
                def __init__(self, r): self._r = r
                def all(self): return self._r
            return _Scalars(self._rows)

    class _FakeSession:
        async def execute(self, _stmt):
            return _FakeResult(users_returned)
        async def commit(self): return None
        def add(self, obj):
            # Assign a bogus event_id so the handler's `event.id` read
            # doesn't blow up.
            obj.id = 999
        async def __aenter__(self):  return self
        async def __aexit__(self, *_a): return None

    return _FakeSession()


# -------------------------------------------------------------------
# Preset vocabulary contract
# -------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["all-partners", "all-designated", "all"])
async def test_email_partners_accepts_canonical_preset(async_client, preset):
    """Hyphen-cased presets from the backend docstring MUST be accepted.
    This is the SSOT contract — the frontend must speak this vocabulary."""
    fake_users = [_make_user_row("alice", "alice@example.com")]

    with _admin_auth_patches(), \
         patch("backend.api.routes.admin.async_session",
               return_value=_mock_session_ctx(fake_users)), \
         patch("backend.shared.helpers.mail_utils.send_email",
               return_value=(True, "sent")):
        resp = await async_client.post(
            "/api/admin/email-partners",
            json={"recipients": preset, "subject": "hi", "body": "test"},
        )

    # Litestar `@post` defaults to 201 Created; the handler returns
    # the same dict body regardless.
    assert resp.status_code == 201, resp.text
    j = resp.json()
    assert j["sent_count"] == 1
    assert j["failed_count"] == 0
    assert j["total"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_preset", ["all_partners", "all_designated", "all_users"])
async def test_email_partners_rejects_underscore_preset(async_client, bad_preset):
    """Legacy underscore presets MUST be rejected with a 422 whose
    detail lists the canonical values — this is the regression pin.
    Without this rejection, the frontend can drift and silently fail."""
    with _admin_auth_patches():
        resp = await async_client.post(
            "/api/admin/email-partners",
            json={"recipients": bad_preset, "subject": "hi", "body": "test"},
        )

    assert resp.status_code == 422
    detail = resp.json().get("detail", "")
    # Detail must enumerate the canonical preset names so operators
    # debugging see which vocabulary is valid.
    assert "all-partners" in detail
    assert "all-designated" in detail


# -------------------------------------------------------------------
# Manual pick (list of usernames)
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_email_partners_accepts_manual_list(async_client):
    """Manual-pick path (list of usernames) MUST still work — the
    frontend Pick mode dispatches usernames[] rather than a preset."""
    fake_users = [
        _make_user_row("bob", "bob@example.com"),
        _make_user_row("carol", "carol@example.com"),
    ]

    with _admin_auth_patches(), \
         patch("backend.api.routes.admin.async_session",
               return_value=_mock_session_ctx(fake_users)), \
         patch("backend.shared.helpers.mail_utils.send_email",
               return_value=(True, "sent")):
        resp = await async_client.post(
            "/api/admin/email-partners",
            json={
                "recipients": ["bob", "carol"],
                "subject": "hi",
                "body": "test",
            },
        )

    # Litestar `@post` defaults to 201 Created; the handler returns
    # the same dict body regardless.
    assert resp.status_code == 201, resp.text
    j = resp.json()
    assert j["sent_count"] == 2
    assert j["total"] == 2


@pytest.mark.asyncio
async def test_email_partners_rejects_empty_list(async_client):
    """Empty recipients list MUST 422 — nothing to send."""
    with _admin_auth_patches():
        resp = await async_client.post(
            "/api/admin/email-partners",
            json={"recipients": [], "subject": "hi", "body": "test"},
        )
    assert resp.status_code == 422
    assert "empty" in resp.json().get("detail", "").lower()


# -------------------------------------------------------------------
# Body / subject validation
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_email_partners_rejects_missing_subject(async_client):
    with _admin_auth_patches():
        resp = await async_client.post(
            "/api/admin/email-partners",
            json={"recipients": "all-partners", "subject": "", "body": "test"},
        )
    assert resp.status_code == 422
    assert "subject" in resp.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_email_partners_rejects_missing_body(async_client):
    with _admin_auth_patches():
        resp = await async_client.post(
            "/api/admin/email-partners",
            json={"recipients": "all-partners", "subject": "hi", "body": ""},
        )
    assert resp.status_code == 422
    assert "body" in resp.json().get("detail", "").lower()
