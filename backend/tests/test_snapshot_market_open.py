"""
Tests for market_open flag in backend/api/algo/daily_snapshot.py

Covers:
  - _holdings_rows(market_open=False) forces EOD ltp capture during session hours
  - _holdings_rows(market_open=True) respects time-of-day mid_session check
  - _positions_rows(market_open=False) forces EOD day_pnl capture during session hours
  - _positions_rows(market_open=True) respects time-of-day mid_session check
  - snapshot_daily_book(market_open=False) propagates flag to row builders
"""

import asyncio
import json
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# StaticPool forces all checkout/checkin to reuse ONE connection — required for
# SQLite :memory: so that DDL (create_all) and subsequent queries share the
# same in-memory DB instead of each getting a fresh empty one.
_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_session():
    """Provide an in-memory SQLite session with only the daily_book table."""
    from sqlalchemy import MetaData, Table, Column, Integer, String, Text, UniqueConstraint
    from sqlalchemy import Date, DateTime, Numeric

    meta = MetaData()
    Table(
        "daily_book", meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("date", Date, nullable=False),
        Column("account", String(32), nullable=False),
        Column("segment", String(16), nullable=False),
        Column("kind", String(16), nullable=False),
        Column("symbol", String(64), nullable=False),
        Column("exchange", String(8), nullable=True),
        Column("qty", Integer, nullable=False, default=0),
        Column("lots", Integer, nullable=False, default=1),
        Column("lot_size", Integer, nullable=False, default=1),
        Column("avg_cost", Numeric, nullable=True),
        Column("ltp", Numeric, nullable=True),
        Column("day_pnl", Numeric, nullable=True),
        Column("total_pnl", Numeric, nullable=True),
        Column("previous_close", Numeric, nullable=True),
        Column("payload_json", Text, nullable=True),
        Column("captured_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("date", "account", "kind", "symbol",
                         name="uq_daily_book_day_acct_kind_sym"),
    )

    engine = create_async_engine(
        _TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


# ---------------------------------------------------------------------------
# Fixtures: IST timezone and timestamps
# ---------------------------------------------------------------------------

from datetime import timedelta

# Use IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

_D = date(2026, 8, 15)  # Arbitrary trading date


@pytest.fixture
def now_10am_ist():
    """10:00 AM IST on trading day (mid-NSE-session)."""
    return datetime(2026, 8, 15, 10, 0, 0)


@pytest.fixture
def now_11am_ist():
    """11:00 AM IST on trading day (mid-session)."""
    return datetime(2026, 8, 15, 11, 0, 0)


@pytest.fixture
def now_1535_ist():
    """15:35 IST (after NSE close, mid-MCX session)."""
    return datetime(2026, 8, 15, 15, 35, 0)


@pytest.fixture
def now_2335_ist():
    """23:35 IST (after both NSE and MCX close)."""
    return datetime(2026, 8, 15, 23, 35, 0)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

_HOLDING_INFY = {
    "tradingsymbol": "INFY",
    "exchange": "NSE",
    "opening_quantity": 10,
    "average_price": 1500.0,
    "last_price": 1560.0,
    "day_change": 60.0,
    "close_price": 1500.0,
    "pnl": 600.0,
}

_POSITION_NIFTY_FUT = {
    "tradingsymbol": "NIFTY25AUGFUT",
    "exchange": "NFO",
    "quantity": 50,
    "average_price": 25000.0,
    "last_price": 25100.0,
    "close_price": 25050.0,
    "pnl": 2500.0,
    "overnight_quantity": 50,
    "day_buy_quantity": 0,
    "day_sell_quantity": 0,
    "day_buy_value": 0.0,
    "day_sell_value": 0.0,
}

_POSITION_MCX_CRUDEOIL = {
    "tradingsymbol": "CRUDEOIL26AUGFUT",
    "exchange": "MCX",
    "quantity": 100,  # 1 lot × 100 multiplier
    "average_price": 6800.0,
    "last_price": 6900.0,
    "close_price": 6800.0,
    "pnl": 10_000.0,
    "multiplier": 100,
    "overnight_quantity": 1,  # in lots
    "day_buy_quantity": 0,
    "day_sell_quantity": 0,
    "day_buy_value": 0.0,
    "day_sell_value": 0.0,
}


# ---------------------------------------------------------------------------
# Tests: _holdings_rows with market_open flag
# ---------------------------------------------------------------------------

class TestHoldingsRowsMarketOpen:
    """Test _holdings_rows respects market_open flag."""

    def test_holdings_market_open_false_forces_eod_ltp(self, now_10am_ist):
        """market_open=False + 10:00 IST → ltp is captured (not None)."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        rows = _holdings_rows(
            "ZG0790", _D, [_HOLDING_INFY], now_10am_ist,
            market_open=False  # Force EOD mode
        )
        assert len(rows) == 1
        assert rows[0]["ltp"] == 1560.0, \
            f"Expected ltp=1560.0 with market_open=False at 10:00 IST, got {rows[0]['ltp']}"

    def test_holdings_market_open_true_mid_session_emits_null_ltp(self, now_10am_ist):
        """market_open=True (default) + 10:00 IST → ltp is None (mid-session)."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        rows = _holdings_rows(
            "ZG0790", _D, [_HOLDING_INFY], now_10am_ist,
            market_open=True  # Respect time-of-day
        )
        assert len(rows) == 1
        assert rows[0]["ltp"] is None, \
            f"Expected ltp=None with market_open=True at 10:00 IST (mid-session), got {rows[0]['ltp']}"

    def test_holdings_market_open_false_forces_eod_day_pnl(self, now_11am_ist):
        """market_open=False + 11:00 IST → day_pnl is captured (not None)."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        rows = _holdings_rows(
            "ZG0790", _D, [_HOLDING_INFY], now_11am_ist,
            market_open=False
        )
        assert len(rows) == 1
        expected_day_pnl = 60.0 * 10  # day_change × qty
        assert rows[0]["day_pnl"] == pytest.approx(expected_day_pnl), \
            f"Expected day_pnl={expected_day_pnl} with market_open=False, got {rows[0]['day_pnl']}"

    def test_holdings_market_open_true_mid_session_emits_null_day_pnl(self, now_11am_ist):
        """market_open=True + 11:00 IST → day_pnl is None (mid-session)."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        rows = _holdings_rows(
            "ZG0790", _D, [_HOLDING_INFY], now_11am_ist,
            market_open=True
        )
        assert len(rows) == 1
        assert rows[0]["day_pnl"] is None, \
            f"Expected day_pnl=None with market_open=True at 11:00 IST (mid-session), got {rows[0]['day_pnl']}"

    def test_holdings_market_open_false_eod_time_unchanged(self, now_2335_ist):
        """market_open=False after EOD time → no change (ltp already captured)."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        rows = _holdings_rows(
            "ZG0790", _D, [_HOLDING_INFY], now_2335_ist,
            market_open=False
        )
        assert len(rows) == 1
        assert rows[0]["ltp"] == 1560.0

    def test_holdings_market_open_true_eod_time_captures_ltp(self, now_2335_ist):
        """market_open=True after EOD (23:35) → ltp captured (not mid-session)."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        rows = _holdings_rows(
            "ZG0790", _D, [_HOLDING_INFY], now_2335_ist,
            market_open=True
        )
        assert len(rows) == 1
        assert rows[0]["ltp"] == 1560.0, \
            f"Expected ltp=1560.0 at 23:35 (post-close), got {rows[0]['ltp']}"

    def test_holdings_market_open_default_is_true(self, now_10am_ist):
        """market_open defaults to True → time-of-day check applies."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        # Omit market_open — should default to True
        rows = _holdings_rows("ZG0790", _D, [_HOLDING_INFY], now_10am_ist)
        assert len(rows) == 1
        assert rows[0]["ltp"] is None, \
            f"Expected ltp=None at 10:00 IST with market_open=True (default), got {rows[0]['ltp']}"


# ---------------------------------------------------------------------------
# Tests: _positions_rows with market_open flag
# ---------------------------------------------------------------------------

class TestPositionsRowsMarketOpen:
    """Test _positions_rows respects market_open flag."""

    def test_positions_market_open_false_forces_eod_ltp(self, now_10am_ist):
        """market_open=False + 10:00 IST → ltp is captured for NFO."""
        from backend.api.algo.daily_snapshot import _positions_rows

        rows = _positions_rows(
            "ZG0790", _D, [_POSITION_NIFTY_FUT], now_10am_ist,
            market_open=False
        )
        assert len(rows) == 1
        assert rows[0]["ltp"] == 25100.0, \
            f"Expected ltp=25100.0 with market_open=False at 10:00 IST, got {rows[0]['ltp']}"

    def test_positions_market_open_true_mid_session_emits_null_ltp(self, now_10am_ist):
        """market_open=True + 10:00 IST → ltp is None (mid-session for NFO)."""
        from backend.api.algo.daily_snapshot import _positions_rows

        rows = _positions_rows(
            "ZG0790", _D, [_POSITION_NIFTY_FUT], now_10am_ist,
            market_open=True
        )
        assert len(rows) == 1
        assert rows[0]["ltp"] is None, \
            f"Expected ltp=None with market_open=True at 10:00 IST (mid-session), got {rows[0]['ltp']}"

    def test_positions_market_open_false_forces_eod_day_pnl(self, now_10am_ist):
        """market_open=False + 10:00 IST → day_pnl is captured."""
        from backend.api.algo.daily_snapshot import _positions_rows

        rows = _positions_rows(
            "ZG0790", _D, [_POSITION_NIFTY_FUT], now_10am_ist,
            market_open=False
        )
        assert len(rows) == 1
        # day_pnl = (ltp - close) × qty = (25100 - 25050) × 50 = 2500
        expected_day_pnl = 2500.0
        assert rows[0]["day_pnl"] == pytest.approx(expected_day_pnl), \
            f"Expected day_pnl={expected_day_pnl}, got {rows[0]['day_pnl']}"

    def test_positions_market_open_true_mid_session_emits_null_day_pnl(self, now_10am_ist):
        """market_open=True + 10:00 IST → day_pnl is None (mid-session)."""
        from backend.api.algo.daily_snapshot import _positions_rows

        rows = _positions_rows(
            "ZG0790", _D, [_POSITION_NIFTY_FUT], now_10am_ist,
            market_open=True
        )
        assert len(rows) == 1
        assert rows[0]["day_pnl"] is None, \
            f"Expected day_pnl=None with market_open=True at 10:00 IST (mid-session), got {rows[0]['day_pnl']}"

    def test_positions_mcx_market_open_false_at_1535(self, now_1535_ist):
        """MCX mid-session: market_open=False forces EOD at 15:35 IST."""
        from backend.api.algo.daily_snapshot import _positions_rows

        # At 15:35 IST, MCX is still open (09:00-23:30)
        rows = _positions_rows(
            "ZG0790", _D, [_POSITION_MCX_CRUDEOIL], now_1535_ist,
            market_open=False
        )
        assert len(rows) == 1
        assert rows[0]["ltp"] == 6900.0, \
            f"Expected ltp=6900 with market_open=False at 15:35 (MCX still open), got {rows[0]['ltp']}"

    def test_positions_mcx_market_open_true_at_1535(self, now_1535_ist):
        """MCX at 15:35 (mid-session): market_open=True → ltp=None."""
        from backend.api.algo.daily_snapshot import _positions_rows

        rows = _positions_rows(
            "ZG0790", _D, [_POSITION_MCX_CRUDEOIL], now_1535_ist,
            market_open=True
        )
        assert len(rows) == 1
        assert rows[0]["ltp"] is None, \
            f"Expected ltp=None at 15:35 (MCX mid-session), got {rows[0]['ltp']}"

    def test_positions_market_open_default_is_true(self, now_10am_ist):
        """market_open defaults to True → time-of-day check applies."""
        from backend.api.algo.daily_snapshot import _positions_rows

        rows = _positions_rows("ZG0790", _D, [_POSITION_NIFTY_FUT], now_10am_ist)
        assert len(rows) == 1
        assert rows[0]["ltp"] is None, \
            f"Expected ltp=None at 10:00 IST with market_open=True (default), got {rows[0]['ltp']}"


# ---------------------------------------------------------------------------
# Tests: snapshot_daily_book with market_open flag
# ---------------------------------------------------------------------------

class TestSnapshotDailyBookMarketOpen:
    """Test snapshot_daily_book propagates market_open to row builders."""

    def _make_patch_upsert(self, session):
        """Return an async replacement for _upsert_rows that uses `session`."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL

        async def _patched_upsert(rows):
            if not rows:
                return 0
            now_utc = datetime.now(timezone.utc)
            for r in rows:
                r["captured_at"] = now_utc
            await session.execute(_UPSERT_SQL, rows)
            await session.commit()
            return len(rows)

        return _patched_upsert

    def _make_broker_mock(self, holdings=None, positions=None, trades=None):
        """Return a mock broker with account attribute."""
        broker = MagicMock()
        broker.account = "ZG0790"
        broker.holdings.return_value = holdings or []
        broker.positions.return_value = {"net": positions or []}
        broker.trades.return_value = trades or []
        broker.margins.return_value = {
            "equity": {
                "available": {"cash": 100000.0, "opening_balance": 100000.0},
                "utilised": {"debits": 10000, "realised_m2m": 500.0},
                "net": 90000.0,
            }
        }
        return broker

    @pytest.mark.asyncio
    async def test_snapshot_market_open_false_propagates(self, db_session, now_10am_ist):
        """snapshot_daily_book(market_open=False) passes flag to _holdings_rows + _positions_rows."""
        from backend.api.algo import daily_snapshot as ds
        from backend.brokers import registry

        broker_mock = self._make_broker_mock(
            holdings=[_HOLDING_INFY],
            positions=[_POSITION_NIFTY_FUT],
            trades=[]
        )

        with patch.object(ds, "_upsert_rows", self._make_patch_upsert(db_session)):
            with patch.object(ds, "_get_connections", return_value=MagicMock(conn={"ZG0790": MagicMock()})):
                with patch("backend.api.algo.daily_snapshot.timestamp_indian", return_value=now_10am_ist):
                    with patch.object(registry, "all_brokers", return_value=[broker_mock]):
                        result = await ds.snapshot_daily_book(
                            target_date=_D,
                            market_open=False
                        )

        # With market_open=False, holdings and positions should have non-None ltp/day_pnl
        assert result["holdings_rows"] == 1, f"Expected 1 holdings row, got {result['holdings_rows']}"
        assert result["positions_rows"] == 1, f"Expected 1 positions row, got {result['positions_rows']}"

        # Query DB to verify ltp was captured
        holdings_row = (await db_session.execute(
            text("SELECT ltp FROM daily_book WHERE kind='holdings' AND symbol='INFY'")
        )).fetchone()
        assert holdings_row is not None, "Holdings INFY row should exist"
        assert float(holdings_row[0]) == pytest.approx(1560.0), \
            f"Expected holdings ltp=1560.0, got {holdings_row[0]}"

        positions_row = (await db_session.execute(
            text("SELECT ltp FROM daily_book WHERE kind='positions' AND symbol='NIFTY25AUGFUT'")
        )).fetchone()
        assert positions_row is not None, "Positions NIFTY25AUGFUT row should exist"
        assert float(positions_row[0]) == pytest.approx(25100.0), \
            f"Expected positions ltp=25100.0, got {positions_row[0]}"

    @pytest.mark.asyncio
    async def test_snapshot_market_open_true_respects_time_of_day(self, db_session, now_10am_ist):
        """snapshot_daily_book(market_open=True) at 10:00 IST → ltp=None (mid-session)."""
        from backend.api.algo import daily_snapshot as ds
        from backend.brokers import registry

        broker_mock = self._make_broker_mock(
            holdings=[_HOLDING_INFY],
            positions=[_POSITION_NIFTY_FUT],
            trades=[]
        )

        with patch.object(ds, "_upsert_rows", self._make_patch_upsert(db_session)):
            with patch.object(ds, "_get_connections", return_value=MagicMock(conn={"ZG0790": MagicMock()})):
                with patch("backend.api.algo.daily_snapshot.timestamp_indian", return_value=now_10am_ist):
                    with patch.object(registry, "all_brokers", return_value=[broker_mock]):
                        result = await ds.snapshot_daily_book(
                            target_date=_D,
                            market_open=True  # Default
                        )

        assert result["holdings_rows"] == 1
        assert result["positions_rows"] == 1

        # At 10:00 IST (mid-session), ltp should be None
        holdings_row = (await db_session.execute(
            text("SELECT ltp FROM daily_book WHERE kind='holdings' AND symbol='INFY'")
        )).fetchone()
        assert holdings_row is not None
        assert holdings_row[0] is None, \
            f"Expected holdings ltp=None at 10:00 IST (mid-session), got {holdings_row[0]}"

    @pytest.mark.asyncio
    async def test_snapshot_market_open_default_is_true(self, db_session, now_10am_ist):
        """snapshot_daily_book() without market_open defaults to True."""
        from backend.api.algo import daily_snapshot as ds
        from backend.brokers import registry

        broker_mock = self._make_broker_mock(
            holdings=[_HOLDING_INFY],
            positions=[],
            trades=[]
        )

        with patch.object(ds, "_upsert_rows", self._make_patch_upsert(db_session)):
            with patch.object(ds, "_get_connections", return_value=MagicMock(conn={"ZG0790": MagicMock()})):
                with patch("backend.api.algo.daily_snapshot.timestamp_indian", return_value=now_10am_ist):
                    with patch.object(registry, "all_brokers", return_value=[broker_mock]):
                        # Omit market_open
                        result = await ds.snapshot_daily_book(target_date=_D)

        assert result["holdings_rows"] == 1

        # At 10:00 IST (mid-session), ltp should be None (default market_open=True)
        holdings_row = (await db_session.execute(
            text("SELECT ltp FROM daily_book WHERE kind='holdings' AND symbol='INFY'")
        )).fetchone()
        assert holdings_row is not None
        assert holdings_row[0] is None, \
            f"Expected holdings ltp=None at 10:00 IST with market_open=True (default), got {holdings_row[0]}"


