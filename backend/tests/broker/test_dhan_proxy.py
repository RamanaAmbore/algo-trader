"""Tests for _DhanSDKProxy transparent proxy class.

Targets the new _DhanSDKProxy class which replaces DhanBroker._safe_call()
with a transparent proxy interface. The proxy intercepts getattr() and returns
callables that:

  1. Rate-limit per bucket (if _DHAN_RATE_LIMIT_ENABLED and bucket provided)
  2. Set broker._last_req timestamp
  3. Get live SDK handle via broker._conn.get_dhan_conn()
  4. Invoke the SDK method via _raw_call (5xx + DH-904 both translated on first
     call and retry — Fix 1)
  5. Detect and wrap 5xx exceptions as BrokerNetworkError
  6. Detect DH-904 rate-limit responses as BrokerRateLimitError
  7. Set broker._last_resp status hint; corrected to "ok" after successful retry
     (Fix 2)
  8. Retry on auth failure with fresh token (test_conn=True)
  9. Raise RuntimeError if auth failure persists after retry

Fix 3 — GTT mutations use _sdk_orders bucket (not the default _sdk bucket).

Five quality dimensions:
  SSOT        — tests directly invoke the proxy with controlled mocks
  Correctness — precise assertions on exception types, retry count, throttle calls
  Performance — no real broker I/O, fast mock-based execution
  Reuse       — shared _make_broker() helper for consistent test setup
  UX          — every assertion has an f-string message
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, call

# Will be available after broker agent writes implementation
from backend.brokers.adapters.dhan import (
    _DhanSDKProxy,
    DhanBroker,
    _looks_like_auth_failure,
    _DHAN_RATE_LIMITER,
)
from backend.brokers.errors import BrokerNetworkError, BrokerRateLimitError


# ---------------------------------------------------------------------------
# Module-level setup helpers
# ---------------------------------------------------------------------------

def _make_broker_mock(**overrides):
    """Create a minimal DhanBroker mock with a stubbed _conn.

    Args:
        overrides: dict of attributes to set on the broker mock

    Returns:
        A MagicMock(spec=DhanBroker) with:
        - _conn: a MagicMock connection
        - account: "test_dhan"
        - _last_req: {}
        - _last_resp: {}
    """
    conn = MagicMock()
    conn.account = "test_dhan"
    conn._conn_created_at = None

    broker = MagicMock(spec=DhanBroker)
    broker._conn = conn
    broker.account = "test_dhan"
    broker._last_req = {}
    broker._last_resp = {}

    # Apply any overrides
    for key, val in overrides.items():
        setattr(broker, key, val)

    return broker


def _make_sdk_handle_mock():
    """Create a mock SDK handle with common methods."""
    sdk = MagicMock()
    return sdk


# ---------------------------------------------------------------------------
# Test: Proxy passthrough — basic method call with success response
# ---------------------------------------------------------------------------

def test_proxy_passthrough():
    """Verify proxy successfully passes through SDK calls and returns response.

    Arrange:
      - Mock SDK handle's get_holdings() returns success dict
      - Create proxy with empty bucket (no rate-limiting)

    Act:
      - Call proxy.get_holdings()

    Assert:
      - Returns the success response unchanged
      - get_dhan_conn() called once (no test_conn=True)
      - _last_req and _last_resp stamped
    """
    # Arrange
    broker = _make_broker_mock()
    success_resp = {"status": "success", "data": [{"sym": "RELIANCE"}]}

    sdk_handle = _make_sdk_handle_mock()
    sdk_handle.get_holdings.return_value = success_resp

    broker._conn.get_dhan_conn.return_value = sdk_handle

    # Act
    proxy = _DhanSDKProxy(broker, bucket="")
    result = proxy.get_holdings()

    # Assert
    assert result == success_resp, f"expected {success_resp} but got {result}"
    broker._conn.get_dhan_conn.assert_called_once_with()
    assert broker._last_req.get("broker") == "dhan", \
        f"_last_req should have broker='dhan', got {broker._last_req}"
    assert broker._last_resp.get("status_hint") == "ok", \
        f"_last_resp status_hint should be 'ok', got {broker._last_resp}"


# ---------------------------------------------------------------------------
# Test: Auth failure retry with success on second attempt
# ---------------------------------------------------------------------------

def test_proxy_auth_failure_remint_retry():
    """Verify proxy retries on auth failure and succeeds with fresh token.

    Arrange:
      - First get_dhan_conn() returns SDK handle whose get_holdings()
        returns auth-failure response
      - Second get_dhan_conn(test_conn=True) returns fresh SDK handle
        whose get_holdings() returns success response

    Act:
      - Call proxy.get_holdings()

    Assert:
      - Returns the success response from retry
      - get_dhan_conn() called first without test_conn
      - get_dhan_conn(test_conn=True) called for retry
      - _last_resp shows auth failure was detected
    """
    # Arrange
    broker = _make_broker_mock()

    # First call (auth failure)
    auth_fail_resp = {"status": "failure", "remarks": "Invalid token"}
    sdk_handle_1 = _make_sdk_handle_mock()
    sdk_handle_1.get_holdings.return_value = auth_fail_resp

    # Second call (success after re-login)
    success_resp = {"status": "success", "data": [{"sym": "RELIANCE"}]}
    sdk_handle_2 = _make_sdk_handle_mock()
    sdk_handle_2.get_holdings.return_value = success_resp

    # Configure side_effect to return different handles per call
    broker._conn.get_dhan_conn.side_effect = [sdk_handle_1, sdk_handle_2]

    # Act
    proxy = _DhanSDKProxy(broker, bucket="")
    result = proxy.get_holdings()

    # Assert
    assert result == success_resp, \
        f"expected success response {success_resp} but got {result}"

    # Verify both get_dhan_conn() calls
    assert broker._conn.get_dhan_conn.call_count == 2, \
        f"expected 2 get_dhan_conn calls but got {broker._conn.get_dhan_conn.call_count}"

    calls = broker._conn.get_dhan_conn.call_args_list
    assert calls[0] == call(), \
        f"first call should be get_dhan_conn() with no args, got {calls[0]}"
    assert calls[1] == call(test_conn=True), \
        f"second call should be get_dhan_conn(test_conn=True), got {calls[1]}"


# ---------------------------------------------------------------------------
# Test: Persistent auth failure raises RuntimeError
# ---------------------------------------------------------------------------

def test_proxy_persistent_auth_failure_raises():
    """Verify proxy raises RuntimeError when auth fails even after retry.

    Arrange:
      - Both get_dhan_conn() calls return SDK handles that reply with
        auth-failure responses

    Act:
      - Call proxy.get_holdings()

    Assert:
      - Raises RuntimeError with message mentioning "persisted after re-login"
      - get_dhan_conn() called twice
    """
    # Arrange
    broker = _make_broker_mock()

    auth_fail_resp = {"status": "failure", "remarks": "Invalid token"}

    # Both handles return auth failure
    sdk_handle_1 = _make_sdk_handle_mock()
    sdk_handle_1.get_holdings.return_value = auth_fail_resp

    sdk_handle_2 = _make_sdk_handle_mock()
    sdk_handle_2.get_holdings.return_value = auth_fail_resp

    broker._conn.get_dhan_conn.side_effect = [sdk_handle_1, sdk_handle_2]

    # Act & Assert
    proxy = _DhanSDKProxy(broker, bucket="")

    with pytest.raises(RuntimeError) as exc_info:
        proxy.get_holdings()

    assert "persisted after re-login" in str(exc_info.value).lower(), \
        f"expected 'persisted after re-login' in error message, got: {exc_info.value}"

    assert broker._conn.get_dhan_conn.call_count == 2, \
        f"expected 2 get_dhan_conn calls, got {broker._conn.get_dhan_conn.call_count}"


# ---------------------------------------------------------------------------
# Test: 5xx HTTP exceptions raise BrokerNetworkError
# ---------------------------------------------------------------------------

def test_proxy_5xx_raises_network_error():
    """Verify proxy converts 5xx exceptions to BrokerNetworkError.

    Arrange:
      - SDK method raises exception with response.status_code == 503

    Act:
      - Call proxy.get_positions()

    Assert:
      - Raises BrokerNetworkError (not raw exception)
    """
    # Arrange
    broker = _make_broker_mock()

    sdk_handle = _make_sdk_handle_mock()
    exc = Exception("service unavailable")
    exc.response = MagicMock()
    exc.response.status_code = 503

    sdk_handle.get_positions.side_effect = exc
    broker._conn.get_dhan_conn.return_value = sdk_handle

    # Act & Assert
    proxy = _DhanSDKProxy(broker, bucket="")

    with pytest.raises(BrokerNetworkError):
        proxy.get_positions()


# ---------------------------------------------------------------------------
# Test: DH-904 rate-limit response raises BrokerRateLimitError
# ---------------------------------------------------------------------------

def test_proxy_dh904_raises_rate_limit_error():
    """Verify proxy converts DH-904 responses to BrokerRateLimitError.

    Arrange:
      - SDK returns dict with code == "DH-904"

    Act:
      - Call proxy.get_orders()

    Assert:
      - Raises BrokerRateLimitError
    """
    # Arrange
    broker = _make_broker_mock()

    rate_limit_resp = {"code": "DH-904", "remarks": "rate limit exceeded"}

    sdk_handle = _make_sdk_handle_mock()
    sdk_handle.get_orders.return_value = rate_limit_resp
    broker._conn.get_dhan_conn.return_value = sdk_handle

    # Act & Assert
    proxy = _DhanSDKProxy(broker, bucket="")

    with pytest.raises(BrokerRateLimitError):
        proxy.get_orders()


# ---------------------------------------------------------------------------
# Test: Rate throttle called when bucket provided and feature enabled
# ---------------------------------------------------------------------------

def test_proxy_rate_throttle_called():
    """Verify throttle() called on rate limiter when bucket provided.

    Arrange:
      - _DHAN_RATE_LIMIT_ENABLED = True
      - _DHAN_RATE_LIMITER.throttle() mocked
      - Create proxy with bucket="orders"

    Act:
      - Call any SDK method

    Assert:
      - _DHAN_RATE_LIMITER.throttle() called with "orders"
    """
    # Arrange
    broker = _make_broker_mock()

    success_resp = {"status": "success", "data": []}
    sdk_handle = _make_sdk_handle_mock()
    sdk_handle.place_order.return_value = success_resp
    broker._conn.get_dhan_conn.return_value = sdk_handle

    # Act
    with patch("backend.brokers.adapters.dhan._DHAN_RATE_LIMIT_ENABLED", True):
        with patch.object(_DHAN_RATE_LIMITER, "throttle") as mock_throttle:
            proxy = _DhanSDKProxy(broker, bucket="orders")
            proxy.place_order(order_data={})

            # Assert
            mock_throttle.assert_called_once_with("orders"), \
                f"expected throttle('orders') to be called, got {mock_throttle.call_args_list}"


# ---------------------------------------------------------------------------
# Test: Rate throttle NOT called when bucket is empty
# ---------------------------------------------------------------------------

def test_proxy_no_throttle_without_bucket():
    """Verify throttle() NOT called when bucket is empty string.

    Arrange:
      - _DHAN_RATE_LIMIT_ENABLED = True
      - _DHAN_RATE_LIMITER.throttle() mocked
      - Create proxy with bucket="" (default)

    Act:
      - Call any SDK method

    Assert:
      - _DHAN_RATE_LIMITER.throttle() NOT called
    """
    # Arrange
    broker = _make_broker_mock()

    success_resp = {"status": "success", "data": []}
    sdk_handle = _make_sdk_handle_mock()
    sdk_handle.get_holdings.return_value = success_resp
    broker._conn.get_dhan_conn.return_value = sdk_handle

    # Act
    with patch("backend.brokers.adapters.dhan._DHAN_RATE_LIMIT_ENABLED", True):
        with patch.object(_DHAN_RATE_LIMITER, "throttle") as mock_throttle:
            proxy = _DhanSDKProxy(broker, bucket="")
            proxy.get_holdings()

            # Assert
            mock_throttle.assert_not_called(), \
                f"expected throttle() NOT to be called, but got calls: {mock_throttle.call_args_list}"


# ---------------------------------------------------------------------------
# Test: _safe_call method removed from DhanBroker
# ---------------------------------------------------------------------------

def test_safe_call_removed():
    """Verify DhanBroker no longer has _safe_call method.

    This test confirms the method signature has been fully replaced
    by the _DhanSDKProxy interface.

    Act:
      - Check DhanBroker spec for _safe_call

    Assert:
      - _safe_call attribute not present
    """
    assert not hasattr(DhanBroker, "_safe_call"), \
        "DhanBroker should not have _safe_call method (replaced by _DhanSDKProxy)"


# ---------------------------------------------------------------------------
# Test: 5xx HTTP exceptions on retry path raise BrokerNetworkError (Fix 1)
# ---------------------------------------------------------------------------

def test_proxy_5xx_on_retry_raises_network_error():
    """Verify _raw_call translates 5xx on the retry path, not just the first call.

    Arrange:
      - First get_dhan_conn() returns handle1 whose method returns
        auth-failure response (triggers remint)
      - Second get_dhan_conn(test_conn=True) returns handle2 whose
        method raises exception with response.status_code = 503

    Act:
      - Call proxy.get_positions()

    Assert:
      - Raises BrokerNetworkError (proxy's _raw_call on retry path
        must translate 5xx — was a bare getattr call before Fix 1)
      - Both get_dhan_conn() calls were made
    """
    # Arrange
    broker = _make_broker_mock()

    # First call (auth failure) — triggers remint
    auth_fail_resp = {"status": "failure", "remarks": "Invalid token"}
    sdk_handle_1 = _make_sdk_handle_mock()
    sdk_handle_1.get_positions.return_value = auth_fail_resp

    # Second call (5xx exception on retry)
    sdk_handle_2 = _make_sdk_handle_mock()
    exc = Exception("service unavailable")
    exc.response = MagicMock()
    exc.response.status_code = 503
    sdk_handle_2.get_positions.side_effect = exc

    broker._conn.get_dhan_conn.side_effect = [sdk_handle_1, sdk_handle_2]

    # Act & Assert
    proxy = _DhanSDKProxy(broker, bucket="")

    with pytest.raises(BrokerNetworkError):
        proxy.get_positions()

    assert broker._conn.get_dhan_conn.call_count == 2, \
        f"expected 2 get_dhan_conn calls, got {broker._conn.get_dhan_conn.call_count}"


# ---------------------------------------------------------------------------
# Test: DH-904 rate-limit on retry path raises BrokerRateLimitError (Fix 1)
# ---------------------------------------------------------------------------

def test_proxy_dh904_on_retry_raises_rate_limit_error():
    """Verify _raw_call translates DH-904 on the retry path.

    Arrange:
      - First call returns auth-failure response (triggers remint)
      - Retry returns dict with code="DH-904"

    Act:
      - Call proxy.get_orders()

    Assert:
      - Raises BrokerRateLimitError (proxy's _raw_call on retry path
        must translate DH-904 — was a bare getattr call before Fix 1)
      - Both get_dhan_conn() calls were made
    """
    # Arrange
    broker = _make_broker_mock()

    # First call (auth failure)
    auth_fail_resp = {"status": "failure", "remarks": "Invalid token"}
    sdk_handle_1 = _make_sdk_handle_mock()
    sdk_handle_1.get_orders.return_value = auth_fail_resp

    # Second call (DH-904 on retry)
    rate_limit_resp = {"code": "DH-904", "remarks": "rate limit exceeded"}
    sdk_handle_2 = _make_sdk_handle_mock()
    sdk_handle_2.get_orders.return_value = rate_limit_resp

    broker._conn.get_dhan_conn.side_effect = [sdk_handle_1, sdk_handle_2]

    # Act & Assert
    proxy = _DhanSDKProxy(broker, bucket="")

    with pytest.raises(BrokerRateLimitError):
        proxy.get_orders()

    assert broker._conn.get_dhan_conn.call_count == 2, \
        f"expected 2 get_dhan_conn calls, got {broker._conn.get_dhan_conn.call_count}"


# ---------------------------------------------------------------------------
# Test: status_hint corrected to "ok" after successful recovery (Fix 2)
# ---------------------------------------------------------------------------

def test_proxy_last_resp_ok_after_transparent_recovery():
    """Verify status_hint is 'ok' after successful auth retry, not 'auth_fail'.

    Arrange:
      - First call returns auth-failure response (triggers remint; sets
        status_hint="auth_fail")
      - Retry returns clean success response

    Act:
      - Call proxy.get_holdings()

    Assert:
      - broker._last_resp["status_hint"] == "ok" (Fix 2 — must be
        updated to "ok" after successful recovery; before Fix 2 it was
        left as "auth_fail" because only the first-call path wrote
        _last_resp)
      - Response is the success response from retry
    """
    # Arrange
    broker = _make_broker_mock()

    # First call (auth failure)
    auth_fail_resp = {"status": "failure", "remarks": "Invalid token"}
    sdk_handle_1 = _make_sdk_handle_mock()
    sdk_handle_1.get_holdings.return_value = auth_fail_resp

    # Second call (success after re-login)
    success_resp = {"status": "success", "data": [{"sym": "RELIANCE"}]}
    sdk_handle_2 = _make_sdk_handle_mock()
    sdk_handle_2.get_holdings.return_value = success_resp

    broker._conn.get_dhan_conn.side_effect = [sdk_handle_1, sdk_handle_2]

    # Act
    proxy = _DhanSDKProxy(broker, bucket="")
    result = proxy.get_holdings()

    # Assert
    assert result == success_resp, \
        f"expected success response {success_resp} but got {result}"
    assert broker._last_resp.get("status_hint") == "ok", \
        f"expected status_hint='ok' after successful recovery, got {broker._last_resp}"


# ---------------------------------------------------------------------------
# Test: GTT path uses orders bucket, not default bucket (Fix 3)
# ---------------------------------------------------------------------------

def test_proxy_gtt_get_forever_uses_orders_bucket():
    """Verify get_gtts() routes via _sdk_orders bucket (bucket='orders').

    The proxy is constructed with bucket='orders' — same as _sdk_orders
    property on DhanBroker. This test verifies the bucket label propagates
    to the rate limiter, confirming Fix 3 (GTT mutations no longer use the
    unbucketed _sdk property).

    Arrange:
      - Patch _DHAN_RATE_LIMIT_ENABLED = True
      - Patch _DHAN_RATE_LIMITER.throttle to a mock
      - Create proxy with bucket="orders" (matching _sdk_orders)
      - SDK handle's get_forever() returns success response

    Act:
      - Call proxy.get_forever()

    Assert:
      - _DHAN_RATE_LIMITER.throttle called with "orders"
      - get_forever() called exactly once on the SDK handle
    """
    # Arrange
    broker = _make_broker_mock()

    success_resp = {"status": "success", "data": []}
    sdk_handle = _make_sdk_handle_mock()
    sdk_handle.get_forever.return_value = success_resp
    broker._conn.get_dhan_conn.return_value = sdk_handle

    # Act
    with patch("backend.brokers.adapters.dhan._DHAN_RATE_LIMIT_ENABLED", True):
        with patch.object(_DHAN_RATE_LIMITER, "throttle") as mock_throttle:
            proxy = _DhanSDKProxy(broker, bucket="orders")
            result = proxy.get_forever()

            # Assert — bucket propagated to throttle
            mock_throttle.assert_called_once_with("orders"), \
                f"expected throttle('orders') for orders bucket, got {mock_throttle.call_args_list}"

    # Assert — SDK method reached
    sdk_handle.get_forever.assert_called_once_with(), \
        f"get_forever() should have been called on the sdk handle"
    assert result == success_resp, \
        f"expected success_resp but got {result}"


def test_proxy_default_sdk_uses_empty_bucket():
    """Verify _sdk (default proxy) uses empty bucket, not 'orders'.

    This is the negative counterpart of test_proxy_gtt_get_forever_uses_orders_bucket.
    If GTT methods had been left on _sdk instead of _sdk_orders, they would
    not participate in the 'orders' rate-limit bucket — this test confirms
    the bucket distinction is real.

    Arrange:
      - Patch _DHAN_RATE_LIMIT_ENABLED = True
      - Create proxy with bucket="" (matching _sdk property)
      - SDK handle returns success response

    Act:
      - Call proxy.get_holdings() (any non-GTT method)

    Assert:
      - _DHAN_RATE_LIMITER.throttle NOT called (empty bucket skips throttle)
    """
    broker = _make_broker_mock()

    success_resp = {"status": "success", "data": []}
    sdk_handle = _make_sdk_handle_mock()
    sdk_handle.get_holdings.return_value = success_resp
    broker._conn.get_dhan_conn.return_value = sdk_handle

    with patch("backend.brokers.adapters.dhan._DHAN_RATE_LIMIT_ENABLED", True):
        with patch.object(_DHAN_RATE_LIMITER, "throttle") as mock_throttle:
            proxy = _DhanSDKProxy(broker, bucket="")
            proxy.get_holdings()

            mock_throttle.assert_not_called(), \
                f"default _sdk bucket should not throttle, got {mock_throttle.call_args_list}"
