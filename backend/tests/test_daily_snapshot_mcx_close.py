"""
Tests for the day_pnl COALESCE-NULLIF UPSERT guard in backend/api/algo/daily_snapshot.py

Covers:
  - MCX post-settlement zero-overwrite guard: day_pnl = COALESCE(NULLIF(EXCLUDED.day_pnl, 0), daily_book.day_pnl)
  - First-write stale guard: day_pnl = NULL on mid-session writes does NOT overwrite existing non-NULL
  - Update on new nonzero values: a subsequent write with a new nonzero day_pnl DOES update correctly
  - Interaction with total_pnl and other fields (fields not protected still update normally)

Quality dimensions:
  1. SSOT        — _UPSERT_SQL is defined once in daily_snapshot.py; SQL logic is tested here
  2. Performance — UPSERT is single-pass; no extra roundtrips
  3. Stale code  — COALESCE(NULLIF(...)) pattern matches the comment about MCX settlement
  4. Reusable    — Test fixture is reused for all four test cases
  5. UX          — operator sees correct day_pnl after post-settlement writes
"""

import json
from datetime import date, datetime, timezone
from decimal import Decimal

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

    Schema matches backend/api/models.py:DailyBook. Includes the previous_close
    COALESCE guard as well, so we can verify that both freezing patterns work.
    """
    from sqlalchemy import MetaData, Table, Column, Integer, String, Text, UniqueConstraint
    from sqlalchemy import Date, DateTime, Numeric, Float

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
        Column("previous_close", Float, nullable=True),
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


def _get_upsert_sql():
    """Return the _UPSERT_SQL statement from daily_snapshot.py.

    Imported here to ensure we're testing the actual UPSERT logic, not a copy.
    """
    from backend.api.algo.daily_snapshot import _UPSERT_SQL
    return _UPSERT_SQL


async def _upsert_rows(session: AsyncSession, rows: list[dict]) -> int:
    """Upsert rows using the actual _UPSERT_SQL from daily_snapshot.py.

    Mirrors the _upsert_rows helper in daily_snapshot.py, but uses the
    test session instead of the global async_session().
    """
    if not rows:
        return 0
    now_utc = datetime.now(timezone.utc)
    for r in rows:
        r["captured_at"] = now_utc
    await session.execute(_get_upsert_sql(), rows)
    await session.commit()
    return len(rows)


async def _fetch_row(session: AsyncSession, d: date, account: str, kind: str, symbol: str) -> dict | None:
    """Fetch a single row from daily_book by (date, account, kind, symbol)."""
    query = text("""
        SELECT date, account, kind, symbol, qty, ltp, day_pnl, total_pnl, captured_at
        FROM daily_book
        WHERE date = :date AND account = :account AND kind = :kind AND symbol = :symbol
    """)
    result = await session.execute(query, {
        "date": d,
        "account": account,
        "kind": kind,
        "symbol": symbol,
    })
    row = result.first()
    if not row:
        return None
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# Test Case 1: Zero day_pnl on update does NOT overwrite existing nonzero
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_zero_day_pnl_preserves_existing_nonzero(db_session: AsyncSession):
    """
    Insert row with day_pnl=500. Upsert same key with day_pnl=0 (e.g., post-MCX-settlement).
    Assert day_pnl remains 500 (COALESCE-NULLIF guard preserved the original).

    This is the core MCX guard: post-settlement writes send day_pnl=0; the COALESCE-NULLIF
    pattern (day_pnl = COALESCE(NULLIF(EXCLUDED.day_pnl, 0), daily_book.day_pnl)) ensures
    we keep the intra-session value.
    """
    test_date = date(2099, 1, 1)
    test_account = "ZG9999_ZERO_PRESERVE"
    test_symbol = "CRUDEOIL25JAN"

    # Initial insert: day_pnl = 500 (e.g., 9:15 IST capture during session)
    initial_row = {
        "date": test_date,
        "account": test_account,
        "segment": "commodity",
        "kind": "positions",
        "symbol": test_symbol,
        "exchange": "MCX",
        "qty": -20,
        "avg_cost": 7500.0,
        "ltp": 7480.0,
        "day_pnl": 500.0,  # intra-session P&L
        "total_pnl": 1500.0,
        "previous_close": None,
        "payload_json": json.dumps({"exchange": "MCX"}),
    }
    await _upsert_rows(db_session, [initial_row])

    # Verify initial row
    row = await _fetch_row(db_session, test_date, test_account, "positions", test_symbol)
    assert row is not None, "initial insert failed"
    assert float(row["day_pnl"]) == 500.0, f"expected day_pnl=500.0, got {row['day_pnl']}"

    # Second write (e.g., post-MCX-settlement 23:35 IST): day_pnl=0 (broker reset)
    update_row = {
        "date": test_date,
        "account": test_account,
        "segment": "commodity",
        "kind": "positions",
        "symbol": test_symbol,
        "exchange": "MCX",
        "qty": -20,
        "avg_cost": 7500.0,
        "ltp": 7480.0,
        "day_pnl": 0.0,  # post-settlement reset — should NOT overwrite
        "total_pnl": 1500.0,
        "previous_close": None,
        "payload_json": json.dumps({"exchange": "MCX", "settled": True}),
    }
    await _upsert_rows(db_session, [update_row])

    # Verify that day_pnl is still 500.0, not 0.0
    row = await _fetch_row(db_session, test_date, test_account, "positions", test_symbol)
    assert row is not None, "row vanished after update"
    assert float(row["day_pnl"]) == 500.0, (
        f"expected day_pnl=500.0 (preserved from initial), got {row['day_pnl']}. "
        "COALESCE-NULLIF guard failed — zero overwrote the original value."
    )


# ---------------------------------------------------------------------------
# Test Case 2: NULL day_pnl on update does NOT overwrite existing nonzero
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_null_day_pnl_preserves_existing_nonzero(db_session: AsyncSession):
    """
    Insert row with day_pnl=500. Upsert same key with day_pnl=None (e.g., mid-session write).
    Assert day_pnl remains 500 (stale guard: first-write wins).

    Mid-session writes emit day_pnl=None to avoid polluting the close-override path in positions.py.
    The stale guard via COALESCE should preserve the existing value.
    """
    test_date = date(2099, 1, 2)
    test_account = "ZG9999_NULL_PRESERVE"
    test_symbol = "GOLDGULD25JAN"

    # Initial insert (9:15 IST): day_pnl = 500
    initial_row = {
        "date": test_date,
        "account": test_account,
        "segment": "commodity",
        "kind": "positions",
        "symbol": test_symbol,
        "exchange": "MCX",
        "qty": 10,
        "avg_cost": 6500.0,
        "ltp": 6550.0,
        "day_pnl": 500.0,
        "total_pnl": 2000.0,
        "previous_close": None,
        "payload_json": json.dumps({"exchange": "MCX"}),
    }
    await _upsert_rows(db_session, [initial_row])

    row = await _fetch_row(db_session, test_date, test_account, "positions", test_symbol)
    assert float(row["day_pnl"]) == 500.0, "initial insert failed"

    # Second write (11:00 IST, mid-session): day_pnl=None
    update_row = {
        "date": test_date,
        "account": test_account,
        "segment": "commodity",
        "kind": "positions",
        "symbol": test_symbol,
        "exchange": "MCX",
        "qty": 10,
        "avg_cost": 6500.0,
        "ltp": 6560.0,
        "day_pnl": None,  # mid-session write carries None to preserve existing
        "total_pnl": 2100.0,
        "previous_close": None,
        "payload_json": json.dumps({"exchange": "MCX", "mid_session": True}),
    }
    await _upsert_rows(db_session, [update_row])

    # Verify day_pnl is still 500.0
    row = await _fetch_row(db_session, test_date, test_account, "positions", test_symbol)
    assert float(row["day_pnl"]) == 500.0, (
        f"expected day_pnl=500.0 (stale guard preserved), got {row['day_pnl']}. "
        "NULL overwrite should not happen."
    )


# ---------------------------------------------------------------------------
# Test Case 3: New nonzero day_pnl on update DOES update correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_nonzero_day_pnl_updates(db_session: AsyncSession):
    """
    Insert row with day_pnl=500. Upsert same key with day_pnl=750 (a new real value).
    Assert day_pnl updates to 750 (COALESCE-NULLIF allows real changes).

    The guard must only preserve existing values when the new value is 0 or NULL.
    When the new value is nonzero, it should update normally.
    """
    test_date = date(2099, 1, 3)
    test_account = "ZG9999_UPDATE_NEW"
    test_symbol = "SILVSILV25JAN"

    # Initial insert: day_pnl = 500
    initial_row = {
        "date": test_date,
        "account": test_account,
        "segment": "commodity",
        "kind": "positions",
        "symbol": test_symbol,
        "exchange": "MCX",
        "qty": 5,
        "avg_cost": 8000.0,
        "ltp": 8100.0,
        "day_pnl": 500.0,
        "total_pnl": 3000.0,
        "previous_close": None,
        "payload_json": json.dumps({"exchange": "MCX"}),
    }
    await _upsert_rows(db_session, [initial_row])

    row = await _fetch_row(db_session, test_date, test_account, "positions", test_symbol)
    assert float(row["day_pnl"]) == 500.0, "initial insert failed"

    # Second write: day_pnl = 750 (updated real value, not 0 or NULL)
    update_row = {
        "date": test_date,
        "account": test_account,
        "segment": "commodity",
        "kind": "positions",
        "symbol": test_symbol,
        "exchange": "MCX",
        "qty": 5,
        "avg_cost": 8000.0,
        "ltp": 8150.0,  # LTP moved, day_pnl should reflect new state
        "day_pnl": 750.0,  # real update — should propagate
        "total_pnl": 3250.0,
        "previous_close": None,
        "payload_json": json.dumps({"exchange": "MCX", "updated": True}),
    }
    await _upsert_rows(db_session, [update_row])

    # Verify day_pnl updated to 750
    row = await _fetch_row(db_session, test_date, test_account, "positions", test_symbol)
    assert float(row["day_pnl"]) == 750.0, (
        f"expected day_pnl=750.0 (new real value), got {row['day_pnl']}. "
        "Guard should allow nonzero updates."
    )


# ---------------------------------------------------------------------------
# Test Case 4: NULL on first write, then nonzero on second write
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_first_write_null_then_nonzero(db_session: AsyncSession):
    """
    Insert row with day_pnl=None (e.g., first mid-session write before settlement).
    Upsert same key with day_pnl=300 (e.g., final snapshot at 23:35 IST).
    Assert day_pnl updates to 300 (first-write NULL → settled nonzero is OK).

    This ensures that the guard doesn't prevent the FIRST real value from being written.
    """
    test_date = date(2099, 1, 4)
    test_account = "ZG9999_NULL_THEN_VALUE"
    test_symbol = "NICKELKA25JAN"

    # Initial insert (mid-session): day_pnl = None
    initial_row = {
        "date": test_date,
        "account": test_account,
        "segment": "commodity",
        "kind": "positions",
        "symbol": test_symbol,
        "exchange": "MCX",
        "qty": 2,
        "avg_cost": 2500.0,
        "ltp": 2550.0,
        "day_pnl": None,  # mid-session → no P&L captured
        "total_pnl": 500.0,
        "previous_close": None,
        "payload_json": json.dumps({"exchange": "MCX", "partial": True}),
    }
    await _upsert_rows(db_session, [initial_row])

    row = await _fetch_row(db_session, test_date, test_account, "positions", test_symbol)
    assert row["day_pnl"] is None, "initial insert with None failed"

    # Second write (23:35 IST, post-settlement): day_pnl = 300
    update_row = {
        "date": test_date,
        "account": test_account,
        "segment": "commodity",
        "kind": "positions",
        "symbol": test_symbol,
        "exchange": "MCX",
        "qty": 2,
        "avg_cost": 2500.0,
        "ltp": 2550.0,
        "day_pnl": 300.0,  # settled value — should overwrite None
        "total_pnl": 500.0,
        "previous_close": None,
        "payload_json": json.dumps({"exchange": "MCX", "settled": True}),
    }
    await _upsert_rows(db_session, [update_row])

    # Verify day_pnl updated to 300
    row = await _fetch_row(db_session, test_date, test_account, "positions", test_symbol)
    assert float(row["day_pnl"]) == 300.0, (
        f"expected day_pnl=300.0 (NULL → nonzero), got {row['day_pnl']}. "
        "Guard should allow first real value to be written."
    )


# ---------------------------------------------------------------------------
# Test Case 5: Other fields (total_pnl, ltp) still update normally
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_other_fields_update_normally(db_session: AsyncSession):
    """
    Verify that the day_pnl guard does NOT affect other fields.
    They should update normally on every upsert, regardless of day_pnl guard.

    This ensures we didn't accidentally freeze other fields.
    """
    test_date = date(2099, 1, 5)
    test_account = "ZG9999_OTHER_FIELDS"
    test_symbol = "ZINCZING25JAN"

    # Initial insert
    initial_row = {
        "date": test_date,
        "account": test_account,
        "segment": "commodity",
        "kind": "positions",
        "symbol": test_symbol,
        "exchange": "MCX",
        "qty": 100,
        "avg_cost": 500.0,
        "ltp": 520.0,
        "day_pnl": 1000.0,
        "total_pnl": 5000.0,
        "previous_close": None,
        "payload_json": json.dumps({"exchange": "MCX"}),
    }
    await _upsert_rows(db_session, [initial_row])

    row = await _fetch_row(db_session, test_date, test_account, "positions", test_symbol)
    assert float(row["ltp"]) == 520.0
    assert float(row["total_pnl"]) == 5000.0

    # Update with zero day_pnl but different ltp/total_pnl
    # (simulates post-settlement: day_pnl frozen, but other fields reflect new market state)
    update_row = {
        "date": test_date,
        "account": test_account,
        "segment": "commodity",
        "kind": "positions",
        "symbol": test_symbol,
        "exchange": "MCX",
        "qty": 100,
        "avg_cost": 500.0,
        "ltp": 525.0,  # should update
        "day_pnl": 0.0,  # should be preserved from initial (1000.0)
        "total_pnl": 6000.0,  # should update
        "previous_close": None,
        "payload_json": json.dumps({"exchange": "MCX", "updated": True}),
    }
    await _upsert_rows(db_session, [update_row])

    row = await _fetch_row(db_session, test_date, test_account, "positions", test_symbol)
    assert float(row["ltp"]) == 525.0, "ltp should update even when day_pnl is frozen"
    assert float(row["total_pnl"]) == 6000.0, "total_pnl should update even when day_pnl is frozen"
    assert float(row["day_pnl"]) == 1000.0, "day_pnl should still be frozen from initial"


# ---------------------------------------------------------------------------
# Test Case 6: Verify SSOT — SQL is imported from daily_snapshot module
# ---------------------------------------------------------------------------

def test_upsert_sql_comes_from_daily_snapshot():
    """Verify that _UPSERT_SQL is defined in daily_snapshot.py (not mocked)."""
    from backend.api.algo import daily_snapshot
    import inspect

    # _UPSERT_SQL must exist
    assert hasattr(daily_snapshot, "_UPSERT_SQL"), (
        "_UPSERT_SQL not found in backend.api.algo.daily_snapshot — "
        "check that it's defined at module level"
    )

    # Verify it's a SQLAlchemy text() object
    assert hasattr(daily_snapshot._UPSERT_SQL, "text"), (
        "_UPSERT_SQL should be a SQLAlchemy text() object"
    )

    # Verify the SQL string contains the guard pattern
    sql_str = str(daily_snapshot._UPSERT_SQL)
    assert "COALESCE" in sql_str and "NULLIF" in sql_str, (
        "COALESCE-NULLIF guard pattern missing from _UPSERT_SQL. "
        f"SQL: {sql_str}"
    )
    assert "day_pnl" in sql_str, "day_pnl field missing from _UPSERT_SQL"


# ---------------------------------------------------------------------------
# Test Case 7: Verify the guard is on day_pnl, not other fields
# ---------------------------------------------------------------------------

def test_upsert_sql_guard_on_day_pnl_not_total_pnl():
    """
    Verify that the COALESCE-NULLIF guard is ONLY on day_pnl.
    total_pnl and other fields should update normally (no guard).

    This guards against copy-paste errors where the guard gets applied to
    fields it shouldn't.
    """
    from backend.api.algo import daily_snapshot

    sql_str = str(daily_snapshot._UPSERT_SQL)

    # day_pnl should have COALESCE(NULLIF(...))
    assert "day_pnl" in sql_str, "day_pnl missing from SQL"
    # The pattern should be: day_pnl = COALESCE(NULLIF(EXCLUDED.day_pnl, 0), daily_book.day_pnl)
    # We can't easily regex-parse, but we can check that the line contains both
    lines = sql_str.split("\n")
    day_pnl_line = [l for l in lines if "day_pnl" in l and "DO UPDATE" in sql_str]

    # Check that day_pnl line (in context of UPDATE clause) has COALESCE
    full_update_section = sql_str.split("DO UPDATE SET")[1] if "DO UPDATE SET" in sql_str else ""
    assert "COALESCE(NULLIF(EXCLUDED.day_pnl" in full_update_section, (
        "day_pnl should use COALESCE(NULLIF(EXCLUDED.day_pnl, 0), daily_book.day_pnl) guard"
    )

    # total_pnl should update normally (EXCLUDED.total_pnl without guard)
    # Strip extra whitespace since SQL may vary in formatting
    assert "total_pnl" in full_update_section and "= EXCLUDED.total_pnl" in full_update_section, (
        "total_pnl should update normally without guard (no COALESCE)"
    )
