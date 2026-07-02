"""
Circuit-breaker tests for broker_apis._record_fetch / _is_circuit_open.

Covers five quality dimensions:
  SSOT        — single state machine in _record_fetch; no duplicate logic
  Correctness — state transitions, thresholds, exponential back-off, cap
  Performance — breaker short-circuits (no SDK call after OPEN)
  Reuse       — same breaker fields surfaced by fetch_health_snapshot()
  UX          — @for_all_accounts result when one account is OPEN

Scenario catalogue:
  1. Three consecutive DH-906 failures → breaker opens after 3rd.
  2. 4th call short-circuits — SDK NOT invoked.
  3. broker_health reflects circuit_state == 'open'.
  4. Advance time 301s → half-open; success → closed.
  5. Three failures, half-open, failure → re-open for 10 min (2nd cycle).
  6. 5 successive open cycles → cool-off caps at 1800 s.
  7. Success mid-stream resets consecutive_fail_count to 0.
  8. @for_all_accounts with one OPEN account returns result only for closed account.
  9. Groww GrowwAPIAuthenticationException triggers breaker identically.
"""

from __future__ import annotations

import threading
import time as _time
from unittest.mock import MagicMock, patch, call as mock_call

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_health(account: str) -> None:
    """Wipe the per-account entry so each test starts clean."""
    from backend.brokers import broker_apis
    broker_apis._FETCH_HEALTH.pop(account, None)


def _get_entry(account: str) -> dict:
    from backend.brokers import broker_apis
    return broker_apis._FETCH_HEALTH.get(account, {})


def _record(account: str, ok: bool, error: str = "") -> None:
    from backend.brokers.broker_apis import _record_fetch
    _record_fetch(account, ok=ok, error=error)


def _circuit_state(account: str) -> str:
    from backend.brokers.broker_apis import _circuit_state as _cs
    return _cs(account)


def _set_optin(account: str, enabled: bool = True) -> None:
    """Set the in-process breaker opt-in cache so tests can enable/disable
    the state machine without a DB row or rebuild_from_db call."""
    from backend.brokers.broker_apis import set_breaker_optin_cache
    set_breaker_optin_cache(account, enabled)


def _clear_optin(account: str) -> None:
    from backend.brokers.broker_apis import _breaker_optin_cache
    _breaker_optin_cache.pop(account, None)


# ---------------------------------------------------------------------------
# 1–3: Open after threshold
# ---------------------------------------------------------------------------

class TestBreakerOpens:
    """Three consecutive failures must open the breaker; 4th must short-circuit."""

    ACCOUNT = "DH6847_test_open"

    def setup_method(self):
        _reset_health(self.ACCOUNT)
        _set_optin(self.ACCOUNT)  # opt-in so the state machine is active

    def teardown_method(self):
        _clear_optin(self.ACCOUNT)

    def test_state_closed_initially(self):
        assert _circuit_state(self.ACCOUNT) == "closed"

    def test_open_after_three_failures(self):
        for i in range(3):
            _record(self.ACCOUNT, ok=False, error=f"DH-906 fail {i+1}")
        assert _circuit_state(self.ACCOUNT) == "open"

    def test_two_failures_still_closed(self):
        _record(self.ACCOUNT, ok=False, error="fail 1")
        _record(self.ACCOUNT, ok=False, error="fail 2")
        assert _circuit_state(self.ACCOUNT) == "closed"

    def test_open_until_set(self):
        now_before = _time.time()
        for _ in range(3):
            _record(self.ACCOUNT, ok=False, error="DH-906")
        e = _get_entry(self.ACCOUNT)
        until = e.get("circuit_open_until")
        assert until is not None
        # First cycle: 5-min cool-off
        assert abs(until - now_before - 300.0) < 2.0, (
            f"Expected ~300s cool-off, got {until - now_before:.1f}s"
        )

    def test_fourth_call_short_circuits_no_sdk_call(self):
        """After OPEN, _fetch_holdings_local must NOT call broker.holdings().

        We simulate by calling _fetch_holdings_local directly with a mock
        broker and verifying holdings() was never invoked.
        """
        from backend.brokers import broker_apis

        # Force OPEN
        for _ in range(3):
            _record(self.ACCOUNT, ok=False, error="DH-906")
        assert _circuit_state(self.ACCOUNT) == "open"

        mock_broker = MagicMock()
        mock_broker.holdings.return_value = [{"tradingsymbol": "RELIANCE"}]

        # Manually invoke the inner fetch function bypassing @for_all_accounts
        # (decorator resolves accounts from Connections singleton, not needed here).
        df = broker_apis._fetch_holdings_local.__wrapped__(
            connections=lambda: MagicMock(conn={}),
            account=self.ACCOUNT,
            kite=None,
            broker=mock_broker,
        )

        mock_broker.holdings.assert_not_called(), (
            "SDK must not be called when circuit is OPEN"
        )
        assert df.attrs.get("circuit_open") is True
        assert df.attrs.get("fetch_failed") is True

    def test_broker_health_shows_open(self):
        """fetch_health_snapshot() must include circuit_state='open'."""
        for _ in range(3):
            _record(self.ACCOUNT, ok=False, error="DH-906")

        from backend.brokers.broker_apis import fetch_health_snapshot
        # Non-cutover path: reads _FETCH_HEALTH directly.
        with patch("backend.brokers.broker_apis._use_conn_service", return_value=False):
            snap = fetch_health_snapshot()

        entry = snap.get(self.ACCOUNT, {})
        # circuit_open_until must be a future timestamp.
        assert entry.get("circuit_open_until") is not None
        assert entry.get("circuit_open_until") > _time.time()
        assert entry.get("consecutive_fail_count") == 3


