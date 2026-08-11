"""Coverage tests for backend/brokers/connections.py.

Targets pure and near-pure paths that were not exercised by existing tests:

  CON-1  _safe_lock_name — colon / slash sanitization.
  CON-2  _load_cached_token / _save_cached_token — disk I/O via tmp file.
  CON-3  _record_session_ok shim — imports and calls without error.
  CON-4  _emit_conn_event shim — swallows ImportError silently.
  CON-5  KiteConnection._is_kite_conn_expired — pure datetime logic.
  CON-6  KiteConnection._validate_or_clear_kite_token — success + failure paths.
  CON-7  DhanConnection._is_token_expired / _check_recency_guard / _check_login_rate_limit.
  CON-8  DhanConnection.get_dhan_conn — cached-valid fast path.
  CON-9  GrowwConnection.get_groww_conn — None raises; real object returns.
  CON-10 Connections._compute_dhan_deferred_accounts — pure logic, no DB.
  CON-11 Connections._build_row_lookup_maps — pure dict-building.

Five quality dimensions:
  SSOT        — tests call the implementation function directly.
  Correctness — precise assertions on returned / mutated state.
  Performance — no real broker I/O, no DB calls, no asyncio.run().
  Reuse       — shared helpers; __new__ + attribute injection for __init__ bypass.
  UX          — every assert carries an f-string with the actual value.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tmp_token_cache() -> str:
    """Return a path to a fresh, empty temp file that can hold JSON tokens.
    Caller is responsible for removing it."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b"{}")
        return f.name


def _fake_secrets(account: str = "ZG_TEST") -> dict:
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
        "kite_login_url":  "http://example.com/login",
        "kite_twofa_url":  "http://example.com/twofa",
    }


def _kite_conn_bare(account: str = "ZG_TEST") -> "KiteConnection":
    """Construct a KiteConnection via __new__ without triggering __init__'s
    disk I/O or SDK calls.  Caller sets attributes manually."""
    from backend.brokers.connections import KiteConnection
    obj = KiteConnection.__new__(KiteConnection)
    obj.account           = account
    obj._password         = "pw"
    obj.api_key           = "ak"
    obj._api_secret       = "as"
    obj.totp_token        = "tt"
    obj._source_ip        = None
    obj.login_url         = "http://example.com/login"
    obj.twofa_url         = "http://example.com/twofa"
    obj._access_token     = None
    obj._initialized      = True
    obj._login_lock       = threading.Lock()
    obj._conn_created_at  = None
    obj.session           = MagicMock()
    obj.kite              = MagicMock()
    return obj


def _dhan_conn_bare(account: str = "DH_TEST") -> "DhanConnection":
    """Construct a DhanConnection via __new__ without running __init__."""
    from backend.brokers.connections import DhanConnection
    obj = DhanConnection.__new__(DhanConnection)
    obj.account             = account
    obj.client_id           = "CL123"
    obj._api_key            = "ak"
    obj._api_secret         = "as"
    obj._pin                = "1234"
    obj._totp_token         = "JBSWY3DPEHPK3PXP"
    obj._source_ip          = None
    obj._access_token       = None
    obj._conn_created_at    = None
    obj._dhan               = None
    obj._import_error       = None
    obj._login_lock         = threading.Lock()
    obj._login_blocked_until = 0.0
    return obj


def _fake_dhan_rows(*account_ip_priority: tuple) -> list:
    """Build list of SimpleNamespace broker_account rows for Dhan tests.

    Each tuple = (account, source_ip, priority).
    """
    rows = []
    for account, source_ip, priority in account_ip_priority:
        rows.append(SimpleNamespace(
            account=account,
            broker_id="dhan",
            source_ip=source_ip,
            priority=priority,
        ))
    return rows


# ---------------------------------------------------------------------------
# CON-1: _safe_lock_name
# ---------------------------------------------------------------------------

class TestSafeLockName:
    """Colons and slashes in account names are replaced with underscores."""

    def test_colon_replaced(self):
        from backend.brokers.connections import _safe_lock_name
        result = _safe_lock_name("dhan:DH6847")
        assert result == "dhan_DH6847", (
            f"Expected 'dhan_DH6847', got {result!r}"
        )

    def test_slash_replaced(self):
        from backend.brokers.connections import _safe_lock_name
        result = _safe_lock_name("acc/sub")
        assert result == "acc_sub", (
            f"Expected 'acc_sub', got {result!r}"
        )

    def test_plain_name_unchanged(self):
        from backend.brokers.connections import _safe_lock_name
        result = _safe_lock_name("ZG0790")
        assert result == "ZG0790", (
            f"Expected 'ZG0790' unchanged, got {result!r}"
        )

    def test_colon_and_slash_combined(self):
        from backend.brokers.connections import _safe_lock_name
        result = _safe_lock_name("dhan:DH/6847")
        assert result == "dhan_DH_6847", (
            f"Expected 'dhan_DH_6847', got {result!r}"
        )


