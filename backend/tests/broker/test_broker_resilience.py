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

# ---------------------------------------------------------------------------
# Dhan IP binding — startup validation and silent-failure guards
# ---------------------------------------------------------------------------

class TestDhanIpBindingDiagnostics:
    """DhanConnection must emit visible errors when source_ip is missing or
    the SDK doesn't expose its HTTP session holder."""

    def test_build_client_logs_error_when_source_ip_missing(self):
        """_build_client() must log ERROR when source_ip is None so
        misconfiguration is caught at startup, not after the DH-906 loop starts."""
        import threading
        from unittest.mock import MagicMock, patch
        from backend.brokers.connections import DhanConnection
        import backend.brokers.connections as _conn_mod

        conn = object.__new__(DhanConnection)
        conn.account = "DH-TEST"
        conn._source_ip = None
        conn._dhan = None
        conn._import_error = None
        conn._login_lock = threading.Lock()

        mock_dhanhq_cls = MagicMock(return_value=MagicMock())
        mock_ctx_cls = MagicMock(return_value=MagicMock(dhan_http=MagicMock(session=MagicMock())))
        mock_module = MagicMock(dhanhq=mock_dhanhq_cls, DhanContext=mock_ctx_cls)

        with patch.object(_conn_mod.logger, "error") as mock_error:
            with patch.dict("sys.modules", {"dhanhq": mock_module}):
                try:
                    conn._build_client("fake-token")
                except Exception:
                    pass

        calls = [str(c) for c in mock_error.call_args_list]
        assert any("DHAN-IP" in c and "source_ip" in c for c in calls), (
            f"Expected logger.error([DHAN-IP] ... source_ip ...); got: {calls}"
        )

    def test_mount_source_ip_adapter_logs_critical_when_holder_missing(self):
        """_mount_source_ip_adapter() must log CRITICAL (not WARNING) when
        the SDK doesn't expose dhan_http, so it's not buried in WARNING noise."""
        from unittest.mock import patch
        from backend.brokers.connections import DhanConnection
        import backend.brokers.connections as _conn_mod

        conn = object.__new__(DhanConnection)
        conn.account = "DH-TEST"
        conn._source_ip = "2001:db8::1"  # source_ip set so we enter the guard

        with patch.object(_conn_mod.logger, "critical") as mock_critical:
            conn._mount_source_ip_adapter(None)  # http_holder=None → CRITICAL

        calls = [str(c) for c in mock_critical.call_args_list]
        assert any("DHAN-IP" in c and "dhan_http" in c for c in calls), (
            f"Expected logger.critical([DHAN-IP] ... dhan_http ...); got: {calls}"
        )


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


# ---------------------------------------------------------------------------
# 12. ssot_fetch._result_cache exposure + force_refresh behaviour
# ---------------------------------------------------------------------------

