"""Edge-case tests for the RamboQuant broker connection layer.

Targets gaps NOT covered by existing test files (test_circuit_breaker.py,
test_breaker_jitter.py, test_stale_persistence.py, test_broker_priority.py,
test_groww_exceptions.py, test_mmap_sym_registration.py, test_tick_buffer.py,
test_market_data_consistency.py, test_3tier_audit.py, test_remote_broker.py,
test_broker_capabilities.py).

Gaps covered here:

  GAP-1  TickBufferWriter overflow — upsert() returns False when all N slots
         are occupied.  test_tick_buffer.py stops at 78% load (200/256 slots);
         this file fills to 100% and asserts the (N+1)th token is dropped.

  GAP-2  circuit_last_opened_at reset on HALF-OPEN → success.  All existing
         half-open tests verify consecutive_fail_count, circuit_open_until,
         and open_cycle_count but NONE assert that circuit_last_opened_at is
         cleared.  A stale timestamp here causes the admin badge to show the
         last-opened time even after the breaker has closed.

  GAP-3  _is_circuit_open() fast-path gate for non-opt-in accounts.  The
         function returns False immediately when get_breaker_optin_cache()
         is False — this is the protection against a DH6847 failure freezing
         other accounts.  Existing tests only test opt-in accounts.

  GAP-4  record_good_ltp zero-value guard + per-symbol isolation.  LTP cache
         must silently drop ltp <= 0; it must also keep per-symbol entries
         independent so a write to SYM_A doesn't affect SYM_B.

  GAP-5  get_last_good_ltp TTL expiry.  The max_age_s parameter ages out
         entries — the scalar LTP cache (distinct from the frame LKG cache)
         has its own TTL logic not tested anywhere.

  GAP-6  _raw_cache_put / _raw_cache_get key-scope isolation.  "positions"
         put then "holdings" put must not pollute each other; partial
         invalidate must leave the untouched key live.  (test_3tier_audit.py
         covers the round-trip but not the key-scope boundary explicitly.)

  GAP-7  _record_fetch consecutive-fail counter: non-opt-in account health
         stamps updated (last_ok_at / last_fail_at) but breaker fields NOT
         added to entry.

Five quality dimensions applied throughout:
  SSOT        — tests touch the single implementation function directly
  Correctness — precise assertions on the mutated state dict
  Performance — no real broker I/O, no DB calls, no asyncio.run()
  Reuse       — shared helpers in module-level functions, not per-test
  UX          — every assert has an f-string message surfacing the actual value
"""

from __future__ import annotations

import tempfile
import os
import time as _time

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Module-level cleanup helpers
# ---------------------------------------------------------------------------

def _cleanup_ltp(symbol: str) -> None:
    """Remove a single symbol from the in-process last-good-LTP cache."""
    from backend.brokers import broker_apis
    with broker_apis._LAST_GOOD_LTP_LOCK:
        broker_apis._LAST_GOOD_LTP.pop(symbol, None)


def _cleanup_health(account: str) -> None:
    from backend.brokers import broker_apis
    broker_apis._FETCH_HEALTH.pop(account, None)


def _cleanup_optin(account: str) -> None:
    from backend.brokers import broker_apis
    broker_apis._breaker_optin_cache.pop(account, None)


# ---------------------------------------------------------------------------
# GAP-1: TickBufferWriter 100% capacity overflow
# ---------------------------------------------------------------------------

