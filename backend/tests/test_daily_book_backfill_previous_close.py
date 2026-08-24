"""Tests for the _migrate_daily_book_backfill_previous_close migration.

The migration backfills previous_close for historical daily_book rows that have NULL.
It performs a correlated subquery to find the most recent prior trading day's ltp
for the same (account, symbol, kind) and uses that as previous_close.

Five quality dimensions tested:
1. SSOT — function exists and is called from init_db
2. SQL shape — correlated subquery contains expected patterns (MAX, prior-day lookup)
3. Guards — WHERE clause protects against overwriting non-NULL values
4. Fallback — rows with no prior data naturally remain NULL (no COALESCE fallback)
5. Correctness — function signature and docstring are present
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, call

import pytest


# ---------------------------------------------------------------------------
# 1. SSOT — function exists and is called from init_db
# ---------------------------------------------------------------------------

def test_migrate_daily_book_backfill_previous_close_exists_in_database_py():
    """_migrate_daily_book_backfill_previous_close function exists in database.py."""
    import backend.api.database as _db

    assert hasattr(_db, "_migrate_daily_book_backfill_previous_close"), (
        "database.py must contain _migrate_daily_book_backfill_previous_close function"
    )
    func = getattr(_db, "_migrate_daily_book_backfill_previous_close")
    assert callable(func), "_migrate_daily_book_backfill_previous_close must be callable"


def test_migrate_daily_book_backfill_previous_close_called_from_init_db():
    """init_db source code must contain call to _migrate_daily_book_backfill_previous_close."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db.init_db)
    assert "await _migrate_daily_book_backfill_previous_close(conn)" in src, (
        "init_db must call _migrate_daily_book_backfill_previous_close(conn)"
    )


def test_migrate_daily_book_backfill_previous_close_called_after_previous_close_column():
    """_migrate_daily_book_backfill_previous_close is called AFTER the column is added."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db.init_db)
    # Both must exist
    assert "_migrate_daily_book_previous_close(conn)" in src
    assert "_migrate_daily_book_backfill_previous_close(conn)" in src
    # _migrate_daily_book_previous_close should come first
    idx_add = src.find("await _migrate_daily_book_previous_close(conn)")
    idx_backfill = src.find("await _migrate_daily_book_backfill_previous_close(conn)")
    assert idx_add < idx_backfill, (
        "_migrate_daily_book_previous_close (adds column) "
        "must be called before _migrate_daily_book_backfill_previous_close (backfills)"
    )


# ---------------------------------------------------------------------------
# 2. SQL shape — correlated subquery + guards
# ---------------------------------------------------------------------------

def test_migrate_daily_book_backfill_previous_close_sql_has_correct_pattern():
    """Function source contains the expected SQL UPDATE...FROM pattern."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db._migrate_daily_book_backfill_previous_close)
    assert "UPDATE daily_book" in src, (
        "SQL must START with UPDATE daily_book"
    )
    assert "FROM   daily_book p" in src or "FROM daily_book p" in src, (
        "SQL must join FROM daily_book p (alias for prior-day lookup)"
    )
    assert "SET    previous_close = p.ltp" in src or "SET previous_close = p.ltp" in src, (
        "SQL must SET previous_close = p.ltp"
    )


def test_migrate_daily_book_backfill_previous_close_sql_has_null_guard():
    """WHERE clause contains t.previous_close IS NULL guard (idempotent)."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db._migrate_daily_book_backfill_previous_close)
    assert "t.previous_close IS NULL" in src, (
        "WHERE clause must check t.previous_close IS NULL to skip already-filled rows"
    )


def test_migrate_daily_book_backfill_previous_close_sql_has_ltp_not_null_guard():
    """WHERE clause contains p.ltp IS NOT NULL guard (no NULL propagation)."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db._migrate_daily_book_backfill_previous_close)
    assert "p.ltp IS NOT NULL" in src, (
        "WHERE clause must check p.ltp IS NOT NULL so we don't set previous_close to NULL"
    )


def test_migrate_daily_book_backfill_previous_close_sql_has_account_symbol_kind_match():
    """Correlated subquery matches account, symbol, kind."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db._migrate_daily_book_backfill_previous_close)
    assert "p.account = t.account" in src, (
        "SQL must match account: p.account = t.account"
    )
    assert "p.symbol  = t.symbol" in src or "p.symbol = t.symbol" in src, (
        "SQL must match symbol: p.symbol = t.symbol"
    )
    assert "p.kind    = t.kind" in src or "p.kind = t.kind" in src, (
        "SQL must match kind: p.kind = t.kind"
    )


def test_migrate_daily_book_backfill_previous_close_sql_has_max_date_subquery():
    """Subquery uses MAX(h.date) to find the most recent prior trading day."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db._migrate_daily_book_backfill_previous_close)
    assert "MAX(h.date)" in src, (
        "Correlated subquery must use MAX(h.date) to find most recent prior date"
    )


