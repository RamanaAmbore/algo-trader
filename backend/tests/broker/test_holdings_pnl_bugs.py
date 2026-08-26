"""Tests for 4 holdings & P&L bug fixes (Aug 2026).

Covers four critical data integrity fixes across three modules:

  1. P1-A: daily_snapshot.py line 468 — `quantity` takes priority over
     `opening_quantity` when building holdings snapshot rows. Partial-sell
     holdings must use the remaining shares, not the original lot, so
     P&L and value computations are correct. Regression: fix ensures
     `qty=50` is written to the snapshot, not `qty=100` (opening_quantity).

  2. P1-B: daily_snapshot.py lines 473–476 — `previous_close` fallback
     chain now includes `ltp_val` as the last resort. Holding with no
     historical close (cold-boot) and close_price=0 must not land a NULL
     previous_close in the snapshot — use the current LTP instead.
     Regression: guards against `previous_close IS NULL` in the DB row.

  3. P2-B: broker_apis.py `_enrich_holdings` — two-pass Polars strategy
     so `cur_val = inv_val + pnl` doesn't crash when the input frame
     lacks a `pnl` column (Dhan adapter path). Pre-fix: Dhan holdings
     landed with `cur_val=None` and broke the holdings route's P&L grid.
     Post-fix: computes pnl first, then uses it in cur_val calc in a
     safe two-pass design.

  4. P2-A: pnl_math.py line 209 — Case 2 backstop condition changed from
     `_oq > 0` to `_oq != 0` so short overnight positions (oq < 0) also
     receive the `day_change_val` recovery formula. Regression: short
     positions with zeroed dcv were stuck at dcv=0 forever.

Five quality dimensions per fix:
  * SSOT — canonical implementation in daily_snapshot.py / broker_apis.py /
    pnl_math.py; no client-side workarounds.
  * Perf — no extra I/O; DataFrame ops vectorised in Polars / pandas.
  * Stale-code grep — docstrings updated; no dormant code paths.
  * Reuse — holdings enrichment reused across routes/background; backstop
    shared between positions route + background + snapshot writer.
  * UX — partial-sell P&L shown correctly; Dhan holdings visible in grids;
    short positions day P&L non-zero; cold-boot snapshots complete.
"""

from __future__ import annotations

import pandas as pd
import polars as pl
import pytest
from unittest.mock import MagicMock, patch

from backend.api.algo.daily_snapshot import _holdings_rows
from backend.brokers.broker_apis import _enrich_holdings
from backend.api.algo.pnl_math import apply_day_change_backstop


def _make_df(**kwargs) -> pd.DataFrame:
    """Build a single-row DataFrame from keyword column values."""
    return pd.DataFrame([kwargs])


class TestDailySnapshotQtyUsesQuantityNotOpeningQuantity:
    """P1-A: holdings snapshot writes quantity, not opening_quantity."""

    def test_daily_snapshot_qty_uses_quantity_not_opening_quantity(self):
        """Partial-sell holding: snapshot must write quantity=50, not opening_quantity=100.

        When a holding is sold partially, quantity (remaining) < opening_quantity (original).
        The snapshot row's qty field must reflect the remaining quantity so downstream
        P&L and value calculations (inv_val, cur_val, day P&L) are correct.
        """
        from datetime import datetime, date, timezone
        from backend.shared.helpers.date_time_utils import timestamp_indian

        # Simulate a partial-sell holding (quantity=50, opening_quantity=100)
        raw_holding = {
            "tradingsymbol": "SBIN",
            "exchange": "NSE",
            "quantity": 50,  # remaining after sell
            "opening_quantity": 100,  # original lot
            "average_price": 200.0,
            "last_price": 210.0,
            "close_price": 205.0,
            "pnl": 1000.0,  # (210-200)*50 + (200-205)*50 from prior session
            "day_change": 250.0,
        }

        # Empty prev_ltp_map so we must use close_price fallback
        prev_ltp_map = {}
        now_ist = timestamp_indian()
        ltp_val = 210.0

        # Call _holdings_rows which builds the snapshot row dict
        rows = _holdings_rows(
            account="TEST_ACCT",
            target_date=now_ist.date(),
            raw=[raw_holding],
            now_ist=now_ist,
            settled=False,
            market_open=False,  # force EOD mode so ltp/day_pnl are captured
            prev_ltp_map=prev_ltp_map,
        )

        assert len(rows) == 1, "Should produce exactly one row"
        row = rows[0]
        # Regression guard: must be 50 (quantity), not 100 (opening_quantity)
        assert row["qty"] == 50, f"Expected qty=50 but got {row['qty']}"


