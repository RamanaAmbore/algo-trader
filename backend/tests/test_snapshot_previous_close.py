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


# ---------------------------------------------------------------------------
# 2. Perf — UPSERT SQL uses COALESCE freeze (no extra query)
# ---------------------------------------------------------------------------

def test_upsert_sql_coalesce_freeze():
    """_UPSERT_SQL must use COALESCE to freeze the first-write previous_close."""
    from backend.api.algo.daily_snapshot import _UPSERT_SQL

    sql = _UPSERT_SQL.text.lower()
    assert "previous_close" in sql, "_UPSERT_SQL must include previous_close column"
    assert "coalesce(daily_book.previous_close, excluded.previous_close)" in sql, (
        "UPSERT must use COALESCE(daily_book.previous_close, EXCLUDED.previous_close) "
        "to freeze the first-write value and never overwrite a non-NULL entry"
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
    """_positions_snapshot SQL includes db.previous_close and passes it to builder."""
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    assert "db.previous_close" in src, (
        "_positions_snapshot SELECT must include db.previous_close"
    )
    assert "previous_close=prev_close_val" in src, (
        "_positions_snapshot must pass previous_close=prev_close_val "
        "(with yesterday-ltp fallback) to build_snapshot_position_row"
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