class TestTickBufferOverflow:
    """TickBufferWriter.upsert() returns False when the hash table is full.

    The existing test_tick_buffer.py tests up to 78% load (200/256 slots) but
    never asserts the overflow return value. This is the correctness guard for
    the SSOT decision: drop on full, NO wrap-around or eviction.
    """

    @pytest.fixture
    def tmp_buffer(self):
        """Temporary buffer file that is deleted after the test."""
        with tempfile.NamedTemporaryFile(delete=False, prefix="tick_overflow_") as f:
            path = f.name
        yield path
        if os.path.exists(path):
            os.remove(path)

    def test_upsert_returns_false_when_table_full(self, tmp_buffer):
        """After filling all N slots, the (N+1)th unique token returns False."""
        from backend.brokers.tick_buffer import TickBufferWriter

        max_slots = 32  # Small table so test is fast; same code path as 4096

        writer = TickBufferWriter(path=tmp_buffer, max_slots=max_slots)
        try:
            # Fill every slot.  Token 0 is the "empty" sentinel so start at 1.
            # Tokens are inserted as unique values — each occupies one slot via
            # the linear-probe map.  Because max_slots=32, tokens 1..32 fill
            # all 32 slots exactly.
            for token in range(1, max_slots + 1):
                result = writer.upsert(token, float(token) * 10.0)
                assert result is True, (
                    f"upsert(token={token}) expected True but got {result}"
                )

            # One more unique token — no slot available → must return False.
            extra_token = max_slots + 1
            overflow_result = writer.upsert(extra_token, 999.0)
            assert overflow_result is False, (
                f"upsert at 100% capacity: expected False (drop), got {overflow_result}"
            )
        finally:
            writer.close()

    def test_overflow_does_not_corrupt_existing_entries(self, tmp_buffer):
        """After an overflow drop, all previously written tokens still readable."""
        from backend.brokers.tick_buffer import TickBufferWriter, TickBufferReader

        max_slots = 16
        writer = TickBufferWriter(path=tmp_buffer, max_slots=max_slots)
        try:
            # Fill completely (tokens 1..16).
            for token in range(1, max_slots + 1):
                writer.upsert(token, float(token) * 2.0)

            # Overflow attempt — must NOT corrupt prior entries.
            writer.upsert(max_slots + 1, 9999.0)

            writer.close()
            writer = None

            # All original tokens must still be readable.
            reader = TickBufferReader(path=tmp_buffer, max_slots=max_slots)
            try:
                for token in range(1, max_slots + 1):
                    ltp = reader.get_ltp(token)
                    assert ltp == float(token) * 2.0, (
                        f"token={token}: expected {float(token) * 2.0} after overflow, "
                        f"got {ltp}"
                    )
            finally:
                reader.close()
        finally:
            if writer is not None:
                writer.close()

    def test_update_existing_token_always_succeeds_at_capacity(self, tmp_buffer):
        """Updating an EXISTING token when the table is full must return True.

        The drop-on-full rule applies only to NEW slots.  An in-place update
        of an already-registered token does not need a new slot.
        """
        from backend.brokers.tick_buffer import TickBufferWriter

        max_slots = 8
        writer = TickBufferWriter(path=tmp_buffer, max_slots=max_slots)
        try:
            # Fill to capacity with tokens 1..8.
            for token in range(1, max_slots + 1):
                writer.upsert(token, float(token))

            # Table is full — but updating token=1 must still succeed.
            result = writer.upsert(1, 999.0)
            assert result is True, (
                "updating an existing token at full capacity must return True, "
                f"got {result}"
            )
        finally:
            writer.close()


# ---------------------------------------------------------------------------
# GAP-2: circuit_last_opened_at cleared on HALF-OPEN → CLOSED transition
# ---------------------------------------------------------------------------