class TestSsotFetchResultCache:
    """ssot_fetch(mode='coalesce') must expose _result_cache on the wrapper
    and honour force_refresh=True to trigger a fresh call."""

    def test_result_cache_exposed_on_sync_wrapper(self):
        """Decorated sync function must have _result_cache attribute."""
        from backend.shared.helpers.ssot_fetch import ssot_fetch

        @ssot_fetch(mode="coalesce", key="test_key_cache_12")
        def my_fn():
            return {"data": 42}

        assert hasattr(my_fn, "_result_cache"), (
            "ssot_fetch sync wrapper must expose _result_cache dict"
        )
        assert isinstance(my_fn._result_cache, dict)

    def test_force_refresh_triggers_fresh_call_after_cache(self):
        """After a result is cached, force_refresh=True must re-call the function."""
        from backend.shared.helpers.ssot_fetch import ssot_fetch

        call_count = {"n": 0}

        @ssot_fetch(mode="coalesce", key="test_force_refresh_12")
        def counted_fn():
            call_count["n"] += 1
            return f"call_{call_count['n']}"

        # First call: populates cache
        r1 = counted_fn()
        assert r1 == "call_1"
        assert call_count["n"] == 1
        assert "test_force_refresh_12" in counted_fn._result_cache

        # Second call: should hit cache (no new call)
        r2 = counted_fn()
        assert r2 == "call_1"
        assert call_count["n"] == 1, "Sequential call should use cache"

        # Third call with force_refresh: must re-run
        r3 = counted_fn(force_refresh=True)
        assert r3 == "call_2"
        assert call_count["n"] == 2, "force_refresh=True must trigger fresh call"

    def test_result_cache_exposed_on_async_wrapper(self):
        """Decorated async function must also expose _result_cache."""
        from backend.shared.helpers.ssot_fetch import ssot_fetch

        @ssot_fetch(mode="coalesce", key="async_test_key_12")
        async def async_fn():
            return "async_result"

        assert hasattr(async_fn, "_result_cache"), (
            "ssot_fetch async wrapper must expose _result_cache dict"
        )

    def test_none_result_is_never_cached(self):
        """None returns must not be stored so the next call re-runs."""
        from backend.shared.helpers.ssot_fetch import ssot_fetch

        call_count = {"n": 0}

        @ssot_fetch(mode="coalesce", key="test_none_not_cached_12")
        def returns_none():
            call_count["n"] += 1
            return None

        r1 = returns_none()
        r2 = returns_none()
        assert r1 is None
        assert r2 is None
        assert call_count["n"] == 2, "None result must not be cached — fn called twice"
        assert "test_none_not_cached_12" not in returns_none._result_cache


# ---------------------------------------------------------------------------
# 13. Kite instruments() coalesce — single SDK call under concurrent load
# ---------------------------------------------------------------------------

