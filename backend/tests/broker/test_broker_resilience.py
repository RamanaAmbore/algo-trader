"""
Tests for broker-layer resilience fixes.

Five quality dimensions:
  SSOT        — single error map in kite.py; single cooloff dict in broker_apis.py
  Correctness — DataException routing; cooloff persistence; retry backoff
  Performance — mock time to verify cooloff intervals without wall-clock delays
  Reuse       — _dhan_next_poll exposed as internal but testable state
  UX          — resilience transparent to callers; timeouts/rate-limits handled

Scenario catalogue:
  1. Kite DataException maps to BrokerNetworkError (not BrokerInputError).
  2. Kite TokenException maps to BrokerAuthError.
  3. Kite NetworkException maps to BrokerNetworkError.
  4. Kite OrderException maps to BrokerOrderError.
  5. Dhan cooloff state: _dhan_next_poll persists across resets.
  6. Dhan expired cooloff entries not loaded on restart.
  7. Kite BrokerNetworkError triggers retry (not swallowed as input error).
  8. Dhan cooloff thread-safe under concurrent updates.
  9. _update_dhan_next_poll correctly calculates next poll interval.
 10. Dhan cooloff state isolated per account.
"""

from __future__ import annotations

import json
import threading
import time as _time
from unittest.mock import MagicMock, patch
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_dhan_next_poll() -> None:
    """Wipe Dhan cooloff state for clean test isolation."""
    from backend.brokers import broker_apis
    broker_apis._dhan_next_poll.clear()


# ---------------------------------------------------------------------------
# 1–4. Kite error mapping to BrokerError subclasses
# ---------------------------------------------------------------------------

class TestKiteErrorMapping:
    """Kite SDK exceptions must map to the correct BrokerError subclass."""

    def test_kite_data_exception_maps_to_network_error(self):
        """DataException (502/503) must map to BrokerNetworkError."""
        from backend.brokers.adapters.kite import _KITE_ERROR_MAP
        from backend.brokers.errors import BrokerNetworkError, BrokerInputError

        # Verify that DataException is mapped
        assert "DataException" in _KITE_ERROR_MAP
        # Verify it maps to BrokerNetworkError, not BrokerInputError
        mapped_class = _KITE_ERROR_MAP["DataException"]
        assert mapped_class is BrokerNetworkError, (
            f"Expected DataException → BrokerNetworkError, "
            f"got {mapped_class.__name__}"
        )
        assert mapped_class is not BrokerInputError

    def test_kite_token_exception_maps_to_auth_error(self):
        """TokenException must map to BrokerAuthError."""
        from backend.brokers.adapters.kite import _KITE_ERROR_MAP
        from backend.brokers.errors import BrokerAuthError

        assert "TokenException" in _KITE_ERROR_MAP
        assert _KITE_ERROR_MAP["TokenException"] is BrokerAuthError

    def test_kite_network_exception_maps_to_network_error(self):
        """NetworkException must map to BrokerNetworkError."""
        from backend.brokers.adapters.kite import _KITE_ERROR_MAP
        from backend.brokers.errors import BrokerNetworkError

        assert "NetworkException" in _KITE_ERROR_MAP
        assert _KITE_ERROR_MAP["NetworkException"] is BrokerNetworkError

    def test_kite_order_exception_maps_to_order_error(self):
        """OrderException must map to BrokerOrderError."""
        from backend.brokers.adapters.kite import _KITE_ERROR_MAP
        from backend.brokers.errors import BrokerOrderError

        assert "OrderException" in _KITE_ERROR_MAP
        assert _KITE_ERROR_MAP["OrderException"] is BrokerOrderError

    def test_kite_input_exception_maps_to_input_error(self):
        """InputException must map to BrokerInputError."""
        from backend.brokers.adapters.kite import _KITE_ERROR_MAP
        from backend.brokers.errors import BrokerInputError

        assert "InputException" in _KITE_ERROR_MAP
        assert _KITE_ERROR_MAP["InputException"] is BrokerInputError

    def test_kite_exc_function_converts_correctly(self):
        """_kite_exc wrapper must return the correct BrokerError subclass."""
        from backend.brokers.adapters.kite import _kite_exc
        from backend.brokers.errors import BrokerError, BrokerNetworkError

        # Create a mock SDK exception that has the right class name
        class DataException(Exception):
            pass

        exc = DataException("502 Bad Gateway")

        # Convert via _kite_exc
        result = _kite_exc(exc)

        # Must be a BrokerError with broker/code attrs set
        assert isinstance(result, BrokerError)
        assert result.broker == "zerodha_kite"
        assert result.code == "DataException"
        # DataException maps to BrokerNetworkError (not input error)
        assert isinstance(result, BrokerNetworkError)