class TestHalfOpenLastOpenedAtReset:
    """_record_fetch(ok=True) from HALF-OPEN must clear circuit_last_opened_at.

    All existing tests check consecutive_fail_count / circuit_open_until /
    open_cycle_count on the HALF-OPEN→CLOSED transition, but NONE assert that
    circuit_last_opened_at is set back to None.  A stale timestamp causes the
    admin badge to display "circuit open at HH:MM" even after the breaker has
    closed, misleading the operator.
    """

    ACCOUNT = "DH_test_last_opened_reset"

    def setup_method(self):
        _cleanup_health(self.ACCOUNT)
        from backend.brokers.broker_apis import set_breaker_optin_cache
        set_breaker_optin_cache(self.ACCOUNT, True)

    def teardown_method(self):
        _cleanup_health(self.ACCOUNT)
        _cleanup_optin(self.ACCOUNT)

    def test_circuit_last_opened_at_is_none_after_halfopen_success(self):
        """After OPEN → HALF-OPEN → success, circuit_last_opened_at must be None."""
        from backend.brokers.broker_apis import _record_fetch, _FETCH_HEALTH

        # Open the breaker with 3 consecutive fails.
        for _ in range(3):
            _record_fetch(self.ACCOUNT, ok=False, error="test error")

        opened_ts = _FETCH_HEALTH[self.ACCOUNT].get("circuit_last_opened_at")
        assert opened_ts is not None, (
            f"circuit_last_opened_at should be set after OPEN, got {opened_ts}"
        )

        # Expire the cooloff to move to HALF-OPEN.
        _FETCH_HEALTH[self.ACCOUNT]["circuit_open_until"] = _time.time() - 1.0

        # Probe succeeds → CLOSED.
        _record_fetch(self.ACCOUNT, ok=True)

        e = _FETCH_HEALTH[self.ACCOUNT]
        assert e.get("circuit_last_opened_at") is None, (
            f"circuit_last_opened_at must be None after CLOSED; "
            f"got {e.get('circuit_last_opened_at')}"
        )

    def test_all_breaker_fields_zeroed_after_halfopen_success(self):
        """Verify every circuit-breaker field is reset on successful probe."""
        from backend.brokers.broker_apis import _record_fetch, _FETCH_HEALTH, _circuit_state

        for _ in range(3):
            _record_fetch(self.ACCOUNT, ok=False, error="test error")

        # Expire cooloff.
        _FETCH_HEALTH[self.ACCOUNT]["circuit_open_until"] = _time.time() - 1.0
        assert _circuit_state(self.ACCOUNT) == "half-open"

        _record_fetch(self.ACCOUNT, ok=True)

        e = _FETCH_HEALTH[self.ACCOUNT]
        assert e.get("consecutive_fail_count") == 0, (
            f"consecutive_fail_count should be 0, got {e.get('consecutive_fail_count')}"
        )
        assert e.get("circuit_open_until") is None, (
            f"circuit_open_until should be None, got {e.get('circuit_open_until')}"
        )
        assert e.get("circuit_last_opened_at") is None, (
            f"circuit_last_opened_at should be None, got {e.get('circuit_last_opened_at')}"
        )
        assert e.get("open_cycle_count") == 0, (
            f"open_cycle_count should be 0, got {e.get('open_cycle_count')}"
        )
        assert _circuit_state(self.ACCOUNT) == "closed", (
            f"state should be 'closed', got {_circuit_state(self.ACCOUNT)}"
        )


# ---------------------------------------------------------------------------
# GAP-3: _is_circuit_open() fast-path for non-opt-in accounts
# ---------------------------------------------------------------------------

