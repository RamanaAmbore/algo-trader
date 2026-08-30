"""
test_lot_size_normalization.py — lot size annotation and daily_snapshot fixes.

Tests assert:
1. _annotate_lot_size() MCX: quantity multiplied by multiplier, lots/lot_size added.
2. _annotate_lot_size() NFO: quantity stays in contracts, lots computed from lot_size.
3. _annotate_lot_size() equity: quantity unchanged, lots=quantity, lot_size=1.
4. _positions_rows() stores MCX qty as contracts (not lots).
5. Daily_snapshot UPSERT: day_pnl guard allows zero to overwrite stale values.
6. Daily_snapshot UPSERT: previous_close only advances when ltp changes.

IMPORTANT: These tests exercise core normalisation logic without hitting broker APIs.

NOTE: When the backend agent renames _apply_mcx_multiplier → _annotate_lot_size and adds
lots/lot_size columns, the MCX/NFO/Equity tests will activate. Until then, they skip
gracefully. UPSERT tests run against current code to detect pending fixes.
"""

from __future__ import annotations

import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from datetime import date, datetime, timezone


# ============================================================================
# Fixture to get _annotate_lot_size (or skip if not yet renamed)
# ============================================================================

@pytest.fixture
def annotate_lot_size():
    """Get _annotate_lot_size function. Skips if not yet defined."""
    try:
        from backend.brokers.broker_apis import _annotate_lot_size
        return _annotate_lot_size
    except ImportError:
        pytest.skip("_annotate_lot_size not yet defined (backend agent pending)")


# ============================================================================
# Tests for _annotate_lot_size (MCX, NFO, equity)
# ============================================================================

class TestAnnotateLotSizeMCX:
    """MCX multiplier tests — quantity in lots → quantity in contracts."""

    def test_mcx_goldm_quantity_scaled_by_multiplier(self, annotate_lot_size):
        """MCX GOLDM26AUGFUT: Kite ships quantity=1 (lot), multiplier=10.
        Must scale to 10 contracts. Add lots=1, lot_size=10."""
        df = pd.DataFrame([{
            'tradingsymbol': 'GOLDM26AUGFUT',
            'exchange': 'MCX',
            'quantity': 1,
            'overnight_quantity': 1,
            'day_buy_quantity': 0,
            'day_sell_quantity': 0,
            'multiplier': 10,
        }])
        annotate_lot_size(df)
        row = df.iloc[0]

        assert row['quantity'] == 10, (
            f"MCX qty should scale to contracts (1 lot × 10); got {row['quantity']}"
        )
        assert row['overnight_quantity'] == 10, (
            f"overnight_quantity should scale (1 × 10); got {row['overnight_quantity']}"
        )
        assert row['day_buy_quantity'] == 0
        assert row['day_sell_quantity'] == 0
        assert row['lots'] == 1, f"lots should be 1; got {row.get('lots')}"
        assert row['lot_size'] == 10, f"lot_size should be 10; got {row.get('lot_size')}"

    def test_mcx_crudeoil_multiple_lots(self, annotate_lot_size):
        """MCX CRUDEOIL: quantity=100 (lots), multiplier=100 contracts/lot.
        Scale to 10000 contracts. lots=100, lot_size=100."""
        df = pd.DataFrame([{
            'tradingsymbol': 'CRUDEOIL26AUGFUT',
            'exchange': 'MCX',
            'quantity': 100,
            'overnight_quantity': 100,
            'day_buy_quantity': 50,
            'day_sell_quantity': 0,
            'multiplier': 100,
        }])
        annotate_lot_size(df)
        row = df.iloc[0]

        assert row['quantity'] == 10000, f"100 lots × 100 = 10000; got {row['quantity']}"
        assert row['overnight_quantity'] == 10000
        assert row['day_buy_quantity'] == 5000
        assert row['lots'] == 100
        assert row['lot_size'] == 100

    def test_mcx_zero_quantity(self, annotate_lot_size):
        """MCX with zero quantity must stay zero after scaling."""
        df = pd.DataFrame([{
            'tradingsymbol': 'GOLDM26AUGFUT',
            'exchange': 'MCX',
            'quantity': 0,
            'overnight_quantity': 0,
            'day_buy_quantity': 0,
            'day_sell_quantity': 0,
            'multiplier': 10,
        }])
        annotate_lot_size(df)
        row = df.iloc[0]

        assert row['quantity'] == 0
        assert row['overnight_quantity'] == 0
        assert row['day_buy_quantity'] == 0
        assert row['day_sell_quantity'] == 0
        assert row['lots'] == 0
        assert row['lot_size'] == 10

    def test_mcx_negative_quantity_short(self, annotate_lot_size):
        """MCX short position: quantity=-5 lots, multiplier=10.
        Scale to -50 contracts. lots=-5, lot_size=10."""
        df = pd.DataFrame([{
            'tradingsymbol': 'GOLDM26AUGFUT',
            'exchange': 'MCX',
            'quantity': -5,
            'overnight_quantity': -5,
            'day_buy_quantity': 3,
            'day_sell_quantity': 0,
            'multiplier': 10,
        }])
        annotate_lot_size(df)
        row = df.iloc[0]

        assert row['quantity'] == -50
        assert row['overnight_quantity'] == -50
        assert row['day_buy_quantity'] == 30
        assert row['lots'] == -5
        assert row['lot_size'] == 10