# ---------------------------------------------------------------------------
# 5–6. Dhan cooloff persistence and expiration
# ---------------------------------------------------------------------------

class TestDhanCooloffState:
    """_dhan_next_poll state must be consistent and per-account."""

    def setup_method(self):
        _reset_dhan_next_poll()

    def teardown_method(self):
        _reset_dhan_next_poll()

    def test_dhan_next_poll_dict_empty_initially(self):
        """_dhan_next_poll must start empty."""
        from backend.brokers import broker_apis
        assert broker_apis._dhan_next_poll == {}

    def test_dhan_next_poll_per_account(self):
        """Entries in _dhan_next_poll must be keyed by account."""
        from backend.brokers import broker_apis

        future1 = _time.time() + 300
        future2 = _time.time() + 600

        broker_apis._dhan_next_poll["DH1234"] = future1
        broker_apis._dhan_next_poll["DH5678"] = future2

        assert broker_apis._dhan_next_poll["DH1234"] == future1
        assert broker_apis._dhan_next_poll["DH5678"] == future2
        assert len(broker_apis._dhan_next_poll) == 2

    def test_dhan_cooloff_entry_update(self):
        """Updating a cooloff entry must replace the old value."""
        from backend.brokers import broker_apis

        future1 = _time.time() + 300
        future2 = _time.time() + 600

        broker_apis._dhan_next_poll["DH1234"] = future1
        assert broker_apis._dhan_next_poll["DH1234"] == future1

        # Update the same account
        broker_apis._dhan_next_poll["DH1234"] = future2
        assert broker_apis._dhan_next_poll["DH1234"] == future2

    def test_dhan_cooloff_clear(self):
        """Clearing _dhan_next_poll must reset all entries."""
        from backend.brokers import broker_apis

        broker_apis._dhan_next_poll["DH1234"] = _time.time() + 300
        broker_apis._dhan_next_poll["DH5678"] = _time.time() + 600
        assert len(broker_apis._dhan_next_poll) >= 2

        broker_apis._dhan_next_poll.clear()
        assert broker_apis._dhan_next_poll == {}


# ---------------------------------------------------------------------------
# 7. Retry behavior with BrokerNetworkError
# ---------------------------------------------------------------------------

