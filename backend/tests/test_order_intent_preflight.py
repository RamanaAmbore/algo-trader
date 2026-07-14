"""
Tests for order preflight intent handling and G2 fat-finger cap bypass.

Fix: When closing a position, `_ticket_run_preflight` and `preflight_order`
pass intent="close" through to `run_preflight`, which bypasses the G2
5-lot fat-finger cap. Legitimate close orders > 5 lots are now allowed.

Backend route fix:
  - backend/api/routes/orders.py: `preflight_order` handler extracts intent
    from request body and passes it to run_preflight.
  - backend/api/routes/orders_place.py: `_ticket_run_preflight` already passes
    intent via getattr(data, "intent", None).

Unit test coverage (run_preflight canonical path):
  1. intent="close" + 6 lots → G2 NOT blocked (bypass works)
  2. intent="open" + 6 lots → G2 blocked (normal cap enforced)
  3. intent="open" + 3 lots → passes G2 (under cap)
  4. no intent + 6 lots → G2 blocked (missing intent = open behavior)
  5. intent="close" + 10 lots → G2 NOT blocked (stress test)
  6. intent="close" + non-multiple qty → G1 still blocks (G1 not bypassed)
  7. intent="close" + margin shortfall → margin check still applies
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_broker_stub(*,
                      broker_id: str = "zerodha_kite",
                      enabled_exchanges: tuple[str, ...] = ("NSE", "NFO", "BSE", "MCX", "CDS", "NCO", "BFO"),
                      instruments: list[dict] | None = None,
                      basket_required_total: float = 10000.0,
                      margin_net: float = 500000.0,
                      margin_enabled: bool = True,
                      basket_margin_exception: Exception | None = None) -> MagicMock:
    """Build a stub Broker matching test_preflight_characterization pattern."""
    broker = MagicMock()
    broker.broker_id = broker_id
    broker.profile.return_value = {"exchanges": list(enabled_exchanges)}

    if instruments is None:
        instruments = [{
            "tradingsymbol": "NIFTY25APRFUT",
            "exchange":      "NFO",
            "instrument_type": "FUT",
            "freeze_qty":    6000,
            "lot_size":      50,
            "tick_size":     0.05,
        }]
    broker.instruments.return_value = instruments

    if basket_margin_exception:
        broker.basket_order_margins.side_effect = basket_margin_exception
    else:
        broker.basket_order_margins.return_value = [{
            "initial": {"total": basket_required_total},
        }]

    broker.margins.return_value = {
        "equity":    {"enabled": margin_enabled, "net": margin_net},
        "commodity": {"enabled": margin_enabled, "net": margin_net},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


def _conns_with(account: str):
    """Build a Connections stub containing the given account."""
    c = MagicMock()
    c.conn = {account: object()}
    return c


# ─────────────────────────────────────────────────────────────────────────────
# Unit Tests for run_preflight with intent parameter
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_preflight_with_close_intent_6_lots_not_blocked():
    """run_preflight with intent="close" + 6 lots → G2 NOT blocked.

    Verifies the core fix: when intent="close" is passed in the order dict,
    the G2 5-lot fat-finger cap is bypassed."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    qty_6_lots = 300  # 6 lots × 50

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      qty_6_lots,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "transaction_type": "SELL",
            "price":         22000.0,
            "trigger_price": 0,
            "intent":        "close",  # Critical for G2 bypass
        })

    assert result["ok"] is True, \
        f"Expected ok=True for 6-lot close order with intent='close'. Got blocked: {result.get('blocked')}"

    codes = [b["code"] for b in result.get("blocked", [])]
    assert "FAT_FINGER_5_LOT_CAP" not in codes, \
        f"G2 should be bypassed with intent='close', but got: {codes}"


