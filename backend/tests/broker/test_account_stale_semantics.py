"""Tests for `account_stale` semantics (2026-09-29 fix).

`account_stale=True` must mean ONLY "breaker open OR a genuine fetch
exception" — never a routine Dhan interval-gate cadence throttle.

Root cause: `_stale_substitute_frame()` unconditionally set
`df['account_stale'] = True` on every non-empty substitution, including
the interval-skip call sites in `_fetch_margins_local`,
`_fetch_holdings_local`, and `_fetch_positions_local` — even though
those same call sites already recognised interval-skip as "not really
stale" for the `circuit_open` attr (`lkg.attrs.pop("circuit_open",
None)`). Fix: `_stale_substitute_frame` gained a `mark_stale` kwarg
(default True); the three interval-skip call sites pass
`mark_stale=False`.

Covers both the direct unit-level contract of `_stale_substitute_frame`
and the end-to-end behaviour of all three `_fetch_*_local` functions.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from backend.brokers import broker_apis
from backend.brokers.broker_apis import (
    _fetch_holdings_local,
    _fetch_margins_local,
    _fetch_positions_local,
    _record_lkg_frame,
    _stale_substitute_frame,
)

ACCOUNT = "ACCTSTALE_TEST"


def _reset(account: str = ACCOUNT) -> None:
    for kind in ("holdings", "positions", "margins"):
        broker_apis._LKG_FRAME_BY_ACCT.pop((kind, account), None)
        broker_apis._DB_LKG_CACHE.pop((kind, account), None)
    broker_apis._FETCH_HEALTH.pop(account, None)
    broker_apis._dhan_next_poll.pop(account, None)


@pytest.fixture(autouse=True)
def _clean():
    _reset()
    yield
    _reset()


def _make_dhan_broker() -> MagicMock:
    """Mock whose class name contains 'dhan' so _is_dhan_interval_due
    treats it as Dhan-gated (same helper pattern as
    test_margins_ssot_ttl.py)."""
    m = MagicMock()
    m.__class__.__name__ = "DhanBroker"
    return m


# ---------------------------------------------------------------------------
# Unit-level: _stale_substitute_frame's new `mark_stale` kwarg
# ---------------------------------------------------------------------------

class TestStaleSubstituteFrameMarkStale:
    def test_mark_stale_true_default_sets_account_stale_column(self):
        kind, account = "positions", "UNIT_TEST_MARK_STALE"
        broker_apis._LKG_FRAME_BY_ACCT.pop((kind, account), None)
        seed = pd.DataFrame([{"tradingsymbol": "TCS", "quantity": 1, "account": account}])
        _record_lkg_frame(kind, account, seed)
        try:
            df = _stale_substitute_frame(kind, account)
            assert not df.empty
            assert bool(df["account_stale"].iloc[0]) is True
        finally:
            broker_apis._LKG_FRAME_BY_ACCT.pop((kind, account), None)

    def test_mark_stale_false_omits_account_stale_column_value(self):
        kind, account = "positions", "UNIT_TEST_MARK_STALE_FALSE"
        broker_apis._LKG_FRAME_BY_ACCT.pop((kind, account), None)
        seed = pd.DataFrame([{"tradingsymbol": "TCS", "quantity": 1, "account": account}])
        _record_lkg_frame(kind, account, seed)
        try:
            df = _stale_substitute_frame(kind, account, mark_stale=False)
            assert not df.empty
            # Either the column is absent, or present but not True for this row.
            if "account_stale" in df.columns:
                assert bool(df["account_stale"].iloc[0]) is not True
            # Response-level staleness attrs are untouched by mark_stale.
            assert df.attrs.get("stale") is True
        finally:
            broker_apis._LKG_FRAME_BY_ACCT.pop((kind, account), None)


# ---------------------------------------------------------------------------
# End-to-end: _fetch_margins_local
# ---------------------------------------------------------------------------

class TestFetchMarginsLocalAccountStale:
    def _seed_margins_lkg(self):
        seed = pd.DataFrame([{
            "account": ACCOUNT, "type": "C",
            "avail cash": 1000.0, "util debits": 0.0,
        }])
        _record_lkg_frame("margins", ACCOUNT, seed)

    def test_interval_skip_does_not_mark_account_stale(self):
        """Dhan interval-gate throttle substitution must NOT carry
        account_stale=True — it's a cadence skip, not a failure."""
        self._seed_margins_lkg()
        broker_apis._dhan_next_poll[ACCOUNT] = broker_apis._time.time() + 3600.0

        dhan_broker = _make_dhan_broker()
        undecorated = _fetch_margins_local.__wrapped__
        df = undecorated(account=ACCOUNT, broker=dhan_broker, kite=None)

        assert not df.empty, "interval-skip with an existing LKG must substitute, not go empty"
        assert df.attrs.get("interval_skipped") is True
        if "account_stale" in df.columns:
            assert bool(df["account_stale"].iloc[0]) is not True, (
                "interval-skip substitution must not carry account_stale=True"
            )

    def test_circuit_breaker_open_still_marks_account_stale(self, monkeypatch):
        """Regression: a genuine breaker-open substitution must still
        carry account_stale=True."""
        self._seed_margins_lkg()
        monkeypatch.setattr(broker_apis, "_is_circuit_open", lambda acct: acct == ACCOUNT)

        dhan_broker = _make_dhan_broker()
        undecorated = _fetch_margins_local.__wrapped__
        df = undecorated(account=ACCOUNT, broker=dhan_broker, kite=None)

        assert not df.empty
        assert bool(df["account_stale"].iloc[0]) is True

    def test_fetch_exception_still_marks_account_stale(self):
        """Regression: a genuine fetch exception substitution must still
        carry account_stale=True."""
        self._seed_margins_lkg()

        broker = MagicMock()
        broker.__class__.__name__ = "KiteBroker"  # bypass Dhan interval gate
        broker.margins.side_effect = RuntimeError("network timeout")

        undecorated = _fetch_margins_local.__wrapped__
        df = undecorated(account=ACCOUNT, broker=broker, kite=None)

        assert not df.empty
        assert bool(df["account_stale"].iloc[0]) is True


