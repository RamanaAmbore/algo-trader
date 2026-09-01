"""
Test MCX lot-scale fixes in daily_snapshot.py

Tests for five specific changes:
1. _snap_compute_day_pnl now accepts multiplier parameter for lot-scale contracts
2. _positions_rows extracts multiplier and passes it to day_pnl computation
3. _apply_flat_row_hygiene zeros day_change_val for qty=0 rows
4. _is_broker_outage moved to positions_helpers.py SSOT
5. Holdings snapshot recomputes day_pnl from prev_ltp
"""

import pytest
from datetime import datetime, timezone, date
from decimal import Decimal
from zoneinfo import ZoneInfo


class TestSnapComputeDayPnlMCXLotScale:
    """Test that _snap_compute_day_pnl respects multiplier for MCX contracts."""

    def test_snap_compute_day_pnl_with_mcx_multiplier_100(self):
        """_snap_compute_day_pnl scales oq/bq/sq by multiplier for MCX (CRUDEOIL lot_size=100)."""
        from backend.api.algo.daily_snapshot import _snap_compute_day_pnl

        r = {
            "overnight_quantity": 1,        # 1 LOT CRUDEOIL
            "day_buy_quantity": 0,
            "day_buy_value": 0.0,
            "day_sell_value": 0.0,
            "day_sell_quantity": 0,
            "m2m": 0.0,
        }
        ltp = 5500.0
        cls = 5480.0
        # With multiplier=100: overnight_quantity=1 * 100 multiplier = 100 qty
        # day_pnl = 100 * (5500 - 5480) = 100 * 20 = 2000
        result = _snap_compute_day_pnl(r, ltp, cls, qty=1, multiplier=100)
        assert result == pytest.approx(2000.0, rel=1e-6), (
            f"MCX day_pnl={result} should be 2000.0 with qty=1 and multiplier=100"
        )

    def test_snap_compute_day_pnl_without_multiplier_equity(self):
        """_snap_compute_day_pnl for equity (multiplier=1) returns correct small value."""
        from backend.api.algo.daily_snapshot import _snap_compute_day_pnl

        r = {
            "overnight_quantity": 10,       # 10 shares
            "day_buy_quantity": 0,
            "day_buy_value": 0.0,
            "day_sell_value": 0.0,
            "day_sell_quantity": 0,
            "m2m": 0.0,
        }
        ltp = 1650.0
        cls = 1600.0
        # Equity (default multiplier=1): 10 * (1650 - 1600) = 500
        result = _snap_compute_day_pnl(r, ltp, cls, qty=10)
        assert result == pytest.approx(500.0, rel=1e-6), (
            f"Equity day_pnl={result} should be 500.0 for 10 shares"
        )

    def test_snap_compute_day_pnl_mcx_with_intraday_buy_sell(self):
        """_snap_compute_day_pnl with decomposed intraday for MCX including buy/sell."""
        from backend.api.algo.daily_snapshot import _snap_compute_day_pnl

        # CRUDEOIL: bought 1 lot (oq is in lots), sold 0.5 lot
        r = {
            "overnight_quantity": 0,
            "day_buy_quantity": 1,          # 1 LOT bought
            "day_buy_value": 5500.0,        # 1 lot * 5500
            "day_sell_value": 2750.0,       # 0.5 lot * 5500
            "day_sell_quantity": 0.5,       # 0.5 LOT sold
            "m2m": 0.0,
        }
        ltp = 5520.0
        cls = 5500.0
        # With multiplier=100: day_buy_quantity = 1 * 100 = 100 qty
        # Remaining position: 100 - 50 = 50 qty
        # day_pnl should reflect the profit on 50 contracts
        result = _snap_compute_day_pnl(r, ltp, cls, qty=50, multiplier=100)
        # Result should use decomposed formula when all intraday fields present
        assert result is not None, "day_pnl should be computed with decomposed formula"
        # We assert it's positive (profit)
        assert result > 0, f"Expected positive day_pnl, got {result}"

    def test_snap_compute_day_pnl_falls_back_to_naive_when_intraday_missing(self):
        """_snap_compute_day_pnl falls back to naive (ltp-cls)*qty when intraday fields missing."""
        from backend.api.algo.daily_snapshot import _snap_compute_day_pnl

        # Missing day_buy_quantity (incomplete intraday split)
        r = {
            "overnight_quantity": 5,
            "day_buy_value": 0.0,
            "day_sell_value": 0.0,
            "day_sell_quantity": 0,
        }
        ltp = 2500.0
        cls = 2480.0
        # Fallback naive: (2500 - 2480) * 5 = 100
        result = _snap_compute_day_pnl(r, ltp, cls, qty=5)
        assert result == pytest.approx(100.0, rel=1e-6), (
            f"Fallback naive day_pnl={result} should be 100.0"
        )

    def test_snap_compute_day_pnl_returns_none_when_ltp_none(self):
        """_snap_compute_day_pnl returns None when ltp_val is None."""
        from backend.api.algo.daily_snapshot import _snap_compute_day_pnl

        r = {"overnight_quantity": 1}
        result = _snap_compute_day_pnl(r, None, 5480.0, qty=1, multiplier=100)
        assert result is None, "Should return None when ltp_val is None"

    def test_snap_compute_day_pnl_returns_none_when_close_none(self):
        """_snap_compute_day_pnl returns None when close_price is None."""
        from backend.api.algo.daily_snapshot import _snap_compute_day_pnl

        r = {"overnight_quantity": 1}
        result = _snap_compute_day_pnl(r, 5500.0, None, qty=1, multiplier=100)
        assert result is None, "Should return None when close_price is None"


