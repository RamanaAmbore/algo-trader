"""Tests for Kite instruments cache and retry decorator robustness.

Covers three scenarios:
  1. Instruments cache hit — second call avoids HTTP round-trip
  2. retry_kite_conn exponential backoff — delays scale with attempt count
  3. retry_kite_conn 429 (rate-limit) — uses 30s sleep instead of exponential

Quality dimensions:
  SSOT        — test the cache dict + retry decorator directly
  Correctness — verify cache state is clean between tests; backoff matches spec
  Performance — no real broker I/O; mocked time.sleep
  Reuse       — shared setup/teardown helpers at module level
  UX          — each assert has clear message showing actual vs expected
"""

from __future__ import annotations

import time as _time
from unittest.mock import MagicMock, patch, call as mock_call

import pytest

from backend.shared.helpers.decorators import retry_kite_conn
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clear_kite_cache() -> None:
    """Clear the module-level instruments cache in the kite adapter.

    Called before each test to ensure cache isolation between tests.
    """
    from backend.brokers.adapters.kite import _INSTR_CACHE
    _INSTR_CACHE.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Instruments cache hit (no second HTTP call)
# ─────────────────────────────────────────────────────────────────────────────

class TestKiteInstrumentsCache:
    """Verify that KiteBroker.instruments() caches results per exchange.

    Second call to instruments(exchange) must return the cached result without
    invoking kite.instruments() again.
    """

    def setup_method(self):
        """Clear the cache before each test."""
        _clear_kite_cache()

    def test_instruments_cache_hit(self):
        """Second call to instruments(exchange) must use cache, not SDK."""
        from backend.brokers.adapters.kite import KiteBroker

        # Mock the underlying Kite SDK
        mock_kite = MagicMock()
        mock_instruments_response = [
            {"tradingsymbol": "RELIANCE", "exchange": "NSE", "lot_size": 1},
            {"tradingsymbol": "INFY", "exchange": "NSE", "lot_size": 1},
        ]
        mock_kite.instruments.return_value = mock_instruments_response

        # KiteBroker.__init__ takes a KiteConnection, not a raw kite handle.
        # Mock the connection object so get_kite_conn() returns mock_kite.
        mock_conn = MagicMock()
        mock_conn.account = "ZG0001"
        mock_conn.get_kite_conn.return_value = mock_kite

        # Create a KiteBroker with the mock connection
        broker = KiteBroker(conn=mock_conn)

        # First call — should hit the SDK
        result1 = broker.instruments("NSE")
        assert result1 == mock_instruments_response, (
            f"First call should return the mock response, got {result1}"
        )
        assert mock_kite.instruments.call_count == 1, (
            f"Expected 1 SDK call, got {mock_kite.instruments.call_count}"
        )

        # Second call with same exchange — should use cache
        result2 = broker.instruments("NSE")
        assert result2 == mock_instruments_response, (
            f"Second call should return cached response, got {result2}"
        )
        assert mock_kite.instruments.call_count == 1, (
            f"Expected still 1 SDK call (cache hit), got {mock_kite.instruments.call_count}"
        )

        # Call with different exchange — should hit SDK again
        mock_kite.instruments.return_value = [
            {"tradingsymbol": "NIFTY25JUN20000CE", "exchange": "NFO", "lot_size": 50},
        ]
        result3 = broker.instruments("NFO")
        assert mock_kite.instruments.call_count == 2, (
            f"Expected 2 SDK calls after different exchange, got {mock_kite.instruments.call_count}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: retry_kite_conn exponential backoff
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryKiteConnBackoff:
    """Verify that @retry_kite_conn implements exponential backoff delays.

    Delays should be [1, 2, 4, 8, ...] seconds for attempts 0, 1, 2, 3, ...
    (not counting the final attempt which raises). The decorator calls
    time.sleep between attempts.
    """

    def test_exponential_backoff_three_attempts(self, monkeypatch):
        """Three attempts should sleep [1, 2] seconds, then raise."""
        # Track sleep calls
        sleep_calls = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        monkeypatch.setattr("backend.shared.helpers.decorators.time.sleep", mock_sleep)

        # Create a function that always fails
        call_count = [0]

        @retry_kite_conn(max_attempts=3)
        def failing_func():
            call_count[0] += 1
            raise ValueError(f"Attempt {call_count[0]} failed")

        # Call and expect it to fail after 3 attempts
        with pytest.raises(ValueError):
            failing_func()

        assert call_count[0] == 3, (
            f"Expected 3 attempts, got {call_count[0]}"
        )
        # After attempts 0 and 1, sleep is called. Not after attempt 2 (final).
        assert sleep_calls == [1, 2], (
            f"Expected sleep calls [1, 2], got {sleep_calls}"
        )

    def test_retry_with_callable_max_attempts(self, monkeypatch):
        """max_attempts can be a callable; it's looked up on each call."""
        sleep_calls = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        monkeypatch.setattr("backend.shared.helpers.decorators.time.sleep", mock_sleep)

        # Callable that returns different values on each invocation
        max_attempts_fn = MagicMock(side_effect=[3, 3, 3])  # called 3 times
        call_count = [0]

        @retry_kite_conn(max_attempts=max_attempts_fn)
        def failing_func():
            call_count[0] += 1
            raise ValueError(f"Attempt {call_count[0]} failed")

        with pytest.raises(ValueError):
            failing_func()

        # max_attempts_fn is called once per invocation of the decorated function
        assert max_attempts_fn.call_count == 1, (
            f"Expected max_attempts() called once, got {max_attempts_fn.call_count}"
        )
        assert sleep_calls == [1, 2], (
            f"Expected sleep [1, 2], got {sleep_calls}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: retry_kite_conn 429 (rate-limit) uses 30s sleep
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryKiteConn429Sleep:
    """Verify that "Too many requests" (429 / rate-limit) errors use 30s sleep.

    When an exception message contains "Too many requests" (case-insensitive),
    the retry decorator should use a fixed 30-second sleep instead of
    exponential backoff.
    """

    def test_rate_limit_error_uses_30s_sleep(self, monkeypatch):
        """429-like errors should sleep 30s between attempts."""
        sleep_calls = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        monkeypatch.setattr("backend.shared.helpers.decorators.time.sleep", mock_sleep)

        call_count = [0]

        @retry_kite_conn(max_attempts=2)
        def rate_limited_func():
            call_count[0] += 1
            raise Exception("Too many requests")

        with pytest.raises(Exception):
            rate_limited_func()

        assert call_count[0] == 2, (
            f"Expected 2 attempts, got {call_count[0]}"
        )
        # Check that sleep was called with 30 (or >= 30 for rate-limit)
        assert len(sleep_calls) == 1, (
            f"Expected 1 sleep call, got {len(sleep_calls)}"
        )
        # The sleep call should be 30 or greater (at least 30s for rate-limit)
        assert sleep_calls[0] >= 30, (
            f"Expected sleep >= 30s for rate-limit, got {sleep_calls[0]}s"
        )

    def test_non_rate_limit_error_uses_exponential(self, monkeypatch):
        """Non-429 errors should still use exponential backoff."""
        sleep_calls = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        monkeypatch.setattr("backend.shared.helpers.decorators.time.sleep", mock_sleep)

        call_count = [0]

        @retry_kite_conn(max_attempts=3)
        def normal_error_func():
            call_count[0] += 1
            raise ConnectionError(f"Network issue {call_count[0]}")

        with pytest.raises(ConnectionError):
            normal_error_func()

        assert call_count[0] == 3, (
            f"Expected 3 attempts, got {call_count[0]}"
        )
        # Non-rate-limit errors should use exponential: [1, 2]
        assert sleep_calls == [1, 2], (
            f"Expected exponential backoff [1, 2], got {sleep_calls}"
        )

    def test_rate_limit_case_insensitive(self, monkeypatch):
        """Rate-limit check should be case-insensitive."""
        sleep_calls = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        monkeypatch.setattr("backend.shared.helpers.decorators.time.sleep", mock_sleep)

        @retry_kite_conn(max_attempts=2)
        def rate_limited_mixed_case():
            raise Exception("TOO MANY REQUESTS — slow down")

        with pytest.raises(Exception):
            rate_limited_mixed_case()

        # Should have triggered the 30s sleep path
        assert len(sleep_calls) == 1, (
            f"Expected 1 sleep call, got {len(sleep_calls)}"
        )
        assert sleep_calls[0] >= 30, (
            f"Expected sleep >= 30s for rate-limit, got {sleep_calls[0]}s"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test support: test_conn parameter
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryKiteConnTestConnParam:
    """Verify that @retry_kite_conn sets test_conn=True from 2nd attempt onwards.

    If the decorated function has a `test_conn` parameter in its signature,
    the decorator should inject test_conn=True starting from attempt 1 (2nd call).
    """

    def test_test_conn_injected_on_retry(self, monkeypatch):
        """Second attempt should inject test_conn=True."""
        sleep_calls = []
        test_conn_values = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        monkeypatch.setattr("backend.shared.helpers.decorators.time.sleep", mock_sleep)

        call_count = [0]

        @retry_kite_conn(max_attempts=2)
        def func_with_test_conn(test_conn=False):
            call_count[0] += 1
            test_conn_values.append(test_conn)
            if call_count[0] < 2:
                raise ValueError("First attempt fails")
            return "success"

        result = func_with_test_conn()

        assert result == "success", (
            f"Expected success, got {result}"
        )
        assert test_conn_values == [False, True], (
            f"Expected [False, True] for test_conn, got {test_conn_values}"
        )

    def test_test_conn_not_injected_for_functions_without_param(self, monkeypatch):
        """Functions without test_conn param should not error on injection attempt."""
        sleep_calls = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        monkeypatch.setattr("backend.shared.helpers.decorators.time.sleep", mock_sleep)

        call_count = [0]

        @retry_kite_conn(max_attempts=2)
        def func_without_test_conn():
            call_count[0] += 1
            if call_count[0] < 2:
                raise ValueError("First attempt fails")
            return "success"

        result = func_without_test_conn()

        assert result == "success", (
            f"Expected success, got {result}"
        )
        assert call_count[0] == 2, (
            f"Expected 2 calls, got {call_count[0]}"
        )