# ---------------------------------------------------------------------------
# 4: Half-open → success → closed
# ---------------------------------------------------------------------------

class TestHalfOpen:
    ACCOUNT = "DH6847_test_halfopen"

    def setup_method(self):
        _reset_health(self.ACCOUNT)
        _set_optin(self.ACCOUNT)

    def teardown_method(self):
        _clear_optin(self.ACCOUNT)

    def test_halfopen_after_cooloff_then_success_closes(self):
        """Advance time past cool-off → half-open; success → closed."""
        for _ in range(3):
            _record(self.ACCOUNT, ok=False, error="DH-906")
        assert _circuit_state(self.ACCOUNT) == "open"

        # Advance circuit_open_until into the past (patch time).
        e = _get_entry(self.ACCOUNT)
        e["circuit_open_until"] = _time.time() - 1.0  # already expired

        # Verify half-open state
        assert _circuit_state(self.ACCOUNT) == "half-open"

        # Success probe → CLOSED
        _record(self.ACCOUNT, ok=True)
        assert _circuit_state(self.ACCOUNT) == "closed"
        e2 = _get_entry(self.ACCOUNT)
        assert e2.get("consecutive_fail_count") == 0
        assert e2.get("circuit_open_until") is None
        assert e2.get("open_cycle_count") == 0

    def test_no_sdk_inhibition_in_halfopen(self):
        """Half-open state must NOT short-circuit the SDK call."""
        from backend.brokers import broker_apis

        for _ in range(3):
            _record(self.ACCOUNT, ok=False, error="DH-906")

        e = _get_entry(self.ACCOUNT)
        e["circuit_open_until"] = _time.time() - 1.0  # half-open

        from backend.brokers.broker_apis import _is_circuit_open
        assert not _is_circuit_open(self.ACCOUNT), (
            "Half-open must NOT block the probe attempt"
        )


# ---------------------------------------------------------------------------
# 5: Half-open failure → re-open for 10 min (2nd cycle)
# ---------------------------------------------------------------------------

