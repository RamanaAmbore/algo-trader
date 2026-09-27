"""Tests for the per-account NAV breakdown (`compute_firm_nav()["by_account"]`)
introduced by the 2026-09 NAV SSOT consolidation.

Root cause fixed by this feature: `frontend/src/lib/data/nav.js`'s
`navRowForAccount`/`navByAccount` maintained a PARALLEL client-side NAV
formula (missing the `realised` term, no unrealised qty!=0 gate, no
holdings ticker-rescue fallback) that had already drifted from
`backend/api/algo/nav.py`'s `_positions_from_df`. This suite asserts the
backend now exposes a per-account breakdown built from the EXACT SAME
vectorized per-account-filtered calls the firm total uses
(`_accumulate_by_account`), so the invariant `sum(by_account) == firm total`
holds structurally, not by convention.
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from backend.api.algo.nav import (
    _accumulate_by_account,
    _funds_from_df,
    _positions_from_df,
    _holdings_from_df,
    _fetch_funds_phase,
    _fetch_positions_phase,
    _fetch_holdings_from_snapshot,
    _fetch_holdings_phase,
    compute_firm_nav,
)


@pytest.fixture
def stub_ticker():
    ticker = MagicMock()
    ticker.get_ltp_by_sym = MagicMock(return_value=None)
    return ticker


# ---------------------------------------------------------------------------
# _accumulate_by_account — pure helper
# ---------------------------------------------------------------------------

class TestAccumulateByAccount:
    def test_splits_funds_per_account_matching_total(self):
        funds_df = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0, "util option_premium": 500.0},
            {"account": "DH6847", "avail opening_balance": 20000.0, "util option_premium": 0.0},
        ])
        out: dict[str, float] = {}
        _accumulate_by_account(funds_df, out, _funds_from_df)
        assert math.isclose(out["ZG0790"], 10500.0, abs_tol=0.01)
        assert math.isclose(out["DH6847"], 20000.0, abs_tol=0.01)
        total, _ = _funds_from_df(funds_df)
        assert math.isclose(sum(out.values()), total, abs_tol=0.01)

    def test_splits_positions_per_account_including_realised(self):
        """A same-day full exit (qty=0, realised>0) must attribute its
        realised leg to ITS OWN account — the exact drift the old
        frontend formula had (it omitted `realised` entirely)."""
        positions_df = pd.DataFrame([
            {"account": "ZG0790", "symbol": "NIFTY50", "quantity": 0.0,
             "unrealised": 0.0, "realised": 750.0},
            {"account": "DH6847", "symbol": "BANKNIFTY", "quantity": 5.0,
             "unrealised": 500.0, "realised": 0.0},
        ])
        out: dict[str, float] = {}
        _accumulate_by_account(positions_df, out, _positions_from_df)
        assert math.isclose(out["ZG0790"], 750.0, abs_tol=0.01)
        assert math.isclose(out["DH6847"], 500.0, abs_tol=0.01)

    def test_splits_holdings_per_account(self, stub_ticker):
        holdings_df = pd.DataFrame([
            {"account": "ZG0790", "tradingsymbol": "INFY", "opening_quantity": 100.0, "cur_val": 15000.0},
            {"account": "DH6847", "tradingsymbol": "RELIANCE", "opening_quantity": 50.0, "cur_val": 7500.0},
        ])
        out: dict[str, float] = {}
        _accumulate_by_account(holdings_df, out, _holdings_from_df, stub_ticker)
        assert math.isclose(out["ZG0790"], 15000.0, abs_tol=0.01)
        assert math.isclose(out["DH6847"], 7500.0, abs_tol=0.01)

    def test_empty_or_missing_account_column_is_noop(self):
        out: dict[str, float] = {}
        _accumulate_by_account(pd.DataFrame(), out, _funds_from_df)
        assert out == {}
        _accumulate_by_account(pd.DataFrame([{"x": 1}]), out, _funds_from_df)
        assert out == {}

    def test_total_row_excluded(self, stub_ticker):
        holdings_df = pd.DataFrame([
            {"account": "TOTAL", "tradingsymbol": "X", "opening_quantity": 1.0, "cur_val": 999.0},
            {"account": "ZG0790", "tradingsymbol": "INFY", "opening_quantity": 100.0, "cur_val": 15000.0},
        ])
        out: dict[str, float] = {}
        _accumulate_by_account(holdings_df, out, _holdings_from_df, stub_ticker)
        assert "TOTAL" not in out
        assert math.isclose(out["ZG0790"], 15000.0, abs_tol=0.01)


# ---------------------------------------------------------------------------
# Phase functions — backward-compatible signature (by_account optional)
# ---------------------------------------------------------------------------

class TestPhaseFunctionsBackwardCompatible:
    @pytest.mark.asyncio
    async def test_holdings_phase_without_by_account_calls_snapshot_with_two_args(self):
        """Existing call-site contract preserved: when by_account is omitted,
        _fetch_holdings_from_snapshot is invoked with exactly (accounts, errs)
        — no extra positional/keyword arg — matching pre-existing callers."""
        accounts, errs = [], []
        with patch(
            "backend.api.helpers.snapshot_gate.is_exchange_closed_now",
            return_value=True,
        ), patch(
            "backend.api.algo.nav._fetch_holdings_from_snapshot",
            new=AsyncMock(return_value=188_000_000.0),
        ) as mock_snap:
            total = await _fetch_holdings_phase(accounts, errs, ticker=MagicMock())

        mock_snap.assert_called_once_with(accounts, errs)
        assert total == pytest.approx(188_000_000.0)

    @pytest.mark.asyncio
    async def test_holdings_phase_with_by_account_passes_it_through(self):
        accounts, errs = [], []
        by_acct: dict[str, float] = {}
        with patch(
            "backend.api.helpers.snapshot_gate.is_exchange_closed_now",
            return_value=True,
        ), patch(
            "backend.api.algo.nav._fetch_holdings_from_snapshot",
            new=AsyncMock(return_value=100.0),
        ) as mock_snap:
            await _fetch_holdings_phase(accounts, errs, ticker=MagicMock(), by_account=by_acct)

        mock_snap.assert_called_once_with(accounts, errs, by_acct)

    @pytest.mark.asyncio
    async def test_holdings_from_snapshot_populates_by_account(self):
        accounts, errs = [], []
        by_acct: dict[str, float] = {}
        row1 = MagicMock(); row1.cur_val = 150000.0; row1.account = "ZG1234"
        row2 = MagicMock(); row2.cur_val = 50000.0;  row2.account = "DH5678"
        mock_snap = MagicMock(rows=[row1, row2])
        with patch(
            "backend.api.routes.holdings._holdings_snapshot",
            new=AsyncMock(return_value=mock_snap),
        ):
            total = await _fetch_holdings_from_snapshot(accounts, errs, by_acct)

        assert total == pytest.approx(200000.0)
        assert by_acct == {"ZG1234": pytest.approx(150000.0), "DH5678": pytest.approx(50000.0)}

    @pytest.mark.asyncio
    async def test_funds_phase_cached_path_populates_by_account(self):
        accounts, errs = [], []
        by_acct: dict[str, float] = {}
        row1 = MagicMock(); row1.account = "ZG1234"; row1.cash = 10000.0; row1.option_premium = 100.0
        row_total = MagicMock(); row_total.account = "TOTAL"; row_total.cash = 10000.0; row_total.option_premium = 100.0
        cached = MagicMock(rows=[row1, row_total])
        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open",
            return_value=False,
        ), patch(
            "backend.api.cache.peek", return_value=cached,
        ):
            total = await _fetch_funds_phase(accounts, errs, by_acct)

        assert total == pytest.approx(10100.0)
        assert by_acct == {"ZG1234": pytest.approx(10100.0)}

    @pytest.mark.asyncio
    async def test_positions_phase_populates_by_account(self):
        accounts, errs = [], []
        by_acct: dict[str, float] = {}
        df = pd.DataFrame([
            {"account": "ZG1234", "symbol": "NIFTY50", "quantity": 10.0, "unrealised": 1000.0},
        ])
        with patch(
            "backend.api.algo.nav.asyncio.to_thread",
            new=AsyncMock(return_value=[df]),
        ):
            total = await _fetch_positions_phase(accounts, errs, by_acct)

        assert total == pytest.approx(1000.0)
        assert by_acct == {"ZG1234": pytest.approx(1000.0)}


# ---------------------------------------------------------------------------
# compute_firm_nav — by_account invariant
# ---------------------------------------------------------------------------

class TestComputeFirmNavByAccountInvariant:
    @pytest.mark.asyncio
    async def test_by_account_nav_sums_to_firm_nav(self):
        funds_df = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0, "util option_premium": 500.0},
            {"account": "DH6847", "avail opening_balance": 20000.0, "util option_premium": 0.0},
        ])
        positions_df = pd.DataFrame([
            {"account": "ZG0790", "symbol": "NIFTY50", "quantity": 0.0, "unrealised": 0.0, "realised": 750.0},
            {"account": "DH6847", "symbol": "BANKNIFTY", "quantity": 5.0, "unrealised": 500.0, "realised": 0.0},
        ])
        holdings_df = pd.DataFrame([
            {"account": "ZG0790", "tradingsymbol": "INFY", "opening_quantity": 100.0, "cur_val": 15000.0},
        ])

        with patch(
            "backend.api.algo.nav._resolve_conn_keys", new=AsyncMock(return_value=["ZG0790", "DH6847"]),
        ), patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=True,
        ), patch(
            "backend.brokers.broker_apis.fetch_margins", return_value=[funds_df],
        ), patch(
            "backend.brokers.broker_apis.fetch_positions", return_value=[positions_df],
        ), patch(
            "backend.api.helpers.snapshot_gate.is_exchange_closed_now", return_value=False,
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings", return_value=[holdings_df],
        ), patch(
            "backend.brokers.kite_ticker.get_ticker", return_value=MagicMock(get_ltp_by_sym=MagicMock(return_value=None)),
        ):
            snap = await compute_firm_nav()

        assert set(snap["by_account"].keys()) == {"ZG0790", "DH6847"}
        summed = sum(v["nav"] for v in snap["by_account"].values())
        assert summed == pytest.approx(snap["nav"], abs=0.02)

        zg = snap["by_account"]["ZG0790"]
        assert zg["cash"] == pytest.approx(10500.0, abs=0.01)
        assert zg["pos_m2m"] == pytest.approx(750.0, abs=0.01)   # realised term present
        assert zg["holdings_mtm"] == pytest.approx(15000.0, abs=0.01)
        assert zg["nav"] == pytest.approx(26250.0, abs=0.01)

        dh = snap["by_account"]["DH6847"]
        assert dh["cash"] == pytest.approx(20000.0, abs=0.01)
        assert dh["pos_m2m"] == pytest.approx(500.0, abs=0.01)
        assert dh["holdings_mtm"] == pytest.approx(0.0, abs=0.01)
