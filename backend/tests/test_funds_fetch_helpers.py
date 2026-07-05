"""
Unit tests for the funds.py _fetch helper decomposition.

Covers the seven pure helpers extracted from _fetch during the
cyclomatic-complexity refactor (Jul 2026):
  _is_broker_outage
  _stale_since_map
  _rename_broker_cols
  _append_total_row
  _add_derived_columns
  _stale_flag_map
  _hydrate_row
"""

import pandas as pd
import polars as pl
import pytest

from backend.api.routes.funds import (
    _add_derived_columns,
    _append_total_row,
    _hydrate_row,
    _is_broker_outage,
    _rename_broker_cols,
    _stale_flag_map,
    _stale_since_map,
)


# ---------------------------------------------------------------------------
# _is_broker_outage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("msg", [
    "502 Bad Gateway", "503 Service Unavailable", "504 Gateway Timeout",
    "Kite responded: Bad Gateway", "kite gateway timeout retry",
])
def test_is_broker_outage_true(msg):
    assert _is_broker_outage(Exception(msg)) is True


@pytest.mark.parametrize("msg", [
    "connection refused", "generic error", "no data",
])
def test_is_broker_outage_false(msg):
    assert _is_broker_outage(Exception(msg)) is False


# ---------------------------------------------------------------------------
# _stale_since_map
# ---------------------------------------------------------------------------

def _mkdf(rows=None, **attrs):
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    for k, v in attrs.items():
        df.attrs[k] = v
    return df


def test_stale_since_map_empty_list():
    assert _stale_since_map([]) == {}


def test_stale_since_map_no_attr_ignored():
    df = _mkdf({"account": ["ZG0790"], "cash": [1000.0]})
    assert _stale_since_map([df]) == {}


def test_stale_since_map_empty_df_ignored():
    df = _mkdf(stale_since=1783137300)
    assert _stale_since_map([df]) == {}


def test_stale_since_map_missing_account_col_ignored():
    df = _mkdf({"cash": [100.0]}, stale_since=1783137300)
    assert _stale_since_map([df]) == {}


def test_stale_since_map_formats_hh_mm_ist():
    df = _mkdf({"account": ["ZG0790"], "cash": [1000.0]}, stale_since=1783137300)
    val = _stale_since_map([df])["ZG0790"]
    assert val.endswith("IST")
    hh, rest = val.split(":", 1)
    mm, _ = rest.split(" ", 1)
    assert 0 <= int(hh) <= 23
    assert 0 <= int(mm) <= 59


def test_stale_since_map_bad_timestamp_swallowed():
    df = _mkdf({"account": ["ZG0790"]}, stale_since="not-a-number")
    assert _stale_since_map([df]) == {}


# ---------------------------------------------------------------------------
# _rename_broker_cols
# ---------------------------------------------------------------------------

def test_rename_broker_cols_all_present():
    df = pl.DataFrame({
        "avail opening_balance": [100.0],
        "avail cash":            [50.0],
        "net":                   [200.0],
        "util debits":           [30.0],
        "util option_premium":   [5.0],
        "avail collateral":      [10.0],
        "account":               ["ZG0790"],
    })
    out = _rename_broker_cols(df)
    for col in ["cash", "live_cash", "avail_margin", "used_margin",
                "option_premium", "collateral"]:
        assert col in out.columns


def test_rename_broker_cols_partial():
    df = pl.DataFrame({"avail opening_balance": [100.0], "account": ["A"]})
    out = _rename_broker_cols(df)
    assert "cash" in out.columns
    assert "avail opening_balance" not in out.columns


# ---------------------------------------------------------------------------
# _append_total_row
# ---------------------------------------------------------------------------