class TestCircuitOpenNonOptIn:
    """Non-opt-in accounts must never be blocked by the circuit-breaker gate.

    DH6847 is the ONLY opt-in account in production.  A breaker-open on DH6847
    must NOT affect ZG0790, ZJ6294, DH3747, or GR87DF.  _is_circuit_open()
    provides this guarantee via the in-process opt-in cache.
    """

    ACCOUNT_NON_OPTIN = "DH_non_optin_test"
    ACCOUNT_OPTIN     = "DH_optin_test"

    def setup_method(self):
        _cleanup_health(self.ACCOUNT_NON_OPTIN)
        _cleanup_health(self.ACCOUNT_OPTIN)
        from backend.brokers.broker_apis import set_breaker_optin_cache, _breaker_optin_cache
        # Non-opt-in: either absent from cache (default False) or explicitly False.
        _breaker_optin_cache.pop(self.ACCOUNT_NON_OPTIN, None)
        set_breaker_optin_cache(self.ACCOUNT_OPTIN, True)

    def teardown_method(self):
        _cleanup_health(self.ACCOUNT_NON_OPTIN)
        _cleanup_health(self.ACCOUNT_OPTIN)
        _cleanup_optin(self.ACCOUNT_NON_OPTIN)
        _cleanup_optin(self.ACCOUNT_OPTIN)

    def test_is_circuit_open_false_for_non_optin_even_with_forced_state(self):
        """Even if _FETCH_HEALTH shows OPEN state, a non-opt-in account is
        never short-circuited — _is_circuit_open() returns False."""
        from backend.brokers import broker_apis

        # Force-inject an OPEN health entry for the non-opt-in account.
        broker_apis._FETCH_HEALTH[self.ACCOUNT_NON_OPTIN] = {
            "last_ok_at":             0.0,
            "last_fail_at":           _time.time(),
            "last_fail_msg":          "injected",
            "consecutive_fail_count": 5,
            "circuit_open_until":     _time.time() + 300.0,
            "circuit_last_opened_at": _time.time(),
            "open_cycle_count":       1,
        }

        result = broker_apis._is_circuit_open(self.ACCOUNT_NON_OPTIN)
        assert result is False, (
            "_is_circuit_open must return False for non-opt-in account even "
            f"when health state shows OPEN; got {result}"
        )

    def test_is_circuit_open_true_for_optin_account_in_open_state(self):
        """Opt-in account that has entered OPEN state returns True from
        _is_circuit_open() so the fetch is correctly short-circuited."""
        from backend.brokers import broker_apis

        # Force OPEN state for the opt-in account.
        broker_apis._FETCH_HEALTH[self.ACCOUNT_OPTIN] = {
            "last_ok_at":             0.0,
            "last_fail_at":           _time.time(),
            "last_fail_msg":          "injected",
            "consecutive_fail_count": 3,
            "circuit_open_until":     _time.time() + 300.0,
            "circuit_last_opened_at": _time.time(),
            "open_cycle_count":       1,
        }

        result = broker_apis._is_circuit_open(self.ACCOUNT_OPTIN)
        assert result is True, (
            f"_is_circuit_open must return True for opt-in account in OPEN state; "
            f"got {result}"
        )

    def test_non_optin_account_still_gets_health_stamps(self):
        """Non-opt-in accounts still receive last_ok_at / last_fail_at stamps
        (for the admin badge) even though the breaker logic is bypassed."""
        from backend.brokers.broker_apis import _record_fetch, _FETCH_HEALTH

        _record_fetch(self.ACCOUNT_NON_OPTIN, ok=True)
        e = _FETCH_HEALTH.get(self.ACCOUNT_NON_OPTIN)
        assert e is not None, "health entry must be created even for non-opt-in"
        assert e["last_ok_at"] > 0.0, (
            f"last_ok_at should be a recent timestamp, got {e['last_ok_at']}"
        )

        _record_fetch(self.ACCOUNT_NON_OPTIN, ok=False, error="connection refused")
        e2 = _FETCH_HEALTH[self.ACCOUNT_NON_OPTIN]
        assert e2["last_fail_at"] > 0.0, (
            f"last_fail_at should be stamped, got {e2['last_fail_at']}"
        )
        assert e2.get("last_fail_msg") == "connection refused", (
            f"last_fail_msg mismatch: {e2.get('last_fail_msg')}"
        )
        # But no breaker state should be injected.
        assert e2.get("consecutive_fail_count", 0) == 0, (
            "non-opt-in account must NOT have consecutive_fail_count incremented, "
            f"got {e2.get('consecutive_fail_count')}"
        )


# ---------------------------------------------------------------------------
# GAP-4: record_good_ltp zero-value guard + per-symbol isolation
# ---------------------------------------------------------------------------

