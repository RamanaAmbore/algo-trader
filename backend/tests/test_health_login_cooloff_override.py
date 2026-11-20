"""2026-11-20 fix (F1(c) — live ZG0790 CAPTCHA incident).

Root cause: `_health_heartbeat` (`backend/brokers/service/app.py`) gates its
token-validity recheck on `hasattr(conn_obj, '_is_token_expired')`.
`KiteConnection` never implements that method (only Dhan/Groww do), so every
Kite account got `record_session_ok()` stamped every 90s UNCONDITIONALLY,
regardless of real auth state — the 90s heartbeat kept winning the
green/amber race against real (less frequent) `auth_fail`/`fetch_fail`
events for hours while the account was completely blocked by Zerodha's own
login cooloff. Empirically: ZG0790 had 1,503 `auth_fail` + 454 `fetch_fail`
events yet 179 heartbeat `session_ok` stamps kept firing throughout —
confirmed red-for-5s/green-for-85s flapping, and near-solid green for
00:00-02:00 UTC.

This file proves the consumer-side override in
`backend/api/routes/health.py:_hlth_resolve_state()`/`_derive_account_health()`
forces `red` whenever `is_cooling_off` is true on the per-account health-map
entry, REGARDLESS of heartbeat timing — i.e. regardless of how recently
`last_ok_at` was stamped, and regardless of circuit-breaker state. The
broker-layer half of this fix (conn_service surfacing `is_cooling_off`/
`blocked_until` on `/internal/health/brokers`, and `_health_heartbeat` no
longer calling `record_session_ok()` for a cooling-off account) is a parallel
change in `backend/brokers/`, out of scope here — this file only pins the
field-name contract (`is_cooling_off: bool`, `blocked_until: float | None`)
and the consumer's override behavior against that contract.
"""
from __future__ import annotations

import time

from backend.api.routes.health import _hlth_resolve_state, _derive_account_health


def _entry(**overrides) -> dict:
    base = {
        "last_ok_at": 0.0,
        "last_fail_at": 0.0,
        "last_fail_msg": "",
        "consecutive_fail_count": 0,
        "circuit_open_until": None,
        "circuit_last_opened_at": None,
        "open_cycle_count": 0,
    }
    base.update(overrides)
    return base


class TestCooloffForcesRed:
    def test_cooling_off_forces_red_even_with_a_fresh_heartbeat(self):
        """THE key regression case — a spurious 90s heartbeat just
        stamped `last_ok_at` to NOW (the exact false-green bug), but the
        account is actively cooling off. Must still read red."""
        now = time.time()
        entry = _entry(last_ok_at=now - 1.0, last_fail_at=now - 50.0,
                        is_cooling_off=True, blocked_until=now + 300.0)
        state, reason, cb_state, cb_count, cb_until_iso = _derive_account_health(entry, now)
        assert state == "red"
        assert "cooling off" in reason

    def test_cooling_off_forces_red_even_when_circuit_is_closed(self):
        now = time.time()
        entry = _entry(last_ok_at=now - 1.0, is_cooling_off=True, blocked_until=now + 120.0)
        state, *_ = _hlth_resolve_state(entry, now, cb_state="closed", cb_count=0,
                                         cb_until_iso=None, cb_until=None)
        assert state == "red"

    def test_cooling_off_false_does_not_force_red(self):
        """The override must be opt-in via a truthy flag — a normal
        healthy account (`is_cooling_off` absent or False) must resolve
        exactly as before this fix."""
        now = time.time()
        entry = _entry(last_ok_at=now - 1.0, is_cooling_off=False)
        state, *_ = _derive_account_health(entry, now)
        assert state == "green"

        entry2 = _entry(last_ok_at=now - 1.0)  # field entirely absent
        state2, *_ = _derive_account_health(entry2, now)
        assert state2 == "green"

    def test_cooling_off_reason_includes_retry_time_when_blocked_until_present(self):
        now = time.time()
        entry = _entry(is_cooling_off=True, blocked_until=now + 600.0)
        _, reason, *_ = _derive_account_health(entry, now)
        assert "retry at" in reason

    def test_cooling_off_without_blocked_until_still_forces_red(self):
        """`blocked_until` may be `None` (e.g. a cooloff tracked but the
        exact resume time not surfaced) — must still force red, with a
        degraded reason string, never crash on a missing timestamp."""
        now = time.time()
        entry = _entry(is_cooling_off=True, blocked_until=None)
        state, reason, *_ = _derive_account_health(entry, now)
        assert state == "red"
        assert reason == "login blocked — cooling off"

    def test_cooling_off_overrides_a_fresh_heartbeat_regardless_of_open_circuit(self):
        """Cooloff and circuit-breaker-open are independent, unrelated
        mechanisms — both resolve to red, and the cooloff check must not
        accidentally suppress the circuit-breaker reason's own
        correctness when both happen to be true (defensive — not the
        live incident's exact shape, but must not regress)."""
        now = time.time()
        entry = _entry(is_cooling_off=True, blocked_until=now + 60.0,
                        circuit_open_until=now + 900.0)
        state, reason, cb_state, cb_count, cb_until_iso = _derive_account_health(entry, now)
        assert state == "red"
        # The cooloff reason wins (checked first) — this is the documented
        # precedence, not an accidental masking of circuit info (cb_state
        # etc. are still correctly threaded through in the return tuple).
        assert cb_state == "open"


class TestFieldNameContract:
    """Pins the exact field names conn_service's `/internal/health/brokers`
    payload must use for this override to engage — a silent rename on
    either side (health.py vs. the broker-layer producer) would
    reintroduce the false-green gap with no test failure to catch it
    unless this contract is pinned explicitly."""

    def test_field_names_are_is_cooling_off_and_blocked_until(self):
        now = time.time()
        entry = _entry()
        entry["is_cooling_off"] = True
        entry["blocked_until"] = now + 10.0
        state, *_ = _derive_account_health(entry, now)
        assert state == "red"

    def test_a_differently_named_field_does_not_engage_the_override(self):
        """Negative control — confirms the override is keyed on the exact
        field name, not a fuzzy match."""
        now = time.time()
        entry = _entry(last_ok_at=now - 1.0)
        entry["cooling_off"] = True  # wrong key name
        state, *_ = _derive_account_health(entry, now)
        assert state == "green"
