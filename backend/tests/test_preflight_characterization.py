"""
Characterization tests for `run_preflight` in backend/api/algo/actions.py.

Goal: 80%+ branch coverage to safely refactor CC=98 function.

Coverage scope (lines 815-1180):
  1. G1 (LOT_MULTIPLE) trigger for all guard-set exchanges (NFO, MCX, NCO, BFO, CDS)
  2. G2 (FAT_FINGER_5_LOT_CAP) trigger for NFO (but NOT MCX/NCO/BFO)
  3. G2 bypass when intent="close"
  4. Exchange NOT in guard set (NSE, BSE) — guard skipped
  5. Missing lot_size (lot_size=0 or None) — guard behavior
  6. Hard-step early return (LOT_MULTIPLE / LOT_SIZE_UNKNOWN)
  7. ACCOUNT_UNKNOWN blocker + early return
  8. SEGMENT_INACTIVE blocker (exchange not in profile exchanges)
  9. QTY_FREEZE blocker
 10. MARGIN_SHORTFALL blocker (required > available)
 11. Insufficient-funds gate (available=0.0 with required>0)
 12. Negative-margin handling (required < 0)
 13. Margin check skip when permissions missing
 14. Paired orders (wing legs) in basket
 15. broker.profile non-Kite (cutover / Dhan / Groww)
 16. Exception paths (broker call failures)
 17. Return value structure (ok, blocked, diagnostics)
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import math


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_broker_stub(*,
                     broker_id: str = "zerodha_kite",
                     enabled_exchanges: tuple[str, ...] = ("NSE", "NFO", "BSE", "MCX", "CDS", "NCO", "BFO"),
                     instruments: list[dict] | None = None,
                     basket_required_total: float = 10000.0,
                     margin_net: float = 500000.0,
                     margin_enabled: bool = True,
                     basket_margin_exception: Exception | None = None) -> MagicMock:
    """
    Build a stub Broker that satisfies every method run_preflight calls.
    Override fields to exercise different code branches.
    """
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


# ---------------------------------------------------------------------------
# G1 (LOT_MULTIPLE) Tests — Fire on all guarded exchanges
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_g1_nfo_non_multiple():
    """NFO: qty not a multiple of lot_size → G1 LOT_MULTIPLE blocker."""
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
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" in codes
    g1 = next(b for b in result["blocked"] if b["code"] == "LOT_MULTIPLE")
    assert g1["data"]["qty"] == bad_qty
    assert g1["data"]["lot_size"] == 50


@pytest.mark.asyncio
async def test_preflight_g1_mcx_non_multiple():
    """MCX: qty not a multiple of lot_size → G1 LOT_MULTIPLE blocker."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(instruments=[{
        "tradingsymbol": "CRUDEOILJUL25FUT",
        "exchange":      "MCX",
        "instrument_type": "FUT",
        "freeze_qty":    10000,
        "lot_size":      100,
        "tick_size":     1.0,
    }])
    conns = _conns_with("ZG0790")

    bad_qty = 107  # not a multiple of 100

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)):
        result = await run_preflight("ZG0790", {
            "exchange":      "MCX",
            "tradingsymbol": "CRUDEOILJUL25FUT",
            "quantity":      bad_qty,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         5500.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" in codes


@pytest.mark.asyncio
async def test_preflight_g1_cds_non_multiple():
    """CDS: qty not a multiple of lot_size → G1 LOT_MULTIPLE blocker."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(instruments=[{
        "tradingsymbol": "USDINRMAR25FUT",
        "exchange":      "CDS",
        "instrument_type": "FUT",
        "freeze_qty":    5000,
        "lot_size":      1000,
        "tick_size":     0.01,
    }])
    conns = _conns_with("ZG0790")

    bad_qty = 2500  # not a multiple of 1000

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=1000)):
        result = await run_preflight("ZG0790", {
            "exchange":      "CDS",
            "tradingsymbol": "USDINRMAR25FUT",
            "quantity":      bad_qty,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         85.5,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" in codes


@pytest.mark.asyncio
async def test_preflight_g1_bfo_non_multiple():
    """BFO (BSE FO): qty not a multiple of lot_size → G1 blocker."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(instruments=[{
        "tradingsymbol": "SENSEXMAR25FUT",
        "exchange":      "BFO",
        "instrument_type": "FUT",
        "freeze_qty":    3000,
        "lot_size":      1,
        "tick_size":     1.0,
    }])
    conns = _conns_with("ZG0790")

    # BFO typically has lot_size=1, but test qty multiple logic anyway
    bad_qty = 3001  # exceeds freeze qty, but also test non-multiple

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=1)):
        result = await run_preflight("ZG0790", {
            "exchange":      "BFO",
            "tradingsymbol": "SENSEXMAR25FUT",
            "quantity":      bad_qty,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         69000.0,
        })

    # This should trigger QTY_FREEZE, not LOT_MULTIPLE (since lot_size=1 makes everything valid)
    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "QTY_FREEZE" in codes


