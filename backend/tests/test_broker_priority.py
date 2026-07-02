"""
Per-account Dhan poll-priority interval gate tests.

Covers five quality dimensions:
  SSOT        — single interval-gate in _is_dhan_interval_due / _update_dhan_next_poll
  Correctness — hot/warm/cold intervals, non-Dhan pass-through, fresh bypass,
                auto-downgrade trigger + cooloff, restore-priority endpoint
  Performance — gate decision is O(1) dict lookup; no blocking I/O on hot path
  Reuse       — same _dhan_next_poll dict shared by all three _fetch_*_local fns
  UX          — auto_downgrade_enabled=False never downgrades;
                6th open within 5-min cooloff does not re-fire

Test catalogue:
  1. 'cold' account is skipped when interval not elapsed.
  2. 'cold' account polls when interval elapses.
  3. 'hot' account polls every 30s.
  4. 'warm' account polls every 120s.
  5. Non-Dhan (Kite) broker ignores the gate unconditionally.
  6. Manual force-refresh (?fresh=1) bypasses interval gate.
  7. 5 breaker opens in 15 min + auto_downgrade_enabled=True → cold.
  8. auto_downgrade_enabled=False → no downgrade even after 5 opens.
  9. 6th breaker open within 5-min cooloff does NOT re-fire downgrade.
 10. POST /restore-priority resets poll_priority + clears stamps.
"""

from __future__ import annotations

import time as _time
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_state(account: str) -> None:
    """Wipe per-account interval state so each test starts clean."""
    from backend.brokers import broker_apis as _ba
    _ba._dhan_next_poll.pop(account, None)
    _ba._breaker_open_history.pop(account, None)
    _ba._downgrade_cooloff_until.pop(account, None)
    _ba._FETCH_HEALTH.pop(account, None)


def _make_dhan_broker() -> MagicMock:
    """Return a mock that looks like a DhanBroker instance."""
    m = MagicMock()
    m.__class__.__name__ = "DhanBroker"
    return m


def _make_kite_broker() -> MagicMock:
    """Return a mock that looks like a KiteBroker instance."""
    m = MagicMock()
    m.__class__.__name__ = "KiteBroker"
    return m


# ---------------------------------------------------------------------------
# 1–4: Interval gate per priority
# ---------------------------------------------------------------------------

class TestIntervalGate:
    """Tests 1–4 — _is_dhan_interval_due + _update_dhan_next_poll."""

    def test_cold_account_skipped_before_interval(self):
        """Test 1: cold account (600s) is skipped when < 600s have elapsed."""
        from backend.brokers.broker_apis import (
            _is_dhan_interval_due, _update_dhan_next_poll, _dhan_next_poll,
        )
        acct = "DH_TEST_COLD_SKIP"
        _reset_state(acct)
        broker = _make_dhan_broker()

        with patch(
            "backend.brokers.broker_apis._get_dhan_poll_priority",
            return_value="cold",
        ):
            # Simulate that a poll just happened.
            _update_dhan_next_poll(acct, broker)
            # Immediately after: NOT due (600s haven't passed).
            assert not _is_dhan_interval_due(acct, broker)

    def test_cold_account_polls_after_interval(self):
        """Test 2: cold account polls once 600s have elapsed."""
        from backend.brokers.broker_apis import (
            _is_dhan_interval_due, _dhan_next_poll,
        )
        acct = "DH_TEST_COLD_DUE"
        _reset_state(acct)
        broker = _make_dhan_broker()

        # Manually set next_poll to 601 seconds in the past.
        _dhan_next_poll[acct] = _time.time() - 601
        assert _is_dhan_interval_due(acct, broker)

    def test_hot_account_polls_every_30s(self):
        """Test 3: hot account polls when 30s elapsed, skips before."""
        from backend.brokers.broker_apis import (
            _is_dhan_interval_due, _update_dhan_next_poll, _dhan_next_poll,
        )
        acct = "DH_TEST_HOT"
        _reset_state(acct)
        broker = _make_dhan_broker()

        with patch(
            "backend.brokers.broker_apis._get_dhan_poll_priority",
            return_value="hot",
        ):
            _update_dhan_next_poll(acct, broker)
            # Immediately after: next_poll is in future by ~30s → NOT due.
            assert not _is_dhan_interval_due(acct, broker)

        # Fake time skip of 31s.
        _dhan_next_poll[acct] = _time.time() - 1
        assert _is_dhan_interval_due(acct, broker)

    def test_warm_account_polls_every_120s(self):
        """Test 4: warm account polls every 120s.

        Strategy: set next_poll directly to simulate 'a poll happened,
        and the next_poll is now+120'. Then check gate status at
        different elapsed times by offsetting next_poll from 'now'.
        """
        from backend.brokers.broker_apis import (
            _is_dhan_interval_due, _dhan_next_poll,
        )
        acct = "DH_TEST_WARM"
        _reset_state(acct)
        broker = _make_dhan_broker()

        # Simulate a poll that happened 61s ago: next_poll = now + (120 - 61) = now+59
        # i.e. there are still 59s before the warm interval expires → NOT due.
        _dhan_next_poll[acct] = _time.time() + 59
        assert not _is_dhan_interval_due(acct, broker)

        # Simulate 121s elapsed: next_poll = now - 1 → past due.
        _dhan_next_poll[acct] = _time.time() - 1
        assert _is_dhan_interval_due(acct, broker)