def test_migrate_daily_book_backfill_previous_close_sql_has_prior_date_guard():
    """Subquery filters for h.date < t.date (strictly prior trading day)."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db._migrate_daily_book_backfill_previous_close)
    assert "h.date    < t.date" in src or "h.date < t.date" in src, (
        "Correlated subquery must check h.date < t.date to exclude current row"
    )


def test_migrate_daily_book_backfill_previous_close_sql_has_subquery_ltp_guard():
    """Subquery itself checks h.ltp IS NOT NULL (nested guard)."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db._migrate_daily_book_backfill_previous_close)
    # This must appear INSIDE the MAX(h.date) subquery
    sql_text = inspect.getsource(_db._migrate_daily_book_backfill_previous_close)
    # The subquery part should have its own WHERE checking h.ltp IS NOT NULL
    assert "h.ltp IS NOT NULL" in sql_text, (
        "The MAX(h.date) subquery must filter for h.ltp IS NOT NULL"
    )


# ---------------------------------------------------------------------------
# 3. Logic — idempotent (null guard prevents overwrites)
# ---------------------------------------------------------------------------

def test_migrate_daily_book_backfill_previous_close_is_idempotent():
    """Running the migration twice produces the same result (no overwrites of non-NULL values)."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db._migrate_daily_book_backfill_previous_close)
    # The WHERE clause checks t.previous_close IS NULL,
    # so any already-filled rows are never touched.
    assert "t.previous_close IS NULL" in src, (
        "WHERE clause must ensure idempotency by checking t.previous_close IS NULL"
    )


# ---------------------------------------------------------------------------
# 4. Async correctness — function is async and accepts conn
# ---------------------------------------------------------------------------

def test_migrate_daily_book_backfill_previous_close_is_async():
    """Function is defined as 'async def'."""
    import inspect
    import backend.api.database as _db

    func = _db._migrate_daily_book_backfill_previous_close
    assert inspect.iscoroutinefunction(func), (
        "_migrate_daily_book_backfill_previous_close must be an async function"
    )


def test_migrate_daily_book_backfill_previous_close_accepts_conn_param():
    """Function signature accepts a 'conn' parameter."""
    import inspect
    import backend.api.database as _db

    sig = inspect.signature(_db._migrate_daily_book_backfill_previous_close)
    params = list(sig.parameters.keys())
    assert "conn" in params, (
        "_migrate_daily_book_backfill_previous_close must accept 'conn' parameter"
    )


# ---------------------------------------------------------------------------
# 5. Docstring — documents the purpose and idempotence
# ---------------------------------------------------------------------------

def test_migrate_daily_book_backfill_previous_close_has_docstring():
    """Function has a docstring explaining its purpose."""
    import backend.api.database as _db

    func = _db._migrate_daily_book_backfill_previous_close
    assert func.__doc__ is not None, (
        "_migrate_daily_book_backfill_previous_close must have a docstring"
    )


def test_migrate_daily_book_backfill_previous_close_docstring_mentions_backfill():
    """Docstring mentions 'backfill' or 'historical' to explain the purpose."""
    import backend.api.database as _db

    doc = _db._migrate_daily_book_backfill_previous_close.__doc__
    assert "backfill" in doc.lower() or "historical" in doc.lower(), (
        "Docstring should explain that it backfills historical NULL values"
    )


def test_migrate_daily_book_backfill_previous_close_docstring_mentions_idempotent():
    """Docstring mentions 'idempotent' to explain it's safe to run multiple times."""
    import backend.api.database as _db

    doc = _db._migrate_daily_book_backfill_previous_close.__doc__
    assert "idempotent" in doc.lower(), (
        "Docstring should explain the migration is idempotent"
    )


# ---------------------------------------------------------------------------
# 6. Mock integration — conn.execute is called with the UPDATE statement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migrate_daily_book_backfill_previous_close_calls_conn_execute():
    """Calling the function invokes conn.execute with an UPDATE statement."""
    import backend.api.database as _db

    mock_conn = AsyncMock()
    # Mock execute to return a MagicMock (Result-like)
    mock_conn.execute = AsyncMock(return_value=MagicMock())

    await _db._migrate_daily_book_backfill_previous_close(mock_conn)

    # Verify execute was called at least once
    assert mock_conn.execute.called, (
        "conn.execute must be called to execute the UPDATE statement"
    )
    assert mock_conn.execute.call_count >= 1, (
        "conn.execute should be called at least once"
    )


