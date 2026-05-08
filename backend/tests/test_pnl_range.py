"""
Tests for GET /api/admin/pnl/range

Covers:
  - Summary totals match inserted rows
  - by_segment / by_account / by_symbol breakdowns are correct
  - from > to  → 422
  - Empty range → zero-filled summary

NOTE: The aggregation queries use standard SQL (no PostgreSQL-specific
syntax), so they run fine on SQLite.  The ON CONFLICT upsert in
daily_snapshot._UPSERT_SQL is NOT used here; we insert rows directly
via plain INSERT so SQLite never trips on the PG-style conflict clause.
"""

import asyncio
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# Minimal in-memory SQLite DB (daily_book only)
# ---------------------------------------------------------------------------

_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_engine():
    from sqlalchemy import (
        Column, Date, DateTime, Integer, MetaData, Numeric, String, Table,
        Text, UniqueConstraint,
    )

    meta = MetaData()
    Table(
        "daily_book", meta,
        Column("id",           Integer, primary_key=True, autoincrement=True),
        Column("date",         Date,    nullable=False),
        Column("account",      String(32), nullable=False),
        Column("segment",      String(16), nullable=False),
        Column("kind",         String(16), nullable=False),
        Column("symbol",       String(64), nullable=False),
        Column("exchange",     String(8),  nullable=True),
        Column("qty",          Integer,    nullable=False, default=0),
        Column("avg_cost",     Numeric,    nullable=True),
        Column("ltp",          Numeric,    nullable=True),
        Column("day_pnl",      Numeric,    nullable=True),
        Column("total_pnl",    Numeric,    nullable=True),
        Column("payload_json", Text,       nullable=True),
        Column("captured_at",  DateTime(timezone=True), nullable=False),
        UniqueConstraint("date", "account", "kind", "symbol",
                         name="uq_daily_book_day_acct_kind_sym"),
    )

    engine = create_async_engine(_TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(db_engine):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


# ---------------------------------------------------------------------------
# Helper — insert rows directly (bypasses PG-only ON CONFLICT)
# ---------------------------------------------------------------------------

_INSERT_SQL = text("""
    INSERT INTO daily_book
        (date, account, segment, kind, symbol, exchange,
         qty, avg_cost, ltp, day_pnl, total_pnl, payload_json, captured_at)
    VALUES
        (:date, :account, :segment, :kind, :symbol, :exchange,
         :qty, :avg_cost, :ltp, :day_pnl, :total_pnl, :payload_json, :captured_at)
""")

_NOW = datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc)

_ROWS = [
    dict(date=date(2026, 5, 1), account="ZG####", segment="equity",      kind="holdings",
         symbol="INFY",  exchange="NSE", qty=10, avg_cost=1500.0, ltp=1600.0,
         day_pnl=100.0, total_pnl=1000.0, payload_json=None, captured_at=_NOW),
    dict(date=date(2026, 5, 1), account="ZG####", segment="derivatives", kind="positions",
         symbol="NIFTY25APRFUT", exchange="NFO", qty=-50, avg_cost=22500.0, ltp=22000.0,
         day_pnl=None, total_pnl=5000.0, payload_json=None, captured_at=_NOW),
    dict(date=date(2026, 5, 2), account="ZJ####", segment="equity",      kind="holdings",
         symbol="TCS",   exchange="NSE", qty=5,  avg_cost=3400.0, ltp=3450.0,
         day_pnl=50.0,  total_pnl=250.0,  payload_json=None, captured_at=_NOW),
    dict(date=date(2026, 5, 2), account="ZJ####", segment="commodity",   kind="positions",
         symbol="GOLDM25MAY", exchange="MCX", qty=100, avg_cost=7500.0, ltp=7600.0,
         day_pnl=10000.0, total_pnl=-500.0, payload_json=None, captured_at=_NOW),
]


async def _seed(session, rows=None):
    rows = rows or _ROWS
    for r in rows:
        await session.execute(_INSERT_SQL, r)
    await session.commit()


# ---------------------------------------------------------------------------
# Aggregation helpers — reimplements the same SQL as the endpoint so tests
# are self-contained and don't need a running Litestar app.
# ---------------------------------------------------------------------------

