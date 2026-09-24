"""Tests for the MCX/NCO provisional postback fill-quantity lots→contracts
conversion (backend/api/routes/orders.py).

Bug: Kite ships MCX order/postback `quantity` in LOTS (same convention as
its positions intraday fields — CLAUDE.md "Option qty vs lot_size"), but
`_rco_broadcast_position_filled` / `_positions_refresh_after_fill` broadcast
and poll using that raw value unconverted, feeding a lots-not-contracts
quantity into the WS `position_filled` event and the post-fill refresh
delta — the provisional-fill half of the Exp P&L display bug.

Fix: `_mcx_postback_qty_to_contracts` converts once (lot_size from
`backend.brokers.adapters.kite._LOT_INDEX`), applied in
`_postback_broadcast_fanout` before both downstream consumers.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from backend.api.routes.orders import (
    _mcx_postback_qty_to_contracts,
    _postback_broadcast_fanout,
)


class TestMcxPostbackQtyToContracts:
    def test_mcx_lots_converted_to_contracts(self):
        with patch(
            "backend.brokers.adapters.kite._LOT_INDEX",
            {("MCX", "CRUDEOIL26SEPFUT"): 100},
        ):
            out = _mcx_postback_qty_to_contracts("MCX", "CRUDEOIL26SEPFUT", 1, "kite")
        assert out == 100, "1 lot × lot_size=100 must convert to 100 contracts"

    def test_nco_lots_converted_to_contracts(self):
        with patch(
            "backend.brokers.adapters.kite._LOT_INDEX",
            {("NCO", "USDINR26SEPFUT"): 1000},
        ):
            out = _mcx_postback_qty_to_contracts("NCO", "USDINR26SEPFUT", 2, "kite")
        assert out == 2000

    def test_non_mcx_exchange_passes_through_unchanged(self):
        with patch("backend.brokers.adapters.kite._LOT_INDEX", {}):
            out = _mcx_postback_qty_to_contracts("NFO", "NIFTY26SEPFUT", 75, "kite")
        assert out == 75, "NFO/equity qty is already in contracts — no conversion"

    def test_cold_lot_index_passes_through_unconverted(self):
        """Cache miss (cold _LOT_INDEX) — best-effort passthrough, never raises."""
        with patch("backend.brokers.adapters.kite._LOT_INDEX", {}):
            out = _mcx_postback_qty_to_contracts("MCX", "GOLDM26SEPFUT", 3, "kite")
        assert out == 3

    def test_none_qty_returns_zero(self):
        out = _mcx_postback_qty_to_contracts("MCX", "CRUDEOIL26SEPFUT", None, "kite")
        assert out == 0

    def test_dhan_mcx_lots_converted_to_contracts(self):
        """Dhan ships MCX quantity in lots, same convention as Kite."""
        with patch(
            "backend.brokers.adapters.kite._LOT_INDEX",
            {("MCX", "CRUDEOIL26SEPFUT"): 100},
        ):
            out = _mcx_postback_qty_to_contracts("MCX", "CRUDEOIL26SEPFUT", 1, "dhan")
        assert out == 100, "Dhan MCX: 1 lot × lot_size=100 must convert to 100 contracts"

    def test_groww_mcx_qty_not_converted(self):
        """Bug fix: Groww ships CONTRACTS for every exchange including MCX
        (backend/brokers/adapters/groww.py). Applying the lots→contracts
        multiply here would double-convert a Groww MCX fill."""
        with patch(
            "backend.brokers.adapters.kite._LOT_INDEX",
            {("MCX", "CRUDEOIL26SEPFUT"): 100},
        ):
            out = _mcx_postback_qty_to_contracts("MCX", "CRUDEOIL26SEPFUT", 100, "groww")
        assert out == 100, (
            "Groww MCX fill of 100 contracts must stay 100 — NOT be "
            "multiplied by lot_size again"
        )

    def test_groww_nco_qty_not_converted(self):
        with patch(
            "backend.brokers.adapters.kite._LOT_INDEX",
            {("NCO", "USDINR26SEPFUT"): 1000},
        ):
            out = _mcx_postback_qty_to_contracts("NCO", "USDINR26SEPFUT", 2000, "groww")
        assert out == 2000, "Groww NCO fill already in contracts — no conversion"

    def test_paper_mcx_qty_not_converted(self):
        """Regression (2026-09 audit round 3, item #2): paper AlgoOrder
        quantities are ALREADY in contracts (see
        orders_place.py:_ticket_validate_input), never in lots the way
        live Kite/Dhan postbacks are. broker="paper" must NOT trigger the
        lots→contracts multiply — a 1-lot CRUDEOIL paper fill (100
        contracts) previously doubled to 10,000 when this helper used a
        deny-list ("skip only for groww") instead of an allow-list."""
        with patch(
            "backend.brokers.adapters.kite._LOT_INDEX",
            {("MCX", "CRUDEOIL26SEPFUT"): 100},
        ):
            out = _mcx_postback_qty_to_contracts("MCX", "CRUDEOIL26SEPFUT", 100, "paper")
        assert out == 100, (
            "Paper MCX fill of 100 contracts must stay 100 — NOT be "
            "multiplied by lot_size again"
        )

    def test_unknown_broker_defaults_to_unconverted(self):
        """Allow-list design: any broker identifier not explicitly listed
        in `_MCX_LOTS_CONVENTION_BROKERS` passes through unconverted by
        default — the safe failure mode for a future/unrecognised broker
        string, unlike the old deny-list design which defaulted to
        converting (and would have double-converted any new broker that
        forgot to opt out)."""
        with patch(
            "backend.brokers.adapters.kite._LOT_INDEX",
            {("MCX", "CRUDEOIL26SEPFUT"): 100},
        ):
            out = _mcx_postback_qty_to_contracts("MCX", "CRUDEOIL26SEPFUT", 100, "some_future_broker")
        assert out == 100


class TestPostbackFanoutMcxQtyConversion:
    """_postback_broadcast_fanout must convert MCX qty before broadcasting
    position_filled, so the frontend never sees a raw lots value."""

    def test_complete_broadcasts_contracts_not_lots(self):
        captured = []

        def _fake_broadcast(payload):
            captured.append(json.loads(payload))

        with (
            patch("backend.api.routes.orders.broadcast", side_effect=_fake_broadcast),
            patch("backend.api.routes.orders.invalidate"),
            patch(
                "backend.brokers.adapters.kite._LOT_INDEX",
                {("MCX", "CRUDEOIL26SEPFUT"): 100},
            ),
        ):
            _postback_broadcast_fanout(
                status="COMPLETE", order_id="123", account="ZG0790",
                masked="ZG####", symbol="CRUDEOIL26SEPFUT", txn="BUY",
                qty=1, price=6000.0, broker="kite", exchange="MCX",
            )

        fill_events = [c for c in captured if c.get("event") == "position_filled"]
        assert len(fill_events) == 1
        assert fill_events[0]["qty"] == 100, (
            "position_filled must broadcast CONTRACTS (1 lot × lot_size=100), "
            "not the raw provisional lots qty from the postback"
        )

    def test_non_mcx_qty_unaffected(self):
        captured = []

        def _fake_broadcast(payload):
            captured.append(json.loads(payload))

        with (
            patch("backend.api.routes.orders.broadcast", side_effect=_fake_broadcast),
            patch("backend.api.routes.orders.invalidate"),
        ):
            _postback_broadcast_fanout(
                status="COMPLETE", order_id="124", account="ZG0790",
                masked="ZG####", symbol="NIFTY26SEPFUT", txn="SELL",
                qty=50, price=22000.0, broker="kite", exchange="NFO",
            )

        fill_events = [c for c in captured if c.get("event") == "position_filled"]
        assert fill_events[0]["qty"] == -50, "NFO qty already in contracts, sign flipped for SELL"

    def test_groww_mcx_fill_broadcasts_contracts_unconverted(self):
        """Regression guard for the Groww double-conversion bug: a Groww
        MCX fill of 100 contracts must broadcast as 100, not 100×lot_size."""
        captured = []

        def _fake_broadcast(payload):
            captured.append(json.loads(payload))

        with (
            patch("backend.api.routes.orders.broadcast", side_effect=_fake_broadcast),
            patch("backend.api.routes.orders.invalidate"),
            patch(
                "backend.brokers.adapters.kite._LOT_INDEX",
                {("MCX", "CRUDEOIL26SEPFUT"): 100},
            ),
        ):
            _postback_broadcast_fanout(
                status="COMPLETE", order_id="125", account="ZG0790",
                masked="ZG####", symbol="CRUDEOIL26SEPFUT", txn="BUY",
                qty=100, price=6000.0, broker="groww", exchange="MCX",
            )

        fill_events = [c for c in captured if c.get("event") == "position_filled"]
        assert len(fill_events) == 1
        assert fill_events[0]["qty"] == 100, (
            "Groww MCX fill already ships CONTRACTS — must broadcast qty=100 "
            "unconverted, not 100×lot_size=10000"
        )
