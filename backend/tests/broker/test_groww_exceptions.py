"""
Tests for Groww exception handling in GrowwBroker._retry_groww_auth decorator
and inline entitlement-denied paths.

Five quality dimensions:
  SSOT        — single retry decorator; entitlement counter in groww.py only
  Correctness — each exception class routes to the correct branch
  Performance — mock sleep to verify backoff sequence without wall-clock delays
  Reuse       — record_entitlement_denied / get_entitlement_denied_snapshot reused
  UX          — health response exposes counters; entitlement → ok=True semantics

Scenario catalogue:
  1. Authentication → re-mint ONCE then retry
  2. Authorisation (entitlement) → log INFO, count, swallow (no re-mint)
  3. RateLimit → backoff 1/2/4/8 s, retry up to 4 times; raises after exhaustion
  4. RateLimit backoff sequence: mock sleep confirms 1/2/4/8 s (4 sleeps)
  5. Timeout → retry once (refresh_session best-effort); raises after 1 retry
  6. BadRequest → re-raise immediately (no retry, no re-mint)
  7. NotFound → re-raise immediately
  8. Entitlement counter increments correctly per account/segment
  9. get_entitlement_denied_snapshot returns deepcopy-safe dict
 10. broker-health mock includes entitlement counters
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch, call as mock_call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_broker(account: str = "GR87DF") -> "GrowwBroker":  # type: ignore[name-defined]
    """Create a GrowwBroker with a minimal mock _conn."""
    from backend.brokers.adapters.groww import GrowwBroker
    conn = MagicMock()
    conn.account = account
    conn._source_ip = None
    broker = GrowwBroker.__new__(GrowwBroker)
    broker._conn = conn
    return broker


def _reset_entitlement(account: str | None = None) -> None:
    from backend.brokers.adapters.groww import _entitlement_denied
    if account is None:
        _entitlement_denied.clear()
    else:
        _entitlement_denied.pop(account, None)


# ---------------------------------------------------------------------------
# Import the SDK exception classes (skip if not installed)
# ---------------------------------------------------------------------------

try:
    from growwapi.groww.exceptions import (
        GrowwAPIAuthenticationException,
        GrowwAPIAuthorisationException,
        GrowwAPIBadRequestException,
        GrowwAPINotFoundException,
        GrowwAPIRateLimitException,
        GrowwAPITimeoutException,
    )
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _SDK_AVAILABLE,
    reason="growwapi SDK not installed",
)


# ---------------------------------------------------------------------------
# 1. Authentication → re-mint ONCE then retry
# ---------------------------------------------------------------------------

class TestAuthenticationRetry:
    """GrowwAPIAuthenticationException triggers token re-mint then ONE retry."""

    ACCOUNT = "GR_test_authn"

    def setup_method(self):
        _reset_entitlement(self.ACCOUNT)

    def test_remint_called_on_authentication_error(self):
        broker = _make_broker(self.ACCOUNT)
        call_count = {"n": 0}

        def _fn(self_inner):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise GrowwAPIAuthenticationException()
            return "ok"

        with patch(
            "backend.brokers.connections._GROWW_SOURCE_IP_OVERRIDE",
            new=MagicMock(set=lambda x: None, reset=lambda t: None),
        ):
            from backend.brokers.adapters.groww import _retry_groww_auth
            wrapped = _retry_groww_auth(_fn)
            result = wrapped(broker)

        assert result == "ok"
        assert call_count["n"] == 2, "Should retry exactly once after re-mint"
        broker._conn.refresh.assert_called_once()

    def test_second_authentication_failure_propagates(self):
        """If re-mint doesn't fix it, the second attempt must raise."""
        broker = _make_broker(self.ACCOUNT)

        def _always_raise(self_inner):
            raise GrowwAPIAuthenticationException()

        with patch(
            "backend.brokers.connections._GROWW_SOURCE_IP_OVERRIDE",
            new=MagicMock(set=lambda x: None, reset=lambda t: None),
        ):
            from backend.brokers.adapters.groww import _retry_groww_auth
            wrapped = _retry_groww_auth(_always_raise)
            with pytest.raises(GrowwAPIAuthenticationException):
                wrapped(broker)


# ---------------------------------------------------------------------------
# 2. Authorisation (entitlement) → no re-mint, counter bumped
# ---------------------------------------------------------------------------

