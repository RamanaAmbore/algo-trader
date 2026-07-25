"""
Integration test: ntfy deploy receipt round-trip.

Simulates webhook/notify_deploy.py → ntfy POST and monitors receipt via polling API.
Skips automatically when ntfy_topic is not configured in secrets.yaml.

Run explicitly:
    venv/bin/pytest backend/tests/test_ntfy_deploy_receipt.py -v -m integration
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Optional

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_SECRETS_PATH = REPO_ROOT / "backend" / "config" / "secrets.yaml"


@pytest.fixture(scope="module")
def ntfy_cfg() -> dict:
    """Load ntfy config from secrets.yaml; skip if not configured."""
    if not _SECRETS_PATH.exists():
        pytest.skip("secrets.yaml not found")
    with open(_SECRETS_PATH) as f:
        sec = yaml.safe_load(f) or {}
    topic = sec.get("ntfy_topic")
    if not topic:
        pytest.skip("ntfy_topic not configured in secrets.yaml — skipping live ntfy tests")
    return {
        "topic": topic,
        "url": sec.get("ntfy_url", "https://ntfy.sh").rstrip("/"),
        "token": sec.get("ntfy_token"),
    }


def _post_ntfy(cfg: dict, title: str, body: str) -> None:
    """POST a notification to ntfy using urllib.request (same path as notify_deploy.py)."""
    url = f"{cfg['url']}/{cfg['topic']}"
    headers = {
        "Title": title,
        "Tags": "test",
        "Priority": "default",
        "Content-Type": "text/plain",
    }
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10):
        pass


def _poll_ntfy(cfg: dict, since_seconds: int = 15) -> list[dict]:
    """Poll ntfy GET /topic/json?poll=1&since=Xs; return parsed messages."""
    url = f"{cfg['url']}/{cfg['topic']}/json?poll=1&since={since_seconds}s"
    headers = {"Accept": "application/x-ndjson"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
    msgs = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return msgs


# ─────────────────────────────────────────────────────────────────────────────
# Deploy receipt tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestNtfyDeployReceipt:
    """ntfy deploy notification round-trip."""

    def test_deploy_receipt_round_trip(self, ntfy_cfg):
        """POST a simulated deploy notification; poll ntfy to verify receipt."""
        test_title = f"Deploy OK · main → abc1234"
        test_body = "main → abc1234\nLayers: Backend API · Frontend"

        _post_ntfy(ntfy_cfg, title=test_title, body=test_body)

        # Give ntfy a moment to process
        time.sleep(2)

        msgs = _poll_ntfy(ntfy_cfg, since_seconds=30)
        titles = [m.get("title", "") for m in msgs]
        assert any("Deploy OK" in t for t in titles), (
            f"Expected a 'Deploy OK' message in ntfy poll results. Got titles: {titles}"
        )

    def test_layers_included_in_notification(self, ntfy_cfg):
        """Deploy notification includes layers string in body."""
        test_title = f"Deploy OK · backend-test"
        test_body = "main → deadbeef\nLayers: Backend API · Broker Layer · Frontend"

        _post_ntfy(ntfy_cfg, title=test_title, body=test_body)
        time.sleep(2)

        msgs = _poll_ntfy(ntfy_cfg, since_seconds=30)
        bodies = [m.get("message", "") for m in msgs]
        assert any("Broker Layer" in b for b in bodies), (
            f"Expected 'Broker Layer' in notification body. Got: {bodies}"
        )

    def test_commit_hash_in_notification(self, ntfy_cfg):
        """Deploy notification includes commit hash."""
        test_title = f"Deploy OK · main → commit123456"
        test_body = "main → commit123456\nLayers: Backend API"

        _post_ntfy(ntfy_cfg, title=test_title, body=test_body)
        time.sleep(2)

        msgs = _poll_ntfy(ntfy_cfg, since_seconds=30)
        titles = [m.get("title", "") for m in msgs]
        assert any("commit123456" in t for t in titles), (
            f"Expected commit hash in notification title. Got: {titles}"
        )

    def test_fail_notification_received(self, ntfy_cfg):
        """Deploy failure notification is received."""
        test_title = f"Deploy FAIL · main"
        test_body = "main → abc1234\nDeploy failed: connection timeout"

        _post_ntfy(ntfy_cfg, title=test_title, body=test_body)
        time.sleep(2)

        msgs = _poll_ntfy(ntfy_cfg, since_seconds=30)
        titles = [m.get("title", "") for m in msgs]
        assert any("FAIL" in t for t in titles), (
            f"Expected a 'Deploy FAIL' notification. Got titles: {titles}"
        )
