"""
Tests for the order preflight pipeline.

Covers:
  - happy path: all checks pass → ok=True
  - ACCOUNT_UNKNOWN blocker
  - QTY_FREEZE blocker
  - MARGIN_SHORTFALL blocker (mocked basket_margin)

Per project convention we do NOT mock broker API calls for the core
integration flows; the preflight helper is tested by controlling the
Connections singleton and patching the narrowest broker boundary
(kite.basket_margin on the KiteConnect object) for margin tests only.
"""

import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_kite_conn(api_secret: str = "secret"):
    """Build a minimal stub KiteConnection that the preflight helper needs."""
    kite_mock = MagicMock()

    # profile — returns the two exchanges we care about
    kite_mock.profile.return_value = {
        "exchanges": ["NSE", "NFO", "BSE", "MCX", "CDS"],
    }

    # instruments — returns one row with freeze_qty for NIFTY25APRFUT
    kite_mock.instruments.return_value = [
        {
            "tradingsymbol": "NIFTY25APRFUT",
            "exchange":      "NFO",
            "instrument_type": "FUT",
            "freeze_qty":    6000,
            "lot_size":      50,
            "tick_size":     0.05,
        }
    ]

    # basket_margin — default: success (no shortfall)
    kite_mock.basket_margin.return_value = [{
        "initial": {
            "total": 10000.0,
            "available": {"cash": 50000.0},
        }
    }]

    conn = MagicMock()
    conn.api_secret = api_secret
    conn.get_kite_conn.return_value = kite_mock
    return conn, kite_mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_happy_path():
    """All checks pass → ok=True, blocked is empty."""
    from backend.shared.helpers.connections import Connections
    from backend.api.algo.actions import run_preflight

    conn_stub, _kite = _make_kite_conn()
    conns = Connections.__new__(Connections)
    conns.conn = {"ZG0790": conn_stub}

    with patch("backend.shared.helpers.connections.Connections", return_value=conns):
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
    assert result["blocked"] == []
    assert result["diagnostics"]["basket_margin_used"] is not None


@pytest.mark.asyncio
async def test_preflight_account_unknown():
    """Account not in Connections → ACCOUNT_UNKNOWN blocker, skip rest."""
    from backend.api.algo.actions import run_preflight

    conns = MagicMock()
    conns.conn = {}  # empty — no accounts loaded

    with patch("backend.shared.helpers.connections.Connections", return_value=conns):
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

    conn_stub, kite_mock = _make_kite_conn()
    # freeze_qty is 6000; we'll request 8000
    kite_mock.instruments.return_value = [
        {
            "tradingsymbol": "NIFTY25APRFUT",
            "exchange":      "NFO",
            "instrument_type": "FUT",
            "freeze_qty":    6000,
            "lot_size":      50,
            "tick_size":     0.05,
        }
    ]
    conns = MagicMock()
    conns.conn = {"ZG0790": conn_stub}

    with patch("backend.shared.helpers.connections.Connections", return_value=conns):
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

    conn_stub, kite_mock = _make_kite_conn()
    # Override basket_margin to return a shortfall scenario
    kite_mock.basket_margin.return_value = [{
        "initial": {
            "total": 200000.0,               # required ₹2L
            "available": {"cash": 50000.0},  # only ₹50k available
        }
    }]
    conns = MagicMock()
    conns.conn = {"ZG0790": conn_stub}

    with patch("backend.shared.helpers.connections.Connections", return_value=conns):
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