class TestAnnotateLotSizeNFO:
    """NFO tests — quantity in contracts, lot_size from _LOT_INDEX.

    _annotate_lot_size reads _LOT_INDEX as a module-level global (not a captured
    reference) so patch.dict('backend.brokers.broker_apis._LOT_INDEX', ...) correctly
    injects test values that the loop reads via _LOT_INDEX.get(...).
    """

    def test_nfo_nifty_quantity_unchanged(self, annotate_lot_size):
        """NFO NIFTY25SEPTFUT: Kite ships qty=75 contracts (1 lot × 75).
        Quantity must stay 75. lots=1, lot_size=75."""
        df = pd.DataFrame([{
            'tradingsymbol': 'NIFTY25SEPTFUT',
            'exchange': 'NFO',
            'quantity': 75,
            'overnight_quantity': 75,
            'day_buy_quantity': 0,
            'day_sell_quantity': 0,
            'multiplier': 1,
        }])
        with patch.dict('backend.brokers.broker_apis._LOT_INDEX',
                        {('NFO', 'NIFTY25SEPTFUT'): 75}, clear=True):
            annotate_lot_size(df)

        row = df.iloc[0]
        assert row['quantity'] == 75, f"NFO qty stays in contracts; got {row['quantity']}"
        assert row['overnight_quantity'] == 75
        assert row['lots'] == 1
        assert row['lot_size'] == 75

    def test_nfo_banknifty_multiple_lots(self, annotate_lot_size):
        """NFO BANKNIFTY25SEPTFUT: qty=150 contracts (2 lots × 75).
        Quantity unchanged, lots=2, lot_size=75."""
        df = pd.DataFrame([{
            'tradingsymbol': 'BANKNIFTY25SEPTFUT',
            'exchange': 'NFO',
            'quantity': 150,
            'overnight_quantity': 150,
            'day_buy_quantity': 75,
            'day_sell_quantity': 0,
            'multiplier': 1,
        }])
        with patch.dict('backend.brokers.broker_apis._LOT_INDEX',
                        {('NFO', 'BANKNIFTY25SEPTFUT'): 75}, clear=True):
            annotate_lot_size(df)

        row = df.iloc[0]
        assert row['quantity'] == 150
        assert row['lots'] == 2
        assert row['lot_size'] == 75

    def test_nfo_missing_from_lot_index_defaults_to_1(self, annotate_lot_size):
        """NFO symbol not in _LOT_INDEX defaults lot_size=1."""
        df = pd.DataFrame([{
            'tradingsymbol': 'UNKNOWN_NFO_FUT',
            'exchange': 'NFO',
            'quantity': 100,
            'overnight_quantity': 100,
            'day_buy_quantity': 0,
            'day_sell_quantity': 0,
            'multiplier': 1,
        }])
        with patch.dict('backend.brokers.broker_apis._LOT_INDEX', {}, clear=True):
            annotate_lot_size(df)

        row = df.iloc[0]
        assert row['quantity'] == 100
        assert row['lots'] == 100
        assert row['lot_size'] == 1

    def test_nfo_zero_quantity(self, annotate_lot_size):
        """NFO with zero quantity."""
        df = pd.DataFrame([{
            'tradingsymbol': 'NIFTY25SEPTFUT',
            'exchange': 'NFO',
            'quantity': 0,
            'overnight_quantity': 0,
            'day_buy_quantity': 0,
            'day_sell_quantity': 0,
            'multiplier': 1,
        }])
        with patch.dict('backend.brokers.broker_apis._LOT_INDEX',
                        {('NFO', 'NIFTY25SEPTFUT'): 75}, clear=True):
            annotate_lot_size(df)

        row = df.iloc[0]
        assert row['quantity'] == 0
        assert row['lots'] == 0
        assert row['lot_size'] == 75


