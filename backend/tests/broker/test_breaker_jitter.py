"""
Tests for circuit-breaker cool-off jitter (Fix 1).

Five quality dimensions:
  SSOT        — jitter is applied at the single cool-off assignment in _record_fetch
  Correctness — circuit_open_until values diverge across N breaker opens;
                cool-off never exceeds _CB_MAX_COOLOFF_S + 30s
  Performance — uniform(0,30) adds negligible overhead (pure Python random call)
  Reuse       — same _record_fetch path tested by existing test_circuit_breaker.py
  UX          — different accounts tripping simultaneously produce distinct wake times
"""

from __future__ import annotations

import time as _time
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset(account: str) -> None:
    from backend.brokers import broker_apis
    broker_apis._FETCH_HEALTH.pop(account, None)


def _set_optin(account: str) -> None:
    from backend.brokers.broker_apis import set_breaker_optin_cache
    set_breaker_optin_cache(account, True)


def _clear_optin(account: str) -> None:
    from backend.brokers.broker_apis import _breaker_optin_cache
    _breaker_optin_cache.pop(account, None)


def _open_breaker(account: str) -> float:
    """Trip the breaker (3 consecutive fails) and return the circuit_open_until."""
    from backend.brokers.broker_apis import _record_fetch, _FETCH_HEALTH
    for _ in range(3):
        _record_fetch(account, ok=False, error="outage")
    return _FETCH_HEALTH[account]["circuit_open_until"]


# ---------------------------------------------------------------------------
# TestJitterRange — cool-off falls in [base, base+30)
# ---------------------------------------------------------------------------

class TestJitterRange:
    """Cool-off must always be at least the deterministic base and never
    exceed base + 30s (uniform(0,30) upper bound is exclusive)."""

    ACCOUNT = "DH6847_jitter_range"

    def setup_method(self):
        _reset(self.ACCOUNT)
        _set_optin(self.ACCOUNT)

    def teardown_method(self):
        _clear_optin(self.ACCOUNT)
        _reset(self.ACCOUNT)

    def test_first_cycle_cooloff_in_range(self):
        """First cycle base = 300s. Actual must be in [300, 330)."""
        from backend.brokers import broker_apis
        now = _time.time()
        until = _open_breaker(self.ACCOUNT)
        elapsed = until - now
        assert 300.0 <= elapsed < 331.0, (
            f"Expected 300–330s, got {elapsed:.2f}s"
        )

    def test_second_cycle_cooloff_in_range(self):
        """Second cycle base = 600s. Actual must be in [600, 630)."""
        from backend.brokers import broker_apis

        # First open
        _open_breaker(self.ACCOUNT)
        # Expire → half-open
        broker_apis._FETCH_HEALTH[self.ACCOUNT]["circuit_open_until"] = _time.time() - 1.0

        # Probe fails → second open
        now = _time.time()
        from backend.brokers.broker_apis import _record_fetch
        _record_fetch(self.ACCOUNT, ok=False, error="outage 2nd")
        until = broker_apis._FETCH_HEALTH[self.ACCOUNT]["circuit_open_until"]
        elapsed = until - now
        assert 600.0 <= elapsed < 631.0, (
            f"Expected 600–630s, got {elapsed:.2f}s"
        )

    def test_cap_cycle_cooloff_in_range(self):
        """At or past the cap cycle, actual must be in [1800, 1830)."""
        from backend.brokers import broker_apis
        from backend.brokers.broker_apis import _record_fetch

        # Advance to cycle 4 (cap cycle) by expiring breakers
        _open_breaker(self.ACCOUNT)  # cycle 1
        for _ in range(3):
            broker_apis._FETCH_HEALTH[self.ACCOUNT]["circuit_open_until"] = _time.time() - 1.0
            _record_fetch(self.ACCOUNT, ok=False, error="outage")  # cycles 2,3,4

        now = _time.time()
        until = broker_apis._FETCH_HEALTH[self.ACCOUNT]["circuit_open_until"]
        elapsed = until - now
        assert 1800.0 <= elapsed < 1831.0, (
            f"Expected 1800–1830s at cap cycle, got {elapsed:.2f}s"
        )


