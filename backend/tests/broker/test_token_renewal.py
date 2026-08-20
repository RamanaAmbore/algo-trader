"""Tests for on-demand broker token renewal on auth failure.

Verifies that:
1. Auth error detection and dispatch logic when fetch operations fail
2. Connection health tracking via _FETCH_HEALTH and is_account_healthy
3. Token pre-warm uses 60s sleep cadence (not hourly) to hit Kite 05:45–05:59 window
4. Connection service cutover warns when accounts list is empty
"""

from __future__ import annotations

import inspect
import re
import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch, call

import pytest
import pandas as pd


class TestFetchHealthTracking:
    """Test auth error health tracking in fetch functions."""

    def test_fetch_auth_error_detection(self):
        """Test that auth error strings are detected correctly.

        Verify that common auth error patterns are recognized by
        the _is_auth_error_str helper, enabling renewal dispatch.

        Arrange:
          - Various error message strings
        Act:
          - Check if they match auth error patterns
        Assert:
          - Auth error strings return True
          - Normal error strings return False
        """
        from backend.brokers.broker_apis import _is_auth_error_str

        # Auth error cases
        assert _is_auth_error_str("invalid access token"), "Should detect 'invalid access token'"
        assert _is_auth_error_str("invalid token"), "Should detect 'invalid token'"
        assert _is_auth_error_str("Invalid Access Token"), "Should be case-insensitive"
        assert _is_auth_error_str("401 unauthorized — invalid token"), "Should detect in full message"

        # Non-auth error cases
        assert not _is_auth_error_str("network timeout"), "Should not flag network errors"
        assert not _is_auth_error_str("API rate limit exceeded"), "Should not flag rate limits"
        assert not _is_auth_error_str(""), "Should not flag empty string"

    def test_is_account_healthy_tracks_fetch_health(self):
        """Test that is_account_healthy reflects _FETCH_HEALTH status.

        Arrange:
          - Account with recent successful fetch
          - Account with failed fetch or missing health record
        Act:
          - Check is_account_healthy for each account
        Assert:
          - Healthy account returns True
          - Unhealthy account returns False
        """
        from backend.brokers.broker_apis import is_account_healthy, _FETCH_HEALTH
        import time

        # Clear prior state
        _FETCH_HEALTH.clear()

        now = time.time()

        # Account with recent successful fetch
        _FETCH_HEALTH["ACC_GOOD"] = {
            "last_ok_at": now,
            "last_fail_at": 0,
            "last_fail_msg": None,
        }

        # Account with failed fetch
        _FETCH_HEALTH["ACC_BAD"] = {
            "last_ok_at": 0,
            "last_fail_at": now,
            "last_fail_msg": "invalid token",
        }

        # Account with no record
        # (should be considered healthy by default)
        _FETCH_HEALTH["ACC_NEW"] = {
            "last_ok_at": now,
            "last_fail_at": 0,
            "last_fail_msg": None,
        }

        assert is_account_healthy("ACC_GOOD") is True, "Recently successful account should be healthy"
        assert is_account_healthy("ACC_BAD") is False, "Failed account should be unhealthy"
        assert is_account_healthy("ACC_NEW") is True, "New account with success should be healthy"
        assert is_account_healthy("ACC_UNKNOWN") is True, "Unknown account defaults to healthy"


class TestPrewarmSleepCadence:
    """Test token pre-warm sleep interval."""

    def test_prewarm_uses_60s_sleep_not_3600s(self):
        """Guard: _task_prewarm_tokens must use sleep(60) not sleep(3600).

        Rationale:
          Hourly polling (3600s) causes Kite's 05:45–05:59 IST token-mint
          window to be missed statistically. Token expiry at 06:00 IST means
          cold TOTP authentication at market open. 60-second cadence ensures
          the window is hit reliably.

        Assert:
          - _task_prewarm_tokens contains 'await _asyncio.sleep(60)'
          - Does NOT contain 'sleep(3600)'
        """
        with open("backend/brokers/service/app.py") as f:
            src = f.read()

        m = re.search(
            r"async def _task_prewarm_tokens\b.*?(?=\nasync def |\nclass |\Z)",
            src,
            re.DOTALL,
        )
        assert m is not None, "_task_prewarm_tokens not found in service/app.py"
        body = m.group(0)

        assert "sleep(3600)" not in body, (
            "_task_prewarm_tokens must NOT use sleep(3600) — hourly polling "
            "misses the 05:45–05:59 Kite token window. Use sleep(60) to match "
            "background.py _task_token_refresh cadence."
        )
        assert "sleep(60)" in body, (
            "_task_prewarm_tokens must use sleep(60) to reliably hit the "
            "05:45–05:59 Kite token renewal window."
        )