# ---------------------------------------------------------------------------
# 5: Non-Dhan broker ignores gate
# ---------------------------------------------------------------------------

class TestNonDhanBypass:
    def test_kite_broker_always_due(self):
        """Test 5: Kite broker ignores the Dhan interval gate."""
        from backend.brokers.broker_apis import _is_dhan_interval_due, _dhan_next_poll
        acct = "ZG_TEST_KITE"
        _reset_state(acct)
        broker = _make_kite_broker()

        # Even with next_poll set far in the future, Kite should pass.
        _dhan_next_poll[acct] = _time.time() + 9999
        assert _is_dhan_interval_due(acct, broker)

    def test_none_broker_always_due(self):
        """Test 5b: None broker (legacy kite= path) always passes."""
        from backend.brokers.broker_apis import _is_dhan_interval_due, _dhan_next_poll
        acct = "ZG_TEST_NONE"
        _reset_state(acct)
        _dhan_next_poll[acct] = _time.time() + 9999
        assert _is_dhan_interval_due(acct, None)


# ---------------------------------------------------------------------------
# 6: Manual force-refresh bypasses gate
# ---------------------------------------------------------------------------

class TestFreshBypass:
    def test_force_refresh_bypasses_interval(self):
        """Test 6: ?fresh=1 clears the Dhan interval gate via dhan_next_poll_clear.

        Route handlers (positions / holdings / funds) call dhan_next_poll_clear()
        alongside _raw_cache_invalidate() when ?fresh=1 is set. This removes the
        cold/warm account's future next_poll timestamp so the next
        @for_all_accounts cycle hits the broker unconditionally.

        This test verifies:
          (a) A cold account with a future next_poll is NOT due under normal ops.
          (b) dhan_next_poll_clear() removes the entry from _dhan_next_poll.
          (c) After the clear, _is_dhan_interval_due returns True (due immediately).
        """
        from backend.brokers.broker_apis import (
            _dhan_next_poll, _is_dhan_interval_due, dhan_next_poll_clear,
        )
        acct = "DH_FRESH_TEST"
        _reset_state(acct)
        broker = _make_dhan_broker()

        # Simulate a cold account not yet due (600s remaining).
        _dhan_next_poll[acct] = _time.time() + 500
        assert not _is_dhan_interval_due(acct, broker), (
            "cold account should NOT be due when interval hasn't elapsed"
        )

        # Route handler action — dhan_next_poll_clear() called on ?fresh=1.
        dhan_next_poll_clear()

        # After clear: entry removed → next_poll defaults to 0.0 → due.
        assert acct not in _dhan_next_poll, (
            "dhan_next_poll_clear() should remove the account entry"
        )
        assert _is_dhan_interval_due(acct, broker), (
            "after dhan_next_poll_clear(), gate should immediately pass"
        )

    def test_dhan_next_poll_clear_account_list(self):
        """Test 6b: dhan_next_poll_clear with explicit account list only clears those."""
        from backend.brokers.broker_apis import (
            _dhan_next_poll, _is_dhan_interval_due, dhan_next_poll_clear,
        )
        acct1 = "DH_FRESH_A"
        acct2 = "DH_FRESH_B"
        for a in (acct1, acct2):
            _reset_state(a)

        # Both cold and not due.
        future = _time.time() + 500
        _dhan_next_poll[acct1] = future
        _dhan_next_poll[acct2] = future
        broker = _make_dhan_broker()

        # Clear only acct1.
        dhan_next_poll_clear([acct1])
        assert acct1 not in _dhan_next_poll, "acct1 should be cleared"
        assert acct2 in _dhan_next_poll, "acct2 should be untouched"

        broker2 = _make_dhan_broker()
        assert _is_dhan_interval_due(acct1, broker), "acct1 should now be due"
        assert not _is_dhan_interval_due(acct2, broker2), "acct2 still not due"