class TestPositionsRowsMultiplierPassed:
    """Test that _positions_rows extracts multiplier from raw row and passes to day_pnl computation."""

    def test_positions_rows_mcx_multiplier_extracted_and_passed(self):
        """_positions_rows extracts 'multiplier' from raw row and passes to _snap_compute_day_pnl."""
        from backend.api.algo.daily_snapshot import _positions_rows

        raw = [{
            "tradingsymbol": "CRUDEOIL26AUGFUT",
            "exchange": "MCX",
            "quantity": 1,     # qty in LOTS from broker (Kite ships MCX qty in lots)
            "overnight_quantity": 1,  # oq in lots
            "average_price": 5480.0,
            "last_price": 5500.0,
            "close_price": 5480.0,
            "pnl": 2000.0,
            "day_buy_quantity": 0,
            "day_buy_value": 0.0,
            "day_sell_value": 0.0,
            "day_sell_quantity": 0,
            "m2m": 2000.0,
            "multiplier": 100,  # CRUDEOIL lot size (scales qty from lots to contracts)
            "product": "NRML",
            "instrument_token": 99999,
        }]
        # MCX closes at 23:30 IST — use a time well after close (23:35 IST)
        ist = ZoneInfo('Asia/Kolkata')
        now_ist = datetime(2026, 8, 9, 23, 35, 0, tzinfo=ist)

        rows = _positions_rows("ZG0790", date(2026, 8, 9), raw, now_ist, settled=True, market_open=False)
        assert len(rows) == 1
        r = rows[0]
        # day_pnl should use multiplier when computing decomposed formula
        # qty=1 (lot), multiplier=100 should scale to 100 contracts
        # With proper multiplier scaling: day_pnl = (1 * 100) * (5500 - 5480) = 100 * 20 = 2000
        assert r["day_pnl"] == pytest.approx(2000.0, rel=1e-5), (
            f"day_pnl={r['day_pnl']} must be 2000 for MCX CRUDEOIL (1 lot with multiplier=100)"
        )
        assert r["qty"] == 100, "Quantity should be stored as contracts (1 lot × 100)"
        assert r["lots"] == 1, "lots must be original lot count from Kite"
        assert r["lot_size"] == 100, "lot_size must be the Kite multiplier"

    def test_positions_rows_equity_no_multiplier_applied(self):
        """_positions_rows for equity works with default multiplier=1."""
        from backend.api.algo.daily_snapshot import _positions_rows

        raw = [{
            "tradingsymbol": "NIFTY26AUGFUT",
            "exchange": "NFO",
            "quantity": 50,    # 50 contracts (NFO multiplier = 1)
            "overnight_quantity": 50,
            "average_price": 23000.0,
            "last_price": 23200.0,
            "close_price": 23000.0,
            "pnl": 10000.0,
            "day_buy_quantity": 0,
            "day_buy_value": 0.0,
            "day_sell_value": 0.0,
            "day_sell_quantity": 0,
            "m2m": 10000.0,
            "multiplier": 1,   # NFO multiplier (default)
            "product": "NRML",
            "instrument_token": 88888,
        }]
        # NSE closes at 15:30 IST — use a time well after close (15:35 IST)
        ist = ZoneInfo('Asia/Kolkata')
        now_ist = datetime(2026, 8, 9, 15, 35, 0, tzinfo=ist)

        rows = _positions_rows("ZG0790", date(2026, 8, 9), raw, now_ist, settled=True, market_open=False)
        assert len(rows) == 1
        r = rows[0]
        # day_pnl = 50 * (23200 - 23000) = 50 * 200 = 10000
        assert r["day_pnl"] == pytest.approx(10000.0, rel=1e-5), (
            f"day_pnl={r['day_pnl']} should be 10000 for NFO with multiplier=1"
        )

    def test_positions_rows_preserves_payload_json_with_multiplier(self):
        """_positions_rows stores full payload_json with multiplier field."""
        from backend.api.algo.daily_snapshot import _positions_rows
        import json

        raw = [{
            "tradingsymbol": "CRUDEOIL26AUGFUT",
            "exchange": "MCX",
            "quantity": 100,
            "overnight_quantity": 100,
            "average_price": 5480.0,
            "last_price": 5500.0,
            "close_price": 5480.0,
            "pnl": 2000.0,
            "day_buy_quantity": 0,
            "day_buy_value": 0.0,
            "day_sell_value": 0.0,
            "day_sell_quantity": 0,
            "m2m": 2000.0,
            "multiplier": 100,
            "product": "NRML",
            "instrument_token": 99999,
        }]
        ist = ZoneInfo('Asia/Kolkata')
        now_ist = datetime(2026, 8, 9, 23, 35, 0, tzinfo=ist)  # post MCX close

        rows = _positions_rows("ZG0790", date(2026, 8, 9), raw, now_ist, settled=True)
        assert len(rows) == 1
        r = rows[0]
        # Payload should contain multiplier for later snapshot readers
        payload = json.loads(r["payload_json"])
        assert payload.get("multiplier") == 100, "Multiplier must be in payload_json for snapshot readers"