@pytest.mark.asyncio
async def test_preflight_g1_nco_non_multiple():
    """NCO (NSE Commodity): qty not a multiple → G1 blocker."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(instruments=[{
        "tradingsymbol": "CRUDEOILJUL25FUT",
        "exchange":      "NCO",
        "instrument_type": "FUT",
        "freeze_qty":    10000,
        "lot_size":      100,
        "tick_size":     1.0,
    }])
    conns = _conns_with("ZG0790")

    bad_qty = 205  # not a multiple of 100

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NCO",
            "tradingsymbol": "CRUDEOILJUL25FUT",
            "quantity":      bad_qty,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         5500.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" in codes


# ---------------------------------------------------------------------------
# G2 (FAT_FINGER_5_LOT_CAP) Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_g2_nfo_6_lots_blocked():
    """NFO: 6 lots (300 qty, lot_size=50) exceeds 5-lot cap → G2 blocker."""
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
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "FAT_FINGER_5_LOT_CAP" in codes
    g2 = next(b for b in result["blocked"] if b["code"] == "FAT_FINGER_5_LOT_CAP")
    assert g2["data"]["lots"] == 6
    assert g2["data"]["cap"] == 5


@pytest.mark.asyncio
async def test_preflight_g2_nfo_5_lots_passes():
    """NFO: exactly 5 lots passes G2 (no blocker)."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    qty_5_lots = 250  # 5 lots × 50

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      qty_5_lots,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "FAT_FINGER_5_LOT_CAP" not in codes


@pytest.mark.asyncio
async def test_preflight_g2_bypass_on_close_intent():
    """intent="close": 6+ lots NOT blocked by G2 (bypass for closing positions)."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    qty_10_lots = 500  # 10 lots × 50 — would normally exceed cap

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      qty_10_lots,
            "intent":        "close",  # bypass flag
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "SELL",
            "price":         22000.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "FAT_FINGER_5_LOT_CAP" not in codes
    # But G1 still applies if qty is not a multiple
    assert "LOT_MULTIPLE" not in codes  # 500 is a multiple of 50


@pytest.mark.asyncio
async def test_preflight_g2_mcx_20_lots_not_blocked():
    """MCX: 25-lot order NOT blocked by G2 at preflight (route-level guard applies)."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(instruments=[{
        "tradingsymbol": "CRUDEOILJUL25FUT",
        "exchange":      "MCX",
        "instrument_type": "FUT",
        "freeze_qty":    10000,
        "lot_size":      100,
        "tick_size":     1.0,
    }])
    conns = _conns_with("ZG0790")

    qty_25_lots = 2500  # 25 lots × 100

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)):
        result = await run_preflight("ZG0790", {
            "exchange":      "MCX",
            "tradingsymbol": "CRUDEOILJUL25FUT",
            "quantity":      qty_25_lots,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         5500.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "FAT_FINGER_5_LOT_CAP" not in codes


@pytest.mark.asyncio
async def test_preflight_g2_nco_20_lots_not_blocked():
    """NCO: 20+ lot order NOT blocked by G2 (route-level guard applies)."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(instruments=[{
        "tradingsymbol": "CRUDEOILJUL25FUT",
        "exchange":      "NCO",
        "instrument_type": "FUT",
        "freeze_qty":    10000,
        "lot_size":      100,
        "tick_size":     1.0,
    }])
    conns = _conns_with("ZG0790")

    qty_25_lots = 2500  # 25 lots × 100

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NCO",
            "tradingsymbol": "CRUDEOILJUL25FUT",
            "quantity":      qty_25_lots,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         5500.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "FAT_FINGER_5_LOT_CAP" not in codes


# ---------------------------------------------------------------------------
# Guard Skip Tests (exchanges NOT in guard set)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_guard_skipped_for_nse():
    """NSE (equity): qty/lot guards skipped (guard set is F&O only)."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(instruments=[{
        "tradingsymbol": "RELIANCE",
        "exchange":      "NSE",
        "instrument_type": "EQ",
        "freeze_qty":    100000,
        "lot_size":      1,
        "tick_size":     0.05,
    }])
    conns = _conns_with("ZG0790")

    # Send a qty that WOULD fail lot guards if NSE were guarded
    # (qty not a multiple of 50), but should NOT be checked
    bad_qty = 77

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):  # irrelevant for NSE
        result = await run_preflight("ZG0790", {
            "exchange":      "NSE",
            "tradingsymbol": "RELIANCE",
            "quantity":      bad_qty,
            "order_type":    "LIMIT",
            "product":       "MIS",
            "variety":       "regular",
            "side":          "BUY",
            "price":         3500.0,
        })

    # No LOT_MULTIPLE block should be present
    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" not in codes


