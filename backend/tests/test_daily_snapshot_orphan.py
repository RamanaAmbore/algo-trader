"""
Tests for _delete_orphan_positions and the orphan-pruning wiring in snapshot_daily_book.

Root cause fixed: snapshot_daily_book() uses UPSERT only. When Kite removes a settled
position from broker.positions(), the daily_book row was never deleted — it persisted
and appeared as an open position during off-hours.

Fix: After every successful broker positions fetch (raw["positions"] is not None),
_delete_orphan_positions() deletes daily_book rows for (date, account, 'positions')
whose symbol is NOT in the current broker response.

Five quality dimensions:
  SSOT        — _delete_orphan_positions is the sole deletion path for stale position rows.
  Correctness — partial prune (some symbols gone), full wipe (all positions closed),
                and no-op (nothing stale) cases all verified.
  Performance — pure in-memory mocks; no network or real DB calls.
  Reuse       — same helper called from snapshot_daily_book's per-account loop.
  UX          — operator must NOT see settled/expired positions during off-hours.
"""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch, call


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _make_session_mock(rowcount: int = 0) -> AsyncMock:
    """Build an async_session context-manager mock that reports `rowcount`
    deleted rows from session.execute()."""
    mock_result = MagicMock()
    mock_result.rowcount = rowcount

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


TARGET_DATE = date(2026, 7, 27)
ACCOUNT = "ZG0790"


# ────────────────────────────────────────────────────────────────────────────
# Test 1 — partial prune: only symbols missing from broker response deleted
# ────────────────────────────────────────────────────────────────────────────

def test_delete_orphan_positions_removes_stale_rows():
    """DB has NIFTY + BANKNIFTY + RELIANCE; broker now returns only RELIANCE.
    _delete_orphan_positions must issue a DELETE NOT IN {RELIANCE} query."""
    from backend.api.algo.daily_snapshot import _delete_orphan_positions

    mock_session = _make_session_mock(rowcount=2)  # 2 stale rows deleted

    with patch("backend.api.algo.daily_snapshot.async_session",
               return_value=mock_session):
        pruned = asyncio.run(
            _delete_orphan_positions(TARGET_DATE, ACCOUNT, {"RELIANCE"})
        )

    assert pruned == 2, f"Expected 2 rows deleted, got {pruned}"

    # Verify a DELETE statement was executed (not a SELECT or INSERT)
    mock_session.execute.assert_called_once()
    executed_stmt = str(mock_session.execute.call_args[0][0])
    assert "DELETE" in executed_stmt.upper(), (
        "execute() must be called with a DELETE statement"
    )
    # Commit must be called after the delete
    mock_session.commit.assert_awaited_once()


def test_delete_orphan_positions_noop_when_nothing_stale():
    """All three symbols still in broker response → rowcount=0 → no logging."""
    from backend.api.algo.daily_snapshot import _delete_orphan_positions

    mock_session = _make_session_mock(rowcount=0)

    with patch("backend.api.algo.daily_snapshot.async_session",
               return_value=mock_session):
        pruned = asyncio.run(
            _delete_orphan_positions(TARGET_DATE, ACCOUNT,
                                     {"NIFTY", "BANKNIFTY", "RELIANCE"})
        )

    assert pruned == 0, f"Expected 0 deletions when nothing is stale, got {pruned}"
    mock_session.execute.assert_called_once()
    mock_session.commit.assert_awaited_once()


# ────────────────────────────────────────────────────────────────────────────
# Test 2 — empty set: broker returned no positions → wipe all rows for today
# ────────────────────────────────────────────────────────────────────────────

def test_delete_orphan_positions_empty_set_clears_all():
    """Broker returned no positions (all closed) → DELETE all today's rows
    for (date, account, kind='positions')."""
    from backend.api.algo.daily_snapshot import _delete_orphan_positions

    mock_session = _make_session_mock(rowcount=2)

    with patch("backend.api.algo.daily_snapshot.async_session",
               return_value=mock_session):
        pruned = asyncio.run(
            _delete_orphan_positions(TARGET_DATE, ACCOUNT, set())
        )

    assert pruned == 2, f"Expected 2 rows deleted (full wipe), got {pruned}"

    # In the empty branch the DELETE has no NOT IN clause (no symbols param)
    call_args = mock_session.execute.call_args
    stmt_str = str(call_args[0][0]).upper()
    assert "DELETE" in stmt_str, "Must issue a DELETE for empty symbol set"
    # Verify the NOT IN branch is NOT used (no expanding bindparam needed)
    # by checking that the second positional arg (params dict) has no 'symbols' key
    params = call_args[0][1] if len(call_args[0]) > 1 else (call_args[1] or {})
    assert "symbols" not in params, (
        "Empty-set branch must not pass a 'symbols' param to the query"
    )
    mock_session.commit.assert_awaited_once()