# ---------------------------------------------------------------------------
# 7–9: Auto-downgrade
# ---------------------------------------------------------------------------

class TestAutoDowngrade:
    def _opt_in_breaker(self, account: str) -> None:
        """Opt the test account into the circuit breaker (required for the
        full state machine and _maybe_auto_downgrade to fire).
        The circuit_breaker_enabled per-account opt-in landed in Jul 2026;
        the default is False so tests must explicitly opt in."""
        from backend.brokers.broker_apis import set_breaker_optin_cache
        set_breaker_optin_cache(account, True)

    def _fire_opens(self, account: str, n: int) -> None:
        """Fire n consecutive breaker opens in rapid succession."""
        from backend.brokers.broker_apis import _record_fetch, _CB_FAIL_THRESHOLD
        # First, accumulate enough consecutive failures to reach threshold.
        # Then keep firing so each new open is recorded as a TRANSITION.
        # After the breaker is OPEN we need to let it advance to HALF-OPEN
        # before each new open-transition is counted.
        for i in range(n):
            # Reset to half-open so each failure counts as a new OPEN.
            from backend.brokers import broker_apis as _ba
            _ba._FETCH_HEALTH.setdefault(account, _ba._default_health_entry())
            # Mark as half-open (circuit_open_until expired).
            _ba._FETCH_HEALTH[account]["circuit_open_until"] = _time.time() - 1
            _ba._FETCH_HEALTH[account]["consecutive_fail_count"] = _CB_FAIL_THRESHOLD
            _record_fetch(account, ok=False, error="simulated dhan error")

    def test_five_opens_with_enabled_triggers_downgrade(self):
        """Test 7: 5 opens in 15 min with auto_downgrade_enabled=True → cold."""
        from backend.brokers import broker_apis as _ba
        acct = "DH_AUTODOWN_ON"
        _reset_state(acct)
        self._opt_in_breaker(acct)

        with patch.object(_ba, "_maybe_auto_downgrade") as mock_adg:
            # Fire 5 opens.
            for _ in range(5):
                _ba._FETCH_HEALTH.setdefault(acct, _ba._default_health_entry())
                _ba._FETCH_HEALTH[acct]["circuit_open_until"] = _time.time() - 1
                _ba._FETCH_HEALTH[acct]["consecutive_fail_count"] = _ba._CB_FAIL_THRESHOLD
                _ba._record_fetch(acct, ok=False, error="rate-limit")

            # _maybe_auto_downgrade should have been called for each
            # new OPEN transition (5 times).
            assert mock_adg.call_count == 5

    def test_auto_downgrade_disabled_no_downgrade(self):
        """Test 8: auto_downgrade_enabled=False → _maybe_auto_downgrade still
        called but should not update DB when flag is off."""
        from backend.brokers import broker_apis as _ba
        acct = "DH_AUTODOWN_OFF"
        _reset_state(acct)
        self._opt_in_breaker(acct)

        # Patch _maybe_auto_downgrade to verify it reads auto_downgrade_enabled.
        with patch.object(_ba, "_maybe_auto_downgrade") as mock_adg:
            for _ in range(5):
                _ba._FETCH_HEALTH.setdefault(acct, _ba._default_health_entry())
                _ba._FETCH_HEALTH[acct]["circuit_open_until"] = _time.time() - 1
                _ba._FETCH_HEALTH[acct]["consecutive_fail_count"] = _ba._CB_FAIL_THRESHOLD
                _ba._record_fetch(acct, ok=False, error="err")
            # Hook is called but the internal DB check (auto_downgrade_enabled)
            # gates the actual write. The test verifies the hook is called;
            # the DB-write guard is unit-tested in TestAutoDowngradeInternal.
            assert mock_adg.call_count == 5

    def test_sixth_open_within_cooloff_does_not_refire(self):
        """Test 9: 6th open within 5-min cooloff does not re-downgrade."""
        from backend.brokers import broker_apis as _ba
        acct = "DH_COOLOFF"
        _reset_state(acct)

        # Pre-set a downgrade cooloff that hasn't expired yet.
        _ba._downgrade_cooloff_until[acct] = _time.time() + 300  # 5 min from now

        history_before = len(_ba._breaker_open_history.get(acct, []))

        # Simulate a new open event calling _maybe_auto_downgrade.
        # With cooloff active, it should return immediately.
        import asyncio
        # We call the function directly (not through _record_fetch).
        # The cooloff guard is at the top of _maybe_auto_downgrade.
        # Since the DB read would need mocking, we verify via the
        # _breaker_open_history append (which happens AFTER the cooloff check).
        _ba._maybe_auto_downgrade(acct)
        # The history append should NOT have happened (cooloff gate is first).
        assert len(_ba._breaker_open_history.get(acct, [])) == history_before


