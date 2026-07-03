"""
Market-data session consistency — end-to-end request coherence tests.

Verifies that within a single simulated request context, every
market-data callsite (quote, ltp, instruments) obtains the SAME
broker instance rather than re-resolving each time.

Five quality dimensions:
  SSOT       — get_market_data_broker() is the only resolution path;
               get_price_broker() is never called directly from inside
               a request context.
  Perf       — broker is constructed once per request; repeated calls
               O(1) via contextvar cache; no extra round-trips.
  Stale      — no route-layer module calls get_price_broker() directly
               (verified by grep scan over key route files).
  Reusable   — reset_market_data_broker_ctx() restores a clean slate
               so the next request gets a fresh selection.
  Correctness — same broker instance delivered across quote, ltp, and
               instruments callsites within one request context.
"""

from __future__ import annotations

import asyncio
from typing import Optional
from unittest.mock import MagicMock, patch, call

import pytest

from backend.brokers.registry import (
    PriceBroker,
    _MDB_CTX,
    get_market_data_broker,
    reset_market_data_broker_ctx,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_broker(account: str) -> MagicMock:
    b = MagicMock()
    b.account = account
    b.broker_id = "zerodha_kite"
    b.quote.return_value = {f"NSE:{account}": {"last_price": 123.0}}
    b.ltp.return_value = {f"NSE:{account}": {"last_price": 123.0}}
    b.instruments.return_value = []
    return b


def _make_price_broker(*accounts: str) -> PriceBroker:
    brokers = [_make_broker(a) for a in accounts]
    return PriceBroker(brokers)


# ---------------------------------------------------------------------------
# 1. Same instance for quote + ltp + instruments within one context
# ---------------------------------------------------------------------------

class TestSameInstanceAcrossCallsites:
    """Simulates three market-data callsites in a single request."""

    def setup_method(self):
        reset_market_data_broker_ctx()

    def test_quote_ltp_instruments_use_same_broker(self):
        """quote, ltp, instruments callsites receive the identical broker."""
        pb = _make_price_broker("ZG0790")

        with patch(
            "backend.brokers.registry.get_price_broker", return_value=pb
        ) as mock_gpb:
            # Simulate three separate callsites in the same request context.
            broker_for_quote = get_market_data_broker()
            broker_for_ltp   = get_market_data_broker()
            broker_for_instr = get_market_data_broker()

        # All three received the same object.
        assert broker_for_quote is broker_for_ltp
        assert broker_for_ltp is broker_for_instr

        # Underlying resolver called exactly once.
        assert mock_gpb.call_count == 1

    def test_broker_object_is_price_broker(self):
        """get_market_data_broker() returns a PriceBroker, not a raw adapter."""
        pb = _make_price_broker("ZG0790", "ZJ6294")

        with patch("backend.brokers.registry.get_price_broker", return_value=pb):
            result = get_market_data_broker()

        assert isinstance(result, PriceBroker)

    def test_primary_account_exposed(self):
        """The returned broker's .account attribute identifies the primary."""
        pb = _make_price_broker("ZJ6294")  # first = primary

        with patch("backend.brokers.registry.get_price_broker", return_value=pb):
            broker = get_market_data_broker()

        assert broker.account == "ZJ6294"


# ---------------------------------------------------------------------------
# 2. Request boundary isolation — reset between requests
# ---------------------------------------------------------------------------

class TestRequestBoundaryIsolation:
    """Each request gets a fresh broker selection after reset."""

    def setup_method(self):
        reset_market_data_broker_ctx()

    def test_second_request_gets_fresh_broker(self):
        """After reset, next call resolves a new broker."""
        pb_req1 = _make_price_broker("ZG0790")
        pb_req2 = _make_price_broker("ZJ6294")

        with patch("backend.brokers.registry.get_price_broker", return_value=pb_req1):
            req1_broker = get_market_data_broker()

        # Simulate Litestar before_request hook clearing the contextvar.
        reset_market_data_broker_ctx()

        with patch("backend.brokers.registry.get_price_broker", return_value=pb_req2):
            req2_broker = get_market_data_broker()

        assert req1_broker is pb_req1
        assert req2_broker is pb_req2
        assert req1_broker is not req2_broker

    def test_contextvar_none_after_reset(self):
        """Contextvar is None between requests."""
        pb = _make_price_broker("ZG0790")

        with patch("backend.brokers.registry.get_price_broker", return_value=pb):
            get_market_data_broker()

        reset_market_data_broker_ctx()
        assert _MDB_CTX.get(None) is None


# ---------------------------------------------------------------------------
# 3. Concurrent request isolation (asyncio Tasks)
# ---------------------------------------------------------------------------

class TestConcurrentRequestIsolation:
    """Two concurrent asyncio Tasks each use their own cached broker."""

    def setup_method(self):
        reset_market_data_broker_ctx()

    def test_concurrent_tasks_independent_brokers(self):
        """asyncio.gather: task A and task B each get their own broker."""
        pb_a = _make_price_broker("ZG0790")
        pb_b = _make_price_broker("ZJ6294")

        results: list[tuple[str, PriceBroker]] = []

        async def simulate_request_a():
            reset_market_data_broker_ctx()
            with patch("backend.brokers.registry.get_price_broker", return_value=pb_a):
                b = get_market_data_broker()
                results.append(("A", b))

        async def simulate_request_b():
            reset_market_data_broker_ctx()
            with patch("backend.brokers.registry.get_price_broker", return_value=pb_b):
                b = get_market_data_broker()
                results.append(("B", b))

        async def run():
            await asyncio.gather(simulate_request_a(), simulate_request_b())

        asyncio.run(run())

        assert len(results) == 2
        brokers_by_task = {label: broker for label, broker in results}
        assert brokers_by_task["A"] is pb_a
        assert brokers_by_task["B"] is pb_b
        assert brokers_by_task["A"] is not brokers_by_task["B"]


# ---------------------------------------------------------------------------
# 4. Stale-code grep — key route files no longer import get_price_broker
#    for market-data purposes
# ---------------------------------------------------------------------------

class TestStaleCallsiteGrep:
    """Import-level check that route modules use get_market_data_broker."""

    def _grep_module_for_call(self, module_path: str) -> list[int]:
        """Return line numbers where `get_price_broker()` is called (not just
        referenced in a comment or import-only line)."""
        import re
        try:
            with open(module_path) as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            return []

        # Match actual call expressions: `= get_price_broker()` or
        # `(get_price_broker().` — excludes comment lines and import stmts.
        pattern = re.compile(r'(?<!#)get_price_broker\s*\(\s*\)')
        hits = []
        for i, line in enumerate(lines, start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("from ") or stripped.startswith("import "):
                continue
            if pattern.search(line):
                hits.append(i)
        return hits

    def test_quote_py_no_direct_calls(self):
        import backend.api.routes.quote as m
        import inspect
        src_path = inspect.getfile(m)
        hits = self._grep_module_for_call(src_path)
        assert hits == [], (
            f"quote.py calls get_price_broker() directly at lines {hits}; "
            "use get_market_data_broker() instead"
        )

    def test_watchlist_py_no_direct_calls(self):
        import backend.api.routes.watchlist as m
        import inspect
        src_path = inspect.getfile(m)
        hits = self._grep_module_for_call(src_path)
        assert hits == [], (
            f"watchlist.py calls get_price_broker() directly at lines {hits}; "
            "use get_market_data_broker() instead"
        )

    def test_options_py_no_direct_calls(self):
        import backend.api.routes.options as m
        import inspect
        src_path = inspect.getfile(m)
        hits = self._grep_module_for_call(src_path)
        assert hits == [], (
            f"options.py calls get_price_broker() directly at lines {hits}; "
            "use get_market_data_broker() instead"
        )

    def test_instruments_py_no_direct_calls(self):
        import backend.api.routes.instruments as m
        import inspect
        src_path = inspect.getfile(m)
        hits = self._grep_module_for_call(src_path)
        assert hits == [], (
            f"instruments.py calls get_price_broker() directly at lines {hits}; "
            "use get_market_data_broker() instead"
        )
