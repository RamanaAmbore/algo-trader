"""Tests for the MCP server's token provider (backend/mcp/kite_server.py).

Covers: static RAMBOQ_TOKEN passthrough, env / secrets credential lookup,
login caching, pre-expiry refresh, single retry on 401, no retry loop,
missing-credential error, serialised concurrent login, and log hygiene.

The HTTP layer is mocked with httpx.MockTransport and the clock is pinned
through kite_server._now. No real server is contacted.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import jwt
import pytest

import backend.mcp.kite_server as kite_server
import backend.shared.helpers.utils as utils_mod


_REAL_ASYNC_CLIENT = httpx.AsyncClient
_USER = "ops-user"
_PASS = "hunter2-very-secret-pw"


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 1_900_000_000.0}
    monkeypatch.setattr(kite_server, "_now", lambda: state["now"])
    return state


@pytest.fixture
def no_creds(monkeypatch):
    """Clean env + empty secrets.yaml fallback. Tests opt in to creds explicitly."""
    monkeypatch.delenv("RAMBOQ_TOKEN", raising=False)
    monkeypatch.delenv("RAMBOQ_USER", raising=False)
    monkeypatch.delenv("RAMBOQ_PASS", raising=False)
    monkeypatch.setattr(utils_mod, "secrets", {})
    monkeypatch.setattr(kite_server, "_BASE", "http://ramboq.test")
    monkeypatch.setattr(kite_server, "_provider", kite_server._TokenProvider())


@pytest.fixture
def api(monkeypatch, clock, no_creds):
    """Fake RamboQuant API. Login issues a JWT whose exp = now + ttl.
    `api_statuses` is a queue of status codes for non-login requests."""
    state = {
        "ttl": 3600,
        "issued": 0,
        "login_bodies": [],
        "api_auth_headers": [],
        "api_statuses": [],
        "revoked": set(),  # jti values the fake server 401s
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            state["login_bodies"].append(json.loads(request.content))
            state["issued"] += 1
            token = jwt.encode(
                {"exp": int(clock["now"] + state["ttl"]), "jti": state["issued"]},
                "test-signing-key",
                algorithm="HS256",
            )
            return httpx.Response(200, json={"access_token": token, "token_type": "bearer"})
        auth = request.headers.get("authorization")
        state["api_auth_headers"].append(auth)
        if auth and _jti(auth.removeprefix("Bearer ")) in state["revoked"]:
            return httpx.Response(401, json={"detail": "revoked"})
        code = state["api_statuses"].pop(0) if state["api_statuses"] else 200
        body = [{"tradingsymbol": "RELIANCE"}] if code == 200 else {"detail": "x"}
        return httpx.Response(code, json=body)

    def factory(**kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(kite_server.httpx, "AsyncClient", factory)
    return state


def _logins(api_state: dict) -> int:
    return len(api_state["login_bodies"])


def _jti(token: str) -> int | None:
    """jti of a fake-issued JWT; None for a non-JWT (e.g. a static RAMBOQ_TOKEN)."""
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("jti")
    except jwt.PyJWTError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# RAMBOQ_TOKEN static path
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_env_token_used_without_login(monkeypatch, api):
    monkeypatch.setenv("RAMBOQ_TOKEN", "env-static-token-123")
    rows = await kite_server._get("/api/positions/")
    assert rows == [{"tradingsymbol": "RELIANCE"}]
    assert _logins(api) == 0
    assert api["api_auth_headers"] == ["Bearer env-static-token-123"]


@pytest.mark.asyncio
async def test_env_token_401_is_not_retried_with_login(monkeypatch, api):
    monkeypatch.setenv("RAMBOQ_TOKEN", "env-static-token-123")
    api["api_statuses"] = [401]
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await kite_server._get("/api/positions/")
    assert exc.value.response.status_code == 401
    assert _logins(api) == 0
    assert len(api["api_auth_headers"]) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Credential login and cache
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_login_once_then_token_cached(monkeypatch, api):
    monkeypatch.setenv("RAMBOQ_USER", _USER)
    monkeypatch.setenv("RAMBOQ_PASS", _PASS)

    await kite_server._get("/api/positions/")
    await kite_server._get("/api/holdings/")

    assert _logins(api) == 1
    assert api["login_bodies"] == [{"username": _USER, "password": _PASS}]
    assert len(api["api_auth_headers"]) == 2
    assert api["api_auth_headers"][0] == api["api_auth_headers"][1]
    assert api["api_auth_headers"][0].startswith("Bearer ")


@pytest.mark.asyncio
async def test_post_helper_uses_provider_too(monkeypatch, api):
    monkeypatch.setenv("RAMBOQ_USER", _USER)
    monkeypatch.setenv("RAMBOQ_PASS", _PASS)
    await kite_server._post("/api/quote/batch", {"keys": []})
    assert _logins(api) == 1
    assert api["api_auth_headers"][0].startswith("Bearer ")


@pytest.mark.asyncio
async def test_secrets_yaml_fallback_when_env_unset(monkeypatch, api):
    monkeypatch.setattr(utils_mod, "secrets", {"admin_username": "adm", "admin_password": "apw-secret"})
    await kite_server._get("/api/positions/")
    assert api["login_bodies"] == [{"username": "adm", "password": "apw-secret"}]


@pytest.mark.asyncio
async def test_env_overrides_secrets_per_field(monkeypatch, api):
    monkeypatch.setattr(utils_mod, "secrets", {"admin_username": "adm", "admin_password": "apw-secret"})
    monkeypatch.setenv("RAMBOQ_USER", "env-user")
    await kite_server._get("/api/positions/")
    assert api["login_bodies"] == [{"username": "env-user", "password": "apw-secret"}]


@pytest.mark.asyncio
async def test_refresh_when_under_five_minutes_remain(monkeypatch, api, clock):
    monkeypatch.setenv("RAMBOQ_USER", _USER)
    monkeypatch.setenv("RAMBOQ_PASS", _PASS)
    api["ttl"] = 3600

    await kite_server._get("/api/positions/")
    assert _logins(api) == 1

    # 10 minutes before expiry: still fresh, no login.
    clock["now"] += 3600 - 600
    await kite_server._get("/api/positions/")
    assert _logins(api) == 1

    # 4 minutes before expiry: inside the 5-minute margin, so log in again.
    clock["now"] += 600 - 240
    await kite_server._get("/api/positions/")
    assert _logins(api) == 2
    assert api["api_auth_headers"][-1] != api["api_auth_headers"][0]


# ═══════════════════════════════════════════════════════════════════════════
# 401 handling
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_401_then_retry_succeeds(monkeypatch, api):
    monkeypatch.setenv("RAMBOQ_USER", _USER)
    monkeypatch.setenv("RAMBOQ_PASS", _PASS)
    api["api_statuses"] = [401, 200]

    rows = await kite_server._get("/api/positions/")

    assert rows == [{"tradingsymbol": "RELIANCE"}]
    # Initial login, then one forced login after the 401.
    assert _logins(api) == 2
    # Two tool requests: the rejected one, then the retry with the new token.
    assert len(api["api_auth_headers"]) == 2
    assert api["api_auth_headers"][0] != api["api_auth_headers"][1]


@pytest.mark.asyncio
async def test_401_on_retry_returned_to_caller_without_loop(monkeypatch, api):
    monkeypatch.setenv("RAMBOQ_USER", _USER)
    monkeypatch.setenv("RAMBOQ_PASS", _PASS)
    api["api_statuses"] = [401, 401, 401, 401]

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await kite_server._get("/api/positions/")

    assert exc.value.response.status_code == 401
    assert len(api["api_auth_headers"]) == 2  # original + exactly one retry
    assert _logins(api) == 2


@pytest.mark.asyncio
async def test_concurrent_401s_share_one_forced_login(monkeypatch, api):
    monkeypatch.setenv("RAMBOQ_USER", _USER)
    monkeypatch.setenv("RAMBOQ_PASS", _PASS)
    # Warm the cache, then the server revokes that token. Both concurrent calls
    # start on the stale token; whichever 401s first forces one login, and the
    # other caller must reuse the replacement rather than logging in again.
    await kite_server._get("/api/positions/")
    assert _logins(api) == 1
    api["revoked"].add(1)

    results = await asyncio.gather(kite_server._get("/api/a/"), kite_server._get("/api/b/"))

    assert results == [[{"tradingsymbol": "RELIANCE"}]] * 2
    assert _logins(api) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Concurrency
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_concurrent_first_calls_log_in_once(monkeypatch, api):
    monkeypatch.setenv("RAMBOQ_USER", _USER)
    monkeypatch.setenv("RAMBOQ_PASS", _PASS)
    await asyncio.gather(*(kite_server._get("/api/positions/") for _ in range(5)))
    assert _logins(api) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Errors and log hygiene
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_credentials_names_the_variables(api):
    with pytest.raises(RuntimeError) as exc:
        await kite_server._get("/api/positions/")
    msg = str(exc.value)
    assert "RAMBOQ_USER" in msg
    assert "RAMBOQ_PASS" in msg
    assert "RAMBOQ_TOKEN" in msg
    assert _logins(api) == 0
    assert api["api_auth_headers"] == []


@pytest.mark.asyncio
async def test_missing_password_only_still_errors(monkeypatch, api):
    monkeypatch.setenv("RAMBOQ_USER", _USER)
    with pytest.raises(RuntimeError, match="RAMBOQ_PASS"):
        await kite_server._get("/api/positions/")
    assert _logins(api) == 0


@pytest.mark.asyncio
async def test_login_failure_message_omits_password(monkeypatch, api):
    monkeypatch.setenv("RAMBOQ_USER", _USER)
    monkeypatch.setenv("RAMBOQ_PASS", _PASS)

    def bad_login(request):
        return httpx.Response(401, json={"detail": "invalid"})

    monkeypatch.setattr(
        kite_server.httpx,
        "AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(bad_login), **kw),
    )
    with pytest.raises(RuntimeError) as exc:
        await kite_server._get("/api/positions/")
    assert _PASS not in str(exc.value)
    assert "HTTP 401" in str(exc.value)


@pytest.mark.asyncio
async def test_malformed_token_message_omits_token(monkeypatch, api):
    monkeypatch.setenv("RAMBOQ_USER", _USER)
    monkeypatch.setenv("RAMBOQ_PASS", _PASS)
    leaked = "not-a-jwt-LEAKME-998877"

    def weird_login(request):
        return httpx.Response(200, json={"access_token": leaked})

    monkeypatch.setattr(
        kite_server.httpx,
        "AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(weird_login), **kw),
    )
    with pytest.raises(RuntimeError) as exc:
        await kite_server._get("/api/positions/")
    assert leaked not in str(exc.value)
    assert "malformed" in str(exc.value)


@pytest.mark.asyncio
async def test_logs_contain_neither_password_nor_token(monkeypatch, api, caplog):
    monkeypatch.setenv("RAMBOQ_USER", _USER)
    monkeypatch.setenv("RAMBOQ_PASS", _PASS)
    api["api_statuses"] = [401, 200]
    caplog.set_level(logging.DEBUG)

    await kite_server._get("/api/positions/")

    issued = kite_server._provider.cached
    assert issued, "a token should be cached after login"
    assert _logins(api) == 2
    assert _PASS not in caplog.text
    assert issued not in caplog.text
    assert _USER in caplog.text  # username is allowed in logs
    assert "token expires" in caplog.text


@pytest.mark.asyncio
async def test_get_server_info_does_not_login(monkeypatch, api):
    monkeypatch.setenv("RAMBOQ_USER", _USER)
    monkeypatch.setenv("RAMBOQ_PASS", _PASS)
    info = await kite_server.get_server_info()
    assert info["has_token"] is False
    assert info["token_prefix"] == ""
    assert _logins(api) == 0
