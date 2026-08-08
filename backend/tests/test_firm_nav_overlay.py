"""Tests for compute_firm_nav closed-exchange overlay.

Verifies that _overlay_closed_exchange_ltp replaces cur_val with
snapshot_ltp × quantity when an exchange is closed, and is a no-op otherwise.
"""
import pandas as pd
import pytest
from unittest.mock import patch

# Import the private helper
from backend.api.algo.nav import _overlay_closed_exchange_ltp


def _make_holdings_df(exchange="NSE", cur_val=10000.0, qty=10, account="ZG_TEST", symbol="RELIANCE"):
    return pd.DataFrame([{
        "account": account,
        "exchange": exchange,
        "tradingsymbol": symbol,
        "cur_val": cur_val,
        "quantity": qty,
    }])


class TestOverlayClosedExchangeLtp:

    def test_replaces_cur_val_when_exchange_closed(self):
        df = _make_holdings_df(exchange="NSE", cur_val=10000.0, qty=10)
        snap_map = {("ZG_TEST", "RELIANCE"): 1100.0}
        with patch("backend.api.helpers.snapshot_gate.is_exchange_closed_now", return_value=True):
            result = _overlay_closed_exchange_ltp(df, snap_map)
        assert result.at[0, "cur_val"] == pytest.approx(11000.0)  # 1100 × 10

    def test_no_change_when_exchange_open(self):
        df = _make_holdings_df(exchange="NSE", cur_val=10000.0, qty=10)
        snap_map = {("ZG_TEST", "RELIANCE"): 1100.0}
        with patch("backend.api.helpers.snapshot_gate.is_exchange_closed_now", return_value=False):
            result = _overlay_closed_exchange_ltp(df, snap_map)
        assert result.at[0, "cur_val"] == pytest.approx(10000.0)  # unchanged

    def test_no_op_when_snap_map_empty(self):
        df = _make_holdings_df(cur_val=10000.0)
        with patch("backend.api.helpers.snapshot_gate.is_exchange_closed_now", return_value=True):
            result = _overlay_closed_exchange_ltp(df, {})
        assert result.at[0, "cur_val"] == pytest.approx(10000.0)

    def test_no_op_when_df_empty(self):
        df = pd.DataFrame(columns=["account", "exchange", "tradingsymbol", "cur_val", "quantity"])
        snap_map = {("ZG_TEST", "RELIANCE"): 1100.0}
        result = _overlay_closed_exchange_ltp(df, snap_map)
        assert result.empty
