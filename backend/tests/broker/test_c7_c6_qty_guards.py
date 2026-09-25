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

from backend.brokers.base import _exchange_contracts_to_wire
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
# C6 — Kite modify_gtt qty ceiling
# ---------------------------------------------------------------------------

@pytest.fixture()
def kite_adapter():
    mock_conn = MagicMock()
    mock_conn.account = "ZG0790"
    mock_sdk = MagicMock()
    mock_sdk.modify_gtt.return_value = {"trigger_id": 42}
    mock_conn.get_kite_conn.return_value = mock_sdk

    adapter = KiteBroker.__new__(KiteBroker)
    adapter._conn = mock_conn
    return adapter, mock_sdk


class TestKiteModifyGttQtyCeiling:
    def _call_modify_gtt(self, adapter, orders):
        return adapter.modify_gtt(
            "42",
            trigger_type="single",
            tradingsymbol="CRUDEOIL26JUL7500CE",
            exchange="MCX",
            last_price=7500.0,
            orders=orders,
            trigger_values=[7400.0],
        )

    def test_modify_gtt_oversized_mcx_leg_raises_before_sdk_call(self, kite_adapter):
        """An untranslated raw-contract qty (well above the 200-lot ceiling)
        must be refused before self.kite.modify_gtt is ever invoked."""
        adapter, mock_sdk = kite_adapter
        with patch("backend.shared.helpers.settings.get_int", return_value=200):
            with pytest.raises(ValueError, match="absurd-value ceiling"):
                self._call_modify_gtt(
                    adapter,
                    [{"quantity": 500, "price": 7400.0, "order_type": "LIMIT"}],
                )
        mock_sdk.modify_gtt.assert_not_called()

    def test_modify_gtt_within_ceiling_calls_sdk(self, kite_adapter):
        """A legitimate in-lots qty under the ceiling must proceed to the SDK."""
        adapter, mock_sdk = kite_adapter
        with patch("backend.shared.helpers.settings.get_int", return_value=200):
            result = self._call_modify_gtt(
                adapter,
                [{"quantity": 2, "price": 7400.0, "order_type": "LIMIT"}],
            )
        mock_sdk.modify_gtt.assert_called_once()
        assert result == "42"


# ---------------------------------------------------------------------------
# C6b — Dhan modify_gtt qty ceiling
# ---------------------------------------------------------------------------

class TestDhanModifyGttQtyCeiling:
    def _make_broker(self):
        from backend.brokers.adapters.dhan import DhanBroker
        mock_conn = MagicMock()
        mock_conn.account = "DH6847"
        broker = DhanBroker.__new__(DhanBroker)
        broker._conn = mock_conn
        broker._last_req = {}
        broker._last_resp = {}
        return broker

    def test_modify_gtt_oversized_nfo_leg_raises_no_sdk_calls(self):
        """Oversized entry-leg qty must be refused before ANY modify_forever call."""
        broker = self._make_broker()
        mock_sdk_inst = MagicMock()
        mock_sdk_inst.modify_forever.return_value = {"status": "success"}
        with patch.object(
            type(broker), "_sdk_orders", new_callable=lambda: property(lambda self: mock_sdk_inst)
        ):
            with pytest.raises(ValueError, match="50000"):
                broker.modify_gtt(
                    "ORD001",
                    trigger_type="single",
                    tradingsymbol="NIFTY26JUL25000CE",
                    exchange="NFO",
                    last_price=100.0,
                    orders=[{"quantity": 60_000, "price": 100.0, "order_type": "LIMIT"}],
                    trigger_values=[100.0],
                )
        mock_sdk_inst.modify_forever.assert_not_called()

    def test_modify_gtt_oco_oversized_target_leg_makes_zero_sdk_calls(self):
        """OCO modify: an oversized TARGET leg must block the whole modify —
        including the entry leg — not just fail after the entry leg already
        landed (which would leave an asymmetric OCO on the book)."""
        broker = self._make_broker()
        mock_sdk_inst = MagicMock()
        mock_sdk_inst.modify_forever.return_value = {"status": "success"}
        with patch.object(
            type(broker), "_sdk_orders", new_callable=lambda: property(lambda self: mock_sdk_inst)
        ):
            with pytest.raises(ValueError, match="50000"):
                broker.modify_gtt(
                    "ORD001",
                    trigger_type="two-leg",
                    tradingsymbol="NIFTY26JUL25000CE",
                    exchange="NFO",
                    last_price=100.0,
                    orders=[
                        {"quantity": 100, "price": 100.0, "order_type": "LIMIT"},
                        {"quantity": 60_000, "price": 120.0, "order_type": "LIMIT"},
                    ],
                    trigger_values=[100.0, 120.0],
                )
        mock_sdk_inst.modify_forever.assert_not_called()

    def test_modify_gtt_within_ceiling_calls_sdk(self):
        broker = self._make_broker()
        mock_sdk_inst = MagicMock()
        mock_sdk_inst.modify_forever.return_value = {"status": "success"}
        with patch.object(
            type(broker), "_sdk_orders", new_callable=lambda: property(lambda self: mock_sdk_inst)
        ):
            result = broker.modify_gtt(
                "ORD001",
                trigger_type="single",
                tradingsymbol="NIFTY26JUL25000CE",
                exchange="NFO",
                last_price=100.0,
                orders=[{"quantity": 75, "price": 100.0, "order_type": "LIMIT"}],
                trigger_values=[100.0],
            )
        mock_sdk_inst.modify_forever.assert_called_once()
        assert result == "ORD001"