# ---------------------------------------------------------------------------
# Integration test: holiday startup scenario
# ---------------------------------------------------------------------------

class TestHolidayStartupScenario:
    """Simulate the holiday-startup use case."""

    def test_holdings_holiday_startup_captures_prices(self, now_10am_ist):
        """Holiday at 10:00 IST: market_open=False ensures prices are captured."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        holdings = [
            _HOLDING_INFY,
            {
                "tradingsymbol": "TCS",
                "exchange": "NSE",
                "opening_quantity": 5,
                "average_price": 3400.0,
                "last_price": 3450.0,
                "day_change": 50.0,
                "close_price": 3400.0,
                "pnl": 250.0,
            }
        ]

        rows = _holdings_rows(
            "ZG0790", _D, holdings, now_10am_ist,
            market_open=False  # Holiday flag
        )

        assert len(rows) == 2
        # Both should have ltp captured
        assert rows[0]["ltp"] == 1560.0
        assert rows[1]["ltp"] == 3450.0
        # Both should have day_pnl
        assert rows[0]["day_pnl"] is not None
        assert rows[1]["day_pnl"] is not None

    def test_positions_holiday_startup_captures_mcx_prices(self, now_10am_ist):
        """Holiday at 10:00 IST: market_open=False ensures MCX prices captured."""
        from backend.api.algo.daily_snapshot import _positions_rows

        positions = [
            _POSITION_MCX_CRUDEOIL,
            {
                "tradingsymbol": "NATURALGAS26AUGFUT",
                "exchange": "MCX",
                "quantity": 10,
                "average_price": 180.0,
                "last_price": 185.0,
                "close_price": 182.0,
                "pnl": 30.0,
                "multiplier": 1,
                "overnight_quantity": 10,
                "day_buy_quantity": 0,
                "day_sell_quantity": 0,
                "day_buy_value": 0.0,
                "day_sell_value": 0.0,
            }
        ]

        rows = _positions_rows(
            "ZG0790", _D, positions, now_10am_ist,
            market_open=False  # Holiday flag
        )

        assert len(rows) == 2
        # Both MCX positions should have ltp despite 10:00 IST being mid-session normally
        assert rows[0]["ltp"] == 6900.0
        assert rows[1]["ltp"] == 185.0
        # Both should have day_pnl
        assert rows[0]["day_pnl"] is not None
        assert rows[1]["day_pnl"] is not None


# ---------------------------------------------------------------------------
# Edge cases and guards
# ---------------------------------------------------------------------------

class TestMarketOpenEdgeCases:
    """Edge cases with market_open flag."""

    def test_holdings_with_zero_payload_guard_still_applies(self, now_10am_ist):
        """market_open=False doesn't bypass bad-payload guard (ltp=0 + day_pnl=0 + total_pnl=0)."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        # Malformed row: avg_cost > 0 but all prices are zero (bad payload fingerprint)
        bad_holding = {
            "tradingsymbol": "BADROW",
            "exchange": "NSE",
            "opening_quantity": 10,
            "average_price": 1500.0,  # Has cost basis
            "last_price": 0.0,  # Zero ltp
            "day_change": 0.0,  # Zero day_change
            "close_price": 0.0,
            "pnl": 0.0,  # Zero total_pnl (fingerprint of bad payload)
        }

        rows = _holdings_rows(
            "ZG0790", _D, [bad_holding], now_10am_ist,
            market_open=False  # Even with market_open=False
        )

        # Row should still be filtered (bad payload guard)
        assert len(rows) == 0, \
            "Bad-payload rows should be filtered even with market_open=False"

    def test_positions_empty_list_market_open_false(self, now_10am_ist):
        """Empty positions list with market_open=False."""
        from backend.api.algo.daily_snapshot import _positions_rows

        rows = _positions_rows(
            "ZG0790", _D, [], now_10am_ist,
            market_open=False
        )

        assert len(rows) == 0, "Empty input should produce empty output"

    def test_holdings_mcx_payload_rejected_at_eod_with_market_open_false(self, now_10am_ist):
        """Holdings are equity-only; MCX exchange should not appear in holdings."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        mcx_holding = {
            "tradingsymbol": "CRUDEOIL26AUGFUT",
            "exchange": "MCX",
            "opening_quantity": 1,
            "average_price": 6800.0,
            "last_price": 6900.0,
            "day_change": 100.0,
            "close_price": 6800.0,
            "pnl": 100.0,
        }

        rows = _holdings_rows(
            "ZG0790", _D, [mcx_holding], now_10am_ist,
            market_open=False
        )

        # Builders don't filter by exchange — they just pass through
        # The ltp should be captured due to market_open=False
        assert len(rows) == 1
        assert rows[0]["exchange"] == "MCX"
        assert rows[0]["ltp"] == 6900.0


# ---------------------------------------------------------------------------
# Payload integrity test
# ---------------------------------------------------------------------------

class TestMarketOpenPayloadIntegrity:
    """Verify snapshot_extras payload is correctly built with market_open flag."""

    def test_holdings_payload_has_correct_ltp_when_market_open_false(self, now_10am_ist):
        """When market_open=False, payload snapshot_extras.ltp should be non-None."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        rows = _holdings_rows(
            "ZG0790", _D, [_HOLDING_INFY], now_10am_ist,
            market_open=False
        )

        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert "snapshot_extras" in payload
        assert payload["snapshot_extras"]["ltp"] == pytest.approx(1560.0), \
            f"Expected snapshot_extras.ltp=1560.0, got {payload['snapshot_extras']['ltp']}"

    def test_holdings_payload_has_null_ltp_when_market_open_true_midsession(self, now_10am_ist):
        """When market_open=True + mid-session, payload snapshot_extras.ltp should be None."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        rows = _holdings_rows(
            "ZG0790", _D, [_HOLDING_INFY], now_10am_ist,
            market_open=True
        )

        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert "snapshot_extras" in payload
        assert payload["snapshot_extras"]["ltp"] is None, \
            f"Expected snapshot_extras.ltp=None at mid-session, got {payload['snapshot_extras']['ltp']}"

    def test_positions_payload_preserves_settled_flag(self, now_10am_ist):
        """market_open=False doesn't affect settled flag in payload."""
        from backend.api.algo.daily_snapshot import _positions_rows

        rows = _positions_rows(
            "ZG0790", _D, [_POSITION_NIFTY_FUT], now_10am_ist,
            settled=True,
            market_open=False
        )

        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert "snapshot_extras" in payload
        assert payload["snapshot_extras"]["settled"] is True