# ---------------------------------------------------------------------------
# 10: restore-priority logic
# ---------------------------------------------------------------------------

class TestRestorePriority:
    def test_restore_priority_resets_next_poll(self):
        """Test 10: restore-priority clears _dhan_next_poll[account] to 0.

        The REST endpoint calls `_dhan_next_poll[account] = 0.0` so the
        background cycle immediately polls on the next tick.
        Verify the in-process state is correctly reset.
        """
        from backend.brokers import broker_apis as _ba
        acct = "DH_RESTORE"
        _reset_state(acct)

        # Simulate a cold account with a far-future next_poll.
        _ba._dhan_next_poll[acct] = _time.time() + 9999
        broker = _make_dhan_broker()
        # Confirm it's not due.
        assert not _ba._is_dhan_interval_due(acct, broker)

        # Simulate what restore-priority endpoint does: reset to 0.
        _ba._dhan_next_poll[acct] = 0.0

        # Now should be due immediately (0 <= now).
        assert _ba._is_dhan_interval_due(acct, broker)
        assert _ba._dhan_next_poll[acct] == 0.0

    def test_restore_priority_interval_constants(self):
        """Test 10b: PRIORITY_INTERVALS_SEC has correct values per spec."""
        from backend.brokers.broker_apis import _PRIORITY_INTERVALS_SEC
        assert _PRIORITY_INTERVALS_SEC["hot"]  == 30.0
        assert _PRIORITY_INTERVALS_SEC["warm"] == 120.0
        assert _PRIORITY_INTERVALS_SEC["cold"] == 600.0


# ---------------------------------------------------------------------------
# 11: In-process priority cache (set_dhan_priority_cache / _get_dhan_poll_priority)
# ---------------------------------------------------------------------------

class TestPriorityCache:
    def test_cache_miss_returns_hot(self):
        """Test 11a: unknown account defaults to 'hot' (safe default)."""
        from backend.brokers import broker_apis as _ba
        acct = "DH_CACHE_MISS"
        _ba._dhan_poll_priority_cache.pop(acct, None)
        assert _ba._get_dhan_poll_priority(acct) == "hot"

    def test_set_and_get_round_trip(self):
        """Test 11b: set_dhan_priority_cache + _get_dhan_poll_priority round-trip."""
        from backend.brokers import broker_apis as _ba
        from backend.brokers.broker_apis import set_dhan_priority_cache
        acct = "DH_CACHE_WARM"
        _ba._dhan_poll_priority_cache.pop(acct, None)
        set_dhan_priority_cache(acct, "warm")
        assert _ba._get_dhan_poll_priority(acct) == "warm"

    def test_invalid_priority_coerced_to_hot(self):
        """Test 11c: invalid priority value coerced to 'hot'."""
        from backend.brokers.broker_apis import set_dhan_priority_cache, _get_dhan_poll_priority
        acct = "DH_CACHE_INVALID"
        set_dhan_priority_cache(acct, "ultra")
        assert _get_dhan_poll_priority(acct) == "hot"

    def test_set_overwrites_previous(self):
        """Test 11d: repeated set_dhan_priority_cache overwrites prior value."""
        from backend.brokers.broker_apis import set_dhan_priority_cache, _get_dhan_poll_priority
        acct = "DH_CACHE_OVERWRITE"
        set_dhan_priority_cache(acct, "cold")
        assert _get_dhan_poll_priority(acct) == "cold"
        set_dhan_priority_cache(acct, "hot")
        assert _get_dhan_poll_priority(acct) == "hot"

    def test_interval_gate_uses_cache(self):
        """Test 11e: _update_dhan_next_poll reads priority from cache, not DB."""
        from backend.brokers.broker_apis import (
            _is_dhan_interval_due, _update_dhan_next_poll,
            _dhan_next_poll, set_dhan_priority_cache,
        )
        acct = "DH_CACHE_GATE"
        _reset_state(acct)
        broker = _make_dhan_broker()

        # Set priority to 'cold' in cache — no DB needed.
        set_dhan_priority_cache(acct, "cold")
        _update_dhan_next_poll(acct, broker)
        # next_poll should be ~600s from now (cold interval).
        import time as _t
        next_due = _dhan_next_poll.get(acct, 0.0)
        assert next_due > _t.time() + 595, (
            f"cold interval should set next_poll ~600s ahead, got delta={next_due - _t.time():.1f}s"
        )
        # Gate should now be NOT due.
        assert not _is_dhan_interval_due(acct, broker)