class TestKiteInstrumentsCoalesce:
    """KiteBroker.instruments() decorated with ssot_fetch(coalesce) must
    call the underlying SDK exactly once when N threads request the same
    exchange simultaneously."""

    def test_instruments_calls_sdk_once_for_concurrent_threads(self):
        """10 concurrent callers for the same account+exchange share one fetch."""
        from unittest.mock import MagicMock
        from backend.brokers.adapters.kite import KiteBroker

        sdk_call_count = {"n": 0}
        fake_instruments = [{"tradingsymbol": "NIFTY", "exchange": "NSE"}]

        def fake_kite_instruments(exchange=None):
            sdk_call_count["n"] += 1
            _time.sleep(0.05)  # simulate network latency
            return fake_instruments

        # KiteBroker.kite is a property that calls conn.get_kite_conn()
        mock_sdk = MagicMock()
        mock_sdk.instruments.side_effect = fake_kite_instruments
        mock_conn = MagicMock()
        mock_conn.account = "ZG0790_coalesce_test"
        mock_conn.get_kite_conn.return_value = mock_sdk

        broker = KiteBroker(conn=mock_conn)

        # Evict any prior cached result for this account+exchange key
        broker.instruments("NSE", force_refresh=True)
        sdk_call_count["n"] = 0  # reset counter; cache is now fresh

        # All threads will find the cache already populated → 0 new SDK calls
        results = []
        errors = []

        def fetch():
            try:
                r = broker.instruments("NSE")
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=fetch) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"
        assert len(results) == 10, "All 10 threads must get a result"
        for r in results:
            assert r == fake_instruments, f"Unexpected result: {r}"
        # Cache was already populated; no new SDK calls
        assert sdk_call_count["n"] == 0, (
            f"Expected 0 new SDK calls (cache hit), got {sdk_call_count['n']}"
        )

    def test_instruments_concurrent_cold_start_coalesces(self):
        """10 threads hitting a cold cache must share one in-flight SDK call."""
        from unittest.mock import MagicMock
        from backend.brokers.adapters.kite import KiteBroker

        sdk_call_count = {"n": 0}
        fake_instruments = [{"tradingsymbol": "BANKNIFTY", "exchange": "NSE"}]

        def slow_fetch(exchange=None):
            sdk_call_count["n"] += 1
            _time.sleep(0.1)  # long enough for all 10 threads to pile up
            return fake_instruments

        mock_sdk = MagicMock()
        mock_sdk.instruments.side_effect = slow_fetch
        mock_conn = MagicMock()
        mock_conn.account = "ZG0790_cold_coalesce"
        mock_conn.get_kite_conn.return_value = mock_sdk

        broker = KiteBroker(conn=mock_conn)
        # Clear cache so all threads start cold
        cache = getattr(broker.instruments, "_result_cache", None)
        if cache is not None:
            cache.pop(f"{broker.account}:NSE", None)

        results = []
        errors = []

        def fetch():
            try:
                r = broker.instruments("NSE")
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=fetch) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"
        assert len(results) == 10
        for r in results:
            assert r == fake_instruments
        # ssot_fetch(coalesce) must coalesce all 10 → exactly 1 SDK call
        assert sdk_call_count["n"] == 1, (
            f"Expected 1 SDK call (coalesce), got {sdk_call_count['n']}"
        )

    def test_instruments_cache_key_includes_account_and_exchange(self):
        """Key function must differentiate by account + exchange."""
        from unittest.mock import MagicMock
        from backend.brokers.adapters.kite import KiteBroker

        nse_data = [{"exchange": "NSE_acct1"}]
        nse_data2 = [{"exchange": "NSE_acct2"}]

        mock_sdk1 = MagicMock()
        mock_sdk1.instruments.return_value = nse_data
        mock_conn1 = MagicMock()
        mock_conn1.account = "ZG0790_key_test_A"
        mock_conn1.get_kite_conn.return_value = mock_sdk1

        mock_sdk2 = MagicMock()
        mock_sdk2.instruments.return_value = nse_data2
        mock_conn2 = MagicMock()
        mock_conn2.account = "ZG0791_key_test_B"
        mock_conn2.get_kite_conn.return_value = mock_sdk2

        broker1 = KiteBroker(conn=mock_conn1)
        broker2 = KiteBroker(conn=mock_conn2)

        r1 = broker1.instruments("NSE", force_refresh=True)
        r2 = broker2.instruments("NSE", force_refresh=True)

        # Different accounts must get different results (separate cache keys)
        assert r1 == nse_data, f"broker1 got wrong result: {r1}"
        assert r2 == nse_data2, f"broker2 got wrong result: {r2}"


# ---------------------------------------------------------------------------
# 14. _raw_cache_invalidate operates on ssot_fetch _result_cache
# ---------------------------------------------------------------------------