class TestDailySnapshotPreviousCloseNeverNull:
    """P1-B: previous_close fallback chain includes ltp_val as last resort."""

    def test_daily_snapshot_previous_close_never_null(self):
        """Holding with no prev_ltp_map entry and close_price=0: previous_close must be ltp_val.

        When a holding has close_price=0 (e.g., Dhan cold-boot) and no historical
        entry in prev_ltp_map, the fallback chain must produce a non-null value
        using the current LTP as the last resort. This prevents `previous_close IS NULL`
        rows landing in the daily_book snapshot.
        """
        from datetime import datetime, date, timezone
        from backend.shared.helpers.date_time_utils import timestamp_indian

        raw_holding = {
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "quantity": 50,
            "opening_quantity": 50,
            "average_price": 1500.0,
            "last_price": 1520.0,
            "close_price": 0.0,  # cold-boot: no prior session close yet
            "pnl": 1000.0,
        }

        prev_ltp_map = {}  # empty map — no historical prev_close
        now_ist = timestamp_indian()
        ltp_val = 1520.0

        rows = _holdings_rows(
            account="TEST_ACCT",
            target_date=now_ist.date(),
            raw=[raw_holding],
            now_ist=now_ist,
            settled=False,
            market_open=False,
            prev_ltp_map=prev_ltp_map,
        )

        assert len(rows) == 1
        row = rows[0]
        # Regression guard: previous_close must not be None; should fall back to ltp_val
        assert row["previous_close"] is not None, "previous_close must not be None"
        assert row["previous_close"] == pytest.approx(
            ltp_val
        ), f"Expected previous_close={ltp_val} but got {row['previous_close']}"

    def test_daily_snapshot_previous_close_fallback_chain(self):
        """Verify the complete fallback chain: prev_ltp_map > close_price > ltp_val."""
        from backend.shared.helpers.date_time_utils import timestamp_indian

        now_ist = timestamp_indian()
        ltp_val = 1520.0

        # Case 1: prev_ltp_map entry exists — use it (highest priority)
        prev_ltp_map_case1 = {
            ("TEST_ACCT", "INFY", "holdings"): 1510.0,
        }
        raw_holding = {
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "quantity": 50,
            "opening_quantity": 50,
            "average_price": 1500.0,
            "last_price": ltp_val,
            "close_price": 1505.0,  # not used
        }
        rows = _holdings_rows(
            account="TEST_ACCT",
            target_date=now_ist.date(),
            raw=[raw_holding],
            now_ist=now_ist,
            settled=False,
            market_open=False,
            prev_ltp_map=prev_ltp_map_case1,
        )
        assert rows[0]["previous_close"] == pytest.approx(1510.0), (
            "prev_ltp_map entry should win"
        )

        # Case 2: no prev_ltp_map, but close_price > 0 — use close_price
        raw_holding2 = {
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "quantity": 50,
            "opening_quantity": 50,
            "average_price": 1500.0,
            "last_price": ltp_val,
            "close_price": 1505.0,
        }
        rows2 = _holdings_rows(
            account="TEST_ACCT",
            target_date=now_ist.date(),
            raw=[raw_holding2],
            now_ist=now_ist,
            settled=False,
            market_open=False,
            prev_ltp_map={},
        )
        assert rows2[0]["previous_close"] == pytest.approx(1505.0), (
            "close_price should be used when prev_ltp_map is empty"
        )

        # Case 3: no prev_ltp_map, close_price=0 — fall back to ltp_val
        raw_holding3 = {
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "quantity": 50,
            "opening_quantity": 50,
            "average_price": 1500.0,
            "last_price": ltp_val,
            "close_price": 0.0,
        }
        rows3 = _holdings_rows(
            account="TEST_ACCT",
            target_date=now_ist.date(),
            raw=[raw_holding3],
            now_ist=now_ist,
            settled=False,
            market_open=False,
            prev_ltp_map={},
        )
        assert rows3[0]["previous_close"] == pytest.approx(ltp_val), (
            "ltp_val should be the final fallback when close_price is 0"
        )