class TestRecordGoodLtpGuard:
    """record_good_ltp() must silently discard zero / negative LTPs and must
    keep per-symbol entries independent."""

    SYM_A = "NIFTY26JUL22000CE"
    SYM_B = "NIFTY26JUL22000PE"

    def setup_method(self):
        _cleanup_ltp(self.SYM_A)
        _cleanup_ltp(self.SYM_B)

    def teardown_method(self):
        _cleanup_ltp(self.SYM_A)
        _cleanup_ltp(self.SYM_B)

    def test_zero_ltp_not_recorded(self):
        """record_good_ltp with ltp=0 must NOT overwrite a prior good value."""
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        # First: record a good value.
        record_good_ltp(self.SYM_A, 105.0)
        assert get_last_good_ltp(self.SYM_A) == pytest.approx(105.0), (
            f"Expected 105.0 after initial record, got {get_last_good_ltp(self.SYM_A)}"
        )

        # Second: attempt to record ltp=0 — must be silently dropped.
        record_good_ltp(self.SYM_A, 0.0)
        result = get_last_good_ltp(self.SYM_A)
        assert result == pytest.approx(105.0), (
            f"Zero-LTP must not overwrite prior good value; got {result}"
        )

    def test_negative_ltp_not_recorded(self):
        """record_good_ltp with ltp < 0 must be silently dropped."""
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        record_good_ltp(self.SYM_A, 200.0)
        record_good_ltp(self.SYM_A, -50.0)
        result = get_last_good_ltp(self.SYM_A)
        assert result == pytest.approx(200.0), (
            f"Negative LTP must not overwrite prior good value; got {result}"
        )

    def test_per_symbol_isolation(self):
        """Writing SYM_A must not affect SYM_B's cache entry."""
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        record_good_ltp(self.SYM_A, 111.0)
        record_good_ltp(self.SYM_B, 222.0)

        assert get_last_good_ltp(self.SYM_A) == pytest.approx(111.0), (
            f"SYM_A expected 111.0, got {get_last_good_ltp(self.SYM_A)}"
        )
        assert get_last_good_ltp(self.SYM_B) == pytest.approx(222.0), (
            f"SYM_B expected 222.0, got {get_last_good_ltp(self.SYM_B)}"
        )

        # Overwrite SYM_A — SYM_B must be unchanged.
        record_good_ltp(self.SYM_A, 333.0)
        assert get_last_good_ltp(self.SYM_B) == pytest.approx(222.0), (
            f"SYM_B must be unchanged after SYM_A overwrite; got {get_last_good_ltp(self.SYM_B)}"
        )

    def test_unknown_symbol_returns_none(self):
        """get_last_good_ltp for an unrecorded symbol returns None."""
        from backend.brokers.broker_apis import get_last_good_ltp
        result = get_last_good_ltp("NEVER_RECORDED_SYM_XYZ")
        assert result is None, (
            f"Unrecorded symbol must return None, got {result}"
        )


# ---------------------------------------------------------------------------
# GAP-5: get_last_good_ltp TTL expiry via max_age_s parameter
# ---------------------------------------------------------------------------