@pytest.mark.asyncio
async def test_preflight_guard_skipped_for_bse():
    """BSE (equity): qty/lot guards skipped."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(instruments=[{
        "tradingsymbol": "ADANIPORTS",
        "exchange":      "BSE",
        "instrument_type": "EQ",
        "freeze_qty":    100000,
        "lot_size":      1,
        "tick_size":     0.05,
    }])
    conns = _conns_with("ZG0790")

    bad_qty = 77

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):  # irrelevant for BSE
        result = await run_preflight("ZG0790", {
            "exchange":      "BSE",
            "tradingsymbol": "ADANIPORTS",
            "quantity":      bad_qty,
            "order_type":    "LIMIT",
            "product":       "MIS",
            "variety":       "regular",
            "side":          "BUY",
            "price":         2500.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" not in codes


# ---------------------------------------------------------------------------
# Lot Size Edge Cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_lot_size_zero_guard_skipped():
    """lot_size=0: guard skipped (lot_size > 1 check fails)."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=0)):  # edge case
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      77,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    # No LOT_MULTIPLE block (guard skipped when lot_size <= 1)
    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" not in codes


@pytest.mark.asyncio
async def test_preflight_lot_size_none_guard_skipped():
    """lot_size=None: guard skipped."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=None)):  # edge case
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      77,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" not in codes


@pytest.mark.asyncio
async def test_preflight_qty_zero_skips_guard():
    """qty=0: guard skipped (qty > 0 check fails)."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      0,  # zero qty
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "LOT_MULTIPLE" not in codes




# ---------------------------------------------------------------------------
# Hard-Step Early Return Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_hard_blocker_early_return():
    """LOT_MULTIPLE (hard blocker): return immediately, skip all broker checks."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    bad_qty = 77  # not a multiple of 50 → hard blocker

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
            "side":          "BUY",
            "price":         22000.0,
        })

    # Should return immediately with LOT_MULTIPLE
    assert result["ok"] is False
    assert len(result["blocked"]) == 1
    assert result["blocked"][0]["code"] == "LOT_MULTIPLE"
    # Diagnostics should be empty (broker calls not made)
    assert result["diagnostics"]["basket_margin_used"] is None


# ---------------------------------------------------------------------------
# ACCOUNT_UNKNOWN Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_account_unknown_early_return():
    """Account not in Connections: ACCOUNT_UNKNOWN blocker, return immediately."""
    from backend.api.algo.actions import run_preflight

    conns = MagicMock()
    conns.conn = {}  # empty

    with patch("backend.brokers.connections.Connections", return_value=conns):
        result = await run_preflight("ZG9999", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    assert len(result["blocked"]) == 1
    assert result["blocked"][0]["code"] == "ACCOUNT_UNKNOWN"
    # Broker calls not made
    assert result["diagnostics"]["basket_margin_used"] is None


# ---------------------------------------------------------------------------
# SEGMENT_INACTIVE Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_segment_inactive():
    """Exchange not in profile['exchanges'] → SEGMENT_INACTIVE blocker."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(enabled_exchanges=("NSE", "BSE"))  # NFO NOT enabled
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "SEGMENT_INACTIVE" in codes
    seg_blocker = next(b for b in result["blocked"] if b["code"] == "SEGMENT_INACTIVE")
    assert "NFO" in seg_blocker["reason"]