# ---------------------------------------------------------------------------
# CON-2: _load_cached_token / _save_cached_token
# ---------------------------------------------------------------------------

class TestTokenCache:
    """Token cache disk I/O — uses a tmp file to avoid touching prod paths."""

    @pytest.fixture(autouse=True)
    def tmp_cache(self, monkeypatch, tmp_path):
        cache_file = tmp_path / "kite_tokens.json"
        cache_file.write_text("{}")
        from pathlib import Path
        monkeypatch.setattr(
            "backend.brokers.connections._TOKEN_CACHE_PATH",
            cache_file,
        )
        self.cache_path = cache_file
        yield

    def test_save_then_load_returns_token(self):
        from backend.brokers.connections import _save_cached_token, _load_cached_token

        _save_cached_token("ZG0790", "tok_abc123")
        token, created = _load_cached_token("ZG0790")
        assert token == "tok_abc123", (
            f"Expected 'tok_abc123', got {token!r}"
        )
        assert created is not None, "created_at must be set after save"

    def test_unknown_account_returns_none(self):
        from backend.brokers.connections import _load_cached_token

        token, created = _load_cached_token("UNKNOWN_ACCT_XYZ")
        assert token is None, f"Expected None for unknown account, got {token!r}"
        assert created is None, f"Expected None created_at, got {created!r}"

    def test_delete_token_with_empty_string(self):
        from backend.brokers.connections import _save_cached_token, _load_cached_token

        _save_cached_token("ZG0790", "tok_abc123")
        _save_cached_token("ZG0790", "")  # remove it

        token, _ = _load_cached_token("ZG0790")
        assert token is None, (
            f"Token should be deleted after saving empty string, got {token!r}"
        )

    def test_multiple_accounts_independent(self):
        from backend.brokers.connections import _save_cached_token, _load_cached_token

        _save_cached_token("ZG0790", "tok_A")
        _save_cached_token("ZJ6294", "tok_B")

        tok_a, _ = _load_cached_token("ZG0790")
        tok_b, _ = _load_cached_token("ZJ6294")
        assert tok_a == "tok_A", f"ZG0790 expected 'tok_A', got {tok_a!r}"
        assert tok_b == "tok_B", f"ZJ6294 expected 'tok_B', got {tok_b!r}"

    def test_expired_token_returns_none(self):
        """Token older than CONN_RESET_HOURS must not be returned."""
        from backend.brokers.connections import _load_cached_token, CONN_RESET_HOURS

        # Write a token with a created_at in the distant past.
        past = datetime.now(timezone.utc) - timedelta(hours=CONN_RESET_HOURS + 2)
        data = {
            "ZG0790": {
                "access_token": "old_tok",
                "created_at":   past.isoformat(),
            }
        }
        self.cache_path.write_text(json.dumps(data))

        token, _ = _load_cached_token("ZG0790")
        assert token is None, (
            f"Expired token (>{CONN_RESET_HOURS}h old) must return None, got {token!r}"
        )

    def test_save_overwrites_previous_entry(self):
        from backend.brokers.connections import _save_cached_token, _load_cached_token

        _save_cached_token("ZG0790", "first_token")
        _save_cached_token("ZG0790", "second_token")

        token, _ = _load_cached_token("ZG0790")
        assert token == "second_token", (
            f"Second save should overwrite first; got {token!r}"
        )


# ---------------------------------------------------------------------------
# CON-3: _record_session_ok shim
# ---------------------------------------------------------------------------

class TestRecordSessionOkShim:
    """_record_session_ok() must import and call broker_apis.record_session_ok."""

    def test_shim_calls_record_session_ok(self):
        from backend.brokers.connections import _record_session_ok

        with patch("backend.brokers.broker_apis.record_session_ok") as mock_rso:
            _record_session_ok("ZG0790")

        mock_rso.assert_called_once_with("ZG0790"), (
            f"record_session_ok not called with expected account; "
            f"calls: {mock_rso.call_args_list}"
        )

    def test_shim_swallows_import_error(self):
        """If broker_apis is not importable, _record_session_ok must not raise."""
        from backend.brokers.connections import _record_session_ok

        with patch.dict("sys.modules", {"backend.brokers.broker_apis": None}):
            # Must complete without exception even when the lazy import fails.
            _record_session_ok("ZG0790")