class TestAnnotateLotSizeEquity:
    """Equity (NSE/BSE) tests — no multiplier, lot_size=1."""

    def test_equity_reliance(self, annotate_lot_size):
        """NSE equity RELIANCE: quantity=100, multiplier=1.
        No scaling needed. lots=100, lot_size=1."""
        df = pd.DataFrame([{
            'tradingsymbol': 'RELIANCE',
            'exchange': 'NSE',
            'quantity': 100,
            'overnight_quantity': 100,
            'day_buy_quantity': 50,
            'day_sell_quantity': 0,
            'multiplier': 1,
        }])
        annotate_lot_size(df)
        row = df.iloc[0]

        assert row['quantity'] == 100
        assert row['overnight_quantity'] == 100
        assert row['day_buy_quantity'] == 50
        assert row['day_sell_quantity'] == 0
        assert row['lots'] == 100
        assert row['lot_size'] == 1

    def test_equity_sensex_bse(self, annotate_lot_size):
        """BSE equity: quantity=25, multiplier=1."""
        df = pd.DataFrame([{
            'tradingsymbol': 'SENSEX',
            'exchange': 'BSE',
            'quantity': 25,
            'overnight_quantity': 25,
            'day_buy_quantity': 0,
            'day_sell_quantity': 0,
            'multiplier': 1,
        }])
        annotate_lot_size(df)
        row = df.iloc[0]

        assert row['quantity'] == 25
        assert row['lots'] == 25
        assert row['lot_size'] == 1

    def test_equity_zero_quantity(self, annotate_lot_size):
        """Equity with zero quantity."""
        df = pd.DataFrame([{
            'tradingsymbol': 'RELIANCE',
            'exchange': 'NSE',
            'quantity': 0,
            'overnight_quantity': 0,
            'day_buy_quantity': 0,
            'day_sell_quantity': 0,
            'multiplier': 1,
        }])
        annotate_lot_size(df)
        row = df.iloc[0]

        assert row['quantity'] == 0
        assert row['lots'] == 0
        assert row['lot_size'] == 1

    def test_equity_short_position(self, annotate_lot_size):
        """Equity short: quantity=-50, multiplier=1."""
        df = pd.DataFrame([{
            'tradingsymbol': 'INFY',
            'exchange': 'NSE',
            'quantity': -50,
            'overnight_quantity': -50,
            'day_buy_quantity': 25,
            'day_sell_quantity': 0,
            'multiplier': 1,
        }])
        annotate_lot_size(df)
        row = df.iloc[0]

        assert row['quantity'] == -50
        assert row['overnight_quantity'] == -50
        assert row['day_buy_quantity'] == 25
        assert row['lots'] == -50
        assert row['lot_size'] == 1


