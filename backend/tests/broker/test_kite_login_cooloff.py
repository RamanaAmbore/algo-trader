"""
Tests for KiteConnection's cross-call login cool-off (the circuit breaker
fix for the 2026-11-19 live incident: account ZG0790's automated Kite
REST login hammered https://kite.zerodha.com/api/login with 800+
consecutive `400 Client Error: Bad Request` failures, with zero
cross-call memory of the prior failure — `get_kite_conn()` re-attempted
a full login() + totp_authenticate() + setup_access_token() flow on
every single broker-data fetch, for every account, forever).

Mirrors DhanConnection's own `_login_blocked_until` / cool-off-file
mechanism (see test_dhan_token_renewal.py, test_connections_coverage.py's
`TestDhanConnPureMethods` for the Dhan-side precedent), with one
deliberate difference: Kite's escalation is exponential across
consecutive failures (120s, 240s, 480s, 960s, capped at 1800s) rather
than Dhan's flat 120s — see `KiteConnection._attempt_login_with_cooloff`'s
own docstring for why.

The `_isolate_login_cooloff_paths` autouse fixture in this directory's
conftest.py redirects both brokers' on-disk cool-off files to a per-test
tmp_path, so these tests never touch the real, host-wide
`/tmp/ramboq_kite_login_cooloff.json`.

Five quality dimensions (matches test_connections_coverage.py's own
convention):
  SSOT        — tests call KiteConnection's real methods directly, no
                reimplementation of the cool-off logic.
  Correctness — precise assertions on streak / blocked_until / disk state.
  Performance — time.sleep is mocked out; no real network, no real sleep.
  Reuse       — shared `_fake_secrets` / `_kite_conn_bare` helper pattern,
                mirroring test_connections_coverage.py.
  UX          — every assert carries an f-string with the actual value.
"""
from __future__ import annotations

import json
import os
import threading
import time
from unittest.mock import MagicMock

import pytest

from backend.brokers.connections import KiteConnection
from backend.shared.helpers.date_time_utils import timestamp_indian


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_secrets(account: str) -> dict:
    """Minimal secrets dict accepted by KiteConnection.__init__."""
    return {
        "kite_accounts": {
            account: {
                "password":   "testpass",
                "api_key":    "test_api_key",
                "api_secret": "test_api_secret",
                "totp_token": "JBSWY3DPEHPK3PXP",
                "source_ip":  None,
            }
        },
        "kite_login_url":  "http://example.invalid/login",
        "kite_twofa_url":  "http://example.invalid/twofa",
    }


