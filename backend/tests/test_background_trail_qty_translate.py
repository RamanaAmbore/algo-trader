"""
C6 (backend half #2) regression tests — background.py trailing-stop
ratchet must translate `parent_qty` (contracts) to broker wire-format
(lots for MCX/NCO) before calling `broker.modify_gtt`.

Pre-fix: `_process_trail_entry` built `orders_payload["quantity"] =
parent_qty` directly (raw contracts), with no `translate_qty` call.
Kite's `modify_gtt` performs no server-side lots translation, so a
trailing ratchet on an MCX SL would resize the live GTT to `lot_size`×
the intended quantity (e.g. CRUDEOILM lot_size=10, parent_qty=10
contracts (1 lot) sent as quantity=10 → broker reads "10 lots").

Post-fix: `_resolve_trail_wire_qty` resolves lot_size via
`backend.brokers.adapters.kite.get_lot_size` and calls
`broker.translate_qty(exchange, parent_qty, lot_size)` BEFORE
`_build_trail_modify_kwargs` builds the payload — mirroring
`apply_plan_live`'s `_translate_gtt_orders` pattern for the initial
GTT placement.

Uses a real `KiteBroker.translate_qty` (not a mock) so the assertions
exercise the actual lots-conversion arithmetic, independent of the
parallel kite.py modify_gtt ceiling change.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.brokers.adapters.kite import KiteBroker


def _make_kite_broker(modify_gtt_mock: MagicMock) -> KiteBroker:
    """Build a KiteBroker whose translate_qty is the REAL implementation
    (base-class → _exchange_contracts_to_wire) but whose modify_gtt is
    stubbed, so the test exercises the real lots-conversion arithmetic
    without depending on the parallel kite.py SDK-call changes."""
    broker = KiteBroker.__new__(KiteBroker)
    broker._conn = MagicMock()
    broker._conn.account = "ZG0790"
    broker.modify_gtt = modify_gtt_mock
    return broker


def _mcx_single_leg_entry(*, parent_qty: int = 10, current_trigger: float = 7400.0) -> dict:
    return {
        "kind": "gtt",
        "id": "GTT-MCX-1",
        "label": "SL",
        "sl_trail_pct": 2.0,
        "trigger_values": [current_trigger],
        "trigger_type": "single",
        "current_trigger": current_trigger,
        "highest_ltp": 7500.0,
        "lowest_ltp": 7500.0,
        "parent_side": "BUY",
        "parent_qty": parent_qty,
        "parent_symbol": "CRUDEOILM26JULFUT",
        "parent_exchange": "MCX",
        "parent_account": "ZG0790",
        "parent_product": "NRML",
    }


def _mcx_two_leg_entry(*, parent_qty: int = 10) -> dict:
    return {
        "kind": "gtt",
        "id": "GTT-MCX-2",
        "label": "TP+SL",
        "sl_trail_pct": 2.0,
        "trigger_values": [7700.0, 7400.0],
        "trigger_type": "two-leg",
        "current_trigger": 7400.0,
        "tp_trigger": 7700.0,
        "highest_ltp": 7500.0,
        "lowest_ltp": 7500.0,
        "parent_side": "BUY",
        "parent_qty": parent_qty,
        "parent_symbol": "CRUDEOILM26JULFUT",
        "parent_exchange": "MCX",
        "parent_account": "ZG0790",
        "parent_product": "NRML",
    }


# LTP that ratchets the trigger favorably: high(7600) * (1 - 2%) = 7448 > 7400
_FAVORABLE_LTP_MAP = {("ZG0790", "MCX:CRUDEOILM26JULFUT"): 7600.0}


class TestTrailQtyTranslateSingleLeg:
    @pytest.mark.asyncio
    async def test_modify_gtt_receives_translated_lots_not_raw_contracts(self):
        """CRUDEOILM lot_size=10, parent_qty=10 contracts (1 lot) — the
        modify_gtt call must carry quantity=1 (lots), never quantity=10
        (raw contracts)."""
        from backend.api.background import _process_trail_entry

        modify_gtt_mock = MagicMock(return_value="GTT-MCX-1")
        broker = _make_kite_broker(modify_gtt_mock)
        entry = _mcx_single_leg_entry(parent_qty=10)
        row = SimpleNamespace(id=1)

        with patch("backend.brokers.registry.get_broker", return_value=broker), \
             patch("backend.brokers.adapters.kite.get_lot_size",
                   new=AsyncMock(return_value=10)):
            changed = await _process_trail_entry(entry, row, dict(_FAVORABLE_LTP_MAP))

        assert changed is True
        modify_gtt_mock.assert_called_once()
        _, kwargs = modify_gtt_mock.call_args
        orders_payload = kwargs["orders"]
        assert len(orders_payload) == 1
        assert orders_payload[0]["quantity"] == 1, (
            f"expected translated qty=1 (lot), got "
            f"{orders_payload[0]['quantity']} — untranslated contracts "
            f"would resize the live GTT to 10x the intended lots"
        )
        # Watermark + entry state updated.
        assert entry["current_trigger"] == pytest.approx(7448.0, rel=1e-3)

    @pytest.mark.asyncio
    async def test_nfo_contracts_unaffected_by_translation(self):
        """Non-MCX/NCO exchanges: translate_qty is a no-op — quantity
        passes through unchanged (already contracts)."""
        from backend.api.background import _process_trail_entry

        modify_gtt_mock = MagicMock(return_value="GTT-NFO-1")
        broker = _make_kite_broker(modify_gtt_mock)
        entry = _mcx_single_leg_entry(parent_qty=75, current_trigger=98.0)
        entry["parent_exchange"] = "NFO"
        entry["parent_symbol"] = "NIFTY24MAY24000CE"
        entry["highest_ltp"] = 100.0
        entry["lowest_ltp"] = 100.0
        row = SimpleNamespace(id=2)
        ltp_map = {("ZG0790", "NFO:NIFTY24MAY24000CE"): 105.0}

        with patch("backend.brokers.registry.get_broker", return_value=broker), \
             patch("backend.brokers.adapters.kite.get_lot_size",
                   new=AsyncMock(return_value=1)):
            changed = await _process_trail_entry(entry, row, ltp_map)

        assert changed is True
        modify_gtt_mock.assert_called_once()
        _, kwargs = modify_gtt_mock.call_args
        assert kwargs["orders"][0]["quantity"] == 75


class TestTrailQtyTranslateTwoLeg:
    @pytest.mark.asyncio
    async def test_two_leg_oco_both_orders_get_translated_qty(self):
        """Two-leg (OCO) MCX trail: BOTH the TP and SL legs in
        orders_payload must carry the translated (lots) quantity."""
        from backend.api.background import _process_trail_entry

        modify_gtt_mock = MagicMock(return_value="GTT-MCX-2")
        broker = _make_kite_broker(modify_gtt_mock)
        entry = _mcx_two_leg_entry(parent_qty=20)  # 2 lots at lot_size=10
        row = SimpleNamespace(id=3)

        with patch("backend.brokers.registry.get_broker", return_value=broker), \
             patch("backend.brokers.adapters.kite.get_lot_size",
                   new=AsyncMock(return_value=10)):
            changed = await _process_trail_entry(entry, row, dict(_FAVORABLE_LTP_MAP))

        assert changed is True
        modify_gtt_mock.assert_called_once()
        _, kwargs = modify_gtt_mock.call_args
        orders_payload = kwargs["orders"]
        assert len(orders_payload) == 2
        for leg in orders_payload:
            assert leg["quantity"] == 2, (
                f"expected translated qty=2 (lots), got {leg['quantity']}"
            )


class TestTrailQtyTranslateColdCache:
    @pytest.mark.asyncio
    async def test_cold_lot_size_cache_skips_modify_gtt(self):
        """lot_size cache miss (0) for MCX/NCO — must NOT call modify_gtt
        with an untranslated quantity; skip the modify this cycle."""
        from backend.api.background import _process_trail_entry

        modify_gtt_mock = MagicMock()
        broker = _make_kite_broker(modify_gtt_mock)
        entry = _mcx_single_leg_entry(parent_qty=10)
        row = SimpleNamespace(id=4)

        with patch("backend.brokers.registry.get_broker", return_value=broker), \
             patch("backend.brokers.adapters.kite.get_lot_size",
                   new=AsyncMock(return_value=0)):
            changed = await _process_trail_entry(entry, row, dict(_FAVORABLE_LTP_MAP))

        modify_gtt_mock.assert_not_called()
        # Watermark still advances even though modify was skipped (Phase 3C #3
        # persistence-on-watermark-advance behaviour is unaffected by this fix).
        assert changed is True

    @pytest.mark.asyncio
    async def test_translate_qty_rejection_skips_modify_gtt(self):
        """translate_qty raising ValueError (e.g. non-multiple qty caught
        by the base-layer QTY-GUARD) must not propagate — the trail
        poller skips this entry's modify rather than crashing the row."""
        from backend.api.background import _process_trail_entry

        modify_gtt_mock = MagicMock()
        broker = _make_kite_broker(modify_gtt_mock)
        # parent_qty=15 is not a clean multiple of lot_size=10 — the
        # real translate_qty (QTY-GUARD) raises ValueError for this.
        entry = _mcx_single_leg_entry(parent_qty=15)
        row = SimpleNamespace(id=5)

        with patch("backend.brokers.registry.get_broker", return_value=broker), \
             patch("backend.brokers.adapters.kite.get_lot_size",
                   new=AsyncMock(return_value=10)):
            changed = await _process_trail_entry(entry, row, dict(_FAVORABLE_LTP_MAP))

        modify_gtt_mock.assert_not_called()
        assert changed is True
