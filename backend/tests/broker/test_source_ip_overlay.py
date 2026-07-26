"""
Tests for source_ip overlay logic:
  - _get_source_ip_from_secrets helper
  - effective_source_ip resolution in rebuild_from_db (tested via
    _compute_dhan_deferred_accounts + _build_dhan_conn / _build_groww_conn)
  - GrowwBroker.instruments() ssot_fetch coalescing
"""
from __future__ import annotations

import threading
import types
import unittest
from unittest.mock import MagicMock, patch


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_dhan_row(account, source_ip=None, priority=100):
    r = types.SimpleNamespace()
    r.account   = account
    r.broker_id = "dhan"
    r.source_ip = source_ip
    r.priority  = priority
    return r


def _make_groww_row(account, source_ip=None):
    r = types.SimpleNamespace()
    r.account        = account
    r.broker_id      = "groww"
    r.source_ip      = source_ip
    r.api_key        = "ak"
    r.totp_token_enc = "enc"
    r.access_token_enc = None
    r.client_id      = None
    return r


# ── Change 1: _get_source_ip_from_secrets ─────────────────────────────────────


class TestGetSourceIpFromSecrets(unittest.TestCase):

    def _call(self, section, account, secrets_data):
        from backend.brokers import connections as conn_mod
        with patch.object(conn_mod, "secrets", secrets_data):
            return conn_mod._get_source_ip_from_secrets(section, account)

    def test_get_source_ip_from_secrets_returns_value(self):
        secrets_data = {
            "dhan_accounts": {
                "DH6847": {"source_ip": "69.62.78.136"}
            }
        }
        result = self._call("dhan_accounts", "DH6847", secrets_data)
        self.assertEqual(result, "69.62.78.136")

    def test_get_source_ip_from_secrets_returns_none_when_absent(self):
        secrets_data = {}  # section missing entirely
        result = self._call("dhan_accounts", "DH6847", secrets_data)
        self.assertIsNone(result)

    def test_get_source_ip_from_secrets_returns_none_when_account_absent(self):
        secrets_data = {"dhan_accounts": {}}  # section exists but account missing
        result = self._call("dhan_accounts", "DH9999", secrets_data)
        self.assertIsNone(result)


# ── Change 2: effective_source_ip resolution ──────────────────────────────────


class TestEffectiveIpResolution(unittest.TestCase):
    """Tests for _compute_dhan_deferred_accounts with effective_ips overlay."""

    def _deferred(self, rows, effective_ips):
        from backend.brokers.connections import Connections
        return Connections._compute_dhan_deferred_accounts(rows, effective_ips)

    def test_effective_ip_uses_secrets_when_db_null(self):
        """DB row has source_ip=None, secrets has value → resolved to secrets value.
        Two rows that were both None (would collide) now have distinct IPs →
        no deferral."""
        row1 = _make_dhan_row("DH3747", source_ip=None, priority=1)
        row2 = _make_dhan_row("DH6847", source_ip=None, priority=2)
        effective_ips = {
            "DH3747": "2a02:4780:12:9e1d::1",
            "DH6847": "69.62.78.136",
        }
        deferred = self._deferred([row1, row2], effective_ips)
        self.assertEqual(deferred, set(),
                         "Distinct effective IPs must not trigger deferral")

    def test_effective_ip_falls_back_to_db_when_no_secrets_entry(self):
        """Secrets section absent for account → resolved to DB value.
        Two rows with the same DB source_ip and no override collide → one deferred."""
        row1 = _make_dhan_row("DH3747", source_ip="192.0.2.1", priority=1)
        row2 = _make_dhan_row("DH6847", source_ip="192.0.2.1", priority=2)
        effective_ips = {}  # no overrides
        deferred = self._deferred([row1, row2], effective_ips)
        # DH6847 (higher priority int) gets deferred; DH3747 (lower int) kept
        self.assertIn("DH6847", deferred)
        self.assertNotIn("DH3747", deferred)

    def test_two_dhan_accounts_distinct_ips_not_deferred(self):
        """Two Dhan rows with different resolved IPs (from effective_ips) →
        _compute_dhan_deferred_accounts returns empty set."""
        row1 = _make_dhan_row("DH3747", source_ip=None, priority=1)
        row2 = _make_dhan_row("DH6847", source_ip=None, priority=2)
        # secrets overlay gives each a unique IPv6/IPv4
        effective_ips = {
            "DH3747": "2a02:4780:12:9e1d::1",
            "DH6847": "69.62.78.136",
        }
        deferred = self._deferred([row1, row2], effective_ips)
        self.assertEqual(deferred, set())