class TestExponentialBackoff:
    ACCOUNT = "DH6847_test_exp"

    def setup_method(self):
        _reset_health(self.ACCOUNT)
        _set_optin(self.ACCOUNT)

    def teardown_method(self):
        _clear_optin(self.ACCOUNT)

    def test_second_open_cycle_10_min(self):
        """3 fails → open (5m). Expire. 3 more → re-open (10m)."""
        # First open
        for _ in range(3):
            _record(self.ACCOUNT, ok=False, error="DH-906")
        assert _circuit_state(self.ACCOUNT) == "open"

        # Expire (half-open)
        _get_entry(self.ACCOUNT)["circuit_open_until"] = _time.time() - 1.0
        assert _circuit_state(self.ACCOUNT) == "half-open"

        # Probe fails → second open
        now_before = _time.time()
        _record(self.ACCOUNT, ok=False, error="DH-906 again")
        e = _get_entry(self.ACCOUNT)
        assert _circuit_state(self.ACCOUNT) == "open"
        until = e["circuit_open_until"]
        # 2nd cycle: 5m × 2^1 = 10m = 600s
        assert abs(until - now_before - 600.0) < 2.0, (
            f"2nd cycle expected ~600s, got {until - now_before:.1f}s"
        )
        assert e["open_cycle_count"] == 2

    def test_third_open_cycle_20_min(self):
        """Third open cycle: 5m × 2^2 = 20m = 1200s."""
        for _ in range(3):
            _record(self.ACCOUNT, ok=False, error="DH-906")
        _get_entry(self.ACCOUNT)["circuit_open_until"] = _time.time() - 1.0
        # 2nd fail
        _record(self.ACCOUNT, ok=False, error="DH-906")
        _get_entry(self.ACCOUNT)["circuit_open_until"] = _time.time() - 1.0
        # 3rd fail
        now = _time.time()
        _record(self.ACCOUNT, ok=False, error="DH-906")
        e = _get_entry(self.ACCOUNT)
        until = e["circuit_open_until"]
        assert abs(until - now - 1200.0) < 2.0, (
            f"3rd cycle expected ~1200s, got {until - now:.1f}s"
        )


# ---------------------------------------------------------------------------
# 6: Cool-off cap at 1800 s
# ---------------------------------------------------------------------------

class TestCooloffCap:
    ACCOUNT = "DH6847_test_cap"

    def setup_method(self):
        _reset_health(self.ACCOUNT)
        _set_optin(self.ACCOUNT)

    def teardown_method(self):
        _clear_optin(self.ACCOUNT)

    def test_cap_at_1800s_after_5_cycles(self):
        """After 5 open cycles, cool-off must not exceed 1800 s.

        Cycle sequence (open_cycle_count starts at 0):
          cycle 0 → 5m  × 2^0 = 300s  (count after = 1)
          cycle 1 → 5m  × 2^1 = 600s  (count after = 2)
          cycle 2 → 5m  × 2^2 = 1200s (count after = 3)
          cycle 3 → 5m  × 2^3 = 2400s → capped 1800s (count after = 4)
          cycle 4 → capped 1800s       (count after = 5)
        """
        from backend.brokers import broker_apis

        cooloffs: list[float] = []
        for _cycle in range(5):
            # Trigger threshold
            e = broker_apis._FETCH_HEALTH.setdefault(
                self.ACCOUNT, broker_apis._default_health_entry()
            )
            # Reset only consecutive_fail_count so each cycle starts fresh
            e["consecutive_fail_count"] = broker_apis._CB_FAIL_THRESHOLD - 1
            now = _time.time()
            _record(self.ACCOUNT, ok=False, error="DH-906")
            until = broker_apis._FETCH_HEALTH[self.ACCOUNT]["circuit_open_until"]
            cooloffs.append(until - now)
            # Expire breaker for the next iteration
            broker_apis._FETCH_HEALTH[self.ACCOUNT]["circuit_open_until"] = _time.time() - 1.0

        assert cooloffs[0] <= 305.0
        assert cooloffs[1] <= 605.0
        assert cooloffs[2] <= 1205.0
        # Cycles 3+ must be capped
        assert cooloffs[3] <= 1805.0
        assert cooloffs[4] <= 1805.0
        # All caps respect the 1800s maximum
        for c in cooloffs[3:]:
            assert c <= broker_apis._CB_MAX_COOLOFF_S + 2.0, (
                f"Cool-off exceeded cap: {c:.0f}s"
            )


# ---------------------------------------------------------------------------
# 7: Success mid-stream resets counter
# ---------------------------------------------------------------------------

