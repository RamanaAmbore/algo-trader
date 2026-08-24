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
        Column("lots", Integer, nullable=False, default=1),
        Column("lot_size", Integer, nullable=False, default=1),
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

    def test_holdings_previous_close_populated(self):
        """Test that previous_close is populated from close_price when present."""
        from backend.api.algo.daily_snapshot import _holdings_rows
        holding_with_close = {
            "tradingsymbol": "SIEMENS",
            "exchange": "NSE",
            "opening_quantity": 5,
            "average_price": 3000.0,
            "last_price": 7500.0,
            "day_change": 150.0,
            "pnl": 10000.0,
            "close_price": 7350.0,  # Previous close price
        }
        rows = _holdings_rows("ZG0790", self._D, [holding_with_close], self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["previous_close"] == 7350.0, \
            f"expected previous_close=7350.0 but got {rows[0]['previous_close']}"

    def test_holdings_previous_close_falls_back_to_ltp_when_absent(self):
        """Fix P1-B: when close_price is absent, previous_close falls back to ltp_val.

        Before Fix P1-B, previous_close was None when close_price was missing.
        After the fix, ltp_val is the last-resort fallback so that NULL never
        reaches the DB (which would trigger the |ltp-close|<=0.005 guard in
        holdings.py and produce stale day_change_val=0).
        """
        from backend.api.algo.daily_snapshot import _holdings_rows
        holding_no_close = {
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "opening_quantity": 10,
            "average_price": 1500.0,
            "last_price": 1560.0,
            "day_change": 60.0,
            "pnl": 600.0,
            # close_price intentionally missing
        }
        rows = _holdings_rows("ZG0790", self._D, [holding_no_close], self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["previous_close"] is not None, (
            "previous_close must not be None when close_price missing — ltp_val fallback must apply"
        )
        assert rows[0]["previous_close"] == pytest.approx(1560.0), (
            f"previous_close must equal ltp_val=1560.0 (ltp fallback); got {rows[0]['previous_close']}"
        )

    def test_holdings_previous_close_falls_back_to_ltp_when_zero(self):
        """Fix P1-B: when close_price is zero, previous_close falls back to ltp_val."""
        from backend.api.algo.daily_snapshot import _holdings_rows
        holding_zero_close = {
            "tradingsymbol": "FOO",
            "exchange": "NSE",
            "opening_quantity": 5,
            "average_price": 1000.0,
            "last_price": 1100.0,
            "day_change": 100.0,
            "pnl": 500.0,
            "close_price": 0,  # Zero close price
        }
        rows = _holdings_rows("ZG0790", self._D, [holding_zero_close], self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["previous_close"] is not None, (
            "previous_close must not be None when close_price=0 — ltp_val fallback must apply"
        )
        assert rows[0]["previous_close"] == pytest.approx(1100.0), (
            f"previous_close must equal ltp_val=1100.0; got {rows[0]['previous_close']}"
        )

    def test_holdings_previous_close_falls_back_to_ltp_when_none(self):
        """Fix P1-B: when close_price is explicitly None, previous_close falls back to ltp_val."""
        from backend.api.algo.daily_snapshot import _holdings_rows
        holding_none_close = {
            "tradingsymbol": "BAR",
            "exchange": "BSE",
            "opening_quantity": 2,
            "average_price": 2000.0,
            "last_price": 2100.0,
            "day_change": 100.0,
            "pnl": 200.0,
            "close_price": None,  # Explicitly None
        }
        rows = _holdings_rows("ZG0790", self._D, [holding_none_close], self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["previous_close"] is not None, (
            "previous_close must not be None when close_price=None — ltp_val fallback must apply"
        )
        assert rows[0]["previous_close"] == pytest.approx(2100.0), (
            f"previous_close must equal ltp_val=2100.0; got {rows[0]['previous_close']}"
        )

    def test_holdings_previous_close_stored_in_payload(self):
        """Test that previous_close is also captured in snapshot_extras for downstream readers."""
        from backend.api.algo.daily_snapshot import _holdings_rows
        holding_with_close = {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "opening_quantity": 1,
            "average_price": 2500.0,
            "last_price": 2600.0,
            "day_change": 100.0,
            "pnl": 100.0,
            "close_price": 2550.0,
        }
        rows = _holdings_rows("ZG0790", self._D, [holding_with_close], self._NOW_EOD)
        assert len(rows) == 1

        # Verify previous_close is in the row
        assert rows[0]["previous_close"] == 2550.0

        # Verify prev_close is also in the payload snapshot_extras
        payload = json.loads(rows[0]["payload_json"])
        assert "snapshot_extras" in payload, "snapshot_extras missing from payload"
        assert payload["snapshot_extras"]["prev_close"] == 2550.0, \
            f"expected prev_close=2550.0 in snapshot_extras, got {payload['snapshot_extras'].get('prev_close')}"

    def test_positions_row_shape(self):
        from backend.api.algo.daily_snapshot import _positions_rows
        rows = _positions_rows("ZG0790", self._D, _POSITIONS, self._NOW_EOD)
        assert len(rows) == 1
        r = rows[0]
        assert r["kind"] == "positions"
        assert r["segment"] == "derivatives"
        assert r["qty"] == -50

    def test_positions_previous_close_populated(self):
        """Test that positions previous_close is populated from close_price when present."""
        from backend.api.algo.daily_snapshot import _positions_rows
        pos_with_close = {
            "traditionsymbol": "NIFTY25APRFUT",
            "exchange": "NFO",
            "quantity": -50,
            "average_price": 22500.0,
            "last_price": 22400.0,
            "pnl": 5000.0,
            "close_price": 22450.0,  # Prior session settlement
            "tradingsymbol": "NIFTY25APRFUT",  # Override above
        }
        rows = _positions_rows("ZG0790", self._D, [pos_with_close], self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["previous_close"] == 22450.0, \
            f"expected previous_close=22450.0 but got {rows[0]['previous_close']}"

    def test_positions_previous_close_none_when_absent(self):
        """Test that positions previous_close is None when close_price is absent."""
        from backend.api.algo.daily_snapshot import _positions_rows
        pos_no_close = {
            "tradingsymbol": "BANKNIFTY25APRFUT",
            "exchange": "NFO",
            "quantity": 10,
            "average_price": 52000.0,
            "last_price": 52100.0,
            "pnl": 1000.0,
            # close_price intentionally missing
        }
        rows = _positions_rows("ZG0790", self._D, [pos_no_close], self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["previous_close"] is None, \
            f"expected previous_close=None when close_price missing, got {rows[0]['previous_close']}"

    def test_positions_previous_close_none_when_zero(self):
        """Test that positions previous_close is None when close_price is zero."""
        from backend.api.algo.daily_snapshot import _positions_rows
        pos_zero_close = {
            "tradingsymbol": "CRUDEOIL26JULFUT",
            "exchange": "MCX",
            "quantity": 1,
            "average_price": 245.0,
            "last_price": 264.5,
            "pnl": 19.5,
            "close_price": 0,  # Zero close price
        }
        rows = _positions_rows("ZG0790", self._D, [pos_zero_close], self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["previous_close"] is None, \
            f"expected previous_close=None when close_price=0, got {rows[0]['previous_close']}"

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

    # ------------------------------------------------------------------
    # Change A: MCX day_pnl lot-scale fix
    # ------------------------------------------------------------------

    def test_snap_compute_day_pnl_mcx_lot_scale(self):
        """MCX new position: oq/bq/sq in lots must be scaled by multiplier=100.

        CRUDEOIL: lot_size=100, oq=1 lot, ltp=6900, cls=6800 → correct day
        P&L = (6900-6800) × 100 = 10_000. Without the fix the result was
        100× too small: (6900-6800) × 1 = 100.
        """
        from backend.api.algo.daily_snapshot import _snap_compute_day_pnl
        r = {
            "overnight_quantity":  1,    # in LOTS
            "day_buy_quantity":    0,
            "day_sell_quantity":   0,
            "day_buy_value":       0.0,
            "day_sell_value":      0.0,
        }
        result = _snap_compute_day_pnl(r, ltp_val=6900.0, close_price=6800.0, qty=1, multiplier=100)
        assert result == pytest.approx(10_000.0), \
            f"MCX day_pnl should be 10000 (oq×m=100 contracts), got {result}"

    def test_snap_compute_day_pnl_equity_unaffected(self):
        """Equity (multiplier=1) must be unchanged after lot-scale addition."""
        from backend.api.algo.daily_snapshot import _snap_compute_day_pnl
        r = {
            "overnight_quantity":  10,
            "day_buy_quantity":    0,
            "day_sell_quantity":   0,
            "day_buy_value":       0.0,
            "day_sell_value":      0.0,
        }
        result = _snap_compute_day_pnl(r, ltp_val=2600.0, close_price=2500.0, qty=10, multiplier=1)
        assert result == pytest.approx(1000.0), \
            f"Equity day_pnl = (2600-2500)×10 = 1000, got {result}"

    def test_snap_compute_day_pnl_mcx_intraday_bv_sv_not_scaled(self):
        """day_buy_value / day_sell_value are absolute ₹ — must NOT be
        scaled by multiplier. Only oq/bq/sq are in lots."""
        from backend.api.algo.daily_snapshot import _snap_compute_day_pnl
        # 1 lot CRUDEOIL bought intraday at 6800 (bv=6800×100=680000), sold at
        # 6900 (sv=6900×100=690000). Net = sv - bv = 10000.
        r = {
            "overnight_quantity":  0,
            "day_buy_quantity":    1,    # 1 lot
            "day_sell_quantity":   1,    # 1 lot
            "day_buy_value":  680_000.0, # absolute ₹ — already in rupees
            "day_sell_value": 690_000.0, # absolute ₹ — already in rupees
        }
        result = _snap_compute_day_pnl(r, ltp_val=6900.0, close_price=6800.0, qty=0, multiplier=100)
        # decomposed: oq×(ltp-cls) + (sv - bv) - sq×ltp + bq×ltp  (simplified for flat intraday)
        # = 0 + (690000 - 680000) - 100×6900 + 100×6900 = 10000
        assert result == pytest.approx(10_000.0), \
            f"MCX intraday bv/sv should not be scaled; expected 10000, got {result}"

    def test_positions_rows_mcx_reads_multiplier(self):
        """_positions_rows must read multiplier from raw row and pass through.

        CRUDEOIL overnight position: multiplier=100, oq=1 lot.
        Expected day_pnl = (ltp - cls) × oq × 100 = (6900 - 6800) × 100 = 10000.
        """
        from backend.api.algo.daily_snapshot import _positions_rows
        mcx_pos = [{
            "tradingsymbol": "CRUDEOIL26AUGFUT",
            "exchange": "MCX",
            "quantity": 100,            # contracts (qty = lots × lot_size)
            "average_price": 6800.0,
            "last_price": 6900.0,
            "close_price": 6800.0,
            "pnl": 10_000.0,
            "multiplier": 100,          # lot_size field Kite ships
            "overnight_quantity":  1,   # in LOTS
            "day_buy_quantity":    0,
            "day_sell_quantity":   0,
            "day_buy_value":       0.0,
            "day_sell_value":      0.0,
        }]
        # Use EOD time (after MCX close) so the snapshot captures day_pnl
        rows = _positions_rows("ZG0790", self._D, mcx_pos, self._NOW_EOD)
        assert len(rows) == 1, "expected one row"
        assert rows[0]["day_pnl"] == pytest.approx(10_000.0), \
            f"MCX day_pnl via _positions_rows: expected 10000, got {rows[0]['day_pnl']}"

    def test_positions_rows_mcx_multiplier_guard_lt1(self):
        """multiplier < 1 must be clamped to 1 (bad broker data guard)."""
        from backend.api.algo.daily_snapshot import _positions_rows
        pos = [{
            "tradingsymbol": "CRUDEOIL26AUGFUT",
            "exchange": "MCX",
            "quantity": 1,
            "average_price": 6800.0,
            "last_price": 6900.0,
            "close_price": 6800.0,
            "pnl": 100.0,
            "multiplier": 0,            # pathological value → should clamp to 1
            "overnight_quantity":  1,
            "day_buy_quantity":    0,
            "day_sell_quantity":   0,
            "day_buy_value":       0.0,
            "day_sell_value":      0.0,
        }]
        rows = _positions_rows("ZG0790", self._D, pos, self._NOW_EOD)
        assert len(rows) == 1
        # With multiplier clamped to 1: (6900-6800)×1 = 100
        assert rows[0]["day_pnl"] == pytest.approx(100.0), \
            f"multiplier=0 should clamp to 1; expected day_pnl=100, got {rows[0]['day_pnl']}"


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


# ---------------------------------------------------------------------------
# Tests for bug fixes: _backfill_recompute_derived + _snap_holding_eod_vals
# ---------------------------------------------------------------------------


class TestBackfillRecomputeDerived:
    """Tests for _backfill_recompute_derived (close_was_missing flag).

    Validates Fix A: when Dhan ships close_price=0, the broker's existing
    day_change equals ltp - 0 = ltp (e.g. 3952). After backfill sets the
    real close_price, we must recompute day_change = ltp - close (e.g. -28).
    """

    def test_recomputes_day_change_when_close_was_missing(self):
        """Dhan case: day_change was ltp-0=ltp; after backfill sets close=prev_close, recompute."""
        from backend.api.algo.daily_snapshot import _backfill_recompute_derived
        r = {
            "last_price": 3952.0,
            "close_price": 3980.0,
            "day_change": 3952.0,  # Wrong: ltp - 0, from broker
            "average_price": 3606.0,
            "opening_quantity": 2,
        }
        _backfill_recompute_derived(r, "opening_quantity", close_was_missing=True)
        assert r["day_change"] == pytest.approx(3952.0 - 3980.0), \
            f"expected day_change=-28 (ltp-close) but got {r['day_change']}"

    def test_does_not_recompute_when_close_was_present(self):
        """Kite case: close was non-zero from broker, day_change from broker is trusted."""
        from backend.api.algo.daily_snapshot import _backfill_recompute_derived
        r = {
            "last_price": 3952.0,
            "close_price": 3980.0,
            "day_change": -28.0,  # Correct value from broker
            "average_price": 3606.0,
            "opening_quantity": 2,
        }
        _backfill_recompute_derived(r, "opening_quantity", close_was_missing=False)
        assert r["day_change"] == pytest.approx(-28.0), \
            f"expected day_change=-28 (unchanged) but got {r['day_change']}"

    def test_sets_day_change_when_not_present(self):
        """Groww case: day_change absent; backfill should set it."""
        from backend.api.algo.daily_snapshot import _backfill_recompute_derived
        r = {
            "last_price": 3952.0,
            "close_price": 3980.0,
            # day_change intentionally missing
            "average_price": 3606.0,
            "opening_quantity": 2,
        }
        _backfill_recompute_derived(r, "opening_quantity", close_was_missing=False)
        assert r["day_change"] == pytest.approx(3952.0 - 3980.0), \
            f"expected day_change=-28 (ltp-close) but got {r['day_change']}"

    def test_does_not_set_day_change_when_close_zero_and_flag_false(self):
        """When close_price is zero and close_was_missing=False, day_change is not set.
        This matches the legacy behavior for brokers where close_price=0 is legitimate."""
        from backend.api.algo.daily_snapshot import _backfill_recompute_derived
        r = {
            "last_price": 3952.0,
            "close_price": 0,  # Zero close
            "average_price": 3606.0,
            "opening_quantity": 2,
        }
        _backfill_recompute_derived(r, "opening_quantity", close_was_missing=False)
        assert "day_change" not in r or r.get("day_change") is None, \
            f"expected day_change to remain absent, but got {r.get('day_change')}"

    def test_does_not_set_pnl_when_already_present(self):
        """pnl is only set if not present; existing value is never overwritten."""
        from backend.api.algo.daily_snapshot import _backfill_recompute_derived
        r = {
            "last_price": 3952.0,
            "close_price": 3980.0,
            "average_price": 3606.0,
            "opening_quantity": 2,
            "pnl": 100.0,  # Broker-provided pnl
        }
        _backfill_recompute_derived(r, "opening_quantity", close_was_missing=False)
        assert r["pnl"] == pytest.approx(100.0), \
            f"expected pnl=100 (unchanged) but got {r['pnl']}"

    def test_computes_pnl_when_absent(self):
        """When pnl is absent, compute from (ltp - avg) * qty."""
        from backend.api.algo.daily_snapshot import _backfill_recompute_derived
        r = {
            "last_price": 3952.0,
            "close_price": 3980.0,
            "average_price": 3606.0,
            "opening_quantity": 2,
            # pnl intentionally missing
        }
        _backfill_recompute_derived(r, "opening_quantity", close_was_missing=False)
        expected_pnl = (3952.0 - 3606.0) * 2
        assert r["pnl"] == pytest.approx(expected_pnl), \
            f"expected pnl={expected_pnl} but got {r.get('pnl')}"


class TestSnapHoldingEodVals:
    """Tests for _snap_holding_eod_vals — day_pnl stores total (not per-share).

    Validates Fix B: day_pnl_v is computed as day_change × qty, not just
    day_change. This allows holdings.py to correctly compute day P&L % by
    dividing total by close_notional (prev_close × qty).
    """

    def _make_row(self, ltp, day_change, pnl, qty=1):
        """Helper to build a holdings row dict with minimal fields."""
        return {
            "last_price": ltp,
            "day_change": day_change,
            "pnl": pnl,
            "opening_quantity": qty,
        }

    def test_day_pnl_is_total_for_multi_qty(self):
        """day_pnl = day_change × qty, not per-share."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals
        r = self._make_row(ltp=3952, day_change=-28.0, pnl=692.0, qty=2)
        ltp_val, day_pnl_v, total_pnl_v = _snap_holding_eod_vals(r, mid_session=False)
        assert day_pnl_v == pytest.approx(-56.0), \
            f"expected day_pnl=day_change×qty=-56.0 but got {day_pnl_v}"

    def test_day_pnl_for_qty_one(self):
        """qty=1: day_pnl = day_change (same as before fix, but now internally computed)."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals
        r = self._make_row(ltp=3952, day_change=-28.0, pnl=692.0, qty=1)
        _, day_pnl_v, _ = _snap_holding_eod_vals(r, mid_session=False)
        assert day_pnl_v == pytest.approx(-28.0), \
            f"expected day_pnl=-28.0 but got {day_pnl_v}"

    def test_day_pnl_positive_for_multi_qty(self):
        """Positive day_change: total day_pnl is proportional to qty."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals
        r = self._make_row(ltp=1560, day_change=60.0, pnl=600.0, qty=10)
        _, day_pnl_v, _ = _snap_holding_eod_vals(r, mid_session=False)
        assert day_pnl_v == pytest.approx(600.0), \
            f"expected day_pnl=60×10=600.0 but got {day_pnl_v}"

    def test_day_pnl_none_when_mid_session(self):
        """Mid-session: day_pnl is None to avoid partial-day values."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals
        r = self._make_row(ltp=3952, day_change=-28.0, pnl=692.0, qty=2)
        ltp_val, day_pnl_v, _ = _snap_holding_eod_vals(r, mid_session=True)
        assert ltp_val is None, f"expected ltp=None mid-session but got {ltp_val}"
        assert day_pnl_v is None, f"expected day_pnl=None mid-session but got {day_pnl_v}"

    def test_day_pnl_none_when_day_change_absent(self):
        """No day_change field → day_pnl is None."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals
        r = {
            "last_price": 3952,
            "pnl": 692.0,
            "opening_quantity": 2,
            # day_change intentionally missing
        }
        _, day_pnl_v, _ = _snap_holding_eod_vals(r, mid_session=False)
        assert day_pnl_v is None, \
            f"expected day_pnl=None when day_change absent but got {day_pnl_v}"

    def test_total_pnl_unchanged(self):
        """total_pnl_v is unchanged by Fix B — still just the raw pnl field."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals
        r = self._make_row(ltp=3952, day_change=-28.0, pnl=692.0, qty=2)
        _, _, total_pnl_v = _snap_holding_eod_vals(r, mid_session=False)
        assert total_pnl_v == pytest.approx(692.0), \
            f"expected total_pnl=692.0 (unchanged) but got {total_pnl_v}"

    def test_ltp_val_returned_correctly(self):
        """ltp_val is returned as-is at EOD."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals
        r = self._make_row(ltp=3952.0, day_change=-28.0, pnl=692.0, qty=2)
        ltp_val, _, _ = _snap_holding_eod_vals(r, mid_session=False)
        assert ltp_val == pytest.approx(3952.0), \
            f"expected ltp=3952.0 but got {ltp_val}"

    def test_zero_day_change_still_multiplied(self):
        """Zero day_change is still multiplied by qty (result is 0 × qty = 0)."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals
        r = self._make_row(ltp=3000, day_change=0.0, pnl=0.0, qty=5)
        _, day_pnl_v, _ = _snap_holding_eod_vals(r, mid_session=False)
        assert day_pnl_v == pytest.approx(0.0), \
            f"expected day_pnl=0.0 (0×5) but got {day_pnl_v}"

    def test_negative_qty_handled(self):
        """Negative qty (short positions): day_pnl sign flips with qty sign."""
        from backend.api.algo.daily_snapshot import _snap_holding_eod_vals
        r = {
            "last_price": 3952,
            "day_change": -28.0,
            "pnl": 56.0,
            "opening_quantity": -2,  # Short 2 shares
        }
        _, day_pnl_v, _ = _snap_holding_eod_vals(r, mid_session=False)
        assert day_pnl_v == pytest.approx(56.0), \
            f"expected day_pnl=-28×(-2)=56.0 but got {day_pnl_v}"


# ---------------------------------------------------------------------------
# Fix P1-A — qty field must prefer `quantity` over `opening_quantity`
# so partially-sold holdings record the remaining (post-sell) qty.
# ---------------------------------------------------------------------------

class TestHoldingsRowQtyPriority:
    """Regression tests for Fix P1-A: `_holdings_rows` qty field ordering.

    Before the fix, `opening_quantity` was used first, writing the pre-sell
    quantity for partially-sold holdings. The fix swaps the priority so
    `quantity` (remaining shares) is used first and `opening_quantity` is
    only the fallback for older broker payloads that omit `quantity`.
    """

    _D = date(2026, 8, 19)
    _NOW_EOD = datetime(2026, 8, 19, 23, 35)

    def test_partially_sold_holding_uses_quantity_not_opening_quantity(self):
        """Partially-sold holding (qty=50, oq=100) must write qty=50 into DB.

        Before the fix: qty field used opening_quantity=100 (pre-sell).
        After the fix:  qty field uses quantity=50 (remaining shares).
        """
        from backend.api.algo.daily_snapshot import _holdings_rows

        holding = {
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "quantity": 50,            # remaining after partial sell
            "opening_quantity": 100,   # pre-sell qty — must NOT win
            "average_price": 1500.0,
            "last_price": 1600.0,
            "day_change": 100.0,
            "pnl": 5000.0,
            "close_price": 1550.0,
        }
        rows = _holdings_rows("ZG0790", self._D, [holding], self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["qty"] == 50, (
            f"qty must be `quantity`=50 (remaining), not `opening_quantity`=100; "
            f"got {rows[0]['qty']}"
        )

    def test_partial_sell_qty_less_than_opening_quantity(self):
        """Second partial-sell scenario: qty=30, opening_quantity=80.

        Verifies the fix holds for an arbitrary partially-sold quantity
        (not just 50/100 from the first test).
        """
        from backend.api.algo.daily_snapshot import _holdings_rows

        holding = {
            "tradingsymbol": "TCS",
            "exchange": "NSE",
            "quantity": 30,
            "opening_quantity": 80,
            "average_price": 3400.0,
            "last_price": 3450.0,
            "day_change": 50.0,
            "pnl": 5000.0,
            "close_price": 3420.0,
        }
        rows = _holdings_rows("ZG0790", self._D, [holding], self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["qty"] == 30, (
            f"qty must be `quantity`=30 (remaining shares); got {rows[0]['qty']}"
        )

    def test_unsold_holding_quantity_equals_opening_quantity(self):
        """Unsold holding (qty == oq): either field gives the same result."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        holding = {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "quantity": 10,
            "opening_quantity": 10,
            "average_price": 2500.0,
            "last_price": 2600.0,
            "day_change": 100.0,
            "pnl": 1000.0,
            "close_price": 2550.0,
        }
        rows = _holdings_rows("ZG0790", self._D, [holding], self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["qty"] == 10, (
            f"Unsold holding: qty must be 10; got {rows[0]['qty']}"
        )

    def test_quantity_field_absent_falls_back_to_opening_quantity(self):
        """When `quantity` key is missing, fall back to `opening_quantity`."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        holding = {
            "tradingsymbol": "SBIN",
            "exchange": "NSE",
            # `quantity` key intentionally absent (older broker payload)
            "opening_quantity": 25,
            "average_price": 800.0,
            "last_price": 850.0,
            "day_change": 50.0,
            "pnl": 1250.0,
            "close_price": 820.0,
        }
        rows = _holdings_rows("ZG0790", self._D, [holding], self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["qty"] == 25, (
            f"Missing `quantity` key: must fall back to opening_quantity=25; got {rows[0]['qty']}"
        )


# ---------------------------------------------------------------------------
# Fix P1-B — previous_close must fall back to ltp_val when both prev_ltp_map
# and close_price are unavailable, preventing NULL in DB which causes the
# |ltp-close|≤0.005 post-settlement guard to route to stale day_change_val=0.
# ---------------------------------------------------------------------------

class TestHoldingsRowPreviousCloseLtpFallback:
    """Regression tests for Fix P1-B: previous_close must use ltp_val as
    last-resort fallback when no prior close exists.

    Affected path: _holdings_rows() previous_close field, line 473-476.
    When previous_close=None reaches the DB, _build_holding_row_from_snapshot
    in holdings.py:147 falls back to close_price_f=ltp_f, which then triggers
    the |ltp-close|<=0.005 post-settlement guard -> routes to day_change_val=0.
    The ltp_val fallback ensures same-day buys get previous_close=ltp ->
    day P&L = 0, which is correct.
    """

    _D = date(2026, 8, 19)
    _NOW_EOD = datetime(2026, 8, 19, 23, 35)

    def test_previous_close_falls_back_to_ltp_when_no_close_price(self):
        """When close_price is 0/absent AND prev_ltp_map is empty,
        previous_close must be ltp_val (not None).

        This is the same-day-buy case: no prior session exists, so
        previous_close = current ltp -> day P&L = (ltp - ltp) * qty = 0.
        """
        from backend.api.algo.daily_snapshot import _holdings_rows

        holding = {
            "tradingsymbol": "NEWBUY",
            "exchange": "NSE",
            "quantity": 20,
            "opening_quantity": 0,
            "average_price": 500.0,
            "last_price": 510.0,
            "day_change": 10.0,
            "pnl": 200.0,
            "close_price": 0,         # no prior close — same-day buy
        }
        rows = _holdings_rows("ZG0790", self._D, [holding], self._NOW_EOD,
                               prev_ltp_map=None)
        assert len(rows) == 1
        assert rows[0]["previous_close"] is not None, (
            "previous_close must not be None when close_price=0 and no prev_ltp_map — "
            "must fall back to ltp_val to prevent NULL triggering 0-P&L guard in holdings.py"
        )
        assert rows[0]["previous_close"] == pytest.approx(510.0), (
            f"previous_close must equal ltp_val=510.0 when no prior close; "
            f"got {rows[0]['previous_close']}"
        )

    def test_previous_close_ltp_fallback_produces_zero_day_pnl(self):
        """When previous_close == ltp (same-day buy fallback), day P&L = 0.

        This is the semantically correct result: bought today, no prior session,
        so day gain = 0 (entry and close are the same).
        """
        from backend.api.algo.daily_snapshot import _holdings_rows

        holding = {
            "tradingsymbol": "FRESHBUY",
            "exchange": "NSE",
            "quantity": 10,
            "opening_quantity": 0,
            "average_price": 1000.0,
            "last_price": 1000.0,
            "day_change": 0.0,
            "pnl": 0.0,
            "close_price": 0,
        }
        rows = _holdings_rows("ZG0790", self._D, [holding], self._NOW_EOD,
                               prev_ltp_map=None)
        assert len(rows) == 1
        # previous_close = ltp_val = 1000, so day P&L formula = (1000-1000)*10 = 0
        assert rows[0]["previous_close"] == pytest.approx(1000.0)

    def test_prev_ltp_map_takes_priority_over_ltp_fallback(self):
        """prev_ltp_map value wins over the ltp_val fallback."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        holding = {
            "tradingsymbol": "HDFCBANK",
            "exchange": "NSE",
            "quantity": 5,
            "opening_quantity": 5,
            "average_price": 1600.0,
            "last_price": 1650.0,
            "day_change": 50.0,
            "pnl": 250.0,
            "close_price": 0,          # broker close absent
        }
        # prev_ltp_map provides the real prior session LTP
        prev_ltp_map = {("ZG0790", "HDFCBANK", "holdings"): 1620.0}
        rows = _holdings_rows("ZG0790", self._D, [holding], self._NOW_EOD,
                               prev_ltp_map=prev_ltp_map)
        assert len(rows) == 1
        assert rows[0]["previous_close"] == pytest.approx(1620.0), (
            f"prev_ltp_map should win over ltp_val fallback; "
            f"got {rows[0]['previous_close']}"
        )

    def test_close_price_takes_priority_over_ltp_fallback(self):
        """A valid close_price from broker wins over the ltp_val fallback."""
        from backend.api.algo.daily_snapshot import _holdings_rows

        holding = {
            "tradingsymbol": "WIPRO",
            "exchange": "NSE",
            "quantity": 8,
            "opening_quantity": 8,
            "average_price": 400.0,
            "last_price": 420.0,
            "day_change": 20.0,
            "pnl": 160.0,
            "close_price": 410.0,      # valid broker close
        }
        rows = _holdings_rows("ZG0790", self._D, [holding], self._NOW_EOD,
                               prev_ltp_map=None)
        assert len(rows) == 1
        assert rows[0]["previous_close"] == pytest.approx(410.0), (
            f"close_price=410 must win over ltp_val fallback; "
            f"got {rows[0]['previous_close']}"
        )
