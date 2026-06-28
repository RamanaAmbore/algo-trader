"""
Tests for the order preflight pipeline.

Covers:
  - happy path: all checks pass → ok=True
  - ACCOUNT_UNKNOWN blocker
  - QTY_FREEZE blocker
  - MARGIN_SHORTFALL blocker (mocked basket_margin)

`run_preflight` now talks to the Broker ABC (profile / instruments /
basket_order_margins / margins / normalise_qty), resolved via the
registry's `get_broker(account)`. The tests therefore mock that
boundary — a stub Broker exposing the ABC methods. The previous
`broker.kite.X` mocking layer is obsolete (the refactor that
introduced Broker ABC moved the calls one layer up).
"""

import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_broker_stub(*,
                     enabled_exchanges: tuple[str, ...] = ("NSE", "NFO", "BSE", "MCX", "CDS"),
                     instruments: list[dict] | None = None,
                     basket_required_total: float = 10000.0,
                     margin_net: float = 500000.0,
                     margin_enabled: bool = True) -> MagicMock:
    """Build a stub Broker that satisfies every method run_preflight
    calls. Default values let the happy-path test pass; individual
    tests override the fields they exercise."""
    broker = MagicMock()
    broker.profile.return_value = {"exchanges": list(enabled_exchanges)}
    broker.instruments.return_value = instruments or [{
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange":      "NFO",
        "instrument_type": "FUT",
        "freeze_qty":    6000,
        "lot_size":      50,
        "tick_size":     0.05,
    }]
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
    """Build a Connections stub whose `.conn` dict contains the given
    account (value can be anything truthy; preflight only checks the
    KEY for ACCOUNT_UNKNOWN, then resolves the actual broker via
    get_broker()). Returns a MagicMock for patching."""
    c = MagicMock()
    c.conn = {account: object()}
    return c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_happy_path():
    """All checks pass → ok=True, blocked is empty."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns  = _conns_with("ZG0790")

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

    assert result["ok"] is True, f"unexpected blockers: {result['blocked']}"
    assert result["blocked"] == []
    assert result["diagnostics"]["basket_margin_used"] is not None


@pytest.mark.asyncio
async def test_preflight_account_unknown():
    """Account not in Connections → ACCOUNT_UNKNOWN blocker, skip rest."""
    from backend.api.algo.actions import run_preflight

    conns = MagicMock()
    conns.conn = {}  # empty — no accounts loaded

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
    # Further checks should not run — diagnostics untouched
    assert result["diagnostics"]["basket_margin_used"] is None


@pytest.mark.asyncio
async def test_preflight_qty_freeze():
    """Quantity exceeds freeze_qty → QTY_FREEZE blocker."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()    # default instruments carry freeze_qty=6000
    conns  = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      8000,   # > 6000 freeze limit
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "QTY_FREEZE" in codes
    freeze_blocker = next(b for b in result["blocked"] if b["code"] == "QTY_FREEZE")
    assert freeze_blocker["data"]["freeze_qty"] == 6000
    assert freeze_blocker["data"]["requested"]  == 8000


@pytest.mark.asyncio
async def test_preflight_margin_shortfall():
    """basket_margin reports required > available → MARGIN_SHORTFALL blocker."""
    from backend.api.algo.actions import run_preflight

    # Required ₹2L, available ₹50k — shortfall ₹1.5L.
    broker = _make_broker_stub(basket_required_total=200000.0, margin_net=50000.0)
    conns  = _conns_with("ZG0790")

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
    # Diagnostics should reflect the values.
    assert result["diagnostics"]["basket_margin_used"] == pytest.approx(200000.0)
    assert result["diagnostics"]["available_margin"]   == pytest.approx(50000.0)
    assert result["diagnostics"]["margin_shortfall"]   == pytest.approx(150000.0)