class TestResetOnSuccess:
    ACCOUNT = "DH6847_test_reset"

    def setup_method(self):
        _reset_health(self.ACCOUNT)
        _set_optin(self.ACCOUNT)

    def teardown_method(self):
        _clear_optin(self.ACCOUNT)

    def test_success_resets_fail_count(self):
        _record(self.ACCOUNT, ok=False, error="fail1")
        _record(self.ACCOUNT, ok=False, error="fail2")
        assert _get_entry(self.ACCOUNT)["consecutive_fail_count"] == 2
        _record(self.ACCOUNT, ok=True)
        assert _get_entry(self.ACCOUNT)["consecutive_fail_count"] == 0

    def test_success_prevents_open(self):
        """2 fails + 1 success: should NOT open (counter reset to 0)."""
        _record(self.ACCOUNT, ok=False, error="fail1")
        _record(self.ACCOUNT, ok=False, error="fail2")
        _record(self.ACCOUNT, ok=True)
        # Now 2 more fails — still only 2 consecutive, not 3
        _record(self.ACCOUNT, ok=False, error="fail3")
        _record(self.ACCOUNT, ok=False, error="fail4")
        assert _circuit_state(self.ACCOUNT) == "closed"


# ---------------------------------------------------------------------------
# 8: @for_all_accounts with OPEN account
# ---------------------------------------------------------------------------

class TestForAllAccountsSkip:
    """When one account's circuit is OPEN, the fetch for that account returns
    an empty DataFrame (short-circuit); the healthy account proceeds normally.

    Implementation note: @for_all_accounts still CALLS _fetch_holdings_local
    for both accounts (the decorator has no breaker awareness — that's by
    design per the advisor's recommendation: no coupling between decorators.py
    and broker_apis state). The breaker check is at entry of _fetch_*_local
    so the SDK call is skipped, but the decorator still dispatches.
    """

    OPEN_ACCOUNT  = "DH6847_test_fanout_open"
    GOOD_ACCOUNT  = "DH6847_test_fanout_good"

    def setup_method(self):
        _reset_health(self.OPEN_ACCOUNT)
        _reset_health(self.GOOD_ACCOUNT)
        _set_optin(self.OPEN_ACCOUNT)
        _set_optin(self.GOOD_ACCOUNT)

    def teardown_method(self):
        _clear_optin(self.OPEN_ACCOUNT)
        _clear_optin(self.GOOD_ACCOUNT)

    def test_open_account_returns_empty_failed_df(self):
        """Manually invoke _fetch_holdings_local for the open account."""
        from backend.brokers import broker_apis

        # Force OPEN
        for _ in range(3):
            _record(self.OPEN_ACCOUNT, ok=False, error="DH-906")

        mock_broker = MagicMock()
        mock_broker.holdings.return_value = [{"tradingsymbol": "RELIANCE"}]

        df = broker_apis._fetch_holdings_local.__wrapped__(
            connections=lambda: MagicMock(conn={}),
            account=self.OPEN_ACCOUNT,
            kite=None,
            broker=mock_broker,
        )

        mock_broker.holdings.assert_not_called()
        assert df.empty
        assert df.attrs.get("fetch_failed") is True
        assert df.attrs.get("circuit_open") is True

    def test_closed_account_proceeds_normally(self):
        """Closed-circuit account must call SDK normally."""
        from backend.brokers import broker_apis

        mock_broker = MagicMock()
        mock_broker.holdings.return_value = []

        df = broker_apis._fetch_holdings_local.__wrapped__(
            connections=lambda: MagicMock(conn={}),
            account=self.GOOD_ACCOUNT,
            kite=None,
            broker=mock_broker,
        )

        mock_broker.holdings.assert_called_once()


# ---------------------------------------------------------------------------
# 9: Groww authentication exception triggers breaker
# ---------------------------------------------------------------------------