# ---------------------------------------------------------------------------
# CON-4: _emit_conn_event shim (connections.py module-level)
# ---------------------------------------------------------------------------

class TestEmitConnEventShim:
    """_emit_conn_event() at module level must swallow failures silently."""

    def test_shim_swallows_import_error(self):
        from backend.brokers import connections

        with patch.dict("sys.modules", {"backend.brokers.service.conn_events": None}):
            # Must not raise even if conn_events can't be imported.
            connections._emit_conn_event("ZG0790", "zerodha_kite", "token_ok")

    def test_shim_forwards_event_when_importable(self):
        from backend.brokers import connections

        fake_fire = MagicMock()
        fake_module = MagicMock()
        fake_module._emit_conn_event = fake_fire

        with patch.dict("sys.modules", {"backend.brokers.service.conn_events": fake_module}):
            connections._emit_conn_event("ZG0790", "zerodha_kite", "token_ok", {"k": "v"})

        fake_fire.assert_called_once_with(
            "ZG0790", "zerodha_kite", "token_ok", {"k": "v"}
        ), f"Expected forwarded call, got {fake_fire.call_args_list}"


# ---------------------------------------------------------------------------
# CON-5: KiteConnection._is_kite_conn_expired
# ---------------------------------------------------------------------------

class TestKiteConnExpiry:
    """Pure datetime logic — no broker SDK calls needed."""

    def test_none_created_at_is_expired(self):
        conn = _kite_conn_bare()
        conn._conn_created_at = None
        now = datetime.now(timezone.utc)
        assert conn._is_kite_conn_expired(now) is True, (
            "Connection with _conn_created_at=None must be considered expired"
        )

    def test_fresh_connection_is_not_expired(self):
        from backend.brokers.connections import CONN_RESET_HOURS
        conn = _kite_conn_bare()
        conn._conn_created_at = datetime.now(timezone.utc) - timedelta(hours=1)
        now = datetime.now(timezone.utc)
        assert conn._is_kite_conn_expired(now) is False, (
            f"Connection 1h old should not be expired when CONN_RESET_HOURS={CONN_RESET_HOURS}"
        )

    def test_old_connection_is_expired(self):
        from backend.brokers.connections import CONN_RESET_HOURS
        conn = _kite_conn_bare()
        conn._conn_created_at = datetime.now(timezone.utc) - timedelta(
            hours=CONN_RESET_HOURS + 1
        )
        now = datetime.now(timezone.utc)
        assert conn._is_kite_conn_expired(now) is True, (
            f"Connection >{CONN_RESET_HOURS}h old must be expired"
        )


# ---------------------------------------------------------------------------
# CON-6: KiteConnection._validate_or_clear_kite_token
# ---------------------------------------------------------------------------

class TestKiteValidateOrClearToken:
    """_validate_or_clear_kite_token success + failure paths."""

    @pytest.fixture(autouse=True)
    def tmp_cache(self, monkeypatch, tmp_path):
        cache_file = tmp_path / "kite_tokens.json"
        cache_file.write_text("{}")
        monkeypatch.setattr(
            "backend.brokers.connections._TOKEN_CACHE_PATH",
            cache_file,
        )

    def test_valid_token_returns_true(self):
        conn = _kite_conn_bare()
        conn._access_token = "valid_token"
        conn.kite.profile = MagicMock(return_value={"name": "Test"})

        result = conn._validate_or_clear_kite_token()
        assert result is True, (
            f"Valid token path must return True, got {result!r}"
        )

    def test_invalid_token_clears_and_returns_false(self):
        conn = _kite_conn_bare()
        conn._access_token = "stale_token"
        conn.kite.profile = MagicMock(side_effect=Exception("Invalid token"))

        result = conn._validate_or_clear_kite_token()
        assert result is False, (
            f"Invalid token path must return False, got {result!r}"
        )
        assert conn._access_token is None, (
            f"_access_token must be cleared on invalid token, got {conn._access_token!r}"
        )

    def test_no_token_attempts_restore_then_returns_false(self, monkeypatch):
        """When _access_token is None and cache is empty, must return False."""
        conn = _kite_conn_bare()
        conn._access_token = None
        # _try_restore_token will find nothing in the empty cache.
        monkeypatch.setattr(conn, "_try_restore_token", MagicMock())

        result = conn._validate_or_clear_kite_token()
        assert result is False, (
            f"No token + empty cache must return False, got {result!r}"
        )


