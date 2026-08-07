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

    def test_holdings_previous_close_none_when_absent(self):
        """Test that previous_close is None when close_price is absent."""
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
        assert rows[0]["previous_close"] is None, \
            f"expected previous_close=None when close_price missing, got {rows[0]['previous_close']}"

    def test_holdings_previous_close_none_when_zero(self):
        """Test that previous_close is None when close_price is zero."""
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
        assert rows[0]["previous_close"] is None, \
            f"expected previous_close=None when close_price=0, got {rows[0]['previous_close']}"

    def test_holdings_previous_close_none_when_none(self):
        """Test that previous_close is None when close_price is explicitly None."""
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
        assert rows[0]["previous_close"] is None, \
            f"expected previous_close=None when close_price is None, got {rows[0]['previous_close']}"

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
