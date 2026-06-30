"""
persistence_mode.py — Flip the persistence runtime mode from the shell.

Connects to the running API process via HTTP and POSTs the admin endpoint.
Authentication uses a service token from secrets.yaml (preferred) or falls
back to username + password login.

Usage:
    ./venv/bin/python scripts/persistence_mode.py off|soft|hard|status

Setup (one-time, on server):
    Add to /opt/ramboq/backend/config/secrets.yaml:

        service_admin_token: <paste a long-lived admin JWT here>

    Mint the token by logging in as admin on the web UI and copying the
    access_token from the browser's localStorage, or by running:

        ./venv/bin/python scripts/manage.py generate-service-token

    The token is a standard 24-h JWT.  For an always-valid service token,
    mint via the web UI and copy; the script will log a warning when the
    token is within 2h of expiry so you know to refresh it.

    Alternatively store admin_username + admin_password in secrets.yaml and
    the script will log in and fetch a fresh JWT on every call.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Constants ──────────────────────────────────────────────────────────────

_VALID_ARGS = ("off", "soft", "hard", "status")
_API_PORT_PROD = 8502
_API_PORT_DEV  = 8503


def _load_secrets() -> dict:
    try:
        import yaml
    except ImportError:
        _die("PyYAML not installed — run: pip install pyyaml")
    secrets_path = ROOT / "backend" / "config" / "secrets.yaml"
    if not secrets_path.exists():
        _die(f"secrets.yaml not found at {secrets_path}")
    with open(secrets_path) as f:
        return yaml.safe_load(f) or {}


def _die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _warn(msg: str) -> None:
    print(f"WARN:  {msg}", file=sys.stderr)


def _detect_api_base() -> str:
    """Pick the right port: prod (8502) if on the main branch, dev (8503) otherwise."""
    import subprocess
    try:
        branch = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        port = _API_PORT_PROD if branch == "main" else _API_PORT_DEV
    except Exception:
        port = _API_PORT_PROD
    return f"http://127.0.0.1:{port}"


def _jwt_expiry_remaining(token: str) -> float | None:
    """Return seconds until the JWT expires, or None if undecodable."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        import base64
        payload_b64 = parts[1] + "=="   # re-pad
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if exp is None:
            return None
        return float(exp) - time.time()
    except Exception:
        return None


def _get_token(secrets: dict, base_url: str) -> str:
    """Return an admin JWT, either from secrets or by logging in."""
    import urllib.request
    import urllib.error

    # ── Preferred: pre-minted service token ────────────────────────────
    token = (secrets.get("service_admin_token") or "").strip()
    if token:
        remaining = _jwt_expiry_remaining(token)
        if remaining is not None and remaining < 0:
            _die(
                "service_admin_token in secrets.yaml has expired.\n"
                "  Log in on the web UI, copy the access_token from "
                "localStorage, and update the key."
            )
        if remaining is not None and remaining < 7200:
            _warn(
                f"service_admin_token expires in {remaining/3600:.1f}h — "
                "consider refreshing it soon."
            )
        return token

    # ── Fallback: username + password login ────────────────────────────
    username = (secrets.get("admin_username") or "").strip()
    password = (secrets.get("admin_password") or "").strip()
    if not username or not password:
        _die(
            "No service_admin_token found in secrets.yaml and no "
            "admin_username / admin_password either.\n\n"
            "Add ONE of the following to backend/config/secrets.yaml "
            "on the server:\n\n"
            "  # Option A — long-lived JWT (preferred):\n"
            "  service_admin_token: <paste JWT here>\n\n"
            "  # Option B — credentials for auto-login:\n"
            "  admin_username: <your admin username>\n"
            "  admin_password: <your admin password>"
        )

    login_url = f"{base_url}/api/auth/login"
    body = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        login_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        token = data.get("access_token", "")
        if not token:
            _die(f"Login succeeded but no access_token in response: {data}")
        return token
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")[:300]
        _die(f"Login failed (HTTP {e.code}): {body_text}")
    except Exception as e:
        _die(f"Login request failed: {e}")


def _call_api(method: str, url: str, token: str) -> dict:
    """Fire a GET or POST to the admin endpoint and return the JSON body."""
    import urllib.request
    import urllib.error

    req = urllib.request.Request(
        url,
        data=b"" if method == "POST" else None,
        headers={"Authorization": f"Bearer {token}"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")[:400]
        _die(f"API call failed (HTTP {e.code}): {body_text}")
    except Exception as e:
        _die(f"API call failed: {e}")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in _VALID_ARGS:
        print(
            "Usage: persistence_mode.py off|soft|hard|status\n\n"
            "  status — print current mode\n"
            "  off    — normal cache → DB → broker hierarchy\n"
            "  soft   — bypass cache+DB, pull from broker, write-back heals tiers\n"
            "  hard   — soft + ticker recycle (rebuilds WebSocket subscriptions)\n",
            file=sys.stderr,
        )
        sys.exit(1)

    arg = sys.argv[1]
    secrets  = _load_secrets()
    base_url = _detect_api_base()
    token    = _get_token(secrets, base_url)

    if arg == "status":
        result = _call_api("GET", f"{base_url}/api/admin/persistence/mode", token)
        print(f"persistence mode: {result.get('mode', '?')}")
    else:
        result = _call_api("POST", f"{base_url}/api/admin/persistence/mode/{arg}", token)
        print(f"persistence mode set to: {result.get('mode', '?')}")


if __name__ == "__main__":
    main()