# ---------------------------------------------------------------------------
# TestJitterDivergence — multiple accounts differ after same failure mode
# ---------------------------------------------------------------------------

class TestJitterDivergence:
    """Two accounts tripping at the same instant must have different
    circuit_open_until values (jitter is per-open call, not global)."""

    ACCOUNT_A = "DH6847_jitter_div_A"
    ACCOUNT_B = "DH3747_jitter_div_B"

    def setup_method(self):
        for acct in (self.ACCOUNT_A, self.ACCOUNT_B):
            _reset(acct)
            _set_optin(acct)

    def teardown_method(self):
        for acct in (self.ACCOUNT_A, self.ACCOUNT_B):
            _clear_optin(acct)
            _reset(acct)

    def test_two_accounts_get_different_open_until(self):
        """With real random, P(identical) ≈ 0. Run 10 pairs and assert at
        least one pair differs. (Probability of all 10 identical ≈ 1e-18.)"""
        from backend.brokers import broker_apis

        found_different = False
        for _ in range(10):
            _reset(self.ACCOUNT_A)
            _reset(self.ACCOUNT_B)
            until_a = _open_breaker(self.ACCOUNT_A)
            until_b = _open_breaker(self.ACCOUNT_B)
            if abs(until_a - until_b) > 0.0001:
                found_different = True
                break

        assert found_different, (
            "All 10 pairs had identical circuit_open_until — jitter not working"
        )

    def test_divergence_within_jitter_window(self):
        """Both values must be within [300, 330) — divergence stays in-window."""
        from backend.brokers import broker_apis
        now = _time.time()
        until_a = _open_breaker(self.ACCOUNT_A)
        until_b = _open_breaker(self.ACCOUNT_B)

        for acct, until in [(self.ACCOUNT_A, until_a), (self.ACCOUNT_B, until_b)]:
            elapsed = until - now
            assert 300.0 <= elapsed < 331.0, (
                f"account={acct}: out-of-range {elapsed:.2f}s"
            )


# ---------------------------------------------------------------------------
# TestJitterCap — cap + jitter never exceeds _CB_MAX_COOLOFF_S + 30s
# ---------------------------------------------------------------------------

class TestJitterCap:
    """cool-off must never exceed _CB_MAX_COOLOFF_S + 30 regardless of cycle."""

    ACCOUNT = "DH6847_jitter_cap"

    def setup_method(self):
        _reset(self.ACCOUNT)
        _set_optin(self.ACCOUNT)

    def teardown_method(self):
        _clear_optin(self.ACCOUNT)
        _reset(self.ACCOUNT)

    def test_cap_plus_jitter_bound(self):
        """Run 20 consecutive open cycles and assert each cool-off ≤ 1830s."""
        from backend.brokers import broker_apis
        from backend.brokers.broker_apis import _record_fetch, _CB_MAX_COOLOFF_S

        cooloffs: list[float] = []
        for _ in range(20):
            e = broker_apis._FETCH_HEALTH.setdefault(
                self.ACCOUNT, broker_apis._default_health_entry()
            )
            e["consecutive_fail_count"] = broker_apis._CB_FAIL_THRESHOLD - 1
            now = _time.time()
            _record_fetch(self.ACCOUNT, ok=False, error="outage")
            until = broker_apis._FETCH_HEALTH[self.ACCOUNT]["circuit_open_until"]
            cooloffs.append(until - now)
            broker_apis._FETCH_HEALTH[self.ACCOUNT]["circuit_open_until"] = _time.time() - 1.0

        for i, c in enumerate(cooloffs):
            assert c <= _CB_MAX_COOLOFF_S + 31.0, (
                f"Cycle {i}: cool-off {c:.0f}s exceeds cap+jitter bound"
            )
