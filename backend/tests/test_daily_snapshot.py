"""
Tests for backend/api/algo/daily_snapshot.py

Covers:
  - kite_seg_from_exchange() mapping
  - Row builders (_holdings_rows, _positions_rows, _trades_rows)
  - snapshot_daily_book() against an in-memory SQLite DB:
      - correct row counts per kind / segment
      - upsert idempotency (re-run same date → same row count, updated values)
  - trades skipped for past dates
"""

import asyncio
import json
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# In-memory SQLite engine for isolated tests
# ---------------------------------------------------------------------------

_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_session():
    """Provide an in-memory SQLite session with only the daily_book table.

    We build a fresh MetaData with just the daily_book Table definition so
    SQLite never sees the JSONB columns on unrelated models (agents etc.).
    """
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
        Column("avg_cost", Numeric, nullable=True),
        Column("ltp", Numeric, nullable=True),
        Column("day_pnl", Numeric, nullable=True),
        Column("total_pnl", Numeric, nullable=True),
        Column("payload_json", Text, nullable=True),
        Column("captured_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("date", "account", "kind", "symbol",
                         name="uq_daily_book_day_acct_kind_sym"),
    )

    engine = create_async_engine(_TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


# ---------------------------------------------------------------------------
# Monkey-patch _upsert_rows to use the fixture session
# ---------------------------------------------------------------------------

def _make_patch_upsert(session):
    """Return an async replacement for _upsert_rows that uses `session`."""
    from backend.api.algo.daily_snapshot import _UPSERT_SQL
    from datetime import datetime, timezone

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


# ---------------------------------------------------------------------------
# Canned broker data
# ---------------------------------------------------------------------------

_HOLDINGS = [
    {
        "tradingsymbol": "INFY",
        "exchange": "NSE",
        "opening_quantity": 10,
        "average_price": 1500.0,
        "last_price": 1560.0,
        "day_change": 60.0,
        "pnl": 600.0,
    },
    {
        "tradingsymbol": "TCS",
        "exchange": "NSE",
        "opening_quantity": 5,
        "average_price": 3400.0,
        "last_price": 3450.0,
        "day_change": 50.0,
        "pnl": 250.0,
    },
]

_POSITIONS = [
    {
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange": "NFO",
        "quantity": -50,
        "average_price": 22500.0,
        "last_price": 22400.0,
        "pnl": 5000.0,
    },
]

_TRADES = [
    {
        "tradingsymbol": "INFY",
        "exchange": "NSE",
        "filled_quantity": 10,
        "average_price": 1500.0,
        "order_id": "ORD001",
    },
]


# ---------------------------------------------------------------------------
# Helper — build a mock Connections singleton
# ---------------------------------------------------------------------------

def _make_connections(holdings=None, positions=None, trades=None):
    kite = MagicMock()
    kite.holdings.return_value = holdings or []
    kite.positions.return_value = {"net": positions or []}
    kite.trades.return_value = trades or []

    kite_conn = MagicMock()
    kite_conn.get_kite_conn.return_value = kite

    conn_singleton = MagicMock()
    conn_singleton.conn = {"ZG0790": kite_conn}
    return conn_singleton, kite


# ---------------------------------------------------------------------------
# Unit tests — pure functions (no DB)
# ---------------------------------------------------------------------------

class TestSegmentClassifier:
    def test_nse_is_equity(self):
        from backend.api.algo.daily_snapshot import kite_seg_from_exchange
        assert kite_seg_from_exchange("NSE") == "equity"

    def test_bse_is_equity(self):
        from backend.api.algo.daily_snapshot import kite_seg_from_exchange
        assert kite_seg_from_exchange("BSE") == "equity"

    def test_nfo_is_derivatives(self):
        from backend.api.algo.daily_snapshot import kite_seg_from_exchange
        assert kite_seg_from_exchange("NFO") == "derivatives"

    def test_mcx_is_commodity(self):
        from backend.api.algo.daily_snapshot import kite_seg_from_exchange
        assert kite_seg_from_exchange("MCX") == "commodity"

    def test_cds_is_currency(self):
        from backend.api.algo.daily_snapshot import kite_seg_from_exchange
        assert kite_seg_from_exchange("CDS") == "currency"

    def test_unknown_defaults_equity(self):
        from backend.api.algo.daily_snapshot import kite_seg_from_exchange
        assert kite_seg_from_exchange("XYZ") == "equity"


class TestRowBuilders:
    _D = date(2026, 5, 8)
    # 23:35 IST — after both NSE (15:30) and MCX (23:30) close, so row
    # builders emit full ltp/day_pnl for every exchange (no mid-session
    # gating). Keeps these unit tests independent of clock time.
    _NOW_EOD = datetime(2026, 5, 8, 23, 35)

    def test_holdings_row_count(self):
        from backend.api.algo.daily_snapshot import _holdings_rows
        rows = _holdings_rows("ZG0790", self._D, _HOLDINGS, self._NOW_EOD)
        assert len(rows) == 2

    def test_holdings_row_shape(self):
        from backend.api.algo.daily_snapshot import _holdings_rows
        rows = _holdings_rows("ZG0790", self._D, _HOLDINGS, self._NOW_EOD)
        r = rows[0]
        assert r["kind"] == "holdings"
        assert r["segment"] == "equity"
        assert r["qty"] == 10
        assert r["avg_cost"] == 1500.0
        assert r["ltp"] == 1560.0
        assert r["total_pnl"] == 600.0
        assert json.loads(r["payload_json"])["tradingsymbol"] == "INFY"

    def test_positions_row_shape(self):
        from backend.api.algo.daily_snapshot import _positions_rows
        rows = _positions_rows("ZG0790", self._D, _POSITIONS, self._NOW_EOD)
        assert len(rows) == 1
        r = rows[0]
        assert r["kind"] == "positions"
        assert r["segment"] == "derivatives"
        assert r["qty"] == -50

    def test_positions_mid_session_mcx_emits_none(self):
        """MCX position snapshotted at 15:35 IST (mid-MCX-session) must
        emit ltp=None + day_pnl=None so the close-override path in
        positions.py doesn't consume a mid-session value as yesterday's
        EOD. The 23:35 IST follow-up pass captures the real EOD."""
        from backend.api.algo.daily_snapshot import _positions_rows
        mcx_pos = [{
            "tradingsymbol": "CRUDEOIL26JUL6900PE", "exchange": "MCX",
            "last_price": 264.5, "close_price": 220.0, "quantity": 1,
            "average_price": 245.0, "pnl": 19.5,
        }]
        now_1535 = datetime(2026, 5, 8, 15, 35)
        rows = _positions_rows("ZG0790", self._D, mcx_pos, now_1535)
        assert rows[0]["ltp"] is None
        assert rows[0]["day_pnl"] is None
        # qty + avg_cost + total_pnl still captured — they're not session-sensitive
        assert rows[0]["qty"] == 1
        assert rows[0]["avg_cost"] == 245.0
        assert rows[0]["total_pnl"] == 19.5
        # Same row at 23:35 (after MCX close) gets full EOD values
        now_2335 = datetime(2026, 5, 8, 23, 35)
        rows = _positions_rows("ZG0790", self._D, mcx_pos, now_2335)
        assert rows[0]["ltp"] == 264.5
        assert rows[0]["day_pnl"] == 44.5  # (264.5 - 220.0) × 1

    def test_trades_row_shape(self):
        from backend.api.algo.daily_snapshot import _trades_rows
        rows = _trades_rows("ZG0790", self._D, _TRADES)
        assert len(rows) == 1
        r = rows[0]
        assert r["kind"] == "trades"
        assert r["ltp"] is None
        assert r["total_pnl"] is None


# ---------------------------------------------------------------------------
# Integration tests — against in-memory SQLite
# ---------------------------------------------------------------------------

TARGET_DATE = date(2026, 5, 8)
TODAY_IST_STR = "2026-05-08"  # matches TARGET_DATE


def _patch_context(db_session, conn_singleton, ist_datetime):
    """Return a context manager stack patching all external deps for snapshot_daily_book."""
    from backend.api.algo import daily_snapshot as ds
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch.object(ds, "_upsert_rows", _make_patch_upsert(db_session)))
    # _get_connections() is the module-level wrapper; patch it to return our stub.
    stack.enter_context(
        patch.object(ds, "_get_connections", return_value=conn_singleton)
    )
    stack.enter_context(
        patch("backend.api.algo.daily_snapshot.timestamp_indian",
              return_value=ist_datetime)
    )
    return stack