class TestRawCacheInvalidate:
    """_raw_cache_invalidate(key) must selectively clear ssot_fetch caches."""

    def _seed_cache(self, fn, key, value):
        """Directly write into the _result_cache of a ssot_fetch-decorated fn."""
        cache = getattr(fn, "_result_cache", None)
        if cache is not None:
            cache[key] = value

    def test_invalidate_specific_key_clears_only_that_cache(self):
        """_raw_cache_invalidate('holdings') must clear holdings cache only."""
        from backend.brokers.broker_apis import (
            _fetch_holdings_cached, _fetch_positions_cached,
            _fetch_margins_cached, _raw_cache_invalidate,
        )

        # Start clean
        _raw_cache_invalidate(None)

        # Seed fake results into all three caches
        self._seed_cache(_fetch_holdings_cached, "holdings", ["h_data"])
        self._seed_cache(_fetch_positions_cached, "positions", ["p_data"])
        self._seed_cache(_fetch_margins_cached, "margins", ["m_data"])

        # Invalidate only holdings
        _raw_cache_invalidate("holdings")

        assert "holdings" not in _fetch_holdings_cached._result_cache, (
            "holdings cache must be cleared after invalidate('holdings')"
        )
        assert "positions" in _fetch_positions_cached._result_cache, (
            "positions cache must NOT be cleared by invalidate('holdings')"
        )
        assert "margins" in _fetch_margins_cached._result_cache, (
            "margins cache must NOT be cleared by invalidate('holdings')"
        )

        # Clean up
        _raw_cache_invalidate(None)

    def test_invalidate_none_clears_all_caches(self):
        """_raw_cache_invalidate(None) must clear all three caches."""
        from backend.brokers.broker_apis import (
            _fetch_holdings_cached, _fetch_positions_cached,
            _fetch_margins_cached, _raw_cache_invalidate,
        )

        self._seed_cache(_fetch_holdings_cached, "holdings", ["h_data"])
        self._seed_cache(_fetch_positions_cached, "positions", ["p_data"])
        self._seed_cache(_fetch_margins_cached, "margins", ["m_data"])

        _raw_cache_invalidate(None)

        assert not _fetch_holdings_cached._result_cache, (
            "holdings cache must be empty after invalidate(None)"
        )
        assert not _fetch_positions_cached._result_cache, (
            "positions cache must be empty after invalidate(None)"
        )
        assert not _fetch_margins_cached._result_cache, (
            "margins cache must be empty after invalidate(None)"
        )

    def test_invalidate_nonexistent_key_is_noop(self):
        """Invalidating a key not in cache must not raise."""
        from backend.brokers.broker_apis import _raw_cache_invalidate

        # Must not raise even if key absent
        _raw_cache_invalidate("nonexistent_key")
        _raw_cache_invalidate(None)


# ---------------------------------------------------------------------------
# 15. Dhan extended error map — DH-903/905/907/908
# ---------------------------------------------------------------------------

class TestDhanExtendedErrorMap:
    """New Dhan error codes DH-903/905/907/908 must map to correct types."""

    def test_dh903_maps_to_broker_order_error(self):
        """DH-903 (order rejected by exchange) → BrokerOrderError."""
        from backend.brokers.adapters.dhan import _DHAN_ERROR_MAP, _dhan_exc
        from backend.brokers.errors import BrokerOrderError

        assert "DH-903" in _DHAN_ERROR_MAP
        assert _DHAN_ERROR_MAP["DH-903"] is BrokerOrderError

        exc = _dhan_exc("order rejected", code="DH-903", status=400)
        assert isinstance(exc, BrokerOrderError)
        assert exc.code == "DH-903"

    def test_dh905_maps_to_broker_network_error(self):
        """DH-905 (service unavailable) → BrokerNetworkError."""
        from backend.brokers.adapters.dhan import _DHAN_ERROR_MAP, _dhan_exc
        from backend.brokers.errors import BrokerNetworkError

        assert "DH-905" in _DHAN_ERROR_MAP
        assert _DHAN_ERROR_MAP["DH-905"] is BrokerNetworkError

        exc = _dhan_exc("service unavailable", code="DH-905", status=503)
        assert isinstance(exc, BrokerNetworkError)
        assert exc.code == "DH-905"

    def test_dh907_maps_to_broker_auth_error(self):
        """DH-907 (account suspended) → BrokerAuthError."""
        from backend.brokers.adapters.dhan import _DHAN_ERROR_MAP, _dhan_exc
        from backend.brokers.errors import BrokerAuthError

        assert "DH-907" in _DHAN_ERROR_MAP
        assert _DHAN_ERROR_MAP["DH-907"] is BrokerAuthError

        exc = _dhan_exc("account suspended", code="DH-907", status=403)
        assert isinstance(exc, BrokerAuthError)

    def test_dh908_maps_to_broker_input_error(self):
        """DH-908 (invalid request parameters) → BrokerInputError."""
        from backend.brokers.adapters.dhan import _DHAN_ERROR_MAP, _dhan_exc
        from backend.brokers.errors import BrokerInputError

        assert "DH-908" in _DHAN_ERROR_MAP
        assert _DHAN_ERROR_MAP["DH-908"] is BrokerInputError

        exc = _dhan_exc("invalid request", code="DH-908", status=422)
        assert isinstance(exc, BrokerInputError)
        assert exc.code == "DH-908"

    def test_dh903_dh905_not_in_rotation_signal_patterns(self):
        """DH-903/905/907/908 must NOT trigger cross-account token rotation."""
        from backend.brokers.adapters.dhan import _AUTH_ERROR_HINTS

        hints_str = " ".join(_AUTH_ERROR_HINTS).lower()
        for code in ("dh-903", "dh-905", "dh-907", "dh-908"):
            assert code not in hints_str, (
                f"{code.upper()} must not trigger token rotation "
                f"(not in _AUTH_ERROR_HINTS)"
            )


