"""
Tests for the per-broker GTT exchange-capability guard —
`validate_gtt_exchange` on each adapter.

Bracket/Template-GTT removal (T3.1, 2026-11): this file used to also
cover `apply_plan_live`'s pre-SDK-call validation integration and
`apply_template_to_order`'s pre-attach MCX guard — both removed along
with `backend/api/algo/template_attach.py`, since `validate_gtt_exchange`
had no other real caller. `TestValidateGttExchange` below is unaffected:
it tests the adapter methods directly, which are KEPT — `orders_gtt.py`
is a standalone, Bracket-independent "view/cancel any broker GTT"
feature, and `validate_gtt_exchange` is part of the broker ABC
regardless of whether anything currently calls it for a live GTT
placement.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from backend.brokers.adapters.kite import KiteBroker
from backend.brokers.adapters.dhan import DhanBroker
from backend.brokers.adapters.groww import GrowwBroker


class TestValidateGttExchange:
    """Test the validate_gtt_exchange method on each broker adapter."""

    def test_kite_mcx_passes(self):
        """Kite broker allows MCX GTT — no exception raised."""
        adapter = MagicMock(spec=KiteBroker)
        adapter.validate_gtt_exchange = KiteBroker.validate_gtt_exchange.__get__(adapter)
        # Should not raise
        adapter.validate_gtt_exchange("MCX")

    def test_kite_nco_passes(self):
        """Kite broker allows NCO GTT — no exception raised."""
        adapter = MagicMock(spec=KiteBroker)
        adapter.validate_gtt_exchange = KiteBroker.validate_gtt_exchange.__get__(adapter)
        # Should not raise
        adapter.validate_gtt_exchange("NCO")

    def test_kite_nse_passes(self):
        """Kite broker allows NSE GTT — no exception raised."""
        adapter = MagicMock(spec=KiteBroker)
        adapter.validate_gtt_exchange = KiteBroker.validate_gtt_exchange.__get__(adapter)
        # Should not raise
        adapter.validate_gtt_exchange("NSE")

    def test_dhan_mcx_raises(self):
        """Dhan broker rejects MCX GTT — ValueError raised."""
        adapter = DhanBroker.__new__(DhanBroker)
        with pytest.raises(ValueError, match="MCX"):
            adapter.validate_gtt_exchange("MCX")

    def test_dhan_nco_raises(self):
        """Dhan broker rejects NCO GTT — ValueError raised."""
        adapter = DhanBroker.__new__(DhanBroker)
        with pytest.raises(ValueError, match="NCO"):
            adapter.validate_gtt_exchange("NCO")

    def test_dhan_nse_passes(self):
        """Dhan broker allows NSE GTT — no exception raised."""
        adapter = MagicMock(spec=DhanBroker)
        adapter.validate_gtt_exchange = DhanBroker.validate_gtt_exchange.__get__(adapter)
        # Should not raise
        adapter.validate_gtt_exchange("NSE")

    def test_groww_mcx_raises(self):
        """Groww broker rejects MCX GTT — ValueError raised."""
        adapter = GrowwBroker.__new__(GrowwBroker)
        with pytest.raises(ValueError, match="MCX"):
            adapter.validate_gtt_exchange("MCX")

    def test_groww_nco_raises(self):
        """Groww broker rejects NCO GTT — ValueError raised."""
        adapter = GrowwBroker.__new__(GrowwBroker)
        with pytest.raises(ValueError, match="NCO"):
            adapter.validate_gtt_exchange("NCO")

    def test_groww_nse_passes(self):
        """Groww broker allows NSE GTT — no exception raised."""
        adapter = MagicMock(spec=GrowwBroker)
        adapter.validate_gtt_exchange = GrowwBroker.validate_gtt_exchange.__get__(adapter)
        # Should not raise
        adapter.validate_gtt_exchange("NSE")
