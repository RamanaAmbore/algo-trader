"""
Direct unit tests for the PYTEST_RUNNING alert-leak guard
(pytest_alert_transport_blocked, backend/shared/helpers/utils.py) and its
wiring into every leaf transport function in alert_utils.py / mail_utils.py.

P0 production-safety defect: conftest.py sets PYTEST_RUNNING=1 but never
isolates the DB session — is_enabled() checks a DB setting
(notifications.<cap>_enabled) BEFORE falling back to YAML branch
defaults, so a test process that has ever touched a real deployment's DB
(e.g. backend.shared.helpers.settings._CACHE populated by a real
reload_cache() call) can read a capability as enabled and a "test" run
would then send a live Telegram / ntfy / email message.

Design under test: `pytest_alert_transport_blocked()` short-circuits
every leaf transport function whenever PYTEST_RUNNING is set UNLESS the
test explicitly opts in via `@pytest.mark.alert_transport` (which sets
RAMBOQ_ALERT_TRANSPORT_OK=1 for the duration of that test only — see the
autouse fixture in conftest.py).

Every test here patches the REAL transport call (requests.post /
urllib.request.urlopen / smtplib) and calls the REAL guarded function —
never a mock of the guarded function itself, since that pattern
(asserting a function wasn't called, on a mock of that same function)
is vacuous and wouldn't actually prove the guard exists.
"""
from __future__ import annotations

import os

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# pytest_alert_transport_blocked() — the guard predicate itself
# ---------------------------------------------------------------------------

class TestPytestAlertTransportBlocked:
    def test_blocked_when_pytest_running_and_no_opt_in(self, monkeypatch):
        monkeypatch.setenv("PYTEST_RUNNING", "1")
        monkeypatch.delenv("RAMBOQ_ALERT_TRANSPORT_OK", raising=False)
        from backend.shared.helpers.utils import pytest_alert_transport_blocked
        assert pytest_alert_transport_blocked() is True

    def test_not_blocked_when_opted_in(self, monkeypatch):
        monkeypatch.setenv("PYTEST_RUNNING", "1")
        monkeypatch.setenv("RAMBOQ_ALERT_TRANSPORT_OK", "1")
        from backend.shared.helpers.utils import pytest_alert_transport_blocked
        assert pytest_alert_transport_blocked() is False

    def test_not_blocked_when_pytest_running_unset(self, monkeypatch):
        # Simulates a real deployment process (PYTEST_RUNNING never set).
        monkeypatch.delenv("PYTEST_RUNNING", raising=False)
        monkeypatch.delenv("RAMBOQ_ALERT_TRANSPORT_OK", raising=False)
        from backend.shared.helpers.utils import pytest_alert_transport_blocked
        assert pytest_alert_transport_blocked() is False


# ---------------------------------------------------------------------------
# _send_telegram — negative control (guard fires) + positive control
# (marked opt-in reaches the real requests.post call).
# ---------------------------------------------------------------------------

_FAKE_TG_SECRETS = {
    "telegram_bot_token": "tok-123",
    "telegram_chat_id": "chat-456",
}


class TestSendTelegramGuard:
    def test_guard_blocks_post_by_default_even_when_capability_enabled(self):
        """Negative control: even with is_enabled('telegram')=True (simulating
        a DB row that leaked in from a real deployment) and secrets fully
        configured, PYTEST_RUNNING guard must stop requests.post from firing
        — this test carries NO alert_transport marker."""
        with patch("backend.shared.helpers.alert_utils.secrets", _FAKE_TG_SECRETS), \
             patch("backend.shared.helpers.alert_utils.is_enabled", return_value=True), \
             patch("backend.shared.helpers.alert_utils.requests") as mock_requests:

            from backend.shared.helpers.alert_utils import _send_telegram
            _send_telegram("should never be sent")

            mock_requests.post.assert_not_called()

    @pytest.mark.alert_transport
    def test_opted_in_test_reaches_real_transport(self):
        """Positive control — proves the guard is opt-out-able and that the
        negative-control test above is actually exercising the guard, not
        some unrelated no-op (e.g. missing secrets)."""
        mock_resp = MagicMock(ok=True)
        with patch("backend.shared.helpers.alert_utils.secrets", _FAKE_TG_SECRETS), \
             patch("backend.shared.helpers.alert_utils.is_enabled", return_value=True), \
             patch("backend.shared.helpers.alert_utils.requests") as mock_requests:
            mock_requests.post.return_value = mock_resp

            from backend.shared.helpers.alert_utils import _send_telegram
            _send_telegram("marked test reaches transport")

            mock_requests.post.assert_called_once()


