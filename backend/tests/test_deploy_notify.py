"""
Regression guard for webhook/notify_deploy.py dev-branch gate.

Operator confirmation: dev branch Telegram alerts must NOT fire when
cap_in_dev.notify_on_deploy is False. Prod (main) must always fire.

Three cases:
  1. dev  + notify_on_deploy=False  → Telegram NOT called (silent success)
  2. main                           → Telegram called (prod path unaffected)
  3. dev  + notify_on_deploy=True   → Telegram called (operator can re-enable)

The script is invoked via its main() entry point to exercise the real
argument-parsing + gating logic rather than internal helpers.

Note: notify_deploy.py is in webhook/ (not a package) so we import it
via importlib from its file path. subprocess.run for systemctl is patched
so tests don't require a systemd environment.
"""

import importlib.util
import sys
import types
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the script as a module without executing __main__.
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parents[2] / "webhook" / "notify_deploy.py"


def _load_notify():
    spec = importlib.util.spec_from_file_location("notify_deploy", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Fake YAML payloads used across tests
_BASE_CFG = {
    "deploy_branch": "dev",
    "cap_in_dev": {
        "notify_on_deploy": False,
    },
}
_MAIN_CFG = {
    "deploy_branch": "main",
    "cap_in_dev": {
        "notify_on_deploy": False,  # Irrelevant on main — gate is bypassed
    },
}
_DEV_ON_CFG = {
    "deploy_branch": "dev",
    "cap_in_dev": {
        "notify_on_deploy": True,
    },
}
_SECRETS = {
    "telegram_bot_token": "fake-token",
    "telegram_chat_id": "123456",
}


def _run_main(cfg: dict, argv_extra: list[str] | None = None,
              mock_post_response: MagicMock | None = None):
    """
    Run notify_deploy.main() with patched I/O and return the
    mock requests.post object so callers can assert call_count / args.
    """
    notify = _load_notify()

    def _fake_open(path, *a, **kw):
        import io
        import yaml
        if "secrets" in str(path):
            return io.StringIO(yaml.dump(_SECRETS))
        return io.StringIO(yaml.dump(cfg))

    resp = mock_post_response or MagicMock()
    resp.ok = True
    resp.status_code = 200

    base_argv = ["notify_deploy.py", "--status", "ok",
                 "--commit", "abc1234", "--deploy-type", "full"]
    if argv_extra:
        base_argv += argv_extra

    with (
        patch.object(sys, "argv", base_argv),
        patch("builtins.open", side_effect=_fake_open),
        patch("subprocess.run", return_value=MagicMock(stdout="active", returncode=0)),
        patch("requests.post", return_value=resp) as mock_post,
    ):
        try:
            notify.main()
        except SystemExit:
            pass

    return mock_post


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_dev_notify_off_no_telegram():
    """Dev branch + notify_on_deploy=False → Telegram is NOT called."""
    mock_post = _run_main(
        cfg=_BASE_CFG,
        argv_extra=["--branch", "dev"],
    )
    mock_post.assert_not_called()


def test_main_branch_always_fires():
    """Main branch → Telegram is called regardless of cap_in_dev."""
    mock_post = _run_main(
        cfg=_MAIN_CFG,
        argv_extra=["--branch", "main"],
    )
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args.kwargs or {}
    call_args = mock_post.call_args.args
    # Verify the message was sent to Telegram API
    url = call_args[0] if call_args else call_kwargs.get("url", "")
    assert "api.telegram.org" in url


def test_dev_notify_on_fires_when_enabled():
    """Dev branch + notify_on_deploy=True → Telegram IS called (operator override)."""
    mock_post = _run_main(
        cfg=_DEV_ON_CFG,
        argv_extra=["--branch", "dev"],
    )
    mock_post.assert_called_once()


def test_dev_fail_always_fires():
    """Dev branch + notify_on_deploy=False + status=fail → Telegram called.

    Failures always surface even on gated branches so the operator knows
    the deploy broke.
    """
    mock_post = _run_main(
        cfg=_BASE_CFG,
        argv_extra=["--branch", "dev", "--status", "fail", "--reason", "exit code 1"],
    )
    mock_post.assert_called_once()