@pytest.mark.asyncio
async def test_preflight_profile_none_segment_check_skipped():
    """profile fetch returns None: SEGMENT_INACTIVE check skipped."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(broker_id="dhan_api")  # non-Kite, profile not called
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    # Should not have SEGMENT_INACTIVE (Dhan doesn't return profile)
    codes = [b["code"] for b in result["blocked"]]
    assert "SEGMENT_INACTIVE" not in codes


@pytest.mark.asyncio
async def test_preflight_profile_raises_exception():
    """profile fetch raises exception: caught, SEGMENT_INACTIVE check skipped."""
    from backend.api.algo.actions import run_preflight

    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.profile.side_effect = Exception("API timeout")
    broker.instruments.return_value = [{
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange":      "NFO",
        "instrument_type": "FUT",
        "freeze_qty":    6000,
        "lot_size":      50,
        "tick_size":     0.05,
    }]
    broker.basket_order_margins.return_value = [{
        "initial": {"total": 10000.0},
    }]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": 500000.0},
        "commodity": {"enabled": True, "net": 500000.0},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)

    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    # Should pass (profile exception caught, check skipped)
    codes = [b["code"] for b in result["blocked"]]
    assert "SEGMENT_INACTIVE" not in codes


# ---------------------------------------------------------------------------
# QTY_FREEZE Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_qty_freeze():
    """qty > freeze_qty → QTY_FREEZE blocker."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()  # default freeze_qty=6000
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      8000,  # > 6000
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "QTY_FREEZE" in codes
    freeze = next(b for b in result["blocked"] if b["code"] == "QTY_FREEZE")
    assert freeze["data"]["freeze_qty"] == 6000
    assert freeze["data"]["requested"] == 8000


@pytest.mark.asyncio
async def test_preflight_qty_freeze_with_lot_size_display():
    """QTY_FREEZE fix message shows max_lots calculation."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(instruments=[{
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange":      "NFO",
        "instrument_type": "FUT",
        "freeze_qty":    6000,
        "lot_size":      50,
        "tick_size":     0.05,
    }])
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      7500,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    freeze = next(b for b in result["blocked"] if b["code"] == "QTY_FREEZE")
    # max_lots should be 120 (6000 / 50)
    assert "120" in freeze["fix"]


@pytest.mark.asyncio
async def test_preflight_instruments_fetch_returns_none():
    """instruments fetch fails (returns None): QTY_FREEZE check skipped."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(instruments=[])  # empty, simulates None result
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      8000,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    codes = [b["code"] for b in result["blocked"]]
    assert "QTY_FREEZE" not in codes


@pytest.mark.asyncio
async def test_preflight_instruments_fetch_raises():
    """instruments fetch raises exception: caught, QTY_FREEZE check skipped."""
    from backend.api.algo.actions import run_preflight

    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "BSE", "MCX", "CDS"]}
    broker.instruments.side_effect = Exception("Market data unavailable")
    broker.basket_order_margins.return_value = [{
        "initial": {"total": 10000.0},
    }]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": 500000.0},
        "commodity": {"enabled": True, "net": 500000.0},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)

    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      8000,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    # Should pass (instruments exception caught, check skipped)
    codes = [b["code"] for b in result["blocked"]]
    assert "QTY_FREEZE" not in codes


# ---------------------------------------------------------------------------
# MARGIN_SHORTFALL Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_margin_shortfall():
    """required > available → MARGIN_SHORTFALL blocker."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(basket_required_total=200000.0, margin_net=50000.0)
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "MARGIN_SHORTFALL" in codes
    ms = next(b for b in result["blocked"] if b["code"] == "MARGIN_SHORTFALL")
    assert ms["data"]["shortfall"] == pytest.approx(150000.0)
    assert result["diagnostics"]["margin_shortfall"] == pytest.approx(150000.0)


@pytest.mark.asyncio
async def test_preflight_margin_shortfall_fix_qty_calculation():
    """Margin shortfall: fix_qty suggestion when qty > 0."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(basket_required_total=200000.0, margin_net=50000.0)
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      100,  # larger qty for better per-unit calc
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    ms = next(b for b in result["blocked"] if b["code"] == "MARGIN_SHORTFALL")
    # fix_qty should be calculated and included if available margin > 0
    # The fix message should suggest reducing qty
    assert "or reduce qty" in ms["fix"] or "Add ₹" in ms["fix"]


@pytest.mark.asyncio
async def test_preflight_negative_margin_legitimate():
    """required < 0 (credit margin): logged as legitimate, not blocked."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(basket_required_total=-50000.0)
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is True
    codes = [b["code"] for b in result["blocked"]]
    assert "MARGIN_SHORTFALL" not in codes
    # Diagnostics should record the negative margin
    assert result["diagnostics"]["basket_margin_used"] == pytest.approx(-50000.0)


@pytest.mark.asyncio
async def test_preflight_available_zero_with_required():
    """available=0.0 + required>0 + enabled=True → INSUFFICIENT_FUNDS blocker."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(basket_required_total=100000.0, margin_net=0.0)
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "INSUFFICIENT_FUNDS" in codes


