# Plan: Fix ntfy deploy token + integration test with live receive-side verification

## Context
`notify_deploy.py`'s ntfy block has no Bearer token support — `send_ntfy_alert()` in
`alert_utils.py` conditionally adds `Authorization: Bearer {ntfy_token}`, but the inline
ntfy POST in `notify_deploy.py` (lines 136–151) never reads `ntfy_token` from secrets.
If the ntfy topic requires auth (ntfy.sh private topic or self-hosted), every deploy
notification silently fails. The fix is one-liner. The user also wants testing that
actually sends alerts and verifies receipt on the ntfy side, not just mocks.

## Task
1. Fix `notify_deploy.py`: add Bearer token header to the ntfy block.
2. Add a unit test asserting the token appears in the deploy notification request.
3. Add a live integration test (`test_ntfy_integration.py`) that:
   - Sends a real `send_ntfy_alert()` with a unique ID
   - Polls the ntfy topic's `/json?poll=1&since=30s` endpoint
   - Asserts the message appears in the received list
   - Also sends a simulated deploy notification and verifies that too
   - Skips automatically if `ntfy_topic` is not configured in secrets

## Agents
- backend: skip
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Three changes:

  **1. Fix `webhook/notify_deploy.py`** (lines 136–151):
  After `ntfy_url = sec.get("ntfy_url", "https://ntfy.sh")`, add:
  ```python
  ntfy_token = sec.get("ntfy_token")
  ```
  Replace the hardcoded headers dict:
  ```python
  headers={"Title": event_label, "Tags": "rocket", "Priority": "default", "Content-Type": "text/plain"},
  ```
  with:
  ```python
  _ntfy_headers = {"Title": event_label, "Tags": "rocket", "Priority": "default", "Content-Type": "text/plain"}
  if ntfy_token:
      _ntfy_headers["Authorization"] = f"Bearer {ntfy_token}"
  ```
  and pass `headers=_ntfy_headers` to `_urlreq.Request(...)`.

  **2. New unit test `backend/tests/test_ntfy_deploy_token.py`**:
  Using the existing `_run_main` / `_load_notify` pattern from `test_deploy_notify.py`,
  but patching `urllib.request.urlopen` instead of `requests.post` to capture the ntfy
  request:
  - Test A: secrets include `ntfy_token` → `Authorization: Bearer <token>` header present in ntfy Request
  - Test B: secrets omit `ntfy_token` → no Authorization header
  - Test C: secrets include both `ntfy_topic` and `ntfy_token` on prod branch → urlopen called AND has auth header

  Reference files for patterns:
  - `backend/tests/test_deploy_notify.py` (load/run helpers)
  - `backend/tests/test_ntfy_alert_auth.py` (urllib mock assertion pattern)

  **3. New integration test `backend/tests/test_ntfy_integration.py`**:
  Marked `@pytest.mark.integration` and skipped automatically when `ntfy_topic` absent.

  Test structure:
  ```python
  import uuid, time, json, urllib.request, pytest, yaml
  from pathlib import Path

  SECRETS_PATH = Path(__file__).parents[2] / "config" / "secrets.yaml"

  def _load_secrets():
      try:
          return yaml.safe_load(SECRETS_PATH.read_text()) or {}
      except FileNotFoundError:
          return {}

  @pytest.fixture(scope="module")
  def ntfy_config():
      sec = _load_secrets()
      topic = sec.get("ntfy_topic")
      if not topic:
          pytest.skip("ntfy_topic not configured — skipping live ntfy integration tests")
      return {
          "topic": topic,
          "url": sec.get("ntfy_url", "https://ntfy.sh"),
          "token": sec.get("ntfy_token"),
      }

  def _poll_ntfy(cfg, since_seconds=30):
      """Poll ntfy topic for recent messages. Returns list of message dicts."""
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

      def test_send_ntfy_alert_received(self, ntfy_config):
          """send_ntfy_alert() delivers a message; poll verifies receipt."""
          from backend.shared.helpers.alert_utils import send_ntfy_alert
          unique_id = str(uuid.uuid4())[:12]
          title = f"RamboQ Test {unique_id}"
          body = f"Integration test body {unique_id}"

          send_ntfy_alert(title, body, priority="default")
          time.sleep(4)  # allow delivery

          messages = _poll_ntfy(ntfy_config, since_seconds=30)
          titles = [m.get("title", "") for m in messages]
          assert title in titles, (
              f"Expected title {title!r} in ntfy messages.\nReceived: {titles}"
          )

      def test_deploy_ntfy_alert_received(self, ntfy_config):
          """Simulate deploy notification ntfy block; poll verifies receipt."""
          import urllib.request as _urlreq
          cfg = ntfy_config
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

          req = _urlreq.Request(
              f"{cfg['url'].rstrip('/')}/{cfg['topic']}",
              data=body.encode(),
              headers=_ntfy_headers,
              method="POST",
          )
          _urlreq.urlopen(req, timeout=5)
          time.sleep(4)

          messages = _poll_ntfy(cfg, since_seconds=30)
          titles = [m.get("title", "") for m in messages]
          assert event_label in titles, (
              f"Expected deploy notification {event_label!r} in ntfy.\nReceived: {titles}"
          )
  ```

- playwright: skip

## Tests
- pytest: yes (unit: `test_ntfy_deploy_token.py`; integration: `test_ntfy_integration.py`)
- svelte-check: no
- playwright: no

## Commit message
fix(alerts): add Bearer token to ntfy deploy notification; add unit+integration tests with live receive-side check

## Done when
- `notify_deploy.py` ntfy block includes `Authorization: Bearer` when `ntfy_token` present
- Unit tests pass (mock-based, no network)
- Integration tests run against real ntfy and confirm messages are received in the topic
- All existing ntfy + deploy tests still green
