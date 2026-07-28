"""
Tests for the MARKET order_type guard in KiteBroker.place_gtt.

Behavior change (2026-07-27): place_gtt no longer silently coerces MARKET→LIMIT.
Instead it raises BrokerCapabilityError so callers see a clear capability error
rather than having the order type changed under them.

Covers five quality dimensions:
  SSOT        — capability guard lives only in the place_gtt leg-validation loop
  Correctness — MARKET leg raises BrokerCapabilityError; non-MARKET legs proceed
  Performance — error is raised before the SDK call; no network round-trip wasted
  Reuse       — BrokerCapabilityError propagates to template_attach plan.notes
  UX          — operator sees "Kite GTT does not support MARKET" error message

Scenario catalogue:
  1. Single MARKET leg → BrokerCapabilityError raised; SDK never called.
  2. Mixed legs (MARKET + LIMIT) → BrokerCapabilityError raised on MARKET leg.
  3. Multiple MARKET legs → BrokerCapabilityError raised on the first MARKET leg.
  4. Non-MARKET leg (LIMIT) → no error raised; SDK called once.
  5. Non-MARKET leg (SL) → no error raised; SL passes through unchanged.
  6. BrokerCapabilityError message mentions "MARKET" and "LIMIT".
  7. BrokerCapabilityError has broker="zerodha_kite" set correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.brokers.adapters.kite import KiteBroker
from backend.brokers.errors import BrokerCapabilityError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def kite_adapter():
    """KiteBroker with the underlying KiteConnection replaced by a MagicMock.

    KiteBroker.kite is a read-only property that delegates to
    self._conn.get_kite_conn(). We mock _conn so that the property
    returns a MagicMock whose place_gtt returns a minimal valid response.
    """
    mock_conn = MagicMock()
    mock_conn.account = "ZG0790"
    mock_sdk = MagicMock()
    mock_sdk.place_gtt.return_value = {"trigger_id": 42}
    mock_conn.get_kite_conn.return_value = mock_sdk

    adapter = KiteBroker.__new__(KiteBroker)
    adapter._conn = mock_conn
    return adapter


def _call_place_gtt(adapter, orders):
    """Helper: call place_gtt with a minimal valid single-trigger setup."""
    return adapter.place_gtt(
        trigger_type="single",
        tradingsymbol="NIFTY24JUNFUT",
        exchange="NFO",
        last_price=22500.0,
        trigger_values=[22000.0],
        orders=orders,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGttMarketCoerce:

    def test_market_leg_raises_capability_error(self, kite_adapter):
        """MARKET leg raises BrokerCapabilityError; SDK is never called."""
        leg = {"order_type": "MARKET", "quantity": 1, "price": 22000.0}
        with pytest.raises(BrokerCapabilityError):
            _call_place_gtt(kite_adapter, [leg])
        kite_adapter.kite.place_gtt.assert_not_called()

    def test_mixed_legs_market_raises_before_sdk(self, kite_adapter):
        """A mixed-leg GTT with a MARKET leg raises before the SDK call."""
        market_leg = {"order_type": "MARKET", "quantity": 1, "price": 22000.0}
        limit_leg = {"order_type": "LIMIT", "quantity": 1, "price": 22000.0}
        with pytest.raises(BrokerCapabilityError):
            _call_place_gtt(kite_adapter, [market_leg, limit_leg])
        kite_adapter.kite.place_gtt.assert_not_called()

    def test_multiple_market_legs_raise_on_first(self, kite_adapter):
        """Multiple MARKET legs — BrokerCapabilityError raised on the first one."""
        legs = [
            {"order_type": "MARKET", "quantity": 1, "price": 22000.0},
            {"order_type": "MARKET", "quantity": 2, "price": 22000.0},
        ]
        with pytest.raises(BrokerCapabilityError):
            _call_place_gtt(kite_adapter, legs)
        kite_adapter.kite.place_gtt.assert_not_called()

    def test_limit_leg_not_mutated(self, kite_adapter):
        """A LIMIT leg passes through unchanged and SDK is called."""
        leg = {"order_type": "LIMIT", "quantity": 1, "price": 22000.0}
        _call_place_gtt(kite_adapter, [leg])
        assert leg["order_type"] == "LIMIT"
        kite_adapter.kite.place_gtt.assert_called_once()

    def test_sl_leg_not_mutated(self, kite_adapter):
        """An SL leg is not touched; SDK is called normally."""
        leg = {
            "order_type": "SL",
            "quantity": 1,
            "price": 21900.0,
            "trigger_price": 22000.0,
        }
        _call_place_gtt(kite_adapter, [leg])
        assert leg["order_type"] == "SL"

    def test_capability_error_message_mentions_market_and_limit(self, kite_adapter):
        """BrokerCapabilityError message is operator-readable and mentions MARKET/LIMIT."""
        leg = {"order_type": "MARKET", "quantity": 1, "price": 22000.0}
        with pytest.raises(BrokerCapabilityError) as exc_info:
            _call_place_gtt(kite_adapter, [leg])
        msg = str(exc_info.value)
        assert "MARKET" in msg, f"Expected 'MARKET' in error message: {msg!r}"
        assert "LIMIT" in msg, f"Expected 'LIMIT' in error message: {msg!r}"

    def test_capability_error_has_broker_tag(self, kite_adapter):
        """BrokerCapabilityError carries broker='zerodha_kite'."""
        leg = {"order_type": "MARKET", "quantity": 1, "price": 22000.0}
        with pytest.raises(BrokerCapabilityError) as exc_info:
            _call_place_gtt(kite_adapter, [leg])
        assert exc_info.value.broker == "zerodha_kite", (
            f"Expected broker='zerodha_kite', got {exc_info.value.broker!r}"
        )
