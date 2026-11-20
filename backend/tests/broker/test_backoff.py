"""Tests for the shared exponential-backoff utility (Part 3, 2026-11-20
incident-fix batch) — `backend/shared/helpers/backoff.py`.

Two existing retry/backoff mechanisms — `retry_kite_conn`
(`backend/shared/helpers/decorators.py`) and the Kite login cooloff
(`_attempt_login_with_cooloff`, `backend/brokers/connections.py`) — were
independently reimplementing the same `min(base * 2**n, cap)` shape with
different literal numbers. Both now call this one shared, pure formula.

The table-driven parity tests below are the actual acceptance criterion:
the migrated call sites must produce BYTE-IDENTICAL delay values to their
pre-existing inline formulas at every attempt count, not merely "a similar
curve". `retry_kite_conn`'s own pre-existing formula was `min(2 ** attempt,
30)` — i.e. an implicit `base=1`, NOT `base=2` (a base of 2.0 would compute
`2 * 2**attempt`, which produces 2, 4, 8, 16, 30, 30, ... instead of the
real 1, 2, 4, 8, 16, 30, 30, ... — a different curve, confirmed by the
parity test below). `base=1.0` is what actually reproduces it exactly.

Five quality dimensions:
  SSOT        — `retry_kite_conn` and `_attempt_login_with_cooloff` both
                call this one function; no second inline formula survives
                at either site (grep-checked below).
  Correctness — table-driven parity against each site's own OLD formula,
                attempts 0-10; cap behaviour; negative-attempt clamp.
  Performance — pure function, no sleep, no I/O, no state — asserted via
                a plain call-count-free computation (nothing to mock).
  Reuse       — one shared formula, not two near-duplicates.
  UX          — every parity assertion carries the attempt index and both
                computed values in its failure message.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from backend.shared.helpers.backoff import exponential_backoff_seconds


# ---------------------------------------------------------------------------
# A: exponential_backoff_seconds — pure formula behaviour
# ---------------------------------------------------------------------------

class TestExponentialBackoffSecondsFormula:
    def test_default_base_and_cap(self):
        assert exponential_backoff_seconds(0) == 2.0
        assert exponential_backoff_seconds(1) == 4.0
        assert exponential_backoff_seconds(2) == 8.0
        assert exponential_backoff_seconds(10) == 30.0  # capped

    def test_custom_base_and_cap(self):
        assert exponential_backoff_seconds(0, base=120.0, cap=1800.0) == 120.0
        assert exponential_backoff_seconds(1, base=120.0, cap=1800.0) == 240.0
        assert exponential_backoff_seconds(3, base=120.0, cap=1800.0) == 960.0
        assert exponential_backoff_seconds(4, base=120.0, cap=1800.0) == 1800.0  # capped
        assert exponential_backoff_seconds(20, base=120.0, cap=1800.0) == 1800.0

    def test_cap_is_never_exceeded_for_a_large_attempt(self):
        for attempt in range(0, 50):
            assert exponential_backoff_seconds(attempt, base=1.0, cap=30.0) <= 30.0

    def test_negative_attempt_is_clamped_to_zero_not_raised_or_negative(self):
        """A caller computing attempt from a decrementing counter (e.g.
        streak - 1 when streak == 0) must never get back a delay smaller
        than `base`, and must never raise."""
        assert exponential_backoff_seconds(-1, base=5.0, cap=100.0) == 5.0
        assert exponential_backoff_seconds(-99, base=5.0, cap=100.0) == 5.0

    def test_no_jitter_by_default_is_deterministic(self):
        for _ in range(5):
            assert exponential_backoff_seconds(3, base=2.0, cap=30.0) == 16.0

    def test_jitter_stays_within_plus_minus_ten_percent_of_capped_value(self):
        base_value = exponential_backoff_seconds(10, base=2.0, cap=30.0)  # 30.0, capped
        for _ in range(200):
            jittered = exponential_backoff_seconds(10, base=2.0, cap=30.0, jitter=True)
            assert base_value * 0.9 <= jittered <= base_value * 1.1, (
                f"jittered value {jittered} outside +/-10% of {base_value}"
            )

    def test_pure_function_no_sleep_no_state_no_persistence(self):
        """This is the whole design contract (Part 3's own plan text):
        a pure formula only. Confirmed structurally: the function's
        source references no `time.sleep`, no module-level mutable
        state, and no file/DB I/O."""
        src = inspect.getsource(exponential_backoff_seconds)
        assert "sleep" not in src, "must never sleep — pure formula only"
        assert "open(" not in src, "must never touch the filesystem"
        assert "global " not in src, "must carry no module-level state"


# ---------------------------------------------------------------------------
# B: retry_kite_conn parity — old inline `min(2 ** attempt, 30)` vs the
#    migrated `exponential_backoff_seconds(attempt, base=1.0, cap=30.0)`
# ---------------------------------------------------------------------------

def _old_retry_kite_conn_formula(attempt: int) -> float:
    """Byte-for-byte the pre-migration inline formula from
    `decorators.py:retry_kite_conn` — kept here ONLY as the reference
    for this parity test, never imported by production code."""
    return min(2 ** attempt, 30)


class TestRetryKiteConnBackoffParity:
    @pytest.mark.parametrize("attempt", range(0, 11))
    def test_byte_identical_to_old_formula_at_every_attempt(self, attempt):
        old = _old_retry_kite_conn_formula(attempt)
        new = exponential_backoff_seconds(attempt, base=1.0, cap=30.0)
        assert new == old, (
            f"attempt={attempt}: old formula gave {old}, new utility "
            f"(base=1.0, cap=30.0) gave {new} — must be byte-identical"
        )

    def test_base_2_would_NOT_have_been_byte_identical(self):
        """Documents why base=1.0, not base=2.0, is correct at this call
        site — a literal reading of 'min(2 ** attempt, 30)' might suggest
        base=2.0, but that computes a DIFFERENT (doubled) curve. This
        test pins the divergence so a future refactor can't silently
        'correct' base=1.0 back to the wrong value."""
        for attempt in range(0, 5):
            old = _old_retry_kite_conn_formula(attempt)
            wrong = exponential_backoff_seconds(attempt, base=2.0, cap=30.0)
            if old < 30:  # below the cap, where the two curves actually differ
                assert wrong != old, (
                    f"attempt={attempt}: base=2.0 unexpectedly matched the "
                    f"old formula ({wrong} == {old}) — re-verify the curves"
                )

    def test_decorators_module_no_longer_has_the_inline_formula(self):
        """SSOT — the literal `2 ** attempt` formula must not still exist
        as a second, parallel implementation (executable code, not just
        explanatory comment text) inside the decorator."""
        import backend.shared.helpers.decorators as decorators_module
        src = inspect.getsource(decorators_module.retry_kite_conn)
        code_lines = [
            line for line in src.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        code_only = "\n".join(code_lines)
        assert "2 ** attempt" not in code_only and "2**attempt" not in code_only, (
            "retry_kite_conn must call exponential_backoff_seconds(), "
            "not keep its own inline formula as executable code"
        )
        assert "exponential_backoff_seconds" in src


# ---------------------------------------------------------------------------
# C: Kite login cooloff parity — old inline
#    `min(120 * (2 ** (streak - 1)), 1800)` vs the migrated
#    `exponential_backoff_seconds(streak - 1, base=120.0, cap=1800.0)`
# ---------------------------------------------------------------------------

def _old_kite_cooloff_formula(login_fail_streak: int) -> float:
    """Byte-for-byte the pre-migration inline formula from
    `connections.py:_attempt_login_with_cooloff` — reference only."""
    return min(120 * (2 ** (login_fail_streak - 1)), 1800)


class TestKiteLoginCooloffBackoffParity:
    @pytest.mark.parametrize("streak", range(1, 12))  # streak starts at 1
    def test_byte_identical_to_old_formula_at_every_streak(self, streak):
        old = _old_kite_cooloff_formula(streak)
        new = exponential_backoff_seconds(streak - 1, base=120.0, cap=1800.0)
        assert new == old, (
            f"streak={streak}: old formula gave {old}, new utility "
            f"(base=120.0, cap=1800.0) gave {new} — must be byte-identical"
        )

    def test_connections_module_no_longer_has_the_inline_formula(self):
        """SSOT — the literal `120 * (2 ** ...)` formula must not still
        exist as a second, parallel implementation."""
        import backend.brokers.connections as connections_module
        src = inspect.getsource(connections_module.KiteConnection._attempt_login_with_cooloff)
        assert "120 * (2 **" not in src and "120 * (2**" not in src, (
            "_attempt_login_with_cooloff must call "
            "exponential_backoff_seconds(), not keep its own inline formula"
        )
        assert "exponential_backoff_seconds" in src


# ---------------------------------------------------------------------------
# D: scope discipline — nothing else was migrated onto this utility.
# ---------------------------------------------------------------------------

class TestOtherBackoffMechanismsUntouched:
    """Dhan's flat cooloff, PriceBroker's rate-limit cooloff, chase's
    backoff, and KiteTicker's reconnect all have a genuine reason to stay
    bespoke (per the Part 3 plan's own audit) and must not import or
    reference this new utility."""

    def test_dhan_connection_does_not_use_the_shared_formula(self):
        import backend.brokers.connections as connections_module
        src = inspect.getsource(connections_module.DhanConnection)
        assert "exponential_backoff_seconds" not in src

    def test_price_broker_rate_limit_cooloff_does_not_use_the_shared_formula(self):
        import backend.brokers.registry as registry_module
        src = inspect.getsource(registry_module)
        assert "exponential_backoff_seconds" not in src


# ---------------------------------------------------------------------------
# E (2026-11-20, same session, operator-reported Alerts-tab flood during
# the incident): `retry_kite_conn` must fail fast — no retry, no sleep,
# DEBUG not ERROR — on `KiteLoginCoolingOff` specifically, while every
# OTHER exception type keeps today's exact 3-attempt-then-ERROR behaviour.
# The genuine-exhaustion ERROR line must also now include the last
# exception's own text, so F2's captured real error message (e.g.
# "Invalid CAPTCHA values.") actually reaches an operator reading it.
# ---------------------------------------------------------------------------

class TestKiteLoginCoolingOffFailsFastAndLogsAtDebug:
    def test_fails_fast_single_attempt_no_sleep_logs_at_debug_not_error(self, monkeypatch):
        from backend.shared.helpers.decorators import retry_kite_conn
        from backend.brokers.errors import KiteLoginCoolingOff

        call_count = {"n": 0}
        sleep_mock = MagicMock()
        debug_mock = MagicMock()
        error_mock = MagicMock()
        monkeypatch.setattr("backend.shared.helpers.decorators.time.sleep", sleep_mock)
        monkeypatch.setattr("backend.shared.helpers.decorators.logger.debug", debug_mock)
        monkeypatch.setattr("backend.shared.helpers.decorators.logger.error", error_mock)

        @retry_kite_conn(max_attempts=3)
        def always_cooling_off():
            call_count["n"] += 1
            raise KiteLoginCoolingOff(
                "Kite login cooling off for 'ZG0790' — wait 118s before retrying"
            )

        with pytest.raises(KiteLoginCoolingOff):
            always_cooling_off()

        assert call_count["n"] == 1, (
            f"expected exactly ONE attempt (fail fast, no retry), got "
            f"{call_count['n']}"
        )
        sleep_mock.assert_not_called(), "must never sleep for a known cooloff block"
        error_mock.assert_not_called(), (
            "must not log at ERROR for this known-deterministic case"
        )
        debug_mock.assert_called()
        debug_text = " ".join(str(c.args[0]) for c in debug_mock.call_args_list)
        assert "cooling off" in debug_text.lower()

    def test_a_different_exception_through_the_same_decorator_still_retries_3x_and_logs_error(
        self, monkeypatch,
    ):
        """No regression to the general case — confirms the
        KiteLoginCoolingOff fast-path is narrowly scoped to that one type."""
        from backend.shared.helpers.decorators import retry_kite_conn
        from backend.brokers.errors import BrokerNetworkError

        call_count = {"n": 0}
        error_mock = MagicMock()
        monkeypatch.setattr("backend.shared.helpers.decorators.logger.error", error_mock)
        monkeypatch.setattr("backend.shared.helpers.decorators.time.sleep", lambda *a: None)

        @retry_kite_conn(max_attempts=3)
        def always_network_error():
            call_count["n"] += 1
            raise BrokerNetworkError("502 Bad Gateway")

        with pytest.raises(BrokerNetworkError):
            always_network_error()

        assert call_count["n"] == 3, (
            f"a genuinely different exception must still retry the full "
            f"3 attempts, got {call_count['n']}"
        )
        error_mock.assert_called_once()

    def test_exhaustion_error_log_includes_the_last_exception_text(self, monkeypatch):
        """F2's captured real error text (e.g. the Zerodha CAPTCHA
        message) must actually reach this exhaustion log line, not be
        silently dropped by a generic wrapper phrase."""
        from backend.shared.helpers.decorators import retry_kite_conn

        error_mock = MagicMock()
        monkeypatch.setattr("backend.shared.helpers.decorators.logger.error", error_mock)
        monkeypatch.setattr("backend.shared.helpers.decorators.time.sleep", lambda *a: None)

        @retry_kite_conn(max_attempts=2)
        def always_fails_with_real_message():
            raise RuntimeError("Invalid CAPTCHA values.")

        with pytest.raises(RuntimeError):
            always_fails_with_real_message()

        error_mock.assert_called_once()
        logged_text = str(error_mock.call_args[0][0])
        assert "Invalid CAPTCHA values." in logged_text, (
            f"expected the last exception's own text in the exhaustion "
            f"log line, got {logged_text!r}"
        )
        assert "Operation failed after 2 attempts" in logged_text, (
            "the generic wrapper phrase must still be present too, not "
            f"replaced — got {logged_text!r}"
        )

    def test_kite_login_cooling_off_is_still_a_runtime_error_subclass(self):
        """Any existing broad `except RuntimeError`/`except Exception`
        caller elsewhere in the codebase must keep working unchanged."""
        from backend.brokers.errors import KiteLoginCoolingOff
        assert issubclass(KiteLoginCoolingOff, RuntimeError)

    def test_get_kite_conn_cooloff_gate_raises_kite_login_cooling_off(self):
        """The real producer — get_kite_conn()'s cooloff gate — must
        raise the dedicated type, not a bare RuntimeError."""
        import threading
        import time as _time_mod
        from backend.brokers.connections import KiteConnection
        from backend.brokers.errors import KiteLoginCoolingOff

        conn = KiteConnection.__new__(KiteConnection)
        conn.account = "ZG_BACKOFF_FAILFAST"
        conn._password = "pw"
        conn.api_key = "ak"
        conn._api_secret = "as"
        conn.totp_token = "tt"
        conn._source_ip = None
        conn.login_url = "http://example.invalid/login"
        conn.twofa_url = "http://example.invalid/twofa"
        conn._access_token = None
        conn._initialized = True
        conn._login_lock = threading.Lock()
        conn._conn_created_at = None
        conn._login_blocked_until = _time_mod.time() + 999
        conn._login_fail_streak = 0
        conn._blocked_alert_sent = False
        conn.session = MagicMock()
        conn.kite = MagicMock()
        conn.init_kite_conn = MagicMock(
            side_effect=AssertionError("must never attempt login while cooling off")
        )

        with pytest.raises(KiteLoginCoolingOff):
            conn.get_kite_conn(test_conn=True)