class TestLastGoodLtpTtl:
    """get_last_good_ltp respects the max_age_s parameter.

    The scalar LTP cache has its own TTL (default 1h) independent from the
    per-account frame LKG TTL (24h).  The max_age_s kwarg is the per-call
    override tested here; it avoids time-mocking by using a tiny age.
    """

    SYM = "TTL_TEST_SYM"

    def setup_method(self):
        _cleanup_ltp(self.SYM)

    def teardown_method(self):
        _cleanup_ltp(self.SYM)

    def test_entry_within_ttl_is_returned(self):
        """Entry recorded just now must be returned with a 1-hour max_age_s."""
        from backend.brokers.broker_apis import record_good_ltp, get_last_good_ltp

        record_good_ltp(self.SYM, 500.0)
        result = get_last_good_ltp(self.SYM, max_age_s=3600.0)
        assert result == pytest.approx(500.0), (
            f"Entry within TTL should be returned; got {result}"
        )

    def test_entry_older_than_max_age_s_returns_none(self):
        """Entry recorded with a past timestamp returns None when max_age_s
        is shorter than the elapsed time.

        We bypass real time-passing by directly inserting an entry with a
        timestamp in the past, then verifying it is treated as expired.
        """
        from backend.brokers import broker_apis

        past_ts = _time.time() - 120.0  # 2 minutes ago
        with broker_apis._LAST_GOOD_LTP_LOCK:
            broker_apis._LAST_GOOD_LTP[self.SYM] = (past_ts, 500.0)

        # max_age_s=60 — entry is 120s old → expired.
        result = broker_apis.get_last_good_ltp(self.SYM, max_age_s=60.0)
        assert result is None, (
            f"Entry 120s old with max_age_s=60 should return None; got {result}"
        )

    def test_fresh_write_resets_expiry(self):
        """A fresh write after expiry makes the entry live again.

        Inject an old timestamp (expired), verify it returns None, then
        record a fresh value and confirm it is returned.
        """
        from backend.brokers import broker_apis

        # Insert an expired entry (3-minute-old timestamp, 60s TTL → expired).
        old_ts = _time.time() - 180.0
        with broker_apis._LAST_GOOD_LTP_LOCK:
            broker_apis._LAST_GOOD_LTP[self.SYM] = (old_ts, 400.0)

        assert broker_apis.get_last_good_ltp(self.SYM, max_age_s=60.0) is None, (
            "Old entry should be expired and return None"
        )

        # Fresh write — timestamp is now().
        broker_apis.record_good_ltp(self.SYM, 401.0)

        result = broker_apis.get_last_good_ltp(self.SYM, max_age_s=3600.0)
        assert result == pytest.approx(401.0), (
            f"Fresh write should be visible within TTL; got {result}"
        )


# ---------------------------------------------------------------------------
# GAP-6: _raw_cache key-scope isolation (partial invalidate vs full clear)
# ---------------------------------------------------------------------------

class TestRawCacheKeyScope:
    """Each of "positions", "holdings", "margins" is an independent cache key.

    test_3tier_audit.py covers the put/get/invalidate round-trip for one key;
    this class asserts that partial invalidate leaves the sibling keys live
    and that a new put to one key does not affect another.
    """

    def setup_method(self):
        from backend.brokers.broker_apis import _raw_cache_invalidate
        _raw_cache_invalidate()  # clean slate

    def teardown_method(self):
        from backend.brokers.broker_apis import _raw_cache_invalidate
        _raw_cache_invalidate()

    def test_sibling_keys_independent_on_put(self):
        """Putting "positions" must not make "holdings" appear as cached."""
        from backend.brokers.broker_apis import _raw_cache_put, _raw_cache_get

        df = pd.DataFrame({"account": ["T"], "pnl": [0.0]})
        _raw_cache_put("positions", [df])

        assert _raw_cache_get("positions") is not None, (
            "positions should be cached after put"
        )
        assert _raw_cache_get("holdings") is None, (
            "holdings must not be cached when only positions was put"
        )
        assert _raw_cache_get("margins") is None, (
            "margins must not be cached when only positions was put"
        )

    def test_partial_invalidate_leaves_siblings_live(self):
        """Invalidating one key must leave the other two keys accessible."""
        from backend.brokers.broker_apis import _raw_cache_put, _raw_cache_get, _raw_cache_invalidate

        df = pd.DataFrame({"account": ["T"], "pnl": [0.0]})
        for key in ("positions", "holdings", "margins"):
            _raw_cache_put(key, [df])

        _raw_cache_invalidate("positions")

        assert _raw_cache_get("positions") is None, (
            "positions should be invalidated"
        )
        assert _raw_cache_get("holdings") is not None, (
            "holdings must remain cached after positions-only invalidate"
        )
        assert _raw_cache_get("margins") is not None, (
            "margins must remain cached after positions-only invalidate"
        )

    def test_full_invalidate_clears_all_three_keys(self):
        """_raw_cache_invalidate() with no argument clears every key."""
        from backend.brokers.broker_apis import _raw_cache_put, _raw_cache_get, _raw_cache_invalidate

        df = pd.DataFrame({"account": ["T"], "pnl": [0.0]})
        for key in ("positions", "holdings", "margins"):
            _raw_cache_put(key, [df])

        _raw_cache_invalidate()

        for key in ("positions", "holdings", "margins"):
            assert _raw_cache_get(key) is None, (
                f"{key} should be None after full invalidate, got {_raw_cache_get(key)}"
            )