def _kite_conn_bare(account: str = "ZG_COOLOFF_BARE") -> KiteConnection:
    """Construct a KiteConnection via __new__ without triggering __init__'s
    disk I/O or SDK calls — same convention as
    test_connections_coverage.py's `_kite_conn_bare`."""
    obj = KiteConnection.__new__(KiteConnection)
    obj.account           = account
    obj._password         = "pw"
    obj.api_key           = "ak"
    obj._api_secret       = "as"
    obj.totp_token        = "tt"
    obj._source_ip        = None
    obj.login_url         = "http://example.invalid/login"
    obj.twofa_url         = "http://example.invalid/twofa"
    obj._access_token     = None
    obj._initialized      = True
    obj._login_lock       = threading.Lock()
    obj._conn_created_at  = None
    obj._login_blocked_until = 0.0
    obj._login_fail_streak   = 0
    obj._blocked_alert_sent  = False
    obj.session           = MagicMock()
    obj.kite              = MagicMock()
    return obj


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Every test in this file calls a @retry_kite_conn-decorated method at
    least indirectly. Without this, a failing login would burn real
    wall-clock seconds on the decorator's own exponential backoff between
    its internal retries."""
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# A: a login failure sets streak=1 and an ~120s cool-off
# ---------------------------------------------------------------------------

class TestLoginFailureSetsCooloff:
    def test_first_failure_sets_streak_one_and_120s_window(self):
        conn = _kite_conn_bare("ZG_COOLOFF_A1")
        conn.init_kite_conn = MagicMock(
            side_effect=RuntimeError("400 Client Error: Bad Request")
        )
        before = time.time()

        with pytest.raises(RuntimeError, match="Bad Request"):
            conn._attempt_login_with_cooloff()

        assert conn._login_fail_streak == 1, (
            f"expected fail streak 1 after first failure, got "
            f"{conn._login_fail_streak}"
        )
        cooloff_s = conn._login_blocked_until - before
        assert 115 <= cooloff_s <= 125, (
            f"expected ~120s cool-off on first failure, got {cooloff_s:.1f}s "
            f"(blocked_until={conn._login_blocked_until}, before={before})"
        )

    def test_failure_does_not_add_a_second_emit_conn_event(self, monkeypatch):
        """login()/totp_authenticate()/setup_access_token() already each
        call _emit_conn_event on their own failure — _attempt_login_with_
        cooloff must not add a second one (it only tracks cool-off state)."""
        conn = _kite_conn_bare("ZG_COOLOFF_A2")
        conn.init_kite_conn = MagicMock(side_effect=RuntimeError("boom"))
        emit_mock = MagicMock()
        monkeypatch.setattr(
            "backend.brokers.connections._emit_conn_event", emit_mock
        )

        with pytest.raises(RuntimeError):
            conn._attempt_login_with_cooloff()

        emit_mock.assert_not_called()


# ---------------------------------------------------------------------------
# B: a second consecutive failure escalates (240s, not 120s)
# ---------------------------------------------------------------------------

class TestConsecutiveFailureEscalates:
    def test_second_failure_escalates_to_240s(self):
        conn = _kite_conn_bare("ZG_COOLOFF_B1")
        conn.init_kite_conn = MagicMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            conn._attempt_login_with_cooloff()
        assert conn._login_fail_streak == 1, (
            f"expected streak 1 after first failure, got {conn._login_fail_streak}"
        )

        # Clear the gate manually so the second attempt actually reaches
        # init_kite_conn again (mirrors a real caller retrying once the
        # first cool-off window has elapsed).
        conn._login_blocked_until = 0.0
        before2 = time.time()
        with pytest.raises(RuntimeError):
            conn._attempt_login_with_cooloff()

        assert conn._login_fail_streak == 2, (
            f"expected streak 2 after second consecutive failure, got "
            f"{conn._login_fail_streak}"
        )
        cooloff_s = conn._login_blocked_until - before2
        assert 235 <= cooloff_s <= 245, (
            f"expected ~240s cool-off on second consecutive failure "
            f"(not 120s — escalation must double), got {cooloff_s:.1f}s"
        )

    def test_escalation_caps_at_1800s(self):
        conn = _kite_conn_bare("ZG_COOLOFF_B2")
        conn.init_kite_conn = MagicMock(side_effect=RuntimeError("boom"))
        conn._login_fail_streak = 10  # way past the cap

        before = time.time()
        with pytest.raises(RuntimeError):
            conn._attempt_login_with_cooloff()

        cooloff_s = conn._login_blocked_until - before
        assert 1795 <= cooloff_s <= 1805, (
            f"expected cool-off capped at 1800s even at streak=11, got "
            f"{cooloff_s:.1f}s"
        )


# ---------------------------------------------------------------------------
# C: get_kite_conn raises immediately during cool-off — THE actual fix.
#    No call to init_kite_conn (and so no network call) must occur.
# ---------------------------------------------------------------------------

class TestCooloffGateBlocksLoginDuringWindow:
    def test_get_kite_conn_raises_without_calling_init_kite_conn(self):
        conn = _kite_conn_bare("ZG_COOLOFF_C1")
        conn._access_token = None  # no cached token to validate
        conn._login_blocked_until = time.time() + 999  # active, long window
        conn.init_kite_conn = MagicMock(
            side_effect=AssertionError(
                "init_kite_conn must never be called while in cool-off"
            )
        )

        with pytest.raises(RuntimeError, match="cooling off"):
            conn.get_kite_conn(test_conn=True)

        conn.init_kite_conn.assert_not_called()

    def test_repeated_calls_during_cooloff_never_touch_init_kite_conn(self):
        """The actual bug being fixed: before this change, EVERY caller of
        get_kite_conn(test_conn=True) during a failure burst re-attempted a
        full login — this proves a second (and third) external call during
        an active cool-off makes zero additional login attempts."""
        conn = _kite_conn_bare("ZG_COOLOFF_C2")
        conn._access_token = None
        conn._login_blocked_until = time.time() + 999
        conn.init_kite_conn = MagicMock()

        for _ in range(3):
            with pytest.raises(RuntimeError, match="cooling off"):
                conn.get_kite_conn(test_conn=True)

        conn.init_kite_conn.assert_not_called()


# ---------------------------------------------------------------------------
# C2 (2026-11-20 follow-up — the real mechanism behind the 436 failed
# quote/historical calls during the ZG0790 incident): a FAILED login must
# never advance `_conn_created_at`. Before this fix, `get_kite_conn()`
# stamped `_conn_created_at = now` BEFORE attempting re-auth, so a single
# failed login made the very next call (even test_conn=False) take the
# fast path at the top of the function and return a stale, tokenless
# `self.kite` with NO exception raised at all — silently bypassing the
# cool-off gate for the rest of CONN_RESET_HOURS.
# ---------------------------------------------------------------------------

class TestFailedLoginNeverAdvancesConnCreatedAt:
    def test_second_call_after_one_failure_still_hits_the_cooloff_gate(
        self, monkeypatch,
    ):
        """After ONE failed login, a second call to get_kite_conn() —
        even a default test_conn=False call, exactly what
        KiteBroker.kite's property makes — must still take the slow
        path and raise the cool-off RuntimeError, instead of silently
        returning a stale self.kite with no exception and no further
        protection."""
        # Force exactly one internal attempt per external get_kite_conn()
        # call, so "first call fails" / "second call" map 1:1 onto two
        # real external calls, matching the bug report's own framing.
        monkeypatch.setattr(
            "backend.shared.helpers.settings.get_int", lambda *a, **k: 1
        )

        conn = _kite_conn_bare("ZG_CONN_FRESH_1")
        conn._access_token = None            # no cached token to validate
        conn._conn_created_at = None         # connection starts expired
        conn.init_kite_conn = MagicMock(
            side_effect=RuntimeError("Invalid CAPTCHA values.")
        )

        # First call: a genuinely new login attempt that fails.
        with pytest.raises(RuntimeError, match="Invalid CAPTCHA"):
            conn.get_kite_conn(test_conn=True)

        assert conn._conn_created_at is None, (
            "a FAILED login must never advance _conn_created_at — got "
            f"{conn._conn_created_at!r}"
        )
        assert conn._login_blocked_until > time.time(), (
            "the failed attempt must have engaged the cool-off window"
        )

        # Second call — the real-world case: a default test_conn=False
        # call (what KiteBroker.kite's property makes). Must NOT take
        # the fast path and return a stale self.kite with no exception —
        # must still reach, and raise, the cool-off gate.
        conn.init_kite_conn.reset_mock()
        with pytest.raises(RuntimeError, match="cooling off"):
            conn.get_kite_conn(test_conn=False)

        conn.init_kite_conn.assert_not_called(), (
            "the cool-off gate must short-circuit before any further "
            "network call — init_kite_conn must not run a second time"
        )

    def test_successful_login_does_advance_conn_created_at(self, monkeypatch):
        """The inverse — a genuinely successful login MUST stamp
        _conn_created_at, so routine calls take the cheap fast path
        instead of re-validating every single time."""
        monkeypatch.setattr(
            "backend.shared.helpers.settings.get_int", lambda *a, **k: 1
        )

        conn = _kite_conn_bare("ZG_CONN_FRESH_2")
        conn._access_token = None
        conn._conn_created_at = None
        conn.init_kite_conn = MagicMock(return_value=None)  # succeeds

        result = conn.get_kite_conn(test_conn=True)

        assert result is conn.kite
        assert conn._conn_created_at is not None, (
            "a successful login must stamp _conn_created_at"
        )

    def test_valid_cached_token_also_advances_conn_created_at(self):
        """A still-valid cached token path (no login attempted at all)
        must also stamp _conn_created_at, so the next call takes the
        fast path instead of re-probing the token every time."""
        conn = _kite_conn_bare("ZG_CONN_FRESH_3")
        conn._access_token = "a-real-looking-token"
        conn._conn_created_at = None
        conn.kite.profile = MagicMock(return_value={"user_id": "ZG_CONN_FRESH_3"})
        conn.init_kite_conn = MagicMock(
            side_effect=AssertionError("must not attempt a fresh login")
        )

        result = conn.get_kite_conn(test_conn=True)

        assert result is conn.kite
        conn.init_kite_conn.assert_not_called()
        assert conn._conn_created_at is not None, (
            "a confirmed-valid cached token must also stamp _conn_created_at"
        )


# ---------------------------------------------------------------------------
# D: a still-valid cached token is returned normally, cool-off or not.
# ---------------------------------------------------------------------------

class TestCooloffNeverBlocksAValidCachedToken:
    def test_valid_token_returned_even_with_active_future_cooloff(self):
        conn = _kite_conn_bare("ZG_COOLOFF_D1")
        conn._access_token = "a-real-looking-token"
        conn.kite.profile = MagicMock(return_value={"user_id": "ZG_COOLOFF_D1"})
        # Simulate a cool-off set by an EARLIER, unrelated failure that is
        # still active right now.
        conn._login_blocked_until = time.time() + 500
        conn.init_kite_conn = MagicMock(
            side_effect=AssertionError(
                "init_kite_conn must never be called for a valid cached token"
            )
        )

        result = conn.get_kite_conn(test_conn=True)

        assert result is conn.kite, f"expected cached kite handle, got {result!r}"
        conn.init_kite_conn.assert_not_called()

    def test_valid_token_returned_when_a_prior_cooloff_has_since_expired(self):
        conn = _kite_conn_bare("ZG_COOLOFF_D2")
        conn._access_token = "a-real-looking-token"
        conn.kite.profile = MagicMock(return_value={"user_id": "ZG_COOLOFF_D2"})
        conn._login_blocked_until = time.time() - 50  # expired cool-off
        conn.init_kite_conn = MagicMock(
            side_effect=AssertionError(
                "init_kite_conn must never be called for a valid cached token"
            )
        )

        result = conn.get_kite_conn(test_conn=True)

        assert result is conn.kite, f"expected cached kite handle, got {result!r}"
        conn.init_kite_conn.assert_not_called()

    def test_fast_path_not_expired_not_test_conn_never_reaches_cooloff_gate(self):
        """The ordinary fast path (cached connection not yet expired,
        test_conn=False) must return immediately without ever evaluating
        the cool-off gate — same invariant, from the other direction."""
        conn = _kite_conn_bare("ZG_COOLOFF_D3")
        conn._conn_created_at = timestamp_indian()
        conn._login_blocked_until = time.time() + 999
        conn.init_kite_conn = MagicMock(
            side_effect=AssertionError("must not reach init_kite_conn on fast path")
        )

        result = conn.get_kite_conn(test_conn=False)

        assert result is conn.kite, f"expected fast-path kite handle, got {result!r}"
        conn.init_kite_conn.assert_not_called()


# ---------------------------------------------------------------------------
# E: a success resets the streak and clears the cool-off.
# ---------------------------------------------------------------------------

class TestSuccessResetsCooloff:
    def test_success_resets_streak_and_blocked_until(self):
        conn = _kite_conn_bare("ZG_COOLOFF_E1")
        conn._login_fail_streak = 3
        conn._login_blocked_until = time.time() + 600
        conn.init_kite_conn = MagicMock(return_value=None)  # succeeds

        conn._attempt_login_with_cooloff()

        assert conn._login_fail_streak == 0, (
            f"expected streak reset to 0 on success, got {conn._login_fail_streak}"
        )
        assert conn._login_blocked_until == 0.0, (
            f"expected blocked_until cleared to 0.0 on success, got "
            f"{conn._login_blocked_until}"
        )


# ---------------------------------------------------------------------------
# F: cool-off persists to disk and is restored on a fresh KiteConnection.
# ---------------------------------------------------------------------------

class TestCooloffPersistence:
    def test_failure_persists_cooloff_to_disk_file(self):
        conn = _kite_conn_bare("ZG_COOLOFF_F1")
        conn.init_kite_conn = MagicMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            conn._attempt_login_with_cooloff()

        # _KITE_LOGIN_COOLOFF_PATH is redirected to tmp_path by this
        # directory's autouse conftest fixture.
        from backend.brokers import connections as _conn_mod
        path = _conn_mod._KITE_LOGIN_COOLOFF_PATH
        assert os.path.exists(path), f"expected cool-off file at {path}"
        with open(path) as f:
            data = json.load(f)
        assert "ZG_COOLOFF_F1" in data, f"expected account key in {data!r}"
        assert abs(data["ZG_COOLOFF_F1"] - conn._login_blocked_until) < 1.0

    def test_restart_restores_active_cooloff_from_disk(self):
        account = "ZG_COOLOFF_F2"
        secrets = _fake_secrets(account)

        conn1 = KiteConnection(account, secrets)
        conn1.init_kite_conn = MagicMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            conn1._attempt_login_with_cooloff()
        persisted_until = conn1._login_blocked_until
        assert persisted_until > time.time(), (
            f"expected an active future cool-off, got {persisted_until} "
            f"vs now={time.time()}"
        )

        # Simulate a process restart: a brand-new KiteConnection instance
        # for the SAME account must restore the still-active cool-off from
        # disk, not start clean at 0.0.
        conn2 = KiteConnection(account, secrets)

        assert conn2._login_blocked_until >= persisted_until - 1.0, (
            f"expected restored cool-off ~{persisted_until}, got "
            f"{conn2._login_blocked_until}"
        )
        # And the restored block actually gates a real login attempt on
        # the new instance too.
        conn2._access_token = None
        conn2.init_kite_conn = MagicMock(
            side_effect=AssertionError("restored cool-off must still gate login")
        )
        with pytest.raises(RuntimeError, match="cooling off"):
            conn2.get_kite_conn(test_conn=True)
        conn2.init_kite_conn.assert_not_called()

    def test_expired_persisted_cooloff_is_not_restored(self):
        account = "ZG_COOLOFF_F3"
        secrets = _fake_secrets(account)
        from backend.brokers import connections as _conn_mod

        # Write an already-expired cool-off entry directly to the
        # (isolated) cool-off file, simulating a restart long after the
        # window elapsed.
        with open(_conn_mod._KITE_LOGIN_COOLOFF_PATH, "w") as f:
            json.dump({account: time.time() - 500}, f)

        conn = KiteConnection(account, secrets)

        assert conn._login_blocked_until == 0.0, (
            f"expected an expired persisted cool-off to be ignored, got "
            f"{conn._login_blocked_until}"
        )

    def test_success_clears_persisted_cooloff_entry(self):
        account = "ZG_COOLOFF_F4"
        secrets = _fake_secrets(account)
        from backend.brokers import connections as _conn_mod

        conn = KiteConnection(account, secrets)
        conn.init_kite_conn = MagicMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            conn._attempt_login_with_cooloff()

        with open(_conn_mod._KITE_LOGIN_COOLOFF_PATH) as f:
            data = json.load(f)
        assert account in data, f"expected persisted entry before success, got {data!r}"

        conn.init_kite_conn = MagicMock(return_value=None)  # now succeeds
        conn._attempt_login_with_cooloff()

        with open(_conn_mod._KITE_LOGIN_COOLOFF_PATH) as f:
            data_after = json.load(f)
        assert account not in data_after, (
            f"expected persisted entry cleared after success, got {data_after!r}"
        )


# ---------------------------------------------------------------------------
# F3 (2026-11-20 incident) — one escalation-tier alert, not per-retry spam.
# Once the cooloff escalates to its own worst tier (the 1800s cap), exactly
# ONE tagged `_DISPATCH_LOGGER` record must fire per "reached max cooloff"
# episode — not on every retry while still capped, and it must re-arm after
# a successful login.
# ---------------------------------------------------------------------------

class TestEscalationTierAlert:
    def test_alert_fires_once_when_cooloff_reaches_the_cap(self, monkeypatch):
        from backend.brokers import connections as _conn_mod

        conn = _kite_conn_bare("ZG_COOLOFF_G1")
        conn.init_kite_conn = MagicMock(
            side_effect=RuntimeError("Invalid CAPTCHA values.")
        )
        conn._login_fail_streak = 10  # already at/above the cap

        dispatch_mock = MagicMock()
        monkeypatch.setattr(_conn_mod._DISPATCH_LOGGER, "error", dispatch_mock)

        with pytest.raises(RuntimeError):
            conn._attempt_login_with_cooloff()

        dispatch_mock.assert_called_once()
        kwargs = dispatch_mock.call_args.kwargs
        extra = kwargs["extra"]
        assert extra["alert_event"] == "broker_login_blocked", (
            f"expected alert_event='broker_login_blocked', got {extra!r}"
        )
        assert extra["action_required"] is True, (
            f"expected action_required=True to force the ntfy/email gates, "
            f"got {extra!r}"
        )
        assert extra["source_type"] == "process"
        assert extra["source_name"] == "Broker login"
        assert "alert" in extra["tags"]
        assert "Invalid CAPTCHA values." in extra["error"], (
            f"expected the F2-captured real error text in the alert body, "
            f"got {extra!r}"
        )
        assert conn._blocked_alert_sent is True

        # Must not double-alert through ERROR_AGENT's blanket catch-all
        # (this is itself an ERROR-level, alert-shaped record).
        assert extra["self_alerted"] is True, (
            f"expected self_alerted=True so ERROR_AGENT doesn't also fire "
            f"on this record, got {extra!r}"
        )

        # The account code in `extra` must be masked — `_AccountMaskFilter`
        # only masks the rendered message text, never `extra` values.
        from backend.shared.helpers.utils import mask_account
        assert extra["account"] == mask_account("ZG_COOLOFF_G1"), (
            f"expected extra['account'] to be masked via mask_account(), "
            f"got {extra['account']!r}"
        )
        assert extra["account"] != "ZG_COOLOFF_G1", (
            "the raw unmasked account code must never reach extra['account']"
        )

    def test_alert_does_not_spam_on_every_retry_while_still_capped(self, monkeypatch):
        """A multi-hour outage, repeatedly retried while still at the
        capped tier, must fire this alert exactly once — not once per
        retry."""
        from backend.brokers import connections as _conn_mod

        conn = _kite_conn_bare("ZG_COOLOFF_G2")
        conn.init_kite_conn = MagicMock(side_effect=RuntimeError("boom"))
        conn._login_fail_streak = 10

        dispatch_mock = MagicMock()
        monkeypatch.setattr(_conn_mod._DISPATCH_LOGGER, "error", dispatch_mock)

        for _ in range(4):
            with pytest.raises(RuntimeError):
                conn._attempt_login_with_cooloff()

        dispatch_mock.assert_called_once()

    def test_alert_not_fired_below_the_cap(self, monkeypatch):
        """A fresh (streak=1, ~120s) failure must NOT fire the escalation
        alert — only the worst tier (1800s cap) does."""
        from backend.brokers import connections as _conn_mod

        conn = _kite_conn_bare("ZG_COOLOFF_G3")
        conn.init_kite_conn = MagicMock(side_effect=RuntimeError("boom"))

        dispatch_mock = MagicMock()
        monkeypatch.setattr(_conn_mod._DISPATCH_LOGGER, "error", dispatch_mock)

        with pytest.raises(RuntimeError):
            conn._attempt_login_with_cooloff()

        dispatch_mock.assert_not_called()
        assert conn._blocked_alert_sent is False

    def test_alert_rearms_after_a_successful_login(self, monkeypatch):
        """Once the streak resets to 0 on a successful login, the next
        time the cooloff escalates back to the cap must fire a fresh
        alert — `_blocked_alert_sent` must not stay stuck True forever."""
        from backend.brokers import connections as _conn_mod

        conn = _kite_conn_bare("ZG_COOLOFF_G4")
        conn.init_kite_conn = MagicMock(side_effect=RuntimeError("boom"))
        conn._login_fail_streak = 10

        dispatch_mock = MagicMock()
        monkeypatch.setattr(_conn_mod._DISPATCH_LOGGER, "error", dispatch_mock)

        with pytest.raises(RuntimeError):
            conn._attempt_login_with_cooloff()
        dispatch_mock.assert_called_once()
        assert conn._blocked_alert_sent is True

        # A successful login clears both the streak and the alert flag.
        conn.init_kite_conn = MagicMock(return_value=None)
        conn._attempt_login_with_cooloff()
        assert conn._blocked_alert_sent is False, (
            "a successful login must reset _blocked_alert_sent so the "
            "alert can re-fire for a future outage"
        )

        # A fresh outage that escalates back to the cap fires again.
        conn.init_kite_conn = MagicMock(side_effect=RuntimeError("boom again"))
        conn._login_fail_streak = 10
        with pytest.raises(RuntimeError):
            conn._attempt_login_with_cooloff()
        assert dispatch_mock.call_count == 2, (
            f"expected the alert to re-fire for a fresh outage after a "
            f"successful login in between, got {dispatch_mock.call_count} calls"
        )

    def test_alert_failure_is_swallowed_never_masks_the_real_exception(self, monkeypatch):
        """If logging the escalation alert itself raises, the original
        login failure must still propagate — never swallowed by an alert
        plumbing error."""
        from backend.brokers import connections as _conn_mod

        conn = _kite_conn_bare("ZG_COOLOFF_G5")
        conn.init_kite_conn = MagicMock(side_effect=RuntimeError("boom"))
        conn._login_fail_streak = 10

        monkeypatch.setattr(
            _conn_mod._DISPATCH_LOGGER, "error",
            MagicMock(side_effect=RuntimeError("logging backend down")),
        )

        with pytest.raises(RuntimeError, match="boom"):
            conn._attempt_login_with_cooloff()
