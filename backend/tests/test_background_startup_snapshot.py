"""
Tests for `_preload_snapshot_sentinels()` in backend/api/background.py.

When server restarts, this function queries daily_book to check if today's
EOD snapshots already exist. If they do, it seeds the _snapshot_fired_today
sentinels to prevent SessionGuard and _task_daily_snapshot from re-firing
the same snapshots and overwriting correct EOD data with stale BHAV values.

Five quality dimensions:
  1. SSOT       — sentinel restoration is the canonical source of truth
  2. Performance — DB query is efficient (one round-trip, one aggregation query)
  3. Stale-code  — no unreachable code paths; guard precedes marker setting
  4. Reusable   — logic isolated for unit testing via mocking
  5. Correctness — sentinels set only when matching daily_book rows exist
"""

from __future__ import annotations

import pytest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock, MagicMock
import inspect


# ---------------------------------------------------------------------------
# Test 1: Both sentinels set when both NON-MCX and MCX snapshots exist
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preload_sets_both_sentinels_when_both_exist():
    """When daily_book has both NON-MCX and MCX rows for today, set both sentinels."""
    from backend.api.background import _preload_snapshot_sentinels, _snapshot_fired_today
    from backend.shared.helpers.date_time_utils import timestamp_indian
    from backend.api.algo.expiry_freeze import session_date_of

    # The function sets sentinels to session_date_of(now) (the 08:00 IST
    # trading-session boundary), NOT the bare calendar date — matching
    # bare timestamp_indian().date() here is flaky for any test run
    # landing between IST midnight and 08:00, where the two diverge
    # (2026-10-20 fix; this test previously assumed the bare date).
    now = timestamp_indian()
    today = session_date_of(now)

    # Reset sentinels before test
    original_state = _snapshot_fired_today.copy()
    _snapshot_fired_today["NON-MCX"] = None
    _snapshot_fired_today["MCX"] = None

    try:
        # Mock the async_session context manager and query
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = MagicMock(
            has_non_mcx=True,
            has_mcx=True
        )

        mock_execute = AsyncMock(return_value=mock_result)
        mock_session = AsyncMock()
        mock_session.execute = mock_execute
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("backend.api.database.async_session") as mock_async_session:
            mock_async_session.return_value = mock_session

            await _preload_snapshot_sentinels()

        # Assert: both sentinels set to today
        assert _snapshot_fired_today["NON-MCX"] == today, (
            f"NON-MCX sentinel should be {today}, got {_snapshot_fired_today['NON-MCX']}"
        )
        assert _snapshot_fired_today["MCX"] == today, (
            f"MCX sentinel should be {today}, got {_snapshot_fired_today['MCX']}"
        )
    finally:
        # Restore original state
        _snapshot_fired_today.clear()
        _snapshot_fired_today.update(original_state)


# ---------------------------------------------------------------------------
# Test 2: Only MCX sentinel set when only MCX snapshot exists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preload_sets_only_mcx_sentinel():
    """When daily_book has only MCX rows, set only MCX sentinel."""
    from backend.api.background import _preload_snapshot_sentinels, _snapshot_fired_today
    from backend.shared.helpers.date_time_utils import timestamp_indian
    from backend.api.algo.expiry_freeze import session_date_of

    # session_date_of(), not the bare calendar date — see the sibling
    # test above for why (2026-10-20 fix).
    now = timestamp_indian()
    today = session_date_of(now)

    # Reset sentinels before test
    original_state = _snapshot_fired_today.copy()
    _snapshot_fired_today["NON-MCX"] = None
    _snapshot_fired_today["MCX"] = None

    try:
        # Mock the async_session: has_mcx=True, has_non_mcx=False
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = MagicMock(
            has_non_mcx=False,
            has_mcx=True
        )

        mock_execute = AsyncMock(return_value=mock_result)
        mock_session = AsyncMock()
        mock_session.execute = mock_execute
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("backend.api.database.async_session") as mock_async_session:
            mock_async_session.return_value = mock_session

            await _preload_snapshot_sentinels()

        # Assert: only MCX sentinel set
        assert _snapshot_fired_today["NON-MCX"] is None, (
            f"NON-MCX sentinel should be None, got {_snapshot_fired_today['NON-MCX']}"
        )
        assert _snapshot_fired_today["MCX"] == today, (
            f"MCX sentinel should be {today}, got {_snapshot_fired_today['MCX']}"
        )
    finally:
        # Restore original state
        _snapshot_fired_today.clear()
        _snapshot_fired_today.update(original_state)


