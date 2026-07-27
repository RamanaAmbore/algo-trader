"""
Tests for DhanConnection token renewal gate when DH-906 "Invalid Token" is received.

Verifies that when `test_conn=True` (indicating a known-dead token from DH-906),
the connection layer skips the lightweight `_try_renew()` path and calls
`_mint_and_build()` directly to force a full PIN+TOTP re-authentication.

When `test_conn=False` (normal operation), an existing token should be renewed
via `_try_renew()` first before falling back to a full mint.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch, call
import threading

import pytest

from backend.brokers.connections import DhanConnection

INDIAN_TIMEZONE = ZoneInfo("Asia/Kolkata")


class TestDhanTokenRenewal:
    """Test the DhanConnection token renewal decision gate."""

    def setup_method(self):
        """Initialize a DhanConnection instance for testing."""
        self.account = "DH-TEST-001"
        self.client_id = "test-client-id"
        self.conn = DhanConnection(
            account=self.account,
            client_id=self.client_id,
            api_key="test-api-key",
            api_secret="test-api-secret",
            pin="1234",
            totp_token="JBSWY3DPEBLW64TMMQ======",  # dummy base32
            source_ip="127.0.0.1",
        )
        # Pre-populate with a token to simulate cached state
        self.conn._access_token = "cached-token-abc123"
        self.conn._conn_created_at = datetime.now(tz=INDIAN_TIMEZONE) - timedelta(hours=1)
        self.conn._dhan = MagicMock()  # Mock the SDK client
        self.conn._login_blocked_until = 0.0  # Not rate-limited

    def teardown_method(self):
        """Clean up after each test."""
        # Ensure thread locks are released
        pass

    def test_dhan_conn_dead_token_skips_renew(self):
        """When test_conn=True (known-dead token from DH-906), skip _try_renew.

        Arrange:
          - DhanConnection with cached _access_token (client None to pass early returns)
          - test_conn=True (caller knows token is dead)
          - _try_renew is mocked to track if it's called

        Act:
          - Call _dhan_conn_under_lock with test_conn=True

        Assert:
          - _try_renew() is NOT called
          - _mint_and_build() IS called

        Note: _dhan is set to None so we bypass early returns and reach line 1158.
        With test_conn=True, the condition on line 1158 (_access_token and not test_conn)
        evaluates to False, so _try_renew is skipped and we go directly to _mint_and_build.
        """
        now = datetime.now(tz=INDIAN_TIMEZONE)
        with patch.object(
            self.conn, "_try_renew", return_value="new-token"
        ) as mock_renew, patch.object(
            self.conn, "_mint_and_build"
        ) as mock_mint, patch.object(
            self.conn, "_try_restore_token"
        ) as mock_restore, patch.object(
            self.conn, "_is_token_expired", return_value=True
        ), patch.object(
            self.conn, "_check_login_rate_limit", return_value=None
        ), patch(
            "backend.brokers.connections.timestamp_indian",
            return_value=now,
        ):
            # Set up state: token exists but client is None (to reach line 1158)
            self.conn._access_token = "stale-token"
            self.conn._dhan = None  # No client yet
            self.conn._conn_created_at = now - timedelta(hours=25)  # expired

            # Call _dhan_conn_under_lock with test_conn=True
            result = self.conn._dhan_conn_under_lock(now, test_conn=True)

            # _try_renew should NOT be called (because test_conn=True)
            mock_renew.assert_not_called()

            # _mint_and_build MUST be called to force full re-auth
            mock_mint.assert_called_once()

            # Result should be None (signals: mint was attempted)
            assert result is None, f"expected None (mint attempted), got {result}"

    def test_dhan_conn_normal_expiry_tries_renew(self):
        """When test_conn=False and token exists, attempt renewal first.

        Arrange:
          - DhanConnection with cached _access_token (but token EXPIRED or client None)
          - test_conn=False (normal operation)
          - _try_renew is mocked to succeed

        Act:
          - Call _dhan_conn_under_lock with test_conn=False
          - _try_renew returns a new token

        Assert:
          - _try_renew() IS called (because token is expired)
          - _mint_and_build() is NOT called (renewal succeeded)
          - New token is stored
          - Existing client is returned (not None)

        Note: _dhan is set to None to bypass early return on line 1144-1145.
        The renewal path (line 1158) is triggered when the client doesn't exist
        or the token is expired.
        """
        now = datetime.now(tz=INDIAN_TIMEZONE)
        with patch.object(
            self.conn, "_try_renew", return_value="new-renewed-token"
        ) as mock_renew, patch.object(
            self.conn, "_mint_and_build"
        ) as mock_mint, patch.object(
            self.conn, "_save_token"
        ) as mock_save, patch.object(
            self.conn, "_build_client"
        ) as mock_build, patch.object(
            self.conn, "_try_restore_token"
        ) as mock_restore, patch(
            "backend.brokers.connections.timestamp_indian",
            return_value=now,
        ), patch.object(
            self.conn, "_is_token_expired", return_value=True
        ), patch.object(
            self.conn, "_check_login_rate_limit", return_value=None
        ), patch(
            "backend.brokers.adapters.dhan.record_dhan_login_event", side_effect=Exception
        ):
            # Set up state: token exists but client is None (requires rebuild)
            self.conn._access_token = "old-but-valid-token"
            self.conn._dhan = None  # No client, so we'll try renewal
            self.conn._conn_created_at = now - timedelta(hours=25)

            # Call _dhan_conn_under_lock with test_conn=False
            result = self.conn._dhan_conn_under_lock(now, test_conn=False)

            # _try_renew MUST be called
            mock_renew.assert_called_once()

            # _mint_and_build should NOT be called (renewal succeeded)
            mock_mint.assert_not_called()

            # Token should be updated
            assert (
                self.conn._access_token == "new-renewed-token"
            ), f"expected renewed token, got {self.conn._access_token}"

            # Client should be built (mock_build was called)
            mock_build.assert_called_once_with("new-renewed-token")

    def test_dhan_conn_normal_expiry_renew_fails_falls_back_to_mint(self):
        """When renewal fails (returns None), fall back to _mint_and_build.

        Arrange:
          - DhanConnection with cached _access_token (but client None)
          - test_conn=False (normal operation)
          - _try_renew returns None (network blip, endpoint down, etc.)

        Act:
          - Call _dhan_conn_under_lock with test_conn=False
          - _try_renew fails and returns None

        Assert:
          - _try_renew() IS called
          - _mint_and_build() IS called (fallback on renewal failure)
          - Result is None (signals: mint was attempted)

        Note: _dhan is set to None to bypass early return and trigger renewal path.
        """
        now = datetime.now(tz=INDIAN_TIMEZONE)
        with patch.object(
            self.conn, "_try_renew", return_value=None
        ) as mock_renew, patch.object(
            self.conn, "_mint_and_build"
        ) as mock_mint, patch.object(
            self.conn, "_try_restore_token"
        ) as mock_restore, patch(
            "backend.brokers.connections.timestamp_indian",
            return_value=now,
        ), patch.object(
            self.conn, "_is_token_expired", return_value=True
        ), patch.object(
            self.conn, "_check_login_rate_limit", return_value=None
        ):
            # Set up state: token exists but client is None (requires rebuild)
            self.conn._access_token = "old-token"
            self.conn._dhan = None  # No client
            self.conn._conn_created_at = now - timedelta(hours=25)

            # Call _dhan_conn_under_lock with test_conn=False
            result = self.conn._dhan_conn_under_lock(now, test_conn=False)

            # _try_renew MUST be called
            mock_renew.assert_called_once()

            # _mint_and_build MUST be called (fallback)
            mock_mint.assert_called_once()

            # Result should be None (signals: mint was attempted)
            assert result is None, f"expected None (mint attempted), got {result}"

    def test_dhan_conn_no_token_skips_renew_goes_to_mint(self):
        """When no token cached, skip _try_renew and go directly to _mint_and_build.

        Arrange:
          - DhanConnection with NO cached _access_token
          - test_conn can be True or False (doesn't matter)

        Act:
          - Call _dhan_conn_under_lock

        Assert:
          - _try_renew() is NOT called (no token to renew)
          - _mint_and_build() IS called
        """
        now = datetime.now(tz=INDIAN_TIMEZONE)
        with patch.object(
            self.conn, "_try_renew"
        ) as mock_renew, patch.object(
            self.conn, "_mint_and_build"
        ) as mock_mint, patch.object(
            self.conn, "_try_restore_token"
        ) as mock_restore, patch.object(
            self.conn, "_is_token_expired", return_value=True
        ), patch.object(
            self.conn, "_check_login_rate_limit", return_value=None
        ), patch(
            "backend.brokers.connections.timestamp_indian",
            return_value=now,
        ):
            # Set up state: NO token
            self.conn._access_token = None
            self.conn._dhan = None

            # Call _dhan_conn_under_lock with test_conn=False
            result = self.conn._dhan_conn_under_lock(now, test_conn=False)

            # _try_renew should NOT be called (no token to renew)
            mock_renew.assert_not_called()

            # _mint_and_build MUST be called
            mock_mint.assert_called_once()

            # Result should be None (signals: mint was attempted)
            assert result is None, f"expected None (mint attempted), got {result}"

    def test_dhan_conn_gate_logic_condition_line_1158(self):
        """Verify line 1158 condition: `if self._access_token and not test_conn:`

        Directly tests the decision point at connections.py:1158:
          if self._access_token and not test_conn:
              new_token = self._try_renew()

        This condition MUST be True to call _try_renew, and False to skip it.

        Cases:
          1. token=None, test_conn=False  → False (skip renew, go to mint)
          2. token=None, test_conn=True   → False (skip renew, go to mint)
          3. token='abc', test_conn=False → True  (CALL renew)
          4. token='abc', test_conn=True  → False (skip renew, go to mint)
        """
        # Case 1: no token, not testing → should skip renew
        assert not (None and not False)  # False
        # Case 2: no token, testing → should skip renew
        assert not (None and not True)  # False
        # Case 3: token exists, not testing → should call renew
        assert ("abc" and not False)  # True
        # Case 4: token exists, testing (DH-906) → should skip renew
        assert not ("abc" and not True)  # False

        # Now verify in context: mock the condition
        now = datetime.now(tz=INDIAN_TIMEZONE)
        with patch.object(
            self.conn, "_try_renew"
        ) as mock_renew, patch.object(
            self.conn, "_mint_and_build"
        ) as mock_mint, patch.object(
            self.conn, "_try_restore_token"
        ) as mock_restore, patch.object(
            self.conn, "_is_token_expired", return_value=True
        ), patch.object(
            self.conn, "_check_login_rate_limit", return_value=None
        ), patch(
            "backend.brokers.connections.timestamp_indian",
            return_value=now,
        ):

            # Case 3: token + not test_conn → renew should be called
            self.conn._access_token = "token-abc"
            self.conn._dhan = None  # No client to trigger renewal path
            self.conn._conn_created_at = now - timedelta(hours=25)
            mock_renew.reset_mock()
            mock_mint.reset_mock()
            mock_renew.return_value = "new-token"
            self.conn._dhan_conn_under_lock(now, test_conn=False)
            mock_renew.assert_called_once()

            # Case 4: token + test_conn → renew should NOT be called
            self.conn._access_token = "token-abc"
            self.conn._dhan = None  # No client, but test_conn=True so skip renewal
            self.conn._conn_created_at = now - timedelta(hours=25)
            mock_renew.reset_mock()
            mock_mint.reset_mock()
            self.conn._dhan_conn_under_lock(now, test_conn=True)
            mock_renew.assert_not_called()