# ---------------------------------------------------------------------------
# CON-7: DhanConnection._is_token_expired / _check_recency_guard /
#         _check_login_rate_limit
# ---------------------------------------------------------------------------

class TestDhanConnPureMethods:
    """Pure state checks on DhanConnection — no network calls."""

    def test_is_token_expired_none_created(self):
        conn = _dhan_conn_bare()
        conn._conn_created_at = None
        now = datetime.now(timezone.utc)
        assert conn._is_token_expired(now) is True, (
            "_conn_created_at=None must be expired"
        )

    def test_is_token_expired_fresh(self):
        from backend.brokers.connections import CONN_RESET_HOURS
        conn = _dhan_conn_bare()
        conn._conn_created_at = datetime.now(timezone.utc) - timedelta(hours=1)
        now = datetime.now(timezone.utc)
        assert conn._is_token_expired(now) is False, (
            f"1-hour-old token must not be expired with CONN_RESET_HOURS={CONN_RESET_HOURS}"
        )

    def test_is_token_expired_old(self):
        from backend.brokers.connections import CONN_RESET_HOURS
        conn = _dhan_conn_bare()
        conn._conn_created_at = datetime.now(timezone.utc) - timedelta(
            hours=CONN_RESET_HOURS + 2
        )
        now = datetime.now(timezone.utc)
        assert conn._is_token_expired(now) is True, (
            f"Token >{CONN_RESET_HOURS}h old must be expired"
        )

    def test_recency_guard_false_when_no_token(self):
        conn = _dhan_conn_bare()
        conn._access_token    = None
        conn._conn_created_at = datetime.now(timezone.utc)
        conn._dhan            = MagicMock()
        now = datetime.now(timezone.utc)
        assert conn._check_recency_guard(now, test_conn=True) is False, (
            "No access_token → recency guard must return False"
        )

    def test_recency_guard_true_when_recently_minted(self):
        conn = _dhan_conn_bare()
        conn._access_token    = "tok"
        conn._conn_created_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        conn._dhan            = MagicMock()
        now = datetime.now(timezone.utc)
        assert conn._check_recency_guard(now, test_conn=True) is True, (
            "Token minted 10s ago with test_conn=True must trigger recency guard"
        )

    def test_recency_guard_false_when_old_token(self):
        conn = _dhan_conn_bare()
        conn._access_token    = "tok"
        conn._conn_created_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        conn._dhan            = MagicMock()
        now = datetime.now(timezone.utc)
        assert conn._check_recency_guard(now, test_conn=True) is False, (
            "Token minted 120s ago must NOT trigger 60s recency guard"
        )

    def test_login_rate_limit_not_in_cooloff(self):
        conn = _dhan_conn_bare()
        conn._login_blocked_until = 0.0  # not in cooloff
        result = conn._check_login_rate_limit(test_conn=False)
        assert result is None, (
            f"Not in cooloff → must return None, got {result!r}"
        )

    def test_login_rate_limit_in_cooloff_with_client(self):
        conn = _dhan_conn_bare()
        conn._login_blocked_until = time.time() + 120.0
        conn._dhan = MagicMock()
        # When in cooloff but _dhan available and test_conn=False, returns _dhan.
        result = conn._check_login_rate_limit(test_conn=False)
        assert result is conn._dhan, (
            f"In cooloff with cached client must return _dhan; got {result!r}"
        )

    def test_login_rate_limit_in_cooloff_test_conn_raises(self):
        conn = _dhan_conn_bare()
        conn._login_blocked_until = time.time() + 120.0
        conn._dhan = MagicMock()
        with pytest.raises(RuntimeError, match="rate-limited"):
            conn._check_login_rate_limit(test_conn=True)

    def test_login_rate_limit_in_cooloff_no_client_raises(self):
        conn = _dhan_conn_bare()
        conn._login_blocked_until = time.time() + 120.0
        conn._dhan = None
        with pytest.raises(RuntimeError, match="rate-limited"):
            conn._check_login_rate_limit(test_conn=False)


# ---------------------------------------------------------------------------
# CON-8: DhanConnection.get_dhan_conn — cached-valid fast path
# ---------------------------------------------------------------------------

