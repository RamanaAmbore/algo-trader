"""
Snapshot payload extension — OHLC + volume + OI + day-change fields
captured into `daily_book.payload_json.snapshot_extras` at close_settled.

Five quality dimensions:
  • SSOT       — `_extract_snapshot_extras` is the only place that
                 builds the extras block. Row builders route through it.
  • Correctness— every field maps to the right broker key; None-safe
                 (missing keys don't crash).
  • Performance— pure sync dict access; no I/O.
  • Reuse      — same helper used by holdings + positions row builders.
  • UX         — `settled` flag distinguishes the initial close capture
                 from the ~15-min-later settled capture, so operators
                 can grep the payload.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pytest


IST = ZoneInfo("Asia/Kolkata")


def _kite_position_row() -> dict:
    """A realistic Kite /positions row shape — used to validate the
    extras extractor lifts the expected fields."""
    return {
        "tradingsymbol":         "RELIANCE",
        "exchange":              "NSE",
        "product":               "MIS",
        "quantity":              10,
        "overnight_quantity":    5,
        "day_buy_quantity":      5,
        "day_buy_value":         6500.0,
        "day_sell_quantity":     0,
        "day_sell_value":        0.0,
        "average_price":         1298.0,
        "last_price":            1310.5,
        "close_price":           1295.0,
        "pnl":                   125.0,
        "day_change":            15.5,
        "day_change_percentage": 1.19,
        "ohlc": {
            "open":  1298.0,
            "high":  1315.0,
            "low":   1290.0,
            "close": 1310.75,  # broker adjusted close (weighted-avg-last-30-min)
        },
        "volume":                1_234_567,
        "oi":                    None,
    }


def _kite_option_row() -> dict:
    """F&O option row — has OI, no equity fields."""
    return {
        "tradingsymbol": "NIFTY26MAR25000CE",
        "exchange":      "NFO",
        "quantity":      50,
        "average_price": 120.0,
        "last_price":    145.0,
        "close_price":   118.0,
        "pnl":           1250.0,
        "day_change":    27.0,
        "day_change_percentage": 22.88,
        "ohlc": {
            "open":  118.0,
            "high":  150.0,
            "low":   115.0,
            "close": 144.5,
        },
        "volume":  2_500,
        "oi":      15_000,
    }


def test_extras_extracts_all_ohlcv_fields():
    from backend.api.algo.daily_snapshot import _extract_snapshot_extras
    r = _kite_position_row()
    extras = _extract_snapshot_extras(r, ltp_val=1310.5, settled=True)

    assert extras["open"]           == 1298.0
    assert extras["high"]           == 1315.0
    assert extras["low"]            == 1290.0
    assert extras["close_settled"]  == 1310.75
    assert extras["prev_close"]     == 1295.0
    assert extras["volume"]         == 1_234_567
    assert extras["oi"]             is None  # equity — no OI
    assert extras["day_change_val"] == 15.5
    assert extras["day_change_pct"] == 1.19
    assert extras["ltp"]            == 1310.5
    assert extras["settled"]        is True


def test_extras_close_settled_none_when_settled_false():
    """The `close_settled` field should ONLY be populated when the
    caller flags `settled=True`. At the initial `<exch>:close` event
    the broker's adjusted close hasn't landed yet."""
    from backend.api.algo.daily_snapshot import _extract_snapshot_extras
    r = _kite_position_row()
    extras = _extract_snapshot_extras(r, ltp_val=1310.5, settled=False)

    assert extras["close_settled"] is None
    assert extras["settled"]        is False


def test_extras_option_row_includes_oi():
    from backend.api.algo.daily_snapshot import _extract_snapshot_extras
    r = _kite_option_row()
    extras = _extract_snapshot_extras(r, ltp_val=145.0, settled=True)

    assert extras["oi"]             == 15_000
    assert extras["volume"]         == 2_500
    assert extras["close_settled"]  == 144.5
    assert extras["day_change_val"] == 27.0


def test_extras_none_safe_on_missing_fields():
    """A broker row with no OHLC / volume / OI (fetched from a broker
    that doesn't populate them) still returns a valid extras dict with
    Nones."""
    from backend.api.algo.daily_snapshot import _extract_snapshot_extras
    minimal = {
        "tradingsymbol": "FOO",
        "exchange":      "NSE",
        "average_price": 100.0,
        "last_price":    101.0,
        # No ohlc, close_price, day_change, volume, oi.
    }
    extras = _extract_snapshot_extras(minimal, ltp_val=101.0, settled=True)

    assert extras["open"]           is None
    assert extras["high"]           is None
    assert extras["low"]            is None
    assert extras["close_settled"]  is None
    assert extras["prev_close"]     is None
    assert extras["volume"]         is None
    assert extras["oi"]             is None
    assert extras["ltp"]            == 101.0
    assert extras["settled"]        is True


def test_row_payload_with_extras_wraps_broker_row():
    """The `_row_payload_with_extras` wrapper produces a JSON body that
    keeps the raw Kite row fields at the top level AND embeds a nested
    `snapshot_extras` block. Downstream readers can rely on either shape."""
    from backend.api.algo.daily_snapshot import _row_payload_with_extras
    r = _kite_position_row()
    body_str = _row_payload_with_extras(r, ltp_val=1310.5, settled=True)
    body = json.loads(body_str)

    # Top-level fields preserved for legacy readers.
    assert body["tradingsymbol"] == "RELIANCE"
    assert body["ohlc"]["close"] == 1310.75

    # New extras block.
    assert "snapshot_extras" in body
    extras = body["snapshot_extras"]
    assert extras["close_settled"] == 1310.75
    assert extras["settled"]       is True


def test_positions_row_builder_uses_extras():
    """`_positions_rows` writes payload_json with the extras block."""
    from backend.api.algo.daily_snapshot import _positions_rows

    now_ist = datetime(2026, 3, 16, 15, 45, tzinfo=IST)  # after NSE close
    rows = _positions_rows(
        account="ZG0790",
        target_date=date(2026, 3, 16),
        raw=[_kite_position_row()],
        now_ist=now_ist,
        settled=True,
    )
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["snapshot_extras"]["close_settled"] == 1310.75
    assert payload["snapshot_extras"]["settled"]       is True


def test_holdings_row_builder_uses_extras():
    """`_holdings_rows` writes payload_json with the extras block."""
    from backend.api.algo.daily_snapshot import _holdings_rows

    holding = {
        "tradingsymbol":         "GOLDBEES",
        "exchange":              "NSE",
        "quantity":              100,
        "opening_quantity":      100,
        "average_price":         50.0,
        "last_price":            55.5,
        "close_price":           54.75,
        "pnl":                   555.0,
        "day_change":            0.75,
        "day_change_percentage": 1.37,
        "ohlc": {
            "open":  54.5, "high": 56.0, "low": 54.0, "close": 55.4,
        },
        "volume":                50_000,
    }
    now_ist = datetime(2026, 3, 16, 15, 45, tzinfo=IST)
    rows = _holdings_rows(
        account="ZG0790",
        target_date=date(2026, 3, 16),
        raw=[holding],
        now_ist=now_ist,
        settled=True,
    )
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["snapshot_extras"]["close_settled"] == 55.4
    assert payload["snapshot_extras"]["volume"]        == 50_000
    assert payload["snapshot_extras"]["settled"]       is True


def test_settled_false_at_close_event():
    """The initial `<exch>:close` fires with settled=False so the extras
    block reports `settled=False` and `close_settled=None`."""
    from backend.api.algo.daily_snapshot import _positions_rows

    now_ist = datetime(2026, 3, 16, 15, 32, tzinfo=IST)  # just after close
    rows = _positions_rows(
        account="ZG0790",
        target_date=date(2026, 3, 16),
        raw=[_kite_position_row()],
        now_ist=now_ist,
        settled=False,
    )
    payload = json.loads(rows[0]["payload_json"])
    assert payload["snapshot_extras"]["settled"]        is False
    assert payload["snapshot_extras"]["close_settled"]  is None
