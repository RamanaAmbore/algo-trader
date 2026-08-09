"""
Test holdings snapshot day_pnl recomputation with prev_ltp

Verifies that _build_holding_row_from_snapshot uses (ltp - prev_ltp) * qty
when prev_ltp is provided and > 0, overriding previous_close-based formula and
the stored day_pnl from daily_book (which may be stale).

Column order for 11-element raw_row (Change C2):
  account, symbol, exchange, qty, avg_cost, ltp, previous_close,
  day_pnl, total_pnl, captured_at, prev_ltp
"""

import pytest
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")


def _make_row(
    ltp,
    previous_close=None,
    prev_ltp=None,
    qty=10,
    avg_cost="1600.00",
    day_pnl="100.00",
    total_pnl="500.00",
    account="ZG0790",
    symbol="HDFCBANK",
    exchange="NSE",
):
    """Helper: build an 11-element raw_row tuple for _build_holding_row_from_snapshot."""
    captured_at = datetime(2026, 8, 9, 16, 0, 0, tzinfo=_IST)
    return (
        account, symbol, exchange,
        qty, Decimal(avg_cost), Decimal(str(ltp)),
        Decimal(str(previous_close)) if previous_close is not None else None,
        Decimal(day_pnl), Decimal(total_pnl),
        captured_at,
        Decimal(str(prev_ltp)) if prev_ltp is not None else None,
    )