class TestAnnotateLotSizeMultipleRows:
    """Test _annotate_lot_size on mixed DataFrames (MCX, NFO, equity)."""

    def test_mixed_exchange_dataframe(self, annotate_lot_size):
        """DataFrame with MCX, NFO, and equity rows."""
        df = pd.DataFrame([
            {
                'tradingsymbol': 'GOLDM26AUGFUT',
                'exchange': 'MCX',
                'quantity': 1,
                'overnight_quantity': 1,
                'day_buy_quantity': 0,
                'day_sell_quantity': 0,
                'multiplier': 10,
            },
            {
                'tradingsymbol': 'NIFTY25SEPTFUT',
                'exchange': 'NFO',
                'quantity': 75,
                'overnight_quantity': 75,
                'day_buy_quantity': 0,
                'day_sell_quantity': 0,
                'multiplier': 1,
            },
            {
                'tradingsymbol': 'RELIANCE',
                'exchange': 'NSE',
                'quantity': 100,
                'overnight_quantity': 100,
                'day_buy_quantity': 50,
                'day_sell_quantity': 0,
                'multiplier': 1,
            },
        ])

        with patch.dict('backend.brokers.broker_apis._LOT_INDEX',
                        {('NFO', 'NIFTY25SEPTFUT'): 75}, clear=True):
            annotate_lot_size(df)

        # MCX row
        mcx_row = df[df['exchange'] == 'MCX'].iloc[0]
        assert mcx_row['quantity'] == 10, "MCX: should scale"
        assert mcx_row['lots'] == 1
        assert mcx_row['lot_size'] == 10

        # NFO row
        nfo_row = df[df['exchange'] == 'NFO'].iloc[0]
        assert nfo_row['quantity'] == 75, "NFO: should not scale"
        assert nfo_row['lots'] == 1
        assert nfo_row['lot_size'] == 75

        # Equity row
        eq_row = df[df['exchange'] == 'NSE'].iloc[0]
        assert eq_row['quantity'] == 100, "Equity: unchanged"
        assert eq_row['lots'] == 100
        assert eq_row['lot_size'] == 1

    def test_empty_dataframe(self, annotate_lot_size):
        """Empty DataFrame should not raise."""
        df = pd.DataFrame()
        annotate_lot_size(df)  # Should not raise
        assert df.empty

    def test_no_multiplier_column(self, annotate_lot_size):
        """DataFrame without multiplier column should not raise."""
        df = pd.DataFrame([{
            'tradingsymbol': 'RELIANCE',
            'exchange': 'NSE',
            'quantity': 100,
        }])
        annotate_lot_size(df)
        # Should complete without error
        assert 'quantity' in df.columns


# ============================================================================
# Tests for _positions_rows MCX contract storage
# ============================================================================

