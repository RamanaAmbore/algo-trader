"""Tests for Issue 1 — cold-boot creates correct connection types per broker.

Verifies that _rebuild_from_yaml does NOT wrap non-Kite accounts in a
KiteConnection. This was the bug: every account in kite_accounts (regardless
of its 'broker' key) got a KiteConnection, causing Dhan/Groww to silently
fail on first call and zero out holdings in the TOTAL.

Five quality dimensions:
  SSOT        — call _rebuild_from_yaml directly via object.__new__ bypass.
  Correctness — assert KiteConnection for kite, absent-from-conn for dhan/groww.
  Performance — no real broker I/O, no DB calls, no asyncio.
  Reuse       — shared _make_connections_instance helper.
  UX          — every assert carries a descriptive message with the actual value.
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_connections_instance():
    """Return a bare Connections object with __init__ bypassed.

    SingletonBase.__new__ maintains a global instance registry; using
    object.__new__ on the Connections class directly avoids touching that
    registry and prevents cross-test interference.
    """
    from backend.brokers.connections import Connections
    instance = object.__new__(Connections)
    # Guard attribute so _rebuild_from_yaml doesn't see a half-baked state.
    instance._singleton_initialized = False
    return instance


def _fake_secrets(
    kite_account: str = "ZG_TEST",
    dhan_account: str = "DH_TEST",
    groww_account: str = "GW_TEST",
) -> dict:
    """Minimal secrets dict that matches what secrets.yaml would provide
    under the kite_accounts key for a mixed-broker deployment."""
    return {
        "kite_accounts": {
            kite_account: {
                "broker": "zerodha_kite",
                "api_key": "fake_kite_api_key",
                "api_secret": "fake_kite_api_secret",
                "password": "fake_password",
                "totp_token": "JBSWY3DPEHPK3PXP",  # valid base32
                "source_ip": None,
            },
            dhan_account: {
                "broker": "dhan",
                "api_key": "fake_dhan_api_key",
                "api_secret": "fake_dhan_secret",
                "password": "fake_dhan_pin",
                "totp_token": "JBSWY3DPEHPK3PXP",
                "source_ip": None,
            },
            groww_account: {
                "broker": "groww",
                "api_key": "fake_groww_api_key",
                "totp_token": "JBSWY3DPEHPK3PXP",
                "source_ip": None,
            },
        },
        "kite_login_url":  "https://kite.zerodha.com/connect/login",
        "kite_twofa_url":  "https://kite.zerodha.com/api/twofa",
    }


# ---------------------------------------------------------------------------
# COLD-1  KiteConnection only built for kite-typed accounts
# ---------------------------------------------------------------------------

class TestColdBootBrokerType:

    def _call_rebuild(self, fake_secrets_dict: dict):
        """Instantiate Connections via __new__, patch secrets, call _rebuild_from_yaml."""
        from backend.brokers.connections import Connections

        instance = _make_connections_instance()

        # Patch the module-level KiteConnection name so the dict comprehension in
        # _rebuild_from_yaml creates lightweight stubs instead of real connections.
        # Avoid patch.object(KiteConnection, "__new__") — it leaves __new__ in the
        # class __dict__ after restore, breaking object.__new__ arg checks in Python
        # 3.14 for tests that run after this one.
        with patch(
            "backend.brokers.connections.KiteConnection",
            side_effect=lambda account, secrets_data: MagicMock(account=account),
        ), patch(
            "backend.brokers.connections.secrets", fake_secrets_dict
        ):
            instance._rebuild_from_yaml()

        return instance

    def test_kite_account_gets_kite_connection(self):
        """A kite_accounts entry with broker=zerodha_kite must be in self.conn."""
        secrets_data = _fake_secrets()
        instance = self._call_rebuild(secrets_data)
        assert "ZG_TEST" in instance.conn, (
            f"Expected ZG_TEST in conn after cold-boot; conn={list(instance.conn.keys())}"
        )

    def test_dhan_account_absent_from_conn(self):
        """A kite_accounts entry with broker=dhan must NOT produce a connection
        in self.conn — DhanConnection cannot be built from kite_accounts YAML blobs
        (missing client_id / PIN / TOTP seed in the right fields)."""
        secrets_data = _fake_secrets()
        instance = self._call_rebuild(secrets_data)
        assert "DH_TEST" not in instance.conn, (
            f"DH_TEST must NOT be in conn on cold-boot; "
            f"found {type(instance.conn.get('DH_TEST')).__name__}"
        )

    def test_groww_account_absent_from_conn(self):
        """A kite_accounts entry with broker=groww must NOT produce a connection
        in self.conn."""
        secrets_data = _fake_secrets()
        instance = self._call_rebuild(secrets_data)
        assert "GW_TEST" not in instance.conn, (
            f"GW_TEST must NOT be in conn on cold-boot; "
            f"found {type(instance.conn.get('GW_TEST')).__name__}"
        )

    def test_broker_id_map_correct_for_dhan(self):
        """_broker_id_map must still carry 'dhan' for the Dhan account even
        though it has no entry in self.conn (so get_broker() resolves correctly
        when rebuild_from_db populates self.conn later)."""
        secrets_data = _fake_secrets()
        instance = self._call_rebuild(secrets_data)
        assert instance._broker_id_map.get("DH_TEST") == "dhan", (
            f"_broker_id_map['DH_TEST'] expected 'dhan', "
            f"got {instance._broker_id_map.get('DH_TEST')!r}"
        )

    def test_broker_id_map_correct_for_kite(self):
        """_broker_id_map must carry 'zerodha_kite' for the Kite account."""
        secrets_data = _fake_secrets()
        instance = self._call_rebuild(secrets_data)
        assert instance._broker_id_map.get("ZG_TEST") == "zerodha_kite", (
            f"_broker_id_map['ZG_TEST'] expected 'zerodha_kite', "
            f"got {instance._broker_id_map.get('ZG_TEST')!r}"
        )

    def test_broker_id_map_correct_for_groww(self):
        """_broker_id_map must carry 'groww' for the Groww account."""
        secrets_data = _fake_secrets()
        instance = self._call_rebuild(secrets_data)
        assert instance._broker_id_map.get("GW_TEST") == "groww", (
            f"_broker_id_map['GW_TEST'] expected 'groww', "
            f"got {instance._broker_id_map.get('GW_TEST')!r}"
        )

    def test_priority_map_includes_all_accounts(self):
        """_priority_map must include all accounts (Kite + Dhan + Groww) — they
        all need a default priority even if only Kite has a live conn object."""
        secrets_data = _fake_secrets()
        instance = self._call_rebuild(secrets_data)
        for acct in ("ZG_TEST", "DH_TEST", "GW_TEST"):
            assert acct in instance._priority_map, (
                f"Expected {acct} in _priority_map; "
                f"got {list(instance._priority_map.keys())}"
            )

    def test_hist_enabled_map_includes_all_accounts(self):
        """_hist_enabled_map must include all accounts."""
        secrets_data = _fake_secrets()
        instance = self._call_rebuild(secrets_data)
        for acct in ("ZG_TEST", "DH_TEST", "GW_TEST"):
            assert acct in instance._hist_enabled_map, (
                f"Expected {acct} in _hist_enabled_map; "
                f"got {list(instance._hist_enabled_map.keys())}"
            )

    def test_only_kite_accounts_in_conn(self):
        """self.conn must contain ONLY Kite-typed accounts after cold-boot."""
        secrets_data = _fake_secrets()
        instance = self._call_rebuild(secrets_data)
        non_kite_in_conn = [
            acct for acct in instance.conn
            if instance._broker_id_map.get(acct) not in ("zerodha_kite", "kite", "")
        ]
        assert not non_kite_in_conn, (
            f"Non-Kite accounts found in conn: {non_kite_in_conn}"
        )

    def test_broker_default_empty_treated_as_kite(self):
        """An account with no 'broker' key (defaults to '') is treated as Kite
        and gets a connection."""
        secrets_data = {
            "kite_accounts": {
                "ZG_NOTYPE": {
                    "api_key": "ak",
                    "api_secret": "as",
                    "password": "pw",
                    "totp_token": "JBSWY3DPEHPK3PXP",
                },
            },
            "kite_login_url": "https://kite.zerodha.com/connect/login",
            "kite_twofa_url": "https://kite.zerodha.com/api/twofa",
        }
        from backend.brokers.connections import Connections

        instance = _make_connections_instance()
        with patch(
            "backend.brokers.connections.KiteConnection",
            side_effect=lambda account, sd: MagicMock(account=account),
        ), patch(
            "backend.brokers.connections.secrets", secrets_data
        ):
            instance._rebuild_from_yaml()

        assert "ZG_NOTYPE" in instance.conn, (
            f"Account with no 'broker' key must default to Kite; "
            f"conn={list(instance.conn.keys())}"
        )

    def test_kite_alias_treated_as_kite(self):
        """An account with broker='kite' (alias) must also get a connection."""
        secrets_data = {
            "kite_accounts": {
                "ZG_ALIAS": {
                    "broker": "kite",
                    "api_key": "ak",
                    "api_secret": "as",
                    "password": "pw",
                    "totp_token": "JBSWY3DPEHPK3PXP",
                },
            },
            "kite_login_url": "https://kite.zerodha.com/connect/login",
            "kite_twofa_url": "https://kite.zerodha.com/api/twofa",
        }
        from backend.brokers.connections import Connections

        instance = _make_connections_instance()
        with patch(
            "backend.brokers.connections.KiteConnection",
            side_effect=lambda account, sd: MagicMock(account=account),
        ), patch(
            "backend.brokers.connections.secrets", secrets_data
        ):
            instance._rebuild_from_yaml()

        assert "ZG_ALIAS" in instance.conn, (
            f"Account with broker='kite' alias must be in conn; "
            f"conn={list(instance.conn.keys())}"
        )


# ---------------------------------------------------------------------------
# COLD-2  holdings.py error string is broker-neutral
# ---------------------------------------------------------------------------

class TestHoldingsErrorString:

    def test_no_kite_in_holdings_error_message(self):
        """'Broker (Kite)' must not appear in the holdings outage error string."""
        import ast
        import pathlib

        holdings_path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "api" / "routes" / "holdings.py"
        )
        source = holdings_path.read_text()
        assert "Broker (Kite) returned no holdings data" not in source, (
            "holdings.py still contains 'Broker (Kite)' — must be broker-neutral"
        )

    def test_neutral_string_present_in_holdings(self):
        """The correct broker-neutral string must be present in holdings.py."""
        import pathlib

        holdings_path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "api" / "routes" / "holdings.py"
        )
        source = holdings_path.read_text()
        assert "Broker returned no holdings data" in source, (
            "holdings.py missing the broker-neutral error string"
        )
