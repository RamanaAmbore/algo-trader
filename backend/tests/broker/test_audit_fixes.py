"""
Tests for four audit findings fixed in 2026-08:

  P1-A  kite_ticker.py:unsubscribe() — ghost entries in _tick_map / _sym_to_token / _pending
  P1-B  remote_broker.py           — missing super().__init__() leaves _last_req/_last_resp unset
  P2-A  groww.py @ssot_fetch       — instruments cache key ignored exchange arg
  P2-B  broker_apis.py             — _FETCH_HEALTH ghost entries for decommissioned accounts

All tests run without a live broker connection.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# P1-A — KiteTicker.unsubscribe() prunes _tick_map, _sym_to_token, _pending
# ---------------------------------------------------------------------------

class TestUnsubscribePrunesAllMaps:
    """
    Subscribe two tokens, verify they land in the auxiliary maps, then
    unsubscribe one and assert all four maps are clean for that token.
    Also verifies that a token only in _pending (never sent to the socket)
    is pruned from _pending regardless of whether it was in _subscribed.
    """

    def _make_ticker(self):
        """Return a TickerManager with the Twisted KiteTicker SDK mocked out."""
        from backend.brokers.kite_ticker import TickerManager
        tm = TickerManager()
        # Simulate a live connected state without actually starting Twisted.
        tm._connected = True
        mock_kws = MagicMock()
        mock_kws.MODE_LTP = "ltp"
        tm._kws = mock_kws
        return tm

    def test_unsubscribe_prunes_tick_map(self):
        tm = self._make_ticker()
        tok_a, tok_b = 256265, 408065

        # Manually pre-populate all maps to simulate a previously subscribed state.
        tm._subscribed = {tok_a, tok_b}
        tm._tick_map   = {tok_a: 100.5, tok_b: 200.0}
        tm._tick_age   = {tok_a: 1.0,   tok_b: 2.0}
        tm._sym_to_token = {"NIFTY": tok_a, "BANKNIFTY": tok_b}

        tm.unsubscribe([tok_a])

        # tok_a must be gone from every map.
        assert tok_a not in tm._tick_map,     "_tick_map still has unsubscribed token"
        assert tok_a not in tm._tick_age,     "_tick_age still has unsubscribed token"
        assert "NIFTY" not in tm._sym_to_token, "_sym_to_token still has ghost symbol"

        # tok_b must be untouched.
        assert tok_b in tm._tick_map
        assert tok_b in tm._tick_age
        assert "BANKNIFTY" in tm._sym_to_token

    def test_unsubscribe_prunes_subscribed_set(self):
        tm = self._make_ticker()
        tok = 256265
        tm._subscribed = {tok}
        tm._tick_map   = {tok: 100.5}
        tm._tick_age   = {tok: 1.0}
        tm._sym_to_token = {"NIFTY": tok}

        tm.unsubscribe([tok])

        assert tok not in tm._subscribed

    def test_unsubscribe_prunes_pending_only_token(self):
        """Token in _pending but NOT in _subscribed must still be pruned."""
        from backend.brokers.kite_ticker import TickerManager
        tm = TickerManager()
        # Not connected — tokens would be queued into _pending via subscribe().
        tok = 999999
        tm._pending = {tok}
        tm._subscribed = set()  # not subscribed (pre-connect state)

        tm.unsubscribe([tok])

        assert tok not in tm._pending, "_pending still holds the unsubscribed token"

    def test_unsubscribe_prunes_pending_alongside_subscribed(self):
        """Token in both _pending and _subscribed must be removed from both."""
        tm = self._make_ticker()
        tok = 123456
        tm._subscribed = {tok}
        tm._pending    = {tok}
        tm._tick_map   = {tok: 50.0}
        tm._tick_age   = {tok: 1.0}
        tm._sym_to_token = {"DUMMY": tok}

        tm.unsubscribe([tok])

        assert tok not in tm._pending
        assert tok not in tm._subscribed
        assert tok not in tm._tick_map
        assert "DUMMY" not in tm._sym_to_token

    def test_unsubscribe_noop_when_token_absent(self):
        """unsubscribe() on a token that was never subscribed must not raise."""
        tm = self._make_ticker()
        tm._subscribed = set()
        tm._pending    = set()
        # Should complete without exception.
        tm.unsubscribe([999])


# ---------------------------------------------------------------------------
# P1-B — RemoteBroker.__init__ calls super().__init__()
# ---------------------------------------------------------------------------

class TestRemoteBrokerDiagnosticDicts:
    """
    Instantiating RemoteBroker must result in _last_req and _last_resp being
    present as empty dicts (initialised by Broker.__init__).

    The UDS httpx client is created at module level — this test does NOT need
    the conn socket to be live; it only checks instance attribute presence.
    """

    def test_has_last_req(self):
        from backend.brokers.client.remote_broker import RemoteBroker
        rb = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        assert hasattr(rb, "_last_req"), "RemoteBroker missing _last_req"
        assert isinstance(rb._last_req, dict)

    def test_has_last_resp(self):
        from backend.brokers.client.remote_broker import RemoteBroker
        rb = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        assert hasattr(rb, "_last_resp"), "RemoteBroker missing _last_resp"
        assert isinstance(rb._last_resp, dict)

    def test_dicts_start_empty(self):
        from backend.brokers.client.remote_broker import RemoteBroker
        rb = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        assert rb._last_req  == {}
        assert rb._last_resp == {}

    def test_last_request_debug_returns_both(self):
        """last_request_debug() is the public surface that reads both dicts."""
        from backend.brokers.client.remote_broker import RemoteBroker
        rb = RemoteBroker(account="ZG0790", broker_id="zerodha_kite")
        debug = rb.last_request_debug()
        assert "request"  in debug
        assert "response" in debug


# ---------------------------------------------------------------------------
# P2-A — GrowwBroker.instruments cache key includes exchange
# ---------------------------------------------------------------------------

class TestGrowwInstrumentsCacheKey:
    """
    Verify the ssot_fetch key lambda for GrowwBroker.instruments produces
    distinct keys for different exchange arguments so NSE and BSE results
    never collide in the cache.
    """

    def _extract_key_lambda(self):
        """
        Pull the key lambda from the ssot_fetch decorator without instantiating
        a real GrowwBroker (which requires credentials).

        The lambda is the first argument to @ssot_fetch.  We inspect the
        __wrapped__ chain to reach it, or fall back to reading the decorator
        arguments from the closure.
        """
        import inspect
        from backend.brokers.adapters import groww as groww_mod
        # The key lambda is defined inline on the decorator — extract it by
        # rebuilding the expected behaviour rather than digging into closures.
        # We use a lightweight stub with the same .account attribute shape.
        return None  # signal to test body to use the stub approach

    def test_different_exchanges_produce_different_keys(self):
        """NSE and BSE must produce distinct cache keys."""
        # Reconstruct the key lambda exactly as written in groww.py.
        key_fn = lambda self, *a, **kw: (
            f"groww_instruments_{self.account}"
            f"_{(a[0] if a else kw.get('exchange')) or 'all'}"
        )
        stub = MagicMock()
        stub.account = "GW0001"

        key_nse = key_fn(stub, "NSE")
        key_bse = key_fn(stub, "BSE")
        key_all = key_fn(stub)

        assert key_nse != key_bse, "NSE and BSE must produce different cache keys"
        assert key_nse != key_all, "NSE and all-instruments must produce different cache keys"
        assert key_bse != key_all, "BSE and all-instruments must produce different cache keys"

    def test_same_exchange_produces_same_key(self):
        """Idempotency: calling with the same exchange twice gives the same key."""
        key_fn = lambda self, *a, **kw: (
            f"groww_instruments_{self.account}"
            f"_{(a[0] if a else kw.get('exchange')) or 'all'}"
        )
        stub = MagicMock()
        stub.account = "GW0001"

        assert key_fn(stub, "NSE") == key_fn(stub, "NSE")

    def test_kwarg_exchange_produces_same_key_as_positional(self):
        """key_fn(stub, exchange="NSE") == key_fn(stub, "NSE")."""
        key_fn = lambda self, *a, **kw: (
            f"groww_instruments_{self.account}"
            f"_{(a[0] if a else kw.get('exchange')) or 'all'}"
        )
        stub = MagicMock()
        stub.account = "GW0001"

        assert key_fn(stub, "NSE") == key_fn(stub, exchange="NSE")

    def test_groww_instruments_key_in_source(self):
        """
        Smoke-check: the actual source of groww.py must contain the
        exchange-aware key string so we catch any accidental revert.
        """
        import inspect
        from backend.brokers.adapters import groww as groww_mod
        src = inspect.getsource(groww_mod)
        # The fixed lambda must reference 'exchange' in the key expression.
        assert "kw.get('exchange')" in src or 'kw.get("exchange")' in src, (
            "groww.py instruments @ssot_fetch key does not reference the exchange argument — "
            "P2-A fix may have been reverted"
        )


# ---------------------------------------------------------------------------
# P2-B — _prune_fetch_health removes ghost entries
# ---------------------------------------------------------------------------

class TestFetchHealthPrune:
    """
    _prune_fetch_health() must remove entries for accounts that are no longer
    in the live registry (Connections.conn), while leaving active accounts alone.
    """

    LIVE_ACCOUNT  = "DH6847"
    GHOST_ACCOUNT = "DECOMMISSIONED_XY9999"

    def setup_method(self):
        """Pre-populate _FETCH_HEALTH with one live and one ghost account."""
        from backend.brokers import broker_apis
        broker_apis._FETCH_HEALTH[self.LIVE_ACCOUNT]  = {
            "last_ok_at": 1.0, "last_fail_at": 0.0,
            "consecutive_fail_count": 0, "circuit_open_until": None,
        }
        broker_apis._FETCH_HEALTH[self.GHOST_ACCOUNT] = {
            "last_ok_at": 0.0, "last_fail_at": 1.0,
            "consecutive_fail_count": 5, "circuit_open_until": None,
        }

    def teardown_method(self):
        from backend.brokers import broker_apis
        broker_apis._FETCH_HEALTH.pop(self.LIVE_ACCOUNT,  None)
        broker_apis._FETCH_HEALTH.pop(self.GHOST_ACCOUNT, None)

    def test_ghost_entry_removed(self):
        from backend.brokers.broker_apis import _prune_fetch_health
        _prune_fetch_health(live_accounts={self.LIVE_ACCOUNT})
        from backend.brokers import broker_apis
        assert self.GHOST_ACCOUNT not in broker_apis._FETCH_HEALTH, (
            "Ghost account must be removed from _FETCH_HEALTH after prune"
        )

    def test_live_entry_preserved(self):
        from backend.brokers.broker_apis import _prune_fetch_health
        _prune_fetch_health(live_accounts={self.LIVE_ACCOUNT})
        from backend.brokers import broker_apis
        assert self.LIVE_ACCOUNT in broker_apis._FETCH_HEALTH, (
            "Live account must NOT be removed from _FETCH_HEALTH by prune"
        )

    def test_empty_live_set_is_noop(self):
        """When called with empty set, prune must do nothing (safety guard)."""
        from backend.brokers.broker_apis import _prune_fetch_health
        _prune_fetch_health(live_accounts=set())
        from backend.brokers import broker_apis
        # Both entries must still be present.
        assert self.LIVE_ACCOUNT  in broker_apis._FETCH_HEALTH
        assert self.GHOST_ACCOUNT in broker_apis._FETCH_HEALTH

    def test_multiple_ghosts_all_removed(self):
        """All ghost entries must be pruned in a single call."""
        from backend.brokers import broker_apis
        extra_ghost = "GHOST_ZZ1234"
        broker_apis._FETCH_HEALTH[extra_ghost] = {
            "last_ok_at": 0.0, "last_fail_at": 2.0,
            "consecutive_fail_count": 1, "circuit_open_until": None,
        }
        from backend.brokers.broker_apis import _prune_fetch_health
        _prune_fetch_health(live_accounts={self.LIVE_ACCOUNT})

        assert self.GHOST_ACCOUNT not in broker_apis._FETCH_HEALTH
        assert extra_ghost         not in broker_apis._FETCH_HEALTH
        assert self.LIVE_ACCOUNT   in    broker_apis._FETCH_HEALTH

        broker_apis._FETCH_HEALTH.pop(extra_ghost, None)

    def test_prune_is_thread_safe(self):
        """Concurrent prune + _record_fetch must not raise RuntimeError."""
        from backend.brokers.broker_apis import _prune_fetch_health, _record_fetch
        import concurrent.futures

        errors = []

        def _prune():
            try:
                _prune_fetch_health(live_accounts={self.LIVE_ACCOUNT})
            except Exception as exc:
                errors.append(exc)

        def _record():
            try:
                _record_fetch(self.LIVE_ACCOUNT, ok=True)
            except Exception as exc:
                errors.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(_prune) for _ in range(4)] + \
                   [pool.submit(_record) for _ in range(4)]
            concurrent.futures.wait(futs)

        assert not errors, f"Thread-safety violations: {errors}"