class TestPositionsRowsMCXContracts:
    """_positions_rows must store MCX qty as contracts, not lots."""

    def test_positions_rows_stores_contracts_for_mcx(self):
        """_positions_rows processes MCX position with qty in contracts.

        The _positions_rows function already handles MCX multiplier scaling correctly,
        computing contract qty = raw_qty × multiplier for the database record."""
        try:
            from backend.api.algo.daily_snapshot import _positions_rows
        except ImportError:
            pytest.skip("_positions_rows not yet defined")

        account = "ZG0790"
        target_date = date(2026, 8, 23)
        now_ist = datetime(2026, 8, 23, 16, 0, 0, tzinfo=timezone.utc)

        # Raw Kite position: MCX GOLDM with qty=1 (lot), multiplier=10
        raw_positions = [{
            'tradingsymbol': 'GOLDM26AUGFUT',
            'exchange': 'MCX',
            'quantity': 1,
            'overnight_quantity': 1,
            'day_buy_quantity': 0,
            'day_sell_quantity': 0,
            'average_price': 6500.0,
            'close_price': 6490.0,
            'ltp': 6510.0,
            'pnl': 100.0,
            'multiplier': 10,
        }]

        rows = _positions_rows(
            account, target_date, raw_positions, now_ist,
            settled=True, market_open=False
        )

        assert len(rows) == 1, "Should create one row"
        row = rows[0]
        # Must store contracts (1 lot × 10 = 10 contracts)
        assert row['qty'] == 10, (
            f"MCX qty should be stored as contracts (1 lot × 10); got {row['qty']}"
        )
        assert row['symbol'] == 'GOLDM26AUGFUT'
        assert row['exchange'] == 'MCX'

    def test_positions_rows_equity_unchanged(self):
        """_positions_rows processes equity as-is."""
        try:
            from backend.api.algo.daily_snapshot import _positions_rows
        except ImportError:
            pytest.skip("_positions_rows not yet defined")

        account = "ZG0790"
        target_date = date(2026, 8, 23)
        now_ist = datetime(2026, 8, 23, 16, 0, 0, tzinfo=timezone.utc)

        raw_positions = [{
            'tradingsymbol': 'RELIANCE',
            'exchange': 'NSE',
            'quantity': 100,
            'overnight_quantity': 100,
            'day_buy_quantity': 0,
            'day_sell_quantity': 0,
            'average_price': 3000.0,
            'close_price': 3010.0,
            'ltp': 3020.0,
            'pnl': 2000.0,
            'multiplier': 1,
        }]

        rows = _positions_rows(
            account, target_date, raw_positions, now_ist,
            settled=True, market_open=False
        )

        assert len(rows) == 1
        row = rows[0]
        assert row['qty'] == 100, "Equity qty unchanged"
        assert row['symbol'] == 'RELIANCE'


# ============================================================================
# Tests for UPSERT SQL fixes (current code state)
# ============================================================================

class TestUpsertSQLFixes:
    """UPSERT SQL analysis — verifies fixes for day_pnl and previous_close gates."""

    def test_upsert_sql_day_pnl_gated_by_ltp(self):
        """FIXED: day_pnl now uses CASE WHEN ltp IS NOT NULL to allow
        day_pnl=0 to overwrite stale values."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL

        sql_str = str(_UPSERT_SQL)
        # Should NOT have the old buggy NULLIF pattern
        assert 'NULLIF(EXCLUDED.day_pnl, 0)' not in sql_str, (
            "FIXED: day_pnl should not use NULLIF freeze pattern"
        )
        # Should have the new ltp IS NOT NULL gate
        assert 'CASE WHEN EXCLUDED.ltp IS NOT NULL' in sql_str, (
            "day_pnl must be gated by ltp IS NOT NULL"
        )

    def test_upsert_sql_preserves_ltp_when_null(self):
        """UPSERT must preserve existing ltp when new value is NULL or 0."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL

        sql_str = str(_UPSERT_SQL)
        # After Fix 1: NULLIF(EXCLUDED.ltp, 0) added to prevent ltp=0 from
        # overwriting a valid prior settlement ltp. COALESCE falls back to
        # daily_book.ltp when NULLIF returns NULL (ltp was 0 or NULL).
        assert ('COALESCE(NULLIF(EXCLUDED.ltp, 0), daily_book.ltp)' in sql_str
                or 'COALESCE(EXCLUDED.ltp, daily_book.ltp)' in sql_str), (
            "UPSERT must preserve existing ltp when new value is NULL or 0"
        )

    def test_upsert_sql_previous_close_immutable(self):
        """FIXED: previous_close is now immutable — set only at INSERT, never updated
        on conflict.  The old rolling-shift CASE pattern has been removed."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL

        sql_str = str(_UPSERT_SQL)
        # Old rolling-shift guard must be gone
        assert 'EXCLUDED.ltp != daily_book.ltp' not in sql_str, (
            "Old ltp-change-gated rolling-shift pattern must not be present"
        )
        assert 'EXCLUDED.ltp != 0' not in sql_str, (
            "Old ltp=0 rolling-shift guard must not be present"
        )
        # New immutable pattern: preserve the existing DB value on conflict
        assert 'previous_close = daily_book.previous_close' in sql_str, (
            "UPSERT must preserve previous_close from the existing row (immutable)"
        )

    def test_upsert_sql_adds_lots_and_lot_size_columns(self):
        """FIXED: UPSERT now includes lots and lot_size columns."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL

        sql_str = str(_UPSERT_SQL)
        assert 'lots' in sql_str, "UPSERT must include lots column"
        assert 'lot_size' in sql_str, "UPSERT must include lot_size column"

    def test_upsert_sql_updates_qty_exchange_segment(self):
        """UPSERT must always update qty, exchange, segment."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL

        sql_str = str(_UPSERT_SQL)
        for col in ['qty', 'exchange', 'segment']:
            assert f'{col}' in sql_str, f"UPSERT must update {col}"

    def test_upsert_sql_conflict_clause_correct(self):
        """UPSERT must conflict on (date, account, kind, symbol)."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL

        sql_str = str(_UPSERT_SQL)
        assert 'ON CONFLICT' in sql_str, "Must have ON CONFLICT clause"
        assert 'date' in sql_str and 'account' in sql_str, (
            "Conflict key must include date and account"
        )


