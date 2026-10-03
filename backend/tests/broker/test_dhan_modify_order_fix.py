"""
Regression test for the 2026-10 Dhan `modify_order` audit fix (dhan.py).

`dhanhq` 2.2.0's real `modify_order(self, order_id, order_type, leg_name,
quantity, price, trigger_price, disclosed_quantity, validity)` signature
has NO default value for any parameter (verified via
`inspect.signature(dhanhq.dhanhq.modify_order)` against the installed
SDK). `DhanBroker.modify_order` pre-fix called it with only order_id/
quantity/price/trigger_price/order_type — a call missing `leg_name`,
`disclosed_quantity`, and `validity` raises
`TypeError: modify_order() missing 3 required positional arguments:
'leg_name', 'disclosed_quantity', and 'validity'` on EVERY call, so no
Dhan order modify (chase price update, take-profit ratchet, …) ever
reached the broker.

The fake SDK object's `modify_order` below has the REAL signature (no
defaults) so a regression back to the old call shape fails this test
with a TypeError exactly as it would against the live SDK — not a
permissive MagicMock that would silently re-mask the bug.
"""

from __future__ import annotations

import inspect

import pytest
from unittest.mock import MagicMock

from dhanhq import dhanhq as _dhanhq_class

from backend.brokers.adapters.dhan import DhanBroker


class _RealSignatureDhanSDK:
    """Stand-in SDK object exposing `modify_order` with dhanhq 2.2.0's
    real (no-default) positional signature, so a wrong-arity call from
    the adapter raises TypeError here just like it would live."""

    def __init__(self):
        self.last_call: dict = {}

    def modify_order(self, order_id, order_type, leg_name, quantity,
                      price, trigger_price, disclosed_quantity, validity):
        # Confirm this really matches the installed SDK's own signature
        # (belt-and-suspenders — catches drift if dhanhq is upgraded).
        inspect.signature(_dhanhq_class.modify_order).bind(
            self, order_id, order_type, leg_name, quantity, price,
            trigger_price, disclosed_quantity, validity,
        )
        self.last_call = {
            "order_id": order_id, "order_type": order_type,
            "leg_name": leg_name, "quantity": quantity, "price": price,
            "trigger_price": trigger_price,
            "disclosed_quantity": disclosed_quantity, "validity": validity,
        }
        return {"status": "success", "data": {"orderId": order_id}}


@pytest.fixture
def broker():
    conn_mock = MagicMock()
    sdk = _RealSignatureDhanSDK()
    conn_mock.get_dhan_conn.return_value = sdk
    broker = DhanBroker(conn=conn_mock)
    broker._test_sdk = sdk  # stash for assertions
    return broker


class TestDhanModifyOrderRealSignature:
    def test_modify_order_does_not_raise_typeerror(self, broker):
        """Pre-fix this raised TypeError on every call — no exception at
        all is the primary regression guard."""
        result = broker.modify_order("ORD1", quantity=10, price=105.5)
        assert result == "ORD1"

    def test_modify_order_defaults_leg_name_entry_leg(self, broker):
        broker.modify_order("ORD1", quantity=10, price=105.5)
        assert broker._test_sdk.last_call["leg_name"] == "ENTRY_LEG"

    def test_modify_order_defaults_disclosed_quantity_zero(self, broker):
        broker.modify_order("ORD1", quantity=10, price=105.5)
        assert broker._test_sdk.last_call["disclosed_quantity"] == 0

    def test_modify_order_defaults_validity_day(self, broker):
        broker.modify_order("ORD1", quantity=10, price=105.5)
        assert broker._test_sdk.last_call["validity"] == "DAY"

    def test_modify_order_honours_explicit_overrides(self, broker):
        broker.modify_order(
            "ORD1", quantity=10, price=105.5,
            leg_name="TARGET_LEG", disclosed_quantity=5, validity="IOC",
        )
        last = broker._test_sdk.last_call
        assert last["leg_name"] == "TARGET_LEG"
        assert last["disclosed_quantity"] == 5
        assert last["validity"] == "IOC"

    def test_modify_order_raises_on_broker_rejection(self, broker):
        def fake_modify_order(order_id, order_type, leg_name, quantity,
                               price, trigger_price, disclosed_quantity,
                               validity):
            return {"status": "failed", "remarks": "price out of range"}

        broker._test_sdk.modify_order = fake_modify_order
        with pytest.raises(RuntimeError, match="rejected"):
            broker.modify_order("ORD1", quantity=10, price=105.5)
