"""
Tests for MCX/NCO G1 LOT_MULTIPLE and G2 FAT_FINGER guard behavior.

Covers:
  - MCX/NCO: G1 guard DOES fire when qty is not a valid multiple (like NFO)
  - MCX/NCO: G2 (FAT_FINGER_5_LOT_CAP) is SKIPPED (route-level 20-lot cap is authoritative)
  - NFO: G1 guard DOES fire when qty is not a valid multiple
  - NFO: G2 (FAT_FINGER_5_LOT_CAP) fires for qty > 5 lots
  - Equity (lot_size=1): no G1/G2 check at all

Patch strategy:
  - stub Broker ABC (profile / instruments / basket_order_margins / margins)
  - patch Connections and registry.get_broker
  - patch backend.brokers.adapters.kite.get_lot_size (async)
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


MCX_LOT_SIZE = 100
MCX_SYMBOL   = "CRUDEOILAUG25FUT"


def _make_broker_stub(*,
                      enabled_exchanges: tuple[str, ...] = ("NSE", "NFO", "BSE", "MCX", "NCO", "CDS"),
                      basket_required_total: float = 10_000.0,
                      margin_net: float = 500_000.0) -> MagicMock:
    broker = MagicMock()
    broker.profile.return_value = {"exchanges": list(enabled_exchanges)}
    broker.instruments.return_value = [{
        "tradingsymbol": MCX_SYMBOL,
        "exchange": "MCX",
        "instrument_type": "FUT",
        "freeze_qty": 10_000,
        "lot_size": MCX_LOT_SIZE,
        "tick_size": 1.0,
    }]
    broker.basket_order_margins.return_value = [{
        "initial": {"total": basket_required_total},
    }]
    broker.margins.return_value = {
        "equity": {"enabled": True, "net": margin_net},
        "commodity": {"enabled": True, "net": margin_net},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


def _conns_with(account: str) -> MagicMock:
    c = MagicMock()
    c.conn = {account: object()}
    return c


# ──────────────────────────────────────────────────────────────────────────────
# MCX/NCO G1 LOT_MULTIPLE: fires on non-multiple qty (same as NFO)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcx_g1_fires_on_non_multiple_qty():
    """MCX qty=50 with lot_size=100 (non-multiple) → G1 LOT_MULTIPLE fires."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    bad_qty = 50  # not a multiple of 100

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=MCX_LOT_SIZE)):
        result = await run_preflight("ZG0790", {
            "exchange": "MCX",
            "tradingsymbol": MCX_SYMBOL,
            "quantity": bad_qty,
            "order_type": "LIMIT",
            "product": "NRML",
            "variety": "regular",
            "side": "BUY",
            "price": 5500.0,
        })

    # G1 must fire for MCX non-multiple
    assert result["ok"] is False, f"Expected G1 block, got ok={result['ok']}"
    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" in codes, (
        f"MCX G1 must fire for non-multiple qty; got: {result['blocked']}"
    )
    g1 = next(b for b in result["blocked"] if b["code"] == "LOT_MULTIPLE")
    assert g1["data"]["qty"] == bad_qty
    assert g1["data"]["lot_size"] == MCX_LOT_SIZE


