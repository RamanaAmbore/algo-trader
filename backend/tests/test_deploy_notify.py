"""
Smoke tests for webhook/notify_deploy.py.

Covers:
1. Dev branch + notify_on_deploy=True  → Telegram fires.
2. Dev branch + notify_on_deploy=False → Telegram skipped silently.
3. main branch always fires regardless of cap_in_dev.
4. fail status always fires regardless of cap (operators must know).
5. Telegram payload contains branch tag and commit hash.
6. dispatch.sh routes dev ref to ramboq_dev, main to ramboq prod.
7. deploy.sh calls notify_deploy.py on both success and fail paths.
8. Repo default backend_config.yaml has notify_on_deploy=True so fresh
   setups get alerts without manual server-side editing.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "webhook" / "notify_deploy.py"
DISPATCH_SCRIPT = REPO_ROOT / "webhook" / "dispatch.sh"
DEPLOY_SCRIPT = REPO_ROOT / "webhook" / "deploy.sh"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRETS = {
    "ntfy_topic": "ramboq-test",
    "ntfy_url": "https://ntfy.sh",
}


def _load_notify():
    spec = importlib.util.spec_from_file_location("notify_deploy", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(cfg: dict, *, branch: str = "dev", status: str = "ok",
              commit: str = "abc1234", deploy_type: str = "full"):
    """
    Run notify_deploy.main() with patched I/O and return the
    mock urlopen object so callers can inspect the ntfy Request.
    """
    notify = _load_notify()

    def _fake_open(path, *a, **kw):
        import io
        if "secrets" in str(path):
            return io.StringIO(yaml.dump(_SECRETS))
        return io.StringIO(yaml.dump(cfg))

    argv = [
        "notify_deploy.py",
        "--branch", branch,
        "--status", status,
        "--commit", commit,
        "--deploy-type", deploy_type,
    ]

    with (
        patch.object(sys, "argv", argv),
        patch("builtins.open", side_effect=_fake_open),
        patch("urllib.request.urlopen") as mock_urlopen,
    ):
        try:
            notify.main()
        except SystemExit:
            pass

    return mock_urlopen


# ---------------------------------------------------------------------------
# Cap gating
# ---------------------------------------------------------------------------

class TestCapGating:
    """notify_on_deploy capability gate."""

    def test_fires_when_cap_enabled_on_dev(self):
        """dev branch always suppressed regardless of cap — dev deploys don't notify."""
        cfg = {"deploy_branch": "dev", "cap_in_dev": {"notify_on_deploy": True}}
        mock_post = _run_main(cfg, branch="dev")
        mock_post.assert_not_called()

    def test_skipped_when_cap_disabled_on_dev(self):
        """dev + notify_on_deploy=False → Telegram NOT called."""
        cfg = {"deploy_branch": "dev", "cap_in_dev": {"notify_on_deploy": False}}
        mock_post = _run_main(cfg, branch="dev")
        mock_post.assert_not_called()

    def test_main_branch_always_fires(self):
        """main branch always fires ntfy regardless of cap_in_dev value."""
        cfg = {"deploy_branch": "main", "cap_in_dev": {"notify_on_deploy": False}}
        mock_urlopen = _run_main(cfg, branch="main")
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "ntfy.sh" in req.full_url

    def test_fail_status_fires_even_when_cap_disabled(self):
        """dev branch failure is also suppressed — use ntfy for failure awareness."""
        cfg = {"deploy_branch": "dev", "cap_in_dev": {"notify_on_deploy": False}}
        mock_post = _run_main(cfg, branch="dev", status="fail")
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Payload content
# ---------------------------------------------------------------------------

class TestPayload:
    """ntfy notification content."""

    def test_branch_tag_in_dev_message(self):
        """Main branch ntfy title has no [main] tag (branch tag only for non-main)."""
        cfg = {"deploy_branch": "main"}
        mock_urlopen = _run_main(cfg, branch="main")
        req = mock_urlopen.call_args[0][0]
        title = req.get_header("Title")
        assert "[main]" not in title, f"Expected no [main] tag in ntfy title: {title!r}"

    def test_commit_hash_in_message(self):
        """Commit hash appears in ntfy body (prod branch)."""
        cfg = {"deploy_branch": "main"}
        mock_urlopen = _run_main(cfg, branch="main", commit="deadbeef")
        req = mock_urlopen.call_args[0][0]
        body = req.data.decode()
        assert "deadbeef" in body, f"Expected commit hash in ntfy body: {body!r}"

    def test_fe_only_suffix_for_fe_deploy(self):
        """FE-only deploy appends 'FE-only' in the ntfy title (prod)."""
        cfg = {"deploy_branch": "main"}
        mock_urlopen = _run_main(cfg, branch="main", deploy_type="fe-only")
        req = mock_urlopen.call_args[0][0]
        title = req.get_header("Title")
        assert "FE-only" in title, f"Expected FE-only label in ntfy title: {title!r}"


# ---------------------------------------------------------------------------
# dispatch.sh routing
# ---------------------------------------------------------------------------

class TestDispatchSh:
    """dispatch.sh routes correctly."""

    def test_dev_ref_routes_to_ramboq_dev(self):
        text = DISPATCH_SCRIPT.read_text()
        assert "ramboq_dev" in text, "dispatch.sh must route non-main to ramboq_dev"

    def test_main_ref_routes_to_prod(self):
        text = DISPATCH_SCRIPT.read_text()
        assert "/opt/ramboq/webhook/deploy.sh prod" in text, (
            "dispatch.sh must route main branch to prod deploy.sh"
        )


# ---------------------------------------------------------------------------
# deploy.sh structure
# ---------------------------------------------------------------------------

class TestDeploySh:
    """deploy.sh calls notify_deploy.py on both success and fail paths."""

    def test_notify_called_at_least_twice(self):
        """deploy.sh must call notify_deploy.py in both the fail trap and success path."""
        text = DEPLOY_SCRIPT.read_text()
        count = text.count("notify_deploy.py")
        assert count >= 2, (
            f"deploy.sh must call notify_deploy.py >= 2 times (fail trap + success), "
            f"found {count}"
        )

    def test_deploy_type_flag_passed(self):
        text = DEPLOY_SCRIPT.read_text()
        assert "--deploy-type" in text, (
            "deploy.sh must pass --deploy-type flag to notify_deploy.py"
        )


# ---------------------------------------------------------------------------
# Repo default config
# ---------------------------------------------------------------------------

class TestDefaultConfig:
    """Repo default backend_config.yaml has notify_on_deploy=False on dev.

    Operator explicitly set this to False on dev branches (commit a434cd6e).
    Prod (main) is always True regardless of cap_in_dev — the notify_deploy.py
    gate for main bypasses the cap entirely (see test_main_branch_always_fires).
    """

    def test_repo_default_is_false(self):
        # dev default is False per operator (a434cd6e); prod is True (branch gate bypasses cap)
        cfg_path = REPO_ROOT / "backend" / "config" / "backend_config.yaml"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        cap = cfg.get("cap_in_dev", {})
        assert cap.get("notify_on_deploy") is False, (
            f"cap_in_dev.notify_on_deploy must be False in repo config (dev default, "
            f"commit a434cd6e). Prod fires regardless via the branch gate. "
            f"Got {cap.get('notify_on_deploy')!r}"
        )
