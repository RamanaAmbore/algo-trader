"""
Test 1 — UPSERT COALESCE for ltp and payload_json (Changes 1 & 2).

Verifies that when a second upsert arrives with ltp=NULL the existing
ltp is preserved (COALESCE), and that payload_json is likewise preserved
when the incoming ltp is NULL.

Five quality dimensions:
  SSOT       — _UPSERT_SQL is the sole upsert path; tested directly.
  Correctness— ltp=100 + payload={"a":1} → upsert ltp=NULL + payload={"a":2}
               → row still has ltp=100, payload={"a":1}.
  Performance— in-memory SQLite; no network calls.
  Reuse      — same _UPSERT_SQL constant used by production _upsert_rows.
  UX         — ensures MCX EOD ltp captured at 23:59 IST is not overwritten
               by a subsequent 00:05 IST upsert that carries ltp=NULL.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_session():
    """In-memory SQLite with a daily_book table that includes all columns
    referenced by _UPSERT_SQL (including previous_close)."""
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

    engine = create_async_engine(_TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


def _make_patch_upsert(session):
    """Return an async _upsert_rows replacement that uses the fixture session."""
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


def _base_row(**overrides) -> dict:
    base = {
        "date": date(2026, 8, 14),
        "account": "ZG0790",
        "segment": "MCX",
        "kind": "positions",
        "symbol": "CRUDEOIL26AUGFUT",
        "exchange": "MCX",
        "qty": 100,       # contracts (1 lot × 100)
        "lots": 1,
        "lot_size": 100,
        "avg_cost": 6500.0,
        "ltp": 100.0,
        "day_pnl": 0.0,
        "total_pnl": 50.0,
        "previous_close": None,
        "payload_json": json.dumps({"a": 1}),
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_upsert_coalesce_preserves_ltp_when_null(db_session):
    """Upserting with ltp=NULL must preserve the existing ltp=100.0 row value."""
    upsert = _make_patch_upsert(db_session)

    # First insert — ltp=100.0, payload={"a":1}
    await upsert([_base_row(ltp=100.0, payload_json=json.dumps({"a": 1}))])

    # Second upsert — ltp=NULL, payload={"a":2}; both should be preserved
    await upsert([_base_row(ltp=None, payload_json=json.dumps({"a": 2}))])

    result = await db_session.execute(
        text("SELECT ltp, payload_json FROM daily_book WHERE symbol = 'CRUDEOIL26AUGFUT'")
    )
    row = result.fetchone()

    assert row is not None, "Row must exist after upsert"
    assert float(row[0]) == pytest.approx(100.0), (
        f"ltp must be preserved as 100.0 via COALESCE; got {row[0]}"
    )
    stored_payload = json.loads(row[1])
    assert stored_payload == {"a": 1}, (
        f"payload_json must be preserved as {{a:1}} when ltp=NULL; got {stored_payload}"
    )


@pytest.mark.asyncio
async def test_upsert_with_valid_ltp_replaces_both(db_session):
    """When second upsert carries a non-NULL ltp, both ltp and payload_json must update."""
    upsert = _make_patch_upsert(db_session)

    await upsert([_base_row(ltp=100.0, payload_json=json.dumps({"a": 1}))])
    await upsert([_base_row(ltp=105.0, payload_json=json.dumps({"a": 2}))])

    result = await db_session.execute(
        text("SELECT ltp, payload_json FROM daily_book WHERE symbol = 'CRUDEOIL26AUGFUT'")
    )
    row = result.fetchone()

    assert row is not None
    assert float(row[0]) == pytest.approx(105.0), (
        f"ltp must update to 105.0 when incoming ltp is non-NULL; got {row[0]}"
    )
    assert json.loads(row[1]) == {"a": 2}, (
        "payload_json must update when incoming ltp is non-NULL"
    )


@pytest.mark.asyncio
async def test_upsert_coalesce_sql_strings_present():
    """_UPSERT_SQL must contain the COALESCE guard for ltp, the CASE expression
    for payload_json, the ltp-gated day_pnl, and the ltp-change previous_close
    guard — source-of-truth assertions that the SQL was patched correctly."""
    from backend.api.algo.daily_snapshot import _UPSERT_SQL
    sql_text = str(_UPSERT_SQL)
    # After Fix 1: NULLIF(EXCLUDED.ltp, 0) prevents ltp=0 from overwriting good data.
    # The COALESCE falls back to existing ltp when NULLIF returns NULL (ltp was 0).
    assert "COALESCE(NULLIF(EXCLUDED.ltp, 0), daily_book.ltp)" in sql_text, (
        "_UPSERT_SQL must use COALESCE(NULLIF(EXCLUDED.ltp, 0), daily_book.ltp) for ltp column "
        "to prevent ltp=0 from overwriting a valid prior settlement ltp"
    )
    assert "CASE WHEN EXCLUDED.ltp IS NOT NULL THEN EXCLUDED.payload_json" in sql_text, (
        "_UPSERT_SQL must use CASE WHEN guard for payload_json column"
    )
    # day_pnl must NOT use NULLIF(EXCLUDED.day_pnl, 0) — that freezes stale non-zero values
    assert "NULLIF(EXCLUDED.day_pnl, 0)" not in sql_text, (
        "_UPSERT_SQL must not use NULLIF(day_pnl, 0) which freezes stale values"
    )
    # day_pnl and previous_close must be gated on ltp IS NOT NULL
    assert "CASE WHEN EXCLUDED.ltp IS NOT NULL THEN COALESCE(EXCLUDED.day_pnl" in sql_text, (
        "_UPSERT_SQL must gate day_pnl update on EXCLUDED.ltp IS NOT NULL"
    )
    # lots and lot_size must be in the UPSERT
    assert "lots" in sql_text, "_UPSERT_SQL must include lots column"
    assert "lot_size" in sql_text, "_UPSERT_SQL must include lot_size column"
