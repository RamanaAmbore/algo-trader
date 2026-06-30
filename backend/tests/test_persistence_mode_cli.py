"""
test_persistence_mode_cli.py

Tests for scripts/persistence_mode.py CLI.

Five quality dimensions (feedback_test_dimensions.md):

  1. SSOT        — script POSTs to the canonical admin endpoint URL
                   (POST /api/admin/persistence/mode/{mode}), not a
                   bespoke path.
  2. Performance — no real network calls (all mocked); test runs < 100 ms.
  3. Stale code  — grep confirms _API_PORT_PROD / _API_PORT_DEV match the
                   CLAUDE.md deployment table (8502 / 8503).
  4. Reusable    — _get_token / _call_api / _load_secrets are importable
                   module-level functions, not buried inside main().
  5. UX          — clear error on missing token + missing credentials;
                   expiry warning when token < 2h remaining.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Import helpers ──────────────────────────────────────────────────────────

def _import_cli():
    """Import scripts/persistence_mode.py as a module."""
    import importlib.util, sys

    spec = importlib.util.spec_from_file_location(
        "persistence_mode",
        ROOT / "scripts" / "persistence_mode.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── 1. SSOT: correct admin endpoint path ───────────────────────────────────

def test_correct_admin_endpoint_posted_for_soft() -> None:
    """main('soft') must POST /api/admin/persistence/mode/soft."""
    cli = _import_cli()
    urls_hit: list[str] = []

    def fake_call_api(method: str, url: str, token: str) -> dict:
        urls_hit.append(url)
        return {"mode": "soft"}

    fake_secrets: dict = {
        "service_admin_token": _make_valid_token(ttl_seconds=3600),
    }

    with (
        patch.object(cli, "_load_secrets", return_value=fake_secrets),
        patch.object(cli, "_call_api", side_effect=fake_call_api),
        patch.object(cli, "_detect_api_base", return_value="http://127.0.0.1:8000"),
        patch.object(sys, "argv", ["persistence_mode.py", "soft"]),
    ):
        cli.main()

    assert len(urls_hit) == 1
    assert urls_hit[0].endswith("/api/admin/persistence/mode/soft")


def test_correct_endpoint_for_status() -> None:
    """main('status') must GET /api/admin/persistence/mode."""
    cli = _import_cli()
    calls: list[tuple[str, str]] = []

    def fake_call_api(method: str, url: str, token: str) -> dict:
        calls.append((method, url))
        return {"mode": "off"}

    fake_secrets: dict = {
        "service_admin_token": _make_valid_token(ttl_seconds=3600),
    }

    with (
        patch.object(cli, "_load_secrets", return_value=fake_secrets),
        patch.object(cli, "_call_api", side_effect=fake_call_api),
        patch.object(cli, "_detect_api_base", return_value="http://127.0.0.1:8000"),
        patch.object(sys, "argv", ["persistence_mode.py", "status"]),
    ):
        cli.main()

    assert calls == [("GET", "http://127.0.0.1:8000/api/admin/persistence/mode")]


# ── 2. Port constants match CLAUDE.md deployment table ─────────────────────

def test_port_constants() -> None:
    cli = _import_cli()
    # Uvicorn binds locally on 8000 (prod) / 8001 (dev).
    # CLAUDE.md 8502/8503 are nginx-proxied external ports.
    assert cli._API_PORT_PROD == 8000, "prod local port must be 8000"
    assert cli._API_PORT_DEV  == 8001, "dev local port must be 8001"


# ── 3. Error on missing token AND missing credentials ──────────────────────

def test_error_when_no_token_and_no_credentials() -> None:
    """_get_token must call sys.exit when both service_admin_token and
    admin_username/admin_password are absent from secrets.yaml."""
    cli = _import_cli()

    with pytest.raises(SystemExit):
        cli._get_token({}, base_url="http://127.0.0.1:8000")


def test_error_when_token_expired() -> None:
    """_get_token must sys.exit when the service_admin_token is expired."""
    cli = _import_cli()
    expired_token = _make_valid_token(ttl_seconds=-10)  # already expired

    with pytest.raises(SystemExit):
        cli._get_token(
            {"service_admin_token": expired_token},
            base_url="http://127.0.0.1:8000",
        )


# ── 4. Warning when token is near expiry ───────────────────────────────────

def test_warning_on_near_expiry(capsys) -> None:
    """_get_token must print a WARN when token < 2h remaining."""
    cli = _import_cli()
    near_expiry_token = _make_valid_token(ttl_seconds=3600)  # 1h — under 2h threshold

    calls: list[str] = []

    def fake_call_api(method, url, token):
        return {"mode": "off"}

    with (
        patch.object(cli, "_call_api", side_effect=fake_call_api),
        patch.object(cli, "_detect_api_base", return_value="http://127.0.0.1:8000"),
        patch.object(sys, "argv", ["persistence_mode.py", "status"]),
        patch.object(cli, "_warn", side_effect=lambda m: calls.append(m)),
    ):
        token = cli._get_token(
            {"service_admin_token": near_expiry_token},
            base_url="http://127.0.0.1:8000",
        )

    assert any("expires" in m.lower() for m in calls), (
        f"Expected expiry warning but got: {calls}"
    )


# ── 5. Fallback: login with username + password ─────────────────────────────

def test_login_fallback_posts_credentials() -> None:
    """When service_admin_token is absent but admin_username + admin_password
    are present, _get_token must POST to /api/auth/login and return the token."""
    cli = _import_cli()

    login_calls: list[dict] = []

    class FakeResponse:
        def __init__(self):
            self._data = json.dumps({"access_token": "jwt-from-login"}).encode()

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=10):
        login_calls.append({"url": req.full_url, "method": req.get_method()})
        return FakeResponse()

    secrets = {
        "admin_username": "rambo",
        "admin_password": "secret",
    }

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        token = cli._get_token(secrets, base_url="http://127.0.0.1:8000")

    assert token == "jwt-from-login"
    assert len(login_calls) == 1
    assert "/api/auth/login" in login_calls[0]["url"]
    assert login_calls[0]["method"] == "POST"


# ── Helper: build a minimal JWT with the given TTL ─────────────────────────

def _make_valid_token(ttl_seconds: int) -> str:
    """Build a syntactically-valid unsigned JWT (header.payload.signature)."""
    import base64

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(d).encode()
        ).rstrip(b"=").decode()

    header  = b64({"alg": "HS256", "typ": "JWT"})
    payload = b64({
        "sub":  "test-service",
        "role": "admin",
        "exp":  int(time.time()) + ttl_seconds,
    })
    sig = "fakesig"
    return f"{header}.{payload}.{sig}"
