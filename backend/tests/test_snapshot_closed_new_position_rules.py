"""
Tests for two special-case rules in _positions_rows():

  Rule 1 — New position (overnight_quantity == 0, qty > 0):
    previous_close = avg_cost (entry price).
    There is no prior settlement for a brand-new position; using entry price
    ensures day P&L = (ltp - avg_cost) × qty from the first snapshot.

  Rule 2 — Closed position (qty == 0):
    ltp anchored to exit price (day trade VWAP) so subsequent intraday
    rescans on the same date don't overwrite it with a live market price.
    UPSERT SQL: when daily_book.qty = 0 AND daily_book.ltp IS NOT NULL,
    keep existing ltp (freeze).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TODAY = date(2026, 8, 23)
_NOW   = datetime(2026, 8, 23, 16, 20, 0, tzinfo=timezone.utc)

_BASE_POS = {
    "tradingsymbol":    "NIFTY26AUGFUT",
    "exchange":         "NFO",
    "average_price":    24500.0,
    "last_price":       24600.0,
    "close_price":      24400.0,
    "pnl":              5000.0,
    "day_buy_quantity":  0,
    "day_buy_value":     0.0,
    "day_sell_quantity": 0,
    "day_sell_value":    0.0,
    "day_change_val":    0.0,
    "multiplier":        None,
}


def _call_positions_rows(raw: list[dict], prev_ltp_map=None, market_open=False):
    """Call _positions_rows with market closed (EOD mode) and no broker patches."""
    from backend.api.algo.daily_snapshot import _positions_rows

    with patch("backend.brokers.adapters.kite._LOT_INDEX", {}):
        return _positions_rows(
            account="ZG0790",
            target_date=_TODAY,
            raw=raw,
            now_ist=_NOW,
            settled=True,
            market_open=market_open,
            prev_ltp_map=prev_ltp_map,
        )


# ---------------------------------------------------------------------------
# Rule 1: New position — previous_close = avg_cost
# ---------------------------------------------------------------------------

def test_new_position_previous_close_equals_avg_cost():
    """overnight_quantity=0, qty>0 → previous_close must equal avg_cost."""
    pos = {**_BASE_POS, "quantity": 50, "overnight_quantity": 0}
    rows = _call_positions_rows([pos])

    assert rows, "expected one row"
    row = rows[0]
    assert row["previous_close"] == 24500.0, (
        f"New position: previous_close should equal avg_cost=24500, got {row['previous_close']}"
    )


def test_new_position_ignores_broker_close_price():
    """New position: broker close_price (stale BHAV) must NOT be used as previous_close."""
    pos = {**_BASE_POS, "quantity": 50, "overnight_quantity": 0,
           "close_price": 24300.0}  # stale BHAV price
    rows = _call_positions_rows([pos])

    row = rows[0]
    # Must use avg_cost (24500), not broker close_price (24300)
    assert row["previous_close"] == 24500.0


def test_overnight_position_uses_close_ref_not_avg_cost():
    """overnight_quantity>0 → previous_close must NOT use avg_cost."""
    pos = {**_BASE_POS, "quantity": 50, "overnight_quantity": 50}
    prev_map = {("ZG0790", "NIFTY26AUGFUT", "positions"): 24350.0}
    rows = _call_positions_rows([pos], prev_ltp_map=prev_map)

    row = rows[0]
    # SSOT: daily_book.ltp from prior session, not avg_cost
    assert row["previous_close"] == 24350.0, (
        f"Overnight position: previous_close should be daily_book.ltp=24350, got {row['previous_close']}"
    )


def test_overnight_position_falls_back_to_broker_close_when_no_prev_ltp():
    """No prev_ltp_map entry → fall back to broker close_price for overnight positions."""
    pos = {**_BASE_POS, "quantity": 50, "overnight_quantity": 50,
           "close_price": 24400.0}
    rows = _call_positions_rows([pos], prev_ltp_map={})

    row = rows[0]
    assert row["previous_close"] == 24400.0


# ---------------------------------------------------------------------------
# Rule 2: Closed position — ltp anchored to exit price
# ---------------------------------------------------------------------------

def test_closed_long_ltp_set_to_sell_vwap():
    """qty=0, oq>=0: ltp = day_sell_value / day_sell_quantity (exit VWAP)."""
    pos = {
        **_BASE_POS,
        "quantity":           0,
        "overnight_quantity": 50,
        "last_price":         24700.0,    # live market price (should NOT be used)
        "day_sell_quantity":  50,
        "day_sell_value":     50 * 24650.0,  # sold at 24650
        "pnl":                12500.0,
    }
    rows = _call_positions_rows([pos])

    assert rows, "closed position row must still be written"
    row = rows[0]
    assert abs(row["ltp"] - 24650.0) < 0.01, (
        f"Closed long: ltp should be exit VWAP=24650, got {row['ltp']}"
    )


def test_closed_short_ltp_set_to_buy_vwap():
    """qty=0, oq<0 (short): ltp = day_buy_value / day_buy_quantity."""
    pos = {
        **_BASE_POS,
        "quantity":           0,
        "overnight_quantity": -50,
        "average_price":      24500.0,
        "last_price":         24200.0,    # live market (should NOT be used)
        "day_buy_quantity":   50,
        "day_buy_value":      50 * 24300.0,  # covered at 24300
        "pnl":                10000.0,
    }
    rows = _call_positions_rows([pos])

    row = rows[0]
    assert abs(row["ltp"] - 24300.0) < 0.01, (
        f"Closed short: ltp should be cover VWAP=24300, got {row['ltp']}"
    )


def test_closed_position_no_trade_data_falls_back_to_last_price():
    """qty=0 but day_sell_quantity=0: ltp falls back to last_price."""
    pos = {
        **_BASE_POS,
        "quantity":           0,
        "overnight_quantity": 50,
        "last_price":         24700.0,
        "day_sell_quantity":  0,
        "day_sell_value":     0.0,
        "pnl":                10000.0,
    }
    rows = _call_positions_rows([pos])

    row = rows[0]
    assert row["ltp"] == 24700.0, (
        f"No trade data: should fall back to last_price=24700, got {row['ltp']}"
    )


def test_mcx_closed_position_exit_price_divides_by_lot_size():
    """MCX (multiplier=100): exit price = sell_value / (sell_qty_lots × 100)."""
    pos = {
        **_BASE_POS,
        "tradingsymbol":      "CRUDEOIL26AUGFUT",
        "exchange":           "MCX",
        "multiplier":         100,
        "quantity":           0,
        "overnight_quantity": 2,          # 2 lots
        "last_price":         5200.0,     # per barrel live price
        "day_sell_quantity":  2,          # 2 lots
        "day_sell_value":     2 * 100 * 5150.0,  # total ₹ value
        "pnl":                -1000.0,
    }
    rows = _call_positions_rows([pos])

    row = rows[0]
    assert abs(row["ltp"] - 5150.0) < 0.01, (
        f"MCX closed: ltp should be 5150 per barrel, got {row['ltp']}"
    )


# ---------------------------------------------------------------------------
# UPSERT SQL freeze guard
# ---------------------------------------------------------------------------

def test_upsert_sql_contains_qty_zero_freeze():
    """UPSERT SQL must freeze ltp when daily_book.qty = 0 AND ltp IS NOT NULL."""
    import inspect
    import backend.api.algo.daily_snapshot as ds

    src = inspect.getsource(ds)
    assert "daily_book.qty = 0" in src, (
        "UPSERT must check daily_book.qty = 0 to freeze ltp for closed positions"
    )
    assert "daily_book.ltp IS NOT NULL" in src, (
        "UPSERT qty=0 freeze must also guard on ltp IS NOT NULL"
    )