@pytest.mark.asyncio
async def test_run_preflight_with_open_intent_6_lots_blocked():
    """run_preflight with intent="open" + 6 lots → G2 blocked.

    Verifies that WITHOUT intent="close", the G2 5-lot cap is enforced."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    qty_6_lots = 300  # 6 lots × 50

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      qty_6_lots,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "transaction_type": "BUY",
            "price":         22000.0,
            "trigger_price": 0,
            "intent":        "open",  # Explicit open order
        })

    assert result["ok"] is False, \
        f"Expected ok=False for 6-lot open order. Got: {result}"

    codes = [b["code"] for b in result.get("blocked", [])]
    assert "FAT_FINGER_5_LOT_CAP" in codes, \
        f"Expected FAT_FINGER_5_LOT_CAP blocker for 6-lot open order. Got: {codes}"

    g2_blocker = next(b for b in result["blocked"] if b["code"] == "FAT_FINGER_5_LOT_CAP")
    assert g2_blocker["data"]["lots"] == 6
    assert g2_blocker["data"]["cap"] == 5


@pytest.mark.asyncio
async def test_run_preflight_with_open_intent_3_lots_passes():
    """run_preflight with intent="open" + 3 lots → passes G2.

    Verifies that small lots (< 5) pass even without intent="close"."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    qty_3_lots = 150  # 3 lots × 50

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      qty_3_lots,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "transaction_type": "BUY",
            "price":         22000.0,
            "trigger_price": 0,
            "intent":        "open",
        })

    assert result["ok"] is True, \
        f"Expected ok=True for 3-lot order. Got blocked: {result.get('blocked')}"

    codes = [b["code"] for b in result.get("blocked", [])]
    assert "FAT_FINGER_5_LOT_CAP" not in codes, \
        f"G2 should not block for 3-lot order. Got: {codes}"


@pytest.mark.asyncio
async def test_run_preflight_without_intent_6_lots_blocked():
    """run_preflight WITHOUT intent field → defaults to open behavior (G2 blocks).

    When intent is not provided in the order dict, G2 cap should still apply."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    qty_6_lots = 300  # 6 lots × 50

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      qty_6_lots,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "transaction_type": "BUY",
            "price":         22000.0,
            "trigger_price": 0,
            # NO intent field
        })

    assert result["ok"] is False, \
        f"Expected ok=False for 6-lot order without intent. Got: {result}"

    codes = [b["code"] for b in result.get("blocked", [])]
    assert "FAT_FINGER_5_LOT_CAP" in codes, \
        f"Expected FAT_FINGER_5_LOT_CAP when intent is missing. Got: {codes}"


@pytest.mark.asyncio
async def test_run_preflight_close_intent_10_lots_not_blocked():
    """run_preflight with intent="close" + 10 lots → G2 NOT blocked.

    Stress-test the bypass: even large closes (10 lots) should pass with intent."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    qty_10_lots = 500  # 10 lots × 50

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      qty_10_lots,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "transaction_type": "SELL",
            "price":         22000.0,
            "trigger_price": 0,
            "intent":        "close",
        })

    assert result["ok"] is True, \
        f"Expected ok=True for 10-lot close order. Got blocked: {result.get('blocked')}"

    codes = [b["code"] for b in result.get("blocked", [])]
    assert "FAT_FINGER_5_LOT_CAP" not in codes, \
        f"G2 should not block 10-lot close. Got: {codes}"


# ─────────────────────────────────────────────────────────────────────────────
# Edge Cases
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_preflight_intent_close_with_g1_block():
    """intent="close" bypasses G2 but NOT G1 (lot multiple).

    qty not a multiple of lot_size → should still be blocked by G1."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    bad_qty = 77  # not a multiple of 50

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      bad_qty,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "transaction_type": "SELL",
            "price":         22000.0,
            "trigger_price": 0,
            "intent":        "close",  # Bypasses G2 but not G1
        })

    # Should be blocked by G1, even with intent="close"
    assert result["ok"] is False, \
        f"Expected G1 blocker for non-multiple qty. Got: {result}"

    codes = [b["code"] for b in result.get("blocked", [])]
    assert "LOT_MULTIPLE" in codes, \
        f"Expected LOT_MULTIPLE blocker. Got: {codes}"


@pytest.mark.asyncio
async def test_run_preflight_close_intent_still_checks_margin():
    """intent="close" bypasses G2 but still checks margin (MARGIN_SHORTFALL).

    Even if bypassing fat-finger cap, we must still verify margin."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(basket_required_total=200000.0, margin_net=50000.0)
    conns = _conns_with("ZG0790")
    qty_6_lots = 300

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      qty_6_lots,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "transaction_type": "BUY",
            "price":         22000.0,
            "trigger_price": 0,
            "intent":        "close",
        })

    # Should be blocked by MARGIN_SHORTFALL (not G2)
    assert result["ok"] is False, \
        f"Expected margin shortfall blocker. Got: {result}"

    codes = [b["code"] for b in result.get("blocked", [])]
    assert "MARGIN_SHORTFALL" in codes, \
        f"Expected MARGIN_SHORTFALL. Got: {codes}"
    assert "FAT_FINGER_5_LOT_CAP" not in codes, \
        f"G2 should be bypassed, not present. Got: {codes}"
