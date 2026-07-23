"""
Tests for ntfy auth token support and startup validation.

Covers:
  - send_ntfy_alert sends Authorization header when ntfy_token is configured
  - Authorization header omitted when ntfy_token is absent
  - Header format: "Bearer <token>"
  - No network call when ntfy_topic is missing (early return)
  - Token from secrets.yaml is respected
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime
from zoneinfo import ZoneInfo


BASE_SECRETS = {
    "ntfy_topic": "ramboq_alerts",
    "ntfy_url": "https://ntfy.sh",
    "ntfy_night_start": 22,
    "ntfy_night_end": 7,
}


class TestNtfyAuthHeader:
    """Authorization header with token."""

    def test_ntfy_sends_auth_header_when_token_set(self):
        """When ntfy_token is in secrets, Authorization: Bearer header must be sent."""
        secrets_with_token = {
            **BASE_SECRETS,
            "ntfy_token": "test-token-12345",
        }
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", secrets_with_token), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test Alert", "Body message")

            # Verify Authorization header was set
            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None
            auth_header = req.headers.get("Authorization")
            assert auth_header == "Bearer test-token-12345", (
                f"Expected 'Bearer test-token-12345', got {auth_header!r}"
            )

    def test_ntfy_omits_auth_header_when_no_token(self):
        """Without ntfy_token in secrets, no Authorization header sent."""
        secrets_without_token = {
            **BASE_SECRETS,
            # ntfy_token missing
        }
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", secrets_without_token), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test Alert", "Body message")

            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None
            auth_header = req.headers.get("Authorization")
            assert auth_header is None, (
                f"Expected no Authorization header when token is missing, got {auth_header!r}"
            )

    def test_ntfy_empty_token_string_treated_as_absent(self):
        """When ntfy_token is empty string, treat as missing (no Authorization header)."""
        secrets_with_empty_token = {
            **BASE_SECRETS,
            "ntfy_token": "",
        }
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", secrets_with_empty_token), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test Alert", "Body message")

            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None
            auth_header = req.headers.get("Authorization")
            assert auth_header is None, (
                f"Expected no Authorization header for empty token, got {auth_header!r}"
            )

    def test_ntfy_auth_header_with_multiple_sends(self):
        """Auth header sent on all retries (urgent priority = 3 sends)."""
        secrets_with_token = {
            **BASE_SECRETS,
            "ntfy_token": "my-token",
        }
        # Late night (urgent priority) = 3 sends
        dt = datetime(2026, 7, 14, 23, 30, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", secrets_with_token), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Urgent Test", "Message")

            # 3 sends for urgent priority
            assert mock_urlopen.call_count == 3, f"Expected 3 sends, got {mock_urlopen.call_count}"

            # Verify each request had the auth header
            for call in mock_urlopen.call_args_list:
                req = call[0][0] if call[0] else None
                assert req is not None
                auth_header = req.headers.get("Authorization")
                assert auth_header == "Bearer my-token", (
                    f"Expected auth header on retry, got {auth_header!r}"
                )


class TestNtfyNoOpGates:
    """Early return when ntfy_topic is missing."""

    def test_ntfy_no_topic_is_noop(self):
        """When ntfy_topic missing, no network call is made."""
        secrets_without_topic = {
            "ntfy_url": "https://ntfy.sh",
            "ntfy_token": "token-present",  # token set but topic missing
            "ntfy_night_start": 22,
            "ntfy_night_end": 7,
        }

        with patch("backend.shared.helpers.alert_utils.secrets", secrets_without_topic), \
             patch("urllib.request.urlopen") as mock_urlopen:

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test", "Message")

            mock_urlopen.assert_not_called(), (
                "urllib.request.urlopen should not be called when ntfy_topic is missing"
            )

    def test_ntfy_empty_topic_is_noop(self):
        """When ntfy_topic is empty string, no network call."""
        secrets_with_empty_topic = {
            "ntfy_topic": "",
            "ntfy_url": "https://ntfy.sh",
            "ntfy_token": "token-present",
            "ntfy_night_start": 22,
            "ntfy_night_end": 7,
        }

        with patch("backend.shared.helpers.alert_utils.secrets", secrets_with_empty_topic), \
             patch("urllib.request.urlopen") as mock_urlopen:

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test", "Message")

            mock_urlopen.assert_not_called()


class TestNtfyAuthWithCustomUrl:
    """Auth header sent to custom ntfy URLs."""

    def test_ntfy_auth_with_custom_url(self):
        """Auth header works with custom ntfy_url."""
        secrets_with_custom = {
            "ntfy_topic": "alerts",
            "ntfy_url": "https://custom.example.com/ntfy",
            "ntfy_token": "custom-token",
            "ntfy_night_start": 22,
            "ntfy_night_end": 7,
        }
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", secrets_with_custom), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test", "Message")

            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None

            # Check URL contains custom domain
            assert "custom.example.com" in req.full_url, (
                f"Expected custom URL in request, got {req.full_url}"
            )

            # Check auth header is present
            auth_header = req.headers.get("Authorization")
            assert auth_header == "Bearer custom-token", (
                f"Expected auth header with custom URL, got {auth_header!r}"
            )


class TestNtfyAuthExceptionHandling:
    """Exception handling with auth header present."""

    def test_ntfy_auth_exception_swallowed(self):
        """Network error with auth header → silently swallowed."""
        secrets_with_token = {
            **BASE_SECRETS,
            "ntfy_token": "test-token",
        }
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", secrets_with_token), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.side_effect = Exception("Network error")

            from backend.shared.helpers.alert_utils import send_ntfy_alert

            # Should not raise
            result = send_ntfy_alert("Test", "Message")
            assert result is None, f"Expected None return on exception, got {result}"

    def test_ntfy_auth_connection_error_swallowed(self):
        """Connection failure with auth header → silently swallowed."""
        secrets_with_token = {
            **BASE_SECRETS,
            "ntfy_token": "test-token",
        }
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", secrets_with_token), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.side_effect = OSError("Connection refused")

            from backend.shared.helpers.alert_utils import send_ntfy_alert

            # Should not raise
            result = send_ntfy_alert("Test", "Message")
            assert result is None
