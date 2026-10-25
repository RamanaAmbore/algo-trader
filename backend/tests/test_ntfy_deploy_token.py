"""
2026-10 operator requirement #4 (deploy/code-deployment alerts never go
to ntfy or email): the ntfy send block was REMOVED from
`webhook/notify_deploy.py` entirely, not just gated. This file used to
assert the ntfy Authorization-header/URL contract; it now asserts the
inverse — `urllib.request.urlopen` (the ntfy transport) is never called
at all, even when `ntfy_topic`/`ntfy_token` are fully configured in
secrets. Kept as its own file (not deleted) since it's still the right
regression home for "does notify_deploy.py ever talk to ntfy" — only the
expected answer flipped.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    def test_ntfy_never_called_even_with_token_configured(self):
        """ntfy_topic + ntfy_token both present → urlopen (ntfy's
        transport) is still never called — the whole block is gone."""
        mock_urlopen = _run_with_ntfy({
            "ntfy_topic": "ramboq_alerts",
            "ntfy_token": "my-secret-token",
        })
        mock_urlopen.assert_not_called()

    def test_ntfy_never_called_without_token(self):
        mock_urlopen = _run_with_ntfy({
            "ntfy_topic": "ramboq_alerts",
            # ntfy_token not set
        })
        mock_urlopen.assert_not_called()

    def test_no_ntfy_call_when_no_topic(self):
        """ntfy_topic absent → urllib.request.urlopen not called either."""
        mock_urlopen = _run_with_ntfy({
            "ntfy_token": "some-token",
            # ntfy_topic not set
        })
        mock_urlopen.assert_not_called()

    def test_ntfy_never_called_regardless_of_ntfy_url(self):
        mock_urlopen = _run_with_ntfy({
            "ntfy_topic": "ramboq_alerts",
            "ntfy_url": "https://ntfy.sh",
        })
        mock_urlopen.assert_not_called()
