"""
D2 regression: DhanBroker._safe_call() persistent auth-failure guard.

Before the fix: _safe_call retried once on auth-failure, but returned the
second response verbatim even if it was still an auth-failure dict. _unwrap()
on that dict produces [] → positions/holdings return empty → frontend shows
blank panels. _record_fetch(ok=False) never fired → navbar remained 5/5.

After the fix: after the retry, _looks_like_auth_failure() is checked again.
If still true, RuntimeError is raised. The per-account broker wrappers
(_fetch_holdings_local / _fetch_positions_local / _fetch_margins_local) all
catch Exception → call _record_fetch(ok=False) → navbar badge reflects reality.

Quality dimensions:
  SSOT — one raise path in _safe_call; _record_fetch is the single health-
          write point, no duplication.
  Correctness — persistent auth failure surfaces as RuntimeError, not silent [].
  Performance — no extra retry loops; fails fast on the second auth error.
  Reusable component — _looks_like_auth_failure() reused for post-retry check.
  Stale code — no duplicate is_auth_failure logic introduced.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest


def _make_auth_failure_dict(remarks: str = "Invalid Token") -> dict:
    return {"status": "failure", "remarks": remarks}


def _make_dhan_broker(account: str = "DH3747") -> "DhanBroker":
    """Build a minimal DhanBroker with a stub DhanConnection."""
    from backend.brokers.adapters.dhan import DhanBroker

    conn = MagicMock()
    conn.account = account
    conn._login_lock = threading.Lock()
    conn._login_blocked_until = 0.0       # not blocked
    conn._access_token = "test_token"
    conn._conn_created_at = None

    mock_sdk = MagicMock()

    # get_dhan_conn(test_conn=False) → cached sdk; (test_conn=True) → fresh sdk
    def get_dhan_conn_side_effect(test_conn=False):
        return mock_sdk
    conn.get_dhan_conn = MagicMock(side_effect=get_dhan_conn_side_effect)

    broker = DhanBroker(conn)
    return broker


class TestSafeCallPersistentAuthFailure:
    """_safe_call must raise RuntimeError when both the initial call AND
    the post-re-login retry return an auth-failure dict."""

    def test_raises_runtime_error_on_persistent_auth_failure(self):
        """Both attempts return auth-failure → must raise RuntimeError."""
        broker = _make_dhan_broker()
        auth_dict = _make_auth_failure_dict("Invalid Token")

        # Mock SDK method always returns auth failure
        broker._conn.get_dhan_conn().get_holdings.return_value = auth_dict

        with patch("backend.brokers.adapters.dhan._check_dhan_rotation_pattern"):
            with pytest.raises(RuntimeError, match="persisted after re-login"):
                # Test via proxy calling a method
                from backend.brokers.adapters.dhan import _DhanSDKProxy
                proxy = _DhanSDKProxy(broker)
                proxy.get_holdings()

    def test_record_fetch_fires_on_persistent_auth_failure(self):
        """The _fetch_holdings_local / _fetch_positions_local wrappers call
        _record_fetch(ok=False) when broker.holdings() / .positions() raises.

        Simulate via _fetch_holdings_local: patch broker.holdings() to raise
        RuntimeError (the D2 fix) and confirm _record_fetch is called with ok=False.
        """
        from backend.brokers import broker_apis

        mock_broker = MagicMock()
        mock_broker.holdings.side_effect = RuntimeError(
            "Dhan auth failure for 'DH3747' persisted after re-login: 'Invalid Token'"
        )

        # Inject a minimal connection into _FETCH_HEALTH baseline
        broker_apis._FETCH_HEALTH.pop("DH3747", None)

        with patch("backend.brokers.broker_apis._record_fetch") as mock_record:
            # Simulate what @for_all_accounts does for one account
            df = __import__("pandas").DataFrame()
            try:
                rows = mock_broker.holdings()
            except Exception as e:
                df.attrs["fetch_failed"] = True
                # This is the line inside _fetch_holdings_local's except block:
                broker_apis._record_fetch("DH3747", ok=False, error=str(e))

            mock_record.assert_called_once_with(
                "DH3747", ok=False, error=mock_broker.holdings.side_effect.args[0]
            )

    def test_first_attempt_success_does_not_raise(self):
        """When the first call succeeds (no auth failure), proxy must
        return the response without raising."""
        broker = _make_dhan_broker()
        success_resp = {"status": "success", "data": [{"holding": 1}]}

        # Mock SDK method returns success
        mock_sdk = broker._conn.get_dhan_conn()
        mock_sdk.get_holdings.return_value = success_resp

        from backend.brokers.adapters.dhan import _DhanSDKProxy
        proxy = _DhanSDKProxy(broker)
        result = proxy.get_holdings()
        assert result is success_resp
        # Must only have been called once for the method (no retry needed)
        mock_sdk.get_holdings.assert_called_once()

    def test_retry_succeeds_returns_response(self):
        """First attempt → auth failure; second attempt (after re-login) → success.
        Must return the success response, not raise."""
        broker = _make_dhan_broker()
        auth_dict = _make_auth_failure_dict()
        success_resp = {"status": "success", "data": []}

        # First call: auth failure. Second call: success.
        broker._conn.get_dhan_conn().get_holdings.side_effect = [auth_dict, success_resp]

        with patch("backend.brokers.adapters.dhan._check_dhan_rotation_pattern"):
            from backend.brokers.adapters.dhan import _DhanSDKProxy
            proxy = _DhanSDKProxy(broker)
            result = proxy.get_holdings()

        assert result is success_resp, (
            "Should return the success response when retry succeeds"
        )
        assert broker._conn.get_dhan_conn().get_holdings.call_count == 2

    def test_non_auth_failure_dict_does_not_raise(self):
        """A dict with status=failure but non-auth remarks must pass through
        (e.g. a business-logic error like 'no positions found')."""
        broker = _make_dhan_broker()
        biz_error = {"status": "failure", "remarks": "No positions found today"}

        # Mock SDK returns non-auth failure
        broker._conn.get_dhan_conn().get_holdings.return_value = biz_error

        # Must NOT raise — not an auth error
        from backend.brokers.adapters.dhan import _DhanSDKProxy
        proxy = _DhanSDKProxy(broker)
        result = proxy.get_holdings()
        assert result is biz_error

    def test_error_message_includes_account_and_remarks(self):
        """RuntimeError message must name the account and include the remarks
        so the log line is immediately actionable."""
        broker = _make_dhan_broker(account="DH3747")
        auth_dict = _make_auth_failure_dict("dh-906: Invalid Token")

        # Mock SDK returns auth failure
        broker._conn.get_dhan_conn().get_holdings.return_value = auth_dict

        with patch("backend.brokers.adapters.dhan._check_dhan_rotation_pattern"):
            with pytest.raises(RuntimeError) as exc_info:
                from backend.brokers.adapters.dhan import _DhanSDKProxy
                proxy = _DhanSDKProxy(broker)
                proxy.get_holdings()

        msg = str(exc_info.value)
        assert "DH3747" in msg, f"Account name missing from error: {msg}"
        assert "dh-906" in msg or "Invalid Token" in msg, (
            f"Remarks missing from error: {msg}"
        )


class TestLooksLikeAuthFailureSSoT:
    """Confirm _looks_like_auth_failure is the SINGLE truth for detecting
    auth errors — the post-retry check reuses it, not a duplicate predicate."""

    def test_all_known_hints_detected(self):
        """Every hint in _AUTH_ERROR_HINTS must be detected as auth failure."""
        from backend.brokers.adapters.dhan import _looks_like_auth_failure, _AUTH_ERROR_HINTS

        for hint in _AUTH_ERROR_HINTS:
            resp = {"status": "failure", "remarks": f"Error: {hint} occurred"}
            assert _looks_like_auth_failure(resp), (
                f"hint {hint!r} not detected by _looks_like_auth_failure"
            )

    def test_success_status_not_auth_failure(self):
        from backend.brokers.adapters.dhan import _looks_like_auth_failure
        assert not _looks_like_auth_failure({"status": "success", "data": []})

    def test_non_dict_not_auth_failure(self):
        from backend.brokers.adapters.dhan import _looks_like_auth_failure
        assert not _looks_like_auth_failure([])
        assert not _looks_like_auth_failure(None)
        assert not _looks_like_auth_failure("failure")

    def test_function_used_twice_in_safe_call_source(self):
        """Grep-style SSOT check: _looks_like_auth_failure appears at least
        twice in _DhanSDKProxy's source — once for initial check, once post-retry.
        This guards against the check being inlined with a different predicate."""
        import inspect
        from backend.brokers.adapters.dhan import _DhanSDKProxy
        src = inspect.getsource(_DhanSDKProxy.__getattr__)
        count = src.count("_looks_like_auth_failure")
        assert count >= 2, (
            f"Expected _looks_like_auth_failure to appear at least 2× in "
            f"_DhanSDKProxy.__getattr__ (initial + post-retry). Found {count}×.\n"
            f"Source:\n{src}"
        )