# ---------------------------------------------------------------------------
# Test 2: _snap_holding_eod_vals close_price fallback when last_price=0
# ---------------------------------------------------------------------------

class TestSnapHoldingEodValsClosePriceFallback:
    """Verify _snap_holding_eod_vals uses close_price when last_price=0."""

    def test_ltp_zero_closes_price_nonzero_uses_close_price(self):
        """When last_price=0 but close_price > 0, should fallback to close_price for ltp_val."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals

        row = {
            "last_price": 0,  # Zero ltp (Dhan returns 0 when cache cold)
            "close_price": 1500.0,  # Non-zero close
            "day_change": 60.0,
            "pnl": 300.0,
            "opening_quantity": 10,
        }

        ltp_val, day_pnl_v, total_pnl_v = _snap_holding_eod_vals(row, mid_session=False)

        # With close_price fallback fix, should use close_price when last_price=0
        assert ltp_val == pytest.approx(1500.0), (
            f"When last_price=0 but close_price=1500.0, ltp_val should fallback to close_price, "
            f"got {ltp_val}"
        )

    def test_ltp_zero_close_price_zero_returns_zero(self):
        """When both last_price=0 and close_price=0, ltp_val should be 0.0."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals

        row = {
            "last_price": 0,
            "close_price": 0,
            "day_change": None,
            "pnl": 0.0,
            "opening_quantity": 10,
        }

        ltp_val, day_pnl_v, total_pnl_v = _snap_holding_eod_vals(row, mid_session=False)

        # With both prices zero, will fallback through: last_price=0 → close_price=0 → 0.0
        assert ltp_val == 0.0, (
            f"When both last_price=0 and close_price=0, ltp_val should be 0.0, got {ltp_val}"
        )

    def test_snap_holding_eod_vals_mid_session_suppresses_prices(self):
        """mid_session=True suppresses ltp/day_pnl regardless of prices."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals

        row = {
            "last_price": 1560.0,
            "close_price": 1500.0,
            "day_change": 60.0,
            "pnl": 600.0,
            "opening_quantity": 10,
        }

        ltp_val, day_pnl_v, total_pnl_v = _snap_holding_eod_vals(row, mid_session=True)

        # Both should be None when mid_session=True
        assert ltp_val is None, "ltp_val must be None during mid-session"
        assert day_pnl_v is None, "day_pnl_v must be None during mid-session"
        # total_pnl_v is always captured (not suppressed)
        assert total_pnl_v == 600.0

    def test_snap_holding_eod_vals_computes_day_pnl_from_day_change(self):
        """day_pnl_v = day_change × qty when mid_session=False."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals

        row = {
            "last_price": 1560.0,
            "close_price": 1500.0,
            "day_change": 60.0,
            "pnl": 600.0,
            "opening_quantity": 10,
        }

        ltp_val, day_pnl_v, total_pnl_v = _snap_holding_eod_vals(row, mid_session=False)

        expected_day_pnl = 60.0 * 10  # 600.0
        assert day_pnl_v == pytest.approx(expected_day_pnl), (
            f"day_pnl_v should be day_change × qty = {expected_day_pnl}, got {day_pnl_v}"
        )

    def test_snap_holding_eod_vals_missing_day_change_returns_none(self):
        """When day_change is missing/None, day_pnl_v is None."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals

        row = {
            "last_price": 1560.0,
            "close_price": 1500.0,
            "day_change": None,  # Missing
            "pnl": 600.0,
            "opening_quantity": 10,
        }

        ltp_val, day_pnl_v, total_pnl_v = _snap_holding_eod_vals(row, mid_session=False)

        assert day_pnl_v is None, "day_pnl_v should be None when day_change is None"


# ---------------------------------------------------------------------------
# Test 3: _snap_all_filtered returns True when all holdings filtered + no positions
# ---------------------------------------------------------------------------

class TestSnapAllFiltered:
    """Test _snap_all_filtered function for weekend/holiday scenarios."""

    def test_snap_all_filtered_returns_true_when_holdings_filtered_and_no_positions(self):
        """Weekend scenario: Dhan returns holdings but all filtered, no positions."""
        from backend.api.algo.daily_snapshot import _snap_all_filtered

        account = "DH3747"
        target_date = date(2026, 8, 16)  # Saturday

        # Raw broker response has 3 holdings, but all were filtered
        raw = {
            "holdings": [
                {"tradingsymbol": "RELIANCE", "exchange": "NSE", "last_price": 0},
                {"tradingsymbol": "INFY", "exchange": "NSE", "last_price": 0},
                {"tradingsymbol": "TCS", "exchange": "NSE", "last_price": 0},
            ],
            "positions": []  # No positions on weekend
        }

        h_rows = []  # All 3 holdings were filtered
        p_rows = []  # No positions

        result = _snap_all_filtered(account, target_date, raw, h_rows, p_rows)

        assert result is True, (
            "Should return True when broker returned holdings but all were filtered + no positions "
            "(indicates bad payload, prior snapshot should be preserved)"
        )

    def test_snap_all_filtered_returns_false_when_some_holdings_pass(self):
        """Normal scenario: some holdings are not filtered."""
        from backend.api.algo.daily_snapshot import _snap_all_filtered

        account = "ZG0790"
        target_date = date(2026, 8, 14)

        raw = {
            "holdings": [
                {"tradingsymbol": "RELIANCE", "exchange": "NSE"},
                {"tradingsymbol": "INFY", "exchange": "NSE"},
            ],
            "positions": []
        }

        h_rows = [{"symbol": "RELIANCE", "ltp": 2850.0}]  # 1 of 2 passed
        p_rows = []

        result = _snap_all_filtered(account, target_date, raw, h_rows, p_rows)

        assert result is False, (
            "Should return False when at least one holding row passes the filter"
        )

    def test_snap_all_filtered_returns_true_when_all_positions_filtered(self):
        """All positions were filtered: broker returned them but they didn't pass."""
        from backend.api.algo.daily_snapshot import _snap_all_filtered

        account = "ZG0790"
        target_date = date(2026, 8, 14)

        raw = {
            "holdings": [],  # No holdings
            "positions": [
                {"tradingsymbol": "NIFTY25AUGFUT", "exchange": "NFO", "last_price": 0},
                {"tradingsymbol": "BANKNIFTY25AUGFUT", "exchange": "NFO", "last_price": 0},
            ]
        }

        h_rows = []
        p_rows = []  # All 2 positions were filtered

        result = _snap_all_filtered(account, target_date, raw, h_rows, p_rows)

        assert result is True, (
            "Should return True when broker returned positions but all were filtered"
        )

    def test_snap_all_filtered_returns_true_when_holdings_filtered_even_if_positions_pass(self):
        """Holdings filtered but positions passed: should still return True (bad holdings)."""
        from backend.api.algo.daily_snapshot import _snap_all_filtered

        account = "ZG0790"
        target_date = date(2026, 8, 14)

        raw = {
            "holdings": [{"tradingsymbol": "RELIANCE", "exchange": "NSE"}],
            "positions": [{"tradingsymbol": "NIFTY25AUGFUT", "exchange": "NFO"}]
        }

        h_rows = []  # All 1 holding filtered (bad payload)
        p_rows = [{"symbol": "NIFTY25AUGFUT", "ltp": 25100.0}]  # 1 position passed

        result = _snap_all_filtered(account, target_date, raw, h_rows, p_rows)

        # Returns True because holdings were all filtered (bad payload signal)
        # This protects the snapshot — holdings won't be updated, positions will be
        assert result is True, (
            "Should return True when holdings are all filtered, even if positions passed "
            "(indicates holdings bad payload, protects snapshot)"
        )

    def test_snap_all_filtered_returns_false_when_both_holdings_and_positions_pass(self):
        """Both holdings and positions have some rows pass the filter."""
        from backend.api.algo.daily_snapshot import _snap_all_filtered

        account = "ZG0790"
        target_date = date(2026, 8, 14)

        raw = {
            "holdings": [
                {"tradingsymbol": "RELIANCE", "exchange": "NSE"},
                {"tradingsymbol": "INFY", "exchange": "NSE"},
            ],
            "positions": [{"tradingsymbol": "NIFTY25AUGFUT", "exchange": "NFO"}]
        }

        h_rows = [{"symbol": "RELIANCE", "ltp": 2850.0}]  # 1 of 2 passed
        p_rows = [{"symbol": "NIFTY25AUGFUT", "ltp": 25100.0}]  # 1 of 1 passed

        result = _snap_all_filtered(account, target_date, raw, h_rows, p_rows)

        assert result is False, (
            "Should return False when at least one row passes from both holdings and positions"
        )

    def test_snap_all_filtered_returns_false_when_no_raw_holdings(self):
        """Empty raw holdings (broker returned nothing)."""
        from backend.api.algo.daily_snapshot import _snap_all_filtered

        account = "ZG0790"
        target_date = date(2026, 8, 14)

        raw = {
            "holdings": [],  # Broker returned no holdings
            "positions": []
        }

        h_rows = []
        p_rows = []

        result = _snap_all_filtered(account, target_date, raw, h_rows, p_rows)

        assert result is False, (
            "Should return False when broker returned no holdings (not a filtering issue, just empty account)"
        )

    def test_snap_all_filtered_returns_false_when_no_raw_positions(self):
        """Empty raw positions (broker returned nothing — no filtering)."""
        from backend.api.algo.daily_snapshot import _snap_all_filtered

        account = "ZG0790"
        target_date = date(2026, 8, 14)

        raw = {
            "holdings": [{"tradingsymbol": "RELIANCE", "exchange": "NSE"}],
            "positions": []  # Broker returned no positions (account has no open positions)
        }

        h_rows = [{"symbol": "RELIANCE", "ltp": 2850.0}]
        p_rows = []

        result = _snap_all_filtered(account, target_date, raw, h_rows, p_rows)

        assert result is False, (
            "Should return False when broker returned no positions (no filtering happened)"
        )

    def test_snap_all_filtered_returns_false_when_raw_positions_none(self):
        """raw['positions'] is None (broker call failed, not filtering)."""
        from backend.api.algo.daily_snapshot import _snap_all_filtered

        account = "ZG0790"
        target_date = date(2026, 8, 14)

        raw = {
            "holdings": [{"tradingsymbol": "RELIANCE", "exchange": "NSE"}],
            "positions": None  # Broker positions call failed
        }

        h_rows = [{"symbol": "RELIANCE", "ltp": 2850.0}]
        p_rows = []

        result = _snap_all_filtered(account, target_date, raw, h_rows, p_rows)

        # When positions is None, len(None or []) = len([]) = 0 (no raw positions)
        # raw_p_count = 0, so the "all positions filtered" check doesn't fire
        # Only holdings check matters: raw_h_count=1 > 0, len(h_rows)=1, so NOT all filtered
        assert result is False, (
            "Should return False when positions call failed (None) but holdings passed"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