class TestDhanConnCachedPath:
    """When token is fresh and _dhan is set, get_dhan_conn() returns it
    immediately without acquiring the cross-process lock."""

    def test_cached_valid_returns_dhan_object(self):
        from backend.brokers.connections import CONN_RESET_HOURS

        conn = _dhan_conn_bare()
        fake_client = MagicMock()
        conn._dhan            = fake_client
        conn._access_token    = "valid_tok"
        conn._conn_created_at = datetime.now(timezone.utc) - timedelta(hours=1)

        result = conn.get_dhan_conn(test_conn=False)
        assert result is fake_client, (
            f"Cached valid conn must return _dhan directly; got {result!r}"
        )


# ---------------------------------------------------------------------------
# CON-9: GrowwConnection.get_groww_conn
# ---------------------------------------------------------------------------

class TestGrowwConnGetConn:
    """GrowwConnection.get_groww_conn() raises when _groww is None; returns
    the client when set."""

    def _groww_bare(self, account: str = "GR_TEST") -> "GrowwConnection":
        from backend.brokers.connections import GrowwConnection
        obj = GrowwConnection.__new__(GrowwConnection)
        obj.account       = account
        obj._api_key      = ""
        obj._totp_seed    = ""
        obj._access_token = ""
        obj._source_ip    = None
        obj._groww        = None
        obj._import_error = None
        obj._login_lock   = threading.Lock()
        # New fields added by the token-expiry + rate-limit hardening:
        obj._conn_created_at    = 0.0   # 0.0 → _is_token_expired() returns False (not-yet-built)
        obj._login_blocked_until = 0.0  # 0.0 → not in cooloff
        return obj

    def test_raises_when_groww_is_none(self):
        conn = self._groww_bare()
        conn._groww = None
        with pytest.raises(RuntimeError, match="not initialised"):
            conn.get_groww_conn()

    def test_returns_groww_when_set(self):
        conn = self._groww_bare()
        fake_groww = MagicMock()
        conn._groww = fake_groww
        result = conn.get_groww_conn()
        assert result is fake_groww, (
            f"get_groww_conn must return the _groww client, got {result!r}"
        )


# ---------------------------------------------------------------------------
# CON-10: Connections._compute_dhan_deferred_accounts
# ---------------------------------------------------------------------------

class TestDhanDeferredAccounts:
    """Pure logic — no DB, no SDK. Uses SimpleNamespace rows."""

    def test_single_dhan_per_ip_not_deferred(self):
        from backend.brokers.connections import Connections

        rows = _fake_dhan_rows(("DH6847", "2a02::1", 10))
        deferred = Connections._compute_dhan_deferred_accounts(rows)
        assert deferred == set(), (
            f"Single Dhan account per IP should not be deferred; got {deferred}"
        )

    def test_two_dhan_same_ip_lower_priority_deferred(self):
        from backend.brokers.connections import Connections

        rows = _fake_dhan_rows(
            ("DH6847", "2a02::1", 5),   # lower number = higher priority → kept
            ("DH3747", "2a02::1", 10),  # higher number → deferred
        )
        deferred = Connections._compute_dhan_deferred_accounts(rows)
        assert "DH3747" in deferred, (
            f"DH3747 (priority=10) should be deferred; got {deferred}"
        )
        assert "DH6847" not in deferred, (
            f"DH6847 (priority=5) should be kept; got {deferred}"
        )

    def test_two_dhan_different_ips_neither_deferred(self):
        from backend.brokers.connections import Connections

        rows = _fake_dhan_rows(
            ("DH6847", "2a02::1", 5),
            ("DH3747", "2a02::2", 5),
        )
        deferred = Connections._compute_dhan_deferred_accounts(rows)
        assert deferred == set(), (
            f"Two Dhan accounts on different IPs should not be deferred; got {deferred}"
        )

    def test_non_dhan_rows_ignored(self):
        from backend.brokers.connections import Connections

        rows = [
            SimpleNamespace(account="ZG0790", broker_id="zerodha_kite", source_ip=None, priority=1),
            SimpleNamespace(account="GR87DF", broker_id="groww",         source_ip=None, priority=1),
        ]
        deferred = Connections._compute_dhan_deferred_accounts(rows)
        assert deferred == set(), (
            f"Non-Dhan rows must never be deferred; got {deferred}"
        )

    def test_three_dhan_same_ip_two_deferred(self):
        from backend.brokers.connections import Connections

        rows = _fake_dhan_rows(
            ("DH1111", "2a02::1", 1),   # kept
            ("DH2222", "2a02::1", 2),   # deferred
            ("DH3333", "2a02::1", 3),   # deferred
        )
        deferred = Connections._compute_dhan_deferred_accounts(rows)
        assert "DH1111" not in deferred, (
            f"DH1111 (priority=1) must be kept; got {deferred}"
        )
        assert "DH2222" in deferred, (
            f"DH2222 must be deferred; got {deferred}"
        )
        assert "DH3333" in deferred, (
            f"DH3333 must be deferred; got {deferred}"
        )

    def test_effective_ips_override_db_source_ip(self):
        """secrets.yaml IP overlay changes grouping — two rows that share DB
        source_ip but have different effective_ips should NOT be co-grouped."""
        from backend.brokers.connections import Connections

        rows = _fake_dhan_rows(
            ("DH6847", "2a02::1", 5),
            ("DH3747", "2a02::1", 10),  # same DB IP but secrets.yaml gives it ::2
        )
        effective_ips = {
            "DH6847": "2a02::1",
            "DH3747": "2a02::2",   # overlay gives it a unique IP
        }
        deferred = Connections._compute_dhan_deferred_accounts(rows, effective_ips)
        assert deferred == set(), (
            f"Different effective IPs must not cause deferral; got {deferred}"
        )


