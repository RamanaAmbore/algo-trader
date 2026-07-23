"""
Unit tests: notify_deploy.py ntfy block sends Authorization: Bearer header
when ntfy_token is present in secrets, and omits it when absent.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "webhook" / "notify_deploy.py"

_BASE_SECRETS = {
    "telegram_bot_token": "fake-tg-token",
    "telegram_chat_id": "123456",
}


def _load_notify():
    spec = importlib.util.spec_from_file_location("notify_deploy", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_with_ntfy(secrets_override: dict, *, branch: str = "main", status: str = "ok"):
    """Run notify_deploy.main() on prod branch and capture the urllib.request.urlopen calls."""
    notify = _load_notify()
    secrets = {**_BASE_SECRETS, **secrets_override}
    cfg = {"deploy_branch": "main"}

    def _fake_open(path, *a, **kw):
        import io
        if "secrets" in str(path):
            return io.StringIO(yaml.dump(secrets))
        return io.StringIO(yaml.dump(cfg))

    tg_resp = MagicMock()
    tg_resp.ok = True
    tg_resp.status_code = 200
    ntfy_resp = MagicMock()

    argv = ["notify_deploy.py", "--branch", branch, "--status", status, "--commit", "abc1234", "--deploy-type", "full"]

    with (
        patch.object(sys, "argv", argv),
        patch("builtins.open", side_effect=_fake_open),
        patch("subprocess.run", return_value=MagicMock(stdout="active", returncode=0)),
        patch("requests.post", return_value=tg_resp),
        patch("urllib.request.urlopen", return_value=ntfy_resp) as mock_urlopen,
    ):
        try:
            notify.main()
        except SystemExit:
            pass

    return mock_urlopen


class TestNtfyDeployToken:
    def test_token_sent_when_configured(self):
        """ntfy_token present → Authorization: Bearer header in deploy notification."""
        mock_urlopen = _run_with_ntfy({
            "ntfy_topic": "ramboq_alerts",
            "ntfy_token": "my-secret-token",
        })
        mock_urlopen.assert_called()
        req = mock_urlopen.call_args[0][0]
        auth = req.headers.get("Authorization")
        assert auth == "Bearer my-secret-token", f"Expected Bearer header, got {auth!r}"

    def test_token_omitted_when_absent(self):
        """ntfy_token absent → no Authorization header."""
        mock_urlopen = _run_with_ntfy({
            "ntfy_topic": "ramboq_alerts",
            # ntfy_token not set
        })
        mock_urlopen.assert_called()
        req = mock_urlopen.call_args[0][0]
        auth = req.headers.get("Authorization")
        assert auth is None, f"Expected no Authorization header, got {auth!r}"

    def test_no_ntfy_call_when_no_topic(self):
        """ntfy_topic absent → urllib.request.urlopen not called for ntfy."""
        mock_urlopen = _run_with_ntfy({
            "ntfy_token": "some-token",
            # ntfy_topic not set
        })
        mock_urlopen.assert_not_called()

    def test_ntfy_url_in_request(self):
        """ntfy request goes to {ntfy_url}/{ntfy_topic}."""
        mock_urlopen = _run_with_ntfy({
            "ntfy_topic": "ramboq_alerts",
            "ntfy_url": "https://ntfy.sh",
        })
        mock_urlopen.assert_called()
        req = mock_urlopen.call_args[0][0]
        assert "ntfy.sh/ramboq_alerts" in req.full_url, f"Unexpected URL: {req.full_url}"