class TestConnServiceWarning:
    """Test connection service cutover warnings."""

    def test_loaded_accounts_warns_when_conn_service_empty(self):
        """_loaded_accounts() must warn when conn_service returns empty list.

        Under cutover mode (local Connections is empty), if list_remote_accounts()
        returns no accounts, a WARNING should be logged so the operator sees the
        root cause of the 0/5 badge via logs.

        Assert:
          - The warning message exists in the source code at the cutover empty branch
        """
        with open("backend/api/routes/brokers.py") as f:
            src = f.read()

        # Verify that the warning message is present in _loaded_accounts function
        assert "_loaded_accounts: conn_service returned no accounts" in src, (
            "_loaded_accounts must log a WARNING when conn_service returns "
            "no accounts under cutover mode, so the operator can diagnose "
            "the 0/5 badge root cause via logs"
        )

        # Verify the warning is in the right branch (empty in_conn after cutover)
        m = re.search(
            r"if not in_conn:.*?if is_cutover_on\(\):.*?"
            r"if not in_conn:.*?"
            r"logger\.warning\(\s*['\"].*?conn_service returned no accounts",
            src,
            re.DOTALL,
        )
        assert m is not None, (
            "Warning must be logged when list_remote_accounts() returns "
            "empty list in cutover mode"
        )

    def test_loaded_accounts_filters_unhealthy(self):
        """_loaded_accounts() filters out unhealthy accounts.

        Arrange:
          - Connections has 3 accounts: ACC1 (healthy), ACC2 (unhealthy), ACC3 (healthy)
          - is_account_healthy mocked to return False for ACC2
        Act:
          - Call _loaded_accounts()
        Assert:
          - Returns {ACC1, ACC3} (unhealthy ACC2 filtered out)
        """
        from unittest.mock import patch as mock_patch
        from backend.api.routes.brokers import _loaded_accounts

        with mock_patch("backend.brokers.connections.Connections") as mock_conns, \
             mock_patch("backend.brokers.broker_apis.is_account_healthy") as mock_health, \
             mock_patch("backend.brokers.client.is_cutover_on", return_value=False):
            mock_conns.return_value.conn = {
                "ACC1": MagicMock(),
                "ACC2": MagicMock(),
                "ACC3": MagicMock(),
            }

            def is_healthy(account):
                return account != "ACC2"

            mock_health.side_effect = is_healthy

            result = _loaded_accounts()

        assert result == {"ACC1", "ACC3"}, "Expected unhealthy ACC2 to be filtered out"


class TestCircuitBreakerRecordFetch:
    """Test _record_fetch health tracking."""

    def test_record_fetch_updates_fail_timestamp_on_failure(self):
        """_record_fetch must update last_fail_at on failure.

        Arrange:
          - Account with no prior health record
          - Call _record_fetch with ok=False
        Act:
          - Verify last_fail_at is updated
        Assert:
          - last_fail_at set to current timestamp
          - last_fail_msg contains the error message
        """
        from backend.brokers.broker_apis import _record_fetch, _FETCH_HEALTH
        import time

        _FETCH_HEALTH.clear()

        before_time = time.time()
        _record_fetch("ACC_TEST", ok=False, error="API timeout")
        after_time = time.time()

        record = _FETCH_HEALTH.get("ACC_TEST")
        assert record is not None, "Expected health record to be created"
        assert before_time <= record.get("last_fail_at", 0) <= after_time, (
            "Expected last_fail_at to be set to current timestamp"
        )
        assert "timeout" in record.get("last_fail_msg", "").lower(), "Expected error message in record"

    def test_record_fetch_updates_ok_timestamp_on_success(self):
        """_record_fetch must update last_ok_at on success.

        Arrange:
          - Account with prior failures
        Act:
          - Call _record_fetch with ok=True
        Assert:
          - last_ok_at updated to current timestamp
          - Account becomes healthy
        """
        from backend.brokers.broker_apis import _record_fetch, _FETCH_HEALTH
        import time

        _FETCH_HEALTH.clear()

        # Record a failure first
        _record_fetch("ACC_TEST", ok=False, error="API error")
        fail_record = _FETCH_HEALTH["ACC_TEST"].copy()

        # Now record a success
        time.sleep(0.01)  # Ensure last_ok_at > last_fail_at
        before_time = time.time()
        _record_fetch("ACC_TEST", ok=True)
        after_time = time.time()

        record = _FETCH_HEALTH.get("ACC_TEST")
        assert before_time <= record.get("last_ok_at", 0) <= after_time, (
            "Expected last_ok_at to be set to current timestamp"
        )
        assert record.get("last_ok_at") > fail_record.get("last_fail_at"), (
            "Expected last_ok_at to be greater than last_fail_at after success"
        )
        assert is_account_healthy("ACC_TEST") is True, "Account should be healthy after success"


class TestAuthErrorStringPatterns:
    """Test auth error detection patterns."""

    def test_auth_error_patterns_cover_common_cases(self):
        """_is_auth_error_str must detect common auth error patterns.

        Verify coverage of:
        - Kite "invalid access token"
        - Dhan "invalid token"
        - HTTP 401 patterns
        - Case variations
        """
        from backend.brokers.broker_apis import _is_auth_error_str

        # Common Kite auth errors
        test_cases = [
            ("invalid access token", True),
            ("Invalid Access Token", True),
            ("401 Unauthorized: invalid access token", True),
            ("invalid token", True),
            ("INVALID TOKEN", True),
            ("Unauthorized: invalid token", True),
            # Non-auth errors
            ("connection timeout", False),
            ("rate limit exceeded", False),
            ("502 bad gateway", False),
            ("404 not found", False),
            ("", False),
        ]

        for error_msg, expected_is_auth in test_cases:
            result = _is_auth_error_str(error_msg)
            assert result == expected_is_auth, (
                f"_is_auth_error_str('{error_msg}') should return {expected_is_auth}, "
                f"but got {result}"
            )


def is_account_healthy(account: str) -> bool:
    """Import and call is_account_healthy for test convenience."""
    from backend.brokers.broker_apis import is_account_healthy as iah
    return iah(account)