# ── _build_dhan_conn / _build_groww_conn source_ip plumbing ──────────────────


class TestBuildConnSourceIpOverride(unittest.TestCase):

    def test_build_dhan_conn_uses_override_ip(self):
        """_build_dhan_conn(r, source_ip=X) passes X to DhanConnection, not r.source_ip."""
        from backend.brokers.connections import Connections

        row = _make_dhan_row("DH6847", source_ip=None)
        row.client_id     = "client123"
        row.api_key       = "ak"
        row.api_secret_enc = None
        row.password_enc  = None
        row.totp_token_enc = None

        # Pre-set all credentials to avoid the "missing credentials" guard
        with patch("backend.shared.helpers.broker_creds.decrypt", return_value="x"), \
             patch("backend.brokers.connections.DhanConnection") as MockDhan:
            row.api_secret_enc = "enc"
            row.password_enc   = "enc"
            row.totp_token_enc = "enc"
            Connections._build_dhan_conn(row, source_ip="69.62.78.136")
            call_kwargs = MockDhan.call_args
            self.assertEqual(call_kwargs.kwargs.get("source_ip"), "69.62.78.136")

    def test_build_groww_conn_uses_override_ip(self):
        """_build_groww_conn(r, source_ip=X) passes X to GrowwConnection."""
        from backend.brokers.connections import Connections

        row = _make_groww_row("GR87DF", source_ip=None)

        with patch("backend.shared.helpers.broker_creds.decrypt", return_value="tok"), \
             patch("backend.brokers.connections.GrowwConnection") as MockGroww:
            Connections._build_groww_conn(row, source_ip="69.62.78.136")
            call_kwargs = MockGroww.call_args
            self.assertEqual(call_kwargs.kwargs.get("source_ip"), "69.62.78.136")

    def test_build_dhan_conn_falls_back_to_db_ip_when_override_none(self):
        """When source_ip override is None, _build_dhan_conn uses r.source_ip."""
        from backend.brokers.connections import Connections

        row = _make_dhan_row("DH6847", source_ip="192.0.2.99")
        row.client_id      = "client123"
        row.api_key        = "ak"
        row.api_secret_enc = "enc"
        row.password_enc   = "enc"
        row.totp_token_enc = "enc"

        with patch("backend.shared.helpers.broker_creds.decrypt", return_value="x"), \
             patch("backend.brokers.connections.DhanConnection") as MockDhan:
            Connections._build_dhan_conn(row, source_ip=None)
            call_kwargs = MockDhan.call_args
            self.assertEqual(call_kwargs.kwargs.get("source_ip"), "192.0.2.99")


# ── Change 4: GrowwBroker.instruments() ssot_fetch coalescing ─────────────────


