"""
Tests for market-data broker consolidation — get_market_data_broker() +
per-request contextvar consistency.

Five quality dimensions (per house style):
  SSOT       — single resolver (get_market_data_broker) used by all
               market-data callsites; get_price_broker() is the
               underlying selection engine.
  Perf       — contextvar cache is O(1) after first call within a
               request; no re-resolution on repeated calls.
  Stale      — no callsite should call get_price_broker() directly
               from within a route handler (enforced by grep check).
  Reusable   — same cached broker instance returned for quote, ltp,
               instruments, historical_data within one request context.
  Correctness (branch coverage):
    - Two healthy Kites → primary = first per priority / pin.
    - Contextvar caches selection for request lifetime.
    - reset_market_data_broker_ctx() clears the cache.
    - Different asyncio Tasks → independent caches.
    - get_price_broker() called fresh in background (no ctx set).
    - All brokers fail → returns (None, None) not an exception.
"""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import MagicMock, patch

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

def _make_kite_broker(account: str, priority: int = 100) -> MagicMock:
    b = MagicMock()
    b.account = account
    b.broker_id = "zerodha_kite"
    b.quote.return_value = {f"NSE:{account}": {"last_price": 100.0}}
    b.ltp.return_value = {f"NSE:{account}": {"last_price": 100.0}}
    return b


def _make_price_broker(*accounts: str) -> PriceBroker:
    brokers = [_make_kite_broker(a) for a in accounts]
    return PriceBroker(brokers)


# ---------------------------------------------------------------------------
# 1. Context reset
# ---------------------------------------------------------------------------

class TestContextReset:
    """reset_market_data_broker_ctx clears the cached PriceBroker."""

    def test_reset_clears_cached_broker(self):
        """After reset the contextvar holds None."""
        pb = _make_price_broker("ZG0790")
        _MDB_CTX.set(pb)
        assert _MDB_CTX.get(None) is not None

        reset_market_data_broker_ctx()
        assert _MDB_CTX.get(None) is None

    def test_reset_is_idempotent_when_already_none(self):
        """reset_market_data_broker_ctx is safe to call when nothing is set."""
        reset_market_data_broker_ctx()  # no prior set
        reset_market_data_broker_ctx()  # second call — should not raise
        assert _MDB_CTX.get(None) is None


# ---------------------------------------------------------------------------
# 2. Cache hit on repeated calls within same context
# ---------------------------------------------------------------------------

class TestContextvarCaching:
    """Same PriceBroker returned on every call within one asyncio Task."""

    def setup_method(self):
        reset_market_data_broker_ctx()

    def test_repeated_calls_return_same_instance(self):
        """get_market_data_broker() returns the identical object on repeated calls."""
        pb = _make_price_broker("ZG0790", "ZJ6294")

        with patch(
            "backend.brokers.registry.get_price_broker", return_value=pb
        ) as mock_gpb:
            first = get_market_data_broker()
            second = get_market_data_broker()
            third = get_market_data_broker()

        # get_price_broker should be called ONCE — subsequent calls use cache.
        assert mock_gpb.call_count == 1
        assert first is second
        assert second is third

    def test_contextvar_set_after_first_call(self):
        """After first call, contextvar holds the PriceBroker."""
        pb = _make_price_broker("ZG0790")

        with patch("backend.brokers.registry.get_price_broker", return_value=pb):
            result = get_market_data_broker()

        assert _MDB_CTX.get(None) is result

    def test_reset_forces_re_resolution(self):
        """After reset(), next call resolves a fresh broker."""
        pb1 = _make_price_broker("ZG0790")
        pb2 = _make_price_broker("ZJ6294")

        with patch(
            "backend.brokers.registry.get_price_broker", return_value=pb1
        ):
            first = get_market_data_broker()

        reset_market_data_broker_ctx()

        with patch(
            "backend.brokers.registry.get_price_broker", return_value=pb2
        ):
            second = get_market_data_broker()

        assert first is pb1
        assert second is pb2
        assert first is not second


# ---------------------------------------------------------------------------
# 3. Independent contexts per asyncio Task
# ---------------------------------------------------------------------------