class TestEnrichHoldingsTwoPassDhanNoPnlColumn:
    """P2-B: _enrich_holdings works for Dhan (no pnl column in input)."""

    def test_enrich_holdings_two_pass_dhan_no_pnl_column(self):
        """DataFrame without pnl column (Dhan path): _enrich_holdings must populate cur_val > 0.

        Dhan adapter sometimes returns holdings frames without a `pnl` column.
        The _enrich_holdings function must handle this gracefully by:
          1. Computing pnl from (ltp - avg) * qty in the first pass
          2. Using the computed pnl to calculate cur_val = inv_val + pnl in the second pass

        This test verifies that cur_val is correctly computed even when pnl
        was not in the original frame.
        """
        # Build a Dhan-like frame: no `pnl` column, but has the raw data needed to compute it
        df = pd.DataFrame(
            [
                {
                    "last_price": 210.0,
                    "average_price": 200.0,
                    "quantity": 50,
                    "opening_quantity": 50,
                    "close_price": 205.0,
                    # NOTE: no `pnl` column — Dhan omits this
                }
            ]
        )

        # Ensure no pnl column exists pre-call
        assert "pnl" not in df.columns, "Test setup: pnl column should not exist yet"

        # Call _enrich_holdings which should compute pnl and cur_val
        result = _enrich_holdings(df)

        # Post-call: pnl should be computed
        assert "pnl" in result.columns, "pnl column should exist after enrichment"
        assert result["pnl"].iloc[0] != 0, f"pnl should be non-zero, got {result['pnl'].iloc[0]}"

        # cur_val should also be computed
        assert "cur_val" in result.columns, "cur_val column should exist after enrichment"
        assert (
            result["cur_val"].iloc[0] > 0
        ), f"cur_val should be > 0, got {result['cur_val'].iloc[0]}"

    def test_enrich_holdings_pnl_column_consistency(self):
        """When pnl is provided vs computed, results should match.

        Two frames: one with explicit pnl, one without. Both should produce
        identical cur_val after enrichment (up to floating-point precision).
        """
        common_cols = {
            "last_price": 215.0,
            "average_price": 200.0,
            "quantity": 50,
            "opening_quantity": 50,
            "close_price": 205.0,
        }

        # Frame 1: with explicit pnl column
        df_with_pnl = pd.DataFrame([{**common_cols, "pnl": 750.0}])

        # Frame 2: without pnl column (Dhan path)
        df_without_pnl = pd.DataFrame([common_cols])

        result_with = _enrich_holdings(df_with_pnl)
        result_without = _enrich_holdings(df_without_pnl)

        # Both should have pnl column post-enrichment
        assert "pnl" in result_with.columns
        assert "pnl" in result_without.columns

        # Both should have cur_val column post-enrichment
        assert "cur_val" in result_with.columns
        assert "cur_val" in result_without.columns

        # cur_val should be identical (same formula: inv_val + pnl)
        assert result_with["cur_val"].iloc[0] == pytest.approx(
            result_without["cur_val"].iloc[0]
        ), "cur_val must be consistent regardless of whether pnl was in input"


class TestEnrichHoldingsTwoPassKitePath:
    """P2-B (Kite path): two-pass fix must not break frames that already have a pnl column."""

    def test_kite_pnl_column_trusted(self):
        """Kite ships pnl; broker value must be preferred over formula when not-null."""
        broker_pnl = 750.0  # not equal to (210-200)*50 = 500 — proves broker value is used
        df = pd.DataFrame(
            [
                {
                    "last_price": 210.0,
                    "average_price": 200.0,
                    "quantity": 50,
                    "close_price": 205.0,
                    "pnl": broker_pnl,
                }
            ]
        )
        result = _enrich_holdings(df)

        assert "pnl" in result.columns
        assert result["pnl"].iloc[0] == pytest.approx(broker_pnl), (
            f"Broker pnl should be trusted; expected {broker_pnl}, got {result['pnl'].iloc[0]}"
        )
        # cur_val = inv_val + pnl; inv_val = avg*qty = 200*50 = 10000
        assert "cur_val" in result.columns
        assert result["cur_val"].iloc[0] == pytest.approx(200.0 * 50 + broker_pnl), (
            f"cur_val = inv_val + broker_pnl; got {result['cur_val'].iloc[0]}"
        )

    def test_dhan_vs_kite_cur_val_both_positive(self):
        """Both Dhan (no pnl) and Kite (has pnl) paths must produce positive cur_val.

        This is the primary regression guard for the sibling-alias Polars bug.
        Before the two-pass fix, Dhan frames raised ColumnNotFoundError on
        pl.col('pnl') inside a single with_columns pass, leaving cur_val absent.
        """
        dhan_df = pd.DataFrame(
            [{"last_price": 210.0, "average_price": 200.0, "quantity": 50, "close_price": 205.0}]
        )
        kite_df = pd.DataFrame(
            [
                {
                    "last_price": 210.0,
                    "average_price": 200.0,
                    "quantity": 50,
                    "close_price": 205.0,
                    "pnl": 500.0,
                }
            ]
        )

        dhan_result = _enrich_holdings(dhan_df)
        kite_result = _enrich_holdings(kite_df)

        for label, result in [("Dhan", dhan_result), ("Kite", kite_result)]:
            assert "cur_val" in result.columns, f"{label}: cur_val missing"
            assert result["cur_val"].iloc[0] > 0, (
                f"{label}: cur_val must be > 0, got {result['cur_val'].iloc[0]}"
            )

    def test_dhan_pnl_percentage_nonzero_after_fix(self):
        """Dhan frame without pnl: pnl_percentage must be non-zero after two-pass enrichment.

        pnl_percentage = pnl / inv_val * 100. Without the two-pass fix, pnl is never
        computed so pnl_percentage stays absent or zero.
        """
        # ltp=220, avg=200, qty=100 → pnl=2000, inv_val=20000, pnl_pct=10%
        df = pd.DataFrame(
            [{"last_price": 220.0, "average_price": 200.0, "quantity": 100, "close_price": 210.0}]
        )
        result = _enrich_holdings(df)

        assert "pnl_percentage" in result.columns, "pnl_percentage must exist"
        val = result["pnl_percentage"].iloc[0]
        assert val == pytest.approx(10.0, rel=1e-6), f"Expected 10.0%, got {val}"