@pytest.mark.skip(reason="SQLite ON CONFLICT does not honour the UniqueConstraint here; "
                          "production code targets PostgreSQL which handles it correctly. "
                          "Track in follow-up: build a SQLite-compatible upsert path for tests.")
@pytest.mark.asyncio
async def test_snapshot_row_counts(db_session):
    """snapshot_daily_book inserts correct rows per kind."""
    from backend.api.algo import daily_snapshot as ds

    conn_singleton, _kite = _make_connections(
        holdings=_HOLDINGS, positions=_POSITIONS, trades=_TRADES
    )

    with _patch_context(db_session, conn_singleton,
                        datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)):
        result = await ds.snapshot_daily_book(target_date=TARGET_DATE)

    assert result["accounts"] == ["ZG0790"]
    assert result["holdings_rows"] == 2
    assert result["positions_rows"] == 1
    assert result["trades_rows"] == 1
    assert result["errors"] == []

    # Verify rows in DB
    count = (await db_session.execute(
        text("SELECT COUNT(*) FROM daily_book")
    )).scalar()
    assert count == 4  # 2 holdings + 1 positions + 1 trades


@pytest.mark.skip(reason="SQLite ON CONFLICT incompatibility — same as test_snapshot_row_counts.")
@pytest.mark.asyncio
async def test_snapshot_upsert_idempotency(db_session):
    """Re-running the snapshot for the same date updates values, not duplicates."""
    from backend.api.algo import daily_snapshot as ds

    conn_singleton, kite = _make_connections(
        holdings=_HOLDINGS, positions=_POSITIONS, trades=_TRADES
    )
    ist_ts = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)

    with _patch_context(db_session, conn_singleton, ist_ts):
        await ds.snapshot_daily_book(target_date=TARGET_DATE)

    count_after_first = (await db_session.execute(
        text("SELECT COUNT(*) FROM daily_book")
    )).scalar()

    # Change LTP on INFY and re-run
    updated_holdings = [
        {**_HOLDINGS[0], "last_price": 1600.0, "pnl": 1000.0},
        _HOLDINGS[1],
    ]
    conn_singleton2, _ = _make_connections(
        holdings=updated_holdings, positions=_POSITIONS, trades=_TRADES
    )
    ist_ts2 = datetime(2026, 5, 8, 15, 40, 0, tzinfo=timezone.utc)

    with _patch_context(db_session, conn_singleton2, ist_ts2):
        await ds.snapshot_daily_book(target_date=TARGET_DATE)

    count_after_second = (await db_session.execute(
        text("SELECT COUNT(*) FROM daily_book")
    )).scalar()

    # Row count must not change
    assert count_after_first == count_after_second, "Upsert must not create duplicate rows"

    # Verify INFY ltp was updated
    row = (await db_session.execute(
        text("SELECT ltp FROM daily_book WHERE symbol='INFY' AND kind='holdings'")
    )).fetchone()
    assert row is not None
    assert float(row[0]) == pytest.approx(1600.0)


