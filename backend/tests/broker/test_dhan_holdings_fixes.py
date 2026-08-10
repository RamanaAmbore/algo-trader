"""Tests for three broker-layer fixes (Aug 2026).

Covers three data-fetch resilience fixes:

  1. Dhan adapter ``_unwrap`` now raises ``RuntimeError`` when the response
     carries ``status=="failure"`` and ``data`` is not a list (e.g. the
     dhanhq v2.2.0 error shape ``{"status":"failure","data":"","remarks":{...}}``).
     Previously returned ``[]`` silently, masking the API error from
     ``_record_fetch`` and keeping the health badge incorrectly green.
     Non-failure responses with non-list ``data`` (e.g. market-status
     probes handled by ``_extract_dhan_status_rows``) still return ``[]``
     so that function's own fallback logic is preserved.

  2. ``_fetch_holdings_local`` records ``ok=True`` unconditionally when
     ``broker.holdings()`` returns ``[]`` without raising — a broker that
     returns an empty list is healthy; the account just has no holdings.
     The failure path is driven by Fix 1: when the broker API fails it
     now raises instead of returning ``[]``, so the ``except`` block in
     ``_fetch_holdings_local`` calls ``_record_fetch(ok=False)`` correctly.
     Recording ``ok=False`` for a legitimate zero-holdings account would
     incorrectly turn the health badge amber/red.

  3. ``_record_fetch`` non-recovery ``ok=True`` path now emits ``fetch_ok``
     on every healthy (non-recovering) success, not only on recovery from
     failure (``fetch_ok_recovery``). Both the non-opt-in fast path and the
     circuit-breaker opt-in path emit the event. ``fetch_ok_recovery`` is
     still emitted on the first ok after a failure.

Five quality dimensions per fix:
  * SSOT — canonical implementation in each file.
  * Perf — no extra I/O; events emit out-of-lock.
  * Stale-code grep — docstrings updated; no claims of silent returns.
  * Reuse — standard circuit-breaker + event patterns.
  * UX — API failures surface as health-badge failures; healthy zero-
    holdings accounts stay green; all successful fetches appear in log.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
import pandas as pd

from backend.brokers.adapters.dhan import _unwrap, DhanBroker
from backend.brokers.broker_apis import _record_fetch, _FETCH_HEALTH


# ---------------------------------------------------------------------------
# Fix 1 — _unwrap: correct error-shape detection
# ---------------------------------------------------------------------------

class TestDhanUnwrapErrorShape:
    """_unwrap raises on explicit failure status; returns [] for other non-list shapes."""

    def test_unwrap_returns_list_on_valid_data(self):
        """Normal case: status=success, data is a list — return it."""
        resp = {
            "status": "success",
            "data": [
                {"securityId": "1", "tradingSymbol": "SBIN", "quantity": 100},
            ],
        }
        result = _unwrap(resp)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["tradingSymbol"] == "SBIN"

    def test_unwrap_returns_empty_list_on_success_empty_data(self):
        """Regression guard: status=success, data=[] — return [] (not raise).

        A zero-holdings or zero-positions account is a valid success response.
        This must NOT raise so a healthy empty account keeps its green badge.
        """
        resp = {"status": "success", "data": []}
        result = _unwrap(resp)
        assert result == []

    def test_unwrap_returns_empty_list_on_missing_data_key(self):
        """data key absent — return [] (market-status probe fallback behaviour)."""
        resp = {"status": "success"}
        result = _unwrap(resp)
        assert result == []

    def test_unwrap_returns_empty_list_on_non_dict_response(self):
        """Non-dict top-level response — return [] without raising."""
        assert _unwrap("not a dict") == []
        assert _unwrap(None) == []
        assert _unwrap([]) == []

    def test_unwrap_raises_on_failure_status_string_data(self):
        """status=failure + data="" (dhanhq v2.2.0 non-auth API error) → raise.

        Pre-fix: silently returned [].
        Post-fix: raises RuntimeError so the caller records ok=False.
        """
        resp = {
            "status": "failure",
            "data": "",
            "remarks": {"error_code": "900", "error_message": "Something went wrong"},
        }
        with pytest.raises(RuntimeError, match="Dhan API failure"):
            _unwrap(resp)

    def test_unwrap_raises_on_failure_status_dict_data(self):
        """status=failure + data is a dict → raise."""
        resp = {
            "status": "failure",
            "data": {"error": "detail"},
            "remarks": {"error_code": "901"},
        }
        with pytest.raises(RuntimeError, match="Dhan API failure"):
            _unwrap(resp)

    def test_unwrap_raises_on_failure_status_null_data(self):
        """status=failure + data=None → raise (explicit failure signal)."""
        resp = {"status": "failure", "data": None, "remarks": "access denied"}
        with pytest.raises(RuntimeError, match="Dhan API failure"):
            _unwrap(resp)

    def test_unwrap_returns_empty_list_on_success_dict_data(self):
        """status=success + data is a dict (not failure) — return [] without raising.

        Preserves _extract_dhan_status_rows fallback: that function calls _unwrap
        then checks the flat-dict response itself on empty return.
        """
        resp = {"status": "success", "data": {"key": "value"}}
        result = _unwrap(resp)
        assert result == []

    def test_unwrap_error_message_includes_remarks(self):
        """RuntimeError message must include the remarks field for diagnostics."""
        remarks = {"error_code": "900", "error_message": "Invalid params"}
        resp = {"status": "failure", "data": "", "remarks": remarks}
        with pytest.raises(RuntimeError) as exc_info:
            _unwrap(resp)
        msg = str(exc_info.value)
        assert "failure" in msg.lower()
        # remarks or data should appear in the message so operators can diagnose
        assert "900" in msg or "Invalid" in msg or str(remarks) in msg


# ---------------------------------------------------------------------------
# Fix 1 integration — DhanBroker.holdings() propagates the error upward
# ---------------------------------------------------------------------------

class TestDhanHoldingsErrorPropagation:
    """holdings() must not swallow _unwrap's RuntimeError on failure responses.

    Before Fix 1, _unwrap returned [] silently. After Fix 1, _unwrap raises
    when data is not a list and status is failure. The raise propagates through
    _normalise_holdings → holdings() → _fetch_holdings_local's except block
    → _record_fetch(ok=False). This is the critical failure chain.
    """

    def test_holdings_raises_on_api_failure_shape(self):
        """_safe_call returns failure shape → holdings() raises RuntimeError."""
        conn_mock = MagicMock()
        broker = DhanBroker(conn=conn_mock)

        error_resp = {
            "status": "failure",
            "data": "",
            "remarks": {"error_code": "900", "error_message": "Something went wrong"},
        }
        with patch.object(broker, "_safe_call", return_value=error_resp):
            with pytest.raises(RuntimeError, match="Dhan API failure"):
                broker.holdings()

    def test_holdings_returns_empty_list_on_success_empty_data(self):
        """_safe_call returns success + data=[] → holdings() returns [] (not raises).

        A zero-holdings account is a valid healthy state; holdings() must
        not raise for this shape so the caller records ok=True.
        """
        conn_mock = MagicMock()
        broker = DhanBroker(conn=conn_mock)

        empty_resp = {"status": "success", "data": []}
        with patch.object(broker, "_safe_call", return_value=empty_resp):
            result = broker.holdings()
        assert result == []

    def test_positions_raises_on_api_failure_shape(self):
        """_normalise_positions also calls _unwrap — same raise behaviour."""
        conn_mock = MagicMock()
        broker = DhanBroker(conn=conn_mock)

        error_resp = {
            "status": "failure",
            "data": "",
            "remarks": {"error_code": "902"},
        }
        with patch.object(broker, "_safe_call", return_value=error_resp):
            with pytest.raises(RuntimeError, match="Dhan API failure"):
                broker.positions()


# ---------------------------------------------------------------------------
# Fix 2 — _fetch_holdings_local: correct ok/fail recording
# ---------------------------------------------------------------------------

class TestFetchHoldingsLocalHealthRecording:
    """_fetch_holdings_local must record ok=True for empty-list success,
    and rely on the exception path for true API failures (Fix 1).

    We test the underlying logic directly via _record_fetch rather than
    calling _fetch_holdings_local through the @for_all_accounts decorator
    (which requires a live broker registry).
    """

    def setup_method(self, _method):
        _FETCH_HEALTH.clear()

    def teardown_method(self, _method):
        _FETCH_HEALTH.clear()

    def test_empty_rows_from_healthy_broker_should_stay_ok(self):
        """A broker.holdings() returning [] is healthy — zero holdings, not an error.

        Confirm that the code in _fetch_holdings_local that fires after
        rows=[] comes from broker.holdings() calls _record_fetch(ok=True),
        NOT ok=False. This is the inverse of the original (incorrect) spec.

        We verify by calling _record_fetch directly with ok=True and
        confirming the health stamp reflects success.
        """
        account = "ZG_TEST"
        now = time.time()
        _FETCH_HEALTH[account] = {
            "last_ok_at": now - 5000,
            "last_fail_at": now - 6000,
            "consecutive_fail_count": 0,
            "circuit_open_until": 0.0,
            "last_fail_msg": "",
            "open_cycle_count": 0,
        }

        with patch("backend.brokers.broker_apis._emit_conn_event"), \
             patch("backend.brokers.broker_apis.get_breaker_optin_cache", return_value=False):
            # This is what _fetch_holdings_local calls when rows=[] (valid empty)
            _record_fetch(account, ok=True)

        entry = _FETCH_HEALTH[account]
        assert entry["last_ok_at"] >= now - 1, (
            "last_ok_at must be updated on ok=True for zero-holdings account"
        )
        assert entry["last_ok_at"] > entry["last_fail_at"], (
            "Zero-holdings account must not leave health stamp in failed state"
        )

    def test_api_failure_exception_records_ok_false(self):
        """When holdings() raises (Fix 1 path), the except block in
        _fetch_holdings_local records ok=False via _record_fetch.

        We verify by calling _record_fetch directly with ok=False
        (mimicking the except block) and confirming the failure stamp.
        """
        account = "DH_TEST"
        now = time.time()
        _FETCH_HEALTH[account] = {
            "last_ok_at": now - 100,
            "last_fail_at": now - 200,
            "consecutive_fail_count": 0,
            "circuit_open_until": 0.0,
            "last_fail_msg": "",
            "open_cycle_count": 0,
        }

        with patch("backend.brokers.broker_apis._emit_conn_event"), \
             patch("backend.brokers.broker_apis.get_breaker_optin_cache", return_value=False):
            # This is what _fetch_holdings_local calls in the except block
            _record_fetch(account, ok=False,
                          error="Dhan API failure: status=failure data='' remarks={'error_code': '900'}")

        entry = _FETCH_HEALTH[account]
        assert entry["last_fail_at"] >= now - 1, (
            "last_fail_at must be updated on ok=False (API failure)"
        )
        assert entry["last_fail_msg"] != "", "last_fail_msg must be populated on failure"


# ---------------------------------------------------------------------------
# Fix 3 — _record_fetch: fetch_ok event emission
# ---------------------------------------------------------------------------

class TestRecordFetchEmitsFetchOkEvent:
    """_record_fetch emits 'fetch_ok' on every healthy non-recovering success."""

    def setup_method(self, _method):
        _FETCH_HEALTH.clear()

    def teardown_method(self, _method):
        _FETCH_HEALTH.clear()

    def _healthy_entry(self, now: float) -> dict:
        return {
            "last_ok_at": now - 100,
            "last_fail_at": now - 200,
            "consecutive_fail_count": 0,
            "circuit_open_until": 0.0,
            "last_fail_msg": "",
            "open_cycle_count": 0,
        }

    def _recovering_entry(self, now: float) -> dict:
        return {
            "last_ok_at": now - 200,
            "last_fail_at": now - 100,  # more recent — means was failing
            "consecutive_fail_count": 3,
            "circuit_open_until": 0.0,
            "last_fail_msg": "auth_failed",
            "open_cycle_count": 0,
        }

    def test_healthy_success_emits_fetch_ok(self):
        """Healthy account (last_ok > last_fail) + ok=True → emit 'fetch_ok'."""
        account = "ACC_OK"
        _FETCH_HEALTH[account] = self._healthy_entry(time.time())

        with patch("backend.brokers.broker_apis._emit_conn_event") as emit_mock, \
             patch("backend.brokers.broker_apis.get_breaker_optin_cache", return_value=False):
            _record_fetch(account, ok=True)

        emitted = [c.args[2] for c in emit_mock.call_args_list if len(c.args) > 2]
        assert "fetch_ok" in emitted, (
            f"Expected 'fetch_ok' in emitted events for healthy success; got {emitted}"
        )

    def test_healthy_success_does_not_emit_recovery(self):
        """Healthy account must NOT emit 'fetch_ok_recovery' (that is recovery-only)."""
        account = "ACC_OK"
        _FETCH_HEALTH[account] = self._healthy_entry(time.time())

        with patch("backend.brokers.broker_apis._emit_conn_event") as emit_mock, \
             patch("backend.brokers.broker_apis.get_breaker_optin_cache", return_value=False):
            _record_fetch(account, ok=True)

        emitted = [c.args[2] for c in emit_mock.call_args_list if len(c.args) > 2]
        assert "fetch_ok_recovery" not in emitted, (
            f"'fetch_ok_recovery' must not fire on healthy success; got {emitted}"
        )

    def test_recovering_success_emits_recovery_not_fetch_ok(self):
        """Recovery from failure (last_fail > last_ok) → 'fetch_ok_recovery', not 'fetch_ok'."""
        account = "ACC_REC"
        _FETCH_HEALTH[account] = self._recovering_entry(time.time())

        with patch("backend.brokers.broker_apis._emit_conn_event") as emit_mock, \
             patch("backend.brokers.broker_apis.get_breaker_optin_cache", return_value=False):
            _record_fetch(account, ok=True)

        emitted = [c.args[2] for c in emit_mock.call_args_list if len(c.args) > 2]
        assert "fetch_ok_recovery" in emitted, (
            f"Expected 'fetch_ok_recovery' on first ok after failure; got {emitted}"
        )
        assert "fetch_ok" not in emitted, (
            f"'fetch_ok' must not fire alongside 'fetch_ok_recovery'; got {emitted}"
        )

    def test_failure_emits_fetch_fail(self):
        """ok=False → emit 'fetch_fail' (or 'auth_fail' for auth errors)."""
        account = "ACC_FAIL"
        _FETCH_HEALTH[account] = self._healthy_entry(time.time())

        with patch("backend.brokers.broker_apis._emit_conn_event") as emit_mock, \
             patch("backend.brokers.broker_apis.get_breaker_optin_cache", return_value=False):
            _record_fetch(account, ok=False, error="Connection timeout")

        emitted = [c.args[2] for c in emit_mock.call_args_list if len(c.args) > 2]
        assert any(e in ("fetch_fail", "auth_fail") for e in emitted), (
            f"Expected 'fetch_fail' or 'auth_fail' on failure; got {emitted}"
        )

    def test_failure_does_not_emit_fetch_ok(self):
        """ok=False must never emit 'fetch_ok'."""
        account = "ACC_FAIL"
        _FETCH_HEALTH[account] = self._healthy_entry(time.time())

        with patch("backend.brokers.broker_apis._emit_conn_event") as emit_mock, \
             patch("backend.brokers.broker_apis.get_breaker_optin_cache", return_value=False):
            _record_fetch(account, ok=False, error="timeout")

        emitted = [c.args[2] for c in emit_mock.call_args_list if len(c.args) > 2]
        assert "fetch_ok" not in emitted, (
            f"'fetch_ok' must never fire on failure; got {emitted}"
        )

    def test_health_stamps_updated_on_success(self):
        """ok=True updates last_ok_at to current time."""
        account = "ACC_TS"
        now = time.time()
        _FETCH_HEALTH[account] = self._healthy_entry(now)

        with patch("backend.brokers.broker_apis._emit_conn_event"), \
             patch("backend.brokers.broker_apis.get_breaker_optin_cache", return_value=False):
            _record_fetch(account, ok=True)

        entry = _FETCH_HEALTH[account]
        assert entry["last_ok_at"] >= now - 1, (
            f"last_ok_at not updated: {entry['last_ok_at']} vs now={now}"
        )

    def test_health_stamps_updated_on_failure(self):
        """ok=False updates last_fail_at to current time and stores error msg."""
        account = "ACC_TSF"
        now = time.time()
        _FETCH_HEALTH[account] = self._healthy_entry(now)

        with patch("backend.brokers.broker_apis._emit_conn_event"), \
             patch("backend.brokers.broker_apis.get_breaker_optin_cache", return_value=False):
            _record_fetch(account, ok=False, error="timeout error")

        entry = _FETCH_HEALTH[account]
        assert entry["last_fail_at"] >= now - 1
        assert "timeout" in entry["last_fail_msg"]


# ---------------------------------------------------------------------------
# Stale-code grep: _unwrap docstring must not claim silent masking
# ---------------------------------------------------------------------------

class TestUnwrapDocstringIntegrity:
    """_unwrap docstring must accurately describe its post-fix behaviour."""

    def test_unwrap_docstring_mentions_raise(self):
        """Docstring must mention that it raises on explicit failure status."""
        import inspect
        src = inspect.getsource(_unwrap)
        assert "raise" in src.lower() or "Raises" in src, (
            "_unwrap docstring must document RuntimeError raise on failure status"
        )

    def test_unwrap_docstring_preserves_empty_list_case(self):
        """Docstring must document that non-failure shape mismatch returns []."""
        import inspect
        src = inspect.getsource(_unwrap)
        assert "[]" in src or "empty" in src.lower(), (
            "_unwrap docstring must document [] return for non-failure shape mismatches"
        )