class TestBuildHoldingRowFromSnapshotPrevLtp:
    """Test that _build_holding_row_from_snapshot recomputes day_pnl correctly."""

    # -----------------------------------------------------------------------
    # Priority 1: prev_ltp from prev_batch CTE (most-recent prior snapshot)
    # -----------------------------------------------------------------------

    def test_prev_ltp_takes_first_priority_over_previous_close(self):
        """(ltp - prev_ltp) * qty is used when prev_ltp > 0, overriding previous_close.

        ltp=1650, prev_ltp=1630, previous_close=1620, qty=10
        -> day_change_val = (1650 - 1630) * 10 = 200  (NOT 1650-1620=300)
        """
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = _make_row(ltp=1650, previous_close=1620, prev_ltp=1630, qty=10)
        row, inv_val, cur_val, total_pnl_f, day_change_f = _build_holding_row_from_snapshot(raw_row)

        assert row.day_change_val == pytest.approx(200.0, rel=1e-5), (
            f"day_change_val={row.day_change_val} must be (ltp-prev_ltp)*qty = "
            f"(1650-1630)*10 = 200.0; NOT previous_close-based 300.0"
        )

    def test_prev_ltp_beats_stored_day_pnl(self):
        """prev_ltp computation overrides the stale stored day_pnl from DB."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        # Stored day_pnl=999, but correct value = (1650-1640)*10 = 100
        raw_row = _make_row(ltp=1650, previous_close=1640, prev_ltp=1640, qty=10, day_pnl="999.00")
        row, *_ = _build_holding_row_from_snapshot(raw_row)

        assert row.day_change_val == pytest.approx(100.0, rel=1e-5), (
            "prev_ltp path: (1650-1640)*10=100 must override stored day_pnl=999"
        )

    # -----------------------------------------------------------------------
    # Priority 2: previous_close fallback when prev_ltp is None or zero
    # -----------------------------------------------------------------------

    def test_previous_close_fallback_when_prev_ltp_none(self):
        """When prev_ltp is None, fallback to (ltp - previous_close) * qty."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        # prev_ltp=None -> use previous_close: (1650-1620)*10 = 300
        raw_row = _make_row(ltp=1650, previous_close=1620, prev_ltp=None, qty=10, day_pnl="100.00")
        row, *_ = _build_holding_row_from_snapshot(raw_row)

        assert row.day_change_val == pytest.approx(300.0, rel=1e-5), (
            "prev_ltp=None -> fallback to (ltp-previous_close)*qty = (1650-1620)*10 = 300"
        )

    def test_previous_close_fallback_when_prev_ltp_zero(self):
        """When prev_ltp == 0, treat as absent and fallback to previous_close."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        # prev_ltp=0 (no prior batch in DB) -> use previous_close: (1650-1620)*10 = 300
        raw_row = _make_row(ltp=1650, previous_close=1620, prev_ltp=0, qty=10, day_pnl="100.00")
        row, *_ = _build_holding_row_from_snapshot(raw_row)

        assert row.day_change_val == pytest.approx(300.0, rel=1e-5), (
            "prev_ltp=0 treated as absent -> fallback to (ltp-previous_close)*qty = 300"
        )

    # -----------------------------------------------------------------------
    # Priority 3: stored day_pnl when both prev_ltp and previous_close absent
    # -----------------------------------------------------------------------

    def test_stored_day_pnl_fallback_when_both_prior_prices_absent(self):
        """When prev_ltp=None and previous_close=0, use stored day_pnl."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = _make_row(
            ltp=110, previous_close=0, prev_ltp=None, qty=5,
            avg_cost="100.00", day_pnl="50.00", total_pnl="50.00",
            symbol="NEWSTOCK",
        )
        row, *_ = _build_holding_row_from_snapshot(raw_row)

        assert row.day_change_val == pytest.approx(50.0, rel=1e-5), (
            "Both prev_ltp and previous_close absent -> use stored day_pnl=50"
        )

    # -----------------------------------------------------------------------
    # Percentage and other derived fields
    # -----------------------------------------------------------------------

    def test_day_change_percentage_computed_from_previous_close_denominator(self):
        """day_change_percentage denominator is previous_close * qty (when prev_close > 0)."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        # prev_ltp == previous_close -> day_change_val = (550-530)*100 = 2000
        raw_row = _make_row(
            ltp=550, previous_close=530, prev_ltp=530,
            qty=100, avg_cost="500.00", day_pnl="2000.00", total_pnl="5000.00",
            symbol="SBIN",
        )
        row, *_ = _build_holding_row_from_snapshot(raw_row)

        assert row.day_change_val == pytest.approx(2000.0, rel=1e-5)
        # day_change_percentage = 2000 / (530 * 100) * 100 approx 3.77%
        expected_pct = (2000.0 / (530.0 * 100)) * 100.0
        assert row.day_change_percentage == pytest.approx(expected_pct, rel=1e-4), (
            f"day_change_percentage={row.day_change_percentage} should be {expected_pct}%"
        )

    def test_multiple_holdings_each_recomputed_independently(self):
        """Each row is independently recomputed from its own prev_ltp."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        row1_raw = _make_row(
            ltp=1650, previous_close=1600, prev_ltp=1600,
            qty=10, avg_cost="1600.00", symbol="HDFCBANK",
        )
        row2_raw = _make_row(
            ltp=2600, previous_close=2550, prev_ltp=2550,
            qty=5, avg_cost="2500.00", symbol="INFY",
        )

        row1, *_ = _build_holding_row_from_snapshot(row1_raw)
        row2, *_ = _build_holding_row_from_snapshot(row2_raw)

        # HDFCBANK: (1650 - 1600) * 10 = 500
        assert row1.day_change_val == pytest.approx(500.0, rel=1e-5)
        # INFY: (2600 - 2550) * 5 = 250
        assert row2.day_change_val == pytest.approx(250.0, rel=1e-5)

    def test_zero_qty_produces_zero_day_pnl(self):
        """Zero quantity produces zero day_pnl regardless of price movement."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = _make_row(
            ltp=110, previous_close=100, prev_ltp=100,
            qty=0, avg_cost="100.00", day_pnl="0.00", total_pnl="0.00",
            symbol="CLOSEDSTOCK",
        )
        row, *_ = _build_holding_row_from_snapshot(raw_row)

        assert row.day_change_val == pytest.approx(0.0, rel=1e-5)
        assert row.quantity == 0

    def test_negative_qty_short_position(self):
        """Short holdings (negative qty) sign-correct day_change_val."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        # ltp=1550 (below close 1600) on short -10 -> profit
        raw_row = _make_row(
            ltp=1550, previous_close=1600, prev_ltp=1600,
            qty=-10, avg_cost="1600.00", day_pnl="500.00", total_pnl="500.00",
            symbol="SHORTSTOCK",
        )
        row, *_ = _build_holding_row_from_snapshot(raw_row)

        # (1550 - 1600) * -10 = (-50) * -10 = 500
        expected_dcv = (1550.0 - 1600.0) * (-10)
        assert row.day_change_val == pytest.approx(expected_dcv, rel=1e-5), (
            f"Short position: day_change_val={row.day_change_val}, expected {expected_dcv}"
        )

    def test_preserves_total_pnl_unchanged(self):
        """total_pnl is broker-owned -- must not be recomputed."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = _make_row(
            ltp=1650, previous_close=1600, prev_ltp=1600,
            qty=10, avg_cost="1500.00", day_pnl="500.00", total_pnl="1500.00",
        )
        row, *_ = _build_holding_row_from_snapshot(raw_row)

        assert row.pnl == pytest.approx(1500.0, rel=1e-5), (
            "total_pnl must remain unchanged (broker-owned)"
        )
        # pnl_percentage = 1500 / (1500 * 10) * 100 = 10%
        assert row.pnl_percentage == pytest.approx(10.0, rel=1e-4)

    def test_close_price_set_from_previous_close(self):
        """close_price field is set to previous_close (frozen first-write)."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = _make_row(
            ltp=1650, previous_close=1600, prev_ltp=1600,
            qty=10, avg_cost="1600.00",
        )
        row, *_ = _build_holding_row_from_snapshot(raw_row)

        assert row.close_price == pytest.approx(1600.0, rel=1e-5), (
            "close_price must be set to previous_close (frozen settlement)"
        )

    def test_11_column_tuple_required(self):
        """_build_holding_row_from_snapshot must accept 11-column tuple (with prev_ltp)."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        captured_at = datetime(2026, 8, 9, 16, 0, 0, tzinfo=_IST)
        # 11 elements -- the new schema after adding prev_ltp (Change C2)
        raw_row = (
            "ZG0790", "RELIANCE", "NSE",
            10, Decimal("2500.00"), Decimal("2600.00"),
            Decimal("2550.00"), Decimal("500.00"), Decimal("1000.00"),
            captured_at,
            Decimal("2550.00"),  # prev_ltp -- 11th column
        )
        row, inv_val, cur_val, total_pnl_f, day_change_f = _build_holding_row_from_snapshot(raw_row)

        assert row.tradingsymbol == "RELIANCE"
        assert row.quantity == 10
        # day_change_val = (2600-2550)*10 = 500 (prev_ltp path)
        assert row.day_change_val == pytest.approx(500.0, rel=1e-5)