class TestAuthorisationEntitlement:
    """GrowwAPIAuthorisationException in _quote_batch_ohlc / ltp must bump
    the entitlement counter and return partial/empty results (no re-raise)."""

    ACCOUNT = "GR_test_authz"

    def setup_method(self):
        _reset_entitlement(self.ACCOUNT)

    def test_quote_batch_ohlc_authorisation_counts_and_returns_empty(self):
        broker = _make_broker(self.ACCOUNT)
        mock_sdk = MagicMock()
        mock_sdk.get_ohlc.side_effect = GrowwAPIAuthorisationException()
        broker._conn.get_groww_conn.return_value = mock_sdk

        result = broker._quote_batch_ohlc(["NSE:RELIANCE"])

        assert result == {}
        from backend.brokers.adapters.groww import get_entitlement_denied_snapshot
        snap = get_entitlement_denied_snapshot()
        assert self.ACCOUNT in snap
        total = sum(snap[self.ACCOUNT].values())
        assert total >= 1, "Expected at least one entitlement denial counted"

    def test_authorisation_does_not_call_refresh(self):
        broker = _make_broker(self.ACCOUNT)
        mock_sdk = MagicMock()
        mock_sdk.get_ohlc.side_effect = GrowwAPIAuthorisationException()
        broker._conn.get_groww_conn.return_value = mock_sdk

        broker._quote_batch_ohlc(["NSE:RELIANCE"])

        broker._conn.refresh.assert_not_called()

    def test_ltp_authorisation_counts_segment(self):
        broker = _make_broker(self.ACCOUNT)
        mock_sdk = MagicMock()
        mock_sdk.get_ltp.side_effect = GrowwAPIAuthorisationException()
        broker._conn.get_groww_conn.return_value = mock_sdk

        with patch(
            "backend.brokers.connections._GROWW_SOURCE_IP_OVERRIDE",
            new=MagicMock(set=lambda x: None, reset=lambda t: None),
        ):
            result = broker.ltp(["NSE:RELIANCE"])

        assert result == {}
        from backend.brokers.adapters.groww import get_entitlement_denied_snapshot
        snap = get_entitlement_denied_snapshot()
        assert self.ACCOUNT in snap


# ---------------------------------------------------------------------------
# 3 & 4. RateLimit → backoff sequence + exhaustion
# ---------------------------------------------------------------------------

class TestRateLimitBackoff:
    """GrowwAPIRateLimitException triggers exponential backoff 1/2/4/8s."""

    ACCOUNT = "GR_test_rl"

    def setup_method(self):
        _reset_entitlement(self.ACCOUNT)

    def test_ratelimit_retries_up_to_max_and_raises(self):
        """After 4 retries, the exception must propagate."""
        broker = _make_broker(self.ACCOUNT)
        call_count = {"n": 0}

        def _always_rate_limit(self_inner):
            call_count["n"] += 1
            raise GrowwAPIRateLimitException()

        with patch("backend.brokers.adapters.groww._time") as mock_time, \
             patch(
                 "backend.brokers.connections._GROWW_SOURCE_IP_OVERRIDE",
                 new=MagicMock(set=lambda x: None, reset=lambda t: None),
             ):
            mock_time.sleep = MagicMock()
            from backend.brokers.adapters.groww import _retry_groww_auth, _GROWW_RATE_LIMIT_MAX_RETRIES
            wrapped = _retry_groww_auth(_always_rate_limit)
            with pytest.raises(GrowwAPIRateLimitException):
                wrapped(broker)

        # First attempt + 4 retries = 5 calls
        assert call_count["n"] == 1 + _GROWW_RATE_LIMIT_MAX_RETRIES

    def test_ratelimit_backoff_sequence_1_2_4_8(self):
        """Backoff sleeps must follow 1/2/4/8 s (4 retries, capped at 30)."""
        broker = _make_broker(self.ACCOUNT)

        def _always_rate_limit(self_inner):
            raise GrowwAPIRateLimitException()

        sleep_calls: list[float] = []

        def _fake_sleep(s: float) -> None:
            sleep_calls.append(s)

        with patch("backend.brokers.adapters.groww._time") as mock_time, \
             patch(
                 "backend.brokers.connections._GROWW_SOURCE_IP_OVERRIDE",
                 new=MagicMock(set=lambda x: None, reset=lambda t: None),
             ):
            mock_time.sleep = _fake_sleep
            from backend.brokers.adapters.groww import _retry_groww_auth
            wrapped = _retry_groww_auth(_always_rate_limit)
            with pytest.raises(GrowwAPIRateLimitException):
                wrapped(broker)

        # 4 retries → 4 sleeps: 1, 2, 4, 8
        assert len(sleep_calls) == 4, f"Expected 4 sleeps, got {sleep_calls}"
        assert sleep_calls == [1.0, 2.0, 4.0, 8.0], (
            f"Expected backoff [1, 2, 4, 8], got {sleep_calls}"
        )

    def test_ratelimit_success_on_second_attempt_no_more_retries(self):
        """If the 2nd attempt succeeds, no further retries or sleeps."""
        broker = _make_broker(self.ACCOUNT)
        attempts = {"n": 0}

        def _fn(self_inner):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise GrowwAPIRateLimitException()
            return "recovered"

        sleep_calls: list[float] = []

        with patch("backend.brokers.adapters.groww._time") as mock_time, \
             patch(
                 "backend.brokers.connections._GROWW_SOURCE_IP_OVERRIDE",
                 new=MagicMock(set=lambda x: None, reset=lambda t: None),
             ):
            mock_time.sleep = lambda s: sleep_calls.append(s)
            from backend.brokers.adapters.groww import _retry_groww_auth
            wrapped = _retry_groww_auth(_fn)
            result = wrapped(broker)

        assert result == "recovered"
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 1.0  # only the first backoff sleep


