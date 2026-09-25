"""
C1 regression tests — order-row quantity normalization for MCX/NCO
(backend/api/routes/orders_helpers.py:_row_from_dict / _mcx_row_qty_to_contracts).

Pre-fix: `_row_from_dict` passed the broker's raw `quantity` (and
`pending_quantity` / `filled_quantity`) straight through for MCX/NCO
rows. Kite/Dhan report these fields in LOTS for MCX/NCO (exchange wire
convention), but every OrderRow consumer downstream treats `quantity`
as CONTRACTS — the SAME convention already normalized for the positions
DataFrame in `backend/brokers/broker_apis.py:_annotate_lot_size`
(~line 1992-2003). Leaving order rows unnormalized let the frontend's
modify ticket derive lots from the raw value, multiply back by
lot_size, and send a contracts-sized quantity into a broker field that
expects lots — silently resizing the order (1-lot CRUDEOILM modify
sending quantity=10 → broker reads 10 lots).

Broker-id gating covers BOTH the canonical `Broker.broker_id` value
("zerodha_kite") and the short-form id used elsewhere in the codebase
("kite"), plus "dhan" — Groww (confirmed CONTRACTS for MCX already)
must NEVER be converted.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _crudeoilm_order_dict(qty: int = 10, pending: int = 10, filled: int = 0) -> dict:
    """1-lot CRUDEOILM MCX order — broker reports qty in LOTS. lot_size=10."""
    return {
        "order_id": "MCX-ORD-1",
        "exchange": "MCX",
        "tradingsymbol": "CRUDEOILM26JULFUT",
        "transaction_type": "SELL",
        "quantity": qty,
        "pending_quantity": pending,
        "filled_quantity": filled,
        "price": 0.0,
        "trigger_price": 0.0,
        "average_price": 7500.0,
        "status": "OPEN",
        "order_type": "LIMIT",
        "product": "NRML",
        "variety": "regular",
        "order_timestamp": "",
        "exchange_timestamp": "",
        "status_message": "",
        "tag": "",
    }


_LOT_INDEX_PATCH_TARGET = "backend.brokers.adapters.kite._LOT_INDEX"
_CRUDEOILM_LOT_SIZE = 10


class TestMcxRowQtyToContracts:
    """Direct unit tests of the conversion helper."""

    def test_zerodha_kite_canonical_broker_id_converts(self):
        """`Broker.broker_id` returns 'zerodha_kite' (canonical) for the
        real KiteBroker adapter — this MUST be in the lots-convention
        allow-list, or Kite rows (the primary broker) never convert."""
        from backend.api.routes.orders_helpers import _mcx_row_qty_to_contracts

        with patch.dict(_LOT_INDEX_PATCH_TARGET,
                        {("MCX", "CRUDEOILM26JULFUT"): _CRUDEOILM_LOT_SIZE}, clear=True):
            result = _mcx_row_qty_to_contracts(
                "MCX", "CRUDEOILM26JULFUT", "zerodha_kite", 1,
            )
        assert result == 10, f"1 lot * lot_size=10 must be 10 contracts, got {result}"

    def test_short_form_kite_broker_id_converts(self):
        from backend.api.routes.orders_helpers import _mcx_row_qty_to_contracts

        with patch.dict(_LOT_INDEX_PATCH_TARGET,
                        {("MCX", "CRUDEOILM26JULFUT"): _CRUDEOILM_LOT_SIZE}, clear=True):
            result = _mcx_row_qty_to_contracts(
                "MCX", "CRUDEOILM26JULFUT", "kite", 1,
            )
        assert result == 10

    def test_dhan_broker_id_converts(self):
        from backend.api.routes.orders_helpers import _mcx_row_qty_to_contracts

        with patch.dict(_LOT_INDEX_PATCH_TARGET,
                        {("MCX", "CRUDEOILM26JULFUT"): _CRUDEOILM_LOT_SIZE}, clear=True):
            result = _mcx_row_qty_to_contracts(
                "MCX", "CRUDEOILM26JULFUT", "dhan", 2,
            )
        assert result == 20

    def test_groww_broker_id_not_converted(self):
        """Groww already reports MCX quantity in CONTRACTS — must NOT be
        double-converted."""
        from backend.api.routes.orders_helpers import _mcx_row_qty_to_contracts

        with patch.dict(_LOT_INDEX_PATCH_TARGET,
                        {("MCX", "CRUDEOILM26JULFUT"): _CRUDEOILM_LOT_SIZE}, clear=True):
            result = _mcx_row_qty_to_contracts(
                "MCX", "CRUDEOILM26JULFUT", "groww", 10,
            )
        assert result == 10, "Groww MCX qty must pass through unconverted"

    def test_non_mcx_exchange_unconverted(self):
        from backend.api.routes.orders_helpers import _mcx_row_qty_to_contracts

        result = _mcx_row_qty_to_contracts("NFO", "NIFTY24MAY24000CE", "zerodha_kite", 75)
        assert result == 75

    def test_nco_exchange_also_converted(self):
        from backend.api.routes.orders_helpers import _mcx_row_qty_to_contracts

        with patch.dict(_LOT_INDEX_PATCH_TARGET,
                        {("NCO", "USDINR26JULFUT"): 1000}, clear=True):
            result = _mcx_row_qty_to_contracts("NCO", "USDINR26JULFUT", "zerodha_kite", 1)
        assert result == 1000

    def test_cold_cache_passes_through_unconverted(self):
        """lot_size cache miss (0/absent) — best-effort passthrough, never raises."""
        from backend.api.routes.orders_helpers import _mcx_row_qty_to_contracts

        with patch.dict(_LOT_INDEX_PATCH_TARGET, {}, clear=True):
            result = _mcx_row_qty_to_contracts("MCX", "CRUDEOILM26JULFUT", "zerodha_kite", 1)
        assert result == 1

    def test_unknown_broker_id_passes_through_unconverted(self):
        """A future/unknown broker id (e.g. 'paper') must default to
        unconverted rather than silently assume the lots convention."""
        from backend.api.routes.orders_helpers import _mcx_row_qty_to_contracts

        with patch.dict(_LOT_INDEX_PATCH_TARGET,
                        {("MCX", "CRUDEOILM26JULFUT"): _CRUDEOILM_LOT_SIZE}, clear=True):
            result = _mcx_row_qty_to_contracts("MCX", "CRUDEOILM26JULFUT", "paper", 10)
        assert result == 10

    def test_non_numeric_qty_returns_zero(self):
        from backend.api.routes.orders_helpers import _mcx_row_qty_to_contracts

        assert _mcx_row_qty_to_contracts("MCX", "X", "zerodha_kite", None) == 0
        assert _mcx_row_qty_to_contracts("MCX", "X", "zerodha_kite", "garbage") == 0


class TestRowFromDictMcxNormalization:
    """End-to-end through `_row_from_dict` — the exact worked example
    from the incident: 1-lot CRUDEOILM MCX order, lot_size=10."""

    def test_crudeoilm_1_lot_order_normalized_to_10_contracts(self):
        from backend.api.routes.orders_helpers import _row_from_dict

        d = _crudeoilm_order_dict(qty=1, pending=1, filled=0)
        with patch.dict(_LOT_INDEX_PATCH_TARGET,
                        {("MCX", "CRUDEOILM26JULFUT"): _CRUDEOILM_LOT_SIZE}, clear=True):
            row = _row_from_dict(d, "ZG0790", "zerodha_kite")

        assert row.quantity == 10, (
            f"1 lot CRUDEOILM (lot_size=10) must normalize to quantity=10 "
            f"contracts, got {row.quantity}"
        )
        assert row.pending_quantity == 10
        assert row.filled_quantity == 0

    def test_crudeoilm_partial_fill_all_three_fields_normalized(self):
        """quantity, pending_quantity, and filled_quantity must ALL be
        normalized consistently — they carry the same broker-native
        (lots) unit for a given order."""
        from backend.api.routes.orders_helpers import _row_from_dict

        # 2-lot order, 1 lot filled, 1 lot pending.
        d = _crudeoilm_order_dict(qty=2, pending=1, filled=1)
        with patch.dict(_LOT_INDEX_PATCH_TARGET,
                        {("MCX", "CRUDEOILM26JULFUT"): _CRUDEOILM_LOT_SIZE}, clear=True):
            row = _row_from_dict(d, "ZG0790", "zerodha_kite")

        assert row.quantity == 20
        assert row.pending_quantity == 10
        assert row.filled_quantity == 10

    def test_nfo_order_unaffected(self):
        from backend.api.routes.orders_helpers import _row_from_dict

        d = {
            "order_id": "NFO-1", "exchange": "NFO",
            "tradingsymbol": "NIFTY24MAY24000CE",
            "transaction_type": "BUY", "quantity": 75,
            "pending_quantity": 75, "filled_quantity": 0,
            "price": 0.0, "trigger_price": 0.0, "average_price": 0.0,
            "status": "OPEN", "order_type": "LIMIT", "product": "NRML",
            "variety": "regular", "order_timestamp": "",
            "exchange_timestamp": "", "status_message": "", "tag": "",
        }
        row = _row_from_dict(d, "ZG0790", "zerodha_kite")
        assert row.quantity == 75

    def test_default_broker_id_empty_string_no_conversion(self):
        """Calling _row_from_dict without a broker_id (legacy 2-arg
        call shape) must not raise and must not convert — safest
        default when the caller doesn't know the broker."""
        from backend.api.routes.orders_helpers import _row_from_dict

        d = _crudeoilm_order_dict(qty=1)
        with patch.dict(_LOT_INDEX_PATCH_TARGET,
                        {("MCX", "CRUDEOILM26JULFUT"): _CRUDEOILM_LOT_SIZE}, clear=True):
            row = _row_from_dict(d, "ZG0790")
        assert row.quantity == 1


class TestFetchOrdersPassesBrokerId:
    """`_fetch_orders` must forward `broker.broker_id` into `_row_from_dict`
    so MCX rows convert without every call site remembering to do it."""

    def test_fetch_orders_converts_mcx_qty_via_broker_id(self):
        from backend.api.routes.orders_helpers import _fetch_orders

        broker = MagicMock()
        broker.account = "ZG0790"
        broker.broker_id = "zerodha_kite"
        broker.orders.return_value = [_crudeoilm_order_dict(qty=1, pending=1, filled=0)]

        with patch("backend.brokers.registry.all_brokers", return_value=[broker]), \
             patch.dict(_LOT_INDEX_PATCH_TARGET,
                        {("MCX", "CRUDEOILM26JULFUT"): _CRUDEOILM_LOT_SIZE}, clear=True):
            result = _fetch_orders()

        assert len(result.rows) == 1
        assert result.rows[0].quantity == 10, (
            f"_fetch_orders must convert MCX qty via broker.broker_id, "
            f"got quantity={result.rows[0].quantity}"
        )
