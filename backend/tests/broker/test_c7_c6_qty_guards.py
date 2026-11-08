"""Tests for the C7 / C6 silent-oversize order-safety fixes.

C7 — backend/brokers/base.py:_exchange_contracts_to_wire
     Sub-lot MCX/NCO qty (contracts < lot_size) and non-multiple qty
     (contracts % lot_size != 0) must raise ValueError instead of
     silently passing through unchanged / silently flooring.

C6  — backend/brokers/adapters/kite.py:modify_gtt
     Must apply the same _check_kite_gtt_qty_ceiling last-line-defense
     that place_gtt already applies, before calling self.kite.modify_gtt.

C6b — backend/brokers/adapters/dhan.py:modify_gtt
     Must apply the same NFO/BFO qty ceiling that place_gtt already
     applies (via the shared _check_dhan_gtt_qty_ceiling helper), across
     ALL legs before either SDK modify_forever call — an oversized
     target leg must not be allowed to fire after the entry leg was
     already modified (would leave an asymmetric OCO on the book).
"""

from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from unittest.mock import MagicMock, patch

import pytest

from backend.brokers.base import (
    InstrumentsCacheMissError,
    _exchange_contracts_to_wire,
    _validate_exchange_qty_multiple,
)
from backend.brokers.adapters.kite import KiteBroker


# ---------------------------------------------------------------------------
# C7 — _exchange_contracts_to_wire direct tests
# ---------------------------------------------------------------------------

class TestExchangeContractsToWireGuards:
    def test_mcx_sub_lot_raises(self):
        """contracts=50 < lot_size=100 on MCX must raise, not pass through."""
        with pytest.raises(ValueError, match="QTY-GUARD"):
            _exchange_contracts_to_wire("MCX", 50, 100)

    def test_nco_sub_lot_raises(self):
        with pytest.raises(ValueError, match="QTY-GUARD"):
            _exchange_contracts_to_wire("NCO", 10, 100)

    def test_mcx_non_multiple_raises(self):
        """contracts=150 is not a whole multiple of lot_size=100 — must raise,
        not silently floor to 1 lot and discard the 50-contract remainder."""
        with pytest.raises(ValueError, match="QTY-GUARD"):
            _exchange_contracts_to_wire("MCX", 150, 100)

    def test_mcx_clean_multiple_converts(self):
        """contracts=200 is a clean 2× multiple of lot_size=100 → 2 lots."""
        assert _exchange_contracts_to_wire("MCX", 200, 100) == 2

    def test_mcx_single_lot_clean_multiple(self):
        assert _exchange_contracts_to_wire("MCX", 100, 100) == 1

    def test_mcx_zero_qty_returns_zero(self):
        """contracts=0 is a clean multiple of any lot_size — must not raise."""
        assert _exchange_contracts_to_wire("MCX", 0, 100) == 0

    def test_nfo_non_multiple_qty_unaffected(self):
        """Non-MCX/NCO exchanges are a no-op passthrough regardless of
        lot_size divisibility — NFO ships contracts, not lots."""
        assert _exchange_contracts_to_wire("NFO", 37, 50) == 37

    def test_lot_size_le_1_still_raises(self):
        """Existing lot_size<=1 cache-miss guard is unchanged by this fix."""
        with pytest.raises(ValueError, match="QTY-GUARD"):
            _exchange_contracts_to_wire("MCX", 100, 1)

    def test_via_kite_broker_translate_qty_sub_lot_raises(self):
        """End-to-end through KiteBroker.translate_qty (the real call path)."""
        broker = KiteBroker.__new__(KiteBroker)
        broker._conn = MagicMock()
        with pytest.raises(ValueError, match="QTY-GUARD"):
            broker.translate_qty("MCX", 50, 100)


# ---------------------------------------------------------------------------
# _validate_exchange_qty_multiple — the validation-only helper split out of
# _exchange_contracts_to_wire (2026-09 orders-page council audit finding #6)
# so an adapter that does NOT convert contracts to lots on the wire (Groww)
# can still apply the same malformed-qty guard.
# ---------------------------------------------------------------------------