class TestGrowwBreakerTrigger:
    """GrowwAPIAuthenticationException (or any exception) from broker.holdings()
    routes through the existing except block which calls _record_fetch(ok=False).
    After 3 consecutive failures, breaker must OPEN.

    We test this via the except path inside _fetch_holdings_local (which catches
    all Exception and calls _record_fetch(ok=False)).
    """

    ACCOUNT = "GR87DF_test_groww"

    def setup_method(self):
        _reset_health(self.ACCOUNT)
        _set_optin(self.ACCOUNT)

    def teardown_method(self):
        _clear_optin(self.ACCOUNT)

    def _simulate_groww_auth_error(self, n: int) -> None:
        """Simulate n consecutive Groww auth failures via the actual
        _fetch_holdings_local error path (broker.holdings raises)."""
        from backend.brokers import broker_apis

        for _ in range(n):
            mock_broker = MagicMock()
            mock_broker.holdings.side_effect = Exception(
                "GrowwAPIAuthenticationException: token expired"
            )
            broker_apis._fetch_holdings_local.__wrapped__(
                connections=lambda: MagicMock(conn={}),
                account=self.ACCOUNT,
                kite=None,
                broker=mock_broker,
            )

    def test_three_groww_auth_failures_open_breaker(self):
        self._simulate_groww_auth_error(3)
        assert _circuit_state(self.ACCOUNT) == "open"

    def test_fourth_groww_call_short_circuits(self):
        self._simulate_groww_auth_error(3)
        assert _circuit_state(self.ACCOUNT) == "open"

        # 4th call — SDK must NOT be invoked
        from backend.brokers import broker_apis
        mock_broker = MagicMock()
        mock_broker.holdings.return_value = []

        df = broker_apis._fetch_holdings_local.__wrapped__(
            connections=lambda: MagicMock(conn={}),
            account=self.ACCOUNT,
            kite=None,
            broker=mock_broker,
        )

        mock_broker.holdings.assert_not_called()
        assert df.attrs.get("circuit_open") is True


# ---------------------------------------------------------------------------
# 10: Concurrent-probe race — only one cycle increment per HALF-OPEN event
# ---------------------------------------------------------------------------

class TestConcurrentProbeRace:
    """Three parallel probe failures during HALF-OPEN must advance
    open_cycle_count by exactly 1, not 3.

    Simulated sequentially under the lock (which is what actually happens
    in the threaded executor) — the race is that three _record_fetch calls
    see expired circuit_open_until and all take the re-open branch.
    """

    ACCOUNT = "DH6847_test_race"

    def setup_method(self):
        _reset_health(self.ACCOUNT)
        _set_optin(self.ACCOUNT)

    def teardown_method(self):
        _clear_optin(self.ACCOUNT)

    def test_three_parallel_halfopen_failures_bump_cycle_once(self):
        import time as _t
        from backend.brokers import broker_apis

        # Put breaker in OPEN state (open_cycle_count = 1)
        for _ in range(3):
            _record(self.ACCOUNT, ok=False, error="DH-906")
        assert _circuit_state(self.ACCOUNT) == "open"
        assert _get_entry(self.ACCOUNT)["open_cycle_count"] == 1

        # Expire → HALF-OPEN
        _get_entry(self.ACCOUNT)["circuit_open_until"] = _t.time() - 1.0
        assert _circuit_state(self.ACCOUNT) == "half-open"

        # Three consecutive probe failures (simulates parallel dispatch
        # landing sequentially under the lock).
        _record(self.ACCOUNT, ok=False, error="probe fail 1")
        _record(self.ACCOUNT, ok=False, error="probe fail 2")
        _record(self.ACCOUNT, ok=False, error="probe fail 3")

        e = _get_entry(self.ACCOUNT)
        assert _circuit_state(self.ACCOUNT) == "open"
        assert e["open_cycle_count"] == 2, (
            f"Expected exactly 1 cycle advance (0→1→2), got {e['open_cycle_count']}"
        )
        # 2nd cycle cool-off is 10 min = 600s
        import time as _time2
        until = e["circuit_open_until"]
        assert abs(until - _time2.time() - 600.0) < 5.0, (
            f"Expected ~600s 2nd-cycle cooloff, got {until - _time2.time():.1f}s"
        )


# ---------------------------------------------------------------------------
# 11: Per-account opt-in — non-opt-in accounts never enter OPEN state
# ---------------------------------------------------------------------------