@pytest.mark.asyncio
async def test_snapshot_no_trades_for_past_date(db_session):
    """Trades are skipped when target_date != today IST."""
    from backend.api.algo import daily_snapshot as ds

    conn_singleton, kite = _make_connections(
        holdings=_HOLDINGS, positions=_POSITIONS, trades=_TRADES
    )

    past_date = date(2026, 4, 1)  # clearly not today

    with _patch_context(db_session, conn_singleton,
                        datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)):
        result = await ds.snapshot_daily_book(target_date=past_date)

    assert result["trades_rows"] == 0
    # trades() should NOT have been called
    kite.trades.assert_not_called()


@pytest.mark.skip(reason="SQLite ON CONFLICT incompatibility — same as test_snapshot_row_counts.")
@pytest.mark.asyncio
async def test_snapshot_per_account_error_is_tolerated(db_session):
    """A broker failure on one account logs an error but doesn't abort."""
    from backend.api.algo import daily_snapshot as ds

    kite = MagicMock()
    kite.holdings.side_effect = Exception("Kite outage")
    kite.positions.return_value = {"net": _POSITIONS}
    kite.trades.return_value = _TRADES

    kite_conn = MagicMock()
    kite_conn.get_kite_conn.return_value = kite

    conn_singleton = MagicMock()
    conn_singleton.conn = {"ZG0790": kite_conn}

    with _patch_context(db_session, conn_singleton,
                        datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)):
        result = await ds.snapshot_daily_book(target_date=TARGET_DATE)

    # holdings fetch failed → only positions + trades rows, no crash
    assert result["holdings_rows"] == 0
    assert result["positions_rows"] == 1
    assert result["trades_rows"] == 1
    assert result["errors"] == []   # per-kind errors are logged + continue; account doesn't error out