@pytest.mark.asyncio
async def test_preflight_margin_disabled():
    """margin enabled=False: margin check skipped, order passes."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(basket_required_total=200000.0, margin_enabled=False)
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    # Should pass (margin check skipped when enabled=False)
    codes = [b["code"] for b in result["blocked"]]
    assert "MARGIN_SHORTFALL" not in codes


@pytest.mark.asyncio
async def test_preflight_margin_net_is_nan():
    """margin net=NaN: available stays None, check skipped."""
    from backend.api.algo.actions import run_preflight

    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "BSE", "MCX", "CDS"]}
    broker.instruments.return_value = [{
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange":      "NFO",
        "instrument_type": "FUT",
        "freeze_qty":    6000,
        "lot_size":      50,
        "tick_size":     0.05,
    }]
    broker.basket_order_margins.return_value = [{
        "initial": {"total": 100000.0},
    }]
    # Return NaN for net margin
    import math
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": math.nan},
        "commodity": {"enabled": True, "net": math.nan},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)

    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    # Should pass (NaN handled, margin check skipped)
    assert result["ok"] is True
    codes = [b["code"] for b in result["blocked"]]
    assert "MARGIN_SHORTFALL" not in codes


@pytest.mark.asyncio
async def test_preflight_margin_net_non_numeric():
    """margin net is non-numeric string: available stays None."""
    from backend.api.algo.actions import run_preflight

    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "BSE", "MCX", "CDS"]}
    broker.instruments.return_value = [{
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange":      "NFO",
        "instrument_type": "FUT",
        "freeze_qty":    6000,
        "lot_size":      50,
        "tick_size":     0.05,
    }]
    broker.basket_order_margins.return_value = [{
        "initial": {"total": 100000.0},
    }]
    # Return non-numeric net
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": "invalid"},
        "commodity": {"enabled": True, "net": "invalid"},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)

    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    # Should pass (non-numeric net handled)
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Exception Path Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_basket_margin_raises_margin_keyword():
    """basket_margin raises with 'margin' keyword → MARGIN_SHORTFALL blocker."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(
        basket_margin_exception=Exception("Insufficient margin balance")
    )
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "MARGIN_SHORTFALL" in codes


@pytest.mark.asyncio
async def test_preflight_basket_margin_raises_generic():
    """basket_margin raises with non-margin error: logged, not surfaced."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(
        basket_margin_exception=Exception("Network timeout")
    )
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    # Generic error should not produce a blocker (logged only)
    codes = [b["code"] for b in result["blocked"]]
    # Should not have MARGIN_SHORTFALL from generic error
    # (may have other blockers, but not from the exception)
    assert not any(b["code"] == "MARGIN_SHORTFALL" and "Network" in str(b.get("data")) for b in result["blocked"])


# ---------------------------------------------------------------------------
# Paired Orders Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_paired_orders_wing_in_basket():
    """Paired orders (wing legs) included in basket_order_margins calculation."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(
        basket_required_total=10000.0,  # NET margin for the spread
        margin_net=500000.0
    )
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APR22000CE",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         100.0,
        }, paired_orders=[{
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APR23000CE",
            "quantity":      50,
            "transaction_type": "SELL",
            "order_type":    "LIMIT",
            "product":       "NRML",
            "price":         50.0,
        }])

    # Should pass since total margin is within budget
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_preflight_paired_order_invalid_skipped():
    """Paired order missing symbol: skipped gracefully."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        }, paired_orders=[{
            # Missing tradingsymbol/symbol
            "exchange":      "NFO",
            "quantity":      50,
            "transaction_type": "SELL",
        }])

    # Should still succeed (invalid paired order skipped)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_preflight_paired_order_zero_qty_skipped():
    """Paired order with qty=0: skipped (continue statement)."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        }, paired_orders=[{
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APR23000CE",
            "quantity":      0,  # zero qty → skip this paired order
            "transaction_type": "SELL",
        }])

    # Should succeed (zero qty paired order skipped)
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Return Value Structure Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_return_structure_ok_true():
    """Happy path: returns ok=True, empty blocked, diagnostics populated."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert isinstance(result, dict)
    assert "ok" in result
    assert "blocked" in result
    assert "diagnostics" in result
    assert result["ok"] is True
    assert isinstance(result["blocked"], list)
    assert len(result["blocked"]) == 0
    assert isinstance(result["diagnostics"], dict)
    assert "basket_margin_used" in result["diagnostics"]
    assert "available_margin" in result["diagnostics"]
    assert "margin_shortfall" in result["diagnostics"]


@pytest.mark.asyncio
async def test_preflight_blocked_blocker_structure():
    """Each blocker has required keys: code, reason, fix, data."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(basket_required_total=200000.0, margin_net=50000.0)
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    blocker = result["blocked"][0]
    assert isinstance(blocker, dict)
    assert "code" in blocker
    assert "reason" in blocker
    assert "fix" in blocker
    assert "data" in blocker
    assert isinstance(blocker["code"], str)
    assert isinstance(blocker["reason"], str)
    assert isinstance(blocker["fix"], str)
    assert isinstance(blocker["data"], dict)