class TestValidateExchangeQtyMultiple:
    def test_mcx_sub_lot_raises(self):
        with pytest.raises(ValueError, match="QTY-GUARD"):
            _validate_exchange_qty_multiple("MCX", 50, 100)

    def test_mcx_non_multiple_raises(self):
        with pytest.raises(ValueError, match="QTY-GUARD"):
            _validate_exchange_qty_multiple("MCX", 150, 100)

    def test_lot_size_le_1_raises(self):
        with pytest.raises(ValueError, match="QTY-GUARD"):
            _validate_exchange_qty_multiple("MCX", 100, 1)

    def test_mcx_zero_qty_noop(self):
        _validate_exchange_qty_multiple("MCX", 0, 100)  # must not raise

    def test_mcx_clean_multiple_noop(self):
        _validate_exchange_qty_multiple("MCX", 200, 100)  # must not raise

    def test_non_mcx_exchange_always_noop(self):
        """Non-MCX/NCO exchanges never validate, regardless of divisibility —
        this is the exact behavior Groww's translate_qty relies on for NSE/NFO."""
        _validate_exchange_qty_multiple("NFO", 37, 50)  # must not raise

    def test_via_kite_broker_translate_qty_non_multiple_raises(self):
        broker = KiteBroker.__new__(KiteBroker)
        broker._conn = MagicMock()
        with pytest.raises(ValueError, match="QTY-GUARD"):
            broker.translate_qty("MCX", 150, 100)

    def test_via_kite_broker_translate_qty_clean_multiple_ok(self):
        broker = KiteBroker.__new__(KiteBroker)
        broker._conn = MagicMock()
        assert broker.translate_qty("MCX", 200, 100) == 2


# ---------------------------------------------------------------------------
# InstrumentsCacheMissError — narrow typing regression guard (2026-11-02).
#
# Only the `lot_size<=1` condition (an instruments-cache miss, transient —
# the cache self-warms within a few minutes) is worth retrying by a future
# caller. The other two conditions (sub-lot qty, non-multiple qty) are
# permanent data/logic problems — retrying would never fix them, so they
# must stay plain ValueError and must NEVER become InstrumentsCacheMissError
# by accident.
# ---------------------------------------------------------------------------

class TestInstrumentsCacheMissErrorNarrowTyping:
    def test_lot_size_le_1_raises_instruments_cache_miss_error(self):
        """The transient cache-miss condition raises the narrow subtype."""
        with pytest.raises(InstrumentsCacheMissError, match="QTY-GUARD"):
            _validate_exchange_qty_multiple("MCX", 100, 1)

    def test_lot_size_le_1_is_still_a_value_error(self):
        """InstrumentsCacheMissError must remain a ValueError subclass so
        every existing bare `except ValueError` catch around translate_qty /
        _exchange_contracts_to_wire / _validate_exchange_qty_multiple keeps
        working unchanged."""
        try:
            _validate_exchange_qty_multiple("MCX", 100, 1)
        except InstrumentsCacheMissError as exc:
            assert isinstance(exc, ValueError)
        else:
            pytest.fail("expected InstrumentsCacheMissError to be raised")

    def test_lot_size_zero_also_raises_instruments_cache_miss_error(self):
        """lot_size=0 (the real-world Kite cache-miss sentinel, not just
        lot_size=1) hits the same `lot_size <= 1` branch."""
        with pytest.raises(InstrumentsCacheMissError, match="QTY-GUARD"):
            _validate_exchange_qty_multiple("MCX", 100, 0)

    def test_sub_lot_qty_stays_plain_value_error_not_cache_miss(self):
        """Sub-lot qty (permanent data problem) must NOT become
        InstrumentsCacheMissError — it must never be retried."""
        with pytest.raises(ValueError, match="QTY-GUARD") as exc_info:
            _validate_exchange_qty_multiple("MCX", 50, 100)
        assert not isinstance(exc_info.value, InstrumentsCacheMissError)

    def test_non_multiple_qty_stays_plain_value_error_not_cache_miss(self):
        """Non-multiple qty (permanent data problem) must NOT become
        InstrumentsCacheMissError — it must never be retried."""
        with pytest.raises(ValueError, match="QTY-GUARD") as exc_info:
            _validate_exchange_qty_multiple("MCX", 150, 100)
        assert not isinstance(exc_info.value, InstrumentsCacheMissError)


# C6 (Kite modify_gtt qty ceiling, TestKiteModifyGttQtyCeiling) and C6b
# (Dhan modify_gtt qty ceiling, TestDhanModifyGttQtyCeiling) removed
# (Bracket/Template-GTT removal, T3.1, 2026-11) — KiteBroker.modify_gtt
# and DhanBroker.modify_gtt were deleted.