# ---------------------------------------------------------------------------
# End-to-end: _fetch_holdings_local (same fix applied for consistency)
# ---------------------------------------------------------------------------

class TestFetchHoldingsLocalAccountStale:
    def _seed_holdings_lkg(self):
        seed = pd.DataFrame([{
            "account": ACCOUNT, "type": "H", "tradingsymbol": "TCS",
            "quantity": 1, "average_price": 100.0,
        }])
        _record_lkg_frame("holdings", ACCOUNT, seed)

    def test_interval_skip_does_not_mark_account_stale(self):
        self._seed_holdings_lkg()
        broker_apis._dhan_next_poll[ACCOUNT] = broker_apis._time.time() + 3600.0

        dhan_broker = _make_dhan_broker()
        undecorated = _fetch_holdings_local.__wrapped__
        df = undecorated(account=ACCOUNT, broker=dhan_broker, kite=None)

        assert not df.empty
        assert df.attrs.get("interval_skipped") is True
        if "account_stale" in df.columns:
            assert bool(df["account_stale"].iloc[0]) is not True

    def test_circuit_breaker_open_still_marks_account_stale(self, monkeypatch):
        self._seed_holdings_lkg()
        monkeypatch.setattr(broker_apis, "_is_circuit_open", lambda acct: acct == ACCOUNT)

        dhan_broker = _make_dhan_broker()
        undecorated = _fetch_holdings_local.__wrapped__
        df = undecorated(account=ACCOUNT, broker=dhan_broker, kite=None)

        assert not df.empty
        assert bool(df["account_stale"].iloc[0]) is True

    def test_fetch_exception_does_not_regress_existing_behaviour(self):
        """`_fetch_holdings_local`'s exception branch does NOT call
        `_stale_substitute_frame` at all (unlike margins/positions) —
        this is a pre-existing gap out of scope for this fix. Confirm
        this fix did not change that: exception still returns an empty,
        fetch_failed frame."""
        self._seed_holdings_lkg()

        broker = MagicMock()
        broker.__class__.__name__ = "KiteBroker"
        broker.holdings.side_effect = RuntimeError("network timeout")

        undecorated = _fetch_holdings_local.__wrapped__
        df = undecorated(account=ACCOUNT, broker=broker, kite=None)

        assert df.empty
        assert df.attrs.get("fetch_failed") is True


# ---------------------------------------------------------------------------
# End-to-end: _fetch_positions_local (same fix applied for consistency)
# ---------------------------------------------------------------------------

class TestFetchPositionsLocalAccountStale:
    def _seed_positions_lkg(self):
        seed = pd.DataFrame([{
            "account": ACCOUNT, "type": "P", "tradingsymbol": "TCS",
            "quantity": 1, "average_price": 100.0, "exchange": "NSE",
            "product": "MIS", "pnl": 0.0, "multiplier": 1,
        }])
        _record_lkg_frame("positions", ACCOUNT, seed)

    def test_interval_skip_does_not_mark_account_stale(self):
        self._seed_positions_lkg()
        broker_apis._dhan_next_poll[ACCOUNT] = broker_apis._time.time() + 3600.0

        dhan_broker = _make_dhan_broker()
        undecorated = _fetch_positions_local.__wrapped__
        df = undecorated(account=ACCOUNT, broker=dhan_broker, kite=None)

        assert not df.empty
        assert df.attrs.get("interval_skipped") is True
        if "account_stale" in df.columns:
            assert bool(df["account_stale"].iloc[0]) is not True

    def test_circuit_breaker_open_still_marks_account_stale(self, monkeypatch):
        self._seed_positions_lkg()
        monkeypatch.setattr(broker_apis, "_is_circuit_open", lambda acct: acct == ACCOUNT)

        dhan_broker = _make_dhan_broker()
        undecorated = _fetch_positions_local.__wrapped__
        df = undecorated(account=ACCOUNT, broker=dhan_broker, kite=None)

        assert not df.empty
        assert bool(df["account_stale"].iloc[0]) is True

    def test_fetch_exception_still_marks_account_stale(self):
        self._seed_positions_lkg()

        broker = MagicMock()
        broker.__class__.__name__ = "KiteBroker"
        broker.positions.side_effect = RuntimeError("network timeout")

        undecorated = _fetch_positions_local.__wrapped__
        df = undecorated(account=ACCOUNT, broker=broker, kite=None)

        assert not df.empty
        assert bool(df["account_stale"].iloc[0]) is True
