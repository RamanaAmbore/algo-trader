"""
Tests for AuditMiddleware in backend/api/audit.py.

Verifies:
  - _audit_should_skip() — non-HTTP, non-mutating, suppressed paths
  - _audit_should_write() — 2xx/3xx always, 4xx/5xx only with opt-in flag
  - _audit_extract_actor() — JWT payload extraction + demo role handling
  - _derive_category_from_path() — path-to-category routing
  - AuditMiddleware.handle() — X-Request-ID injection + skipping logic

Six dimensions:
  SSOT    — each function's logic matches the audit.py source.
  Perf    — skip decision is O(1) per scope; category lookup is O(N) prefixes (small set).
  Stale   — grep confirms _MUTATING set, _SUPPRESS_PREFIXES tuple, _PATH_CATEGORY_RULES tuple.
  Reuse   — no new state beyond scope captures; integrates with existing middleware pattern.
  UX      — middleware transparent to handlers (request headers, response headers).
  Error   — missing JWT payload gracefully returns empty actor; no DB failures (fire-and-forget).
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ─── Stale guard: grep for constants ───────────────────────────────────────

def test_mutating_set_includes_post_patch_put_delete():
    import pathlib
    audit_src = pathlib.Path(__file__).parent.parent / "api" / "audit.py"
    text = audit_src.read_text()
    assert "_MUTATING = frozenset({\"POST\", \"PATCH\", \"PUT\", \"DELETE\"})" in text, (
        "audit.py: _MUTATING frozenset must include POST, PATCH, PUT, DELETE"
    )


def test_suppress_prefixes_includes_auth_and_health():
    import pathlib
    audit_src = pathlib.Path(__file__).parent.parent / "api" / "audit.py"
    text = audit_src.read_text()
    assert "/api/health" in text, "audit.py: /api/health must be in _SUPPRESS_PREFIXES"
    assert "/api/auth/login" in text, "audit.py: /api/auth/login must be in _SUPPRESS_PREFIXES"
    assert "/api/auth/refresh" in text, "audit.py: /api/auth/refresh must be in _SUPPRESS_PREFIXES"


def test_path_category_rules_includes_order_and_config():
    import pathlib
    audit_src = pathlib.Path(__file__).parent.parent / "api" / "audit.py"
    text = audit_src.read_text()
    assert "order.place" in text, "audit.py: order.place category must exist"
    assert "order.fill" in text, "audit.py: order.fill category must exist"
    assert "config.broker" in text, "audit.py: config.broker category must exist"


# ─── _audit_should_skip() tests ────────────────────────────────────────────

def test_skip_non_http_scope():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "websocket", "method": "POST", "path": "/api/orders"}
    assert _audit_should_skip(scope) is True


def test_skip_get_method():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "GET", "path": "/api/orders"}
    assert _audit_should_skip(scope) is True


def test_skip_head_method():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "HEAD", "path": "/api/orders"}
    assert _audit_should_skip(scope) is True


def test_skip_options_method():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "OPTIONS", "path": "/api/orders"}
    assert _audit_should_skip(scope) is True


def test_skip_trace_method():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "TRACE", "path": "/api/orders"}
    assert _audit_should_skip(scope) is True


def test_no_skip_post_method():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "POST", "path": "/api/orders"}
    assert _audit_should_skip(scope) is False


def test_no_skip_patch_method():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "PATCH", "path": "/api/orders"}
    assert _audit_should_skip(scope) is False


def test_no_skip_put_method():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "PUT", "path": "/api/orders"}
    assert _audit_should_skip(scope) is False


def test_no_skip_delete_method():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "DELETE", "path": "/api/orders"}
    assert _audit_should_skip(scope) is False


def test_skip_health_endpoint():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "POST", "path": "/api/health"}
    assert _audit_should_skip(scope) is True


def test_skip_health_endpoint_with_trailing_slash():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "POST", "path": "/api/health/"}
    assert _audit_should_skip(scope) is True


def test_skip_auth_login_endpoint():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "POST", "path": "/api/auth/login"}
    assert _audit_should_skip(scope) is True


def test_skip_auth_refresh_endpoint():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "POST", "path": "/api/auth/refresh"}
    assert _audit_should_skip(scope) is True


def test_skip_auth_whoami_endpoint():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "POST", "path": "/api/auth/whoami"}
    assert _audit_should_skip(scope) is True


def test_no_skip_normal_order_post():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "POST", "path": "/api/orders/ticket"}
    assert _audit_should_skip(scope) is False


def test_missing_type_defaults_to_non_http():
    from backend.api.audit import _audit_should_skip
    scope = {"method": "POST", "path": "/api/orders"}
    assert _audit_should_skip(scope) is True


def test_missing_method_defaults_to_non_mutating():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "path": "/api/orders"}
    assert _audit_should_skip(scope) is True


def test_missing_path_treated_as_root():
    from backend.api.audit import _audit_should_skip
    scope = {"type": "http", "method": "POST"}
    # "/" is not in suppress list, so POST / should NOT skip
    assert _audit_should_skip(scope) is False


# ─── _audit_should_write() tests ───────────────────────────────────────────

def test_should_write_200():
    from backend.api.audit import _audit_should_write
    assert _audit_should_write(200) is True


def test_should_write_201():
    from backend.api.audit import _audit_should_write
    assert _audit_should_write(201) is True


def test_should_write_204():
    from backend.api.audit import _audit_should_write
    assert _audit_should_write(204) is True


def test_should_write_302():
    from backend.api.audit import _audit_should_write
    assert _audit_should_write(302) is True


def test_should_write_399():
    from backend.api.audit import _audit_should_write
    assert _audit_should_write(399) is True


def test_should_not_write_1xx():
    from backend.api.audit import _audit_should_write
    assert _audit_should_write(101) is False


def test_should_not_write_100():
    from backend.api.audit import _audit_should_write
    assert _audit_should_write(100) is False


def test_should_not_write_4xx_by_default():
    from backend.api.audit import _audit_should_write
    # 400, 401, 404, 422 should return False when audit.log_failed_mutations is not set
    assert _audit_should_write(400) is False
    assert _audit_should_write(401) is False
    assert _audit_should_write(404) is False
    assert _audit_should_write(422) is False


def test_should_not_write_5xx_by_default():
    from backend.api.audit import _audit_should_write
    # 500, 502, 503 should return False when audit.log_failed_mutations is not set
    assert _audit_should_write(500) is False
    assert _audit_should_write(502) is False
    assert _audit_should_write(503) is False


@patch('backend.shared.helpers.settings.get_bool')
def test_should_write_4xx_when_flag_enabled(mock_get_bool):
    from backend.api.audit import _audit_should_write
    mock_get_bool.return_value = True
    assert _audit_should_write(400) is True


@patch('backend.shared.helpers.settings.get_bool')
def test_should_write_5xx_when_flag_enabled(mock_get_bool):
    from backend.api.audit import _audit_should_write
    mock_get_bool.return_value = True
    assert _audit_should_write(500) is True


@patch('backend.shared.helpers.settings.get_bool', side_effect=Exception("Settings read failed"))
def test_should_write_4xx_fails_closed_on_setting_error(mock_get_bool):
    from backend.api.audit import _audit_should_write
    # When the settings read fails, return False (fail closed)
    assert _audit_should_write(400) is False


# ─── _audit_extract_actor() tests ──────────────────────────────────────────

def test_actor_extraction_from_scope_state():
    from backend.api.audit import _audit_extract_actor
    scope = {
        "state": {
            "token_payload": {
                "user_id": 42,
                "sub": "alice",
                "role": "admin",
            }
        }
    }
    user_id, username, role = _audit_extract_actor(scope)
    assert user_id == 42, f"Expected user_id=42, got {user_id}"
    assert username == "alice", f"Expected username='alice', got {username!r}"
    assert role == "admin", f"Expected role='admin', got {role!r}"


def test_actor_extraction_user_id_as_string():
    from backend.api.audit import _audit_extract_actor
    scope = {
        "state": {
            "token_payload": {
                "user_id": "123",
                "sub": "bob",
                "role": "trader",
            }
        }
    }
    user_id, username, role = _audit_extract_actor(scope)
    assert user_id == 123, f"Expected user_id=123 (converted from string), got {user_id}"
    assert username == "bob"
    assert role == "trader"


def test_actor_extraction_user_id_invalid_converts_to_none():
    from backend.api.audit import _audit_extract_actor
    scope = {
        "state": {
            "token_payload": {
                "user_id": "not_a_number",
                "sub": "charlie",
                "role": "viewer",
            }
        }
    }
    user_id, username, role = _audit_extract_actor(scope)
    assert user_id is None, f"Expected user_id=None for invalid int, got {user_id}"
    assert username == "charlie"
    assert role == "viewer"


def test_actor_extraction_missing_state():
    from backend.api.audit import _audit_extract_actor
    scope = {}
    user_id, username, role = _audit_extract_actor(scope)
    assert user_id is None, f"Expected user_id=None, got {user_id}"
    assert username == "", f"Expected username='', got {username!r}"
    assert role == "", f"Expected role='', got {role!r}"


def test_actor_extraction_none_token_payload():
    from backend.api.audit import _audit_extract_actor
    scope = {"state": {"token_payload": None}}
    user_id, username, role = _audit_extract_actor(scope)
    assert user_id is None
    assert username == ""
    assert role == ""


def test_actor_extraction_missing_token_payload():
    from backend.api.audit import _audit_extract_actor
    scope = {"state": {}}
    user_id, username, role = _audit_extract_actor(scope)
    assert user_id is None
    assert username == ""
    assert role == ""


def test_actor_extraction_demo_role_sets_username():
    from backend.api.audit import _audit_extract_actor
    scope = {
        "state": {
            "token_payload": {
                "user_id": None,
                "sub": "",
                "role": "demo",
            }
        }
    }
    user_id, username, role = _audit_extract_actor(scope)
    assert user_id is None
    assert username == "demo", f"Expected username='demo' for demo role, got {username!r}"
    assert role == "demo"


def test_actor_extraction_missing_user_id():
    from backend.api.audit import _audit_extract_actor
    scope = {
        "state": {
            "token_payload": {
                "sub": "dave",
                "role": "trader",
            }
        }
    }
    user_id, username, role = _audit_extract_actor(scope)
    assert user_id is None
    assert username == "dave"
    assert role == "trader"


def test_actor_extraction_missing_sub():
    from backend.api.audit import _audit_extract_actor
    scope = {
        "state": {
            "token_payload": {
                "user_id": 99,
                "role": "admin",
            }
        }
    }
    user_id, username, role = _audit_extract_actor(scope)
    assert user_id == 99
    assert username == "", f"Expected username='', got {username!r}"
    assert role == "admin"


def test_actor_extraction_missing_role():
    from backend.api.audit import _audit_extract_actor
    scope = {
        "state": {
            "token_payload": {
                "user_id": 88,
                "sub": "eve",
            }
        }
    }
    user_id, username, role = _audit_extract_actor(scope)
    assert user_id == 88
    assert username == "eve"
    assert role == "", f"Expected role='', got {role!r}"


# ─── _derive_category_from_path() tests ────────────────────────────────────

def test_derive_category_order_place_ticket():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/orders/ticket")
    assert category == "order.place", f"Expected 'order.place', got {category!r}"


def test_derive_category_order_place_basket():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/orders/basket")
    assert category == "order.place", f"Expected 'order.place', got {category!r}"


def test_derive_category_order_fill_postback():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/orders/postback")
    assert category == "order.fill", f"Expected 'order.fill', got {category!r}"


def test_derive_category_order_modify_put():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("PUT", "/api/orders/123")
    assert category == "order.modify", f"Expected 'order.modify', got {category!r}"


def test_derive_category_order_modify_patch():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("PATCH", "/api/orders/456")
    assert category == "order.modify", f"Expected 'order.modify', got {category!r}"


def test_derive_category_order_cancel_delete():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("DELETE", "/api/orders/789")
    assert category == "order.cancel", f"Expected 'order.cancel', got {category!r}"


def test_derive_category_order_cancel_delete_with_verb():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("DELETE", "/api/orders/789/cancel")
    assert category == "order.cancel", f"Expected 'order.cancel', got {category!r}"


def test_derive_category_user():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/admin/users/")
    assert category == "user", f"Expected 'user', got {category!r}"


def test_derive_category_user_with_id():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/admin/users/42")
    assert category == "user", f"Expected 'user', got {category!r}"


def test_derive_category_config_broker():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/admin/brokers")
    assert category == "config.broker", f"Expected 'config.broker', got {category!r}"


def test_derive_category_config_broker_with_id():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("PATCH", "/api/admin/brokers/1")
    assert category == "config.broker", f"Expected 'config.broker', got {category!r}"


def test_derive_category_config_settings():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/admin/settings")
    assert category == "config", f"Expected 'config', got {category!r}"


def test_derive_category_config_grammar():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/admin/grammar/reload")
    assert category == "config.grammar", f"Expected 'config.grammar', got {category!r}"


def test_derive_category_config_hedge():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/admin/hedge-proxies")
    assert category == "config.hedge", f"Expected 'config.hedge', got {category!r}"


def test_derive_category_agent():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/agents/")
    assert category == "agent", f"Expected 'agent', got {category!r}"


def test_derive_category_strategy():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/strategies/")
    assert category == "strategy", f"Expected 'strategy', got {category!r}"


def test_derive_category_system_nav():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/nav/compute")
    assert category == "system.nav", f"Expected 'system.nav', got {category!r}"


def test_derive_category_system_statement():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/admin/statements")
    assert category == "system.statement", f"Expected 'system.statement', got {category!r}"


def test_derive_category_http_fallback():
    from backend.api.audit import _derive_category_from_path
    category = _derive_category_from_path("POST", "/api/unknown/path")
    assert category == "http", f"Expected 'http' (fallback), got {category!r}"


def test_derive_category_case_insensitive():
    from backend.api.audit import _derive_category_from_path
    # Path matching should be case-insensitive
    category = _derive_category_from_path("POST", "/API/ORDERS/TICKET")
    assert category == "order.place", f"Expected 'order.place' (case-insensitive), got {category!r}"


def test_derive_category_put_on_generic_admin_path():
    from backend.api.audit import _derive_category_from_path
    # PUT on /api/admin/something/{id} should use the base category
    # This tests the general case where method-based routing doesn't apply
    category = _derive_category_from_path("PUT", "/api/admin/users/42")
    assert category == "user", f"Expected 'user' (no PUT override), got {category!r}"


def test_derive_category_delete_on_admin_path():
    from backend.api.audit import _derive_category_from_path
    # DELETE on /api/admin/users/42 should use the base category (no DELETE override for users)
    category = _derive_category_from_path("DELETE", "/api/admin/users/42")
    assert category == "user", f"Expected 'user' (no DELETE override), got {category!r}"


# ─── AuditMiddleware.handle() tests ────────────────────────────────────────

@pytest.mark.asyncio
async def test_x_request_id_injected_in_response():
    from backend.api.audit import AuditMiddleware

    # Create a simple ASGI app that echoes a response
    async def echo_app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 201,
            "headers": [],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"id": 1}',
        })

    # Instantiate the middleware with the app
    middleware = AuditMiddleware()

    # Build a scope for a POST to /api/orders
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/orders/ticket",
        "headers": [],
        "state": {},
    }

    # Collect sent messages
    sent_messages = []

    async def mock_send(message):
        sent_messages.append(message)

    async def mock_receive():
        return {"type": "http.request", "body": b""}

    # Call the middleware's handle method directly
    await middleware.handle(scope, mock_receive, mock_send, echo_app)

    # Find the http.response.start message
    start_msg = next((m for m in sent_messages if m.get("type") == "http.response.start"), None)
    assert start_msg is not None, "No http.response.start message found"

    # Check for x-request-id header
    headers = start_msg.get("headers", [])
    request_id_header = None
    for name, value in headers:
        if name == b"x-request-id":
            request_id_header = value.decode("ascii")
            break

    assert request_id_header is not None, "x-request-id header not injected"
    assert len(request_id_header) > 0, "x-request-id header is empty"


@pytest.mark.asyncio
async def test_skipped_scope_bypasses_wrapping():
    from backend.api.audit import AuditMiddleware

    # Create a simple ASGI app
    call_count = 0
    async def counting_app(scope, receive, send):
        nonlocal call_count
        call_count += 1
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [],
        })
        await send({
            "type": "http.response.body",
            "body": b"",
        })

    middleware = AuditMiddleware()

    # Build a GET scope (should be skipped)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/orders",
        "headers": [],
        "state": {},
    }

    sent_messages = []

    async def mock_send(message):
        sent_messages.append(message)

    async def mock_receive():
        return {"type": "http.request", "body": b""}

    # Call the middleware's handle method directly
    await middleware.handle(scope, mock_receive, mock_send, counting_app)

    # The app should have been called exactly once (no wrapping)
    assert call_count == 1, f"Expected 1 app call, got {call_count}"

    # Messages should be passed through unchanged
    start_msg = next((m for m in sent_messages if m.get("type") == "http.response.start"), None)
    assert start_msg is not None

    # For skipped paths, x-request-id should NOT be injected
    # (because we bypass the send wrapper entirely)
    headers = start_msg.get("headers", [])
    request_id_header = None
    for name, value in headers:
        if name == b"x-request-id":
            request_id_header = value.decode("ascii")
            break

    assert request_id_header is None, "x-request-id should not be injected for skipped scopes"


@pytest.mark.asyncio
async def test_middleware_preserves_existing_request_id():
    from backend.api.audit import AuditMiddleware

    existing_request_id = "my-custom-id-123"

    async def echo_app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 201,
            "headers": [],
        })
        await send({
            "type": "http.response.body",
            "body": b"",
        })

    middleware = AuditMiddleware()

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/orders",
        "headers": [],
        "state": {
            "request_id": existing_request_id,
        },
    }

    sent_messages = []

    async def mock_send(message):
        sent_messages.append(message)

    async def mock_receive():
        return {"type": "http.request", "body": b""}

    await middleware.handle(scope, mock_receive, mock_send, echo_app)

    # Check that the existing request_id is used
    start_msg = next((m for m in sent_messages if m.get("type") == "http.response.start"), None)
    assert start_msg is not None

    headers = start_msg.get("headers", [])
    request_id_header = None
    for name, value in headers:
        if name == b"x-request-id":
            request_id_header = value.decode("ascii")
            break

    assert request_id_header == existing_request_id, (
        f"Expected existing request_id {existing_request_id!r}, got {request_id_header!r}"
    )


@pytest.mark.asyncio
async def test_middleware_mints_new_request_id_when_missing():
    from backend.api.audit import AuditMiddleware
    import re

    async def echo_app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 201,
            "headers": [],
        })
        await send({
            "type": "http.response.body",
            "body": b"",
        })

    middleware = AuditMiddleware()

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/orders",
        "headers": [],
        "state": {},
    }

    sent_messages = []

    async def mock_send(message):
        sent_messages.append(message)

    async def mock_receive():
        return {"type": "http.request", "body": b""}

    await middleware.handle(scope, mock_receive, mock_send, echo_app)

    # Check that a new UUID was minted
    start_msg = next((m for m in sent_messages if m.get("type") == "http.response.start"), None)
    assert start_msg is not None

    headers = start_msg.get("headers", [])
    request_id_header = None
    for name, value in headers:
        if name == b"x-request-id":
            request_id_header = value.decode("ascii")
            break

    assert request_id_header is not None, "x-request-id header not injected"

    # Verify it looks like a UUID
    uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    assert re.match(uuid_pattern, request_id_header), (
        f"Generated request_id {request_id_header!r} doesn't match UUID pattern"
    )

    # Verify it was stored in scope.state
    assert scope["state"]["request_id"] == request_id_header, (
        "request_id not stored in scope.state"
    )