@pytest.mark.asyncio
async def test_mcx_g1_passes_on_exact_multiple():
    """MCX qty=100 with lot_size=100 (1 lot exactly) → passes G1."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=MCX_LOT_SIZE)):
        result = await run_preflight("ZG0790", {
            "exchange": "MCX",
            "tradingsymbol": MCX_SYMBOL,
            "quantity": MCX_LOT_SIZE,
            "order_type": "LIMIT",
            "product": "NRML",
            "variety": "regular",
            "side": "BUY",
            "price": 5500.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" not in codes, f"Unexpected G1 block: {result['blocked']}"


@pytest.mark.asyncio
async def test_mcx_g1_passes_on_two_lot_multiple():
    """MCX qty=200 with lot_size=100 (2 lots) → passes G1."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=MCX_LOT_SIZE)):
        result = await run_preflight("ZG0790", {
            "exchange": "MCX",
            "tradingsymbol": MCX_SYMBOL,
            "quantity": 200,
            "order_type": "LIMIT",
            "product": "NRML",
            "variety": "regular",
            "side": "BUY",
            "price": 5500.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" not in codes, f"Unexpected G1 block: {result['blocked']}"


# ──────────────────────────────────────────────────────────────────────────────
# MCX/NCO G2 (FAT_FINGER_5_LOT_CAP): skipped (route-level 20-lot cap is authoritative)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcx_g2_skipped_for_6_lots():
    """MCX qty=6 lots → G2 FAT_FINGER_5_LOT_CAP SKIPPED (not fired at preflight).

    MCX has a route-level 20-lot cap (422 response), not the 5-lot preflight cap.
    G2 is skipped for MCX/NCO to avoid false 422s that would shadow the route guard.
    """
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    qty_lots_6 = 6 * MCX_LOT_SIZE

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=MCX_LOT_SIZE)):
        result = await run_preflight("ZG0790", {
            "exchange": "MCX",
            "tradingsymbol": MCX_SYMBOL,
            "quantity": qty_lots_6,
            "order_type": "LIMIT",
            "product": "NRML",
            "variety": "regular",
            "side": "BUY",
            "price": 5500.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "FAT_FINGER_5_LOT_CAP" not in codes, (
        f"MCX G2 must be skipped (route has 20-lot cap); got: {result['blocked']}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# NFO G2 (FAT_FINGER_5_LOT_CAP): still fires for NFO (5-lot cap)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nfo_g2_still_fires_for_6_lots():
    """NFO qty=6 lots (300 contracts at lot_size=50) → FAT_FINGER_5_LOT_CAP fired."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    nfo_lot_size = 50
    # 6 lots = 300 contracts
    qty = 6 * nfo_lot_size

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=nfo_lot_size)):
        result = await run_preflight("ZG0790", {
            "exchange": "NFO",
            "tradingsymbol": "NIFTY25AUGFUT",
            "quantity": qty,
            "order_type": "LIMIT",
            "product": "NRML",
            "variety": "regular",
            "side": "BUY",
            "price": 22000.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "FAT_FINGER_5_LOT_CAP" in codes, (
        f"Expected FAT_FINGER for NFO 6 lots, got: {result['blocked']}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# NCO (NSE Commodity): same treatment as MCX (G1 fires, G2 skipped)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nco_g1_fires_on_non_multiple():
    """NCO qty not a multiple → G1 LOT_MULTIPLE fires (like MCX)."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    bad_qty = MCX_LOT_SIZE + 7

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=MCX_LOT_SIZE)):
        result = await run_preflight("ZG0790", {
            "exchange": "NCO",
            "tradingsymbol": "CRUDEOILNOV25FUT",
            "quantity": bad_qty,
            "order_type": "LIMIT",
            "product": "NRML",
            "variety": "regular",
            "side": "BUY",
            "price": 5500.0,
        })

    # G1 must fire for NCO non-multiple
    assert result["ok"] is False, f"Expected G1 block, got ok={result['ok']}"
    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" in codes, (
        f"NCO G1 must fire for non-multiple qty; got: {result['blocked']}"
    )


@pytest.mark.asyncio
async def test_nco_g2_skipped_for_6_lots():
    """NCO qty=6 lots → G2 FAT_FINGER_5_LOT_CAP SKIPPED."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    qty_lots_6 = 6 * MCX_LOT_SIZE

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=MCX_LOT_SIZE)):
        result = await run_preflight("ZG0790", {
            "exchange": "NCO",
            "tradingsymbol": "CRUDEOILNOV25FUT",
            "quantity": qty_lots_6,
            "order_type": "LIMIT",
            "product": "NRML",
            "variety": "regular",
            "side": "BUY",
            "price": 5500.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "FAT_FINGER_5_LOT_CAP" not in codes, (
        f"NCO G2 must be skipped (route has 20-lot cap); got: {result['blocked']}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Equity (lot_size=1): no G1 check at all
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_equity_no_g1_check():
    """NSE equity (lot_size=1) → no G1 LOT_MULTIPLE check."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=1)):  # equity sentinel
        result = await run_preflight("ZG0790", {
            "exchange": "NSE",
            "tradingsymbol": "RELIANCE",
            "quantity": 7,  # arbitrary qty, not a "multiple" concern for equity
            "order_type": "LIMIT",
            "product": "CNC",
            "variety": "regular",
            "side": "BUY",
            "price": 2900.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" not in codes, (
        f"Equity (lot_size=1) must not have G1 check, got: {result['blocked']}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Close intent: intent="close" bypasses FAT_FINGER (G2) for all exchanges
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nfo_close_bypasses_g2():
    """close intent bypasses FAT_FINGER (G2) even for NFO."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    nfo_lot_size = 50
    # 6 lots normally triggers FAT_FINGER
    qty = 6 * nfo_lot_size

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=nfo_lot_size)):
        result = await run_preflight("ZG0790", {
            "exchange": "NFO",
            "tradingsymbol": "NIFTY25AUGFUT",
            "quantity": qty,
            "order_type": "LIMIT",
            "product": "NRML",
            "variety": "regular",
            "side": "SELL",
            "price": 22000.0,
            "intent": "close",
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "FAT_FINGER_5_LOT_CAP" not in codes, (
        f"close intent must bypass FAT_FINGER; got: {result['blocked']}"
    )


@pytest.mark.asyncio
async def test_nfo_open_fires_g2():
    """NFO open order with 6 lots → FAT_FINGER fires (no close intent)."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    nfo_lot_size = 50
    qty = 6 * nfo_lot_size

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=nfo_lot_size)):
        result = await run_preflight("ZG0790", {
            "exchange": "NFO",
            "tradingsymbol": "NIFTY25AUGFUT",
            "quantity": qty,
            "order_type": "LIMIT",
            "product": "NRML",
            "variety": "regular",
            "side": "BUY",
            "price": 22000.0,
            # no intent → open order
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "FAT_FINGER_5_LOT_CAP" in codes, (
        f"FAT_FINGER must fire for NFO 6-lot open; got: {result['blocked']}"
    )