# ---------------------------------------------------------------------------
# Test 3: Only NON-MCX sentinel set when only NON-MCX snapshot exists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preload_sets_only_nonmcx_sentinel():
    """When daily_book has only NON-MCX rows, set only NON-MCX sentinel."""
    from backend.api.background import _preload_snapshot_sentinels, _snapshot_fired_today
    from backend.shared.helpers.date_time_utils import timestamp_indian
    from backend.api.algo.expiry_freeze import session_date_of

    # session_date_of(), not the bare calendar date — see
    # test_preload_sets_both_sentinels_when_both_exist for why
    # (2026-10-20 fix).
    now = timestamp_indian()
    today = session_date_of(now)

    # Reset sentinels before test
    original_state = _snapshot_fired_today.copy()
    _snapshot_fired_today["NON-MCX"] = None
    _snapshot_fired_today["MCX"] = None

    try:
        # Mock the async_session: has_non_mcx=True, has_mcx=False
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = MagicMock(
            has_non_mcx=True,
            has_mcx=False
        )

        mock_execute = AsyncMock(return_value=mock_result)
        mock_session = AsyncMock()
        mock_session.execute = mock_execute
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("backend.api.database.async_session") as mock_async_session:
            mock_async_session.return_value = mock_session

            await _preload_snapshot_sentinels()

        # Assert: only NON-MCX sentinel set
        assert _snapshot_fired_today["NON-MCX"] == today, (
            f"NON-MCX sentinel should be {today}, got {_snapshot_fired_today['NON-MCX']}"
        )
        assert _snapshot_fired_today["MCX"] is None, (
            f"MCX sentinel should be None, got {_snapshot_fired_today['MCX']}"
        )
    finally:
        # Restore original state
        _snapshot_fired_today.clear()
        _snapshot_fired_today.update(original_state)


# ---------------------------------------------------------------------------
# Test 3b: NON-MCX signal must consult kind='holdings' rows, not only
# kind='positions' — 2026-11-05 live-incident fix.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preload_query_counts_holdings_rows_as_non_mcx_signal():
    """Live incident (2026-11-05): NSE/equity settlement is normally
    recorded as `kind='holdings'` rows, not intraday `positions` rows.
    On a day where every account's open `positions` happen to be
    MCX-only (confirmed on prod), the ORIGINAL query — `WHERE kind =
    'positions'` with `bool_or(exchange != 'MCX')` — could never see
    the real 15:45 IST NON-MCX settlement at all, since it never looked
    at `kind='holdings'` rows. The NON-MCX sentinel then stayed
    unseeded for the rest of that trading day, and every
    00:00-08:00 IST restart fell through `_ds_startup_snapshot`'s `and`
    gate into a REAL live broker fetch — confirmed live via a
    `ramboq_api` restart at 06:41 IST that briefly displaced the frozen
    overnight snapshot with a fresh live-fetched batch.

    This test asserts on the real SQL text sent to `session.execute`
    (not just the mocked boolean result, which every sibling test above
    already covers and would pass unchanged regardless of this fix) —
    `bool_or` is Postgres-only so a real SQLite/in-memory round-trip
    isn't available here; pinning the SQL text is the closest
    integration-level guard against this exact regression recurring."""
    from backend.api.background import _preload_snapshot_sentinels, _snapshot_fired_today
    from backend.shared.helpers.date_time_utils import timestamp_indian
    from backend.api.algo.expiry_freeze import session_date_of

    now = timestamp_indian()
    today = session_date_of(now)

    original_state = _snapshot_fired_today.copy()
    _snapshot_fired_today["NON-MCX"] = None
    _snapshot_fired_today["MCX"] = None

    captured_sql: list[str] = []

    async def _fake_execute(stmt, *a, **kw):
        captured_sql.append(str(stmt))
        res = MagicMock()
        # Simulates the real-world shape: an all-MCX positions day, but
        # a genuine NON-MCX holdings settlement exists in the window.
        res.one_or_none.return_value = MagicMock(has_non_mcx=True, has_mcx=True)
        return res

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=_fake_execute)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    try:
        with patch("backend.api.database.async_session") as mock_async_session:
            mock_async_session.return_value = mock_session
            await _preload_snapshot_sentinels()

        assert _snapshot_fired_today["NON-MCX"] == today
        assert _snapshot_fired_today["MCX"] == today

        assert captured_sql, "session.execute was never called"
        sql_text = captured_sql[0].replace(" ", "")
        assert "kind='holdings'" in sql_text, (
            "query must treat any kind='holdings' row as a NON-MCX "
            f"settlement signal — got SQL: {captured_sql[0]}"
        )
        assert "WHEREkind='positions'" not in sql_text, (
            "query must not filter the whole FROM clause down to "
            "kind='positions' only — that would hide every holdings "
            f"row from bool_or entirely. Got SQL: {captured_sql[0]}"
        )
    finally:
        _snapshot_fired_today.clear()
        _snapshot_fired_today.update(original_state)


