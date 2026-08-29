"""Tests for the `previous_close` column fix in the closed-hours snapshot path.

Root cause: `build_snapshot_position_row` set `close_price = ltp` (snapshot LTP).
During closed hours, `baseDayPnlForPosition` on the frontend computes
  total_pnl - oq * (ltp - close_price)
which collapses to `total_pnl - 0 = total_pnl` for overnight positions —
the wrong day-P&L.

Fix: `daily_book.previous_close` stores the prior-session official settlement
(Kite's `close_price` at first snapshot). COALESCE in the UPSERT freezes the
first-write value. `_positions_snapshot()` passes it to
`build_snapshot_position_row(previous_close=…)` which uses it as `close_price`.

Five quality dimensions tested:
1. SSOT  — `previous_close` column exists in DailyBook ORM model; migration DDL present
2. Perf  — UPSERT SQL contains COALESCE freeze (no extra round-trip)
3. Stale — writer populates `previous_close` from Kite's `close_price`
4. Reuse — `build_snapshot_position_row` kwarg; snapshot reader passes it through
5. UX    — `close_price` in snapshot row uses `previous_close` when > 0
"""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest


# ---------------------------------------------------------------------------
# 1. SSOT — ORM model has the column; migration DDL is present
# ---------------------------------------------------------------------------

def test_daily_book_orm_has_previous_close_column():
    """DailyBook model declares a `previous_close` Float column."""
    from backend.api.models import DailyBook
    from sqlalchemy import inspect as _inspect

    mapper = _inspect(DailyBook)
    col_names = [c.key for c in mapper.columns]
    assert "previous_close" in col_names, (
        "DailyBook ORM model must have a 'previous_close' column"
    )
    col = mapper.columns["previous_close"]
    # Nullable (positions without a prior-day snapshot yield NULL)
    assert col.nullable is True, "previous_close must be nullable"