# ---------------------------------------------------------------------------
# _send_telegram_info
# ---------------------------------------------------------------------------

class TestSendTelegramInfoGuard:
    def test_guard_blocks_post_by_default(self):
        with patch("backend.shared.helpers.alert_utils.secrets", _FAKE_TG_SECRETS), \
             patch("backend.shared.helpers.alert_utils.is_enabled", return_value=True), \
             patch("backend.shared.helpers.alert_utils.requests") as mock_requests:

            from backend.shared.helpers.alert_utils import _send_telegram_info
            _send_telegram_info("should never be sent")

            mock_requests.post.assert_not_called()

    @pytest.mark.alert_transport
    def test_opted_in_test_reaches_real_transport(self):
        mock_resp = MagicMock(ok=True)
        with patch("backend.shared.helpers.alert_utils.secrets", _FAKE_TG_SECRETS), \
             patch("backend.shared.helpers.alert_utils.is_enabled", return_value=True), \
             patch("backend.shared.helpers.alert_utils.requests") as mock_requests:
            mock_requests.post.return_value = mock_resp

            from backend.shared.helpers.alert_utils import _send_telegram_info
            _send_telegram_info("marked test reaches transport")

            mock_requests.post.assert_called_once()


# ---------------------------------------------------------------------------
# send_ntfy_alert
# ---------------------------------------------------------------------------

_FAKE_NTFY_SECRETS = {"ntfy_topic": "ramboq_alerts", "ntfy_url": "https://ntfy.sh"}


class TestSendNtfyAlertGuard:
    def test_guard_blocks_urlopen_by_default(self):
        with patch("backend.shared.helpers.alert_utils.secrets", _FAKE_NTFY_SECRETS), \
             patch("urllib.request.urlopen") as mock_urlopen:

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("title", "should never be sent")

            mock_urlopen.assert_not_called()

    @pytest.mark.alert_transport
    def test_opted_in_test_reaches_real_transport(self):
        with patch("backend.shared.helpers.alert_utils.secrets", _FAKE_NTFY_SECRETS), \
             patch("urllib.request.urlopen") as mock_urlopen:

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("title", "marked test reaches transport", priority="high")

            mock_urlopen.assert_called_once()


# ---------------------------------------------------------------------------
# send_email (mail_utils.py) — the leaf email sender used by
# _dispatch_email / _send_order_failure_messages / send_summary. Called
# DIRECTLY by _fire_wing_unprotected_alert's sibling paths and every
# other alert surface, so it needs the same guard directly (not just via
# the router).
# ---------------------------------------------------------------------------

class TestSendEmailGuard:
    def test_guard_blocks_smtp_by_default(self):
        """Negative control — even with mail capability enabled and full
        SMTP secrets configured, the guard must stop _IPv4SMTP from ever
        being constructed."""
        with patch("backend.shared.helpers.mail_utils.is_enabled", return_value=True), \
             patch("backend.shared.helpers.mail_utils._IPv4SMTP") as mock_smtp_cls, \
             patch("backend.shared.helpers.mail_utils.secrets", {
                 "smtp_server": "smtp.example.com",
                 "smtp_port": 587,
                 "smtp_user": "user@example.com",
                 "smtp_pass": "secret",
             }):
            from backend.shared.helpers.mail_utils import send_email
            ok, msg = send_email("Name", "someone@example.com", "subject", "<p>body</p>")

            mock_smtp_cls.assert_not_called()
            assert ok is True
            assert "PYTEST_RUNNING" in msg

    @pytest.mark.alert_transport
    def test_opted_in_test_reaches_real_transport(self):
        with patch("backend.shared.helpers.mail_utils.is_enabled", return_value=True), \
             patch("backend.shared.helpers.mail_utils._IPv4SMTP") as mock_smtp_cls, \
             patch("backend.shared.helpers.mail_utils.secrets", {
                 "smtp_server": "smtp.example.com",
                 "smtp_port": 587,
                 "smtp_user": "user@example.com",
                 "smtp_pass": "secret",
             }):
            mock_server = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = mock_server

            from backend.shared.helpers.mail_utils import send_email
            ok, msg = send_email("Name", "someone@example.com", "subject", "<p>body</p>")

            mock_smtp_cls.assert_called_once()
            mock_server.sendmail.assert_called_once()
            assert ok is True