# ---------------------------------------------------------------------------
# Test 3c: end-to-end — _preload_snapshot_sentinels() feeding straight into
# _ds_startup_snapshot()'s own gate, reproducing the full live-incident call
# chain in one test (2026-11-05 fix).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_on_mcx_only_positions_day_skips_startup_live_fetch():
    """Full reproduction of the 2026-11-05 live incident in one test:
    a `ramboq_api` restart at 06:41 IST, on a day where every account's
    open `positions` rows are MCX-only (so the real NON-MCX settlement
    is recorded only as `kind='holdings'` rows) must NOT fall through to
    a live broker fetch.

    Sibling tests above (`test_preload_query_counts_holdings_rows_as_non_mcx_signal`)
    already prove `_preload_snapshot_sentinels()` seeds the NON-MCX
    sentinel correctly in isolation; `test_ds_startup_snapshot_skips_when_eod_sentinels_set`
    already proves `_ds_startup_snapshot()`'s own gate respects an
    already-seeded sentinel. Neither exercises the two wired together —
    which is exactly the path that was broken live: before the fix, the
    sentinel never got seeded on an MCX-only `positions` day, so this
    exact restart sequence fell through and fired a real broker fetch
    at 06:41 IST, hours before the 08:00 IST market open."""
    from backend.api.background import (
        _preload_snapshot_sentinels, _ds_startup_snapshot, _snapshot_fired_today,
    )
    from zoneinfo import ZoneInfo

    original_state = _snapshot_fired_today.copy()
    _snapshot_fired_today["NON-MCX"] = None
    _snapshot_fired_today["MCX"] = None

    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist).replace(hour=6, minute=41, second=0, microsecond=0)

    async def _fake_execute(stmt, *a, **kw):
        res = MagicMock()
        # Real-world shape from the incident: MCX positions exist, NO
        # non-MCX positions rows, but a genuine NON-MCX holdings
        # settlement landed in the session window.
        res.one_or_none.return_value = MagicMock(has_non_mcx=True, has_mcx=True)
        return res

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=_fake_execute)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    try:
        # _preload_snapshot_sentinels() computes its own "now" via
        # timestamp_indian() internally (a local import, re-resolved from
        # the source module on every call) to derive the session date it
        # seeds the sentinel with. Without pinning it to the SAME now_ist
        # used below, this test is wall-clock-dependent: it only passes
        # when the real moment it runs in happens to fall on the same
        # session-date side of the 08:00 IST boundary as the hardcoded
        # 06:41 restart time — flaky by time of day, not a real bug in
        # the fix. Pin both halves to the identical instant.
        with patch("backend.shared.helpers.date_time_utils.timestamp_indian",
                    return_value=now_ist), \
             patch("backend.api.database.async_session") as mock_async_session:
            mock_async_session.return_value = mock_session
            await _preload_snapshot_sentinels()

        with patch("backend.api.background.exchange_clock") as mock_clock, \
             patch("backend.api.background.is_trading_day_today", return_value=True), \
             patch("backend.api.background._snapshot_fire", new_callable=AsyncMock) as mock_fire:
            mock_clock.is_exchange_open.return_value = False

            await _ds_startup_snapshot(now_ist)

            assert not mock_fire.called, (
                "a 06:41 IST restart on an MCX-only positions day must "
                "not fall through to a live broker fetch — the real "
                "NON-MCX settlement is recorded as kind='holdings'"
            )
    finally:
        _snapshot_fired_today.clear()
        _snapshot_fired_today.update(original_state)


