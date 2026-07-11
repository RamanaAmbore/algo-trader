"""
Tests that run_preflight applies G1/G2 guards for exchange="NCO"
(NSE Commodity — was missing from the outer gate before the P1 fix).

NCO is treated like MCX for lot-size purposes:
  - G1 (LOT_MULTIPLE) must fire when qty is not a valid multiple
  - G2 for NCO uses the 20-lot MCX-style cap (the route-level guard),
    NOT the 5-lot NFO cap; the preflight itself does NOT fire a
    FAT_FINGER block for MCX/NCO — it defers to the route. So a
    25-lot NCO order that is a valid multiple must NOT be blocked
    by G2 at preflight — but G1 MUST catch a non-multiple qty.

Patch strategy mirrors test_preflight.py:
  - stub Broker ABC (profile / instruments / basket_order_margins / margins)
  - patch Connections and registry.get_broker
  - patch backend.brokers.adapters.kite.get_lot_size (async)
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


# ---------------------------------------------------------------------------
# Helpers  (same pattern as test_preflight.py)
# ---------------------------------------------------------------------------

NCO_LOT_SIZE = 100   # representative MCX/NCO lot size (e.g. CRUDEOIL)
NCO_SYMBOL   = "CRUDEOILJUL25FUT"


def _make_broker_stub_nco(*,
                          enabled_exchanges: tuple[str, ...] = (
                              "NSE", "NFO", "BSE", "MCX", "NCO", "CDS"),
                          basket_required_total: float = 10_000.0,
                          margin_net: float = 500_000.0) -> MagicMock:
    broker = MagicMock()
    broker.profile.return_value = {"exchanges": list(enabled_exchanges)}
    broker.instruments.return_value = [{
        "tradingsymbol":   NCO_SYMBOL,
        "exchange":        "NCO",
        "instrument_type": "FUT",
        "freeze_qty":      10_000,
        "lot_size":        NCO_LOT_SIZE,
        "tick_size":       1.0,
    }]
    broker.basket_order_margins.return_value = [{
        "initial": {"total": basket_required_total},
    }]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": margin_net},
        "commodity": {"enabled": True, "net": margin_net},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


def _conns_with(account: str) -> MagicMock:
    c = MagicMock()
    c.conn = {account: object()}
    return c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_nco_valid_lot_passes():
    """NCO order with qty == lot_size passes G1/G2 at preflight."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub_nco()
    conns  = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=NCO_LOT_SIZE)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NCO",
            "tradingsymbol": NCO_SYMBOL,
            "quantity":      NCO_LOT_SIZE,   # exactly 1 lot — valid
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         5500.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" not in codes, f"unexpected G1 block: {result['blocked']}"


@pytest.mark.asyncio
async def test_preflight_nco_g1_fires_on_non_multiple():
    """NCO order with qty not a multiple of lot_size → G1 LOT_MULTIPLE block."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub_nco()
    conns  = _conns_with("ZG0790")

    bad_qty = NCO_LOT_SIZE + 7   # 107 — not a multiple of 100

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=NCO_LOT_SIZE)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NCO",
            "tradingsymbol": NCO_SYMBOL,
            "quantity":      bad_qty,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         5500.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" in codes, f"G1 did not fire; blocked={result['blocked']}"
    g1 = next(b for b in result["blocked"] if b["code"] == "LOT_MULTIPLE")
    assert g1["data"]["qty"] == bad_qty
    assert g1["data"]["lot_size"] == NCO_LOT_SIZE


@pytest.mark.asyncio
async def test_preflight_nco_25_lots_not_blocked_by_g2():
    """
    NCO uses the 20-lot MCX-style route guard, not the 5-lot NFO cap.
    Preflight deliberately defers to the route-level check for MCX/NCO,
    so 25 valid lots must NOT be blocked by FAT_FINGER at preflight.
    """
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub_nco()
    conns  = _conns_with("ZG0790")

    lots_25_qty = 25 * NCO_LOT_SIZE   # 2500 — 25 lots, valid multiple

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=NCO_LOT_SIZE)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NCO",
            "tradingsymbol": NCO_SYMBOL,
            "quantity":      lots_25_qty,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         5500.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "FAT_FINGER_5_LOT_CAP" not in codes, (
        f"G2 incorrectly fired for NCO 25-lot order: {result['blocked']}"
    )
