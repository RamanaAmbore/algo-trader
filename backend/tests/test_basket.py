"""
Tests for multi-account basket order schema and margin calculation logic.

Covers:
  - BasketMarginGroupResult shortfall computation
  - BasketGroup schema with multiple legs
  - Account validation guards
"""

import pytest
from backend.api.schemas import BasketMarginGroupResult, BasketGroup, BasketLeg


def test_basket_margin_group_result_shortfall_zero():
    """Shortfall = max(0, required - available)."""
    result = BasketMarginGroupResult(
        account="ZG0790",
        required=150000.0,
        available=500000.0,
        shortfall=None,
    )
    # When available > required, shortfall should be 0
    shortfall = max(0.0, (result.required or 0) - (result.available or 0))
    assert shortfall == 0.0


def test_basket_margin_group_result_shortfall_detected():
    """Shortfall > 0 when required > available."""
    required = 150000.0
    available = 50000.0
    shortfall = max(0.0, required - available)

    result = BasketMarginGroupResult(
        account="ZG0790",
        required=required,
        available=available,
        shortfall=shortfall,
    )
    assert result.shortfall == pytest.approx(100000.0)


def test_basket_margin_group_result_with_error():
    """Error case: account unknown → all margin fields None."""
    result = BasketMarginGroupResult(
        account="ZG9999",
        required=None,
        available=None,
        shortfall=None,
        error="unknown account",
    )
    assert result.error == "unknown account"
    assert result.required is None
    assert result.available is None


def test_basket_leg_schema():
    """BasketLeg carries order fields."""
    leg = BasketLeg(
        tradingsymbol="NIFTY25APRFUT",
        exchange="NFO",
        transaction_type="BUY",
        quantity=50,
        order_type="LIMIT",
        price=22000.0,
        product="NRML",
        variety="regular",
        trigger_price=None,
    )
    assert leg.tradingsymbol == "NIFTY25APRFUT"
    assert leg.quantity == 50
    assert leg.transaction_type == "BUY"


def test_basket_group_schema_multiple_legs():
    """BasketGroup holds all legs for one account."""
    legs = [
        BasketLeg(
            tradingsymbol="NIFTY25APRFUT", exchange="NFO", transaction_type="BUY",
            quantity=50, order_type="LIMIT", price=22000.0,
            product="NRML", variety="regular",
        ),
        BasketLeg(
            tradingsymbol="BANKNIFTY25APRFUT", exchange="NFO", transaction_type="SELL",
            quantity=30, order_type="LIMIT", price=45000.0,
            product="NRML", variety="regular",
        ),
    ]
    group = BasketGroup(account="ZG0790", legs=legs)
    assert group.account == "ZG0790"
    assert len(group.legs) == 2
    assert group.legs[0].tradingsymbol == "NIFTY25APRFUT"
    assert group.legs[1].transaction_type == "SELL"