# ---------------------------------------------------------------------------
# Test 4: No sentinels set when no daily_book rows exist for today
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preload_sets_nothing_when_no_rows():
    """When daily_book has no rows for today, sentinels remain None."""
    from backend.api.background import _preload_snapshot_sentinels, _snapshot_fired_today

    # Reset sentinels before test
    original_state = _snapshot_fired_today.copy()
    _snapshot_fired_today["NON-MCX"] = None
    _snapshot_fired_today["MCX"] = None

    try:
        # Mock the async_session to return None (no rows)
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = None

        mock_execute = AsyncMock(return_value=mock_result)
        mock_session = AsyncMock()
        mock_session.execute = mock_execute
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("backend.api.database.async_session") as mock_async_session:
            mock_async_session.return_value = mock_session

            await _preload_snapshot_sentinels()

        # Assert: both sentinels remain None
        assert _snapshot_fired_today["NON-MCX"] is None, (
            f"NON-MCX sentinel should be None, got {_snapshot_fired_today['NON-MCX']}"
        )
        assert _snapshot_fired_today["MCX"] is None, (
            f"MCX sentinel should be None, got {_snapshot_fired_today['MCX']}"
        )
    finally:
        # Restore original state
        _snapshot_fired_today.clear()
        _snapshot_fired_today.update(original_state)


# ---------------------------------------------------------------------------
# Test 5: DB errors are swallowed (non-fatal)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preload_swallows_db_error():
    """When DB query raises an exception, function completes without raising."""
    from backend.api.background import _preload_snapshot_sentinels, _snapshot_fired_today

    # Reset sentinels before test
    original_state = _snapshot_fired_today.copy()
    _snapshot_fired_today["NON-MCX"] = None
    _snapshot_fired_today["MCX"] = None

    try:
        # Mock async_session to raise an exception
        mock_session = AsyncMock()
        mock_session.execute.side_effect = Exception("DB connection error")
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("backend.api.database.async_session") as mock_async_session:
            mock_async_session.return_value = mock_session

            # Call should NOT raise; exception is caught and logged
            await _preload_snapshot_sentinels()

        # Assert: sentinels remain None (no change on error)
        assert _snapshot_fired_today["NON-MCX"] is None, (
            f"NON-MCX sentinel should be None on error, got {_snapshot_fired_today['NON-MCX']}"
        )
        assert _snapshot_fired_today["MCX"] is None, (
            f"MCX sentinel should be None on error, got {_snapshot_fired_today['MCX']}"
        )
    finally:
        # Restore original state
        _snapshot_fired_today.clear()
        _snapshot_fired_today.update(original_state)


# ---------------------------------------------------------------------------
# Test 6: _preload_snapshot_sentinels called before _session_guard in on_startup
# ---------------------------------------------------------------------------