# ────────────────────────────────────────────────────────────────────────────
# Test 3 — integration: snapshot_daily_book prunes orphan on successful fetch
# ────────────────────────────────────────────────────────────────────────────

def _make_loop_mock(raw_data: dict) -> MagicMock:
    """Return a mock event loop whose run_in_executor immediately returns raw_data.

    snapshot_daily_book calls:
        raw = await loop.run_in_executor(_local_executor, _fetch_account_data, ...)
    We patch the loop to skip the thread pool entirely.
    """
    mock_loop = MagicMock()
    mock_loop.run_in_executor = AsyncMock(return_value=raw_data)
    return mock_loop


def test_snapshot_prunes_orphan_on_refresh():
    """
    Broker returns only RELIANCE in positions (NIFTY is gone after settlement).
    snapshot_daily_book() must call _delete_orphan_positions with symbol set
    containing only RELIANCE so the NIFTY row gets pruned.

    Mocks:
      - _get_connections() → single account 'ZG0790'
      - all_brokers (at registry source) → one fake broker
      - loop.run_in_executor → returns raw_data directly (bypasses thread pool)
      - _upsert_rows → no-op
      - _delete_orphan_positions → capture call args
    """
    from backend.api.algo.daily_snapshot import snapshot_daily_book

    # Minimal broker-shape position row (Kite net positions format)
    reliance_pos = {
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "quantity": 10,
        "average_price": 2800.0,
        "last_price": 2850.0,
        "close_price": 2800.0,
        "pnl": 500.0,
        "day_change": 50.0,
        "day_change_percentage": 1.78,
        "overnight_quantity": 10,
        "day_buy_quantity": 0,
        "day_sell_quantity": 0,
        "day_buy_value": 0.0,
        "day_sell_value": 0.0,
    }

    # positions is a list (not None) → pruning guard fires
    raw_data = {
        "holdings": [],
        "positions": [reliance_pos],
        "trades": [],
        "funds": [],
    }

    fake_broker = MagicMock()
    fake_broker.account = ACCOUNT

    delete_calls: list[tuple] = []

    async def fake_delete_orphan(target_date, account, current_symbols):
        delete_calls.append((target_date, account, frozenset(current_symbols)))
        return 1  # pretend NIFTY row was pruned

    async def fake_upsert(rows):
        return len(rows)

    mock_connections = MagicMock()
    mock_connections.conn = {ACCOUNT: MagicMock()}
    mock_loop = _make_loop_mock(raw_data)

    async def fake_delete_prior(account, current_symbols):
        return 0

    with (
        patch("backend.api.algo.daily_snapshot._get_connections",
              return_value=mock_connections),
        # all_brokers is imported locally inside snapshot_daily_book — patch at source
        patch("backend.brokers.registry.all_brokers",
              return_value=[fake_broker]),
        patch("backend.api.algo.daily_snapshot._upsert_rows",
              side_effect=fake_upsert),
        patch("backend.api.algo.daily_snapshot._delete_orphan_positions",
              side_effect=fake_delete_orphan),
        patch("backend.api.algo.daily_snapshot._delete_prior_orphan_positions",
              side_effect=fake_delete_prior),
        patch("asyncio.get_running_loop", return_value=mock_loop),
    ):
        result = asyncio.run(snapshot_daily_book(target_date=TARGET_DATE))

    assert result["errors"] == [], f"snapshot_daily_book reported errors: {result['errors']}"
    assert ACCOUNT in result["accounts"], "Account must be listed as processed"

    # _delete_orphan_positions must have been called once for our account
    assert len(delete_calls) == 1, (
        f"_delete_orphan_positions must be called exactly once; calls: {delete_calls}"
    )
    called_date, called_account, called_syms = delete_calls[0]
    assert called_date == TARGET_DATE
    assert called_account == ACCOUNT
    assert called_syms == frozenset({"RELIANCE"}), (
        f"Must pass only RELIANCE (current broker symbol) — got {called_syms}"
    )