# ---------------------------------------------------------------------------
# CON-11: Connections._build_row_lookup_maps
# ---------------------------------------------------------------------------

class TestBuildRowLookupMaps:
    """Pure dict-building from row objects — no DB or SDK."""

    def _make_row(self, account, broker_id="zerodha_kite", priority=100,
                  historical_data_enabled=True):
        return SimpleNamespace(
            account=account,
            broker_id=broker_id,
            priority=priority,
            historical_data_enabled=historical_data_enabled,
        )

    def test_all_three_maps_populated(self):
        from backend.brokers.connections import Connections

        rows = [
            self._make_row("ZG0790", "zerodha_kite", 10, True),
            self._make_row("DH6847", "dhan",         20, False),
        ]
        new_conn = {"ZG0790": MagicMock(), "DH6847": MagicMock()}

        bid_map, pri_map, hist_map = Connections._build_row_lookup_maps(rows, new_conn)

        assert bid_map["ZG0790"] == "zerodha_kite", (
            f"ZG0790 broker_id expected 'zerodha_kite', got {bid_map.get('ZG0790')!r}"
        )
        assert bid_map["DH6847"] == "dhan", (
            f"DH6847 broker_id expected 'dhan', got {bid_map.get('DH6847')!r}"
        )
        assert pri_map["ZG0790"] == 10, (
            f"ZG0790 priority expected 10, got {pri_map.get('ZG0790')!r}"
        )
        assert pri_map["DH6847"] == 20, (
            f"DH6847 priority expected 20, got {pri_map.get('DH6847')!r}"
        )
        assert hist_map["ZG0790"] is True, (
            f"ZG0790 hist_enabled expected True, got {hist_map.get('ZG0790')!r}"
        )
        assert hist_map["DH6847"] is False, (
            f"DH6847 hist_enabled expected False, got {hist_map.get('DH6847')!r}"
        )

    def test_rows_absent_from_new_conn_excluded(self):
        """Rows whose account is not in new_conn must be excluded."""
        from backend.brokers.connections import Connections

        rows = [
            self._make_row("ZG0790", "zerodha_kite", 10),
            self._make_row("DH6847", "dhan",         20),
        ]
        new_conn = {"ZG0790": MagicMock()}  # DH6847 NOT in map

        bid_map, pri_map, hist_map = Connections._build_row_lookup_maps(rows, new_conn)

        assert "DH6847" not in bid_map, (
            f"DH6847 not in new_conn should be excluded from broker_id_map; "
            f"got {bid_map!r}"
        )
        assert "ZG0790" in bid_map, "ZG0790 must be present"

    def test_none_broker_id_defaults_to_zerodha_kite(self):
        from backend.brokers.connections import Connections

        rows = [self._make_row("ZG0790", broker_id=None)]
        new_conn = {"ZG0790": MagicMock()}

        bid_map, _, _ = Connections._build_row_lookup_maps(rows, new_conn)
        assert bid_map["ZG0790"] == "zerodha_kite", (
            f"None broker_id must default to 'zerodha_kite', got {bid_map.get('ZG0790')!r}"
        )