class TestCase2BackstopFiresForShortOvernightPositions:
    """P2-A: Case 2 backstop fires for short overnight positions (oq != 0, not oq > 0)."""

    def test_case2_backstop_fires_for_short_overnight(self):
        """Short overnight position (oq=-10): apply_day_change_backstop must set day_change_val != 0.

        Case 2 backstop should fire for SHORT overnight positions (oq < 0), not only
        for LONG overnight positions (oq > 0). The condition was `_oq > 0` but should
        be `_oq != 0` to include both long and short.

        For a short position:
          oq = -10 (sold short)
          qty = -10 (still holding the short)
          close_price = 100 (close of prior session)
          average_price = 105 (short sold at avg 105)
          pnl = -500 (position is in profit: short at 105, now at 100)
          day_change_val = 0 (LTP gate zeroed it)

        Case 2 recovery formula: day_pnl = pnl − (close − avg) × oq
          = -500 − (100 − 105) × (-10)
          = -500 − 50
          = -550

        Pre-fix: case 2 condition was `_oq > 0`, so short position (oq=-10 < 0)
        didn't match. Result: day_change_val stayed 0 (bug).

        Post-fix: condition is `_oq != 0`, so short position matches. Result:
        day_change_val = -550 (correct).
        """
        df = _make_df(
            overnight_quantity=-10,  # short
            quantity=-10,
            day_change_val=0.0,  # dcv was zeroed by gate
            pnl=-500.0,  # broker pnl is valid (profit on short)
            close_price=100.0,
            average_price=105.0,  # shorted at 105
        )

        result = apply_day_change_backstop(df)

        # Regression guard: day_change_val must not be 0; must be computed
        assert result.loc[0, "day_change_val"] != 0.0, (
            "day_change_val must be non-zero for short overnight position"
        )

        # Expected value: pnl - (close - avg) * oq = -500 - (100 - 105) * (-10) = -550
        expected_dcv = -500.0 - (100.0 - 105.0) * (-10)
        assert result.loc[0, "day_change_val"] == pytest.approx(
            expected_dcv
        ), f"Expected day_change_val={expected_dcv} but got {result.loc[0, 'day_change_val']}"

    def test_case2_backstop_short_vs_long_consistency(self):
        """Short and long positions with symmetric P&L must have opposite day_change_val signs."""
        # Long overnight position
        long_df = _make_df(
            overnight_quantity=10,
            quantity=10,
            day_change_val=0.0,
            pnl=500.0,
            close_price=100.0,
            average_price=95.0,
        )

        # Short overnight position with opposite signs
        short_df = _make_df(
            overnight_quantity=-10,
            quantity=-10,
            day_change_val=0.0,
            pnl=-500.0,  # opposite sign (profit on short)
            close_price=100.0,
            average_price=105.0,  # shorted at 105
        )

        long_result = apply_day_change_backstop(long_df)
        short_result = apply_day_change_backstop(short_df)

        long_dcv = long_result.loc[0, "day_change_val"]
        short_dcv = short_result.loc[0, "day_change_val"]

        # Both should be non-zero (both fire Case 2)
        assert long_dcv != 0.0, "Long overnight should fire Case 2"
        assert short_dcv != 0.0, "Short overnight should fire Case 2"

        # Signs should be opposite (long profit vs short profit)
        assert long_dcv * short_dcv < 0, (
            f"Long and short day_change_val should have opposite signs: "
            f"long={long_dcv}, short={short_dcv}"
        )

    def test_case2_backstop_short_loss_position(self):
        """Short position with loss must also fire Case 2."""
        df = _make_df(
            overnight_quantity=-10,
            quantity=-10,
            day_change_val=0.0,
            pnl=-200.0,  # loss on short (price went down from short entry)
            close_price=110.0,
            average_price=100.0,  # shorted at 100
        )

        result = apply_day_change_backstop(df)

        # Must fire Case 2 for short loss position too
        assert result.loc[0, "day_change_val"] != 0.0, (
            "Short overnight loss position must fire Case 2"
        )

        # Expected: pnl - (close - avg) * oq = -200 - (110 - 100) * (-10) = -100
        expected_dcv = -200.0 - (110.0 - 100.0) * (-10)
        assert result.loc[0, "day_change_val"] == pytest.approx(
            expected_dcv
        ), f"Expected {expected_dcv} but got {result.loc[0, 'day_change_val']}"


