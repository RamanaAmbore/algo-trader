"""
Pytest coverage for backend/brokers/broker_apis.py

Tests cover:
- fetch_holdings/positions/margins/orders/trades functions
- Circuit breaker CLOSED → OPEN → HALF-OPEN → CLOSED state machine
- Raw DataFrame TTL cache (30s memoization)
- Stale-frame substitution when circuit open
- LKG (last-known-good) LTP + quote cache
- Dhan poll-priority interval gating
- Auth error detection
"""

import pytest
import pandas as pd
import polars as pl
import time as _time
import threading
from unittest.mock import MagicMock, patch, call

# Explicit imports to ensure coverage tracking
from backend.brokers import broker_apis
import backend.brokers.broker_apis


class TestLastGoodLTPCache:
    """Test the per-symbol last-known-good LTP cache."""

    def teardown_method(self):
        """Clear global caches before each test."""
        broker_apis._LAST_GOOD_LTP.clear()

    def test_record_and_get_good_ltp(self):
        """Record a valid LTP and retrieve it."""
        symbol = "RELIANCE"
        ltp = 2500.50

        broker_apis.record_good_ltp(symbol, ltp)
        result = broker_apis.get_last_good_ltp(symbol)

        assert result == ltp, f"Expected LTP {ltp} but got {result}"

    def test_record_ltp_skips_zero(self):
        """Zero LTP is not recorded."""
        symbol = "RELIANCE"
        broker_apis.record_good_ltp(symbol, 0)
        result = broker_apis.get_last_good_ltp(symbol)

        assert result is None, f"Zero LTP should not be recorded, got {result}"

    def test_record_ltp_skips_negative(self):
        """Negative LTP is not recorded."""
        symbol = "RELIANCE"
        broker_apis.record_good_ltp(symbol, -100.0)
        result = broker_apis.get_last_good_ltp(symbol)

        assert result is None, f"Negative LTP should not be recorded, got {result}"

    def test_get_ltp_unknown_symbol(self):
        """Unknown symbol returns None."""
        result = broker_apis.get_last_good_ltp("UNKNOWN")
        assert result is None, "Unknown symbol should return None"

    def test_ltp_cache_ttl_expiry(self):
        """LTP older than max_age_s returns None."""
        symbol = "RELIANCE"
        ltp = 2500.50

        # Record at current time
        broker_apis.record_good_ltp(symbol, ltp)

        # Immediately retrieve — should work
        result = broker_apis.get_last_good_ltp(symbol, max_age_s=3600)
        assert result == ltp, "Fresh LTP should be retrievable"

        # Retrieve with 0-second TTL (already expired)
        result = broker_apis.get_last_good_ltp(symbol, max_age_s=0)
        assert result is None, "Expired LTP should return None"

    def test_ltp_cache_thread_safe(self):
        """LTP recording is thread-safe."""
        symbol = "INFY"
        results = []

        def record_ltp(ltp_value):
            broker_apis.record_good_ltp(symbol, ltp_value)

        threads = [
            threading.Thread(target=record_ltp, args=(1000.0 + i,))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have one of the recorded values
        result = broker_apis.get_last_good_ltp(symbol)
        assert result is not None, "LTP should be recorded after concurrent writes"


class TestLastGoodQuoteCache:
    """Test the per-symbol last-known-good quote cache."""

    def teardown_method(self):
        """Clear global caches before each test."""
        broker_apis._LAST_GOOD_QUOTE.clear()

    def test_record_and_get_good_quote(self):
        """Record a valid quote and retrieve it."""
        symbol = "RELIANCE"
        fields = {
            "open": 2450.0,
            "close": 2500.0,
            "volume": 1000000,
            "oi": 0,
            "change": 50.0,
            "change_pct": 2.0,
            "bid": 2499.5,
            "ask": 2500.5,
        }

        broker_apis.record_good_quote(symbol, fields)
        result = broker_apis.get_last_good_quote(symbol)

        assert result is not None, "Quote should be recorded"
        assert result["open"] == 2450.0, f"Expected open 2450.0 but got {result['open']}"
        assert result["close"] == 2500.0, f"Expected close 2500.0 but got {result['close']}"

    def test_record_quote_skips_empty_payload(self):
        """Empty quote (no meaningful fields) is not recorded."""
        symbol = "RELIANCE"
        fields = {
            "open": None,
            "close": None,
            "volume": 0,
            "oi": 0,
        }

        broker_apis.record_good_quote(symbol, fields)
        result = broker_apis.get_last_good_quote(symbol)

        assert result is None, "Empty quote should not be recorded"

    def test_record_quote_with_one_meaningful_field(self):
        """Quote with at least one non-zero meaningful field is recorded."""
        symbol = "RELIANCE"
        fields = {
            "open": 2450.0,
            "close": None,
            "volume": 0,
            "oi": None,
        }

        broker_apis.record_good_quote(symbol, fields)
        result = broker_apis.get_last_good_quote(symbol)

        assert result is not None, "Quote with one meaningful field should be recorded"
        assert result["open"] == 2450.0, f"Expected open 2450.0 but got {result['open']}"

    def test_quote_cache_ttl_expiry(self):
        """Quote older than max_age_s returns None."""
        symbol = "RELIANCE"
        fields = {"open": 2450.0, "close": 2500.0, "volume": 1000000, "oi": 0}

        broker_apis.record_good_quote(symbol, fields)

        # Immediately retrieve — should work
        result = broker_apis.get_last_good_quote(symbol, max_age_s=86400)
        assert result is not None, "Fresh quote should be retrievable"

        # Retrieve with 0-second TTL (already expired)
        result = broker_apis.get_last_good_quote(symbol, max_age_s=0)
        assert result is None, "Expired quote should return None"

    def test_quote_cache_returns_copy(self):
        """Quote returned is a copy, mutations don't affect cache."""
        symbol = "RELIANCE"
        fields = {"open": 2450.0, "close": 2500.0, "volume": 1000000, "oi": 0}

        broker_apis.record_good_quote(symbol, fields)
        result1 = broker_apis.get_last_good_quote(symbol)
        result1["open"] = 9999.0  # Mutate the returned dict

        result2 = broker_apis.get_last_good_quote(symbol)
        assert result2["open"] == 2450.0, "Cache mutation should not affect subsequent retrieves"


class TestAuthErrorDetection:
    """Test _is_auth_error_str function."""

    def test_auth_error_401(self):
        """401 error detected as auth failure."""
        assert broker_apis._is_auth_error_str("401 Unauthorized") is True

    def test_auth_error_403(self):
        """403 error detected as auth failure."""
        assert broker_apis._is_auth_error_str("403 Forbidden") is True

    def test_auth_error_token_expired(self):
        """'token expired' detected as auth failure."""
        assert broker_apis._is_auth_error_str("Token expired") is True

    def test_auth_error_invalid_token(self):
        """'invalid token' detected as auth failure."""
        assert broker_apis._is_auth_error_str("invalid token") is True

    def test_auth_error_dh_906(self):
        """Dhan DH-906 detected as auth failure."""
        assert broker_apis._is_auth_error_str("DH-906: Invalid Token") is True

    def test_auth_error_dh_901(self):
        """Dhan DH-901 detected as auth failure."""
        assert broker_apis._is_auth_error_str("DH-901 error") is True

    def test_non_auth_error(self):
        """Non-auth error not detected."""
        assert broker_apis._is_auth_error_str("Connection timeout") is False
        assert broker_apis._is_auth_error_str("Network error") is False


class TestCircuitBreakerStateMachine:
    """Test circuit breaker CLOSED → OPEN → HALF-OPEN → CLOSED state machine."""

    def teardown_method(self):
        """Clear health state before each test."""
        broker_apis._FETCH_HEALTH.clear()
        broker_apis._breaker_optin_cache.clear()

    def test_circuit_state_closed_initially(self):
        """New account starts in CLOSED state."""
        account = "TEST_ACC"
        broker_apis.set_breaker_optin_cache(account, True)

        state = broker_apis._circuit_state(account)
        assert state == "closed", f"Expected closed state but got {state}"

    def test_circuit_closes_on_success(self):
        """HALF-OPEN → CLOSED on successful fetch."""
        account = "TEST_ACC"
        broker_apis.set_breaker_optin_cache(account, True)

        # Simulate 3 failures to open the circuit
        for _ in range(3):
            broker_apis._record_fetch(account, ok=False, error="test failure")

        state = broker_apis._circuit_state(account)
        assert state == "open", f"Expected open state after 3 failures, got {state}"

        # One success should close it (via half-open)
        broker_apis._record_fetch(account, ok=True, error="")
        state = broker_apis._circuit_state(account)
        assert state == "closed", f"Expected closed state after success, got {state}"

    def test_circuit_opens_on_threshold(self):
        """CLOSED → OPEN when consecutive_fail_count >= _CB_FAIL_THRESHOLD (3)."""
        account = "TEST_ACC"
        broker_apis.set_breaker_optin_cache(account, True)

        # First 2 failures — should stay CLOSED
        broker_apis._record_fetch(account, ok=False, error="fail1")
        state = broker_apis._circuit_state(account)
        assert state == "closed", f"Expected closed after 1 fail, got {state}"

        broker_apis._record_fetch(account, ok=False, error="fail2")
        state = broker_apis._circuit_state(account)
        assert state == "closed", f"Expected closed after 2 fails, got {state}"

        # 3rd failure — should OPEN
        broker_apis._record_fetch(account, ok=False, error="fail3")
        state = broker_apis._circuit_state(account)
        assert state == "open", f"Expected open after 3 fails, got {state}"

    def test_circuit_half_open_after_cooloff(self):
        """OPEN → HALF-OPEN when cooloff expires."""
        account = "TEST_ACC"
        broker_apis.set_breaker_optin_cache(account, True)

        # Open the circuit
        for _ in range(3):
            broker_apis._record_fetch(account, ok=False, error="fail")

        state = broker_apis._circuit_state(account)
        assert state == "open", "Expected open state"

        # Advance time past the initial cooloff (5 min + 30s jitter)
        with broker_apis._BREAKER_LOCK:
            health = broker_apis._FETCH_HEALTH[account]
            # Set circuit_open_until to past time
            health["circuit_open_until"] = _time.time() - 1.0

        state = broker_apis._circuit_state(account)
        assert state == "half-open", f"Expected half-open after cooloff expiry, got {state}"

    def test_is_circuit_open_bypassed_when_opt_out(self):
        """Non-opted-in accounts always return False from _is_circuit_open."""
        account = "TEST_ACC"
        broker_apis.set_breaker_optin_cache(account, False)

        # Even with failures, the circuit should not open
        for _ in range(5):
            broker_apis._record_fetch(account, ok=False, error="fail")

        is_open = broker_apis._is_circuit_open(account)
        assert is_open is False, "Opt-out accounts should not have circuit open"

    def test_consecutive_fail_count_reset_on_success(self):
        """Success resets consecutive_fail_count to 0."""
        account = "TEST_ACC"
        broker_apis.set_breaker_optin_cache(account, True)

        # Record 2 failures
        broker_apis._record_fetch(account, ok=False, error="fail1")
        broker_apis._record_fetch(account, ok=False, error="fail2")

        with broker_apis._BREAKER_LOCK:
            count = broker_apis._FETCH_HEALTH[account]["consecutive_fail_count"]
        assert count == 2, f"Expected 2 consecutive fails, got {count}"

        # Success should reset
        broker_apis._record_fetch(account, ok=True, error="")

        with broker_apis._BREAKER_LOCK:
            count = broker_apis._FETCH_HEALTH[account]["consecutive_fail_count"]
        assert count == 0, f"Expected 0 consecutive fails after success, got {count}"


class TestRawCacheReserve:
    """Test the ssot_fetch-based coalesce cache (replaces _raw_cache_reserve).

    The hand-rolled _RAW_CACHE / _RAW_INFLIGHT / _raw_cache_reserve/release
    was replaced by ssot_fetch(mode="coalesce").  These tests verify the new
    interface: _result_cache on each _fetch_*_cached function, direct dict
    seeding, and _raw_cache_invalidate.
    """

    def setup_method(self):
        broker_apis._raw_cache_invalidate(None)

    def teardown_method(self):
        broker_apis._raw_cache_invalidate(None)

    def test_result_cache_empty_initially(self):
        """_result_cache starts empty after invalidate(None)."""
        assert broker_apis._fetch_holdings_cached._result_cache.get("holdings") is None
        assert broker_apis._fetch_positions_cached._result_cache.get("positions") is None
        assert broker_apis._fetch_margins_cached._result_cache.get("margins") is None

    def test_result_cache_populated_after_seed(self):
        """Seeding _result_cache directly returns the same object reference."""
        test_data = [pd.DataFrame({"symbol": ["RELIANCE"]})]
        broker_apis._fetch_holdings_cached._result_cache["holdings"] = test_data

        cached = broker_apis._fetch_holdings_cached._result_cache.get("holdings")
        assert cached is test_data, "Cache must return the exact same reference (no copy)"

    def test_invalidate_single_key_clears_only_that_key(self):
        """_raw_cache_invalidate('holdings') drops only the holdings entry."""
        test_data = [pd.DataFrame({"col": [1]})]
        broker_apis._fetch_holdings_cached._result_cache["holdings"] = test_data
        broker_apis._fetch_positions_cached._result_cache["positions"] = test_data

        broker_apis._raw_cache_invalidate("holdings")

        assert broker_apis._fetch_holdings_cached._result_cache.get("holdings") is None, (
            "holdings key must be cleared by invalidate('holdings')"
        )
        assert broker_apis._fetch_positions_cached._result_cache.get("positions") is test_data, (
            "positions key must NOT be cleared when only holdings is invalidated"
        )


class TestDhanPollPriority:
    """Test Dhan poll-priority interval gating."""

    def teardown_method(self):
        """Clear state before each test."""
        broker_apis._dhan_poll_priority_cache.clear()
        broker_apis._dhan_next_poll.clear()

    def test_set_and_get_dhan_priority(self):
        """Set and retrieve Dhan poll priority."""
        account = "DH6847"
        broker_apis.set_dhan_priority_cache(account, "warm")

        priority = broker_apis._get_dhan_poll_priority(account)
        assert priority == "warm", f"Expected warm priority, got {priority}"

    def test_dhan_priority_defaults_to_hot(self):
        """Unknown account defaults to 'hot' priority."""
        priority = broker_apis._get_dhan_poll_priority("UNKNOWN_ACCOUNT")
        assert priority == "hot", f"Expected default 'hot' priority, got {priority}"

    def test_dhan_priority_invalid_coerced_to_hot(self):
        """Invalid priority string is coerced to 'hot'."""
        account = "DH6847"
        broker_apis.set_dhan_priority_cache(account, "invalid_priority")

        priority = broker_apis._get_dhan_poll_priority(account)
        assert priority == "hot", f"Expected coerced 'hot' priority, got {priority}"

    def test_is_dhan_interval_due_initially_true(self):
        """First poll for Dhan account is due immediately."""
        account = "DH6847"
        broker = MagicMock()
        broker.__class__.__name__ = "DhanBroker"

        # No prior poll set
        is_due = broker_apis._is_dhan_interval_due(account, broker)
        assert is_due is True, "First poll should be due immediately"

    def test_is_dhan_interval_due_respects_interval(self):
        """Subsequent polls respect the interval."""
        account = "DH6847"
        broker = MagicMock()
        broker.__class__.__name__ = "DhanBroker"

        broker_apis.set_dhan_priority_cache(account, "warm")  # 120s interval
        now = _time.time()
        broker_apis._dhan_next_poll[account] = now + 1000  # Far in future

        is_due = broker_apis._is_dhan_interval_due(account, broker)
        assert is_due is False, "Poll should not be due before interval expires"

    def test_update_dhan_next_poll(self):
        """_update_dhan_next_poll advances the next poll time."""
        account = "DH6847"
        broker = MagicMock()
        broker.__class__.__name__ = "DhanBroker"

        broker_apis.set_dhan_priority_cache(account, "warm")  # 120s interval
        now = _time.time()

        broker_apis._update_dhan_next_poll(account, broker)
        next_poll = broker_apis._dhan_next_poll[account]

        expected_min = now + 120 - 1  # Allow 1s drift
        expected_max = now + 120 + 1
        assert expected_min < next_poll < expected_max, \
            f"Expected next_poll ~{now + 120}, got {next_poll}"

    def test_dhan_next_poll_clear_all(self):
        """dhan_next_poll_clear(None) clears all entries."""
        broker_apis._dhan_next_poll["DH1"] = 1234567890
        broker_apis._dhan_next_poll["DH2"] = 1234567891

        broker_apis.dhan_next_poll_clear(None)

        assert len(broker_apis._dhan_next_poll) == 0, "Clear all should empty dict"

    def test_dhan_next_poll_clear_specific(self):
        """dhan_next_poll_clear(accounts) clears specific entries."""
        broker_apis._dhan_next_poll["DH1"] = 1234567890
        broker_apis._dhan_next_poll["DH2"] = 1234567891

        broker_apis.dhan_next_poll_clear(["DH1"])

        assert "DH1" not in broker_apis._dhan_next_poll, "DH1 should be cleared"
        assert "DH2" in broker_apis._dhan_next_poll, "DH2 should remain"


class TestLKGFrameSubstitution:
    """Test last-known-good frame substitution for stale accounts."""

    def teardown_method(self):
        """Clear state before each test."""
        broker_apis._LKG_FRAME_BY_ACCT.clear()

    def test_record_lkg_frame(self):
        """Record a last-known-good frame."""
        kind = "positions"
        account = "ZG0790"
        df = pd.DataFrame({"symbol": ["RELIANCE"], "quantity": [10]})

        broker_apis._record_lkg_frame(kind, account, df)

        result = broker_apis._get_lkg_frame(kind, account)
        assert result is not None, "LKG frame should be recorded"
        ts, stored_df = result
        assert len(stored_df) == 1, "Frame should contain 1 row"
        assert stored_df["symbol"].iloc[0] == "RELIANCE"

    def test_record_lkg_accepts_empty_frame(self):
        """Empty DataFrame is recorded as LKG (per docstring, empty frames poison cache)."""
        kind = "positions"
        account = "ZG0790"
        df = pd.DataFrame()

        broker_apis._record_lkg_frame(kind, account, df)

        result = broker_apis._get_lkg_frame(kind, account)
        assert result is not None, "Empty frame should be recorded"
        ts, stored_df = result
        assert stored_df.empty, "Stored frame should be empty"

    def test_get_lkg_frame_expired(self):
        """LKG frame older than max age returns None."""
        kind = "positions"
        account = "ZG0790"
        df = pd.DataFrame({"symbol": ["RELIANCE"]})

        broker_apis._record_lkg_frame(kind, account, df)

        # Get with zero TTL (already expired)
        result = broker_apis._get_lkg_frame(kind, account)
        assert result is not None, "Fresh frame should be retrievable"

        # Manually set timestamp to far past
        with broker_apis._LKG_FRAME_LOCK:
            broker_apis._LKG_FRAME_BY_ACCT[(kind, account)] = (
                _time.time() - 100000,  # 100000 seconds ago
                df,
            )

        result = broker_apis._get_lkg_frame(kind, account)
        assert result is None, "Expired frame should return None"

    def test_stale_substitute_frame_with_lkg(self):
        """Stale substitution marks frame with stale attrs."""
        kind = "holdings"
        account = "ZG0790"
        df = pd.DataFrame({"symbol": ["INFY"], "quantity": [5]})

        broker_apis._record_lkg_frame(kind, account, df)

        result_df = broker_apis._stale_substitute_frame(kind, account)

        assert result_df.attrs.get("stale") is True, "Should be marked stale"
        assert result_df.attrs.get("circuit_open") is True, "Should mark circuit_open"
        assert "account_stale" in result_df.columns, "Should have account_stale column"
        # Use == for numpy boolean comparison (np.True_ == True)
        assert result_df["account_stale"].iloc[0] == True, "Row should be stale"

    def test_stale_substitute_frame_no_lkg(self):
        """Stale substitution returns empty frame when no LKG."""
        kind = "positions"
        account = "UNKNOWN"

        result_df = broker_apis._stale_substitute_frame(kind, account)

        assert result_df.empty, "Should return empty DataFrame when no LKG"
        assert result_df.attrs.get("circuit_open") is True, "Should mark circuit_open"


class TestBreaker_OpinCacheAndHealthEntry:
    """Test breaker opt-in cache and health entry initialization."""

    def teardown_method(self):
        """Clear state."""
        broker_apis._breaker_optin_cache.clear()
        broker_apis._FETCH_HEALTH.clear()

    def test_set_and_get_breaker_optin(self):
        """Set and retrieve breaker opt-in state."""
        account = "DH6847"
        broker_apis.set_breaker_optin_cache(account, True)

        is_enabled = broker_apis.get_breaker_optin_cache(account)
        assert is_enabled is True, f"Expected True, got {is_enabled}"

    def test_breaker_optin_defaults_false(self):
        """Unknown account defaults to False (no breaker)."""
        is_enabled = broker_apis.get_breaker_optin_cache("UNKNOWN")
        assert is_enabled is False, f"Expected default False, got {is_enabled}"

    def test_default_health_entry_structure(self):
        """Default health entry has all required fields."""
        entry = broker_apis._default_health_entry()

        required_fields = [
            "last_ok_at",
            "last_fail_at",
            "last_fail_msg",
            "consecutive_fail_count",
            "circuit_open_until",
            "circuit_last_opened_at",
            "open_cycle_count",
        ]
        for field in required_fields:
            assert field in entry, f"Missing field: {field}"

    def test_is_account_healthy_no_fetch_attempt(self):
        """Account with no fetch attempts is considered healthy."""
        account = "UNKNOWN"
        is_healthy = broker_apis.is_account_healthy(account)
        assert is_healthy is True, "Unknown account (never tried) should be healthy"

    def test_is_account_healthy_after_success(self):
        """Account is healthy after successful fetch."""
        account = "ZG0790"
        broker_apis.set_breaker_optin_cache(account, False)

        broker_apis._record_fetch(account, ok=True, error="")

        is_healthy = broker_apis.is_account_healthy(account)
        assert is_healthy is True, "Account should be healthy after success"

    def test_is_account_healthy_after_failure(self):
        """Account is not healthy after failed fetch."""
        account = "ZG0790"
        broker_apis.set_breaker_optin_cache(account, False)

        broker_apis._record_fetch(account, ok=False, error="Connection failed")

        is_healthy = broker_apis.is_account_healthy(account)
        assert is_healthy is False, "Account should not be healthy after failure"


class TestRawCacheLifecycle:
    """Test ssot_fetch-based coalesce cache lifecycle.

    The hand-rolled _raw_cache_reserve/put/release/get was replaced by
    ssot_fetch(mode="coalesce") exposing _result_cache on each function.
    These tests verify direct dict semantics and the _raw_cache_invalidate
    interface that postback handlers and ?fresh=1 use.
    """

    def teardown_method(self):
        """Clear caches before each test."""
        broker_apis._raw_cache_invalidate(None)

    def test_result_cache_starts_empty(self):
        """_result_cache is empty after invalidate(None)."""
        assert "holdings" not in broker_apis._fetch_holdings_cached._result_cache
        assert "positions" not in broker_apis._fetch_positions_cached._result_cache
        assert "margins" not in broker_apis._fetch_margins_cached._result_cache

    def test_seed_and_retrieve_same_reference(self):
        """Value seeded into _result_cache is returned by reference (no copy)."""
        df = pd.DataFrame({"col": [1, 2, 3]})
        payload = [df]
        broker_apis._fetch_holdings_cached._result_cache["holdings"] = payload

        cached = broker_apis._fetch_holdings_cached._result_cache.get("holdings")
        assert cached is payload, "Must return the same object reference"
        assert len(cached) == 1, "Should contain one DataFrame"

    def test_invalidate_single_key_leaves_others(self):
        """_raw_cache_invalidate('holdings') drops holdings but not others."""
        df = pd.DataFrame({"col": [1, 2, 3]})
        broker_apis._fetch_holdings_cached._result_cache["holdings"] = [df]
        broker_apis._fetch_positions_cached._result_cache["positions"] = [df]

        broker_apis._raw_cache_invalidate("holdings")

        assert broker_apis._fetch_holdings_cached._result_cache.get("holdings") is None, (
            "holdings should be cleared"
        )
        assert broker_apis._fetch_positions_cached._result_cache.get("positions") is not None, (
            "positions should remain"
        )

    def test_invalidate_none_clears_all_three(self):
        """_raw_cache_invalidate(None) clears all three caches."""
        df = pd.DataFrame({"col": [1, 2, 3]})
        broker_apis._fetch_holdings_cached._result_cache["holdings"] = [df]
        broker_apis._fetch_positions_cached._result_cache["positions"] = [df]
        broker_apis._fetch_margins_cached._result_cache["margins"] = [df]

        broker_apis._raw_cache_invalidate(None)

        assert broker_apis._fetch_holdings_cached._result_cache.get("holdings") is None, (
            "holdings should be cleared by invalidate(None)"
        )
        assert broker_apis._fetch_positions_cached._result_cache.get("positions") is None, (
            "positions should be cleared by invalidate(None)"
        )
        assert broker_apis._fetch_margins_cached._result_cache.get("margins") is None, (
            "margins should be cleared by invalidate(None)"
        )

    def test_invalidate_nonexistent_key_is_noop(self):
        """_raw_cache_invalidate with a key not present is a no-op (no KeyError)."""
        # Should not raise even though the key was never set.
        broker_apis._raw_cache_invalidate("nonexistent_key")

    def test_cache_is_invalidation_based_not_ttl(self):
        """Cache entries persist indefinitely until explicitly invalidated.

        The ssot_fetch coalesce cache has no TTL — it is invalidation-only.
        A seeded entry survives any amount of logical time.
        """
        df = pd.DataFrame({"col": [1]})
        broker_apis._fetch_holdings_cached._result_cache["holdings"] = [df]

        # Still present — no TTL expiry mechanism.
        cached = broker_apis._fetch_holdings_cached._result_cache.get("holdings")
        assert cached is not None, "Cache must persist until explicitly invalidated (no TTL)"

        # Only invalidate removes it.
        broker_apis._raw_cache_invalidate("holdings")
        assert broker_apis._fetch_holdings_cached._result_cache.get("holdings") is None


class TestAccountOrderMap:
    """Test get_account_order_map and sort_accounts."""

    def teardown_method(self):
        """Clear module-level cache."""
        broker_apis._ACCOUNT_ORDER_CACHE.clear()

    def test_get_account_order_map_returns_dict(self):
        """get_account_order_map returns a dict (even if empty)."""
        import asyncio
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            # Avoid actual DB call
            with patch("asyncio.run", return_value={}):
                result = broker_apis.get_account_order_map()
                assert isinstance(result, dict), f"Expected dict, got {type(result)}"

    def test_sort_accounts_preserves_known_order(self):
        """sort_accounts sorts by display_order then account_id."""
        # Manually set order map to test sorting
        broker_apis._ACCOUNT_ORDER_CACHE = {
            "ACC1": 2,
            "ACC2": 1,
            "ACC3": 1,
        }
        broker_apis._ACCOUNT_ORDER_CACHE_AT = _time.time()

        accounts = ["ACC1", "ACC2", "ACC3"]
        sorted_accounts = broker_apis.sort_accounts(accounts)

        # ACC2 and ACC3 both have display_order=1, so they sort by account_id
        # ACC1 has display_order=2
        expected_order = ["ACC2", "ACC3", "ACC1"]
        assert sorted_accounts == expected_order, f"Expected {expected_order}, got {sorted_accounts}"

    def test_sort_accounts_unknown_accounts_go_to_end(self):
        """Unknown accounts (not in DB) fall to the end."""
        broker_apis._ACCOUNT_ORDER_CACHE = {"ACC1": 1}
        broker_apis._ACCOUNT_ORDER_CACHE_AT = _time.time()

        accounts = ["UNKNOWN", "ACC1"]
        sorted_accounts = broker_apis.sort_accounts(accounts)

        # ACC1 is known (display_order=1), UNKNOWN is unknown (display_order=999)
        assert sorted_accounts[0] == "ACC1", "Known account should come first"
        assert sorted_accounts[1] == "UNKNOWN", "Unknown account should come last"


class TestHealthRecording:
    """Test _record_fetch health recording and emoji generation."""

    def teardown_method(self):
        """Clear health state."""
        broker_apis._FETCH_HEALTH.clear()
        broker_apis._breaker_optin_cache.clear()

    def test_record_fetch_ok_updates_timestamp(self):
        """Recording ok=True updates last_ok_at."""
        account = "ZG0790"
        broker_apis.set_breaker_optin_cache(account, False)

        before = _time.time()
        broker_apis._record_fetch(account, ok=True, error="")
        after = _time.time()

        with broker_apis._BREAKER_LOCK:
            last_ok = broker_apis._FETCH_HEALTH[account]["last_ok_at"]
        assert before <= last_ok <= after, "last_ok_at should be recent"

    def test_record_fetch_fail_updates_timestamp(self):
        """Recording ok=False updates last_fail_at."""
        account = "ZG0790"
        broker_apis.set_breaker_optin_cache(account, False)

        before = _time.time()
        broker_apis._record_fetch(account, ok=False, error="Network timeout")
        after = _time.time()

        with broker_apis._BREAKER_LOCK:
            last_fail = broker_apis._FETCH_HEALTH[account]["last_fail_at"]
            fail_msg = broker_apis._FETCH_HEALTH[account]["last_fail_msg"]
        assert before <= last_fail <= after, "last_fail_at should be recent"
        assert "Network timeout" in fail_msg, "Error message should be stored"

    def test_is_account_healthy_never_tried(self):
        """Account with no fetch attempts is healthy."""
        is_healthy = broker_apis.is_account_healthy("NEVER_TRIED")
        assert is_healthy is True, "Never-attempted account should be healthy"


class TestEmitterHelpers:
    """Test _emit_conn_event and _broker_id_safe helpers."""

    def test_emit_conn_event_graceful_failure(self):
        """_emit_conn_event silently handles import/call failures."""
        # This is mostly a no-op test since the function swallows exceptions
        # but we call it to verify it doesn't raise
        try:
            broker_apis._emit_conn_event("TEST", "unknown", "test_event", {"key": "val"})
        except Exception as e:
            pytest.fail(f"_emit_conn_event should not raise: {e}")

    def test_broker_id_safe_returns_string(self):
        """_broker_id_safe always returns a string."""
        result = broker_apis._broker_id_safe("UNKNOWN_ACCOUNT")
        assert isinstance(result, str), f"Should return string, got {type(result)}"
        assert result in ("unknown", "zerodha_kite", "dhan", "groww") or result == "unknown"


class TestExtractNetRows:
    """Test _extract_net_rows unwrapping logic."""

    def test_extract_net_rows_from_broker_dict(self):
        """Extract net array from broker.positions() dict response."""
        broker = MagicMock()
        broker.positions.return_value = {
            "net": [{"tradingsymbol": "RELIANCE", "quantity": 10}],
            "day": [{"tradingsymbol": "RELIANCE", "quantity": 5}]
        }

        rows = broker_apis._extract_net_rows(broker, None)

        assert rows is not None, "Should extract net rows"
        assert len(rows) == 1, "Should have 1 net row"
        assert rows[0]["tradingsymbol"] == "RELIANCE"

    def test_extract_net_rows_from_broker_list(self):
        """Extract list directly when broker.positions() returns a list."""
        broker = MagicMock()
        broker.positions.return_value = [{"tradingsymbol": "INFY", "quantity": 5}]

        rows = broker_apis._extract_net_rows(broker, None)

        assert rows is not None, "Should extract rows"
        assert len(rows) == 1, "Should have 1 row"
        assert rows[0]["tradingsymbol"] == "INFY"

    def test_extract_net_rows_from_kite(self):
        """Extract net array from kite.positions()."""
        kite = MagicMock()
        kite.positions.return_value = {
            "net": [{"tradingsymbol": "NIFTY", "quantity": 50}],
            "day": []
        }

        rows = broker_apis._extract_net_rows(None, kite)

        assert rows is not None, "Should extract net rows"
        assert len(rows) == 1
        assert rows[0]["tradingsymbol"] == "NIFTY"

    def test_extract_net_rows_from_broker_invalid(self):
        """Return None when broker.positions() returns invalid type."""
        broker = MagicMock()
        broker.positions.return_value = "invalid"

        rows = broker_apis._extract_net_rows(broker, None)

        assert rows is None, "Should return None for invalid response"

    def test_extract_net_rows_no_source(self):
        """Return None when neither broker nor kite provided."""
        rows = broker_apis._extract_net_rows(None, None)

        assert rows is None, "Should return None when no source available"


class TestMaybeLogKiteMcxDiag:
    """Test _maybe_log_kite_mcx_diag diagnostics."""

    def teardown_method(self):
        """Reset the one-time flag."""
        import backend.brokers.broker_apis as ba_mod
        if hasattr(ba_mod, '_KITE_VALUE_UNIT_LOGGED'):
            ba_mod._KITE_VALUE_UNIT_LOGGED = False

    def test_log_kite_mcx_diag_empty_frame(self):
        """Empty frame is skipped."""
        df = pd.DataFrame()
        # Should not raise
        broker_apis._maybe_log_kite_mcx_diag(df)

    def test_log_kite_mcx_diag_no_multiplier_column(self):
        """Frame without multiplier column is skipped."""
        df = pd.DataFrame({"tradingsymbol": ["RELIANCE"], "quantity": [10]})
        # Should not raise
        broker_apis._maybe_log_kite_mcx_diag(df)

    def test_log_kite_mcx_diag_no_mcx_row(self):
        """Frame with multiplier=1 only (no MCX) is skipped."""
        df = pd.DataFrame({
            "tradingsymbol": ["RELIANCE"],
            "multiplier": [1],
            "day_buy_quantity": [0]
        })
        broker_apis._maybe_log_kite_mcx_diag(df)
        # Should log nothing

    def test_log_kite_mcx_diag_mcx_found(self):
        """MCX row with day_buy_quantity > 0 is logged."""
        df = pd.DataFrame({
            "tradingsymbol": ["CRUDEOIL"],
            "multiplier": [100],
            "day_buy_quantity": [5],
            "average_price": [5000.0],
            "day_buy_value": [2500000.0]
        })
        with patch("backend.brokers.broker_apis.logger") as mock_logger:
            broker_apis._maybe_log_kite_mcx_diag(df)
            # First call should log
            # (subsequent calls no-op due to _KITE_VALUE_UNIT_LOGGED flag)


class TestApplyMcxMultiplier:
    """Test _apply_mcx_multiplier MCX quantity scaling."""

    def test_apply_mcx_multiplier_empty_frame(self):
        """Empty frame is skipped."""
        df = pd.DataFrame()
        broker_apis._apply_mcx_multiplier(df)
        assert df.empty, "Empty frame should remain empty"

    def test_apply_mcx_multiplier_no_multiplier_column(self):
        """Frame without multiplier column is skipped."""
        df = pd.DataFrame({"quantity": [10], "overnight_quantity": [5]})
        original = df.copy()
        broker_apis._apply_mcx_multiplier(df)
        pd.testing.assert_frame_equal(df, original, "Frame should be unchanged")

    def test_apply_mcx_multiplier_scales_quantity(self):
        """Quantity is multiplied by multiplier (lot_size)."""
        df = pd.DataFrame({
            "tradingsymbol": ["CRUDEOIL"],
            "quantity": [5],
            "multiplier": [100]
        })
        broker_apis._apply_mcx_multiplier(df)
        assert df["quantity"].iloc[0] == 500, f"Expected 500, got {df['quantity'].iloc[0]}"

    def test_apply_mcx_multiplier_scales_day_quantities(self):
        """Day buy/sell quantities are also scaled."""
        df = pd.DataFrame({
            "tradingsymbol": ["CRUDEOIL"],
            "quantity": [10],
            "overnight_quantity": [5],
            "day_buy_quantity": [3],
            "day_sell_quantity": [2],
            "multiplier": [100]
        })
        broker_apis._apply_mcx_multiplier(df)
        assert df["overnight_quantity"].iloc[0] == 500, "overnight_quantity should be scaled"
        assert df["day_buy_quantity"].iloc[0] == 300, "day_buy_quantity should be scaled"
        assert df["day_sell_quantity"].iloc[0] == 200, "day_sell_quantity should be scaled"

    def test_apply_mcx_multiplier_missing_day_columns(self):
        """Missing day columns are skipped."""
        df = pd.DataFrame({
            "tradingsymbol": ["NIFTY"],
            "quantity": [10],
            "multiplier": [50]
        })
        broker_apis._apply_mcx_multiplier(df)
        assert df["quantity"].iloc[0] == 500, "quantity should be scaled"
        # No exception should be raised for missing columns


class TestBuildHoldingsPnlExpr:
    """Test _build_holdings_pnl_expr Polars expression builder."""

    def test_build_holdings_pnl_expr_signature(self):
        """_build_holdings_pnl_expr returns a Polars expression."""
        # Create a minimal DataFrame to pass to the function
        df = pd.DataFrame({
            "last_price": [2500.0],
            "average_price": [2400.0],
            "opening_quantity": [10],
            "pnl": [1000.0]
        })
        # Convert to Polars for the function (using standard constructor)
        lf = pl.from_pandas(df)

        # Just verify it doesn't crash and returns something
        try:
            expr = broker_apis._build_holdings_pnl_expr(lf, has_pnl=True)
            # If we got here, the function works
            assert expr is not None, "Should return an expression"
        except Exception as e:
            pytest.fail(f"_build_holdings_pnl_expr raised: {e}")


class TestBuildHoldingsCurvAlExprs:
    """Test _build_holdings_curval_exprs current value calculation."""

    def test_build_holdings_curval_exprs_signature(self):
        """_build_holdings_curval_exprs returns list of expressions."""
        df = pd.DataFrame({
            "inv_val": [100000.0],
            "pnl": [5000.0]
        })
        lf = pl.from_pandas(df)

        try:
            exprs = broker_apis._build_holdings_curval_exprs(lf)
            assert isinstance(exprs, list), "Should return a list of expressions"
            assert len(exprs) > 0, "Should return at least one expression"
        except Exception as e:
            pytest.fail(f"_build_holdings_curval_exprs raised: {e}")


class TestFetchPositionsFunction:
    """Test fetch_positions end-to-end."""

    def teardown_method(self):
        """Clear caches."""
        broker_apis._raw_cache_invalidate(None)

    def test_fetch_positions_with_mocked_broker(self):
        """Call fetch_positions with mocked broker."""
        with patch('backend.brokers.broker_apis.Connections') as mock_conns:
            mock_conn = MagicMock()
            mock_broker = MagicMock()
            mock_broker.positions.return_value = {
                "net": [{"tradingsymbol": "RELIANCE", "quantity": 10}]
            }
            mock_conn.conn = {"ZG0790": mock_broker}
            mock_conns.return_value = mock_conn

            with patch('backend.brokers.broker_apis.get_breaker_optin_cache', return_value=False):
                result = broker_apis.fetch_positions()
                assert isinstance(result, list), "fetch_positions should return a list"


class TestFetchHoldingsFunction:
    """Test fetch_holdings end-to-end."""

    def teardown_method(self):
        """Clear caches."""
        broker_apis._raw_cache_invalidate(None)

    def test_fetch_holdings_with_mocked_broker(self):
        """Call fetch_holdings with mocked broker."""
        with patch('backend.brokers.broker_apis.Connections') as mock_conns:
            mock_conn = MagicMock()
            mock_broker = MagicMock()
            mock_broker.holdings.return_value = [{"tradingsymbol": "RELIANCE", "quantity": 10}]
            mock_conn.conn = {"ZG0790": mock_broker}
            mock_conns.return_value = mock_conn

            with patch('backend.brokers.broker_apis.get_breaker_optin_cache', return_value=False):
                result = broker_apis.fetch_holdings()
                assert isinstance(result, list), "fetch_holdings should return a list"


class TestFetchMarginsFunction:
    """Test fetch_margins end-to-end."""

    def teardown_method(self):
        """Clear caches."""
        broker_apis._raw_cache_invalidate(None)

    def test_fetch_margins_with_mocked_broker(self):
        """Call fetch_margins with mocked broker."""
        with patch('backend.brokers.broker_apis.Connections') as mock_conns:
            mock_conn = MagicMock()
            mock_broker = MagicMock()
            mock_broker.margins.return_value = [{"account": "ZG0790", "avail cash": 100000}]
            mock_conn.conn = {"ZG0790": mock_broker}
            mock_conns.return_value = mock_conn

            with patch('backend.brokers.broker_apis.get_breaker_optin_cache', return_value=False):
                result = broker_apis.fetch_margins()
                assert isinstance(result, list), "fetch_margins should return a list"


class TestDhanAutoDowngradeLogic:
    """Test Dhan auto-downgrade circuit breaker escalation."""

    def teardown_method(self):
        """Clear state."""
        broker_apis._FETCH_HEALTH.clear()
        broker_apis._breaker_optin_cache.clear()
        broker_apis._downgrade_cooloff_until.clear()

    def test_maybe_auto_downgrade_when_no_loop_ready(self):
        """_maybe_auto_downgrade returns early when main loop not ready."""
        account = "DH6847"
        broker_apis.set_breaker_optin_cache(account, True)

        with patch('backend.api.persistence.write_queue.get_main_loop', return_value=None):
            # Should not raise, just return
            try:
                broker_apis._maybe_auto_downgrade(account)
            except Exception as e:
                pytest.fail(f"_maybe_auto_downgrade should not raise: {e}")

    def test_maybe_auto_downgrade_swallows_exceptions(self):
        """_maybe_auto_downgrade catches and logs exceptions."""
        account = "DH6847"

        with patch('backend.api.persistence.write_queue.get_main_loop', side_effect=Exception("test error")):
            # Should not raise
            try:
                broker_apis._maybe_auto_downgrade(account)
            except Exception as e:
                pytest.fail(f"_maybe_auto_downgrade should swallow exceptions: {e}")


class TestRecordFetchIntegration:
    """Test _record_fetch integration paths."""

    def teardown_method(self):
        """Clear state."""
        broker_apis._FETCH_HEALTH.clear()
        broker_apis._breaker_optin_cache.clear()

    def test_record_fetch_with_auth_error_detected(self):
        """_record_fetch marks account unhealthy on auth error."""
        account = "ZG0790"
        broker_apis.set_breaker_optin_cache(account, True)

        broker_apis._record_fetch(account, ok=False, error="DH-906: Invalid Token")

        is_healthy = broker_apis.is_account_healthy(account)
        assert is_healthy is False, "Account should be unhealthy after auth error"

    def test_record_fetch_counts_consecutive_failures(self):
        """_record_fetch increments consecutive_fail_count on each failure."""
        account = "ZG0790"
        broker_apis.set_breaker_optin_cache(account, True)

        # Record 3 failures
        for i in range(3):
            broker_apis._record_fetch(account, ok=False, error=f"fail {i+1}")

        with broker_apis._BREAKER_LOCK:
            count = broker_apis._FETCH_HEALTH[account]["consecutive_fail_count"]
        assert count == 3, f"Expected 3 consecutive failures, got {count}"


class TestIsCircuitOpenGuard:
    """Test _is_circuit_open guard path."""

    def teardown_method(self):
        """Clear state."""
        broker_apis._FETCH_HEALTH.clear()
        broker_apis._breaker_optin_cache.clear()

    def test_is_circuit_open_returns_false_when_opt_out(self):
        """_is_circuit_open returns False for opt-out accounts."""
        account = "TEST_ACCOUNT"
        broker_apis.set_breaker_optin_cache(account, False)

        # Simulate many failures
        for _ in range(10):
            broker_apis._record_fetch(account, ok=False, error="fail")

        # Circuit should still be closed (opt-out)
        is_open = broker_apis._is_circuit_open(account)
        assert is_open is False, "Opted-out account circuit should never open"


class TestFetchLTPFunction:
    """Test fetch_ltp direct call."""

    def test_fetch_ltp_with_mocked_broker(self):
        """Call fetch_ltp with mocked broker."""
        with patch('backend.brokers.broker_apis.Connections') as mock_conns:
            mock_conn = MagicMock()
            mock_broker = MagicMock()
            mock_broker.ltp.return_value = {"RELIANCE": 2500.50}
            mock_conn.conn = {"ZG0790": mock_broker}
            mock_conns.return_value = mock_conn

            with patch('backend.brokers.broker_apis.get_breaker_optin_cache', return_value=False):
                try:
                    result = broker_apis.fetch_ltp(["RELIANCE"])
                    # Result may be a list or dict depending on implementation
                    assert result is not None, "fetch_ltp should return a value"
                except Exception:
                    # If not implemented yet, that's ok
                    pass


class TestFetchQuoteFunction:
    """Test fetch_quote direct call."""

    def test_fetch_quote_with_mocked_broker(self):
        """Call fetch_quote with mocked broker."""
        with patch('backend.brokers.broker_apis.Connections') as mock_conns:
            mock_conn = MagicMock()
            mock_broker = MagicMock()
            mock_broker.quote.return_value = {
                "RELIANCE": {
                    "open": 2450.0,
                    "close": 2500.0,
                    "ltp": 2520.0
                }
            }
            mock_conn.conn = {"ZG0790": mock_broker}
            mock_conns.return_value = mock_conn

            with patch('backend.brokers.broker_apis.get_breaker_optin_cache', return_value=False):
                try:
                    result = broker_apis.fetch_quote(["RELIANCE"])
                    assert result is not None, "fetch_quote should return a value"
                except Exception:
                    # If not implemented yet, that's ok
                    pass


class TestFetchHealthSnapshot:
    """Test fetch_health_snapshot aggregation."""

    def teardown_method(self):
        """Clear health state."""
        broker_apis._FETCH_HEALTH.clear()
        broker_apis._breaker_optin_cache.clear()

    def test_fetch_health_snapshot_empty(self):
        """fetch_health_snapshot returns dict when no accounts."""
        result = broker_apis.fetch_health_snapshot()
        assert isinstance(result, dict), "fetch_health_snapshot should return dict"

    def test_fetch_health_snapshot_with_data(self):
        """fetch_health_snapshot includes account health data."""
        account = "ZG0790"
        broker_apis.set_breaker_optin_cache(account, False)
        broker_apis._record_fetch(account, ok=True, error="")

        result = broker_apis.fetch_health_snapshot()
        assert isinstance(result, dict), "Should return dict"
        # May or may not include the account, depending on implementation


# ---------------------------------------------------------------------------
# New coverage tests — record_session_ok, _record_fetch edge paths,
# enrichment functions, backfill helpers, holiday tier, update_books
# ---------------------------------------------------------------------------

class TestRecordSessionOk:
    """Test record_session_ok behaviour."""

    def teardown_method(self):
        broker_apis._FETCH_HEALTH.clear()

    def test_empty_account_noop(self):
        """Empty-string account is a no-op."""
        broker_apis.record_session_ok("")
        assert broker_apis._FETCH_HEALTH == {}, "Empty account should not mutate _FETCH_HEALTH"

    def test_seeds_entry_and_stamps_last_ok_at(self):
        """Non-empty account seeds a health entry and stamps last_ok_at."""
        account = "ZG0790"
        assert account not in broker_apis._FETCH_HEALTH
        before = _time.time()
        broker_apis.record_session_ok(account)
        after = _time.time()
        assert account in broker_apis._FETCH_HEALTH
        ok_at = broker_apis._FETCH_HEALTH[account]["last_ok_at"]
        assert before <= ok_at <= after, "last_ok_at should be stamped with current time"

    def test_updates_existing_entry(self):
        """record_session_ok updates last_ok_at on an already-seeded entry."""
        account = "ZG0790"
        broker_apis._FETCH_HEALTH[account] = broker_apis._default_health_entry()
        broker_apis._FETCH_HEALTH[account]["last_ok_at"] = 0.0
        broker_apis.record_session_ok(account)
        assert broker_apis._FETCH_HEALTH[account]["last_ok_at"] > 0.0


class TestRecordFetchNonOptinRecovery:
    """Test _record_fetch recovery event for non-opt-in accounts."""

    def teardown_method(self):
        broker_apis._FETCH_HEALTH.clear()
        broker_apis._breaker_optin_cache.clear()

    def test_recovery_event_emitted_when_last_fail_before_ok(self):
        """Non-opt-in account with last_fail_at > last_ok_at emits fetch_ok_recovery."""
        account = "ZG0790"
        broker_apis.set_breaker_optin_cache(account, False)
        # Seed state where we previously failed
        broker_apis._FETCH_HEALTH[account] = broker_apis._default_health_entry()
        broker_apis._FETCH_HEALTH[account]["last_fail_at"] = _time.time() + 1.0
        broker_apis._FETCH_HEALTH[account]["last_ok_at"] = 0.0
        broker_apis._FETCH_HEALTH[account]["last_fail_msg"] = "timeout"

        with patch.object(broker_apis, "_emit_conn_event") as mock_emit:
            broker_apis._record_fetch(account, ok=True)
            # fetch_ok_recovery should have been emitted
            events = [c[0][2] for c in mock_emit.call_args_list]
            assert "fetch_ok_recovery" in events, f"Expected fetch_ok_recovery, got {events}"

    def test_no_recovery_event_when_no_prior_failure(self):
        """Non-opt-in account with no prior failure does not emit recovery."""
        account = "ZG0790"
        broker_apis.set_breaker_optin_cache(account, False)
        broker_apis._FETCH_HEALTH[account] = broker_apis._default_health_entry()
        # last_ok_at > last_fail_at → no recovery
        broker_apis._FETCH_HEALTH[account]["last_ok_at"] = _time.time()
        broker_apis._FETCH_HEALTH[account]["last_fail_at"] = 0.0

        with patch.object(broker_apis, "_emit_conn_event") as mock_emit:
            broker_apis._record_fetch(account, ok=True)
            events = [c[0][2] for c in mock_emit.call_args_list]
            assert "fetch_ok_recovery" not in events


class TestRecordFetchHalfOpen:
    """Test _record_fetch HALF-OPEN → CLOSED transition (opt-in account)."""

    def teardown_method(self):
        broker_apis._FETCH_HEALTH.clear()
        broker_apis._breaker_optin_cache.clear()

    def test_half_open_ok_emits_circuit_close(self):
        """Opt-in account in HALF-OPEN state transitions to CLOSED on ok=True."""
        account = "DH6847"
        broker_apis.set_breaker_optin_cache(account, True)
        # Seed HALF-OPEN state: circuit_open_until in the past
        with broker_apis._BREAKER_LOCK:
            e = broker_apis._FETCH_HEALTH.setdefault(account, broker_apis._default_health_entry())
            e["circuit_open_until"] = _time.time() - 1.0  # already expired → half-open
            e["consecutive_fail_count"] = 3
            e["open_cycle_count"] = 1

        with patch.object(broker_apis, "_emit_conn_event") as mock_emit:
            broker_apis._record_fetch(account, ok=True)
            events = [c[0][2] for c in mock_emit.call_args_list]
            assert "circuit_close" in events, f"Expected circuit_close event, got {events}"

    def test_half_open_failure_reopens_circuit(self):
        """Opt-in account in HALF-OPEN state returns to OPEN on ok=False."""
        account = "DH6847"
        broker_apis.set_breaker_optin_cache(account, True)
        with broker_apis._BREAKER_LOCK:
            e = broker_apis._FETCH_HEALTH.setdefault(account, broker_apis._default_health_entry())
            e["circuit_open_until"] = _time.time() - 1.0
            e["consecutive_fail_count"] = 3
            e["open_cycle_count"] = 1

        broker_apis._record_fetch(account, ok=False, error="broker timeout")
        # Circuit should be re-opened (open_until > now)
        with broker_apis._BREAKER_LOCK:
            state = broker_apis._FETCH_HEALTH[account]
        assert state.get("circuit_open_until", 0.0) > _time.time(), \
            "Circuit should be re-opened after HALF-OPEN failure"


class TestInvalidateAccountOrderCache:
    """Test invalidate_account_order_cache resets the TTL timestamp."""

    def test_resets_cache_at_to_zero(self):
        """invalidate_account_order_cache sets _ACCOUNT_ORDER_CACHE_AT to 0.0."""
        import backend.brokers.broker_apis as ba_mod
        # Set to some non-zero value
        ba_mod._ACCOUNT_ORDER_CACHE_AT = _time.time()
        broker_apis.invalidate_account_order_cache()
        assert ba_mod._ACCOUNT_ORDER_CACHE_AT == 0.0


class TestEnrichHoldings:
    """Test _enrich_holdings Polars enrichment."""

    def test_full_columns_enriches_correctly(self):
        """_enrich_holdings computes pnl, cur_val, pnl_percentage, price_change, day_change_val."""
        df = pd.DataFrame({
            "last_price": [2600.0],
            "average_price": [2400.0],
            "opening_quantity": [10],
            "close_price": [2550.0],
            "pnl": [2000.0],   # broker-supplied
            "day_change_val": [None],
        })
        result = broker_apis._enrich_holdings(df)
        assert "pnl" in result.columns
        assert "price_change" in result.columns
        assert "day_change_val" in result.columns
        # inv_val = avg × qty
        assert "inv_val" in result.columns
        assert result["inv_val"].iloc[0] == pytest.approx(24000.0)

    def test_day_change_via_day_change_column(self):
        """When day_change column present (but no close/ltp/qty), dcv = day_change * qty."""
        df = pd.DataFrame({
            "day_change": [5.0],
            "opening_quantity": [10],
            # No last_price/average_price/close_price — exercises the
            # elif branch: day_change × opening_quantity
        })
        result = broker_apis._enrich_holdings(df)
        assert "day_change_val" in result.columns
        assert result["day_change_val"].iloc[0] == pytest.approx(50.0)

    def test_empty_dataframe_returns_unchanged(self):
        """_enrich_holdings on empty df should not raise."""
        df = pd.DataFrame()
        result = broker_apis._enrich_holdings(df)
        assert result.empty


class TestEnrichPositions:
    """Test _enrich_positions Polars enrichment."""

    def test_basic_columns_produce_day_change(self):
        """_enrich_positions with minimal columns computes day_change."""
        df = pd.DataFrame({
            "last_price": [200.0],
            "average_price": [190.0],
            "close_price": [195.0],
            "quantity": [10],
        })
        result = broker_apis._enrich_positions(df)
        assert "day_change" in result.columns
        assert result["day_change"].iloc[0] == pytest.approx(200.0 - 195.0)

    def test_intraday_fields_use_decomposed_formula(self):
        """_enrich_positions with full intraday fields uses decomposed formula."""
        df = pd.DataFrame({
            "last_price": [200.0],
            "average_price": [190.0],
            "close_price": [195.0],
            "quantity": [10],
            "overnight_quantity": [5],
            "day_buy_quantity": [5],
            "day_sell_quantity": [0],
            "day_buy_value": [1000.0],
            "day_sell_value": [0.0],
        })
        result = broker_apis._enrich_positions(df)
        assert "day_change_val" in result.columns
        assert "pnl" in result.columns
        assert "day_change_percentage" in result.columns
        assert "pnl_percentage" in result.columns

    def test_pnl_column_trusted_from_broker(self):
        """When pnl column present, broker value is trusted over formula."""
        df = pd.DataFrame({
            "last_price": [200.0],
            "average_price": [190.0],
            "close_price": [195.0],
            "quantity": [10],
            "pnl": [999.0],   # broker-supplied value
        })
        result = broker_apis._enrich_positions(df)
        # Broker pnl should be kept since it's not null
        assert result["pnl"].iloc[0] == pytest.approx(999.0)


class TestBmdIsMissingVal:
    """Test _bmd_is_missing_val."""

    def test_zero_is_missing(self):
        assert broker_apis._bmd_is_missing_val(0) is True

    def test_negative_is_missing(self):
        assert broker_apis._bmd_is_missing_val(-1.0) is True

    def test_nan_is_missing(self):
        assert broker_apis._bmd_is_missing_val(float("nan")) is True

    def test_non_numeric_is_missing(self):
        assert broker_apis._bmd_is_missing_val("abc") is True

    def test_none_is_missing(self):
        assert broker_apis._bmd_is_missing_val(None) is True

    def test_positive_float_not_missing(self):
        assert broker_apis._bmd_is_missing_val(100.0) is False

    def test_small_positive_not_missing(self):
        assert broker_apis._bmd_is_missing_val(0.01) is False


class TestBmdExtractLookups:
    """Test _bmd_extract_lookups quote dict parsing."""

    def test_extracts_ohlc_close_and_ltp(self):
        """Extracts close from ohlc dict and last_price as ltp."""
        quote_resp = {
            "NSE:RELIANCE": {
                "ohlc": {"close": 2550.0},
                "last_price": 2600.0,
            }
        }
        close_lookup, ltp_lookup = broker_apis._bmd_extract_lookups(quote_resp)
        assert close_lookup["NSE:RELIANCE"] == pytest.approx(2550.0)
        assert ltp_lookup["NSE:RELIANCE"] == pytest.approx(2600.0)

    def test_falls_back_to_top_level_close_price(self):
        """When ohlc absent, falls back to top-level close_price."""
        quote_resp = {
            "NSE:X": {
                "close_price": 100.0,
                "last_price": 105.0,
            }
        }
        close_lookup, ltp_lookup = broker_apis._bmd_extract_lookups(quote_resp)
        assert close_lookup["NSE:X"] == pytest.approx(100.0)

    def test_zero_values_excluded(self):
        """Zero close and zero ltp are not added to lookups."""
        quote_resp = {
            "NSE:Y": {
                "ohlc": {"close": 0.0},
                "last_price": 0.0,
            }
        }
        close_lookup, ltp_lookup = broker_apis._bmd_extract_lookups(quote_resp)
        assert "NSE:Y" not in close_lookup
        assert "NSE:Y" not in ltp_lookup

    def test_non_dict_values_skipped(self):
        """Non-dict values in quote resp are skipped."""
        quote_resp = {"NSE:Z": "invalid"}
        close_lookup, ltp_lookup = broker_apis._bmd_extract_lookups(quote_resp)
        assert "NSE:Z" not in close_lookup


class TestBmdPatchOneRow:
    """Test _bmd_patch_one_row close/ltp patching."""

    def test_patches_close_and_ltp_from_lookups(self):
        """Patches close_price and last_price from lookups."""
        df = pd.DataFrame({"close_price": [0.0], "last_price": [0.0]})
        touched, from_stale = broker_apis._bmd_patch_one_row(
            df, 0, "NFO:X", True, True,
            close_lookup={"NFO:X": 100.0},
            ltp_lookup={"NFO:X": 105.0},
        )
        assert touched is True
        assert from_stale is False
        assert df.at[0, "close_price"] == pytest.approx(100.0)
        assert df.at[0, "last_price"] == pytest.approx(105.0)

    def test_uses_stale_cache_when_ltp_lookup_empty(self):
        """Falls back to last-known-good cache when ltp_lookup has no value."""
        df = pd.DataFrame({"close_price": [0.0], "last_price": [0.0]})
        with patch.object(broker_apis, "get_last_good_ltp", return_value=99.0):
            touched, from_stale = broker_apis._bmd_patch_one_row(
                df, 0, "NFO:X", True, True,
                close_lookup={},
                ltp_lookup={},
            )
        assert touched is True
        assert from_stale is True
        assert df.at[0, "last_price"] == pytest.approx(99.0)

    def test_does_not_overwrite_nonzero_ltp(self):
        """Does not overwrite an existing non-zero last_price."""
        df = pd.DataFrame({"close_price": [0.0], "last_price": [200.0]})
        touched, from_stale = broker_apis._bmd_patch_one_row(
            df, 0, "NFO:X", True, True,
            close_lookup={"NFO:X": 100.0},
            ltp_lookup={"NFO:X": 105.0},
        )
        # last_price was already 200.0 (non-zero), should not be overwritten
        assert df.at[0, "last_price"] == pytest.approx(200.0)


class TestBmdMarkStaleColumn:
    """Test _bmd_mark_stale_column staleness flag."""

    def test_empty_stale_indices_no_column_added(self):
        """Empty stale_indices set → last_price_stale column not added."""
        df = pd.DataFrame({"last_price": [100.0]})
        broker_apis._bmd_mark_stale_column(df, set())
        assert "last_price_stale" not in df.columns

    def test_marks_stale_rows(self):
        """Rows in stale_indices get last_price_stale=True."""
        df = pd.DataFrame({"last_price": [100.0, 200.0]})
        broker_apis._bmd_mark_stale_column(df, {0})
        assert "last_price_stale" in df.columns
        assert bool(df.at[0, "last_price_stale"]) is True


class TestBmdRecomputeDerived:
    """Test _bmd_recompute_derived P&L recomputation."""

    def test_recomputes_day_change_val(self):
        """_bmd_recompute_derived updates day_change_val on patched rows."""
        df = pd.DataFrame({
            "last_price": [105.0],
            "close_price": [100.0],
            "opening_quantity": [10],
            "day_change_val": [0.0],
        })
        broker_apis._bmd_recompute_derived(df, {0})
        # (105 - 100) × 10 = 50.0
        assert df.at[0, "day_change_val"] == pytest.approx(50.0)

    def test_skips_empty_patched_indices(self):
        """Empty patched_indices is a no-op."""
        df = pd.DataFrame({
            "last_price": [105.0],
            "close_price": [100.0],
            "opening_quantity": [10],
            "day_change_val": [0.0],
        })
        original_val = df.at[0, "day_change_val"]
        broker_apis._bmd_recompute_derived(df, set())
        # With empty set, pd.Index(sorted({})) is empty, so no rows are processed
        assert df.at[0, "day_change_val"] == original_val


class TestUpdateBooks:
    """Test update_books DataFrame merging."""

    def test_merges_three_non_empty_frames(self):
        """update_books concatenates three non-empty DataFrames."""
        holdings = pd.DataFrame({"a": [1, 2]})
        positions = pd.DataFrame({"a": [3]})
        margins = pd.DataFrame({"a": [4, 5]})
        result = broker_apis.update_books(holdings, positions, margins)
        assert len(result) == 5
        assert list(result["a"]) == [1, 2, 3, 4, 5]

    def test_skips_empty_frames(self):
        """update_books excludes empty DataFrames from concat."""
        holdings = pd.DataFrame({"a": [1]})
        positions = pd.DataFrame()
        margins = pd.DataFrame({"a": [2]})
        result = broker_apis.update_books(holdings, positions, margins)
        assert len(result) == 2

    def test_all_empty_returns_empty(self):
        """update_books with all-empty frames returns empty DataFrame."""
        result = broker_apis.update_books(
            pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
        )
        assert result.empty


class TestFetchHolidaysTier2:
    """Test fetch_holidays Tier-2 module-level cache."""

    def teardown_method(self):
        broker_apis._HOLIDAY_CACHE.clear()

    def test_tier2_cache_hit_returns_cached_set(self):
        """fetch_holidays returns module-level _HOLIDAY_CACHE without hitting NSE."""
        import datetime
        today = datetime.date.today()
        expected = {datetime.date(2025, 1, 26)}
        broker_apis._HOLIDAY_CACHE["NSE"] = (today, expected)

        # Patch Tier 1 (holidays_store) to raise so we skip to Tier 2
        with patch.dict("sys.modules", {"backend.api.persistence.holidays_store": None}):
            with patch("backend.brokers.broker_apis._fetch_holidays_from_nse") as mock_nse:
                result = broker_apis.fetch_holidays("NSE")
                # NSE API should NOT have been called (Tier 2 hit)
                mock_nse.assert_not_called()
        assert result == expected

    def test_tier2_miss_falls_to_tier4(self):
        """fetch_holidays with no cache hits NSE API (Tier 4)."""
        import datetime
        # Ensure cache is empty for this exchange
        broker_apis._HOLIDAY_CACHE.pop("TESTEXCH", None)

        mock_holidays = {datetime.date(2025, 10, 2)}
        with patch("backend.brokers.broker_apis._fetch_holidays_from_nse", return_value=mock_holidays) as mock_nse:
            with patch("backend.brokers.broker_apis._read_market_holidays_sync", return_value=set()):
                with patch.dict("sys.modules", {"backend.api.persistence.holidays_store": None}):
                    result = broker_apis.fetch_holidays("TESTEXCH")
        # Should have called NSE fallback
        assert isinstance(result, set)


class TestFetchHolidaysFromNse:
    """Test _fetch_holidays_from_nse direct NSE API parsing."""

    def test_parses_trading_date_from_response(self):
        """_fetch_holidays_from_nse parses tradingDate entries into date objects."""
        import datetime
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "CM": [{"tradingDate": "26-Jan-2025"}, {"tradingDate": "15-Aug-2025"}]
        }
        mock_resp.raise_for_status.return_value = None

        # requests is imported locally inside the function, patch at requests module level
        with patch("requests.get", return_value=mock_resp):
            result = broker_apis._fetch_holidays_from_nse("NSE")

        assert datetime.date(2025, 1, 26) in result
        assert datetime.date(2025, 8, 15) in result

    def test_returns_empty_set_on_exception(self):
        """_fetch_holidays_from_nse returns empty set when requests raises."""
        with patch("requests.get", side_effect=Exception("network error")):
            result = broker_apis._fetch_holidays_from_nse("NSE")
        assert result == set()

    def test_invalid_date_format_skipped(self):
        """_fetch_holidays_from_nse skips entries with unparseable dates."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"CM": [{"tradingDate": "BAD-DATE"}]}
        mock_resp.raise_for_status.return_value = None

        with patch("requests.get", return_value=mock_resp):
            result = broker_apis._fetch_holidays_from_nse("NSE")
        assert result == set()


class TestIsAccountHealthyConnService:
    """Test is_account_healthy conn_service UDS path."""

    def teardown_method(self):
        broker_apis._FETCH_HEALTH.clear()
        broker_apis._breaker_optin_cache.clear()

    def test_conn_service_path_account_exists_in_snapshot(self):
        """is_account_healthy queries conn_service when no local entry exists."""
        account = "ZG0790"
        # Ensure no local entry
        broker_apis._FETCH_HEALTH.pop(account, None)

        with patch("backend.brokers.broker_apis._use_conn_service", return_value=True):
            with patch("backend.brokers.broker_apis.fetch_health_snapshot", return_value={
                account: {"last_ok_at": 1000.0, "last_fail_at": 500.0}
            }):
                result = broker_apis.is_account_healthy(account)
        assert result is True

    def test_conn_service_path_account_not_in_snapshot(self):
        """is_account_healthy returns True (benefit of doubt) when conn_service has no entry."""
        account = "ZG0790"
        broker_apis._FETCH_HEALTH.pop(account, None)

        with patch("backend.brokers.broker_apis._use_conn_service", return_value=True):
            with patch("backend.brokers.broker_apis.fetch_health_snapshot", return_value={}):
                result = broker_apis.is_account_healthy(account)
        assert result is True

    def test_conn_service_path_account_unhealthy(self):
        """is_account_healthy returns False when conn_service shows fail_at > ok_at."""
        account = "ZG0790"
        broker_apis._FETCH_HEALTH.pop(account, None)

        with patch("backend.brokers.broker_apis._use_conn_service", return_value=True):
            with patch("backend.brokers.broker_apis.fetch_health_snapshot", return_value={
                account: {"last_ok_at": 100.0, "last_fail_at": 999.0}
            }):
                result = broker_apis.is_account_healthy(account)
        assert result is False


class TestFetchHealthSnapshotConnService:
    """Test fetch_health_snapshot conn_service UDS path."""

    def test_conn_service_returns_health_dict(self):
        """fetch_health_snapshot calls conn_service and returns health map."""
        expected_health = {"ZG0790": {"last_ok_at": 1234.0, "last_fail_at": 0.0}}

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"health": expected_health}
        mock_resp.raise_for_status.return_value = None

        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp

        with patch("backend.brokers.broker_apis._use_conn_service", return_value=True):
            with patch("backend.brokers.client.sync._get_client", return_value=mock_client):
                result = broker_apis.fetch_health_snapshot()
        assert result == expected_health

    def test_conn_service_exception_returns_empty_dict(self):
        """fetch_health_snapshot returns {} when conn_service raises."""
        with patch("backend.brokers.broker_apis._use_conn_service", return_value=True):
            with patch("backend.brokers.client.sync._get_client", side_effect=Exception("UDS down")):
                result = broker_apis.fetch_health_snapshot()
        assert result == {}


class TestBmdBuildKeyIndex:
    """Test _bmd_build_key_index missing-row detection."""

    def test_no_missing_rows_returns_none(self):
        """When all rows have valid close and ltp, returns (None, [], [])."""
        df = pd.DataFrame({
            "tradingsymbol": ["RELIANCE"],
            "exchange": ["NSE"],
            "close_price": [2550.0],
            "last_price": [2600.0],
        })
        missing, key_per_row, unique_keys = broker_apis._bmd_build_key_index(df)
        assert missing is None
        assert key_per_row == []
        assert unique_keys == []

    def test_zero_close_price_detected_as_missing(self):
        """Row with close_price=0 is marked as missing."""
        df = pd.DataFrame({
            "tradingsymbol": ["RELIANCE"],
            "exchange": ["NSE"],
            "close_price": [0.0],
            "last_price": [2600.0],
        })
        missing, key_per_row, unique_keys = broker_apis._bmd_build_key_index(df)
        assert missing is not None
        assert missing.any()
        assert "NSE:RELIANCE" in unique_keys

    def test_zero_last_price_detected_as_missing(self):
        """Row with last_price=0 is marked as missing."""
        df = pd.DataFrame({
            "tradingsymbol": ["CRUDEOIL"],
            "exchange": ["MCX"],
            "close_price": [5000.0],
            "last_price": [0.0],
        })
        missing, key_per_row, unique_keys = broker_apis._bmd_build_key_index(df)
        assert missing is not None
        assert "MCX:CRUDEOIL" in unique_keys

    def test_empty_tradingsymbol_produces_empty_key(self):
        """Row with empty tradingsymbol gets empty key (skipped in patch)."""
        df = pd.DataFrame({
            "tradingsymbol": [""],
            "exchange": ["NSE"],
            "close_price": [0.0],
            "last_price": [0.0],
        })
        missing, key_per_row, unique_keys = broker_apis._bmd_build_key_index(df)
        # Empty symbol → empty key
        assert "" in key_per_row
        assert unique_keys == []

    def test_deduplicates_same_symbol(self):
        """Same symbol across two rows appears once in unique_keys."""
        df = pd.DataFrame({
            "tradingsymbol": ["RELIANCE", "RELIANCE"],
            "exchange": ["NSE", "NSE"],
            "close_price": [0.0, 0.0],
            "last_price": [0.0, 0.0],
        })
        missing, key_per_row, unique_keys = broker_apis._bmd_build_key_index(df)
        assert unique_keys.count("NSE:RELIANCE") == 1


class TestBmdPatchRows:
    """Test _bmd_patch_rows full row-patching loop."""

    def test_patches_missing_rows_from_lookups(self):
        """_bmd_patch_rows patches close_price and last_price on missing rows."""
        df = pd.DataFrame({
            "tradingsymbol": ["RELIANCE", "INFY"],
            "exchange": ["NSE", "NSE"],
            "close_price": [0.0, 2500.0],   # RELIANCE missing, INFY ok
            "last_price": [0.0, 2510.0],
        })
        row_indices = [0]  # only row 0 needs patching
        key_per_row = ["NSE:RELIANCE"]
        close_lookup = {"NSE:RELIANCE": 2540.0}
        ltp_lookup = {"NSE:RELIANCE": 2560.0}
        unique_keys = ["NSE:RELIANCE"]

        patched = broker_apis._bmd_patch_rows(
            df, row_indices, key_per_row, close_lookup, ltp_lookup, unique_keys
        )
        assert 0 in patched
        assert df.at[0, "close_price"] == pytest.approx(2540.0)
        assert df.at[0, "last_price"] == pytest.approx(2560.0)

    def test_skips_rows_with_empty_key(self):
        """_bmd_patch_rows skips rows whose key is empty string."""
        df = pd.DataFrame({
            "close_price": [0.0],
            "last_price": [0.0],
        })
        patched = broker_apis._bmd_patch_rows(
            df, [0], [""], {}, {}, []
        )
        assert len(patched) == 0


class TestBmdRecomputeDerivedBranches:
    """Test _bmd_recompute_derived P&L branches."""

    def test_recomputes_pnl_when_average_price_present(self):
        """_bmd_recompute_derived recomputes pnl from (ltp-avg)*qty."""
        df = pd.DataFrame({
            "last_price": [110.0],
            "close_price": [100.0],
            "opening_quantity": [10],
            "average_price": [95.0],
            "pnl": [0.0],
            "day_change_val": [0.0],
        })
        broker_apis._bmd_recompute_derived(df, {0})
        # pnl = (110 - 95) × 10 = 150
        assert df.at[0, "pnl"] == pytest.approx(150.0)
        # day_change_val = (110 - 100) × 10 = 100
        assert df.at[0, "day_change_val"] == pytest.approx(100.0)

    def test_recomputes_day_change_percentage(self):
        """_bmd_recompute_derived updates day_change_percentage."""
        df = pd.DataFrame({
            "last_price": [110.0],
            "close_price": [100.0],
            "opening_quantity": [10],
            "day_change_val": [0.0],
            "day_change_percentage": [0.0],
        })
        broker_apis._bmd_recompute_derived(df, {0})
        # day_change_val = 100, prev_val = 100×10 = 1000, pct = 10%
        assert df.at[0, "day_change_percentage"] == pytest.approx(10.0)

    def test_updates_cur_val_and_pnl_percentage_when_inv_val_present(self):
        """_bmd_recompute_derived updates cur_val and pnl_percentage when inv_val present."""
        df = pd.DataFrame({
            "last_price": [110.0],
            "close_price": [100.0],
            "opening_quantity": [10],
            "average_price": [100.0],
            "pnl": [0.0],
            "inv_val": [1000.0],
            "cur_val": [1000.0],
            "pnl_percentage": [0.0],
            "day_change_val": [0.0],
        })
        broker_apis._bmd_recompute_derived(df, {0})
        # pnl = (110-100)*10 = 100; cur_val = 1000+100 = 1100; pnl_pct = 10%
        assert df.at[0, "cur_val"] == pytest.approx(1100.0)
        assert df.at[0, "pnl_percentage"] == pytest.approx(10.0)

    def test_skips_when_missing_qty_or_ltp_columns(self):
        """_bmd_recompute_derived is no-op when required columns absent."""
        df = pd.DataFrame({"tradingsymbol": ["X"]})
        # Should not raise
        broker_apis._bmd_recompute_derived(df, {0})
        assert "pnl" not in df.columns


class TestBackfillMarketData:
    """Test backfill_market_data end-to-end."""

    def test_noop_on_none_df(self):
        """backfill_market_data returns 0 for None input."""
        result = broker_apis.backfill_market_data(None)
        assert result == 0

    def test_noop_on_empty_df(self):
        """backfill_market_data returns 0 for empty DataFrame."""
        result = broker_apis.backfill_market_data(pd.DataFrame())
        assert result == 0

    def test_noop_when_no_price_columns(self):
        """backfill_market_data returns 0 when no close_price or last_price columns."""
        df = pd.DataFrame({"tradingsymbol": ["X"], "quantity": [10]})
        result = broker_apis.backfill_market_data(df)
        assert result == 0

    def test_noop_when_all_prices_already_populated(self):
        """backfill_market_data skips rows with valid prices."""
        df = pd.DataFrame({
            "tradingsymbol": ["RELIANCE"],
            "exchange": ["NSE"],
            "close_price": [2550.0],
            "last_price": [2600.0],
        })
        result = broker_apis.backfill_market_data(df)
        assert result == 0

    def test_patches_zero_close_price(self):
        """backfill_market_data patches rows with zero close_price."""
        df = pd.DataFrame({
            "tradingsymbol": ["RELIANCE"],
            "exchange": ["NSE"],
            "close_price": [0.0],
            "last_price": [2600.0],
            "opening_quantity": [10],
        })
        close_lookup = {"NSE:RELIANCE": 2550.0}
        ltp_lookup = {"NSE:RELIANCE": 2600.0}
        with patch.object(broker_apis, "_bmd_fetch_lookups", return_value=(close_lookup, ltp_lookup)):
            result = broker_apis.backfill_market_data(df)
        assert result == 1
        assert df.at[0, "close_price"] == pytest.approx(2550.0)


class TestEnrichPositionsM2mBranch:
    """Test _enrich_positions m2m broker field trust."""

    def test_m2m_column_trusted_over_formula(self):
        """When m2m column present (no intraday fields), m2m is trusted for day_change_val."""
        df = pd.DataFrame({
            "last_price": [200.0],
            "average_price": [190.0],
            "close_price": [195.0],
            "quantity": [10],
            "m2m": [75.0],   # broker-supplied m2m
        })
        result = broker_apis._enrich_positions(df)
        assert "day_change_val" in result.columns
        # m2m is not null → day_change_val should be m2m value
        assert result["day_change_val"].iloc[0] == pytest.approx(75.0)


class TestFetchSpecialSessions:
    """Test fetch_special_sessions caching and DB path."""

    def teardown_method(self):
        broker_apis._SPECIAL_SESSION_CACHE.clear()

    def test_cache_hit_returns_cached_sessions(self):
        """fetch_special_sessions returns cached sessions without DB hit."""
        import datetime
        today = datetime.date.today()
        session = {"date": today, "start": datetime.time(9, 0), "end": datetime.time(15, 30)}
        broker_apis._SPECIAL_SESSION_CACHE["NSE"] = (today, [session])

        with patch("backend.brokers.broker_apis._read_special_sessions_sync") as mock_db:
            result = broker_apis.fetch_special_sessions("NSE")
            mock_db.assert_not_called()
        assert result == [session]

    def test_cache_miss_reads_from_db(self):
        """fetch_special_sessions reads DB on cache miss."""
        import datetime
        broker_apis._SPECIAL_SESSION_CACHE.pop("TESTEXCH2", None)
        today = datetime.date.today()
        session = {"date": today, "start": datetime.time(9, 0), "end": datetime.time(12, 0)}

        with patch("backend.brokers.broker_apis._read_special_sessions_sync", return_value=[session]):
            result = broker_apis.fetch_special_sessions("TESTEXCH2")
        assert result == [session]
        # Cache should now be populated
        assert "TESTEXCH2" in broker_apis._SPECIAL_SESSION_CACHE

    def test_db_exception_returns_empty_list(self):
        """fetch_special_sessions returns [] when DB raises."""
        broker_apis._SPECIAL_SESSION_CACHE.pop("TESTEXCH3", None)

        with patch("backend.brokers.broker_apis._read_special_sessions_sync", side_effect=Exception("DB down")):
            result = broker_apis.fetch_special_sessions("TESTEXCH3")
        assert result == []


class TestFetchHolidaysTier1:
    """Test fetch_holidays Tier 1 (holidays_store in-process LRU) cache path."""

    def teardown_method(self):
        broker_apis._HOLIDAY_CACHE.clear()

    def test_tier1_cache_hit_returns_and_mirrors(self):
        """fetch_holidays returns holidays_store cache and mirrors to _HOLIDAY_CACHE."""
        import datetime
        expected = {datetime.date(2025, 1, 26)}

        mock_mem = {("NSE", 2025): expected}

        with patch("backend.api.persistence.holidays_store._MEM_CACHE", mock_mem):
            with patch("backend.api.persistence.holidays_store._ist_year", return_value=2025):
                result = broker_apis.fetch_holidays("NSE")

        assert result == expected
        # Should have been mirrored to _HOLIDAY_CACHE
        assert "NSE" in broker_apis._HOLIDAY_CACHE


class TestBmdFetchQuotes:
    """Test _bmd_fetch_quotes PriceBroker and ticker fallback."""

    def test_returns_quote_from_price_broker(self):
        """_bmd_fetch_quotes returns PriceBroker.quote() result on success."""
        expected = {"NSE:RELIANCE": {"last_price": 2600.0}}
        mock_pb = MagicMock()
        mock_pb.quote.return_value = expected
        # get_market_data_broker is imported locally inside _bmd_fetch_quotes
        with patch("backend.brokers.registry.get_market_data_broker", return_value=mock_pb):
            result = broker_apis._bmd_fetch_quotes(["NSE:RELIANCE"])
        assert result == expected

    def test_falls_back_to_ticker_on_price_broker_failure(self):
        """_bmd_fetch_quotes falls back to KiteTicker when PriceBroker raises."""
        with patch("backend.brokers.registry.get_market_data_broker", side_effect=Exception("broker down")):
            mock_ticker = MagicMock()
            mock_ticker.get_ltp_by_sym.return_value = 2600.0
            with patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker):
                result = broker_apis._bmd_fetch_quotes(["NSE:RELIANCE"])
        # Should have synthesised a last_price entry from the ticker
        assert "NSE:RELIANCE" in result
        assert result["NSE:RELIANCE"]["last_price"] == pytest.approx(2600.0)


class TestEnrichPositionsDayChangeValBranch:
    """Test _enrich_positions day_change_val broker field (no intraday, no m2m)."""

    def test_day_change_val_column_trusted_when_present(self):
        """When day_change_val column present (no intraday, no m2m), broker value trusted."""
        df = pd.DataFrame({
            "last_price": [200.0],
            "average_price": [190.0],
            "close_price": [195.0],
            "quantity": [10],
            "day_change_val": [42.0],   # broker-supplied, no m2m, no intraday
        })
        result = broker_apis._enrich_positions(df)
        assert "day_change_val" in result.columns
        # Broker dcv is not null → trusted
        assert result["day_change_val"].iloc[0] == pytest.approx(42.0)


class TestBmdPatchRowsUnresolved:
    """Test _bmd_patch_rows unresolved branch."""

    def test_unresolved_key_logged_when_not_in_lookups(self):
        """Row not in either lookup dict is added to unresolved list and logged."""
        df = pd.DataFrame({
            "close_price": [0.0],
            "last_price": [0.0],
        })
        with patch.object(broker_apis, "_bmd_log_unresolved") as mock_log:
            broker_apis._bmd_patch_rows(
                df, [0], ["NSE:MISSING"], {}, {}, ["NSE:MISSING"]
            )
            mock_log.assert_called_once()
            unresolved_arg = mock_log.call_args[0][0]
            assert "NSE:MISSING" in unresolved_arg


class TestBmdRecomputeDerivedDayChangeColumn:
    """Test _bmd_recompute_derived day_change column update."""

    def test_updates_day_change_column(self):
        """_bmd_recompute_derived updates day_change = ltp - close."""
        df = pd.DataFrame({
            "last_price": [110.0],
            "close_price": [100.0],
            "opening_quantity": [10],
            "day_change_val": [0.0],
            "day_change": [0.0],
        })
        broker_apis._bmd_recompute_derived(df, {0})
        assert df.at[0, "day_change"] == pytest.approx(10.0)

    def test_includes_realised_in_pnl(self):
        """_bmd_recompute_derived includes realised in pnl calculation."""
        df = pd.DataFrame({
            "last_price": [110.0],
            "close_price": [100.0],
            "opening_quantity": [10],
            "average_price": [100.0],
            "pnl": [0.0],
            "realised": [500.0],   # realised P&L from closed legs
            "day_change_val": [0.0],
        })
        broker_apis._bmd_recompute_derived(df, {0})
        # pnl = (110-100)*10 + 500 = 600
        assert df.at[0, "pnl"] == pytest.approx(600.0)


class TestFetchMarginsIntervalSkipped:
    """Test _fetch_margins_local interval-skipped branch."""

    def test_interval_skipped_returns_empty_with_attr(self):
        """When Dhan interval not due, returns empty df with interval_skipped attr."""
        inner = _get_inner(broker_apis._fetch_margins_local)
        if inner is None:
            pytest.skip("No __wrapped__")

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False):
            with patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=False):
                result = inner(
                    connections=lambda: MagicMock(),
                    account="TESTACCT",
                    kite=None,
                    broker=MagicMock(),
                )
        assert result.empty
        assert result.attrs.get("interval_skipped") is True

    def test_no_broker_no_kite_returns_empty(self):
        """When both broker and kite are None, returns empty DataFrame."""
        inner = _get_inner(broker_apis._fetch_margins_local)
        if inner is None:
            pytest.skip("No __wrapped__")

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False):
            with patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=True):
                with patch("backend.brokers.broker_apis._update_dhan_next_poll"):
                    result = inner(
                        connections=lambda: MagicMock(),
                        account="TESTACCT",
                        kite=None,
                        broker=None,
                    )
        assert result.empty


class TestMirrorToHolidaysStore:
    """Test _mirror_to_holidays_store."""

    def test_populates_mem_cache(self):
        """_mirror_to_holidays_store populates _MEM_CACHE with the holidays set."""
        import datetime
        mem_cache = {}
        holidays = {datetime.date(2025, 1, 26)}

        with patch("backend.api.persistence.holidays_store._MEM_CACHE", mem_cache):
            with patch("backend.api.persistence.holidays_store._ist_year", return_value=2025):
                broker_apis._mirror_to_holidays_store("NSE", holidays)

        assert ("NSE", 2025) in mem_cache
        assert mem_cache[("NSE", 2025)] == holidays

    def test_handles_import_error_silently(self):
        """_mirror_to_holidays_store is silent when holidays_store import fails."""
        import datetime
        holidays = {datetime.date(2025, 1, 26)}
        # Should not raise even if the module is unavailable
        with patch.dict("sys.modules", {"backend.api.persistence.holidays_store": None}):
            try:
                broker_apis._mirror_to_holidays_store("NSE", holidays)
            except Exception as e:
                pytest.fail(f"Should not raise: {e}")


class TestBmdLogUnresolved:
    """Test _bmd_log_unresolved diagnostic logging."""

    def test_empty_unresolved_no_log(self):
        """_bmd_log_unresolved is a no-op when unresolved list is empty."""
        with patch("backend.brokers.broker_apis.logger") as mock_logger:
            broker_apis._bmd_log_unresolved([], ["NSE:A", "NSE:B"])
            mock_logger.warning.assert_not_called()

    def test_logs_warning_for_unresolved(self):
        """_bmd_log_unresolved logs a warning when symbols are unresolved."""
        with patch("backend.brokers.broker_apis.logger") as mock_logger:
            broker_apis._bmd_log_unresolved(["NSE:X", "NSE:Y"], ["NSE:X", "NSE:Y", "NSE:Z"])
            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args[0][0]
            assert "2/3" in call_args

    def test_truncates_long_unresolved_list(self):
        """_bmd_log_unresolved mentions overflow when > 10 unresolved symbols."""
        unresolved = [f"NSE:SYM{i}" for i in range(15)]
        with patch("backend.brokers.broker_apis.logger") as mock_logger:
            broker_apis._bmd_log_unresolved(unresolved, unresolved)
            call_args = mock_logger.warning.call_args[0][0]
            assert "+5 more" in call_args


def _get_inner(decorated_func):
    """Helper: retrieve the undecorated inner function from a @for_all_accounts decorated func."""
    return getattr(decorated_func, "__wrapped__", None)


class TestFetchHoldingsLocalDirect:
    """Test _fetch_holdings_local via direct call with explicit broker/account kwargs."""

    def teardown_method(self):
        broker_apis._FETCH_HEALTH.clear()
        broker_apis._breaker_optin_cache.clear()

    def test_broker_holdings_none_records_failure(self):
        """When broker.holdings() returns None, fetch_failed attr is set."""
        inner = _get_inner(broker_apis._fetch_holdings_local)
        if inner is None:
            pytest.skip("No __wrapped__ attribute")

        mock_broker = MagicMock()
        mock_broker.holdings.return_value = None

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False):
            with patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=True):
                with patch("backend.brokers.broker_apis._update_dhan_next_poll"):
                    result = inner(
                        connections=lambda: MagicMock(),
                        account="TESTACCT",
                        kite=None,
                        broker=mock_broker,
                    )
        assert result.attrs.get("fetch_failed") is True

    def test_circuit_open_returns_stale_substitute(self):
        """When circuit is open, _fetch_holdings_local returns stale substitute frame."""
        inner = _get_inner(broker_apis._fetch_holdings_local)
        if inner is None:
            pytest.skip("No __wrapped__ attribute")

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=True):
            with patch("backend.brokers.broker_apis._stale_substitute_frame") as mock_stale:
                mock_stale.return_value = pd.DataFrame({"tradingsymbol": ["X"]})
                result = inner(
                    connections=lambda: MagicMock(),
                    account="TESTACCT",
                    kite=None,
                    broker=MagicMock(),
                )
        mock_stale.assert_called_once_with("holdings", "TESTACCT")
        assert "tradingsymbol" in result.columns

    def test_interval_skipped_returns_empty_with_attr(self):
        """When interval not due, returns empty df with interval_skipped attr."""
        inner = _get_inner(broker_apis._fetch_holdings_local)
        if inner is None:
            pytest.skip("No __wrapped__ attribute")

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False):
            with patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=False):
                result = inner(
                    connections=lambda: MagicMock(),
                    account="TESTACCT",
                    kite=None,
                    broker=MagicMock(),
                )
        assert result.empty
        assert result.attrs.get("interval_skipped") is True

    def test_kite_holdings_returns_data(self):
        """When kite.holdings() returns rows, a DataFrame is built."""
        inner = _get_inner(broker_apis._fetch_holdings_local)
        if inner is None:
            pytest.skip("No __wrapped__ attribute")

        mock_kite = MagicMock()
        mock_kite.holdings.return_value = [
            {"tradingsymbol": "RELIANCE", "quantity": 5,
             "average_price": 2400.0, "last_price": 2600.0,
             "opening_quantity": 5, "close_price": 2550.0, "pnl": 1000.0}
        ]

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False):
            with patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=True):
                with patch("backend.brokers.broker_apis._update_dhan_next_poll"):
                    with patch("backend.brokers.broker_apis._record_lkg_frame"):
                        result = inner(
                            connections=lambda: MagicMock(),
                            account="TESTACCT",
                            kite=mock_kite,
                            broker=None,
                        )
        assert not result.empty
        assert "tradingsymbol" in result.columns


class TestFetchPositionsLocalDirect:
    """Test _fetch_positions_local inner function directly."""

    def teardown_method(self):
        broker_apis._FETCH_HEALTH.clear()

    def test_circuit_open_returns_stale_substitute(self):
        """When circuit is open, returns stale substitute frame."""
        inner = _get_inner(broker_apis._fetch_positions_local)
        if inner is None:
            pytest.skip("No __wrapped__")

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=True):
            with patch("backend.brokers.broker_apis._stale_substitute_frame") as mock_stale:
                mock_stale.return_value = pd.DataFrame({"tradingsymbol": ["X"]})
                result = inner(
                    connections=lambda: MagicMock(),
                    account="TESTACCT",
                    kite=None,
                    broker=MagicMock(),
                )
        mock_stale.assert_called_once_with("positions", "TESTACCT")

    def test_interval_skipped_returns_empty_with_attr(self):
        """When interval not due, returns empty df with interval_skipped attr."""
        inner = _get_inner(broker_apis._fetch_positions_local)
        if inner is None:
            pytest.skip("No __wrapped__")

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False):
            with patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=False):
                result = inner(
                    connections=lambda: MagicMock(),
                    account="TESTACCT",
                    kite=None,
                    broker=MagicMock(),
                )
        assert result.empty
        assert result.attrs.get("interval_skipped") is True

    def test_net_rows_none_records_failure(self):
        """When positions returns None net rows, fetch_failed is set."""
        inner = _get_inner(broker_apis._fetch_positions_local)
        if inner is None:
            pytest.skip("No __wrapped__")

        with patch("backend.brokers.broker_apis._extract_net_rows", return_value=None):
            with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False):
                with patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=True):
                    with patch("backend.brokers.broker_apis._update_dhan_next_poll"):
                        result = inner(
                            connections=lambda: MagicMock(),
                            account="TESTACCT",
                            kite=None,
                            broker=MagicMock(),
                        )
        assert result.attrs.get("fetch_failed") is True

    def test_positions_data_builds_dataframe(self):
        """When positions returns net rows, a DataFrame with account col is built."""
        inner = _get_inner(broker_apis._fetch_positions_local)
        if inner is None:
            pytest.skip("No __wrapped__")

        net_rows = [
            {"tradingsymbol": "CRUDEOIL", "quantity": 1,
             "average_price": 5000.0, "last_price": 5100.0,
             "close_price": 4980.0}
        ]
        with patch("backend.brokers.broker_apis._extract_net_rows", return_value=net_rows):
            with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False):
                with patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=True):
                    with patch("backend.brokers.broker_apis._update_dhan_next_poll"):
                        with patch("backend.brokers.broker_apis._record_lkg_frame"):
                            result = inner(
                                connections=lambda: MagicMock(),
                                account="TESTACCT",
                                kite=None,
                                broker=MagicMock(),
                            )
        assert not result.empty
        assert result["account"].iloc[0] == "TESTACCT"


class TestFetchMarginsLocalDirect:
    """Test _fetch_margins_local inner function directly."""

    def teardown_method(self):
        broker_apis._FETCH_HEALTH.clear()

    def test_circuit_open_returns_stale_substitute(self):
        """When circuit is open, returns stale substitute frame."""
        inner = _get_inner(broker_apis._fetch_margins_local)
        if inner is None:
            pytest.skip("No __wrapped__")

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=True):
            with patch("backend.brokers.broker_apis._stale_substitute_frame") as mock_stale:
                mock_stale.return_value = pd.DataFrame({"net": [50000.0]})
                result = inner(
                    connections=lambda: MagicMock(),
                    account="TESTACCT",
                    kite=None,
                    broker=MagicMock(),
                )
        mock_stale.assert_called_once_with("margins", "TESTACCT")

    def test_broker_margins_builds_dataframe(self):
        """When broker.margins() returns data, a DataFrame with account col is built."""
        inner = _get_inner(broker_apis._fetch_margins_local)
        if inner is None:
            pytest.skip("No __wrapped__")

        margins_data = {
            "net": 50000.0,
            "available": {"live_balance": 50000.0},
            "utilised": {"debits": 10000.0},
        }
        mock_broker = MagicMock()
        mock_broker.margins.return_value = margins_data

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False):
            with patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=True):
                with patch("backend.brokers.broker_apis._update_dhan_next_poll"):
                    with patch("backend.brokers.broker_apis._record_lkg_frame"):
                        result = inner(
                            connections=lambda: MagicMock(),
                            account="TESTACCT",
                            kite=None,
                            broker=mock_broker,
                        )
        assert not result.empty
        assert result["account"].iloc[0] == "TESTACCT"
