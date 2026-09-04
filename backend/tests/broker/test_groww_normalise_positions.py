"""
Tests for Groww _normalise_positions() day_change_val computation.

Five quality dimensions:
  SSOT        — day_change_val computed once in _normalise_positions; no duplicate
  Correctness — formula matches Dhan adapter pattern: (ltp - close) * qty
  Performance — pure dict → no I/O; tests run in <1 ms each
  Reuse       — _gf/_gi helpers reused for ltp/close/qty extraction
  UX          — cold-cache (ltp=0) and missing-close (close=0) both collapse to 0.0

Scenario catalogue:
  1. All fields valid, long position → correct positive day_change_val
  2. All fields valid, short position (negative qty) → correct negative day_change_val
  3. ltp=0 (cold LTP cache on startup) → day_change_val=0.0
  4. close_price=0 (missing previous_close) → day_change_val=0.0
  5. qty=0 (flat position) → day_change_val=0.0
  6. Both ltp and close absent from payload → day_change_val=0.0
  7. close_price via fallback key "previous_close" → computed correctly
  8. ltp via fallback key "ltp" → computed correctly
  9. multiple positions in response → each row computed independently
 10. day_change_val present in output dict alongside last_price and close_price
"""

from __future__ import annotations

import pytest


def _call(rows: list[dict]) -> dict:
    """Wrap rows in the Groww positions response envelope and call _normalise_positions."""
    from backend.brokers.adapters.groww import _normalise_positions
    resp = {"data": {"positions": rows}}
    return _normalise_positions(resp)


# ---------------------------------------------------------------------------
# Scenario 1: valid long position
# ---------------------------------------------------------------------------

def test_day_change_val_long_position():
    result = _call([{
        "trading_symbol": "RELIANCE",
        "exchange": "NSE",
        "quantity": 10,
        "last_price": 2850.0,
        "close_price": 2800.0,
    }])
    row = result["net"][0]
    # (2850 - 2800) * 10 = 500.0
    assert row["day_change_val"] == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Scenario 2: valid short position (negative qty)
# ---------------------------------------------------------------------------

def test_day_change_val_short_position():
    result = _call([{
        "trading_symbol": "INFY",
        "exchange": "NSE",
        "quantity": -5,
        "last_price": 1600.0,
        "close_price": 1550.0,
    }])
    row = result["net"][0]
    # (1600 - 1550) * -5 = -250.0
    assert row["day_change_val"] == pytest.approx(-250.0)


# ---------------------------------------------------------------------------
# Scenario 3: ltp=0 — cold LTP cache on startup
# ---------------------------------------------------------------------------

def test_day_change_val_zero_when_ltp_cold():
    result = _call([{
        "trading_symbol": "TCS",
        "exchange": "NSE",
        "quantity": 10,
        "last_price": 0,
        "close_price": 3500.0,
    }])
    row = result["net"][0]
    assert row["day_change_val"] == 0.0


# ---------------------------------------------------------------------------
# Scenario 4: close_price=0 — previous_close missing
# ---------------------------------------------------------------------------

def test_day_change_val_zero_when_close_missing():
    result = _call([{
        "trading_symbol": "HDFC",
        "exchange": "NSE",
        "quantity": 8,
        "last_price": 1700.0,
        "close_price": 0,
    }])
    row = result["net"][0]
    assert row["day_change_val"] == 0.0


# ---------------------------------------------------------------------------
# Scenario 5: qty=0 — flat / closed position
# ---------------------------------------------------------------------------

def test_day_change_val_zero_when_qty_zero():
    result = _call([{
        "trading_symbol": "WIPRO",
        "exchange": "NSE",
        "quantity": 0,
        "last_price": 500.0,
        "close_price": 490.0,
    }])
    row = result["net"][0]
    assert row["day_change_val"] == 0.0


# ---------------------------------------------------------------------------
# Scenario 6: both ltp and close absent from payload
# ---------------------------------------------------------------------------

def test_day_change_val_zero_when_fields_absent():
    result = _call([{
        "trading_symbol": "ONGC",
        "exchange": "NSE",
        "quantity": 5,
        # no last_price / ltp / close_price / previous_close keys
    }])
    row = result["net"][0]
    assert row["day_change_val"] == 0.0


# ---------------------------------------------------------------------------
# Scenario 7: close_price via fallback key "previous_close"
# ---------------------------------------------------------------------------

def test_day_change_val_uses_previous_close_fallback():
    result = _call([{
        "trading_symbol": "SBIN",
        "exchange": "NSE",
        "quantity": 20,
        "last_price": 620.0,
        "previous_close": 600.0,   # fallback key — not close_price
    }])
    row = result["net"][0]
    # (620 - 600) * 20 = 400.0
    assert row["day_change_val"] == pytest.approx(400.0)


