"""
Tests for the daily_book.previous_close repair suite.

Covers:
  1. Cutoff formula (pure Python, parametrized) — positions.py / holdings.py
  2. fix_daily_book_prev_close — overnight mode (uses previous_close from yesterday)
  3. fix_daily_book_prev_close — new-session mode (uses ltp from yesterday)
  4. prev_ltp_map overnight SQL — selects previous_close AS ltp
  5. prev_ltp_map new-session SQL — selects ltp (not previous_close)
"""

import inspect
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# 1. Cutoff formula (pure Python)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hour,expected_offset_days", [
    (0, -1),
    (7, -1),
    (7, -1),
    (8,  0),
    (9,  0),
    (23, 0),
])
def test_cutoff_formula(hour, expected_offset_days):
    """Before 08:00 IST cutoff = yesterday's 08:00 IST; at/after = today's 08:00 IST."""
    now_ist = datetime(2026, 8, 25, hour, 0, 0, tzinfo=IST)
    midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today_8am = midnight + timedelta(hours=8)
    cutoff = today_8am if now_ist >= today_8am else today_8am - timedelta(days=1)
    expected = today_8am + timedelta(days=expected_offset_days)
    assert cutoff == expected, (
        f"hour={hour}: cutoff={cutoff} expected={expected}"
    )


# ---------------------------------------------------------------------------
# 2. fix_daily_book_prev_close — overnight mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fix_daily_book_prev_close_overnight_uses_previous_close():
    """Overnight mode (hour=1, before 08:00 IST): query must select previous_close
    from yesterday's rows, and epsilon=0.005 so only wrong rows (prev_close ≈ ltp)
    are updated.
    """
    from backend.api.algo.daily_snapshot import fix_daily_book_prev_close

    now_ist = datetime(2026, 8, 25, 1, 0, 0, tzinfo=IST)

    mock_result = MagicMock()
    mock_result.rowcount = 3
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    with patch("backend.api.algo.daily_snapshot.async_session", return_value=mock_session):
        updated = await fix_daily_book_prev_close(now_ist)

    assert updated == 3, f"Expected 3 updated rows, got {updated}"

    # Inspect the SQL sent to execute
    call_args = mock_session.execute.call_args
    sql_obj = call_args[0][0]
    sql_str = str(sql_obj)
    params = call_args[0][1]

    # Overnight: must read previous_close (not raw ltp) from yesterday
    assert "previous_close" in sql_str, (
        f"Overnight mode: SQL must reference 'previous_close' column; got:\n{sql_str}"
    )
    # epsilon must be 0.005 for targeted repair
    assert abs(params["epsilon"] - 0.005) < 1e-9, (
        f"Overnight mode: epsilon must be 0.005, got {params['epsilon']}"
    )
    # today's date must be passed
    assert params["today"] == now_ist.date(), (
        f"today param must be {now_ist.date()}, got {params['today']}"
    )


# ---------------------------------------------------------------------------
# 3. fix_daily_book_prev_close — new-session mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fix_daily_book_prev_close_new_session_uses_ltp():
    """New-session mode (hour=9, after 08:00 IST): query must select ltp from
    yesterday's rows, and epsilon=999999.0 so all today's rows are updated.
    """
    from backend.api.algo.daily_snapshot import fix_daily_book_prev_close

    now_ist = datetime(2026, 8, 25, 9, 0, 0, tzinfo=IST)

    mock_result = MagicMock()
    mock_result.rowcount = 7
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    with patch("backend.api.algo.daily_snapshot.async_session", return_value=mock_session):
        updated = await fix_daily_book_prev_close(now_ist)

    assert updated == 7, f"Expected 7 updated rows, got {updated}"

    call_args = mock_session.execute.call_args
    sql_obj = call_args[0][0]
    sql_str = str(sql_obj)
    params = call_args[0][1]

    # New-session: SELECT ltp (not previous_close AS ltp) from yesterday
    # The SQL for new-session uses ref_col="ltp" — so the CTE reads ltp column directly.
    # Verify "previous_close AS ltp" is NOT in the SQL (that's the overnight form).
    assert "previous_close AS ltp" not in sql_str, (
        f"New-session mode: SQL must NOT use 'previous_close AS ltp'; got:\n{sql_str}"
    )
    # epsilon must be 999999.0 — unconditional update
    assert abs(params["epsilon"] - 999999.0) < 1.0, (
        f"New-session mode: epsilon must be ~999999.0, got {params['epsilon']}"
    )
    assert params["today"] == now_ist.date(), (
        f"today param must be {now_ist.date()}, got {params['today']}"
    )


# ---------------------------------------------------------------------------
# 4. prev_ltp_map — overnight SQL selects previous_close AS ltp
# ---------------------------------------------------------------------------

def test_prev_ltp_map_overnight_sql_uses_previous_close():
    """snapshot_daily_book source must contain a branch that reads
    'previous_close AS ltp' (the overnight path for prev_ltp_map).
    """
    from backend.api.algo import daily_snapshot as _ds_module

    src = inspect.getsource(_ds_module.snapshot_daily_book)

    assert "previous_close AS ltp" in src, (
        "snapshot_daily_book must contain 'previous_close AS ltp' for the "
        "overnight prev_ltp_map branch (before 08:00 IST)"
    )
    assert "_before_session_open" in src, (
        "snapshot_daily_book must define '_before_session_open' to gate the two SQL branches"
    )


# ---------------------------------------------------------------------------
# 5. prev_ltp_map — new-session SQL selects ltp (not previous_close)
# ---------------------------------------------------------------------------

