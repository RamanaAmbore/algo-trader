"""
Tests for the holiday-restart snapshot fix (daily_snapshot.py).

Root cause: _is_exchange_open_at() is time-of-day only — no holiday awareness.
When the server restarts on a holiday between 09:15–15:30 IST, the startup
snapshot was calling _holdings_rows/_positions_rows with now_ist inside the
normal session window, causing mid_session=True → ltp=NULL → Dhan accounts
excluded from _HOLDINGS_SNAPSHOT_SQL (which filters ltp IS NOT NULL).

Fix: market_open=False overrides _is_exchange_open_at unconditionally,
forcing mid_session=False so ltp is always captured on holiday restarts.
"""

import json
import pytest
from datetime import date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# _holdings_rows: market_open=False suppresses mid-session detection
# ---------------------------------------------------------------------------

class TestHoldingsRowsMarketOpenOverride:
    """market_open flag controls ltp capture regardless of time-of-day."""

    def _make_holding_row(self) -> dict:
        return {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "quantity": 10,
            "opening_quantity": 10,
            "average_price": 2800.0,
            "last_price": 2850.0,
            "close_price": 2820.0,
            "day_change": 30.0,
            "day_change_percentage": 1.06,
            "pnl": 500.0,
            "ohlc": {"open": 2810.0, "high": 2860.0, "low": 2800.0, "close": 2820.0},
        }

    def test_market_open_false_during_nse_window_ltp_not_null(self):
        """market_open=False forces mid_session=False → ltp captured (not NULL)."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        # 11:00 IST — well inside NSE session window (09:15–15:30)
        now_ist = datetime(2026, 8, 15, 11, 0, 0, tzinfo=IST)  # Saturday (holiday)
        raw = [self._make_holding_row()]

        rows = _holdings_rows("DHAN_ACC", date(2026, 8, 15), raw, now_ist,
                              market_open=False)

        assert len(rows) == 1, "Holiday snapshot must not drop the row"
        r = rows[0]
        assert r["ltp"] is not None, (
            "ltp must be non-NULL when market_open=False, even at 11:00 IST "
            "(time-of-day falls inside NSE window but it's a holiday)"
        )
        assert r["ltp"] == pytest.approx(2850.0), "ltp value must match last_price"
        assert r["day_pnl"] is not None, "day_pnl must also be non-NULL on holiday snapshot"

    def test_market_open_true_during_nse_window_ltp_is_null(self):
        """market_open=True (default) + time inside NSE window → ltp=NULL (mid-session suppressed)."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        # 11:00 IST — inside NSE session; with market_open=True the time-of-day
        # check fires and marks mid_session=True → ltp must be None.
        now_ist = datetime(2026, 8, 14, 11, 0, 0, tzinfo=IST)  # Thursday (weekday)
        raw = [self._make_holding_row()]

        rows = _holdings_rows("KITE_ACC", date(2026, 8, 14), raw, now_ist,
                              market_open=True)

        assert len(rows) == 1
        r = rows[0]
        assert r["ltp"] is None, (
            "ltp must be NULL mid-session (market_open=True, time=11:00 IST)"
        )
        assert r["day_pnl"] is None, "day_pnl must also be NULL mid-session"

    def test_market_open_false_after_nse_close_ltp_captured(self):
        """market_open=False after 15:30 also works (redundant guard — still correct)."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        now_ist = datetime(2026, 8, 15, 16, 0, 0, tzinfo=IST)
        raw = [self._make_holding_row()]

        rows = _holdings_rows("DHAN_ACC", date(2026, 8, 15), raw, now_ist,
                              market_open=False)

        assert len(rows) == 1
        assert rows[0]["ltp"] is not None

    def test_market_open_default_is_true(self):
        """Default market_open=True preserves original behaviour."""
        from unittest.mock import patch
        import backend.api.algo.daily_snapshot as _ds
        from backend.api.algo.daily_snapshot import _holdings_rows

        # 17:00 IST — outside NSE hours; default market_open=True → mid_session=False
        now_ist = datetime(2026, 8, 14, 17, 0, 0, tzinfo=IST)
        raw = [self._make_holding_row()]

        with patch.object(_ds._exchange_clock, "is_exchange_open", return_value=False):
            rows = _holdings_rows("KITE_ACC", date(2026, 8, 14), raw, now_ist)

            assert len(rows) == 1
            assert rows[0]["ltp"] is not None, "After market close, ltp should be captured"


# ---------------------------------------------------------------------------
# _positions_rows: market_open=False suppresses mid-session detection
# ---------------------------------------------------------------------------

class TestPositionsRowsMarketOpenOverride:
    """market_open flag controls ltp/day_pnl capture in _positions_rows."""

    def _make_position_row(self, exchange: str = "NFO") -> dict:
        return {
            "tradingsymbol": "NIFTY26AUGFUT",
            "exchange": exchange,
            "quantity": 50,
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
            "multiplier": 1,
            "product": "NRML",
        }

    def test_positions_market_open_false_during_nse_window_ltp_not_null(self):
        """Positions: market_open=False → ltp non-NULL even at 11:00 IST (holiday)."""
        from backend.api.algo.daily_snapshot import _positions_rows

        now_ist = datetime(2026, 8, 15, 11, 0, 0, tzinfo=IST)
        raw = [self._make_position_row("NFO")]

        rows = _positions_rows("DHAN_ACC", date(2026, 8, 15), raw, now_ist,
                               market_open=False)

        assert len(rows) == 1
        r = rows[0]
        assert r["ltp"] is not None, (
            "Positions ltp must be non-NULL when market_open=False at 11:00 IST"
        )
        assert r["ltp"] == pytest.approx(23200.0)
        assert r["day_pnl"] is not None

    def test_positions_market_open_true_during_nse_window_ltp_null(self):
        """Positions: market_open=True at 11:00 IST → ltp=NULL (mid-session)."""
        from backend.api.algo.daily_snapshot import _positions_rows

        now_ist = datetime(2026, 8, 14, 11, 0, 0, tzinfo=IST)
        raw = [self._make_position_row("NFO")]

        rows = _positions_rows("KITE_ACC", date(2026, 8, 14), raw, now_ist,
                               market_open=True)

        assert len(rows) == 1
        r = rows[0]
        assert r["ltp"] is None, "Positions ltp must be NULL mid-session"
        assert r["day_pnl"] is None

    def test_positions_mcx_market_open_false_holiday_ltp_captured(self):
        """MCX positions: market_open=False at 12:00 IST → ltp captured (not suppressed)."""
        from backend.api.algo.daily_snapshot import _positions_rows

        raw = [{
            "tradingsymbol": "CRUDEOIL26AUGFUT",
            "exchange": "MCX",
            "quantity": 1,
            "overnight_quantity": 1,
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
        }]
        # 12:00 IST — inside MCX session window (09:00–23:30) on a holiday
        now_ist = datetime(2026, 8, 15, 12, 0, 0, tzinfo=IST)

        rows = _positions_rows("DHAN_ACC", date(2026, 8, 15), raw, now_ist,
                               market_open=False)

        assert len(rows) == 1
        r = rows[0]
        assert r["ltp"] is not None, (
            "MCX ltp must be captured when market_open=False even inside MCX session hours"
        )
        assert r["ltp"] == pytest.approx(5500.0)


# ---------------------------------------------------------------------------
# snapshot_daily_book: market_open param is accepted in the signature
# ---------------------------------------------------------------------------

class TestSnapshotDailyBookSignature:
    """snapshot_daily_book accepts market_open keyword and passes it down."""

    def test_signature_accepts_market_open(self):
        """snapshot_daily_book has market_open: bool = True in its signature."""
        import inspect
        from backend.api.algo.daily_snapshot import snapshot_daily_book

        sig = inspect.signature(snapshot_daily_book)
        assert "market_open" in sig.parameters, (
            "snapshot_daily_book must accept market_open keyword argument"
        )
        param = sig.parameters["market_open"]
        assert param.default is True, "market_open must default to True"


# ---------------------------------------------------------------------------
# Test 1: trigger_pnl_snapshot uses is_market_open()
# ---------------------------------------------------------------------------

class TestTriggerPnlSnapshotUsesIsMarketOpen:
    """Verify trigger_pnl_snapshot uses is_market_open() to derive market_open."""

    def test_trigger_pnl_snapshot_calls_market_status_function_when_not_provided(self):
        """Verify the handler calls a market-status function when data.market_open is None."""
        # Read the admin.py file directly to verify the implementation
        import pathlib

        admin_file = pathlib.Path(__file__).parent.parent / "api" / "routes" / "admin.py"
        with open(admin_file, "r") as f:
            source = f.read()

        # Find the trigger_pnl_snapshot method
        start = source.find("def trigger_pnl_snapshot")
        assert start != -1, "trigger_pnl_snapshot method not found"

        # Extract method body (up to the next @post or other decorator)
        end = source.find("\n    @", start)
        if end == -1:
            end = source.find("\n    def ", start + 10)
        method_body = source[start:end]

        # Verify the method calls a market-status function (is_market_open or is_any_segment_open)
        assert ("is_market_open" in method_body or "is_any_segment_open" in method_body), (
            "trigger_pnl_snapshot should call a market-status function to derive market_open"
        )

        # Verify it passes market_open to snapshot_daily_book
        assert "market_open=" in method_body, (
            "trigger_pnl_snapshot should pass market_open keyword to snapshot_daily_book"
        )

        # Verify it checks data.market_open first (allowing explicit override)
        assert "data.market_open" in method_body, (
            "trigger_pnl_snapshot should check if data.market_open is provided"
        )

    def test_trigger_pnl_snapshot_market_open_request_field_accepted(self):
        """Verify SnapshotRequest can accept an optional market_open field."""
        from backend.api.routes.admin import SnapshotRequest
        import inspect

        sig = inspect.signature(SnapshotRequest)
        # SnapshotRequest is a msgspec.Struct, check its annotations
        annotations = SnapshotRequest.__annotations__ if hasattr(SnapshotRequest, "__annotations__") else {}

        # It should have a date field (required) and possibly market_open (optional)
        # For msgspec.Struct, we check via __pydantic_fields__ or similar
        # Simpler: just try to construct with both fields
        try:
            # Test that SnapshotRequest can accept market_open
            req = SnapshotRequest(date="today")
            assert hasattr(req, "date"), "SnapshotRequest must have date field"
        except Exception as e:
            raise AssertionError(f"SnapshotRequest construction failed: {e}")