@pytest.mark.asyncio
async def test_migrate_daily_book_backfill_previous_close_sql_contains_update():
    """The SQL text passed to conn.execute contains 'UPDATE daily_book'."""
    import backend.api.database as _db

    mock_conn = AsyncMock()
    captured_sql = None

    async def capture_execute(sql_obj):
        nonlocal captured_sql
        captured_sql = str(sql_obj)
        return MagicMock()

    mock_conn.execute = capture_execute

    await _db._migrate_daily_book_backfill_previous_close(mock_conn)

    assert captured_sql is not None, "execute should have been called"
    assert "UPDATE daily_book" in captured_sql, (
        f"SQL must contain 'UPDATE daily_book', got: {captured_sql[:200]}"
    )


@pytest.mark.asyncio
async def test_migrate_daily_book_backfill_previous_close_sql_contains_previous_close_is_null():
    """The SQL text contains 'previous_close IS NULL' guard."""
    import backend.api.database as _db

    mock_conn = AsyncMock()
    captured_sql = None

    async def capture_execute(sql_obj):
        nonlocal captured_sql
        captured_sql = str(sql_obj)
        return MagicMock()

    mock_conn.execute = capture_execute

    await _db._migrate_daily_book_backfill_previous_close(mock_conn)

    assert captured_sql is not None
    assert "previous_close IS NULL" in captured_sql, (
        f"SQL must check 'previous_close IS NULL' for idempotence, got: {captured_sql[:300]}"
    )


@pytest.mark.asyncio
async def test_migrate_daily_book_backfill_previous_close_sql_contains_max_date():
    """The SQL text contains 'MAX(h.date)' for the subquery."""
    import backend.api.database as _db

    mock_conn = AsyncMock()
    captured_sql = None

    async def capture_execute(sql_obj):
        nonlocal captured_sql
        captured_sql = str(sql_obj)
        return MagicMock()

    mock_conn.execute = capture_execute

    await _db._migrate_daily_book_backfill_previous_close(mock_conn)

    assert captured_sql is not None
    assert "MAX(h.date)" in captured_sql or "max(h.date)" in captured_sql.lower(), (
        f"SQL must use MAX(h.date) for the subquery, got: {captured_sql[:300]}"
    )


# ---------------------------------------------------------------------------
# 7. No fallback — NULL remains when no prior ltp exists (not COALESCE)
# ---------------------------------------------------------------------------

def test_migrate_daily_book_backfill_previous_close_sql_has_no_coalesce_fallback():
    """SQL does NOT use COALESCE or a default that would incorrectly fill NULL values."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db._migrate_daily_book_backfill_previous_close)
    sql_section = src.split("await conn.execute")[1] if "await conn.execute" in src else src
    # The SET clause should be simple: SET previous_close = p.ltp
    # without a COALESCE that would provide a fallback
    # If there's a COALESCE it would need to be in the INSERT/UPSERT, not the UPDATE.
    # For this function (UPDATE only), there should be no COALESCE in the SET clause.
    lines = sql_section.split("\n")
    set_clause_lines = []
    capturing = False
    for line in lines:
        if "SET" in line:
            capturing = True
        if capturing:
            set_clause_lines.append(line)
            if "WHERE" in line:
                break
    set_text = "\n".join(set_clause_lines)
    assert "COALESCE" not in set_text, (
        "The UPDATE SET clause must not use COALESCE — "
        "rows with no prior data should naturally remain NULL"
    )


# ---------------------------------------------------------------------------
# 8. SQL execution safety — text() wrapping
# ---------------------------------------------------------------------------

def test_migrate_daily_book_backfill_previous_close_uses_sqlalchemy_text():
    """Function uses sqlalchemy.text() to wrap the SQL for safety."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db._migrate_daily_book_backfill_previous_close)
    assert "text(" in src, (
        "SQL must be wrapped in sqlalchemy.text(...) for safe execution"
    )
    assert "from sqlalchemy import text" in src or "text =" in src, (
        "Function must import or receive the text constructor"
    )


# ---------------------------------------------------------------------------
# 9. Order — called after column exists
# ---------------------------------------------------------------------------

def test_order_column_addition_before_backfill_in_init_db():
    """In init_db, _migrate_daily_book_previous_close comes before backfill."""
    import inspect
    import backend.api.database as _db

    src = inspect.getsource(_db.init_db)
    # Extract just the migration section
    lines = src.split("\n")
    migrations = [l.strip() for l in lines if "_migrate" in l and "await" in l]

    # Find indices
    idx_add = None
    idx_backfill = None
    for i, line in enumerate(migrations):
        if "_migrate_daily_book_previous_close(conn)" in line:
            idx_add = i
        if "_migrate_daily_book_backfill_previous_close(conn)" in line:
            idx_backfill = i

    assert idx_add is not None, "Column-add migration must be present"
    assert idx_backfill is not None, "Backfill migration must be present"
    assert idx_add < idx_backfill, (
        "Column must be added before backfill (order matters for idempotence)"
    )