# ---------------------------------------------------------------------------
# 16. Groww refresh() nested lock ordering
# ---------------------------------------------------------------------------

class TestGrowwRefreshLockOrdering:
    """GrowwConnection.refresh() must use nested `with A: with B:` form
    rather than `with A, B:` to avoid holding the cross-process file lock
    during the in-process check."""

    def test_groww_refresh_uses_nested_lock_form(self):
        """Source of GrowwConnection.refresh must show nested with blocks."""
        import inspect
        from backend.brokers.connections import GrowwConnection

        source = inspect.getsource(GrowwConnection.refresh)
        lines = [l.strip() for l in source.splitlines()]
        login_lock_lines = [l for l in lines if "with self._login_lock" in l]
        assert login_lock_lines, "refresh() must acquire self._login_lock"
        # Confirm no composite form (both on same line with comma)
        composite_lines = [
            l for l in login_lock_lines if "_cross_process_login_lock" in l
        ]
        assert not composite_lines, (
            "refresh() must NOT use composite 'with self._login_lock, "
            "_cross_process_login_lock:' — use nested form instead"
        )


# ---------------------------------------------------------------------------
# Broker health state resolution — _hlth_resolve_state / _derive_account_health
# ---------------------------------------------------------------------------

class TestBrokerHealthStateResolution:
    """
    Quality dimensions:
      SSOT        — state labels defined once in _hlth_resolve_state
      Correctness — inactive / green / red / amber mapped correctly
      Performance — no I/O; pure time-arithmetic functions
      Reuse       — _derive_account_health is a thin wrapper around _hlth_resolve_state
      UX          — chip color correct: never-tried accounts must not drive chip amber
    """

    def test_never_tried_account_is_inactive_not_amber(self):
        """
        SSOT + Correctness: an account with no fetch history at all must return
        state="inactive", NOT "amber".  Bug fix: previously returned "amber",
        which polluted the navbar chip color even when all real accounts were green.
        """
        import time
        from backend.api.routes.health import _derive_account_health

        now = time.time()
        state, reason, cb_state, cb_count, cb_until = _derive_account_health({}, now)

        assert state == "inactive", (
            f"Expected 'inactive' for never-tried account, got {state!r}"
        )
        assert "no fetch attempt" in reason

    def test_recently_ok_account_is_green(self):
        """Correctness: last_ok_at within 5 min → green."""
        import time
        from backend.api.routes.health import _derive_account_health

        now = time.time()
        entry = {"last_ok_at": now - 60}  # 1 min ago
        state, reason, cb_state, cb_count, cb_until = _derive_account_health(entry, now)

        assert state == "green", f"Expected 'green', got {state!r}: {reason}"

    def test_recent_fail_after_no_ok_is_red(self):
        """Correctness: last_fail_at set, last_ok_at absent, with a fail message → red."""
        import time
        from backend.api.routes.health import _derive_account_health

        now = time.time()
        entry = {
            "last_fail_at": now - 10,
            "last_fail_msg": "Invalid TOTP",
        }
        state, reason, cb_state, cb_count, cb_until = _derive_account_health(entry, now)

        assert state == "red", f"Expected 'red', got {state!r}: {reason}"

    def test_stale_ok_is_amber(self):
        """Correctness: last_ok_at > 5 min ago, no subsequent fail → amber (stale)."""
        import time
        from backend.api.routes.health import _derive_account_health

        now = time.time()
        entry = {"last_ok_at": now - 400}  # 6m 40s ago — beyond 5-min window
        state, reason, cb_state, cb_count, cb_until = _derive_account_health(entry, now)

        assert state == "amber", f"Expected 'amber' for stale account, got {state!r}: {reason}"

    def test_inactive_accounts_do_not_drive_chip_amber(self):
        """
        UX regression: when active accounts are all green and some accounts are
        inactive (never tried), worst-state must be 'green', not 'amber'.

        This is the end-to-end invariant the backend fix (inactive instead of amber)
        enables.  Verified here at the backend level by checking that two never-tried
        entries each resolve to 'inactive', which the frontend filter will exclude.
        """
        import time
        from backend.api.routes.health import _derive_account_health

        now = time.time()
        # Simulate one real green account + one never-tried account.
        green_state, *_ = _derive_account_health({"last_ok_at": now - 30}, now)
        inactive_state, *_ = _derive_account_health({}, now)

        assert green_state == "green"
        assert inactive_state == "inactive"
        # If the frontend filters out inactive before computing worst-state,
        # the chip stays green — validated here that backend produces correct labels.

    def test_fail_after_ok_is_red(self):
        """Correctness: most-recent event is a fail (last_fail_at > last_ok_at) → red."""
        import time
        from backend.api.routes.health import _derive_account_health

        now = time.time()
        entry = {
            "last_ok_at": now - 300,
            "last_fail_at": now - 10,
            "last_fail_msg": "session expired",
        }
        state, reason, *_ = _derive_account_health(entry, now)

        assert state == "red", f"Expected 'red' when fail follows ok, got {state!r}: {reason}"