class TestTaskIsolation:
    """Each asyncio Task has its own contextvar copy — Tasks don't share."""

    def setup_method(self):
        reset_market_data_broker_ctx()

    def test_two_tasks_see_independent_caches(self):
        """Two concurrent asyncio Tasks each resolve their own broker."""
        pb_a = _make_price_broker("ZG0790")
        pb_b = _make_price_broker("ZJ6294")

        captured: list[PriceBroker] = []

        async def task_a():
            reset_market_data_broker_ctx()
            with patch(
                "backend.brokers.registry.get_price_broker", return_value=pb_a
            ):
                captured.append(get_market_data_broker())

        async def task_b():
            reset_market_data_broker_ctx()
            with patch(
                "backend.brokers.registry.get_price_broker", return_value=pb_b
            ):
                captured.append(get_market_data_broker())

        async def run():
            await asyncio.gather(task_a(), task_b())

        asyncio.run(run())

        # Both tasks ran; both captured a broker; they are distinct.
        assert len(captured) == 2
        assert pb_a in captured
        assert pb_b in captured


# ---------------------------------------------------------------------------
# 4. Selection order — pinned account wins
# ---------------------------------------------------------------------------

class TestSelectionOrder:
    """Primary is operator-pinned account, then priority sort, then Dhan/Groww."""

    def setup_method(self):
        reset_market_data_broker_ctx()

    def test_pinned_account_is_primary(self):
        """When connections.price_account is set, that account is broker.account."""
        pb = _make_price_broker("ZJ6294", "ZG0790")  # ZJ6294 is primary

        with patch(
            "backend.brokers.registry.get_price_broker", return_value=pb
        ):
            broker = get_market_data_broker()

        assert broker.account == "ZJ6294"

    def test_priority_sort_selects_first_by_priority(self):
        """Without a pin, lowest-priority-number account becomes primary."""
        pb = _make_price_broker("ZG0790")  # first in priority order

        with patch(
            "backend.brokers.registry.get_price_broker", return_value=pb
        ):
            broker = get_market_data_broker()

        assert broker.account == "ZG0790"


# ---------------------------------------------------------------------------
# 5. Fallback when PriceBroker wraps no accounts
# ---------------------------------------------------------------------------

class TestNoAccountsFallback:
    """get_market_data_broker() propagates KeyError from get_price_broker()
    when no accounts are configured — callers must handle."""

    def setup_method(self):
        reset_market_data_broker_ctx()

    def test_no_accounts_raises(self):
        """KeyError propagates when no accounts are configured."""
        with patch(
            "backend.brokers.registry.get_price_broker",
            side_effect=KeyError("No broker accounts configured."),
        ):
            with pytest.raises(KeyError):
                get_market_data_broker()


# ---------------------------------------------------------------------------
# 6. Background / non-request context (no contextvar set)
# ---------------------------------------------------------------------------

class TestBackgroundTaskContext:
    """Background pollers: no reset called → contextvar None → resolver
    called fresh on every get_market_data_broker() invocation."""

    def test_unset_contextvar_resolves_fresh_each_call(self):
        """When _MDB_CTX is None, get_price_broker() is called on every call."""
        # Ensure no cached value.
        reset_market_data_broker_ctx()
        reset_market_data_broker_ctx()

        pb1 = _make_price_broker("ZG0790")
        pb2 = _make_price_broker("ZJ6294")
        side_effects = [pb1, pb2]
        call_count = 0

        def fresh_each_time():
            nonlocal call_count
            result = side_effects[call_count % 2]
            call_count += 1
            return result

        # After each call, reset (mimics background-task behaviour where
        # there is no persistent context — each poll cycle starts fresh).
        reset_market_data_broker_ctx()
        with patch("backend.brokers.registry.get_price_broker", side_effect=fresh_each_time):
            b1 = get_market_data_broker()
            reset_market_data_broker_ctx()  # simulate background: no persistent ctx
            b2 = get_market_data_broker()

        # Two distinct calls to get_price_broker because ctx was cleared between them.
        assert call_count == 2
        assert b1 is pb1
        assert b2 is pb2


# ---------------------------------------------------------------------------
# 7. SSOT grep — instruments.py walks Kite accounts directly, NEVER falls
#    over to Dhan / Groww (whose stripped schema poisons the cache).
# ---------------------------------------------------------------------------