def test_append_total_row_sums_rows():
    df = pl.DataFrame({
        "account": ["A", "B"],
        "cash":    [100.0, 200.0],
        "avail_margin": [50.0, 60.0],
    })
    out = _append_total_row(df, ["cash", "avail_margin"])
    total = out.filter(pl.col("account") == "TOTAL").row(0, named=True)
    assert total["cash"] == pytest.approx(300.0)
    assert total["avail_margin"] == pytest.approx(110.0)


def test_append_total_row_fills_nulls_with_zero():
    df = pl.DataFrame({"account": ["A"], "cash": [100.0]})
    out = _append_total_row(df, ["cash"])
    # No NaN / null anywhere
    assert out.null_count().to_dicts()[0]["cash"] == 0


# ---------------------------------------------------------------------------
# _add_derived_columns
# ---------------------------------------------------------------------------

def test_add_derived_columns_available_funds_equals_avail_margin():
    df = pl.DataFrame({
        "account": ["A", "TOTAL"],
        "cash":    [100.0, 100.0],
        "option_premium": [10.0, 10.0],
        "avail_margin":   [50.0, 50.0],
    })
    out = _add_derived_columns(df)
    a = out.filter(pl.col("account") == "A").row(0, named=True)
    assert a["available_funds"] == pytest.approx(50.0)
    # available_cash = cash − option_premium = 100 − 10 = 90
    assert a["available_cash"] == pytest.approx(90.0)


def test_add_derived_columns_missing_cash_defaults_to_zero():
    df = pl.DataFrame({"account": ["A"], "option_premium": [10.0], "avail_margin": [50.0]})
    out = _add_derived_columns(df)
    a = out.row(0, named=True)
    # available_cash = 0 − 10 = −10 (default cash → 0.0)
    assert a["available_cash"] == pytest.approx(-10.0)


def test_add_derived_columns_missing_avail_margin_defaults_to_zero():
    df = pl.DataFrame({"account": ["A"], "cash": [100.0]})
    out = _add_derived_columns(df)
    a = out.row(0, named=True)
    assert a["available_funds"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _stale_flag_map
# ---------------------------------------------------------------------------

def test_stale_flag_map_missing_col_empty():
    df = pl.DataFrame({"account": ["A"], "cash": [100.0]})
    assert _stale_flag_map(df) == {}


def test_stale_flag_map_only_true_rows_included():
    df = pl.DataFrame({
        "account":       ["A", "B", "C"],
        "cash":          [100.0, 200.0, 300.0],
        "account_stale": [True, False, True],
    })
    m = _stale_flag_map(df)
    assert set(m) == {"A", "C"}
    assert all(m.values()) is True


# ---------------------------------------------------------------------------
# _hydrate_row
# ---------------------------------------------------------------------------

def test_hydrate_row_total_row_untouched():
    r = {"account": "TOTAL", "cash": 100.0}
    out = _hydrate_row(dict(r), {"A": True}, {"A": "09:00 IST"})
    assert "account_stale" not in out
    assert "account_stale_since" not in out


def test_hydrate_row_stamps_stale_flag():
    r = {"account": "A", "cash": 100.0}
    out = _hydrate_row(dict(r), {"A": True}, {})
    assert out["account_stale"] is True


def test_hydrate_row_stamps_since_when_stale_and_present():
    r = {"account": "A", "cash": 100.0}
    out = _hydrate_row(dict(r), {"A": True}, {"A": "09:15 IST"})
    assert out["account_stale_since"] == "09:15 IST"


def test_hydrate_row_no_since_when_not_stale():
    r = {"account": "A", "cash": 100.0}
    out = _hydrate_row(dict(r), {}, {"A": "09:15 IST"})
    assert out["account_stale"] is False
    assert "account_stale_since" not in out


def test_hydrate_row_no_since_when_stale_but_absent_from_map():
    r = {"account": "A", "cash": 100.0}
    out = _hydrate_row(dict(r), {"A": True}, {"OTHER": "09:15 IST"})
    assert out["account_stale"] is True
    assert "account_stale_since" not in out