def test_snapshot_skips_prune_on_broker_positions_failure():
    """When broker.positions() raises, raw['positions'] stays None.
    _delete_orphan_positions must NOT be called — guards against wiping
    rows on a transient broker outage."""
    from backend.api.algo.daily_snapshot import snapshot_daily_book

    # Simulate broker positions failure: positions=None in returned dict
    raw_data = {
        "holdings": [],
        "positions": None,   # positions fetch failed
        "trades": [],
        "funds": [],
    }

    fake_broker = MagicMock()
    fake_broker.account = ACCOUNT

    delete_calls: list = []

    async def fake_delete_orphan(target_date, account, current_symbols):
        delete_calls.append((target_date, account, current_symbols))
        return 0

    async def fake_upsert(rows):
        return len(rows)

    mock_connections = MagicMock()
    mock_connections.conn = {ACCOUNT: MagicMock()}
    mock_loop = _make_loop_mock(raw_data)

    with (
        patch("backend.api.algo.daily_snapshot._get_connections",
              return_value=mock_connections),
        patch("backend.brokers.registry.all_brokers",
              return_value=[fake_broker]),
        patch("backend.api.algo.daily_snapshot._upsert_rows",
              side_effect=fake_upsert),
        patch("backend.api.algo.daily_snapshot._delete_orphan_positions",
              side_effect=fake_delete_orphan),
        patch("asyncio.get_running_loop", return_value=mock_loop),
    ):
        result = asyncio.run(snapshot_daily_book(target_date=TARGET_DATE))

    assert delete_calls == [], (
        "_delete_orphan_positions must NOT be called when raw['positions'] is None "
        f"(broker failure guard). Got calls: {delete_calls}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Test: _fetch_account_data sentinel — positions is None on failure
# ────────────────────────────────────────────────────────────────────────────

def test_fetch_account_data_positions_none_on_broker_failure():
    """_fetch_account_data must leave out['positions'] = None when
    broker.positions() raises, distinguishing failure from empty response."""
    from backend.api.algo.daily_snapshot import _fetch_account_data
    from datetime import date as _date

    mock_broker = MagicMock()
    mock_broker.holdings.return_value = []
    mock_broker.positions.side_effect = RuntimeError("Broker offline")
    mock_broker.trades.return_value = []
    mock_broker.margins.return_value = {}

    result = _fetch_account_data(mock_broker, ACCOUNT, _date(2026, 7, 27))

    assert result["positions"] is None, (
        "positions must be None when broker.positions() raises, "
        f"got: {result['positions']!r}"
    )
    # holdings and trades should still be collected
    assert isinstance(result["holdings"], list)
    assert isinstance(result["trades"], list)


def test_fetch_account_data_positions_empty_list_on_no_open_positions():
    """When broker.positions() returns {'net': []} (no open positions),
    out['positions'] must be an empty list (not None)."""
    from backend.api.algo.daily_snapshot import _fetch_account_data
    from datetime import date as _date

    mock_broker = MagicMock()
    mock_broker.holdings.return_value = []
    mock_broker.positions.return_value = {"net": []}
    mock_broker.trades.return_value = []
    mock_broker.margins.return_value = {}

    result = _fetch_account_data(mock_broker, ACCOUNT, _date(2026, 7, 27))

    assert result["positions"] == [], (
        "positions must be [] (not None) when broker returns empty net list"
    )


# ────────────────────────────────────────────────────────────────────────────
# Test: _delete_prior_orphan_positions — removes settled prior-snapshot symbol
# ────────────────────────────────────────────────────────────────────────────

def test_delete_prior_orphan_positions_removes_settled_symbol():
    """Prior snapshot has IDFCFIRST; broker now returns only RELIANCE.
    _delete_prior_orphan_positions must issue a DELETE with the correct
    subquery anchor and NOT IN :symbols predicate, returning rowcount=1."""
    from backend.api.algo.daily_snapshot import _delete_prior_orphan_positions

    mock_session = _make_session_mock(rowcount=1)

    with patch("backend.api.algo.daily_snapshot.async_session",
               return_value=mock_session):
        pruned = asyncio.run(
            _delete_prior_orphan_positions(ACCOUNT, {"RELIANCE"})
        )

    assert pruned == 1, f"Expected 1 row (IDFCFIRST) deleted, got {pruned}"

    mock_session.execute.assert_called_once()
    stmt_str = str(mock_session.execute.call_args[0][0]).upper()
    assert "DELETE" in stmt_str, "Must issue a DELETE statement"
    # Verify the subquery that anchors the prior-snapshot boundary is present.
    assert "MAX(CAPTURED_AT)" in stmt_str, (
        "DELETE must use MAX(captured_at) subquery to target prior snapshot batches"
    )
    # Symbols param must be passed (non-empty branch)
    params = mock_session.execute.call_args[0][1]
    assert "symbols" in params, "Non-empty branch must pass 'symbols' param"
    assert "RELIANCE" in params["symbols"], (
        "current_symbols must be forwarded as the NOT IN exclusion list"
    )
    mock_session.commit.assert_awaited_once()


def test_delete_prior_orphan_positions_empty_set_wipes_prior():
    """When current_symbols is empty (all positions closed), prior-snapshot
    rows for this account must be deleted without a NOT IN predicate."""
    from backend.api.algo.daily_snapshot import _delete_prior_orphan_positions

    mock_session = _make_session_mock(rowcount=3)

    with patch("backend.api.algo.daily_snapshot.async_session",
               return_value=mock_session):
        pruned = asyncio.run(
            _delete_prior_orphan_positions(ACCOUNT, set())
        )

    assert pruned == 3, f"Expected 3 rows deleted (full prior wipe), got {pruned}"

    call_args = mock_session.execute.call_args
    stmt_str = str(call_args[0][0]).upper()
    assert "DELETE" in stmt_str
    assert "MAX(CAPTURED_AT)" in stmt_str
    # Empty branch: params dict must NOT contain 'symbols'
    params = call_args[0][1]
    assert "symbols" not in params, (
        "Empty-set branch must not pass 'symbols' to avoid a SQL error"
    )
    mock_session.commit.assert_awaited_once()


def test_snapshot_calls_prior_orphan_delete_after_orphan_delete():
    """snapshot_daily_book must call _delete_prior_orphan_positions once
    per account after the existing _delete_orphan_positions call, passing
    the same current-symbol set derived from p_rows."""
    from backend.api.algo.daily_snapshot import snapshot_daily_book

    reliance_pos = {
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "quantity": 10,
        "average_price": 2800.0,
        "last_price": 2850.0,
        "close_price": 2800.0,
        "pnl": 500.0,
        "day_change": 50.0,
        "day_change_percentage": 1.78,
        "overnight_quantity": 10,
        "day_buy_quantity": 0,
        "day_sell_quantity": 0,
        "day_buy_value": 0.0,
        "day_sell_value": 0.0,
    }

    raw_data = {
        "holdings": [],
        "positions": [reliance_pos],
        "trades": [],
        "funds": [],
    }

    fake_broker = MagicMock()
    fake_broker.account = ACCOUNT

    prior_delete_calls: list[tuple] = []

    async def fake_delete_orphan(target_date, account, current_symbols):
        return 0

    async def fake_delete_prior(account, current_symbols):
        prior_delete_calls.append((account, frozenset(current_symbols)))
        return 1

    async def fake_upsert(rows):
        return len(rows)

    mock_connections = MagicMock()
    mock_connections.conn = {ACCOUNT: MagicMock()}
    mock_loop = _make_loop_mock(raw_data)

    with (
        patch("backend.api.algo.daily_snapshot._get_connections",
              return_value=mock_connections),
        patch("backend.brokers.registry.all_brokers",
              return_value=[fake_broker]),
        patch("backend.api.algo.daily_snapshot._upsert_rows",
              side_effect=fake_upsert),
        patch("backend.api.algo.daily_snapshot._delete_orphan_positions",
              side_effect=fake_delete_orphan),
        patch("backend.api.algo.daily_snapshot._delete_prior_orphan_positions",
              side_effect=fake_delete_prior),
        patch("asyncio.get_running_loop", return_value=mock_loop),
    ):
        result = asyncio.run(snapshot_daily_book(target_date=TARGET_DATE))

    assert result["errors"] == []
    assert len(prior_delete_calls) == 1, (
        f"_delete_prior_orphan_positions must be called once; calls: {prior_delete_calls}"
    )
    called_account, called_syms = prior_delete_calls[0]
    assert called_account == ACCOUNT
    assert called_syms == frozenset({"RELIANCE"}), (
        f"Prior-orphan delete must receive current p_rows symbols; got {called_syms}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Test: _positions_snapshot SQL contains qty != 0 filter
# ────────────────────────────────────────────────────────────────────────────

def test_positions_snapshot_excludes_zero_qty():
    """_positions_snapshot must include AND db.qty != 0 in its WHERE clause
    so flat/expired positions from prior snapshots are never served during
    off-market hours.

    Strategy: mock async_session to capture the SQL text passed to
    session.execute, then assert the qty filter is present.  The mock
    returns an empty result set so the function returns None — we only
    care about the SQL shape, not the response body.
    """
    import asyncio as _asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_result = MagicMock()
    mock_result.all.return_value = []          # empty result → function returns None

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    captured_sql: list[str] = []

    original_execute = mock_session.execute

    async def capturing_execute(stmt, *args, **kwargs):
        captured_sql.append(str(stmt))
        return await original_execute(stmt, *args, **kwargs)

    mock_session.execute = capturing_execute

    # async_session is imported locally inside _positions_snapshot via
    # `from backend.api.database import async_session`, so patch at source.
    with patch("backend.api.database.async_session", return_value=mock_session):
        from backend.api.routes.positions import _positions_snapshot
        result = _asyncio.run(_positions_snapshot())

    assert result is None, "Empty DB result must return None from _positions_snapshot"

    assert captured_sql, "session.execute must have been called with the snapshot SQL"
    sql_upper = captured_sql[0].upper()
    assert "QTY != 0" in sql_upper or "DB.QTY != 0" in sql_upper, (
        f"_positions_snapshot SQL must contain 'qty != 0' filter to exclude "
        f"flat/expired positions. SQL was:\n{captured_sql[0]}"
    )