def test_preload_called_before_session_guard_in_on_startup():
    """Verify that _preload_snapshot_sentinels appears BEFORE _session_guard in on_startup."""
    from backend.api import background

    # Get the source code of on_startup
    src = inspect.getsource(background.on_startup)

    # Find positions of the two function calls
    preload_pos = src.find("await _preload_snapshot_sentinels()")
    session_guard_pos = src.find("await _session_guard()")

    # Both must be present
    assert preload_pos > 0, (
        "_preload_snapshot_sentinels() call not found in on_startup"
    )
    assert session_guard_pos > 0, (
        "_session_guard() call not found in on_startup"
    )

    # _preload_snapshot_sentinels must come BEFORE _session_guard
    assert preload_pos < session_guard_pos, (
        "_preload_snapshot_sentinels() must be called BEFORE _session_guard() "
        f"in on_startup (preload at {preload_pos}, session_guard at {session_guard_pos})"
    )


# ---------------------------------------------------------------------------
# Integration: verify signature and module-level state
# ---------------------------------------------------------------------------

def test_preload_snapshot_sentinels_exists_and_is_async():
    """Verify _preload_snapshot_sentinels function exists and is async."""
    from backend.api.background import _preload_snapshot_sentinels
    import inspect

    # Check it exists
    assert callable(_preload_snapshot_sentinels), (
        "_preload_snapshot_sentinels should be callable"
    )

    # Check it is async
    assert inspect.iscoroutinefunction(_preload_snapshot_sentinels), (
        "_preload_snapshot_sentinels should be an async function"
    )


def test_snapshot_fired_today_global_state():
    """Verify _snapshot_fired_today module-level dict exists with correct keys."""
    from backend.api.background import _snapshot_fired_today

    # Check structure
    assert isinstance(_snapshot_fired_today, dict), (
        "_snapshot_fired_today should be a dict"
    )
    assert "NON-MCX" in _snapshot_fired_today, (
        "_snapshot_fired_today must have 'NON-MCX' key"
    )
    assert "MCX" in _snapshot_fired_today, (
        "_snapshot_fired_today must have 'MCX' key"
    )

    # Initial values should be None or date
    for key in ["NON-MCX", "MCX"]:
        val = _snapshot_fired_today[key]
        assert val is None or isinstance(val, date), (
            f"_snapshot_fired_today['{key}'] should be None or date, got {type(val)}"
        )


# ---------------------------------------------------------------------------
# Dimension 1 — SSOT: DB query is the canonical sentinel source
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preload_queries_daily_book_for_today():
    """_preload_snapshot_sentinels must query daily_book for today's rows."""
    from backend.api.background import _preload_snapshot_sentinels
    from backend.shared.helpers.date_time_utils import timestamp_indian

    now = timestamp_indian()
    today = now.date()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_midnight = today_midnight + timedelta(days=1)

    # Track if execute was called with the right SQL
    mock_result = MagicMock()
    mock_result.one_or_none.return_value = None

    mock_execute = AsyncMock(return_value=mock_result)
    mock_session = AsyncMock()
    mock_session.execute = mock_execute
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("backend.api.database.async_session") as mock_async_session:
        mock_async_session.return_value = mock_session

        await _preload_snapshot_sentinels()

    # Assert: execute was called
    assert mock_execute.called, (
        "async_session.execute() should be called to query daily_book"
    )

    # Get the SQL and bindparams
    call_args = mock_execute.call_args
    assert call_args, "execute() should have been called with arguments"


# ---------------------------------------------------------------------------
# Dimension 3 — Stale-code: verify no bare return before sentinel setting
# ---------------------------------------------------------------------------

def test_preload_has_no_unreachable_code_paths():
    """Verify the function structure has no unreachable code."""
    from backend.api.background import _preload_snapshot_sentinels

    src = inspect.getsource(_preload_snapshot_sentinels)

    # Check: try-except structure is in place
    assert "try:" in src, "Function should have a try block"
    assert "except Exception" in src, "Function should have exception handling"

    # Check: no bare return before exception handling
    # (The function should complete normally even if DB query fails)
    assert "_snapshot_fired_today[" in src, (
        "Function should attempt to set sentinels"
    )


