"""
Regression: daily_snapshot must backfill market data (last_price /
close_price) via `broker_apis.backfill_market_data` BEFORE the
`_is_zero_payload_row` guard filters rows.

Defect (2026-07-03):
    Operator reported Groww account (GR87DF) breakdown missing from
    public `/performance` page. Root cause: Groww's `holdings()` /
    `positions()` sometimes return real qty + avg_cost but
    `last_price=0` + `close_price=0` when Groww's market-data cache
    is cold. Before this fix, the snapshot writer left those zeros
    intact, saw the `_is_zero_payload_row` fingerprint (avg > 0 AND
    ltp=0 AND pnl=0 AND day_pnl=0), and dropped the row. `daily_book`
    ended up with zero Groww holdings, so `/api/holdings` snapshot
    reader (closed-hours branch) served zero Groww breakdown and
    Groww disappeared from PerformancePage's NAV grid entirely
    (accounts list = union of holdings + positions + funds — all
    three were empty for GR87DF).

Fix: `_fetch_account_data` now runs `_backfill_market_data_dicts`
right after `broker.holdings()` / `broker.positions()`. This is the
snapshot-path equivalent of the live-path
`broker_apis.backfill_market_data(pd.concat(...))` call in
`/api/holdings` + `/api/positions` route handlers.

Five quality dimensions:
  1. SSOT      — snapshot backfill delegates to
                 `broker_apis.backfill_market_data` — the SAME helper
                 the live routes use, so both paths patch identically.
  2. Perf      — one batched PriceBroker.quote() call per snapshot;
                 no per-row round-trip.
  3. Stale     — grep asserts `_backfill_market_data_dicts` present.
  4. Reuse     — helper wraps the canonical `backfill_market_data`;
                 it does NOT re-implement quote-lookup logic.
  5. UX        — Groww holdings rows land in `daily_book`; NAV
                 breakdown surfaces GR87DF row on public /performance.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Dimension 3 — static source checks
# ---------------------------------------------------------------------------

_SNAP_SRC = Path(__file__).parent.parent / "api" / "algo" / "daily_snapshot.py"


def _src() -> str:
    return _SNAP_SRC.read_text(encoding="utf-8")


def test_snapshot_defines_backfill_helper():
    src = _src()
    assert "_backfill_market_data_dicts" in src, (
        "daily_snapshot.py must define _backfill_market_data_dicts helper"
    )


def test_snapshot_invokes_backfill_after_broker_fetch():
    src = _src()
    # Helper must be called from _fetch_account_data (or equivalent)
    # so both holdings + positions get patched before row builders run.
    assert src.count("_backfill_market_data_dicts") >= 3, (
        "_backfill_market_data_dicts must appear in definition + holdings "
        "call site + positions call site"
    )


def test_snapshot_delegates_to_canonical_backfill():
    """SSOT — the snapshot helper wraps broker_apis.backfill_market_data
    rather than re-implementing quote lookup."""
    src = _src()
    assert "backfill_market_data" in src, (
        "snapshot helper must import + call broker_apis.backfill_market_data"
    )


# ---------------------------------------------------------------------------
# Dimension 4 — helper behaviour: zero-LTP dict rows get patched
# ---------------------------------------------------------------------------

class TestBackfillMarketDataDicts:
    def _fn(self):
        from backend.api.algo.daily_snapshot import _backfill_market_data_dicts
        return _backfill_market_data_dicts

    def test_empty_input_returns_zero(self):
        fn = self._fn()
        assert fn([]) == 0

    def test_zero_ltp_row_gets_patched(self):
        """Groww-shape holdings with ltp=0 + close=0 → backfill patches
        both from PriceBroker.quote() and the guard no longer fires."""
        fn = self._fn()
        rows = [{
            "tradingsymbol": "RELIANCE", "exchange": "NSE",
            "opening_quantity": 5, "average_price": 2800.0,
            "last_price": 0.0, "close_price": 0.0,
            "pnl": 0.0, "day_change": 0.0,
        }]

        # Mock backfill_market_data to simulate Kite quote patching
        # last_price + close_price in-place (mirrors production behaviour).
        def _fake_backfill(df):
            df.loc[0, "last_price"] = 2900.0
            df.loc[0, "close_price"] = 2850.0
            return 1

        with patch("backend.brokers.broker_apis.backfill_market_data",
                   side_effect=_fake_backfill):
            n_patched = fn(rows, qty_col="opening_quantity")

        assert n_patched == 1
        assert rows[0]["last_price"] == 2900.0
        assert rows[0]["close_price"] == 2850.0
        # pnl should be recomputed = (ltp - avg) * qty = (2900 - 2800) * 5 = 500
        assert rows[0]["pnl"] == 500.0
        # day_change should be recomputed = ltp - close = 2900 - 2850 = 50
        assert rows[0]["day_change"] == 50.0

    def test_non_zero_broker_ltp_preserved(self):
        """When broker already returned real LTP, backfill must not
        overwrite (backfill_market_data internally skips non-zero rows)."""
        fn = self._fn()
        rows = [{
            "tradingsymbol": "INFY", "exchange": "NSE",
            "opening_quantity": 10, "average_price": 1500.0,
            "last_price": 1560.0, "close_price": 1550.0,
            "pnl": 600.0, "day_change": 10.0,
        }]

        # Fake backfill leaves non-zero rows untouched (production behaviour).
        def _fake_backfill(df):
            return 0

        with patch("backend.brokers.broker_apis.backfill_market_data",
                   side_effect=_fake_backfill):
            fn(rows, qty_col="opening_quantity")

        assert rows[0]["last_price"] == 1560.0
        assert rows[0]["pnl"] == 600.0

    def test_backfill_failure_is_non_fatal(self):
        """A PriceBroker outage during backfill must NOT drop the rows —
        they pass through untouched and the caller's zero-payload guard
        will still filter them (fallback to pre-fix behaviour)."""
        fn = self._fn()
        rows = [{
            "tradingsymbol": "GOLDBEES", "exchange": "NSE",
            "opening_quantity": 100, "average_price": 65.0,
            "last_price": 0.0, "close_price": 0.0,
            "pnl": 0.0, "day_change": 0.0,
        }]

        def _fake_backfill(df):
            raise RuntimeError("PriceBroker outage")

        with patch("backend.brokers.broker_apis.backfill_market_data",
                   side_effect=_fake_backfill):
            n_patched = fn(rows, qty_col="opening_quantity")

        assert n_patched == 0
        # Row is still present — caller decides via zero-payload guard.
        assert rows[0]["last_price"] == 0.0


# ---------------------------------------------------------------------------
# Dimension 5 — end-to-end: Groww holdings with zero LTP survive snapshot
# ---------------------------------------------------------------------------

class TestGrowwHoldingsSurviveSnapshot:
    _D = date(2026, 7, 3)
    _NOW_EOD = datetime(2026, 7, 3, 23, 35, tzinfo=timezone.utc)

    def _groww_zero_ltp_holdings(self) -> list[dict]:
        """Groww holdings shape with the Kite-normalised column set.
        Groww's _normalise_holdings returns close_price=0 when Groww
        omits previous_close, and pnl=0 when ltp=0.
        """
        return [
            {
                "tradingsymbol": "HFCL", "exchange": "NSE",
                "opening_quantity": 100, "quantity": 100,
                "average_price": 82.5,
                "last_price": 0.0, "close_price": 0.0,
                "pnl": 0.0, "day_change": 0.0,
                "day_change_percentage": 0.0,
                "product": "CNC",
            },
            {
                "tradingsymbol": "NATIONALUM", "exchange": "NSE",
                "opening_quantity": 50, "quantity": 50,
                "average_price": 235.0,
                "last_price": 0.0, "close_price": 0.0,
                "pnl": 0.0, "day_change": 0.0,
                "day_change_percentage": 0.0,
                "product": "CNC",
            },
        ]

    def test_groww_rows_backfilled_then_survive_guard(self):
        """After the fix, Groww holdings rows land in daily_book because
        backfill patches ltp before the zero-payload guard fires."""
        from backend.api.algo import daily_snapshot as ds

        def _fake_backfill(df):
            # Kite quote() populates real close + LTP for both Groww
            # holdings (they're plain NSE equity tickers Kite knows).
            for i in df.index:
                df.at[i, "last_price"] = 90.0 if df.at[i, "tradingsymbol"] == "HFCL" else 245.0
                df.at[i, "close_price"] = 88.0 if df.at[i, "tradingsymbol"] == "HFCL" else 240.0
            return len(df)

        mock_broker = MagicMock()
        mock_broker.account = "GR87DF"
        mock_broker.holdings.return_value = self._groww_zero_ltp_holdings()
        mock_broker.positions.return_value = {"net": []}
        mock_broker.trades.return_value = []
        mock_broker.margins.return_value = {}

        with patch("backend.brokers.broker_apis.backfill_market_data",
                   side_effect=_fake_backfill):
            raw = ds._fetch_account_data(mock_broker, "GR87DF", self._D)

        # After backfill, both Groww rows have real ltp/close
        assert len(raw["holdings"]) == 2
        assert raw["holdings"][0]["last_price"] > 0
        assert raw["holdings"][0]["close_price"] > 0
        assert raw["holdings"][1]["last_price"] > 0

        # And the row builder no longer filters them.
        # Pass an EOD timestamp so the mid-session gate doesn't blank ltp.
        rows = ds._holdings_rows("GR87DF", self._D,
                                  raw["holdings"],
                                  datetime(2026, 7, 3, 23, 35))
        assert len(rows) == 2, (
            f"Expected 2 Groww holdings rows to survive; got {len(rows)}"
        )
        symbols = {r["symbol"] for r in rows}
        assert symbols == {"HFCL", "NATIONALUM"}

    def test_pre_fix_regression_shape(self):
        """Sanity check that WITHOUT backfill the pre-fix shape would
        have been filtered — proves the guard depends on ltp > 0."""
        from backend.api.algo.daily_snapshot import _is_zero_payload_row
        # Pre-fix Groww row: ltp=0, day_pnl=0, total_pnl=0, avg>0.
        row = {"average_price": 82.5}
        assert _is_zero_payload_row(row, ltp=0.0, day_pnl=0.0,
                                     total_pnl=0.0) is True