# ---------------------------------------------------------------------------
# 5. Timeout → retry once
# ---------------------------------------------------------------------------

class TestTimeoutRetry:
    """GrowwAPITimeoutException triggers a single retry after refresh_session."""

    ACCOUNT = "GR_test_timeout"

    def test_timeout_retries_once_and_succeeds(self):
        broker = _make_broker(self.ACCOUNT)
        calls = {"n": 0}

        def _fn(self_inner):
            calls["n"] += 1
            if calls["n"] == 1:
                raise GrowwAPITimeoutException()
            return "ok"

        with patch(
            "backend.brokers.connections._GROWW_SOURCE_IP_OVERRIDE",
            new=MagicMock(set=lambda x: None, reset=lambda t: None),
        ):
            from backend.brokers.adapters.groww import _retry_groww_auth
            wrapped = _retry_groww_auth(_fn)
            result = wrapped(broker)

        assert result == "ok"
        assert calls["n"] == 2

    def test_timeout_retry_raises_after_second_timeout(self):
        """Two consecutive timeouts: the second one must propagate."""
        broker = _make_broker(self.ACCOUNT)

        def _always_timeout(self_inner):
            raise GrowwAPITimeoutException()

        with patch(
            "backend.brokers.connections._GROWW_SOURCE_IP_OVERRIDE",
            new=MagicMock(set=lambda x: None, reset=lambda t: None),
        ):
            from backend.brokers.adapters.groww import _retry_groww_auth
            wrapped = _retry_groww_auth(_always_timeout)
            with pytest.raises(GrowwAPITimeoutException):
                wrapped(broker)

    def test_timeout_refresh_session_failure_does_not_block_retry(self):
        """refresh_session is best-effort; even if it raises the retry fires."""
        broker = _make_broker(self.ACCOUNT)
        broker._conn.refresh_session.side_effect = RuntimeError("session error")
        calls = {"n": 0}

        def _fn(self_inner):
            calls["n"] += 1
            if calls["n"] == 1:
                raise GrowwAPITimeoutException()
            return "ok"

        with patch(
            "backend.brokers.connections._GROWW_SOURCE_IP_OVERRIDE",
            new=MagicMock(set=lambda x: None, reset=lambda t: None),
        ):
            from backend.brokers.adapters.groww import _retry_groww_auth
            wrapped = _retry_groww_auth(_fn)
            result = wrapped(broker)

        assert result == "ok"


# ---------------------------------------------------------------------------
# 6 & 7. BadRequest / NotFound → immediate re-raise
# ---------------------------------------------------------------------------

class TestImmediateRaise:
    """BadRequest (400) and NotFound (404) must not be retried."""

    ACCOUNT = "GR_test_bad"

    def test_bad_request_not_retried(self):
        broker = _make_broker(self.ACCOUNT)
        calls = {"n": 0}

        def _fn(self_inner):
            calls["n"] += 1
            raise GrowwAPIBadRequestException()

        with patch(
            "backend.brokers.connections._GROWW_SOURCE_IP_OVERRIDE",
            new=MagicMock(set=lambda x: None, reset=lambda t: None),
        ):
            from backend.brokers.adapters.groww import _retry_groww_auth
            wrapped = _retry_groww_auth(_fn)
            with pytest.raises(GrowwAPIBadRequestException):
                wrapped(broker)

        assert calls["n"] == 1, "BadRequest must not be retried"

    def test_not_found_not_retried(self):
        broker = _make_broker(self.ACCOUNT)
        calls = {"n": 0}

        def _fn(self_inner):
            calls["n"] += 1
            raise GrowwAPINotFoundException()

        with patch(
            "backend.brokers.connections._GROWW_SOURCE_IP_OVERRIDE",
            new=MagicMock(set=lambda x: None, reset=lambda t: None),
        ):
            from backend.brokers.adapters.groww import _retry_groww_auth
            wrapped = _retry_groww_auth(_fn)
            with pytest.raises(GrowwAPINotFoundException):
                wrapped(broker)

        assert calls["n"] == 1, "NotFound must not be retried"


# ---------------------------------------------------------------------------
# 8. Entitlement counter per-account/segment accuracy
# ---------------------------------------------------------------------------