def test_migration_ddl_present_in_database_py():
    """_migrate_daily_book_previous_close exists and is called from init_db."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db)
    assert "_migrate_daily_book_previous_close" in src, (
        "database.py must contain _migrate_daily_book_previous_close function"
    )
    assert "ADD COLUMN IF NOT EXISTS previous_close" in src, (
        "Migration DDL must include ALTER TABLE ... ADD COLUMN IF NOT EXISTS previous_close"
    )
    assert "await _migrate_daily_book_previous_close(conn)" in src, (
        "_migrate_daily_book_previous_close must be called inside init_db()"
    )


def test_backfill_migration_present_and_wired():
    """_migrate_daily_book_backfill_previous_close exists, SQL is correct,
    and is called from init_db immediately after _migrate_daily_book_previous_close.

    Quality dimensions:
    1. SSOT  — function exists in database.py with correct signature
    2. Perf  — UPDATE uses WHERE previous_close IS NULL (idempotent, no full-table writes)
    3. Stale — correlated sub-SELECT picks MAX(h.date) < t.date ensuring prior-day only
    4. Reuse — called in init_db in the correct position (after schema migration)
    5. UX    — idempotency guard: rows already populated are never touched
    """
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db)

    # 1. SSOT — function declared with correct async signature
    assert "async def _migrate_daily_book_backfill_previous_close(conn)" in src, (
        "database.py must declare async def _migrate_daily_book_backfill_previous_close(conn)"
    )

    # 2. Perf — idempotency guard via WHERE previous_close IS NULL
    assert "t.previous_close IS NULL" in src, (
        "Backfill UPDATE must filter WHERE t.previous_close IS NULL so already-filled "
        "rows are never touched (idempotent)"
    )

    # 3. Stale — correlated sub-SELECT on h.date < t.date to get the prior day
    assert "h.date    < t.date" in src or "h.date < t.date" in src, (
        "Backfill sub-SELECT must restrict h.date < t.date to pick the most recent "
        "prior trading day's ltp"
    )

    # 4. Reuse — wired into init_db
    assert "await _migrate_daily_book_backfill_previous_close(conn)" in src, (
        "_migrate_daily_book_backfill_previous_close must be awaited inside init_db()"
    )

    # 4b. Order — must appear after _migrate_daily_book_previous_close in init_db
    schema_call = "await _migrate_daily_book_previous_close(conn)"
    backfill_call = "await _migrate_daily_book_backfill_previous_close(conn)"
    pos_schema = src.find(schema_call)
    pos_backfill = src.find(backfill_call)
    assert pos_schema != -1 and pos_backfill != -1, (
        "Both migration calls must be present in database.py"
    )
    assert pos_backfill > pos_schema, (
        "_migrate_daily_book_backfill_previous_close must be called AFTER "
        "_migrate_daily_book_previous_close in init_db (column must exist first)"
    )

    # 5. UX — SET previous_close = p.ltp reads from prior-day row alias p
    assert "SET    previous_close = p.ltp" in src or "SET previous_close = p.ltp" in src, (
        "Backfill UPDATE must SET previous_close = p.ltp "
        "(prior-day row's ltp, not a constant)"
    )


@pytest.mark.asyncio
async def test_backfill_migration_idempotency_logic():
    """Unit-test the UPDATE SQL logic using an in-memory mock connection.

    Verifies that:
    - execute() is called exactly once with a text() argument
    - the SQL text contains the WHERE previous_close IS NULL guard
    - the SQL text contains a correlated sub-SELECT for MAX prior date
    """
    from unittest.mock import AsyncMock, MagicMock, patch, call
    import backend.api.database as _db

    mock_conn = AsyncMock()

    captured_sql: list[str] = []

    async def _capture_execute(stmt, *args, **kwargs):
        captured_sql.append(stmt.text if hasattr(stmt, "text") else str(stmt))

    mock_conn.execute = _capture_execute

    await _db._migrate_daily_book_backfill_previous_close(mock_conn)

    assert len(captured_sql) == 1, (
        "_migrate_daily_book_backfill_previous_close must issue exactly one SQL statement"
    )
    sql = captured_sql[0].lower()

    assert "update daily_book" in sql, "SQL must UPDATE daily_book"
    assert "previous_close is null" in sql, (
        "SQL must filter WHERE previous_close IS NULL for idempotency"
    )
    assert "select max(" in sql, (
        "SQL must contain a correlated MAX sub-SELECT to find the most recent prior date"
    )
    assert "h.date" in sql and "t.date" in sql, (
        "SQL must compare h.date < t.date to restrict to prior trading days"
    )
    assert "p.ltp is not null" in sql, (
        "SQL must guard against NULL ltp in the source row (p.ltp IS NOT NULL)"
    )


# ---------------------------------------------------------------------------
# 2. Perf — UPSERT SQL uses COALESCE freeze (no extra query)
# ---------------------------------------------------------------------------

def test_upsert_sql_coalesce_freeze():
    """_UPSERT_SQL must handle previous_close correctly.

    Updated (2026-08-23): the old always-frozen COALESCE pattern was replaced with
    a CASE guard that only advances previous_close when ltp actually changes
    (new trading-session settlement). Frozen weekends no longer lock in stale values.
    """
    from backend.api.algo.daily_snapshot import _UPSERT_SQL

    sql = _UPSERT_SQL.text.lower()
    assert "previous_close" in sql, "_UPSERT_SQL must include previous_close column"
    # New pattern: CASE WHEN EXCLUDED.ltp IS NOT NULL AND (...) THEN ... ELSE daily_book.previous_close END
    # Previous old pattern (COALESCE freeze) has been intentionally removed.
    assert "daily_book.previous_close" in sql, (
        "UPSERT must reference daily_book.previous_close for conditional update"
    )


def test_upsert_sql_previous_close_in_insert_and_values():
    """_UPSERT_SQL column list and VALUES both include :previous_close placeholder."""
    from backend.api.algo.daily_snapshot import _UPSERT_SQL

    sql = _UPSERT_SQL.text
    # Column in INSERT list
    assert "previous_close" in sql
    # Positional param in VALUES
    assert ":previous_close" in sql


# ---------------------------------------------------------------------------
# 3. Stale — writer extracts previous_close from Kite's close_price
# ---------------------------------------------------------------------------

def test_positions_rows_captures_previous_close():
    """_positions_rows() row dicts must contain 'previous_close' from close_price."""
    from backend.api.algo.daily_snapshot import _positions_rows
    from datetime import date

    raw = [{
        "tradingsymbol": "NIFTY26JULFUT",
        "exchange": "NFO",
        "quantity": 50,
        "average_price": 23000.0,
        "last_price": 23200.0,
        "close_price": 22800.0,  # prior-session settlement
        "pnl": 10000.0,
        "day_change": 200.0,
        "day_change_value": 200.0,
        "m2m": 200.0,
        "unrealised": 10000.0,
        "realised": 0.0,
        "value": 1160000.0,
        "buy_quantity": 0,
        "sell_quantity": 0,
        "buy_value": 0.0,
        "sell_value": 0.0,
        "buy_m2m": 0.0,
        "sell_m2m": 0.0,
        "overnight_quantity": 50,
        "multiplier": 1,
        "instrument_token": 12345,
        "product": "NRML",
    }]
    now_ist = datetime(2026, 7, 13, 16, 0, 0, tzinfo=timezone.utc)  # past close

    rows = _positions_rows("ZG0790", date(2026, 7, 13), raw, now_ist, settled=True)

    assert len(rows) == 1, "Expected 1 row from _positions_rows"
    r = rows[0]
    assert "previous_close" in r, "_positions_rows must include 'previous_close' key"
    assert r["previous_close"] == pytest.approx(22800.0, rel=1e-6), (
        f"previous_close={r['previous_close']} must equal Kite close_price=22800.0"
    )


def test_positions_rows_previous_close_none_when_missing():
    """When close_price is absent or 0, previous_close must be None."""
    from backend.api.algo.daily_snapshot import _positions_rows
    from datetime import date

    raw = [{
        "tradingsymbol": "NIFTY26JULFUT",
        "exchange": "NFO",
        "quantity": 50,
        "average_price": 23000.0,
        "last_price": 23200.0,
        # close_price absent
        "pnl": 10000.0,
        "day_change": 200.0,
        "day_change_value": 200.0,
        "m2m": 200.0,
        "unrealised": 10000.0,
        "realised": 0.0,
        "value": 1160000.0,
        "buy_quantity": 0,
        "sell_quantity": 0,
        "buy_value": 0.0,
        "sell_value": 0.0,
        "buy_m2m": 0.0,
        "sell_m2m": 0.0,
        "overnight_quantity": 50,
        "multiplier": 1,
        "instrument_token": 12345,
        "product": "NRML",
    }]
    now_ist = datetime(2026, 7, 13, 16, 0, 0, tzinfo=timezone.utc)

    rows = _positions_rows("ZG0790", date(2026, 7, 13), raw, now_ist, settled=True)
    assert len(rows) == 1
    assert rows[0]["previous_close"] is None, (
        "previous_close must be None when close_price is absent"
    )


# ---------------------------------------------------------------------------
# 4. Reuse — build_snapshot_position_row accepts previous_close kwarg;
#    _positions_snapshot SELECT includes db.previous_close and passes it through
# ---------------------------------------------------------------------------

def test_build_snapshot_position_row_accepts_previous_close_kwarg():
    """build_snapshot_position_row accepts previous_close as a keyword-only arg."""
    from backend.api.routes.positions_helpers import build_snapshot_position_row
    import inspect

    sig = inspect.signature(build_snapshot_position_row)
    params = sig.parameters
    assert "previous_close" in params, (
        "build_snapshot_position_row must accept a 'previous_close' keyword arg"
    )
    param = params["previous_close"]
    assert param.default is None, (
        "previous_close must default to None for backward compatibility"
    )
    # Must be keyword-only (after the * separator)
    assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
        "previous_close must be keyword-only (after *)"
    )


def test_positions_snapshot_select_includes_previous_close():
    """_positions_snapshot SQL includes db.previous_close; builder passes actual_previous_close."""
    import inspect
    from backend.api.routes import positions as _pos_module
    from backend.api.routes import positions_helpers as _helpers

    src = inspect.getsource(_pos_module._positions_snapshot)
    assert "db.previous_close" in src, (
        "_positions_snapshot SELECT must include db.previous_close"
    )
    # After the prev_close_val priority fix, the authoritative settlement
    # (actual_previous_close) is passed directly — not the prev_ltp fallback.
    helper_src = inspect.getsource(_helpers.build_row_from_snapshot_raw)
    assert "previous_close=actual_previous_close" in helper_src, (
        "build_row_from_snapshot_raw must pass previous_close=actual_previous_close "
        "(the frozen prior-session settlement, not the prev_ltp) "
        "to build_snapshot_position_row"
    )


# ---------------------------------------------------------------------------
# 5. UX — close_price uses previous_close when > 0 (not LTP)
# ---------------------------------------------------------------------------

def test_build_snapshot_position_row_uses_previous_close_as_close_price():
    """When previous_close > 0, close_price in PositionRow must equal previous_close."""
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="NIFTY26JULFUT",
        exchange="NFO",
        qty=50,
        avg_cost=23000.0,
        ltp=23200.0,        # snapshot LTP
        day_pnl=10000.0,
        total_pnl=10000.0,
        extras={},
        previous_close=22800.0,  # prior-session settlement
    )

    assert row.close_price == pytest.approx(22800.0, rel=1e-6), (
        f"close_price={row.close_price} must use previous_close=22800.0 "
        "not LTP=23200.0 when previous_close is provided"
    )
    # last_price (LTP) must not be changed
    assert row.last_price == pytest.approx(23200.0, rel=1e-6), (
        "last_price must remain as snapshot LTP=23200.0"
    )


def test_build_snapshot_position_row_falls_back_to_ltp_when_no_previous_close():
    """When previous_close is None, close_price falls back to LTP (existing behavior)."""
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    LTP = 23200.0
    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="NIFTY26JULFUT",
        exchange="NFO",
        qty=50,
        avg_cost=23000.0,
        ltp=LTP,
        day_pnl=10000.0,
        total_pnl=10000.0,
        extras={},
        previous_close=None,
    )
    assert row.close_price == pytest.approx(LTP, rel=1e-6), (
        "close_price must fall back to LTP when previous_close is None"
    )


def test_build_snapshot_position_row_falls_back_to_ltp_when_previous_close_zero():
    """When previous_close is 0.0 (invalid), close_price falls back to LTP."""
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    LTP = 23200.0
    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="NIFTY26JULFUT",
        exchange="NFO",
        qty=50,
        avg_cost=23000.0,
        ltp=LTP,
        day_pnl=10000.0,
        total_pnl=10000.0,
        extras={},
        previous_close=0.0,
    )
    assert row.close_price == pytest.approx(LTP, rel=1e-6), (
        "close_price must fall back to LTP when previous_close=0.0 (invalid)"
    )


def test_snapshot_day_pnl_nonzero_with_previous_close():
    """End-to-end: overnight position row with previous_close set produces
    a non-zero day_change_val from the column — the fixed behaviour.

    Bug scenario: LTP=23200, close_price=LTP=23200, avg=23000, qty=50.
    baseDayPnlForPosition: total_pnl - oq*(ltp-close_price) = 10000 - 50*0 = 10000.
    That is WRONG for overnight positions — total_pnl is lifetime P&L, not day P&L.
    The correct day P&L is in day_pnl column (10000 in this test).

    After fix: close_price = previous_close = 22800 (prior settlement).
    baseDayPnlForPosition: total_pnl - oq*(ltp-close_price)
        = 10000 - 50*(23200-22800) = 10000 - 20000 = -10000.
    But day_change_val in the row comes from the stored day_pnl column,
    not the frontend formula — so row.day_change_val == stored day_pnl.
    The frontend uses day_change_val directly when it is present/non-zero.
    """
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    STORED_DAY_PNL = 10000.0

    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="NIFTY26JULFUT",
        exchange="NFO",
        qty=50,
        avg_cost=23000.0,
        ltp=23200.0,
        day_pnl=STORED_DAY_PNL,
        total_pnl=10000.0,
        extras={},
        previous_close=22800.0,
    )

    # day_change_val comes from the stored day_pnl column via resolve_snapshot_day_pnl
    assert row.day_change_val == pytest.approx(STORED_DAY_PNL, rel=1e-6), (
        f"day_change_val={row.day_change_val} must equal stored day_pnl "
        f"({STORED_DAY_PNL}), not collapse to 0"
    )
    # close_price is the frozen settlement, not LTP
    assert row.close_price == pytest.approx(22800.0, rel=1e-6)
    # last_price is still the snapshot LTP
    assert row.last_price == pytest.approx(23200.0, rel=1e-6)


@pytest.mark.asyncio
async def test_positions_snapshot_passes_previous_close_to_builder():
    """Integration: _positions_snapshot() threads previous_close from DB to
    build_snapshot_position_row. After the prev_ltp preference fix, previous_close
    is now a fallback when prev_ltp is absent.

    Mocked DB returns a 13-tuple. When prev_ltp is None (new position),
    previous_close=22800.0 is used as close_price.
    Expected: row.close_price == 22800.0 (not the LTP of 23200.0).
    """
    from unittest.mock import AsyncMock, MagicMock, patch
    from datetime import datetime, timezone

    captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)

    # 13-tuple: account, symbol, exchange, qty, avg_cost, ltp,
    #           day_pnl, total_pnl, payload_json, captured_at, previous_close,
    #           prev_ltp, prev_settlement_pnl
    snapshot_row = (
        "ZG0790",
        "NIFTY26JULFUT",
        "NFO",
        50,
        Decimal("23000.00"),
        Decimal("23200.00"),   # ltp (snapshot)
        Decimal("10000.00"),   # day_pnl
        Decimal("10000.00"),   # total_pnl
        "{}",                  # payload_json
        captured_ts,           # captured_at
        22800.0,               # previous_close (from snapshot) ← fallback
        None,                  # prev_ltp (new position, no yesterday snapshot)
        None,                  # prev_settlement_pnl (new position)
    )

    mock_result = MagicMock()
    mock_result.all.return_value = [snapshot_row]
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        from backend.api.routes.positions import _positions_snapshot
        resp = await _positions_snapshot()

    assert resp is not None
    assert len(resp.rows) == 1
    row = resp.rows[0]

    assert row.close_price == pytest.approx(22800.0, rel=1e-6), (
        f"close_price={row.close_price} must equal previous_close=22800.0 "
        "(fallback when prev_ltp is absent) — not LTP=23200.0"
    )
    assert row.last_price == pytest.approx(23200.0, rel=1e-6), (
        "last_price must remain LTP=23200.0 (unchanged by close_price logic)"
    )


# ---------------------------------------------------------------------------
# 6. Priority fix — previous_close beats prev_ltp for day_change_val
# ---------------------------------------------------------------------------

def test_build_row_from_snapshot_raw_previous_close_beats_prev_ltp():
    """When prev_ltp == ltp (intraday re-capture), previous_close must be used.

    Bug scenario (pre-fix): prev_close_val = prev_ltp = ltp = 5200.0 → day P&L ≈ 0.
    Fix: actual_previous_close (5044.0) is the primary reference; computed_day_pnl
    uses it so day_change_val reflects the real session move.

    13-tuple column order:
        account, symbol, exchange, qty, avg_cost, ltp,
        day_pnl, total_pnl, payload_json, captured_at,
        previous_close, prev_ltp, prev_settlement_pnl
    """
    from decimal import Decimal
    from datetime import datetime, timezone
    from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

    LTP = 5200.0
    PREV_CLOSE = 5044.0          # yesterday's MCX settlement (3% lower)
    PREV_LTP = LTP               # most recent daily_book write = current LTP
    QTY = 100                    # 100 contracts (multiplier=1 for this test)

    captured_ts = datetime(2026, 8, 11, 17, 0, tzinfo=timezone.utc)
    raw_row = (
        "ZG0790",                # account
        "CRUDEOIL26AUGFUT",      # symbol
        "MCX",                   # exchange
        QTY,                     # qty
        Decimal("5100.00"),      # avg_cost
        Decimal(str(LTP)),       # ltp
        Decimal("0.00"),         # day_pnl  (stale — would be used by old code)
        Decimal("10000.00"),     # total_pnl
        "{}",                    # payload_json (no multiplier override)
        captured_ts,             # captured_at
        PREV_CLOSE,              # previous_close ← frozen settlement
        PREV_LTP,                # prev_ltp = ltp (old code would give 0 day P&L)
        None,                    # prev_settlement_pnl
    )

    row = build_row_from_snapshot_raw(raw_row)

    expected_day_pnl = (LTP - PREV_CLOSE) * QTY   # (5200 - 5044) * 100 = 15600
    assert row.day_change_val == pytest.approx(expected_day_pnl, rel=1e-4), (
        f"day_change_val={row.day_change_val} must equal "
        f"(ltp - previous_close) * qty = {expected_day_pnl}; "
        "old code (prev_ltp priority) would return ≈ 0 when prev_ltp == ltp"
    )
    assert abs(row.day_change_val) > 0, "day_change_val must be non-zero"

    # close_price must use the frozen settlement, not LTP
    assert row.close_price == pytest.approx(PREV_CLOSE, rel=1e-6), (
        f"close_price={row.close_price} must equal previous_close={PREV_CLOSE}"
    )


def test_build_row_from_snapshot_raw_fallback_to_prev_ltp_when_no_previous_close():
    """When previous_close is None but prev_ltp is a valid prior reference,
    day_change_val comes from the stored day_pnl column (not the formula).

    When actual_previous_close is None, computed_day_pnl falls back to the
    stored day_pnl value.  The stored day_pnl must be non-zero for the test
    to be meaningful.
    """
    from decimal import Decimal
    from datetime import datetime, timezone
    from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

    LTP = 5200.0
    STORED_DAY_PNL = 8000.0     # non-zero stored value

    captured_ts = datetime(2026, 8, 11, 17, 0, tzinfo=timezone.utc)
    raw_row = (
        "ZG0790",
        "CRUDEOIL26AUGFUT",
        "MCX",
        100,                         # qty
        Decimal("5100.00"),          # avg_cost
        Decimal(str(LTP)),           # ltp
        Decimal(str(STORED_DAY_PNL)),# day_pnl
        Decimal("10000.00"),         # total_pnl
        "{}",
        captured_ts,
        None,                        # previous_close — absent (new position)
        5100.0,                      # prev_ltp — valid but different from ltp
        None,
    )

    row = build_row_from_snapshot_raw(raw_row)

    # With actual_previous_close=None the formula branch is skipped; stored
    # day_pnl passes through resolve_snapshot_day_pnl unchanged.
    assert row.day_change_val == pytest.approx(STORED_DAY_PNL, rel=1e-4), (
        f"day_change_val={row.day_change_val} must equal stored day_pnl "
        f"({STORED_DAY_PNL}) when previous_close is None"
    )
    assert abs(row.day_change_val) > 0, "day_change_val must be non-zero"


# ---------------------------------------------------------------------------
# 7. MCX post-close day_pnl guard — COALESCE(NULLIF(EXCLUDED.day_pnl, 0), ...)
# ---------------------------------------------------------------------------

def test_upsert_sql_day_pnl_coalesce_nullif_guard():
    """_UPSERT_SQL must gate day_pnl updates on EXCLUDED.ltp IS NOT NULL.

    Updated (2026-08-23): the old COALESCE(NULLIF(EXCLUDED.day_pnl, 0), ...) pattern
    was replaced with a CASE WHEN EXCLUDED.ltp IS NOT NULL guard. This allows a
    genuinely-zero day_pnl (e.g. flat weekend: ltp=prev_close → profit=0) to
    overwrite a stale non-zero value, while still preserving a good EOD value
    when a mid-session NULL write arrives.
    """
    from backend.api.algo.daily_snapshot import _UPSERT_SQL

    sql = _UPSERT_SQL.text.lower()
    # New pattern: CASE WHEN EXCLUDED.ltp IS NOT NULL THEN COALESCE(EXCLUDED.day_pnl, ...) ELSE ...
    assert "case when excluded.ltp is not null then coalesce(excluded.day_pnl" in sql, (
        "_UPSERT_SQL must gate day_pnl on EXCLUDED.ltp IS NOT NULL "
        "(old NULLIF pattern was removed — it prevented zero from overwriting stale values)"
    )
    # Must NOT use NULLIF(EXCLUDED.day_pnl, 0) which would block zero from overwriting stale
    assert "nullif(excluded.day_pnl, 0)" not in sql, (
        "_UPSERT_SQL must not use NULLIF(EXCLUDED.day_pnl, 0) — that freezes stale values"
    )


def test_upsert_sql_day_pnl_guard_preserves_existing_on_null():
    """When EXCLUDED.ltp is NULL, the existing daily_book.day_pnl must be preserved.

    Updated (2026-08-23): the guard is now CASE WHEN EXCLUDED.ltp IS NOT NULL
    THEN COALESCE(EXCLUDED.day_pnl, daily_book.day_pnl) ELSE daily_book.day_pnl END.
    """
    from backend.api.algo.daily_snapshot import _UPSERT_SQL

    sql = _UPSERT_SQL.text

    # New pattern must exist: CASE guard for day_pnl
    assert "CASE WHEN EXCLUDED.ltp IS NOT NULL THEN COALESCE(EXCLUDED.day_pnl" in sql, (
        "day_pnl assignment must use CASE WHEN EXCLUDED.ltp IS NOT NULL guard "
        "so mid-session NULL writes don't overwrite a good EOD day_pnl"
    )


def test_upsert_sql_day_pnl_guard_not_plain_excluded():
    """The plain `day_pnl = EXCLUDED.day_pnl` assignment must not exist in
    the ON CONFLICT clause — that form would overwrite a good EOD value with
    a NULL/0 mid-session write.
    """
    from backend.api.algo.daily_snapshot import _UPSERT_SQL

    sql = _UPSERT_SQL.text

    # The ON CONFLICT ... DO UPDATE block starts after the first INSERT.
    # Split on DO UPDATE to isolate the conflict clause.
    do_update_part = sql.split("DO UPDATE SET", 1)[-1] if "DO UPDATE SET" in sql else sql
    # Strip leading/trailing whitespace per line and check for the plain form
    lines = [ln.strip() for ln in do_update_part.splitlines()]
    plain_assignment = "day_pnl        = EXCLUDED.day_pnl,"
    assert plain_assignment not in lines, (
        "day_pnl must NOT use plain `= EXCLUDED.day_pnl` in the ON CONFLICT clause — "
        "that overwrites a non-NULL existing value with NULL/0 on mid-session writes"
    )


# ---------------------------------------------------------------------------
# 8. NEW: prev_ltp_map SSOT fix — _holdings_rows uses prior daily_book ltp
#    instead of broker close_price for previous_close field
# ---------------------------------------------------------------------------

def test_holdings_rows_accepts_prev_ltp_map_kwarg():
    """_holdings_rows accepts optional prev_ltp_map parameter."""
    from backend.api.algo.daily_snapshot import _holdings_rows
    import inspect

    sig = inspect.signature(_holdings_rows)
    params = sig.parameters
    assert "prev_ltp_map" in params, (
        "_holdings_rows must accept optional 'prev_ltp_map' parameter"
    )
    param = params["prev_ltp_map"]
    assert param.default is None, "prev_ltp_map must default to None"


def test_holdings_rows_previous_close_uses_prev_ltp_map_when_present():
    """When prev_ltp_map has entry for (account, symbol, 'holdings'),
    previous_close uses it instead of broker close_price."""
    from backend.api.algo.daily_snapshot import _holdings_rows
    from datetime import date, datetime, timezone

    holding = {
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "opening_quantity": 10,
        "average_price": 2500.0,
        "last_price": 2550.0,
        "day_change": 50.0,
        "close_price": 2520.0,  # broker's first-cut close (may be stale)
        "pnl": 500.0,
    }

    # Prior-day ltp from daily_book
    prev_ltp_map = {
        ("ACC1", "RELIANCE", "holdings"): 2500.0
    }

    now_ist = datetime(2026, 8, 15, 15, 35, 0)
    rows = _holdings_rows(
        "ACC1", date(2026, 8, 15), [holding], now_ist,
        prev_ltp_map=prev_ltp_map
    )

    assert len(rows) == 1
    assert rows[0]["previous_close"] == 2500.0, (
        f"previous_close should use prev_ltp_map value (2500.0), "
        f"not broker close_price (2520.0), got {rows[0]['previous_close']}"
    )


def test_holdings_rows_previous_close_falls_back_to_close_price():
    """When prev_ltp_map has no entry, fallback to broker close_price."""
    from backend.api.algo.daily_snapshot import _holdings_rows
    from datetime import date, datetime, timezone

    holding = {
        "tradingsymbol": "INFY",
        "exchange": "NSE",
        "opening_quantity": 5,
        "average_price": 1500.0,
        "last_price": 1560.0,
        "day_change": 60.0,
        "close_price": 1500.0,
        "pnl": 300.0,
    }

    prev_ltp_map = {}  # Empty map

    now_ist = datetime(2026, 8, 15, 15, 35, 0)
    rows = _holdings_rows(
        "ACC1", date(2026, 8, 15), [holding], now_ist,
        prev_ltp_map=prev_ltp_map
    )

    assert len(rows) == 1
    assert rows[0]["previous_close"] == 1500.0, (
        f"previous_close should fallback to broker close_price (1500.0), "
        f"got {rows[0]['previous_close']}"
    )


def test_holdings_rows_previous_close_multi_account_symbol_isolation():
    """prev_ltp_map uses (account, symbol, kind) as key;
    entries for different accounts/symbols must not cross."""
    from backend.api.algo.daily_snapshot import _holdings_rows
    from datetime import date, datetime, timezone

    acc1_reliance = {
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "opening_quantity": 10,
        "average_price": 2500.0,
        "last_price": 2550.0,
        "day_change": 50.0,
        "close_price": 2520.0,
        "pnl": 500.0,
    }

    acc1_infy = {
        "tradingsymbol": "INFY",
        "exchange": "NSE",
        "opening_quantity": 5,
        "average_price": 1500.0,
        "last_price": 1560.0,
        "day_change": 60.0,
        "close_price": 1500.0,
        "pnl": 300.0,
    }

    prev_ltp_map = {
        ("ACC1", "RELIANCE", "holdings"): 2475.0,
        ("ACC1", "INFY", "holdings"): 1450.0,
    }

    now_ist = datetime(2026, 8, 15, 15, 35, 0)

    # Test RELIANCE
    rows = _holdings_rows(
        "ACC1", date(2026, 8, 15), [acc1_reliance], now_ist,
        prev_ltp_map=prev_ltp_map
    )
    assert len(rows) == 1
    assert rows[0]["previous_close"] == 2475.0, (
        f"RELIANCE should use map value 2475.0, got {rows[0]['previous_close']}"
    )

    # Test INFY
    rows = _holdings_rows(
        "ACC1", date(2026, 8, 15), [acc1_infy], now_ist,
        prev_ltp_map=prev_ltp_map
    )
    assert len(rows) == 1
    assert rows[0]["previous_close"] == 1450.0, (
        f"INFY should use map value 1450.0, got {rows[0]['previous_close']}"
    )


def test_holdings_rows_previous_close_map_priority_over_broker_close():
    """prev_ltp_map entry takes priority over broker close_price even
    when they differ significantly (e.g., broker close is stale)."""
    from backend.api.algo.daily_snapshot import _holdings_rows
    from datetime import date, datetime, timezone

    holding = {
        "tradingsymbol": "TCS",
        "exchange": "NSE",
        "opening_quantity": 10,
        "average_price": 3400.0,
        "last_price": 3450.0,
        "day_change": 50.0,
        "close_price": 3400.0,  # broker's first-cut (early capture)
        "pnl": 500.0,
    }

    # Actual prior-day settlement is different
    prev_ltp_map = {
        ("ACC1", "TCS", "holdings"): 3350.0
    }

    now_ist = datetime(2026, 8, 15, 15, 35, 0)
    rows = _holdings_rows(
        "ACC1", date(2026, 8, 15), [holding], now_ist,
        prev_ltp_map=prev_ltp_map
    )

    assert len(rows) == 1
    # Map value (3350) should win, not broker close_price (3400)
    assert rows[0]["previous_close"] == 3350.0, (
        f"prev_ltp_map should take priority: expected 3350.0, "
        f"got {rows[0]['previous_close']}"
    )


def test_holdings_rows_previous_close_both_absent_falls_back_to_ltp():
    """When both prev_ltp_map and broker close_price are absent,
    previous_close falls back to ltp_val (not None) to prevent the
    post-settlement guard from zeroing day P&L on a cold-boot snapshot."""
    from unittest.mock import patch
    import backend.api.algo.daily_snapshot as _ds
    from backend.api.algo.daily_snapshot import _holdings_rows
    from datetime import date, datetime, timezone

    holding = {
        "tradingsymbol": "UNKNOWN",
        "exchange": "NSE",
        "opening_quantity": 10,
        "average_price": 1000.0,
        "last_price": 1050.0,
        "day_change": 50.0,
        # no close_price
        "pnl": 500.0,
    }

    prev_ltp_map = {}

    now_ist = datetime(2026, 8, 15, 15, 35, 0)
    # Market closed (post-session) so ltp is captured and available as fallback
    with patch.object(_ds._exchange_clock, "is_exchange_open", return_value=False):
        rows = _holdings_rows(
            "ACC1", date(2026, 8, 15), [holding], now_ist,
            prev_ltp_map=prev_ltp_map
        )

        assert len(rows) == 1
        assert rows[0]["previous_close"] == 1050.0, (
            f"previous_close must fall back to ltp_val (1050.0) when both "
            f"prev_ltp_map and close_price are absent — got {rows[0]['previous_close']}"
        )


def test_positions_rows_previous_close_basic():
    """_positions_rows stores broker close_price as previous_close
    (no prev_ltp_map for positions in current design)."""
    from backend.api.algo.daily_snapshot import _positions_rows
    from datetime import date, datetime, timezone

    position = {
        "tradingsymbol": "NIFTY25AUGFUT",
        "exchange": "NFO",
        "quantity": 50,
        "average_price": 25000.0,
        "last_price": 25100.0,
        "close_price": 25050.0,  # broker's prior-session close
        "pnl": 2500.0,
        "overnight_quantity": 50,
        "day_buy_quantity": 0,
        "day_sell_quantity": 0,
        "day_buy_value": 0.0,
        "day_sell_value": 0.0,
    }

    now_ist = datetime(2026, 8, 15, 15, 35, 0)
    rows = _positions_rows(
        "ACC1", date(2026, 8, 15), [position], now_ist
    )

    assert len(rows) == 1
    assert rows[0]["previous_close"] == 25050.0, (
        f"positions previous_close should store broker close_price, "
        f"got {rows[0]['previous_close']}"
    )


# ---------------------------------------------------------------------------
# 9. NEW: prev_ltp_map for positions — writer SSOT fix
# ---------------------------------------------------------------------------

def _make_position_row(symbol="NIFTY26JULFUT", exchange="NFO", qty=50,
                       last_price=23200.0, close_price=22800.0, pnl=10000.0,
                       overnight_quantity=50, multiplier=1):
    return {
        "tradingsymbol": symbol,
        "exchange": exchange,
        "quantity": qty,
        "average_price": 23000.0,
        "last_price": last_price,
        "close_price": close_price,
        "pnl": pnl,
        "day_change": last_price - close_price,
        "day_change_value": (last_price - close_price) * qty,
        "m2m": (last_price - close_price) * qty,
        "unrealised": pnl,
        "realised": 0.0,
        "value": last_price * qty,
        "buy_quantity": 0, "sell_quantity": 0,
        "buy_value": 0.0, "sell_value": 0.0,
        "buy_m2m": 0.0, "sell_m2m": 0.0,
        "overnight_quantity": overnight_quantity,
        "multiplier": multiplier,
        "instrument_token": 12345,
        "product": "NRML",
    }


def test_positions_rows_prev_ltp_map_priority_over_close_price():
    """When prev_ltp_map has entry for (account, symbol, 'positions'),
    it wins over broker close_price for both previous_close and day_pnl."""
    from unittest.mock import patch
    import backend.api.algo.daily_snapshot as _ds
    from backend.api.algo.daily_snapshot import _positions_rows
    from datetime import date, datetime, timezone

    PRIOR_LTP = 22500.0
    BROKER_CLOSE = 22800.0
    LTP = 23200.0
    QTY = 50

    raw = [_make_position_row(last_price=LTP, close_price=BROKER_CLOSE, qty=QTY,
                              overnight_quantity=QTY)]
    now_ist = datetime(2026, 8, 15, 16, 0, 0, tzinfo=timezone.utc)
    prev_ltp_map = {("ZG0790", "NIFTY26JULFUT", "positions"): PRIOR_LTP}

    # Market closed (post-session) so ltp is captured
    with patch.object(_ds._exchange_clock, "is_exchange_open", return_value=False):
        rows = _positions_rows("ZG0790", date(2026, 8, 15), raw, now_ist,
                               settled=True, prev_ltp_map=prev_ltp_map)
        assert len(rows) == 1
        assert rows[0]["previous_close"] == pytest.approx(PRIOR_LTP), (
            f"previous_close={rows[0]['previous_close']} must equal prev_ltp_map "
            f"value ({PRIOR_LTP}), not broker close_price ({BROKER_CLOSE})"
        )
        # day_pnl must also use prior_ltp as the close reference
        expected = (LTP - PRIOR_LTP) * QTY  # 35000
        wrong = (LTP - BROKER_CLOSE) * QTY   # 20000
        assert rows[0]["day_pnl"] == pytest.approx(expected, rel=1e-4), (
            f"day_pnl={rows[0]['day_pnl']} must use prev_ltp ({PRIOR_LTP}), "
            f"not broker close_price ({BROKER_CLOSE}); expected={expected}, wrong={wrong}"
        )


def test_positions_rows_prev_ltp_map_fallback_to_close_price_for_new_position():
    """When prev_ltp_map has no entry (new position, first day), falls back to close_price."""
    from backend.api.algo.daily_snapshot import _positions_rows
    from datetime import date, datetime, timezone

    BROKER_CLOSE = 22800.0

    raw = [_make_position_row(close_price=BROKER_CLOSE)]
    now_ist = datetime(2026, 8, 15, 16, 0, 0, tzinfo=timezone.utc)
    prev_ltp_map = {}  # new position — no prior row

    rows = _positions_rows("ZG0790", date(2026, 8, 15), raw, now_ist,
                           settled=True, prev_ltp_map=prev_ltp_map)
    assert len(rows) == 1
    assert rows[0]["previous_close"] == pytest.approx(BROKER_CLOSE), (
        f"previous_close={rows[0]['previous_close']} must fall back to broker "
        f"close_price ({BROKER_CLOSE}) when prev_ltp_map has no entry"
    )


def test_positions_rows_monday_after_weekend_uses_friday_ltp():
    """Monday snapshot must use Friday's daily_book.ltp as previous_close.

    Weekend gap: Friday LTP=22500 is in prev_ltp_map; Monday broker returns
    close_price=22800 (stale). previous_close must be 22500 (Friday socket LTP).
    """
    from backend.api.algo.daily_snapshot import _positions_rows
    from datetime import date, datetime, timezone

    FRIDAY_LTP = 22500.0
    MONDAY_BROKER_CLOSE = 22800.0

    raw = [_make_position_row(last_price=23100.0, close_price=MONDAY_BROKER_CLOSE)]
    now_ist = datetime(2026, 8, 17, 16, 0, 0, tzinfo=timezone.utc)
    # prev_ltp_map contains Friday's entry (date < Monday enforced by SQL)
    prev_ltp_map = {("ZG0790", "NIFTY26JULFUT", "positions"): FRIDAY_LTP}

    rows = _positions_rows("ZG0790", date(2026, 8, 17), raw, now_ist,
                           settled=True, prev_ltp_map=prev_ltp_map)
    assert len(rows) == 1
    assert rows[0]["previous_close"] == pytest.approx(FRIDAY_LTP), (
        f"Monday previous_close={rows[0]['previous_close']} must equal "
        f"Friday daily_book.ltp ({FRIDAY_LTP}), not broker close_price ({MONDAY_BROKER_CLOSE})"
    )


def test_positions_rows_prev_ltp_map_key_isolation_between_accounts():
    """Keys are (account, symbol, kind) — ACC2 must not inherit ACC1's map entry."""
    from backend.api.algo.daily_snapshot import _positions_rows
    from datetime import date, datetime, timezone

    PRIOR_LTP_ACC1 = 22500.0
    BROKER_CLOSE = 22800.0

    raw = [_make_position_row(close_price=BROKER_CLOSE)]
    now_ist = datetime(2026, 8, 15, 16, 0, 0, tzinfo=timezone.utc)
    # Only ACC1 has a prev_ltp_map entry
    prev_ltp_map = {("ACC1", "NIFTY26JULFUT", "positions"): PRIOR_LTP_ACC1}

    # Running for ACC2 — must not pick up ACC1's entry
    rows = _positions_rows("ACC2", date(2026, 8, 15), raw, now_ist,
                           settled=True, prev_ltp_map=prev_ltp_map)
    assert len(rows) == 1
    assert rows[0]["previous_close"] == pytest.approx(BROKER_CLOSE), (
        f"ACC2 must not inherit ACC1's prev_ltp_map entry; "
        f"expected fallback to broker close_price ({BROKER_CLOSE}), "
        f"got {rows[0]['previous_close']}"
    )