class TestBreakerOptIn:
    """circuit_breaker_enabled gates the state machine.

    When an account is NOT in _breaker_optin_cache (defaults to False),
    _record_fetch must update health stamps but NEVER open the circuit.
    _is_circuit_open must return False regardless of consecutive fail count.
    """

    OPT_OUT_ACCOUNT = "DH3747_test_optout"
    OPT_IN_ACCOUNT  = "DH6847_test_optin"

    def setup_method(self):
        _reset_health(self.OPT_OUT_ACCOUNT)
        _reset_health(self.OPT_IN_ACCOUNT)
        from backend.brokers.broker_apis import _breaker_optin_cache
        # Ensure clean slate for both accounts in the in-process cache.
        _breaker_optin_cache.pop(self.OPT_OUT_ACCOUNT, None)
        _breaker_optin_cache.pop(self.OPT_IN_ACCOUNT, None)

    def teardown_method(self):
        from backend.brokers.broker_apis import _breaker_optin_cache
        _breaker_optin_cache.pop(self.OPT_OUT_ACCOUNT, None)
        _breaker_optin_cache.pop(self.OPT_IN_ACCOUNT, None)

    def test_non_optin_never_opens_after_10_failures(self):
        """Non-opt-in account stays CLOSED after 10 consecutive failures."""
        for i in range(10):
            _record(self.OPT_OUT_ACCOUNT, ok=False, error=f"fail {i}")
        assert _circuit_state(self.OPT_OUT_ACCOUNT) == "closed", (
            "Non-opt-in account must never enter OPEN state"
        )

    def test_non_optin_is_circuit_open_returns_false(self):
        """_is_circuit_open must return False for non-opt-in accounts
        regardless of the underlying _circuit_state value."""
        from backend.brokers.broker_apis import _is_circuit_open
        # Manually force an 'open' entry to verify the gate overrides it.
        from backend.brokers import broker_apis
        e = broker_apis._FETCH_HEALTH.setdefault(
            self.OPT_OUT_ACCOUNT, broker_apis._default_health_entry()
        )
        e["circuit_open_until"] = _time.time() + 300.0
        # Opt-in cache has no entry → defaults to False.
        assert not _is_circuit_open(self.OPT_OUT_ACCOUNT), (
            "_is_circuit_open must be False for non-opt-in even with open_until set"
        )

    def test_non_optin_record_fetch_updates_health_stamps(self):
        """_record_fetch for non-opt-in accounts must still update
        last_ok_at / last_fail_at for the health badge."""
        before = _time.time()
        _record(self.OPT_OUT_ACCOUNT, ok=False, error="transient error")
        e = _get_entry(self.OPT_OUT_ACCOUNT)
        assert e.get("last_fail_at", 0) >= before, (
            "last_fail_at must be updated for non-opt-in accounts"
        )
        # Breaker fields must stay at defaults.
        assert e.get("consecutive_fail_count", 0) == 0, (
            "consecutive_fail_count must not be incremented for non-opt-in"
        )
        assert e.get("circuit_open_until") is None, (
            "circuit_open_until must remain None for non-opt-in"
        )

        # Success also updates last_ok_at
        _record(self.OPT_OUT_ACCOUNT, ok=True)
        e2 = _get_entry(self.OPT_OUT_ACCOUNT)
        assert e2.get("last_ok_at", 0) >= before

    def test_optin_account_opens_normally(self):
        """An opt-in account obeys the full state machine."""
        from backend.brokers.broker_apis import set_breaker_optin_cache
        set_breaker_optin_cache(self.OPT_IN_ACCOUNT, True)

        for i in range(3):
            _record(self.OPT_IN_ACCOUNT, ok=False, error=f"DH-906 fail {i}")
        assert _circuit_state(self.OPT_IN_ACCOUNT) == "open", (
            "Opt-in account must enter OPEN after 3 consecutive failures"
        )
        e = _get_entry(self.OPT_IN_ACCOUNT)
        assert e["consecutive_fail_count"] >= 3
        assert e["circuit_open_until"] is not None

    def test_set_breaker_optin_cache_toggles_gate(self):
        """set_breaker_optin_cache wires the in-process cache; toggling OFF
        must cause _is_circuit_open to return False again."""
        from backend.brokers.broker_apis import (
            set_breaker_optin_cache,
            get_breaker_optin_cache,
            _is_circuit_open,
        )
        # Opt in.
        set_breaker_optin_cache(self.OPT_IN_ACCOUNT, True)
        assert get_breaker_optin_cache(self.OPT_IN_ACCOUNT) is True

        # Open the breaker.
        for i in range(3):
            _record(self.OPT_IN_ACCOUNT, ok=False, error=f"fail {i}")
        assert _is_circuit_open(self.OPT_IN_ACCOUNT), (
            "Opt-in + open state must return True from _is_circuit_open"
        )

        # Operator disables circuit breaker via PATCH → cache updated immediately.
        set_breaker_optin_cache(self.OPT_IN_ACCOUNT, False)
        assert get_breaker_optin_cache(self.OPT_IN_ACCOUNT) is False
        assert not _is_circuit_open(self.OPT_IN_ACCOUNT), (
            "After disabling opt-in, _is_circuit_open must return False"
        )