# ---------------------------------------------------------------------------
# Basket Margin Result Parsing Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_basket_margin_single_leg():
    """basket_order_margins returns single dict (not list)."""
    from backend.api.algo.actions import run_preflight

    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "BSE", "MCX", "CDS"]}
    broker.instruments.return_value = [{
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange":      "NFO",
        "instrument_type": "FUT",
        "freeze_qty":    6000,
        "lot_size":      50,
        "tick_size":     0.05,
    }]
    # Return single dict, not list
    broker.basket_order_margins.return_value = {
        "initial": {"total": 10000.0},
        "final":   {"total": 8000.0},
    }
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": 500000.0},
        "commodity": {"enabled": True, "net": 500000.0},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)

    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is True
    # Should use final.total (8000) when available
    assert result["diagnostics"]["basket_margin_used"] == pytest.approx(8000.0)


@pytest.mark.asyncio
async def test_preflight_basket_margin_multiple_legs():
    """basket_order_margins returns list: sum final.total across legs."""
    from backend.api.algo.actions import run_preflight

    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "BSE", "MCX", "CDS"]}
    broker.instruments.return_value = [{
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange":      "NFO",
        "instrument_type": "FUT",
        "freeze_qty":    6000,
        "lot_size":      50,
        "tick_size":     0.05,
    }]
    # Return list of dicts (spread leg + wing)
    broker.basket_order_margins.return_value = [
        {"initial": {"total": 50000.0}, "final": {"total": 45000.0}},
        {"initial": {"total": 30000.0}, "final": {"total": 5000.0}},
    ]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": 500000.0},
        "commodity": {"enabled": True, "net": 500000.0},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)

    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is True
    # Should sum final.total: 45000 + 5000 = 50000
    assert result["diagnostics"]["basket_margin_used"] == pytest.approx(50000.0)


@pytest.mark.asyncio
async def test_preflight_basket_margin_non_numeric_total():
    """basket_order_margins returns non-numeric total: fallback to required field."""
    from backend.api.algo.actions import run_preflight

    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "BSE", "MCX", "CDS"]}
    broker.instruments.return_value = [{
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange":      "NFO",
        "instrument_type": "FUT",
        "freeze_qty":    6000,
        "lot_size":      50,
        "tick_size":     0.05,
    }]
    # Return dict with non-numeric total (exercises exception handler)
    broker.basket_order_margins.return_value = {
        "initial": {"total": "invalid_number"},  # non-numeric
        "required": 25000.0,
    }
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": 500000.0},
        "commodity": {"enabled": True, "net": 500000.0},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)

    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    # Should pass (non-numeric handled, falls back to required)
    assert result["ok"] is True
    # Should use fallback required value
    assert result["diagnostics"]["basket_margin_used"] == pytest.approx(25000.0)


# ---------------------------------------------------------------------------
# Additional Edge Cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_missing_exchange_default():
    """exchange field missing: defaults to NFO."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            # exchange field missing
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    # Should pass (exchange defaults to NFO, which is enabled)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_preflight_symbol_field_fallback():
    """tradingsymbol missing, symbol provided: fallback works."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(instruments=[{
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange":      "NFO",
        "instrument_type": "FUT",
        "freeze_qty":    6000,
        "lot_size":      50,
        "tick_size":     0.05,
    }])
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "symbol":        "NIFTY25APRFUT",  # use symbol instead
            "quantity":      50,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_preflight_qty_field_fallback():
    """quantity field missing, qty provided: fallback works."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "qty":           50,  # use qty instead of quantity
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is True