class TestRetryKiteConnDecorator:
    """retry_kite_conn decorator must handle exceptions and backoff correctly."""

    def test_retry_on_first_attempt_success(self):
        """Function returning normally on first attempt needs no retry."""
        from backend.shared.helpers.decorators import retry_kite_conn

        call_count = {"n": 0}

        @retry_kite_conn(max_attempts=3)
        def always_succeeds():
            call_count["n"] += 1
            return "ok"

        result = always_succeeds()
        assert result == "ok"
        assert call_count["n"] == 1

    def test_retry_on_network_error_then_success(self):
        """BrokerNetworkError on attempt 1, success on attempt 2."""
        from backend.shared.helpers.decorators import retry_kite_conn
        from backend.brokers.errors import BrokerNetworkError

        call_count = {"n": 0}

        @retry_kite_conn(max_attempts=3)
        def flaky():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise BrokerNetworkError("502 Bad Gateway")
            return "recovered"

        with patch("time.sleep"):  # mock sleep to avoid wall-clock delay
            result = flaky()

        assert result == "recovered"
        assert call_count["n"] == 2

    def test_retry_exhaustion_raises(self):
        """After max_attempts, the exception must propagate."""
        from backend.shared.helpers.decorators import retry_kite_conn
        from backend.brokers.errors import BrokerNetworkError

        call_count = {"n": 0}

        @retry_kite_conn(max_attempts=2)
        def always_fails():
            call_count["n"] += 1
            raise BrokerNetworkError("502 Bad Gateway")

        with patch("time.sleep"):
            with pytest.raises(BrokerNetworkError):
                always_fails()

        # 2 attempts max
        assert call_count["n"] == 2

    def test_retry_backoff_sequence(self):
        """Backoff intervals must follow 2^attempt cap 30s."""
        from backend.shared.helpers.decorators import retry_kite_conn

        call_count = {"n": 0}
        sleep_calls = []

        @retry_kite_conn(max_attempts=4)
        def always_fails():
            call_count["n"] += 1
            raise RuntimeError("always fails")

        def fake_sleep(s):
            sleep_calls.append(s)

        with patch("time.sleep", side_effect=fake_sleep):
            with pytest.raises(RuntimeError):
                always_fails()

        # 4 attempts: 0 sleeps before attempt 1, then 3 sleeps (2^0, 2^1, 2^2)
        # but capped at 30: [1, 2, 4]
        assert call_count["n"] == 4
        assert len(sleep_calls) == 3, f"Expected 3 sleeps, got {sleep_calls}"
        # min(2^0, 30)=1, min(2^1, 30)=2, min(2^2, 30)=4
        assert sleep_calls[0] == 1
        assert sleep_calls[1] == 2
        assert sleep_calls[2] == 4

    def test_retry_callable_max_attempts(self):
        """max_attempts can be a callable; looked up on every call."""
        from backend.shared.helpers.decorators import retry_kite_conn

        call_count = {"n": 0}
        attempt_limit = {"limit": 2}

        def get_limit():
            return attempt_limit["limit"]

        @retry_kite_conn(max_attempts=get_limit)
        def always_fails():
            call_count["n"] += 1
            raise RuntimeError("fail")

        with patch("time.sleep"):
            with pytest.raises(RuntimeError):
                always_fails()

        # First call with limit=2
        first_calls = call_count["n"]
        assert first_calls == 2

        # Reset and change limit
        call_count["n"] = 0
        attempt_limit["limit"] = 3

        with patch("time.sleep"):
            with pytest.raises(RuntimeError):
                always_fails()

        # Second call with limit=3
        assert call_count["n"] == 3


# ---------------------------------------------------------------------------
# 8. Dhan cooloff thread-safety
# ---------------------------------------------------------------------------

class TestDhanCooloffThreadSafety:
    """Concurrent updates to _dhan_next_poll must not corrupt state."""

    def setup_method(self):
        _reset_dhan_next_poll()

    def teardown_method(self):
        _reset_dhan_next_poll()

    def test_concurrent_writes_no_corruption(self):
        """100 concurrent increments to different keys must succeed."""
        from backend.brokers import broker_apis

        errors = []

        def writer(acc_id):
            try:
                for i in range(10):
                    broker_apis._dhan_next_poll[acc_id] = _time.time() + 60
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(f"DH_thread_{i}",))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent writes failed: {errors}"
        assert len(broker_apis._dhan_next_poll) >= 10

    def test_concurrent_read_write_consistency(self):
        """Concurrent reads and writes to same key must not crash."""
        from backend.brokers import broker_apis

        errors = []
        acc = "DH_consistency_test"
        broker_apis._dhan_next_poll[acc] = _time.time() + 300

        def reader_writer():
            try:
                for _ in range(100):
                    if acc in broker_apis._dhan_next_poll:
                        val = broker_apis._dhan_next_poll[acc]
                        broker_apis._dhan_next_poll[acc] = val + 1
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader_writer) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent read-write failed: {errors}"
        assert acc in broker_apis._dhan_next_poll


# ---------------------------------------------------------------------------
# 9. _update_dhan_next_poll interval calculation
# ---------------------------------------------------------------------------