# ---------------------------------------------------------------------------
# GAP-7: _record_fetch non-opt-in: health stamps only, no breaker fields
# ---------------------------------------------------------------------------

class TestRecordFetchNonOptInHealthStamps:
    """Non-opt-in accounts get last_ok_at / last_fail_at updated by
    _record_fetch but must NOT have the circuit-breaker counter fields
    incremented (consecutive_fail_count stays 0 — the fast path in
    _record_fetch skips the breaker state machine entirely).

    This is the protection invariant: no non-opt-in account should ever
    accrue a consecutive_fail_count high enough to accidentally enter OPEN
    state if the opt-in check is ever relaxed in future.
    """

    ACCOUNT = "ZG_non_optin_health_test"

    def setup_method(self):
        _cleanup_health(self.ACCOUNT)
        _cleanup_optin(self.ACCOUNT)
        # Explicitly absent from cache → default False (non-opt-in).

    def teardown_method(self):
        _cleanup_health(self.ACCOUNT)
        _cleanup_optin(self.ACCOUNT)

    def test_consecutive_fail_count_stays_zero_for_non_optin(self):
        """Repeated failures on a non-opt-in account must NOT increment
        consecutive_fail_count.  The breaker loop is never entered."""
        from backend.brokers.broker_apis import _record_fetch, _FETCH_HEALTH

        for _ in range(10):
            _record_fetch(self.ACCOUNT, ok=False, error="network error")

        e = _FETCH_HEALTH.get(self.ACCOUNT, {})
        count = e.get("consecutive_fail_count", 0)
        assert count == 0, (
            f"Non-opt-in account consecutive_fail_count must stay 0, got {count}"
        )

    def test_circuit_open_until_never_set_for_non_optin(self):
        """10 consecutive failures on a non-opt-in account must never set
        circuit_open_until — the account is never put in OPEN state."""
        from backend.brokers.broker_apis import _record_fetch, _FETCH_HEALTH, _is_circuit_open

        for _ in range(10):
            _record_fetch(self.ACCOUNT, ok=False, error="network error")

        e = _FETCH_HEALTH.get(self.ACCOUNT, {})
        assert e.get("circuit_open_until") is None, (
            "circuit_open_until must never be set for non-opt-in account, "
            f"got {e.get('circuit_open_until')}"
        )
        # Double-check via the public predicate.
        assert _is_circuit_open(self.ACCOUNT) is False, (
            "_is_circuit_open must be False for non-opt-in account"
        )

    def test_last_fail_msg_truncated_to_200_chars(self):
        """last_fail_msg is truncated to 200 characters for non-opt-in accounts
        (same guard as opt-in accounts — the truncation is in the fast path)."""
        from backend.brokers.broker_apis import _record_fetch, _FETCH_HEALTH

        long_error = "E" * 500
        _record_fetch(self.ACCOUNT, ok=False, error=long_error)

        msg = _FETCH_HEALTH.get(self.ACCOUNT, {}).get("last_fail_msg", "")
        assert len(msg) <= 200, (
            f"last_fail_msg should be at most 200 chars, got {len(msg)}"
        )