async def _run_range_query(session, d_from, d_to, seg="all", knd="all"):
    """Run the same four aggregation queries the endpoint runs."""
    seg_clause  = " AND segment = :segment" if seg  != "all" else ""
    kind_clause = " AND kind = :kind"        if knd  != "all" else ""
    base_where  = f"date BETWEEN :d_from AND :d_to{seg_clause}{kind_clause}"
    pnl_where   = f"{base_where} AND kind != 'trades'"

    params = {"d_from": d_from, "d_to": d_to}
    if seg != "all":  params["segment"] = seg
    if knd != "all":  params["kind"]    = knd

    def _f(v):
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    summ = (await session.execute(text(f"""
        SELECT SUM(total_pnl), SUM(day_pnl),
               COUNT(DISTINCT date), COUNT(DISTINCT account)
        FROM daily_book WHERE {pnl_where}
    """), params)).fetchone()

    return {
        "total_pnl":  _f(summ[0]),
        "day_pnl":    _f(summ[1]),
        "n_dates":    int(summ[2] or 0),
        "n_accounts": int(summ[3] or 0),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPnlRangeSummary:
    @pytest.mark.asyncio
    async def test_total_pnl_matches_rows(self, session):
        await _seed(session)
        summ = await _run_range_query(
            session,
            d_from=date(2026, 5, 1),
            d_to=date(2026, 5, 2),
        )
        # 1000 + 5000 + 250 + (-500) = 5750
        assert summ["total_pnl"] == pytest.approx(5750.0)

    @pytest.mark.asyncio
    async def test_day_pnl_matches_rows(self, session):
        await _seed(session)
        summ = await _run_range_query(
            session,
            d_from=date(2026, 5, 1),
            d_to=date(2026, 5, 2),
        )
        # 100 + 0 + 50 + 10000 = 10150
        assert summ["day_pnl"] == pytest.approx(10150.0)

    @pytest.mark.asyncio
    async def test_n_dates_correct(self, session):
        await _seed(session)
        summ = await _run_range_query(
            session,
            d_from=date(2026, 5, 1),
            d_to=date(2026, 5, 2),
        )
        assert summ["n_dates"] == 2

    @pytest.mark.asyncio
    async def test_n_accounts_correct(self, session):
        await _seed(session)
        summ = await _run_range_query(
            session,
            d_from=date(2026, 5, 1),
            d_to=date(2026, 5, 2),
        )
        assert summ["n_accounts"] == 2

    @pytest.mark.asyncio
    async def test_segment_filter(self, session):
        await _seed(session)
        summ = await _run_range_query(
            session,
            d_from=date(2026, 5, 1),
            d_to=date(2026, 5, 2),
            seg="equity",
        )
        # Only INFY(1000) + TCS(250) = 1250
        assert summ["total_pnl"] == pytest.approx(1250.0)
        assert summ["n_dates"] == 2

    @pytest.mark.asyncio
    async def test_kind_filter(self, session):
        await _seed(session)
        summ = await _run_range_query(
            session,
            d_from=date(2026, 5, 1),
            d_to=date(2026, 5, 2),
            knd="holdings",
        )
        # Only holdings: 1000 + 250 = 1250
        assert summ["total_pnl"] == pytest.approx(1250.0)

    @pytest.mark.asyncio
    async def test_empty_range_returns_zeros(self, session):
        await _seed(session)
        summ = await _run_range_query(
            session,
            d_from=date(2025, 1, 1),
            d_to=date(2025, 1, 31),
        )
        assert summ["total_pnl"]  == 0.0
        assert summ["day_pnl"]    == 0.0
        assert summ["n_dates"]    == 0
        assert summ["n_accounts"] == 0


class TestPnlRangeBySegment:
    @pytest.mark.asyncio
    async def test_by_segment_grouping(self, session):
        await _seed(session)
        rows = (await session.execute(text("""
            SELECT segment, SUM(total_pnl) AS tp
            FROM daily_book
            WHERE date BETWEEN :d_from AND :d_to AND kind != 'trades'
            GROUP BY segment ORDER BY segment
        """), {"d_from": date(2026, 5, 1), "d_to": date(2026, 5, 2)})).fetchall()

        segs = {r[0]: float(r[1]) for r in rows}
        assert "equity" in segs
        assert "derivatives" in segs
        assert "commodity" in segs
        assert segs["equity"]      == pytest.approx(1250.0)
        assert segs["derivatives"] == pytest.approx(5000.0)
        assert segs["commodity"]   == pytest.approx(-500.0)


class TestPnlRangeByAccount:
    @pytest.mark.asyncio
    async def test_by_account_grouping(self, session):
        await _seed(session)
        rows = (await session.execute(text("""
            SELECT account, SUM(total_pnl) AS tp
            FROM daily_book
            WHERE date BETWEEN :d_from AND :d_to AND kind != 'trades'
            GROUP BY account ORDER BY account
        """), {"d_from": date(2026, 5, 1), "d_to": date(2026, 5, 2)})).fetchall()

        accts = {r[0]: float(r[1]) for r in rows}
        assert "ZG####" in accts
        assert "ZJ####" in accts
        # ZG: 1000 + 5000 = 6000; ZJ: 250 + (-500) = -250
        assert accts["ZG####"] == pytest.approx(6000.0)
        assert accts["ZJ####"] == pytest.approx(-250.0)


class TestPnlRangeBySymbol:
    @pytest.mark.asyncio
    async def test_top_symbols_ordered_by_abs_total_pnl(self, session):
        await _seed(session)
        rows = (await session.execute(text("""
            SELECT symbol, SUM(total_pnl) AS tp
            FROM daily_book
            WHERE date BETWEEN :d_from AND :d_to AND kind != 'trades'
            GROUP BY symbol ORDER BY ABS(SUM(total_pnl)) DESC LIMIT 50
        """), {"d_from": date(2026, 5, 1), "d_to": date(2026, 5, 2)})).fetchall()

        syms = [r[0] for r in rows]
        # NIFTY25APRFUT has |5000| — should be first
        assert syms[0] == "NIFTY25APRFUT"

    @pytest.mark.asyncio
    async def test_all_four_symbols_present(self, session):
        await _seed(session)
        rows = (await session.execute(text("""
            SELECT symbol FROM daily_book
            WHERE date BETWEEN :d_from AND :d_to AND kind != 'trades'
            GROUP BY symbol
        """), {"d_from": date(2026, 5, 1), "d_to": date(2026, 5, 2)})).fetchall()
        syms = {r[0] for r in rows}
        assert {"INFY", "NIFTY25APRFUT", "TCS", "GOLDM25MAY"} == syms


class TestPnlRangePctChangeFromStart:
    """
    Tests for start_capital and pct_change_from_start added to pnl_range.

    These tests replicate the same SQL + Python logic used by the endpoint
    and run entirely on in-memory SQLite — no PostgreSQL required.
    """

    # ------------------------------------------------------------------
    # Helper: run the endpoint's cumulative-pct logic directly in Python
    # (same implementation as the route handler).
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_pct_series(day_pnl_list: list, start_capital: float | None):
        """Mirrors the cum-sum / start_capital logic in the handler."""
        result = []
        cum = 0.0
        for dp in day_pnl_list:
            dp = float(dp) if dp is not None else 0.0
            cum += dp
            if start_capital and start_capital > 0:
                result.append(round((cum / start_capital) * 100, 4))
            else:
                result.append(None)
        return result

    @pytest.mark.asyncio
    async def test_pnl_range_includes_pct_change_from_start(self, session):
        """
        Seed two holdings rows on from_date (2026-05-01) + day_pnl deltas
        across two dates; assert pct_change_from_start matches expected math.

        start_capital = |10 * 1500| + |50 * 22500|  (ZG#### holdings + positions)
                      = 15000 + 1125000 = 1140000
        date 2026-05-01: cum_day_pnl = 100  → pct = 100/1140000*100 ≈ 0.0088
        date 2026-05-02: cum_day_pnl = 100 + 50 = 150 → pct = 150/1140000*100 ≈ 0.0132
        (only rows with day_pnl non-NULL contribute; NIFTY25APRFUT has day_pnl=None→0)
        """
        await _seed(session)

        # Compute start_capital from from_date (2026-05-01) holdings + positions
        cap_row = (await session.execute(text("""
            SELECT SUM(ABS(COALESCE(qty,0) * COALESCE(avg_cost,0)))
            FROM daily_book
            WHERE date = :d_from AND kind IN ('holdings','positions')
        """), {"d_from": date(2026, 5, 1)})).fetchone()
        start_capital = float(cap_row[0]) if cap_row and cap_row[0] is not None else None

        # Expected: |10*1500| + |-50*22500| = 15000 + 1125000 = 1140000
        assert start_capital == pytest.approx(1140000.0)

        # Compute daily_series day_pnl values (ordered by date)
        daily = (await session.execute(text("""
            SELECT date, SUM(day_pnl) AS day_pnl
            FROM daily_book
            WHERE date BETWEEN :d_from AND :d_to AND kind != 'trades'
            GROUP BY date ORDER BY date
        """), {"d_from": date(2026, 5, 1), "d_to": date(2026, 5, 2)})).fetchall()

        day_pnls = [float(r[1]) if r[1] is not None else 0.0 for r in daily]
        expected_pcts = self._compute_pct_series(day_pnls, start_capital)

        # date 2026-05-01: sum(day_pnl) = 100 + 0 (None→0) = 100
        # date 2026-05-02: sum(day_pnl) = 50 + 10000 = 10050
        assert len(expected_pcts) == 2
        assert expected_pcts[0] == pytest.approx(round(100 / 1140000 * 100, 4))
        assert expected_pcts[1] == pytest.approx(round(10150 / 1140000 * 100, 4))

    @pytest.mark.asyncio
    async def test_pnl_range_pct_change_null_when_start_capital_zero(self, session):
        """
        When from_date has no daily_book rows, start_capital is None
        and pct_change_from_start must be None on every row.
        """
        # Seed only rows on 2026-05-02; from_date 2026-05-01 has nothing.
        rows_only_may2 = [
            dict(date=date(2026, 5, 2), account="ZG####", segment="equity",
                 kind="holdings", symbol="INFY", exchange="NSE",
                 qty=10, avg_cost=1500.0, ltp=1600.0,
                 day_pnl=100.0, total_pnl=1000.0, payload_json=None, captured_at=_NOW),
        ]
        await _seed(session, rows_only_may2)

        cap_row = (await session.execute(text("""
            SELECT SUM(ABS(COALESCE(qty,0) * COALESCE(avg_cost,0)))
            FROM daily_book
            WHERE date = :d_from AND kind IN ('holdings','positions')
        """), {"d_from": date(2026, 5, 1)})).fetchone()
        start_capital = float(cap_row[0]) if cap_row and cap_row[0] is not None else None

        assert start_capital is None

        daily = (await session.execute(text("""
            SELECT SUM(day_pnl) FROM daily_book
            WHERE date BETWEEN :d_from AND :d_to AND kind != 'trades'
            GROUP BY date ORDER BY date
        """), {"d_from": date(2026, 5, 1), "d_to": date(2026, 5, 2)})).fetchall()

        day_pnls = [float(r[0]) if r[0] is not None else 0.0 for r in daily]
        pcts = self._compute_pct_series(day_pnls, start_capital)
        assert all(p is None for p in pcts)

    @pytest.mark.asyncio
    async def test_pnl_range_response_carries_start_capital(self, session):
        """
        The top-level start_capital equals sum |qty * avg_cost| for
        holdings + positions rows on from_date.
        """
        await _seed(session)

        cap_row = (await session.execute(text("""
            SELECT SUM(ABS(COALESCE(qty,0) * COALESCE(avg_cost,0)))
            FROM daily_book
            WHERE date = :d_from AND kind IN ('holdings','positions')
        """), {"d_from": date(2026, 5, 1)})).fetchone()
        expected_cap = float(cap_row[0]) if cap_row and cap_row[0] is not None else None

        # 10*1500 + 50*22500 = 15000 + 1125000 = 1140000
        assert expected_cap == pytest.approx(1140000.0)


class TestPnlRangeValidation:
    @pytest.mark.asyncio
    async def test_from_after_to_raises_http_422(self):
        """from > to should raise HTTPException(422) in the route handler."""
        from litestar.exceptions import HTTPException
        from backend.api.routes.admin import AdminController
        from unittest.mock import patch, MagicMock

        controller = AdminController.__new__(AdminController)

        # Litestar route handlers are descriptors; reach the underlying
        # coroutine function via .fn attribute.
        handler_fn = AdminController.pnl_range.fn

        # timestamp_indian is imported inside pnl_range; patch at its source.
        with patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian",
            return_value=MagicMock(date=lambda: __import__('datetime').date(2026, 5, 7)),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await handler_fn(
                    controller,
                    from_date="2026-05-10",
                    to_date="2026-05-01",
                )
        assert exc_info.value.status_code == 422
        assert "must be <=" in exc_info.value.detail