# ---------------------------------------------------------------------------
# Scenario 8: ltp via fallback key "ltp"
# ---------------------------------------------------------------------------

def test_day_change_val_uses_ltp_fallback():
    result = _call([{
        "trading_symbol": "AXISBANK",
        "exchange": "NSE",
        "quantity": 3,
        "ltp": 1050.0,             # fallback key — not last_price
        "close_price": 1000.0,
    }])
    row = result["net"][0]
    # (1050 - 1000) * 3 = 150.0
    assert row["day_change_val"] == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# Scenario 9: multiple positions computed independently
# ---------------------------------------------------------------------------

def test_day_change_val_multiple_positions():
    result = _call([
        {
            "trading_symbol": "RELIANCE",
            "exchange": "NSE",
            "quantity": 10,
            "last_price": 2850.0,
            "close_price": 2800.0,
        },
        {
            "trading_symbol": "TCS",
            "exchange": "NSE",
            "quantity": 0,            # flat — should be 0
            "last_price": 3700.0,
            "close_price": 3650.0,
        },
        {
            "trading_symbol": "INFY",
            "exchange": "NSE",
            "quantity": 5,
            "last_price": 1600.0,
            "close_price": 1580.0,
        },
    ])
    rows = result["net"]
    assert len(rows) == 3
    assert rows[0]["day_change_val"] == pytest.approx(500.0)   # (2850-2800)*10
    assert rows[1]["day_change_val"] == 0.0                    # qty=0
    assert rows[2]["day_change_val"] == pytest.approx(100.0)   # (1600-1580)*5


# ---------------------------------------------------------------------------
# Scenario 10: day_change_val present alongside last_price and close_price
# ---------------------------------------------------------------------------

def test_day_change_val_present_in_output_dict():
    result = _call([{
        "trading_symbol": "MARUTI",
        "exchange": "NSE",
        "quantity": 2,
        "last_price": 10500.0,
        "close_price": 10400.0,
    }])
    row = result["net"][0]
    assert "day_change_val" in row
    assert "last_price" in row
    assert "close_price" in row
    # Confirm last_price and close_price are still populated correctly
    assert row["last_price"] == pytest.approx(10500.0)
    assert row["close_price"] == pytest.approx(10400.0)
    # (10500 - 10400) * 2 = 200.0
    assert row["day_change_val"] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# translate_qty — MCX lot conversion (matches Kite convention)
# ---------------------------------------------------------------------------

class TestGrowwTranslateQtyMCX:
    def _broker(self):
        from unittest.mock import MagicMock
        from backend.brokers.adapters.groww import GrowwBroker
        conn = MagicMock()
        conn.account = "test"
        conn._source_ip = None
        b = GrowwBroker.__new__(GrowwBroker)
        b._conn = conn
        return b

    def test_mcx_contracts_returned_raw(self):
        b = self._broker()
        # Groww uses CONTRACTS for all exchanges including MCX — no lot conversion.
        # CRUDEOIL 1 lot = 100 contracts, but Groww sends 100 → must return 100.
        assert b.translate_qty("MCX", 100, 100) == 100

    def test_mcx_multiple_contracts_returned_raw(self):
        b = self._broker()
        # 200 contracts sent by Groww → must stay 200, not divide by 100.
        assert b.translate_qty("MCX", 200, 100) == 200

    def test_mcx_naturalgas_returned_raw(self):
        b = self._broker()
        # NATURALGAS lot_size=1250; Groww sends 1250 contracts → stay 1250.
        assert b.translate_qty("MCX", 1250, 1250) == 1250

    def test_nco_contracts_returned_raw(self):
        b = self._broker()
        # NCO: Groww uses contracts, no conversion needed.
        assert b.translate_qty("NCO", 100, 100) == 100

    def test_nfo_no_translation(self):
        b = self._broker()
        # NFO stays in contracts — no change
        assert b.translate_qty("NFO", 250, 250) == 250

    def test_nse_equity_no_translation(self):
        b = self._broker()
        assert b.translate_qty("NSE", 10, 1) == 10

    def test_mcx_lot_size_one_returned_raw(self):
        b = self._broker()
        # lot_size=1 on MCX: Groww still returns contracts as-is.
        assert b.translate_qty("MCX", 100, 1) == 100

    def test_mcx_lot_size_zero_returned_raw(self):
        b = self._broker()
        # lot_size=0: Groww override ignores lot_size entirely.
        assert b.translate_qty("MCX", 100, 0) == 100

    def test_mcx_sub_lot_passes_through(self):
        b = self._broker()
        # sub-lot qty: Groww returns raw contracts unchanged.
        assert b.translate_qty("MCX", 10, 100) == 10
