"""
Unit tests for the holdings.py _fetch helper decomposition.

Covers the four pure helpers extracted from _fetch during the
cyclomatic-complexity refactor (Jul 2026):
  _is_full_outage
  _stale_since_map
  _compute_summary_df
  _apply_stale_since_map

No HTTP-layer or DB fixtures required.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import polars as pl
import pytest

from backend.api.routes.holdings import (
    _apply_stale_since_map,
    _compute_summary_df,
    _is_full_outage,
    _prepare_raw_frame,
    _stale_since_map,
)


# ---------------------------------------------------------------------------
# _is_full_outage
# ---------------------------------------------------------------------------

def _make_df(rows: dict | None = None, **attrs):
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    for k, v in attrs.items():
        df.attrs[k] = v
    return df


def test_is_full_outage_empty_list_is_not_outage():
    assert _is_full_outage([]) is False


def test_is_full_outage_all_failed_is_outage():
    dfs = [_make_df(fetch_failed=True), _make_df(fetch_failed=True)]
    assert _is_full_outage(dfs) is True


def test_is_full_outage_any_success_is_not_outage():
    dfs = [_make_df(fetch_failed=True), _make_df(fetch_failed=False)]
    assert _is_full_outage(dfs) is False


def test_is_full_outage_no_flag_treated_as_success():
    """Absence of fetch_failed attr == success (default False)."""
    dfs = [_make_df(), _make_df()]
    assert _is_full_outage(dfs) is False


# ---------------------------------------------------------------------------
# _stale_since_map
# ---------------------------------------------------------------------------

def test_stale_since_map_empty_list_returns_empty():
    assert _stale_since_map([]) == {}


def test_stale_since_map_missing_stale_since_skipped():
    df = _make_df({"account": ["ZG0790"], "symbol": ["INFY"]})
    assert _stale_since_map([df]) == {}


def test_stale_since_map_empty_df_skipped():
    df = _make_df(stale_since=1234567890)
    assert _stale_since_map([df]) == {}


def test_stale_since_map_no_account_col_skipped():
    df = _make_df({"symbol": ["INFY"]}, stale_since=1234567890)
    assert _stale_since_map([df]) == {}


def test_stale_since_map_formats_ist_time():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    # Build any known IST time programmatically to avoid manual epoch math
    target = datetime(2026, 7, 4, 9, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
    ts = target.timestamp()
    df = _make_df(
        {"account": ["ZG0790"], "symbol": ["INFY"]},
        stale_since=ts,
    )
    out = _stale_since_map([df])
    assert out == {"ZG0790": "09:15 IST"}


def test_stale_since_map_formats_matches_reference():
    """Match the docstring: HH:MM IST format from a raw epoch."""
    df = _make_df(
        {"account": ["ZG0790"], "symbol": ["INFY"]},
        stale_since=1783137300,
    )
    out = _stale_since_map([df])
    val = out["ZG0790"]
    assert val.endswith("IST")
    # Format is HH:MM IST
    hh, rest = val.split(":", 1)
    mm, _ = rest.split(" ", 1)
    assert 0 <= int(hh) <= 23
    assert 0 <= int(mm) <= 59


def test_stale_since_map_bad_timestamp_swallowed():
    df = _make_df(
        {"account": ["ZG0790"], "symbol": ["INFY"]},
        stale_since="not-a-number",
    )
    # Should not raise — the try/except in the helper eats the error
    assert _stale_since_map([df]) == {}


def test_stale_since_map_multiple_accounts():
    df1 = _make_df({"account": ["ZG0790"], "symbol": ["INFY"]}, stale_since=1783137300)
    df2 = _make_df({"account": ["DH6847"], "symbol": ["TATA"]}, stale_since=1783140900)
    out = _stale_since_map([df1, df2])
    assert set(out) == {"ZG0790", "DH6847"}


# ---------------------------------------------------------------------------
# _compute_summary_df
# ---------------------------------------------------------------------------

def test_compute_summary_df_shapes_grouped_plus_total():
    df = pl.DataFrame({
        "account": ["A", "A", "B"],
        "inv_val": [100.0, 200.0, 300.0],
        "cur_val": [110.0, 210.0, 330.0],
        "pnl":     [10.0, 10.0, 30.0],
        "day_change_val": [1.0, 2.0, 3.0],
    })
    out = _compute_summary_df(df)
    accts = set(out["account"].to_list())
    assert accts == {"A", "B", "TOTAL"}
    # TOTAL row aggregates all sums
    total = out.filter(pl.col("account") == "TOTAL").row(0, named=True)
    assert total["inv_val"] == pytest.approx(600.0)
    assert total["cur_val"] == pytest.approx(650.0)
    assert total["pnl"]     == pytest.approx(50.0)


def test_compute_summary_df_pct_uses_yesterday_denominator():
    """day_change_percentage = day_change_val / (cur_val - day_change_val) × 100."""
    df = pl.DataFrame({
        "account": ["A"],
        "inv_val": [100.0],
        "cur_val": [110.0],   # +10 today
        "pnl":     [10.0],
        "day_change_val": [10.0],
    })
    out = _compute_summary_df(df)
    a = out.filter(pl.col("account") == "A").row(0, named=True)
    # yesterday_val = 110 - 10 = 100, so pct = 10/100 = 10%
    assert a["day_change_percentage"] == pytest.approx(10.0)
    # pnl_pct = 10 / 100 * 100 = 10
    assert a["pnl_percentage"] == pytest.approx(10.0)


def test_compute_summary_df_single_account_still_appends_total():
    df = pl.DataFrame({
        "account": ["A"],
        "inv_val": [100.0], "cur_val": [110.0], "pnl": [10.0], "day_change_val": [1.0],
    })
    out = _compute_summary_df(df)
    accts = set(out["account"].to_list())
    assert accts == {"A", "TOTAL"}


# ---------------------------------------------------------------------------
# _apply_stale_since_map
# ---------------------------------------------------------------------------

class _MockRow:
    """Minimal msgspec-compatible row stand-in for helpers that use structs.replace.

    msgspec.structs.replace works on real Struct instances only, so we import
    a real HoldingRow from schemas for these tests.
    """


def _mock_holding(account: str, account_stale: bool):
    from backend.api.schemas import HoldingRow
    # HoldingRow has many required fields; provide sane defaults
    return HoldingRow(
        account=account, tradingsymbol="X", exchange="NSE",
        quantity=1, opening_quantity=1,
        average_price=100.0, close_price=100.0, last_price=100.0,
        inv_val=100.0, cur_val=100.0, pnl=0.0, pnl_percentage=0.0,
        day_change_val=0.0, day_change_percentage=0.0,
        last_price_stale=False, account_stale=account_stale,
    )


def test_apply_stale_since_map_empty_map_returns_input():
    rows = [_mock_holding("ZG0790", True)]
    out = _apply_stale_since_map(rows, {})
    assert out is rows  # identity — no rewrite


def test_apply_stale_since_map_only_stale_rows_get_stamped():
    rows = [
        _mock_holding("ZG0790", True),
        _mock_holding("DH6847", False),
    ]
    stale = {"ZG0790": "09:15 IST"}
    out = _apply_stale_since_map(rows, stale)
    assert out[0].account_stale_since == "09:15 IST"
    # Non-stale row untouched
    assert getattr(out[1], "account_stale_since", None) in (None, "")


def test_apply_stale_since_map_missing_account_in_map_untouched():
    rows = [_mock_holding("ZG0790", True)]
    out = _apply_stale_since_map(rows, {"OTHER": "09:00 IST"})
    # Should not be replaced (account not in map)
    assert getattr(out[0], "account_stale_since", None) in (None, "")


# ---------------------------------------------------------------------------
# _prepare_raw_frame
# ---------------------------------------------------------------------------

def test_prepare_raw_frame_empty_returns_empty():
    df = _prepare_raw_frame([])
    assert df.empty


def test_prepare_raw_frame_all_empty_dfs_returns_empty():
    df = _prepare_raw_frame([pd.DataFrame(), pd.DataFrame()])
    assert df.empty


def test_prepare_raw_frame_calls_backfill_and_override():
    """Non-empty frames trigger backfill_market_data + LTP-override."""
    raw = pd.DataFrame({
        "account": ["ZG0790"], "tradingsymbol": ["INFY"], "exchange": ["NSE"],
        "quantity": [10], "opening_quantity": [10],
        "average_price": [100.0], "close_price": [100.0], "last_price": [110.0],
    })
    with patch("backend.api.routes.holdings.broker_apis.backfill_market_data") as bf, \
         patch("backend.api.routes.holdings._override_stale_ltp_from_ticker") as ov:
        out = _prepare_raw_frame([raw])
    assert bf.called
    assert ov.called
    # Numeric NaN → 0 fill happens
    assert not out.empty