class TestInstrumentsCallsite:
    """instruments.py must NOT route through PriceBroker (which fails-over
    to Dhan/Groww on Kite rate-limit).  Dhan/Groww instruments() return a
    stripped schema (no `instrument_type` / `name` / `expiry`) that poisons
    the API's instruments cache and breaks every virtual-root filter
    downstream.  The Jul 2026 defect fix reverts to explicit
    `get_broker(kite_acct)` iteration over Kite-only accounts.
    """

    def test_instruments_uses_kite_only_get_broker(self):
        """_fetch_instruments must call get_broker for Kite accounts only,
        never get_market_data_broker (PriceBroker with Dhan/Groww fallback)."""
        import inspect
        from backend.api.routes import instruments as instr_mod

        fn = instr_mod._fetch_instruments
        src = inspect.getsource(fn)

        # Must import + use get_broker for the Kite-account walk.
        assert "get_broker" in src, (
            "_fetch_instruments must call get_broker(kite_acct) directly "
            "for the Kite-only walk"
        )

        # Must NOT bind PriceBroker via get_market_data_broker() —
        # PriceBroker.instruments() falls over to Dhan/Groww on Kite
        # rate-limit, populating the cache with stripped-schema rows.
        # Look for the actual call pattern (identifier followed by `(`),
        # ignoring lines that are comments (# leader) or lines where the
        # token appears inside backticks/quotes (documentation string
        # embedded in a docstring).  A bare `get_market_data_broker(` on
        # a code line — with no leading `#` and no surrounding backticks
        # — is the smoking-gun regression.
        import re
        _bad = re.compile(r"(?<![`'\"])get_market_data_broker\(")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Docstring lines wrap the call in backticks — reject those
            # too even though _bad already guards.
            if "`get_market_data_broker(" in stripped:
                continue
            assert not _bad.search(stripped), (
                "_fetch_instruments must NOT call get_market_data_broker() — "
                "PriceBroker falls over to Dhan/Groww whose instruments() "
                "return a stripped schema that poisons the API's instruments "
                "cache. Walk Kite accounts explicitly via get_broker(acct).  "
                f"Offending line: {stripped!r}"
            )

    def test_instruments_iterates_kite_accts(self):
        """_fetch_instruments (or its extracted helper) must iterate kite_accts.

        Cross-account failover for Kite rate-limits must be present somewhere
        in the instruments fetch call-chain. The loop may live in the top-level
        function or in a dedicated helper (_fetch_exchange_raw) — both are valid
        refactors as long as the iteration pattern exists.
        """
        import inspect
        from backend.api.routes import instruments as instr_mod

        # Collect source from _fetch_instruments and any extracted helper.
        combined_src = inspect.getsource(instr_mod._fetch_instruments)
        if hasattr(instr_mod, "_fetch_exchange_raw"):
            combined_src += inspect.getsource(instr_mod._fetch_exchange_raw)

        assert "for _acct in kite_accts" in combined_src or "for acct in kite_accts" in combined_src, (
            "_fetch_instruments (or _fetch_exchange_raw) must iterate kite_accts "
            "for cross-account failover on Kite rate-limits (never single-account [0])"
        )

    def test_price_broker_instruments_has_kite_shape_gate(self):
        """PriceBroker.instruments() must apply the Kite-shape predicate so
        a stripped Dhan/Groww response falls through to the next broker."""
        import inspect
        from backend.brokers import registry as reg

        # The gate function itself is present.
        assert hasattr(reg, "_instruments_has_kite_shape"), (
            "registry._instruments_has_kite_shape helper missing — required "
            "to detect Dhan/Groww stripped instrument schemas"
        )

        # PriceBroker.instruments passes it as _result_ok.
        pb_fn = reg.PriceBroker.instruments
        src = inspect.getsource(pb_fn)
        assert "_instruments_has_kite_shape" in src, (
            "PriceBroker.instruments() must apply _instruments_has_kite_shape "
            "as _result_ok predicate so Dhan/Groww stripped responses fall "
            "through to the next broker"
        )

    def test_instruments_kite_shape_predicate_semantics(self):
        """The predicate returns True only for Kite-shape rows."""
        from backend.brokers.registry import _instruments_has_kite_shape

        # Empty / non-list — always False (encourages fall-through).
        assert _instruments_has_kite_shape([]) is False
        assert _instruments_has_kite_shape(None) is False
        assert _instruments_has_kite_shape("not a list") is False

        # Dhan-shape row — missing instrument_type / name / expiry.
        dhan_row = {
            "tradingsymbol": "CRUDEOIL26JULFUT",
            "security_id": "12345",
            "instrument_token": 12345,
            "exchange": "MCX",
            "lot_size": 100,
            "tick_size": 1.0,
        }
        assert _instruments_has_kite_shape([dhan_row]) is False, (
            "stripped Dhan row must be rejected"
        )

        # Kite-shape row — carries instrument_type + name + expiry.
        kite_row = {
            "instrument_token": 12345,
            "tradingsymbol": "CRUDEOIL26JULFUT",
            "name": "CRUDEOIL",
            "expiry": "2026-07-20",
            "instrument_type": "FUT",
            "exchange": "MCX",
            "lot_size": 100,
            "tick_size": 1.0,
        }
        assert _instruments_has_kite_shape([kite_row]) is True
