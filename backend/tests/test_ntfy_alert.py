"""
Tests for send_ntfy_alert — push notification via ntfy.sh.

Covers:
1. Night window wrapping midnight (ET): hour >= night_start OR hour < night_end.
2. Priority assignment: "urgent" during night, "high" otherwise.
3. No-op when ntfy_topic not configured.
4. Boundary hours at night_start and night_end.
5. HTTP POST headers: Title, Priority, Tags="rotating_light".
6. Silent exception swallowing.

Design note: ntfy.sh is a service that delivers HTTP POST payloads as push
notifications. The function reads from secrets.yaml and conditionally sets
priority based on current ET time. Uses urllib.request for HTTP calls.
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo


BASE_SECRETS = {
    "ntfy_topic": "ramboq_alerts",
    "ntfy_url": "https://ntfy.sh",
    "ntfy_night_start": 22,
    "ntfy_night_end": 7,
}


class TestNtfyPriority:
    """Priority assignment based on ET time."""

    def test_ntfy_urgent_late_night(self):
        """ET hour=23 → priority='urgent' (after night_start=22) → 3 sends."""
        # Mock datetime.now to return ET 23:30
        dt = datetime(2026, 7, 14, 23, 30, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test Alert", "Late night message")

            assert mock_urlopen.call_count == 3, \
                f"Expected 3 calls for urgent priority, got {mock_urlopen.call_count}"
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None and req.headers.get("Priority") == "urgent", \
                f"Expected priority='urgent' at hour=23, got {req.headers.get('Priority') if req else 'None'}"

    def test_ntfy_urgent_past_midnight(self):
        """ET hour=3 → priority='urgent' (before night_end=7) → 3 sends."""
        dt = datetime(2026, 7, 15, 3, 15, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Midnight Alert", "Early morning message")

            assert mock_urlopen.call_count == 3, \
                f"Expected 3 calls for urgent priority, got {mock_urlopen.call_count}"
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None and req.headers.get("Priority") == "urgent", \
                f"Expected priority='urgent' at hour=3, got {req.headers.get('Priority') if req else 'None'}"

    def test_ntfy_high_afternoon(self):
        """ET hour=14 → priority='high' (outside night window)."""
        dt = datetime(2026, 7, 14, 14, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Day Alert", "Afternoon message")

            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None and req.headers.get("Priority") == "high", \
                f"Expected priority='high' at hour=14, got {req.headers.get('Priority') if req else 'None'}"

    def test_ntfy_night_start_boundary(self):
        """ET hour=22 exactly (night_start) → priority='urgent' → 3 sends."""
        dt = datetime(2026, 7, 14, 22, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Boundary Alert", "At night_start")

            assert mock_urlopen.call_count == 3, \
                f"Expected 3 calls for urgent priority, got {mock_urlopen.call_count}"
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None and req.headers.get("Priority") == "urgent", \
                f"Expected priority='urgent' at hour=22 (night_start), got {req.headers.get('Priority') if req else 'None'}"

    def test_ntfy_night_end_boundary(self):
        """ET hour=7 exactly (night_end) → priority='high' (outside window)."""
        dt = datetime(2026, 7, 15, 7, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Morning Alert", "At night_end")

            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None and req.headers.get("Priority") == "high", \
                f"Expected priority='high' at hour=7 (night_end), got {req.headers.get('Priority') if req else 'None'}"


class TestNtfyHttpPayload:
    """HTTP POST payload structure and headers."""

    def test_ntfy_tags_and_title(self):
        """HTTP POST includes Title header and Tags='rotating_light'."""
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Critical Alert", "Something went wrong")

            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None

            # Check URL
            assert req is not None and "ntfy.sh/ramboq_alerts" in req.full_url, \
                f"Expected ntfy URL, got {req.full_url if req else 'None'}"

            # Check headers
            assert req.headers.get("Title") == "Critical Alert", \
                f"Expected Title='Critical Alert', got {req.headers.get('Title')}"
            assert req.headers.get("Tags") == "rotating_light", \
                f"Expected Tags='rotating_light', got {req.headers.get('Tags')}"

    def test_ntfy_custom_url_from_secrets(self):
        """When ntfy_url is overridden in secrets, use it."""
        secrets_with_custom_url = {
            **BASE_SECRETS,
            "ntfy_url": "https://custom.ntfy.sh",
        }
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", secrets_with_custom_url), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test", "Message")

            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None and "custom.ntfy.sh" in req.full_url, \
                f"Expected custom ntfy URL, got {req.full_url if req else ''}"


class TestNtfyNoOp:
    """No-op when ntfy_topic not configured."""

    def test_ntfy_no_topic_is_noop(self):
        """When ntfy_topic is None or empty, urllib.request.urlopen NOT called."""
        secrets_without_topic = {
            "ntfy_url": "https://ntfy.sh",
            "ntfy_night_start": 22,
            "ntfy_night_end": 7,
            # ntfy_topic missing
        }

        with patch("backend.shared.helpers.utils.secrets", secrets_without_topic), \
             patch("urllib.request.urlopen") as mock_urlopen:

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test", "Message")

            mock_urlopen.assert_not_called(), \
                "urllib.request.urlopen should not be called when ntfy_topic is missing"

    def test_ntfy_empty_topic_is_noop(self):
        """When ntfy_topic is empty string, urllib.request.urlopen NOT called."""
        secrets_with_empty_topic = {
            "ntfy_topic": "",
            "ntfy_url": "https://ntfy.sh",
            "ntfy_night_start": 22,
            "ntfy_night_end": 7,
        }

        with patch("backend.shared.helpers.utils.secrets", secrets_with_empty_topic), \
             patch("urllib.request.urlopen") as mock_urlopen:

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test", "Message")

            mock_urlopen.assert_not_called(), \
                "urllib.request.urlopen should not be called when ntfy_topic is empty"


class TestNtfyExceptionHandling:
    """Silent exception swallowing."""

    def test_ntfy_exception_swallowed(self):
        """urllib.request.urlopen raises → function returns None, no exception propagates."""
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.side_effect = Exception("Network error")

            from backend.shared.helpers.alert_utils import send_ntfy_alert

            # Should not raise; exception is silently swallowed
            result = send_ntfy_alert("Test", "Message")

            # Function should return None and no exception should propagate
            assert result is None, f"Expected None return, got {result}"

    def test_ntfy_connection_error_swallowed(self):
        """urllib.request.urlopen raises OSError → silently swallowed."""
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.side_effect = OSError("Connection failed")

            from backend.shared.helpers.alert_utils import send_ntfy_alert

            # Should not raise
            result = send_ntfy_alert("Test", "Message")
            assert result is None


class TestNtfyMessageContent:
    """Message payload content."""

    def test_ntfy_title_and_message_in_payload(self):
        """Title goes to header, message to request body."""
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("My Alert", "My message content")

            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None

            assert req is not None and req.headers.get("Title") == "My Alert"

            # Message should be in the request data
            assert "My message content" in str(req.data if hasattr(req, 'data') else ''), \
                f"Expected message in request data"


class TestNtfyDefaultsAndOverrides:
    """Test default configuration values and overrides."""

    def test_ntfy_default_url_when_not_configured(self):
        """When ntfy_url not in secrets, default to 'https://ntfy.sh'."""
        secrets_without_url = {
            "ntfy_topic": "test_topic",
            "ntfy_night_start": 22,
            "ntfy_night_end": 7,
            # ntfy_url missing
        }
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", secrets_without_url), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test", "Message")

            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None and "ntfy.sh" in req.full_url, \
                f"Expected default ntfy.sh URL, got {req.full_url if req else ''}"

    def test_ntfy_default_night_hours_when_not_configured(self):
        """When ntfy_night_start/end not in secrets, use defaults (22, 7)."""
        secrets_without_night_hours = {
            "ntfy_topic": "test_topic",
            "ntfy_url": "https://ntfy.sh",
            # ntfy_night_start/end missing
        }
        # Hour 23 should be urgent with default night_start=22
        dt = datetime(2026, 7, 14, 23, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", secrets_without_night_hours), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test", "Message")

            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None and req.headers.get("Priority") == "urgent", \
                f"Expected default night window to apply (22-7), got priority={req.headers.get('Priority') if req else 'None'}"


class TestNtfyWrappingMidnight:
    """Night window wrapping behavior (night_start > night_end)."""

    def test_ntfy_wrapping_before_midnight(self):
        """Night 22:00-06:59 wrapping: hour >= 22 OR hour < 7."""
        # Test just before midnight: hour 23
        dt = datetime(2026, 7, 14, 23, 45, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Late Night", "Message")

            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None and req.headers.get("Priority") == "urgent"

    def test_ntfy_wrapping_after_midnight(self):
        """Night 22:00-06:59 wrapping: hour >= 22 OR hour < 7."""
        # Test after midnight: hour 4
        dt = datetime(2026, 7, 15, 4, 30, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Early Morning", "Message")

            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None and req.headers.get("Priority") == "urgent"

    def test_ntfy_outside_wrapping_window(self):
        """Outside night window: hour >= 7 AND hour < 22 → priority='high'."""
        # Test mid-day: hour 12
        dt = datetime(2026, 7, 15, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Mid-Day", "Message")

            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None
            assert req is not None and req.headers.get("Priority") == "high"


class TestNtfyUrllibUsage:
    """Verify ntfy uses urllib.request for IPv4-safe delivery."""

    def test_send_ntfy_uses_urllib_not_httpx(self):
        """Verify ntfy send uses urllib (IPv4-safe) and not httpx."""
        dt = datetime(2026, 7, 14, 15, 0, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch("backend.shared.helpers.alert_utils.secrets", BASE_SECRETS), \
             patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("test", "body")

            # Verify urlopen was called exactly once for high priority
            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None

            # Verify Request object has correct URL and headers
            assert req is not None, "Expected urllib.request.Request object"
            assert "ntfy.sh/ramboq_alerts" in req.full_url, \
                f"Expected ntfy URL, got {req.full_url}"
            assert req.headers.get("Title") == "test", \
                f"Expected Title='test', got {req.headers.get('Title')}"