class TestBrokerPnlZeroPreMarket:
    """Pre-market pnl=0.0 guard in _build_holdings_pnl_expr.

    Kite sends pnl=0.0 explicitly (not null) during the pre-market window
    when last_price=0. Trusting that zero would set cur_val = inv_val + 0 =
    inv_val, making the NavStrip H slot show invested amount instead of the
    correct current market value. The fix: treat pnl=0.0 from the broker as
    untrustworthy and fall back to the computed formula when prices are valid.
    """

    def test_broker_pnl_zero_with_valid_prices_uses_computed_pnl(self):
        """broker pnl=0.0 (not null), last_price=150, avg=100, qty=100 → pnl=5000, cur_val=15000.

        The broker sends pnl=0.0 explicitly during pre-market. The fix must
        ignore that zero and use the formula (ltp - avg) * qty = 5000 instead,
        so cur_val = inv_val + 5000 = 10000 + 5000 = 15000 (not 10000).
        """
        df = pd.DataFrame(
            [
                {
                    "last_price": 150.0,
                    "average_price": 100.0,
                    "quantity": 100,
                    "opening_quantity": 100,
                    "close_price": 120.0,
                    "pnl": 0.0,  # broker sends explicit zero — pre-market window
                }
            ]
        )

        result = _enrich_holdings(df)

        # pnl must be computed from formula, not trusted from broker zero
        assert result["pnl"].iloc[0] == pytest.approx(5000.0), (
            f"Expected pnl=5000 (formula), got {result['pnl'].iloc[0]}"
        )
        # cur_val = inv_val + pnl = 10000 + 5000 = 15000 (not 10000 = inv_val)
        assert result["cur_val"].iloc[0] == pytest.approx(15000.0), (
            f"Expected cur_val=15000, got {result['cur_val'].iloc[0]}"
        )

    def test_broker_pnl_zero_breakeven_no_regression(self):
        """Genuine breakeven: broker pnl=0, last_price==avg → pnl=0, cur_val=inv_val.

        At true breakeven (ltp == avg), the formula (ltp - avg) * qty = 0.
        The fix must not regress this — cur_val should still equal inv_val
        because the position is genuinely flat.
        """
        df = pd.DataFrame(
            [
                {
                    "last_price": 100.0,
                    "average_price": 100.0,
                    "quantity": 100,
                    "opening_quantity": 100,
                    "close_price": 100.0,
                    "pnl": 0.0,  # broker zero AND formula zero — genuinely flat
                }
            ]
        )

        result = _enrich_holdings(df)

        # pnl = (100 - 100) * 100 = 0 — formula agrees with broker
        assert result["pnl"].iloc[0] == pytest.approx(0.0), (
            f"Expected pnl=0 at breakeven, got {result['pnl'].iloc[0]}"
        )
        # cur_val = inv_val + 0 = 10000 (correct — no distortion)
        assert result["cur_val"].iloc[0] == pytest.approx(10000.0), (
            f"Expected cur_val=10000 at breakeven, got {result['cur_val'].iloc[0]}"
        )