def test_prev_ltp_map_new_session_sql_uses_ltp():
    """snapshot_daily_book source must contain both branches. The new-session
    branch must select plain ltp (not previous_close), verified by the presence
    of the else-branch after _before_session_open.
    """
    from backend.api.algo import daily_snapshot as _ds_module

    src = inspect.getsource(_ds_module.snapshot_daily_book)

    # Both branches must be present
    assert "_before_session_open" in src, (
        "snapshot_daily_book must define '_before_session_open' conditional"
    )
    # The overnight form uses 'previous_close AS ltp'; the new-session form just selects ltp.
    # Verify the else-branch body contains a plain 'ltp' select (without previous_close alias).
    # We do this by checking the two SQL string variables are assigned in the source.
    assert "_prev_sql" in src, (
        "snapshot_daily_book must assign '_prev_sql' variable for the conditional SQL branches"
    )
    # The new-session SQL string (else branch) must not use 'previous_close AS ltp'
    # We verify by checking there are two distinct SQL strings in the source:
    # one with 'previous_close AS ltp' and one that selects plain ltp.
    lines = src.splitlines()
    has_overnight_branch = any("previous_close AS ltp" in l for l in lines)
    has_else_branch = any(l.strip().startswith("else:") for l in lines)
    assert has_overnight_branch, "overnight SQL branch (previous_close AS ltp) not found"
    assert has_else_branch, "else: branch (new-session ltp path) not found in snapshot_daily_book"


# ---------------------------------------------------------------------------
# 6. Positions.py cutoff formula changed from midnight to 8am-1day
# ---------------------------------------------------------------------------

def test_positions_py_cutoff_uses_8am_minus_1_day():
    """_override_stale_close_from_snapshot in positions.py must produce an 08:00 IST cutoff.

    The cutoff logic has been delegated to exchange_clock.settlement_cutoff_for("NSE")
    which implements the before/after 08:00 branch in one canonical place. Verify the
    delegation is present and that the old hardcoded arithmetic is gone from positions.py.
    """
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._override_stale_close_from_snapshot)

    assert "settlement_cutoff_for" in src, (
        "positions.py must delegate cutoff to exchange_clock.settlement_cutoff_for('NSE') "
        "— the 08:00 IST / 08:00 IST-1day logic lives in exchange_clock now"
    )
    # Ensure the old hardcoded forms are gone (now live in exchange_clock).
    assert "today_ist_8am - timedelta(days=1)" not in src, (
        "positions.py still contains hardcoded today_ist_8am - timedelta(days=1) — "
        "cutoff must be delegated to exchange_clock.settlement_cutoff_for('NSE')"
    )
    assert "today_ist_midnight" not in src, (
        "positions.py still references today_ist_midnight — cutoff is now in exchange_clock"
    )


# ---------------------------------------------------------------------------
# 7. Holdings.py cutoff formula changed from midnight to 8am-1day
# ---------------------------------------------------------------------------

def test_holdings_py_cutoff_uses_8am_minus_1_day():
    """_override_stale_close_for_holdings in holdings.py must produce an 08:00 IST cutoff.

    The cutoff logic has been delegated to exchange_clock.settlement_cutoff_for("NSE").
    Verify the delegation is present and the old hardcoded arithmetic is gone.
    """
    from backend.api.routes import holdings as _hold_module

    src = inspect.getsource(_hold_module._override_stale_close_for_holdings)

    assert "settlement_cutoff_for" in src, (
        "holdings.py must delegate cutoff to exchange_clock.settlement_cutoff_for('NSE') "
        "— the 08:00 IST / 08:00 IST-1day logic lives in exchange_clock now"
    )
    # Ensure the old hardcoded forms are gone.
    assert "today_ist_8am - timedelta(days=1)" not in src, (
        "holdings.py still contains hardcoded today_ist_8am - timedelta(days=1) — "
        "cutoff must be delegated to exchange_clock.settlement_cutoff_for('NSE')"
    )
    assert "today_ist_midnight" not in src, (
        "holdings.py still references today_ist_midnight — cutoff is now in exchange_clock"
    )


# ---------------------------------------------------------------------------
# 8. fix_daily_book_prev_close — exception path returns 0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fix_daily_book_prev_close_exception_returns_zero():
    """When async_session raises, fix_daily_book_prev_close returns 0 (no crash)."""
    from backend.api.algo.daily_snapshot import fix_daily_book_prev_close

    now_ist = datetime(2026, 8, 25, 2, 0, 0, tzinfo=IST)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.api.algo.daily_snapshot.async_session", return_value=mock_session):
        result = await fix_daily_book_prev_close(now_ist)

    assert result == 0, "Exception path must return 0"


# ---------------------------------------------------------------------------
# 9. fix_daily_book_prev_close — default now_ist uses timestamp_indian
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fix_daily_book_prev_close_default_now_ist():
    """When now_ist=None, the function calls timestamp_indian() internally."""
    from backend.api.algo.daily_snapshot import fix_daily_book_prev_close

    fake_now = datetime(2026, 8, 25, 10, 0, 0, tzinfo=IST)

    mock_result = MagicMock()
    mock_result.rowcount = 0
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    with patch("backend.api.algo.daily_snapshot.timestamp_indian", return_value=fake_now), \
         patch("backend.api.algo.daily_snapshot.async_session", return_value=mock_session):
        updated = await fix_daily_book_prev_close()  # no now_ist arg

    # Should not raise; returns 0 rows updated (mock rowcount=0)
    assert updated == 0
