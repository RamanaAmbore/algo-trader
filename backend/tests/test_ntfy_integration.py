"""
Live integration tests for ntfy alert delivery.

Sends real alerts to ntfy and verifies receipt via the ntfy polling API
(GET /topic/json?poll=1&since=30s). Skips automatically when ntfy_topic
is not configured in secrets.yaml.

Run explicitly:
    venv/bin/pytest backend/tests/test_ntfy_integration.py -v -m integration
"""
import json
import time
import urllib.request
import uuid
from pathlib import Path

import pytest
import yaml

SECRETS_PATH = Path(__file__).resolve().parents[2] / "config" / "secrets.yaml"


def _load_secrets() -> dict:
    try:
        return yaml.safe_load(SECRETS_PATH.read_text()) or {}
    except FileNotFoundError:
        return {}


@pytest.fixture(scope="module")
def ntfy_cfg():
    sec = _load_secrets()
    topic = sec.get("ntfy_topic")
    if not topic:
        pytest.skip("ntfy_topic not configured in secrets.yaml — skipping live ntfy tests")
    return {
        "topic": topic,
        "url": sec.get("ntfy_url", "https://ntfy.sh"),
        "token": sec.get("ntfy_token"),
    }


def _poll_ntfy(cfg: dict, since_seconds: int = 30) -> list[dict]:
    """Poll ntfy topic for recent messages via the JSON polling API."""
    poll_url = f"{cfg['url'].rstrip('/')}/{cfg['topic']}/json?poll=1&since={since_seconds}s"
    headers = {"Accept": "application/json"}
    if cfg["token"]:
        headers["Authorization"] = f"Bearer {cfg['token']}"
    req = urllib.request.Request(poll_url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=10)
    lines = resp.read().decode().strip().split("\n")
    return [json.loads(line) for line in lines if line.strip()]


@pytest.mark.integration
class TestNtfyLiveAlertReceive:
    """Send real alerts and verify receipt via ntfy polling API."""

    def test_send_ntfy_alert_received(self, ntfy_cfg):
        """send_ntfy_alert() delivers to ntfy; poll confirms receipt."""
        from backend.shared.helpers.alert_utils import send_ntfy_alert

        unique_id = str(uuid.uuid4())[:12]
        title = f"RamboQ Test {unique_id}"
        body = f"Integration test body {unique_id}"

        send_ntfy_alert(title, body, priority="default")
        time.sleep(4)  # allow delivery

        messages = _poll_ntfy(ntfy_cfg, since_seconds=30)
        titles = [m.get("title", "") for m in messages]
        assert title in titles, (
            f"Expected title {title!r} in ntfy topic.\n"
            f"Received titles: {titles}"
        )

    def test_deploy_ntfy_notification_received(self, ntfy_cfg):
        """Simulate the deploy notification ntfy block; poll confirms receipt."""
        cfg = ntfy_cfg
        unique_id = str(uuid.uuid4())[:12]
        event_label = f"Deploy Test {unique_id}"
        body = f"deploy integration check {unique_id}"

        _ntfy_headers = {
            "Title": event_label,
            "Tags": "rocket",
            "Priority": "default",
            "Content-Type": "text/plain",
        }
        if cfg["token"]:
            _ntfy_headers["Authorization"] = f"Bearer {cfg['token']}"

        req = urllib.request.Request(
            f"{cfg['url'].rstrip('/')}/{cfg['topic']}",
            data=body.encode(),
            headers=_ntfy_headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        time.sleep(4)

        messages = _poll_ntfy(cfg, since_seconds=30)
        titles = [m.get("title", "") for m in messages]
        assert event_label in titles, (
            f"Expected deploy notification {event_label!r} in ntfy topic.\n"
            f"Received titles: {titles}"
        )