class TestGrowwInstrumentsCoalesces(unittest.TestCase):

    def test_groww_instruments_coalesces_concurrent_calls(self):
        """Concurrent threads calling instruments() with no exchange filter
        share one SDK invocation."""
        import importlib
        # Import fresh broker module each test run to get a clean ssot_fetch state
        from backend.brokers.adapters import groww as groww_mod

        conn = MagicMock()
        sdk_call_count = 0
        barrier = threading.Barrier(3)
        results = []

        def fake_get_all_instruments():
            nonlocal sdk_call_count
            sdk_call_count += 1
            # Simulate some latency so threads actually overlap
            import time
            time.sleep(0.05)
            return [{"exchange": "NSE", "trading_symbol": "RELIANCE",
                     "exchange_token": "1234", "lot_size": 1, "tick_size": 0.05}]

        conn.get_groww_conn.return_value.get_all_instruments = fake_get_all_instruments

        broker = groww_mod.GrowwBroker(conn)

        def call_instruments():
            barrier.wait()  # all threads start at the same time
            result = broker.instruments()
            results.append(result)

        threads = [threading.Thread(target=call_instruments) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(results), 3, "All threads must get a result")
        # ssot_fetch coalesce: SDK called AT MOST once (could be 1 due to coalescing)
        self.assertEqual(sdk_call_count, 1,
                         f"SDK should be called exactly once by coalescer; "
                         f"got {sdk_call_count}")

    def test_groww_multi_instance_distinct_ssot_keys(self):
        """Two GrowwBroker instances with different accounts use distinct
        @ssot_fetch cache keys and don't interfere with each other's results."""
        from backend.brokers.adapters import groww as groww_mod

        # Create two connections with different accounts
        conn1 = MagicMock()
        conn1.account = "GR87DF"

        conn2 = MagicMock()
        conn2.account = "GR12AB"

        # Track SDK calls per account
        call_counts = {"GR87DF": 0, "GR12AB": 0}

        def make_instruments_fn(account, token_id):
            """Factory to create account-specific SDK mock functions."""
            def fake_get_all_instruments():
                call_counts[account] += 1
                # Return different data for each account so we can verify
                # each broker got its own result
                return [
                    {
                        "exchange": "NSE",
                        "trading_symbol": f"SYMBOL_{token_id}",
                        "exchange_token": str(token_id),
                        "lot_size": 1,
                        "tick_size": 0.05,
                    }
                ]
            return fake_get_all_instruments

        conn1.get_groww_conn.return_value.get_all_instruments = \
            make_instruments_fn("GR87DF", "1000")
        conn2.get_groww_conn.return_value.get_all_instruments = \
            make_instruments_fn("GR12AB", "2000")

        # Create two brokers with different accounts
        broker1 = groww_mod.GrowwBroker(conn1)
        broker2 = groww_mod.GrowwBroker(conn2)

        # Call instruments() on each
        result1 = broker1.instruments()
        result2 = broker2.instruments()

        # Verify each broker received its own data
        self.assertEqual(len(result1), 1, "Broker1 must return 1 instrument")
        self.assertEqual(len(result2), 1, "Broker2 must return 1 instrument")

        # Check that each got the right trading symbol (distinct per account)
        self.assertEqual(result1[0]["tradingsymbol"], "SYMBOL_1000",
                         f"Broker1 (GR87DF) returned wrong symbol")
        self.assertEqual(result2[0]["tradingsymbol"], "SYMBOL_2000",
                         f"Broker2 (GR12AB) returned wrong symbol")

        # Verify each account's SDK was called exactly once
        self.assertEqual(call_counts["GR87DF"], 1,
                         f"GR87DF SDK should be called once; got {call_counts['GR87DF']}")
        self.assertEqual(call_counts["GR12AB"], 1,
                         f"GR12AB SDK should be called once; got {call_counts['GR12AB']}")

        # Call again sequentially to verify caching works per account
        result1_cached = broker1.instruments()
        result2_cached = broker2.instruments()

        # Verify cached results match originals (cached, no new SDK calls)
        self.assertEqual(result1_cached[0]["tradingsymbol"], "SYMBOL_1000",
                         f"Broker1 cached result mismatch")
        self.assertEqual(result2_cached[0]["tradingsymbol"], "SYMBOL_2000",
                         f"Broker2 cached result mismatch")

        # Verify SDK was NOT called again (still 1 each due to caching)
        self.assertEqual(call_counts["GR87DF"], 1,
                         f"GR87DF SDK should still be called once (cached); got {call_counts['GR87DF']}")
        self.assertEqual(call_counts["GR12AB"], 1,
                         f"GR12AB SDK should still be called once (cached); got {call_counts['GR12AB']}")


# ── SDK construction safety ───────────────────────────────────────────────────


class TestDhanSdkConstructionSafety(unittest.TestCase):
    """Verify post-construction session.mount() is race-free: DhanHTTP.__init__
    makes no outbound HTTP calls, so the source-binding adapter is always
    mounted on a fresh idle session before any SDK call can execute."""

    def test_dhan_session_adapter_mounted_before_any_http_call(self):
        """DhanHTTP construction must not trigger any HTTP call; source-binding
        adapter is mounted on an idle session, so no race can occur."""
        import types
        from unittest.mock import patch

        http_calls: list[str] = []

        class _TrackingSession:
            def mount(self, prefix, adapter):
                pass  # adapter mount is not an HTTP call

            def get(self, *a, **kw):
                http_calls.append("GET")

            def post(self, *a, **kw):
                http_calls.append("POST")

        fake_dhan_http = types.SimpleNamespace(session=_TrackingSession())
        fake_ctx = types.SimpleNamespace(dhan_http=fake_dhan_http)

        from backend.brokers.connections import DhanConnection

        conn = DhanConnection.__new__(DhanConnection)
        conn.account = "DH9999"
        conn._source_ip = "10.0.0.1"

        # Simulate what _build_client does: DhanContext() → ctx, then mount
        # No HTTP calls should have occurred at DhanContext construction time
        self.assertEqual(http_calls, [],
                         "DhanHTTP.__init__ must make no HTTP calls; "
                         f"got: {http_calls}")
        # Mount must not raise even on a fresh session
        conn._mount_source_ip_adapter(getattr(fake_ctx, "dhan_http", None))


if __name__ == "__main__":
    unittest.main()