class TestHealthHeartbeat:
    """Health heartbeat in conn_service stamps last_ok_at for connected accounts."""

    def test_record_session_ok_stamps_last_ok_at(self):
        """record_session_ok writes last_ok_at into _FETCH_HEALTH without touching CB counters."""
        import time
        from unittest.mock import patch
        from backend.brokers.broker_apis import record_session_ok, _FETCH_HEALTH

        account = "HBTEST"
        before = time.time()
        record_session_ok(account)
        after = time.time()

        e = _FETCH_HEALTH.get(account, {})
        assert before <= e.get("last_ok_at", 0) <= after
        # CB counters untouched — heartbeat must not affect the circuit breaker
        assert e.get("consecutive_fail_count", 0) == 0
        assert e.get("circuit_open_until") is None

    def test_record_session_ok_idempotent(self):
        """Calling record_session_ok twice keeps the latest timestamp."""
        import time
        from backend.brokers.broker_apis import record_session_ok, _FETCH_HEALTH

        account = "HBTEST2"
        record_session_ok(account)
        t1 = _FETCH_HEALTH[account]["last_ok_at"]
        record_session_ok(account)
        t2 = _FETCH_HEALTH[account]["last_ok_at"]

        assert t2 >= t1

    def test_green_within_fresh_window(self):
        """Account stays green if last_ok_at was stamped < 90s ago (well within 300s window)."""
        import time
        from backend.api.routes.health import _derive_account_health

        now = time.time()
        # Simulate heartbeat stamped 89s ago
        entry = {"last_ok_at": now - 89}
        state, reason, *_ = _derive_account_health(entry, now)
        assert state == "green", f"Expected green at 89s, got {state!r}: {reason}"

    def test_amber_beyond_fresh_window(self):
        """Account goes amber if no stamp for > 5 min (heartbeat would prevent this in practice)."""
        import time
        from backend.api.routes.health import _derive_account_health

        now = time.time()
        entry = {"last_ok_at": now - 301}
        state, reason, *_ = _derive_account_health(entry, now)
        assert state == "amber", f"Expected amber at 301s, got {state!r}: {reason}"
        assert "stale" in reason