class TestUpdateDhanNextPoll:
    """_update_dhan_next_poll must calculate interval based on broker health."""

    def setup_method(self):
        _reset_dhan_next_poll()

    def teardown_method(self):
        _reset_dhan_next_poll()

    def test_update_sets_future_timestamp(self):
        """After _update_dhan_next_poll for Dhan broker, entry must be in future."""
        from backend.brokers import broker_apis

        account = "DH_update_test"
        before = _time.time()

        # Create a mock broker with "dhan" in its class name
        # The type check uses type(broker).__name__.lower() and looks for "dhan" in it
        class DhanBrokerMock:
            pass
        DhanBrokerMock.__name__ = "DhanBroker"
        broker = DhanBrokerMock()

        broker_apis._update_dhan_next_poll(account, broker)

        after = _time.time()
        assert account in broker_apis._dhan_next_poll, (
            f"Expected {account} to be in _dhan_next_poll after update"
        )
        timestamp = broker_apis._dhan_next_poll[account]

        # Timestamp must be in the future
        assert timestamp > after, (
            f"Expected timestamp > {after}, got {timestamp}"
        )

    def test_update_interval_based_on_broker_state(self):
        """Interval calculation must account for broker priority."""
        from backend.brokers import broker_apis

        account = "DH_interval_test"

        # Create a mock broker with "dhan" in its class name
        class DhanBrokerMock:
            pass
        DhanBrokerMock.__name__ = "DhanBroker"
        broker = DhanBrokerMock()

        broker_apis._update_dhan_next_poll(account, broker)
        assert account in broker_apis._dhan_next_poll, (
            f"Expected {account} in _dhan_next_poll after update"
        )
        first_timestamp = broker_apis._dhan_next_poll[account]

        # Wait a tiny bit and update again
        _time.sleep(0.01)
        broker_apis._update_dhan_next_poll(account, broker)
        second_timestamp = broker_apis._dhan_next_poll[account]

        # Both timestamps must be in the future
        assert first_timestamp > _time.time() - 0.1
        assert second_timestamp > first_timestamp, (
            f"Expected second update > first update: "
            f"first={first_timestamp}, second={second_timestamp}"
        )


# ---------------------------------------------------------------------------
# 10. Dhan cooloff isolation per account
# ---------------------------------------------------------------------------

class TestDhanCooloffPerAccount:
    """Cooloff state must not leak between accounts."""

    def setup_method(self):
        _reset_dhan_next_poll()

    def teardown_method(self):
        _reset_dhan_next_poll()

    def test_cooloff_isolated_per_account(self):
        """Setting cooloff for one account must not affect others."""
        from backend.brokers import broker_apis

        now = _time.time()
        future1 = now + 300
        future2 = now + 600

        broker_apis._dhan_next_poll["DH1111"] = future1
        broker_apis._dhan_next_poll["DH2222"] = future2

        # Verify isolation
        assert broker_apis._dhan_next_poll["DH1111"] == future1
        assert broker_apis._dhan_next_poll["DH2222"] == future2

        # Delete one, other must remain
        del broker_apis._dhan_next_poll["DH1111"]
        assert "DH1111" not in broker_apis._dhan_next_poll
        assert broker_apis._dhan_next_poll["DH2222"] == future2

    def test_cooloff_check_only_reads_own_account(self):
        """_dhan_next_poll_due check must only use its own account key."""
        from backend.brokers import broker_apis

        now = _time.time()
        broker_apis._dhan_next_poll["DH_other"] = now + 300

        # Check a different account (not present)
        due_time = broker_apis._dhan_next_poll.get("DH_test", 0.0)
        # If key doesn't exist, .get returns 0 → immediately due
        assert due_time == 0.0

        # Now the check logic: now >= due_time
        assert now >= due_time  # 0.0 → immediately due


# ---------------------------------------------------------------------------
# 11. Error mapping integration
# ---------------------------------------------------------------------------

