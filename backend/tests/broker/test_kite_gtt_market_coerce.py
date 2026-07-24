"""
Tests for the MARKET→LIMIT coercion guard in KiteBroker.place_gtt.

Covers five quality dimensions:
  SSOT        — coercion lives only in the place_gtt leg-validation loop; no duplicate logic
  Correctness — MARKET leg is mutated to LIMIT in-place; non-MARKET legs are untouched
  Performance — coercion emits a warning log but does not raise; call proceeds to SDK
  Reuse       — mutation propagates into enriched_orders sent to the SDK
  UX          — no exception surface to the caller; silent auto-correction with log evidence

Scenario catalogue:
  1. Single MARKET leg → coerced to LIMIT; SDK called once.
  2. Mixed legs (MARKET + LIMIT) → only MARKET leg is mutated; LIMIT leg unchanged.
  3. Multiple MARKET legs → all coerced to LIMIT.
  4. Non-MARKET leg (LIMIT) → no coercion; LIMIT unchanged.
  5. Non-MARKET leg (SL) → no coercion; SL unchanged (with required prices).
  6. Coercion warning logged at WARNING level.
  7. Coercion happens before enriched_orders is built → SDK receives LIMIT.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from backend.brokers.adapters.kite import KiteBroker


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

    def test_market_leg_coerced_to_limit(self, kite_adapter):
        """MARKET leg is mutated to LIMIT before SDK call; no exception raised."""
        leg = {"order_type": "MARKET", "quantity": 1, "price": 22000.0}
        _call_place_gtt(kite_adapter, [leg])
        assert leg["order_type"] == "LIMIT"
        kite_adapter.kite.place_gtt.assert_called_once()

    def test_mixed_legs_only_market_coerced(self, kite_adapter):
        """Only the MARKET leg is mutated; the LIMIT leg is left untouched."""
        market_leg = {"order_type": "MARKET", "quantity": 1, "price": 22000.0}
        limit_leg = {"order_type": "LIMIT", "quantity": 1, "price": 22000.0}
        _call_place_gtt(kite_adapter, [market_leg, limit_leg])
        assert market_leg["order_type"] == "LIMIT"
        assert limit_leg["order_type"] == "LIMIT"

    def test_multiple_market_legs_all_coerced(self, kite_adapter):
        """All MARKET legs in a multi-leg GTT are coerced to LIMIT."""
        legs = [
            {"order_type": "MARKET", "quantity": 1, "price": 22000.0},
            {"order_type": "MARKET", "quantity": 2, "price": 22000.0},
        ]
        _call_place_gtt(kite_adapter, legs)
        for leg in legs:
            assert leg["order_type"] == "LIMIT"

    def test_limit_leg_not_mutated(self, kite_adapter):
        """A LIMIT leg passes through unchanged."""
        leg = {"order_type": "LIMIT", "quantity": 1, "price": 22000.0}
        _call_place_gtt(kite_adapter, [leg])
        assert leg["order_type"] == "LIMIT"
        kite_adapter.kite.place_gtt.assert_called_once()

    def test_sl_leg_not_mutated(self, kite_adapter):
        """An SL leg is not touched by the MARKET coercion guard."""
        leg = {
            "order_type": "SL",
            "quantity": 1,
            "price": 21900.0,
            "trigger_price": 22000.0,
        }
        _call_place_gtt(kite_adapter, [leg])
        assert leg["order_type"] == "SL"

    def test_coercion_emits_warning(self, kite_adapter):
        """Warning is logged when MARKET is coerced.

        ramboq_logger sets propagate=False on named loggers, so caplog
        cannot intercept via the root handler. We patch logger.warning
        directly on the adapter module to verify the call was made with
        the expected content.
        """
        import backend.brokers.adapters.kite as _kite_mod
        leg = {"order_type": "MARKET", "quantity": 1, "price": 22000.0}
        with patch.object(_kite_mod.logger, "warning") as mock_warn:
            _call_place_gtt(kite_adapter, [leg])
        assert mock_warn.called, "logger.warning must be called for MARKET→LIMIT coercion"
        warned_text = " ".join(str(a) for a in mock_warn.call_args.args)
        assert "MARKET" in warned_text and "LIMIT" in warned_text, (
            f"Warning message did not mention MARKET→LIMIT: {warned_text!r}"
        )

    def test_sdk_receives_limit_not_market(self, kite_adapter):
        """The enriched_orders passed to the SDK carry order_type='LIMIT', not 'MARKET'."""
        leg = {"order_type": "MARKET", "quantity": 1, "price": 22000.0}
        _call_place_gtt(kite_adapter, [leg])
        _call_args = kite_adapter.kite.place_gtt.call_args
        enriched = _call_args.kwargs.get("orders") or _call_args.args[-1]
        for enriched_leg in enriched:
            assert enriched_leg.get("order_type") != "MARKET", (
                "SDK must not receive a MARKET order_type leg"
            )
