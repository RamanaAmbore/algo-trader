"""Tests for holdings day P&L fix — previous_close field and backstop.

Three mandatory behaviours (per task spec):
  1. `previous_close` field present and non-zero in HoldingRow response.
  2. `_override_stale_close_for_holdings` writes `previous_close` for ALL rows
     (not just close_price-patched ones), using the COALESCE(previous_close, ltp)
     value from daily_book.
  3. `apply_day_change_backstop` recovers day_change_val=0 for a holdings row
     with pnl≠0 (Case 1 backstop — new position / LTP-gate zero).

Quality dimensions:
  - SSOT: HoldingRow schema is the single source for row shape.
  - Performance: DB is mocked — no broker / DB I/O in any test.
  - Stale-code guard: `_ROW_COLS` membership verified to ensure column is
    selected by polars before reaching `_build_holding_rows`.
  - Reusable component: `apply_day_change_backstop` imported from pnl_math.
  - UX correctness: `previous_close=0` for unmatched rows, not NaN / missing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

_IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_raw_df(
    account: str = "TEST001",
    symbol: str = "RELIANCE",
    exchange: str = "NSE",
    quantity: int = 50,
    opening_quantity: int = 50,
    last_price: float = 150.0,
    close_price: float = 150.0,
    average_price: float = 140.0,
    pnl: float = 500.0,
    day_change_val: float = 0.0,
    day_change: float = 0.0,
    day_change_percentage: float = 0.0,
    pnl_percentage: float = 0.0,
) -> pd.DataFrame:
    """Minimal holdings DataFrame shaped like _fetch() produces."""
    return pd.DataFrame([{
        "account": account,
        "tradingsymbol": symbol,
        "exchange": exchange,
        "quantity": quantity,
        "opening_quantity": opening_quantity,
        "average_price": average_price,
        "last_price": last_price,
        "close_price": close_price,
        "inv_val": average_price * quantity,
        "cur_val": last_price * quantity,
        "pnl": pnl,
        "pnl_percentage": pnl_percentage,
        "day_change": day_change,
        "day_change_val": day_change_val,
        "day_change_percentage": day_change_percentage,
        "last_price_stale": False,
        "account_stale": False,
    }])


def _run_override(
    df: pd.DataFrame,
    snapshot_rows: list,
    mock_time: datetime | None = None,
) -> pd.DataFrame:
    """Invoke _override_stale_close_for_holdings with a mocked DB session.

    snapshot_rows: list of (account, symbol, ref_close) 3-tuples representing
        the result of SELECT COALESCE(previous_close, ltp) AS ref_close … from
        daily_book.
    """
    from backend.api.routes.holdings import _override_stale_close_for_holdings

    if mock_time is None:
        mock_time = datetime(2026, 8, 20, 9, 0, 0, tzinfo=_IST)

    mock_result = MagicMock()
    mock_result.all.return_value = snapshot_rows

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian",
            return_value=mock_time,
        ),
    ):
        asyncio.run(_override_stale_close_for_holdings(df))

    return df


# ===========================================================================
# Requirement 1: `previous_close` field present and non-zero in HoldingRow
# ===========================================================================

class TestPreviousCloseInHoldingRowSchema:
    """HoldingRow schema must expose previous_close and the field must flow
    through _ROW_COLS → polars select → _build_holding_rows.
    """

    def test_holding_row_has_previous_close_attribute(self):
        """HoldingRow struct declares previous_close with default 0.0."""
        from backend.api.schemas import HoldingRow

        row = HoldingRow(
            account="TEST001",
            tradingsymbol="RELIANCE",
            exchange="NSE",
            quantity=50,
            average_price=140.0,
            close_price=145.0,
            inv_val=7000.0,
            cur_val=7500.0,
            pnl=500.0,
            pnl_percentage=7.14,
        )
        assert hasattr(row, "previous_close"), (
            "HoldingRow must declare previous_close field — add to schemas.py"
        )
        assert row.previous_close == 0.0, "Default value must be 0.0"

    def test_holding_row_previous_close_accepts_nonzero_value(self):
        """HoldingRow.previous_close carries the value when explicitly supplied."""
        from backend.api.schemas import HoldingRow

        row = HoldingRow(
            account="TEST001",
            tradingsymbol="RELIANCE",
            exchange="NSE",
            quantity=50,
            average_price=140.0,
            close_price=145.0,
            inv_val=7000.0,
            cur_val=7500.0,
            pnl=500.0,
            pnl_percentage=7.14,
            previous_close=143.5,
        )
        assert abs(row.previous_close - 143.5) < 0.001, (
            f"previous_close must be 143.5, got {row.previous_close}"
        )

    def test_previous_close_in_row_cols(self):
        """'previous_close' must appear in _ROW_COLS for polars select."""
        from backend.api.routes.holdings import _ROW_COLS

        assert "previous_close" in _ROW_COLS, (
            "'previous_close' must be listed in _ROW_COLS so the polars select "
            "includes it before _build_holding_rows constructs HoldingRow objects"
        )

    def test_previous_close_flows_from_dataframe_to_holding_row(self):
        """End-to-end: override sets previous_close in raw DataFrame →
        polars select includes it → HoldingRow.previous_close is non-zero.
        """
        import polars as pl
        from backend.api.routes.holdings import _ROW_COLS, _build_holding_rows

        df = _make_raw_df(
            last_price=155.0,
            close_price=155.0,  # will be patched (differs from snap 144.0)
            average_price=140.0,
            pnl=750.0,
        )

        # snapshot: ref_close=144.0 (> 0.005 from close_price=155.0 → patched)
        df = _run_override(df, [("TEST001", "RELIANCE", 144.0)])

        assert "previous_close" in df.columns
        assert abs(df.iloc[0]["previous_close"] - 144.0) < 0.001

        # Verify polars path
        pl_df = pl.from_pandas(df)
        row_cols = [c for c in _ROW_COLS if c in pl_df.columns]
        assert "previous_close" in row_cols
        df_rows = pl_df.select(row_cols)

        rows = _build_holding_rows(df_rows, {})
        assert len(rows) == 1
        assert abs(rows[0].previous_close - 144.0) < 0.001, (
            f"HoldingRow.previous_close must be 144.0, got {rows[0].previous_close}"
        )


# ===========================================================================
# Requirement 2: previous_close written to ALL rows (not just patched ones)
# ===========================================================================

class TestPreviousCloseWrittenForAllRows:
    """_override_stale_close_for_holdings must set previous_close on every row
    that has a snapshot entry, independent of whether close_price is also patched.
    """

    def test_previous_close_set_on_row_within_epsilon(self):
        """Row whose close_price is within epsilon of snapshot gets previous_close
        set even though close_price is not patched.

        Row A: close_price=145.0, ref_close=145.003 → within epsilon (0.005),
               close_price unchanged BUT previous_close must be 145.003.
        Row B: close_price=1500.0, ref_close=1480.0 → > epsilon,
               both close_price and previous_close patched to 1480.0.
        """
        df = pd.concat([
            _make_raw_df(account="T1", symbol="REL",
                         last_price=152.0, close_price=145.0),
            _make_raw_df(account="T1", symbol="INFY",
                         last_price=1550.0, close_price=1500.0),
        ], ignore_index=True)

        df = _run_override(df, [
            ("T1", "REL", 145.003),   # within epsilon — no close_price patch
            ("T1", "INFY", 1480.0),   # > epsilon — close_price patched
        ])

        row_rel = df[df["tradingsymbol"] == "REL"].iloc[0]
        row_infy = df[df["tradingsymbol"] == "INFY"].iloc[0]

        # REL: close_price must NOT be patched (within epsilon)
        assert abs(row_rel["close_price"] - 145.0) < 0.001, (
            "close_price must not change when within epsilon"
        )
        # REL: but previous_close MUST be set (independent write)
        assert abs(row_rel["previous_close"] - 145.003) < 0.001, (
            f"previous_close must be written even when close_price is NOT patched, "
            f"got {row_rel['previous_close']}"
        )

        # INFY: both patched
        assert abs(row_infy["close_price"] - 1480.0) < 0.001
        assert abs(row_infy["previous_close"] - 1480.0) < 0.001

    def test_previous_close_zero_for_rows_with_no_snapshot(self):
        """Row with no snapshot entry → previous_close initialised to 0.0.
        The column must exist regardless (initialised unconditionally).
        """
        df = _make_raw_df(last_price=150.0, close_price=148.0)
        df = _run_override(df, [])  # empty snapshot_map

        assert "previous_close" in df.columns, (
            "previous_close column must be added even when snapshot_map is empty"
        )
        assert df.iloc[0]["previous_close"] == 0.0, (
            "Unmatched row must have previous_close=0.0, not NaN or missing"
        )

    def test_previous_close_uses_coalesce_value(self):
        """The Python layer stores the COALESCE'd ref_close directly.
        Simulated by passing a coalesced value (frozen previous_close from DB)
        as the third element of the 3-tuple — distinct from a raw ltp snapshot.
        """
        df = _make_raw_df(last_price=155.0, close_price=130.0)
        coalesced = 143.0   # frozen previous_close beats raw ltp (152.0)
        df = _run_override(df, [("TEST001", "RELIANCE", coalesced)])

        assert abs(df.iloc[0]["previous_close"] - coalesced) < 0.001, (
            f"previous_close must store the COALESCE'd value ({coalesced}), "
            f"got {df.iloc[0]['previous_close']}"
        )
        # close_price also patched (130 → 143, diff > epsilon)
        assert abs(df.iloc[0]["close_price"] - coalesced) < 0.001

    def test_sql_uses_ltp_not_coalesce(self):
        """DB query must use daily_book.ltp directly — NOT COALESCE(previous_close, ltp).

        The COALESCE was the bug: previous_close is populated from Kite's stale BHAV-copy API
        so COALESCE(previous_close, ltp) returns the stale value → epsilon check passes →
        no patching → wrong day P&L. Fix: daily_book.ltp as ref_close directly.
        """
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        captured_sql: list[str] = []

        mock_result = MagicMock()
        mock_result.all.return_value = []

        mock_session = AsyncMock()

        async def _capture(sql, *a, **kw):
            captured_sql.append(str(sql))
            return mock_result

        mock_session.execute = _capture
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        df = _make_raw_df()
        mock_time = datetime(2026, 8, 20, 9, 0, 0, tzinfo=_IST)

        with (
            patch("backend.api.database.async_session", return_value=mock_session),
            patch(
                "backend.shared.helpers.date_time_utils.timestamp_indian",
                return_value=mock_time,
            ),
        ):
            asyncio.run(_override_stale_close_for_holdings(df))

        assert len(captured_sql) == 1
        sql_lower = captured_sql[0].lower()
        assert "coalesce" not in sql_lower, (
            "SQL must NOT use COALESCE — previous_close is stale BHAV-copy; use daily_book.ltp directly"
        )
        assert "ltp" in sql_lower, "SQL must reference daily_book.ltp as ref_close"
        assert "holdings" in sql_lower, "SQL must filter on kind='holdings'"

    def test_previous_close_per_account_symbol(self):
        """Each (account, symbol) combination gets its own previous_close."""
        df = pd.concat([
            _make_raw_df(account="ACC1", symbol="HDFC",
                         last_price=200.0, close_price=200.0),
            _make_raw_df(account="ACC2", symbol="HDFC",
                         last_price=200.0, close_price=200.0),
        ], ignore_index=True)

        df = _run_override(df, [
            ("ACC1", "HDFC", 195.0),
            ("ACC2", "HDFC", 197.0),
        ])

        acc1 = df[df["account"] == "ACC1"].iloc[0]
        acc2 = df[df["account"] == "ACC2"].iloc[0]

        assert abs(acc1["previous_close"] - 195.0) < 0.001, (
            f"ACC1 previous_close should be 195.0, got {acc1['previous_close']}"
        )
        assert abs(acc2["previous_close"] - 197.0) < 0.001, (
            f"ACC2 previous_close should be 197.0, got {acc2['previous_close']}"
        )

    def test_previous_close_column_initialised_before_db_call(self):
        """Column must exist even when the DB query raises an exception
        (initialised at the top of the function, before the DB call).
        """
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        df = _make_raw_df()

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=RuntimeError("DB down"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_time = datetime(2026, 8, 20, 9, 0, 0, tzinfo=_IST)

        with (
            patch("backend.api.database.async_session", return_value=mock_session),
            patch(
                "backend.shared.helpers.date_time_utils.timestamp_indian",
                return_value=mock_time,
            ),
        ):
            asyncio.run(_override_stale_close_for_holdings(df))

        # Column exists even when DB failed — initialised unconditionally
        assert "previous_close" in df.columns, (
            "previous_close column must be initialised before DB call so it "
            "exists even on DB failure"
        )
        assert df.iloc[0]["previous_close"] == 0.0


# ===========================================================================
# Requirement 3: apply_day_change_backstop rescues day_change_val=0, pnl≠0
# ===========================================================================

class TestApplyDayChangeBackstopHoldings:
    """apply_day_change_backstop called from _fetch() must rescue holdings
    rows where the broker gate zeroed day_change_val but pnl is non-zero.
    """

    def test_case1_new_holding_recovers_day_change_val_from_pnl(self):
        """Case 1: overnight_quantity=0, day_change_val=0, pnl≠0 → dcv=pnl.

        A holding bought intraday has oq=0.  Kite returns day_change_val=0
        when LTP was zero at first tick.  pnl carries the correct value
        (Kite computed it on their side).  Backstop must copy pnl → dcv.
        """
        from backend.api.algo.pnl_math import apply_day_change_backstop

        df = pd.DataFrame([{
            "account": "TEST001",
            "tradingsymbol": "RELIANCE",
            "quantity": 10,
            "overnight_quantity": 0,
            "average_price": 140.0,
            "last_price": 155.0,
            "close_price": 0.0,
            "pnl": 150.0,
            "day_change_val": 0.0,
            "day_change_percentage": 0.0,
            "pnl_percentage": 0.0,
        }])

        result = apply_day_change_backstop(df)

        assert abs(result.iloc[0]["day_change_val"] - 150.0) < 0.001, (
            f"Case 1 backstop must set day_change_val=150.0 (=pnl), "
            f"got {result.iloc[0]['day_change_val']}"
        )

    def test_case1_not_triggered_when_dcv_already_valid(self):
        """Case 1 must NOT overwrite an already-correct non-zero day_change_val."""
        from backend.api.algo.pnl_math import apply_day_change_backstop

        df = pd.DataFrame([{
            "account": "TEST001",
            "tradingsymbol": "INFY",
            "quantity": 20,
            "overnight_quantity": 0,
            "average_price": 1500.0,
            "last_price": 1550.0,
            "close_price": 0.0,
            "pnl": 1000.0,
            "day_change_val": 1000.0,  # already correct
            "day_change_percentage": 0.0,
            "pnl_percentage": 0.0,
        }])

        result = apply_day_change_backstop(df)

        assert abs(result.iloc[0]["day_change_val"] - 1000.0) < 0.001, (
            "Backstop must NOT overwrite a valid non-zero day_change_val"
        )

    def test_case2_overnight_holding_recovers_via_formula(self):
        """Case 2: oq≠0, dcv=0, pnl≠0, close>0, avg>0 → strip overnight carry.

        Formula: day_pnl = pnl − (close − avg) × oq
        """
        from backend.api.algo.pnl_math import apply_day_change_backstop

        oq, avg, cls = 30, 100.0, 110.0
        pnl = (115.0 - avg) * oq        # 450: total gain from entry
        expected = pnl - (cls - avg) * oq  # 450 - 300 = 150: today only

        df = pd.DataFrame([{
            "account": "TEST001",
            "tradingsymbol": "SBIN",
            "quantity": oq,
            "overnight_quantity": oq,
            "average_price": avg,
            "last_price": 115.0,
            "close_price": cls,
            "pnl": pnl,
            "day_change_val": 0.0,
            "day_change_percentage": 0.0,
            "pnl_percentage": 0.0,
        }])

        result = apply_day_change_backstop(df)

        assert abs(result.iloc[0]["day_change_val"] - expected) < 0.001, (
            f"Case 2: day_change_val must be {expected} (today's only), "
            f"got {result.iloc[0]['day_change_val']}"
        )

    def test_backstop_returns_copy_not_inplace(self):
        """apply_day_change_backstop returns a copy — caller must capture it.

        This guards against the silent bug where the caller ignores the return
        value and the rescued day_change_val is lost.
        """
        from backend.api.algo.pnl_math import apply_day_change_backstop

        df = pd.DataFrame([{
            "quantity": 5,
            "overnight_quantity": 0,
            "average_price": 500.0,
            "last_price": 520.0,
            "close_price": 0.0,
            "pnl": 100.0,
            "day_change_val": 0.0,
        }])

        original_id = id(df)
        result = apply_day_change_backstop(df)

        assert id(result) != original_id, (
            "apply_day_change_backstop must return a new DataFrame copy, not mutate in-place. "
            "Caller in _fetch() must use: raw = apply_day_change_backstop(raw)"
        )
        # The copy has the rescued value
        assert abs(result.iloc[0]["day_change_val"] - 100.0) < 0.001

    def test_backstop_safe_on_empty_dataframe(self):
        """Empty DataFrame is returned unchanged without error."""
        from backend.api.algo.pnl_math import apply_day_change_backstop

        result = apply_day_change_backstop(pd.DataFrame())
        assert result is not None
        assert result.empty

    def test_backstop_case1_negative_pnl_recovered(self):
        """Case 1: negative pnl (intraday loss on new buy) is recovered."""
        from backend.api.algo.pnl_math import apply_day_change_backstop

        df = pd.DataFrame([{
            "quantity": 50,
            "overnight_quantity": 0,
            "average_price": 0.0,
            "last_price": 0.0,
            "close_price": 0.0,
            "pnl": -2000.0,
            "day_change_val": 0.0,
        }])

        result = apply_day_change_backstop(df)

        assert abs(result.iloc[0]["day_change_val"] - (-2000.0)) < 0.001, (
            "Case 1 with negative pnl must be recovered (loss case)"
        )

    def test_backstop_does_not_fire_when_pnl_zero(self):
        """Case 1 mask requires pnl≠0 — zero pnl must not trigger recovery."""
        from backend.api.algo.pnl_math import apply_day_change_backstop

        df = pd.DataFrame([{
            "quantity": 10,
            "overnight_quantity": 0,
            "average_price": 2500.0,
            "last_price": 2500.0,
            "close_price": 0.0,
            "pnl": 0.0,
            "day_change_val": 0.0,
        }])

        result = apply_day_change_backstop(df)

        assert result.iloc[0]["day_change_val"] == 0.0, (
            "No recovery when pnl=0 — no real session gain/loss to restore"
        )

    def test_fetch_calls_override_stale_close_for_holdings(self):
        """_fetch() must call _override_stale_close_for_holdings(raw) to patch
        stale close_price values with the prior-session EOD LTP from daily_book.

        The old apply_day_change_backstop inline call was replaced by
        _override_stale_close_for_holdings which combines close_price patching
        and day_change_val recomputation in a single async pass.
        """
        import inspect
        from backend.api.routes.holdings import _fetch

        source = inspect.getsource(_fetch)
        assert "_override_stale_close_for_holdings(raw)" in source, (
            "_fetch() must call _override_stale_close_for_holdings(raw) to patch "
            "stale close_price and recompute day_change_val against the canonical "
            "prior-session EOD LTP."
        )