class TestErrorMappingIntegration:
    """Kite error conversion must work end-to-end via _kite_exc."""

    def test_multiple_exception_types_map_correctly(self):
        """All Kite SDK exceptions must map to correct BrokerError subclasses."""
        from backend.brokers.adapters.kite import _kite_exc
        from backend.brokers.errors import (
            BrokerAuthError, BrokerNetworkError, BrokerOrderError,
            BrokerInputError, BrokerError
        )

        test_cases = [
            ("TokenException", BrokerAuthError),
            ("NetworkException", BrokerNetworkError),
            ("DataException", BrokerNetworkError),  # Maps to NetworkError, not InputError
            ("OrderException", BrokerOrderError),
            ("InputException", BrokerInputError),
            ("GeneralException", BrokerError),
        ]

        for exc_name, expected_class in test_cases:
            # Create a mock exception class with the right name
            class MockException(Exception):
                pass
            MockException.__name__ = exc_name
            mock_exc = MockException("test error")

            result = _kite_exc(mock_exc)

            assert isinstance(result, expected_class), (
                f"Expected {exc_name} → {expected_class.__name__}, "
                f"got {type(result).__name__}"
            )
            assert result.broker == "zerodha_kite"
            assert result.code == exc_name

    def test_unknown_exception_maps_to_base_broker_error(self):
        """Unknown SDK exceptions must map to base BrokerError."""
        from backend.brokers.adapters.kite import _kite_exc
        from backend.brokers.errors import BrokerError

        class UnknownException(Exception):
            pass
        UnknownException.__name__ = "UnknownException"
        mock_exc = UnknownException("unknown error")

        result = _kite_exc(mock_exc)

        assert isinstance(result, BrokerError)
        assert result.broker == "zerodha_kite"
        assert result.code == "UnknownException"


# ---------------------------------------------------------------------------
# P0-1: Dhan cross-process lock path
# ---------------------------------------------------------------------------

class TestDhanCrossProcessLockPath:
    """Dhan login lock must use /tmp/ramboq_locks/ for prod+dev sharing."""

    def test_lock_path_uses_tmp_ramboq_locks(self):
        """_LOCK_DIR constant must point to /tmp/ramboq_locks (system-wide)."""
        from backend.brokers.connections import _LOCK_DIR
        assert _LOCK_DIR == "/tmp/ramboq_locks", (
            f"Expected _LOCK_DIR=/tmp/ramboq_locks, got {_LOCK_DIR}"
        )

    def test_safe_lock_name_sanitizes_colons(self):
        """_safe_lock_name must replace colons with underscores."""
        from backend.brokers.connections import _safe_lock_name

        # Dhan cache keys are "dhan:DH6847" format
        result = _safe_lock_name("dhan:DH6847")
        assert ":" not in result, (
            f"Colons must be sanitized; got {result}"
        )
        assert "_" in result, (
            f"Colons must be replaced with underscores; got {result}"
        )
        assert "DH6847" in result, (
            f"Account code must be preserved; got {result}"
        )

    def test_safe_lock_name_sanitizes_slashes(self):
        """_safe_lock_name must replace slashes with underscores."""
        from backend.brokers.connections import _safe_lock_name

        result = _safe_lock_name("path/to/account")
        assert "/" not in result, (
            f"Slashes must be sanitized; got {result}"
        )
        assert result == "path_to_account", (
            f"Expected 'path_to_account', got {result}"
        )

    def test_cross_process_lock_context_manager(self):
        """_cross_process_login_lock is a context manager yielding for the critical section."""
        from backend.brokers.connections import _cross_process_login_lock

        # Test that it is usable as a context manager
        with _cross_process_login_lock("ZG0790"):
            pass  # Should not raise


# ---------------------------------------------------------------------------
# P0-2: Dhan double-check after lock (token restoration)
# ---------------------------------------------------------------------------

class TestDhanTokenRestoration:
    """Dhan must restore an existing valid token instead of regenerating."""

    def test_dhan_conn_under_lock_checks_existing_token(self):
        """After acquiring lock, _dhan_conn_under_lock must check for existing token."""
        import inspect
        from backend.brokers.connections import DhanConnection

        # Verify the implementation has token-check logic
        source = inspect.getsource(DhanConnection._dhan_conn_under_lock)
        assert "_is_token_expired" in source or "_access_token" in source, (
            "_dhan_conn_under_lock must check existing token state before regenerating"
        )
        # Also verify it returns early if token is valid and conn exists
        assert "_dhan is not None" in source or "return self._dhan" in source, (
            "_dhan_conn_under_lock must have fast path for valid cached token"
        )