# ---------------------------------------------------------------------------
# 12: Auto-downgrade requires BOTH circuit_breaker_enabled AND auto_downgrade_enabled
# ---------------------------------------------------------------------------

class TestAutoDowngradeRequiresBothFlags:
    """_maybe_auto_downgrade must be a no-op when circuit_breaker_enabled=False.

    This is enforced in the DB check inside _check_and_update(). We patch
    the async DB read to return a mock row and verify the early-return fires.
    """

    ACCOUNT = "DH3747_test_adg_guard"

    def setup_method(self):
        _reset_health(self.ACCOUNT)
        from backend.brokers.broker_apis import _breaker_optin_cache, _breaker_open_history
        _breaker_optin_cache.pop(self.ACCOUNT, None)
        _breaker_open_history.pop(self.ACCOUNT, None)

    def teardown_method(self):
        from backend.brokers.broker_apis import _breaker_optin_cache, _breaker_open_history
        _breaker_optin_cache.pop(self.ACCOUNT, None)
        _breaker_open_history.pop(self.ACCOUNT, None)

    def test_auto_downgrade_skipped_when_breaker_disabled(self):
        """Simulate 5 opens in history; verify _maybe_auto_downgrade short-circuits
        when circuit_breaker_enabled=False on the mock DB row.

        Strategy: we run _check_and_update on a real event loop via asyncio.run()
        so no coroutine is left dangling (eliminating the RuntimeWarning). We patch
        the DB session to return a mock row with circuit_breaker_enabled=False and
        assert poll_priority is never mutated.
        """
        import asyncio
        from unittest.mock import MagicMock, AsyncMock, patch
        from backend.brokers import broker_apis

        # Simulate 5 open events in the breaker_open_history.
        import time as _t
        broker_apis._breaker_open_history[self.ACCOUNT] = [
            _t.time() - i * 30 for i in range(5)
        ]

        # Mock row with circuit_breaker_enabled=False, auto_downgrade_enabled=True.
        mock_row = MagicMock()
        mock_row.circuit_breaker_enabled = False
        mock_row.auto_downgrade_enabled = True
        mock_row.poll_priority = "hot"

        # Patch the shared_async_session to return the mock row and set
        # get_main_loop to return a real running loop so run_coroutine_threadsafe
        # can schedule and execute the inner coroutine without leaking it.
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_row
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from backend.brokers.broker_apis import _maybe_auto_downgrade

        # Run the event loop in a background thread so run_coroutine_threadsafe
        # can actually schedule and drain the coroutine. This avoids any
        # RuntimeWarning about unawaited coroutines.
        loop = asyncio.new_event_loop()
        import threading

        def _run_loop():
            loop.run_forever()

        t = threading.Thread(target=_run_loop, daemon=True)
        t.start()
        try:
            with patch("backend.api.database.shared_async_session", return_value=mock_session), \
                 patch("backend.api.persistence.write_queue.get_main_loop", return_value=loop):
                _maybe_auto_downgrade(self.ACCOUNT)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2.0)
            loop.close()

        # poll_priority must NOT have been set on the mock row.
        assert mock_row.poll_priority == "hot", (
            "poll_priority must not change when circuit_breaker_enabled=False"
        )