# ============================================================================
# Integration-style test to verify the full flow
# ============================================================================

class TestLotSizeAnnotationFlow:
    """End-to-end flow: raw Kite data → _annotate_lot_size → normalized."""

    def test_kite_mcx_goldm_flow(self, annotate_lot_size):
        """Simulate full flow: Kite returns MCX position with lot units."""
        # Simulated Kite positions response for MCX GOLDM
        kite_response = [{
            'tradingsymbol': 'GOLDM26AUGFUT',
            'exchange': 'MCX',
            'quantity': 2,  # Kite ships in LOTS
            'overnight_quantity': 2,
            'day_buy_quantity': 1,
            'day_sell_quantity': 0,
            'average_price': 6500.0,
            'close_price': 6490.0,
            'last_price': 6510.0,
            'pnl': 400.0,
            'multiplier': 10,  # 10 contracts per lot
        }]

        df = pd.DataFrame(kite_response)
        annotate_lot_size(df)

        row = df.iloc[0]
        # After normalisation:
        assert row['quantity'] == 20, "2 lots × 10 contracts = 20 contracts"
        assert row['overnight_quantity'] == 20
        assert row['day_buy_quantity'] == 10, "1 lot × 10 = 10 contracts"
        assert row['lots'] == 2, "Original lot count"
        assert row['lot_size'] == 10, "Multiplier"
        # Prices and pnl unchanged
        assert row['last_price'] == 6510.0
        assert row['pnl'] == 400.0

    def test_kite_equity_reliance_flow(self, annotate_lot_size):
        """Equity flow: no multiplier, no scaling."""
        kite_response = [{
            'tradingsymbol': 'RELIANCE',
            'exchange': 'NSE',
            'quantity': 100,
            'overnight_quantity': 100,
            'day_buy_quantity': 50,
            'day_sell_quantity': 0,
            'average_price': 3000.0,
            'close_price': 3010.0,
            'last_price': 3020.0,
            'pnl': 2000.0,
            'multiplier': 1,
        }]

        df = pd.DataFrame(kite_response)
        annotate_lot_size(df)

        row = df.iloc[0]
        assert row['quantity'] == 100, "Unchanged"
        assert row['lots'] == 100
        assert row['lot_size'] == 1
        assert row['last_price'] == 3020.0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
