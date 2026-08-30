"""
Tests for the three-layer ltp=0 corruption defense in daily_snapshot.py.

The defense operates at three levels:
1. UPSERT SQL NULLIF guard — prevents ltp=0 from overwriting good prior ltp
2. Holdings writer — neutralises ltp=0 for non-mid-session snapshots
3. Reader SQL filters — holdings and positions queries exclude zero-ltp rows
"""

import pytest
from datetime import date, datetime, timezone
from unittest.mock import patch, MagicMock
import json


class TestUpsertSqlNullIfGuard:
    """Verify UPSERT SQL contains NULLIF guard against ltp=0."""

    def test_upsert_sql_nullif_excludes_zero_ltp(self):
        """_UPSERT_SQL must contain NULLIF(EXCLUDED.ltp, 0) to filter zeros."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL
        sql_text = str(_UPSERT_SQL)
        assert "NULLIF" in sql_text, "_UPSERT_SQL must use NULLIF to filter zero ltp"
        assert "EXCLUDED.ltp" in sql_text, "_UPSERT_SQL must reference EXCLUDED.ltp in NULLIF"
        assert ", 0)" in sql_text, "_UPSERT_SQL NULLIF must check for 0"

    def test_upsert_sql_coalesce_fallback(self):
        """_UPSERT_SQL must COALESCE nullified ltp back to existing ltp."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL
        sql_text = str(_UPSERT_SQL)
        assert "COALESCE" in sql_text, "_UPSERT_SQL must COALESCE after NULLIF"

    def test_upsert_sql_excludes_ltp_zero_condition(self):
        """_UPSERT_SQL must have explicit condition to exclude ltp=0 updates."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL
        sql_text = str(_UPSERT_SQL)
        # Check for either explicit "!= 0" or "IS NOT NULL" guard
        assert ("!= 0" in sql_text or "IS NOT NULL" in sql_text), (
            "_UPSERT_SQL must explicitly exclude ltp=0 or check IS NOT NULL"
        )


class TestHoldingsWriterNeutralisesZeroLtpNonMidSession:
    """Holdings writer must fall back from ltp=0 to close_price when not mid-session."""

    def test_holdings_rows_zero_ltp_fallback_to_close(self):
        """_holdings_rows with ltp=0 and mid_session=False must fall back to close_price."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        raw = [{
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "average_price": 1800.0,
            "last_price": 0,  # Zero LTP — must use fallback
            "close_price": 1805.0,  # Fallback value
            "quantity": 10,
            "opening_quantity": 10,
            "pnl": 5000.0,  # Non-zero pnl prevents _is_zero_payload_row skip
            "day_change": 50.0,
        }]

        # Mock _is_exchange_open_at to return False (non-mid-session)
        with patch('backend.api.algo.daily_snapshot._is_exchange_open_at', return_value=False):
            rows = _holdings_rows(
                account="TEST",
                target_date=date.today(),
                raw=raw,
                now_ist=datetime.now(timezone.utc),
            )

        assert len(rows) == 1, "Should produce one row"
        # Verify ltp fell back to close_price
        assert rows[0]["ltp"] == 1805.0, (
            "ltp=0 must fall back to close_price for non-mid-session snapshot"
        )

    def test_holdings_rows_zero_ltp_mid_session_true(self):
        """_holdings_rows with ltp=0 and mid_session=True must return None (never fallback mid-session)."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        raw = [{
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "average_price": 1800.0,
            "last_price": 0,
            "close_price": 1805.0,
            "quantity": 10,
            "opening_quantity": 10,
            "pnl": 5000.0,
            "day_change": 50.0,
        }]

        # Mock to simulate mid-session (market open)
        with patch('backend.api.algo.daily_snapshot._is_exchange_open_at', return_value=True):
            rows = _holdings_rows(
                account="TEST",
                target_date=date.today(),
                raw=raw,
                now_ist=datetime.now(timezone.utc),
            )

        assert len(rows) == 1, "Should produce one row"
        # Mid-session always returns None for ltp (no fallback during market hours)
        assert rows[0]["ltp"] is None, "Mid-session must return None for ltp (no EOD staleness)"


class TestHoldingsWriterFallbackToClosePriceOnZeroLtp:
    """Holdings writer must fall back to close_price when ltp=0."""

    def test_snap_holding_eod_vals_fallback_to_close(self):
        """_snap_holding_eod_vals must fall back to close_price when ltp=0."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals

        r = {
            "last_price": 0,  # Zero — must use fallback
            "close_price": 1805.0,
            "day_change": 50.0,
            "pnl": 5000.0,
            "quantity": 10,
        }

        ltp_val, day_pnl_v, total_pnl_v = _snap_holding_eod_vals(r, mid_session=False)

        # Should fall back to close_price
        assert ltp_val == 1805.0, (
            "Must fall back to close_price when ltp=0"
        )

    def test_snap_holding_eod_vals_fallback_to_previous_close(self):
        """_snap_holding_eod_vals must fall back to previous_close when both ltp and close are 0."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals

        r = {
            "last_price": 0,
            "close_price": 0,  # Also zero
            "previous_close": 1800.0,  # Fallback
            "day_change": 50.0,
            "pnl": 5000.0,
            "quantity": 10,
        }

        ltp_val, day_pnl_v, total_pnl_v = _snap_holding_eod_vals(r, mid_session=False)

        # Should fall back to previous_close
        assert ltp_val == 1800.0, (
            "Must fall back to previous_close when both ltp and close are 0"
        )


class TestHoldingsReaderSqlHasRowLevelLtpFilter:
    """Holdings snapshot SQL must filter ltp=0 at row level."""

    def test_holdings_snapshot_sql_has_ltp_filter(self):
        """_HOLDINGS_SNAPSHOT_SQL must contain row-level ltp filters."""
        from backend.api.routes.holdings import _HOLDINGS_SNAPSHOT_SQL

        sql_text = _HOLDINGS_SNAPSHOT_SQL.lower()

        # Must have ltp > 0 check in WHERE clause
        assert "ltp > 0" in sql_text, (
            "_HOLDINGS_SNAPSHOT_SQL must filter ltp > 0 at row level"
        )

        # Check for the safety gate that allows NULL but excludes 0
        # Pattern: (ltp IS NULL OR ltp > 0)
        assert "ltp is not null" in sql_text or "ltp >" in sql_text, (
            "_HOLDINGS_SNAPSHOT_SQL must have ltp safety filter"
        )


class TestPositionsReaderSqlHasLtpFilter:
    """Positions snapshot SQL must filter ltp>0."""

    def test_positions_snapshot_sql_has_ltp_greater_than_zero(self):
        """Positions snapshot query must filter ltp > 0."""
        from backend.api.routes import positions
        import inspect

        # Get the _positions_snapshot function source
        src = inspect.getsource(positions._positions_snapshot)

        # Check for ltp > 0 filter in the SQL
        assert "ltp > 0" in src or "ltp IS NOT NULL" in src, (
            "Positions snapshot SQL must filter ltp > 0"
        )


class TestSnapPositionEodValsUnifiedPnlKite:
    """_snap_position_eod_vals must compute total_pnl correctly from pnl + realised."""

    def test_snap_position_eod_vals_with_pnl_and_realised(self):
        """Total P&L = pnl + realised (when both present)."""
        from backend.api.algo.daily_snapshot import _snap_position_eod_vals

        r = {
            "pnl": 5000.0,  # Unrealised
            "realised": 2000.0,  # From day-trade close
            "last_price": 100.0,
            "close_price": 90.0,
            "overnight_quantity": 10,
            "day_buy_quantity": 0,
            "day_sell_quantity": 0,
            "day_buy_value": 0,
            "day_sell_value": 0,
        }

        ltp_val, day_pnl, total_pnl_v, skip = _snap_position_eod_vals(
            r, mid_session=False, qty=10
        )

        # For the unified formula: total_pnl_v = pnl + realised = 5000 + 2000 = 7000
        assert total_pnl_v == 7000.0, (
            "total_pnl_v must be pnl + realised when both present"
        )
        assert skip is False, "Should not skip with valid pnl"


class TestSnapPositionEodValsNoneRealised:
    """_snap_position_eod_vals must handle None realised gracefully."""

    def test_snap_position_eod_vals_none_realised(self):
        """When realised=None, total_pnl_v = pnl."""
        from backend.api.algo.daily_snapshot import _snap_position_eod_vals

        r = {
            "pnl": 5000.0,
            "realised": None,  # Not provided by broker
            "last_price": 100.0,
            "close_price": 90.0,
            "overnight_quantity": 10,
            "day_buy_quantity": 0,
            "day_sell_quantity": 0,
            "day_buy_value": 0,
            "day_sell_value": 0,
        }

        ltp_val, day_pnl, total_pnl_v, skip = _snap_position_eod_vals(
            r, mid_session=False, qty=10
        )

        assert total_pnl_v == 5000.0, (
            "total_pnl_v must be pnl when realised is None"
        )


class TestSnapPositionEodValsBothNone:
    """_snap_position_eod_vals must handle both pnl and realised as None."""

    def test_snap_position_eod_vals_none_pnl_and_realised(self):
        """When both pnl and realised are None, total_pnl_v must be None."""
        from backend.api.algo.daily_snapshot import _snap_position_eod_vals

        r = {
            "pnl": None,
            "realised": None,
            "last_price": 100.0,
            "close_price": 90.0,
            "overnight_quantity": 10,
            "day_buy_quantity": 0,
            "day_sell_quantity": 0,
            "day_buy_value": 0,
            "day_sell_value": 0,
        }

        ltp_val, day_pnl, total_pnl_v, skip = _snap_position_eod_vals(
            r, mid_session=False, qty=10
        )

        assert total_pnl_v is None, (
            "total_pnl_v must be None when both pnl and realised are None"
        )


class TestHoldingsWriterNeutralisesZeroLtpNoFallback:
    """Holdings writer must neutralise ltp=0 to None when there is no close_price fallback."""

    def test_holdings_rows_zero_ltp_no_fallback_becomes_none(self):
        """_holdings_rows with ltp=0 and no close_price must set ltp=None (not skip row).

        Key invariant: the row is still written (captured_at updates for the account),
        but UPSERT NULLIF preserves the existing settlement ltp instead of overwriting
        with 0. This ensures NSE holdings appear in the 23:45 batch when MCX drives
        max_at to 23:45.
        """
        from backend.api.algo.daily_snapshot import _holdings_rows

        raw = [{
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "average_price": 1800.0,
            "last_price": 0,      # Zero LTP
            "close_price": 0,     # No fallback — both zero
            "previous_close": 0,  # No fallback
            "quantity": 10,
            "opening_quantity": 10,
            "pnl": 5000.0,        # Non-zero pnl prevents _is_zero_payload_row skip
            "day_change": 50.0,
        }]

        with patch('backend.api.algo.daily_snapshot._is_exchange_open_at', return_value=False):
            rows = _holdings_rows(
                account="TEST",
                target_date=date.today(),
                raw=raw,
                now_ist=datetime.now(timezone.utc),
            )

        # Row must not be skipped — it must appear with ltp=None
        assert len(rows) == 1, (
            "Row must NOT be skipped — setting ltp=None preserves the row in the batch"
        )
        assert rows[0]["ltp"] is None, (
            "ltp=0 with no fallback must be neutralised to None (not kept as 0)"
        )

    def test_holdings_rows_zero_ltp_mid_session_not_neutralised(self):
        """Mid-session rows with ltp=0 are NOT neutralised (mid_session gate returns None already)."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        raw = [{
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "average_price": 1800.0,
            "last_price": 0,
            "close_price": 0,
            "previous_close": 0,
            "quantity": 10,
            "opening_quantity": 10,
            "pnl": 5000.0,
            "day_change": 50.0,
        }]

        with patch('backend.api.algo.daily_snapshot._is_exchange_open_at', return_value=True):
            rows = _holdings_rows(
                account="TEST",
                target_date=date.today(),
                raw=raw,
                now_ist=datetime.now(timezone.utc),
            )

        # Mid-session: _snap_holding_eod_vals returns None for ltp (no neutralisation needed)
        assert len(rows) == 1, "Row must not be skipped mid-session"
        assert rows[0]["ltp"] is None, (
            "Mid-session row always has ltp=None from the gate — Fix 2 guard does not fire"
        )