class TestEntitlementCounter:
    """record_entitlement_denied must increment correctly per account/segment."""

    def setup_method(self):
        _reset_entitlement()

    def test_counter_increments_per_account_segment(self):
        from backend.brokers.adapters.groww import (
            record_entitlement_denied,
            get_entitlement_denied_snapshot,
        )
        record_entitlement_denied("GR87DF", "FNO")
        record_entitlement_denied("GR87DF", "FNO")
        record_entitlement_denied("GR87DF", "CASH")
        record_entitlement_denied("GR_OTHER", "COMMODITY")

        snap = get_entitlement_denied_snapshot()
        assert snap["GR87DF"]["FNO"] == 2
        assert snap["GR87DF"]["CASH"] == 1
        assert snap["GR_OTHER"]["COMMODITY"] == 1

    def test_counter_thread_safe(self):
        """100 concurrent increments → count must equal exactly 100."""
        from backend.brokers.adapters.groww import (
            record_entitlement_denied,
            get_entitlement_denied_snapshot,
        )
        ACCOUNT = "GR_thread_test"
        _reset_entitlement(ACCOUNT)

        def _incr():
            for _ in range(10):
                record_entitlement_denied(ACCOUNT, "CASH")

        threads = [threading.Thread(target=_incr) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = get_entitlement_denied_snapshot()
        assert snap[ACCOUNT]["CASH"] == 100

    def test_snapshot_is_independent_copy(self):
        """Modifying the snapshot must not affect the live counter."""
        from backend.brokers.adapters.groww import (
            record_entitlement_denied,
            get_entitlement_denied_snapshot,
        )
        record_entitlement_denied("GR_snap", "FNO")
        snap = get_entitlement_denied_snapshot()
        snap["GR_snap"]["FNO"] = 999  # mutate the snapshot copy

        # Live counter must be unaffected
        snap2 = get_entitlement_denied_snapshot()
        assert snap2["GR_snap"]["FNO"] == 1


# ---------------------------------------------------------------------------
# 9. Entitlement ok=True semantics — entitlement denial is not a health flip
# ---------------------------------------------------------------------------

class TestEntitlementHealthSemantics:
    """Entitlement denial must NOT cause broker_apis._record_fetch(ok=False).

    GrowwBroker._quote_batch_ohlc swallows GrowwAPIAuthorisationException and
    returns partial results — the broker call itself does not raise, so
    broker_apis never fires _record_fetch(ok=False) for this case."""

    def test_quote_batch_does_not_raise_on_authorisation(self):
        broker = _make_broker("GR_health_test")
        mock_sdk = MagicMock()
        mock_sdk.get_ohlc.side_effect = GrowwAPIAuthorisationException()
        broker._conn.get_groww_conn.return_value = mock_sdk

        # Must return normally (not raise)
        result = broker._quote_batch_ohlc(["NSE:RELIANCE"])
        assert isinstance(result, dict)

    def test_ltp_does_not_raise_on_authorisation(self):
        broker = _make_broker("GR_health_test2")
        mock_sdk = MagicMock()
        mock_sdk.get_ltp.side_effect = GrowwAPIAuthorisationException()
        broker._conn.get_groww_conn.return_value = mock_sdk

        with patch(
            "backend.brokers.connections._GROWW_SOURCE_IP_OVERRIDE",
            new=MagicMock(set=lambda x: None, reset=lambda t: None),
        ):
            result = broker.ltp(["NSE:RELIANCE"])
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# 10. broker-health response includes entitlement counters
# ---------------------------------------------------------------------------

class TestBrokerHealthEntitlement:
    """get_entitlement_denied_snapshot() feeds into /api/admin/broker-health."""

    def setup_method(self):
        _reset_entitlement()

    def test_entitlement_snapshot_exposed_by_groww_module(self):
        from backend.brokers.adapters.groww import (
            record_entitlement_denied,
            get_entitlement_denied_snapshot,
        )
        record_entitlement_denied("GR87DF", "FNO")
        record_entitlement_denied("GR87DF", "CASH")

        snap = get_entitlement_denied_snapshot()
        assert "GR87DF" in snap
        assert snap["GR87DF"].get("FNO", 0) >= 1
        assert snap["GR87DF"].get("CASH", 0) >= 1

    def test_broker_health_response_carries_entitlement_field(self):
        """BrokerHealthResponse must have a groww_entitlement_denied field."""
        from backend.api.routes.health import BrokerHealthResponse
        resp = BrokerHealthResponse(accounts=[], groww_entitlement_denied={})
        assert hasattr(resp, "groww_entitlement_denied")

    def test_broker_health_response_field_accepts_nested_dict(self):
        from backend.api.routes.health import BrokerHealthResponse
        data = {"GR87DF": {"FNO": 3, "CASH": 1}}
        resp = BrokerHealthResponse(accounts=[], groww_entitlement_denied=data)
        assert resp.groww_entitlement_denied == data