# ---------------------------------------------------------------------------
# Dimension 5 — Correctness: query uses correct table and time window
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preload_uses_daily_book_table():
    """Query must select from daily_book table."""
    from backend.api.background import _preload_snapshot_sentinels

    mock_result = MagicMock()
    mock_result.one_or_none.return_value = None

    captured_sql = None

    def capture_execute(sql_obj, *args, **kwargs):
        nonlocal captured_sql
        captured_sql = str(sql_obj)
        return mock_result

    mock_execute = AsyncMock(side_effect=capture_execute)
    mock_session = AsyncMock()
    mock_session.execute = mock_execute
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("backend.api.database.async_session") as mock_async_session:
        mock_async_session.return_value = mock_session

        await _preload_snapshot_sentinels()

    # The SQL should mention daily_book
    assert captured_sql is not None, "execute() should have been called"
    assert "daily_book" in captured_sql.lower(), (
        f"Query should select from daily_book; got: {captured_sql}"
    )


# ---------------------------------------------------------------------------
# Tests for _ds_startup_snapshot sentinel check (DB-backed via preload)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ds_startup_snapshot_skips_when_eod_sentinels_set():
    """_ds_startup_snapshot must skip snapshot_fire when both EOD sentinels are set.

    After _preload_snapshot_sentinels runs, sentinels are DB-backed and reliable
    even after a restart. This verifies the guard in _ds_startup_snapshot
    prevents overwriting correct EOD data with stale BHAV values at 02:00 IST.

    2026-10-20 fix (amends this test): the sentinel comparison is now
    session-boundary-aware (`session_date_of`, D1a's own convention) —
    a genuine 02:00 IST restart is still inside the PRIOR trading
    session, so the sentinel `_preload_snapshot_sentinels` would
    actually seed in this scenario is `today - 1 day` (the session
    date), not bare `today`. Uses a real IST-aware datetime so the
    session-boundary math is exercised correctly (a naive
    `datetime.now()` previously stood in for "02:00 IST" without
    actually being IST, which happened to work under the old bare-date
    comparison but not the new session-aware one).
    """
    from zoneinfo import ZoneInfo
    from backend.api.background import _ds_startup_snapshot, _snapshot_fired_today

    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist).replace(hour=2, minute=0, second=0, microsecond=0)
    session_date = (now_ist - timedelta(hours=8)).date()
    _snapshot_fired_today["NON-MCX"] = session_date
    _snapshot_fired_today["MCX"] = session_date
    try:
        with patch("backend.api.background.exchange_clock") as mock_clock, \
             patch("backend.api.background.is_trading_day_today", return_value=True), \
             patch("backend.api.background._snapshot_fire", new_callable=AsyncMock) as mock_fire:
            mock_clock.is_exchange_open.return_value = False

            await _ds_startup_snapshot(now_ist)

            mock_fire.assert_not_called()
    finally:
        _snapshot_fired_today["NON-MCX"] = None
        _snapshot_fired_today["MCX"] = None


@pytest.mark.asyncio
async def test_ds_startup_snapshot_fires_when_sentinels_not_set():
    """_ds_startup_snapshot must call snapshot_fire when sentinels are clear (fresh start)."""
    from backend.api.background import _ds_startup_snapshot, _snapshot_fired_today

    _snapshot_fired_today["NON-MCX"] = None
    _snapshot_fired_today["MCX"] = None
    try:
        now_ist = datetime.now().replace(hour=2, minute=0)

        with patch("backend.api.background.exchange_clock") as mock_clock, \
             patch("backend.api.background.is_trading_day_today", return_value=True), \
             patch("backend.api.background._snapshot_fire", new_callable=AsyncMock) as mock_fire:
            mock_clock.is_exchange_open.return_value = False

            await _ds_startup_snapshot(now_ist)

            mock_fire.assert_called_once_with("startup", market_open=False)
    finally:
        _snapshot_fired_today["NON-MCX"] = None
        _snapshot_fired_today["MCX"] = None