class TestUpsertPreviousCloseGuard:
    """UPSERT SQL must not roll previous_close — it is now immutable."""

    def test_upsert_sql_previous_close_immutable_on_conflict(self):
        """previous_close is immutable: set at INSERT only, never updated on conflict.
        The old rolling-shift CASE (EXCLUDED.ltp != 0 guard) has been removed."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL
        sql_text = str(_UPSERT_SQL)
        # Old rolling-shift guards must be absent
        assert "EXCLUDED.ltp != 0" not in sql_text, (
            "Old ltp=0 rolling-shift guard must not be present — previous_close is now immutable"
        )
        assert "EXCLUDED.ltp != daily_book.ltp" not in sql_text, (
            "Old ltp-change gate must not be present — previous_close is now immutable"
        )
        # Immutable preserve pattern must be present
        assert "previous_close = daily_book.previous_close" in sql_text, (
            "_UPSERT_SQL must preserve previous_close from the existing row (immutable)"
        )


class TestHoldingsSnapshotSqlRowLevelLtpFilter:
    """_HOLDINGS_SNAPSHOT_SQL must filter ltp=0 at row level in final WHERE."""

    def test_final_where_allows_null_excludes_zero(self):
        """Final WHERE must use (ltp IS NULL OR ltp > 0) pattern."""
        from backend.api.routes.holdings import _HOLDINGS_SNAPSHOT_SQL
        sql_lower = _HOLDINGS_SNAPSHOT_SQL.lower()
        # Row-level filter: allows NULL (rows written before this fix), excludes 0
        assert "db.ltp is null or db.ltp > 0" in sql_lower, (
            "_HOLDINGS_SNAPSHOT_SQL final WHERE must contain 'db.ltp IS NULL OR db.ltp > 0'"
        )

    def test_latest_batch_cte_filters_zero_ltp(self):
        """latest_batch CTE must only consider rows with ltp > 0."""
        from backend.api.routes.holdings import _HOLDINGS_SNAPSHOT_SQL
        sql_lower = _HOLDINGS_SNAPSHOT_SQL.lower()
        assert "ltp is not null and ltp > 0" in sql_lower, (
            "_HOLDINGS_SNAPSHOT_SQL latest_batch CTE must require ltp IS NOT NULL AND ltp > 0"
        )


class TestPositionsSnapshotSqlRowLevelLtpFilter:
    """Positions snapshot SQL must filter ltp=0 at row level."""

    def test_positions_final_where_ltp_filter(self):
        """Positions snapshot final WHERE must filter ltp > 0 or allow NULL."""
        import inspect
        from backend.api.routes import positions
        src = inspect.getsource(positions._positions_snapshot)
        # Row-level filter added by Fix 5
        assert "db.ltp is null or db.ltp > 0" in src.lower() or \
               "(db.ltp IS NULL OR db.ltp > 0)" in src, (
            "Positions _positions_snapshot SQL must contain row-level ltp > 0 filter"
        )

    def test_positions_latest_batch_ltp_filter(self):
        """Positions latest_batch CTE must filter ltp > 0."""
        import inspect
        from backend.api.routes import positions
        src = inspect.getsource(positions._positions_snapshot)
        assert "ltp > 0" in src, (
            "Positions _positions_snapshot latest_batch must require ltp > 0"
        )
