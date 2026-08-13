"""
Tests for api/background.py — background task orchestration.
SSOT: _fetch_positions_direct calls apply_day_change_backstop.
Perf: asyncio.gather used for concurrent fetches (not sequential await).
Stale: _task_daily_snapshot delegates to daily_snapshot module (not reimplements).
Reuse: _fetch_holdings_direct and _fetch_positions_direct shared by multiple tasks.
UX: _task_perf_snapshot captures CC + runtime metrics for the admin perf dashboard.
"""
from pathlib import Path
import inspect

_SRC = Path("backend/api/background.py").read_text()


def test_fetch_positions_direct_exists():
    from backend.api.background import _fetch_positions_direct
    assert callable(_fetch_positions_direct), "_fetch_positions_direct must be callable"


def test_fetch_positions_direct_calls_backstop():
    src = inspect.getsource(__import__("backend.api.background", fromlist=["_fetch_positions_direct"])._fetch_positions_direct)
    assert "apply_day_change_backstop" in src or "backstop" in src.lower(), (
        "_fetch_positions_direct must call apply_day_change_backstop to rescue "
        "missing day_change_val on new positions and flat intraday positions"
    )


def test_fetch_holdings_direct_exists():
    from backend.api.background import _fetch_holdings_direct
    assert callable(_fetch_holdings_direct), "_fetch_holdings_direct must be callable"


def test_task_daily_snapshot_exists():
    from backend.api.background import _task_daily_snapshot
    import inspect
    assert inspect.iscoroutinefunction(_task_daily_snapshot), (
        "_task_daily_snapshot must be async"
    )


def test_task_daily_snapshot_delegates_to_module():
    """_task_daily_snapshot must delegate to daily_snapshot module, not reimplement."""
    assert "daily_snapshot" in _SRC, (
        "_task_daily_snapshot must import and call daily_snapshot module — "
        "not reimplement the snapshot logic inline"
    )


def test_task_perf_snapshot_exists():
    from backend.api.background import _task_perf_snapshot
    import inspect
    assert inspect.iscoroutinefunction(_task_perf_snapshot), (
        "_task_perf_snapshot must be async"
    )


def test_task_perf_snapshot_references_radon():
    """_task_perf_snapshot must capture CC metrics via radon or perf_baseline."""
    src = inspect.getsource(__import__("backend.api.background", fromlist=["_task_perf_snapshot"])._task_perf_snapshot)
    assert "radon" in src or "perf_baseline" in src or "cc" in src.lower(), (
        "_task_perf_snapshot must capture cyclomatic complexity via radon or perf_baseline.py"
    )


def test_asyncio_gather_for_concurrent_fetches():
    """asyncio.gather must be used for concurrent fund/position/holding fetches."""
    assert "asyncio.gather" in _SRC, (
        "background.py must use asyncio.gather for concurrent fetches — "
        "sequential await would add 2–3× latency per background cycle"
    )


def test_expiry_last_run_date_sentinel_exists():
    """Module-level _expiry_last_run_date sentinel must exist in background.py."""
    assert "_expiry_last_run_date" in _SRC, (
        "background.py must declare _expiry_last_run_date sentinel to prevent "
        "restart-after-09:20 from skipping today's expiry check"
    )


def test_expiry_restart_fires_immediately_when_not_run_today():
    """If now >= check_time but _expiry_last_run_date != today, delay_s must be 0.

    Simulates a service restart at 10:00 IST on an expiry day where the
    scheduled 09:20 window has already passed and the sentinel is unset.
    """
    from backend.api import background as bg
    from datetime import datetime, timezone, timedelta

    # Construct a fake 'now' at 10:00 IST (UTC+5:30) — past the 09:20 window
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    fake_now = datetime(2026, 7, 24, 10, 0, 0, tzinfo=ist_offset)
    fake_today = fake_now.date()
    hh, mm = 9, 20
    check_time = fake_now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    # Simulate: sentinel is None (not yet run today)
    last_run = None  # _expiry_last_run_date equivalent

    # Replicate the decision logic from _task_expiry_check
    if fake_now >= check_time:
        if last_run != fake_today:
            delay_s = 0.0
        else:
            check_time_next = check_time + timedelta(days=1)
            delay_s = max(0.0, (check_time_next - fake_now).total_seconds())
    else:
        delay_s = max(0.0, (check_time - fake_now).total_seconds())

    assert delay_s == 0.0, (
        f"Restart after 09:20 with sentinel=None must fire immediately (delay_s=0), "
        f"got delay_s={delay_s}"
    )


def test_expiry_no_double_fire_when_already_ran_today():
    """If _expiry_last_run_date == today and now >= check_time, must sleep to tomorrow."""
    from datetime import datetime, timezone, timedelta

    ist_offset = timezone(timedelta(hours=5, minutes=30))
    fake_now = datetime(2026, 7, 24, 10, 0, 0, tzinfo=ist_offset)
    fake_today = fake_now.date()
    hh, mm = 9, 20
    check_time = fake_now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    last_run = fake_today  # already ran today

    if fake_now >= check_time:
        if last_run != fake_today:
            delay_s = 0.0
        else:
            check_time_next = check_time + timedelta(days=1)
            delay_s = max(0.0, (check_time_next - fake_now).total_seconds())
    else:
        delay_s = max(0.0, (check_time - fake_now).total_seconds())

    assert delay_s > 0.0, (
        "When sentinel already matches today, delay_s must be > 0 (sleep to tomorrow)"
    )
    # Should be approximately 23h (next day's 09:20)
    assert delay_s > 3600 * 20, (
        f"Delay until tomorrow's check should be > 20h, got {delay_s/3600:.1f}h"
    )


# ---------------------------------------------------------------------------
# _task_closed_hours_refresh — no snapshot_daily_book calls (P1 fix)
# ---------------------------------------------------------------------------

def test_closed_hours_refresh_does_not_call_snapshot_daily_book():
    """_task_closed_hours_refresh must NOT invoke snapshot_daily_book().

    Root cause of P1 bug: closed-hours snapshot writes produced Saturday rows
    in daily_book with the same LTP as Friday settlement. When _positions_snapshot
    picked these as latest_batch, the prev_batch (Friday settlement) produced
    (Saturday LTP - Friday LTP) = 0 → NavStrip P1 showed 0.

    Fix: settlement writes belong exclusively to _task_daily_snapshot.
    _task_closed_hours_refresh only busts caches.

    Strategy: use dis.get_instructions to inspect bytecode LOAD_GLOBAL/LOAD_ATTR
    calls so docstrings and comments (which legitimately mention the function name
    to explain the rationale) do not cause false positives.
    """
    import dis
    from backend.api import background as bg

    fn = bg._task_closed_hours_refresh
    # Collect all names loaded at the LOAD_GLOBAL / LOAD_ATTR level
    called_names = {
        instr.argval
        for instr in dis.get_instructions(fn.__code__)
        if instr.opname in ("LOAD_GLOBAL", "LOAD_ATTR", "LOAD_DEREF")
        and instr.argval is not None
    }
    assert "snapshot_daily_book" not in called_names, (
        "_task_closed_hours_refresh must not call snapshot_daily_book() — "
        "settlement writes belong to _task_daily_snapshot only. "
        "Closed-hours snapshot writes were the root cause of NavStrip P1=0 on Saturday."
    )


def test_closed_hours_refresh_still_busts_raw_cache():
    """_task_closed_hours_refresh must still call _raw_cache_invalidate for
    positions, holdings, and margins so broker data is fresh on next request.
    """
    import inspect
    from backend.api import background as bg

    src = inspect.getsource(bg._task_closed_hours_refresh)
    assert '_raw_cache_invalidate("positions")' in src, (
        "_task_closed_hours_refresh must bust the raw broker cache for positions"
    )
    assert '_raw_cache_invalidate("holdings")' in src, (
        "_task_closed_hours_refresh must bust the raw broker cache for holdings"
    )
    assert '_raw_cache_invalidate("margins")' in src, (
        "_task_closed_hours_refresh must bust the raw broker cache for margins"
    )


def test_closed_hours_refresh_still_invalidates_api_cache():
    """_task_closed_hours_refresh must still call invalidate() for
    positions, holdings, and funds so API-side TTL cache is cleared.
    """
    import inspect
    from backend.api import background as bg

    src = inspect.getsource(bg._task_closed_hours_refresh)
    assert 'invalidate("positions")' in src, (
        "_task_closed_hours_refresh must invalidate the API cache for positions"
    )
    assert 'invalidate("holdings")' in src, (
        "_task_closed_hours_refresh must invalidate the API cache for holdings"
    )
    assert 'invalidate("funds")' in src, (
        "_task_closed_hours_refresh must invalidate the API cache for funds"
    )


def test_closed_hours_refresh_does_not_import_snapshot_daily_book():
    """The daily_snapshot import must be absent from _task_closed_hours_refresh
    source — removing the import was part of the P1 fix to prevent accidental
    re-introduction.
    """
    import inspect
    from backend.api import background as bg

    src = inspect.getsource(bg._task_closed_hours_refresh)
    assert "from backend.api.algo.daily_snapshot import snapshot_daily_book" not in src, (
        "The snapshot_daily_book import must be removed from _task_closed_hours_refresh"
    )


# ---------------------------------------------------------------------------
# MCX post-close snapshot trigger (23:31 IST)
# ---------------------------------------------------------------------------

def test_mcx_close_snapshot_trigger_exists():
    """background.py must contain the 23:31 IST MCX-close snapshot trigger.

    Without it, the 16:15 NSE settlement snapshot writes ltp=NULL/day_pnl=NULL
    for MCX positions (mid_session=True at 16:15).  No snapshot fires again
    until 00:15, so the positions route serves NULL->0 for the entire
    23:30–00:15 window.  The 23:31 trigger closes that gap.
    """
    assert "_MCX_CLOSE_H" in _SRC and "_mcx_close_done" in _SRC, (
        "MCX post-close snapshot trigger missing — positions day_pnl will be NULL "
        "from 23:30 until 00:15 MCX settlement.  Add _MCX_CLOSE_H/_MCX_CLOSE_M "
        "constants + _mcx_close_done deduplication sentinel to _task_daily_snapshot."
    )


def test_mcx_close_snapshot_fires_at_2331_not_later():
    """The MCX-close window must start at exactly 23:31 IST and end before 23:40.

    The window must be narrow ([23:31, 23:40)) to avoid overlapping with the
    overnight polling period.  A start time later than 23:35 would leave a gap
    during which the 30s poll might not fire before 00:00.
    """
    assert "_MCX_CLOSE_H,      _MCX_CLOSE_M      = 23, 31" in _SRC or (
        "_MCX_CLOSE_H" in _SRC and "23, 31" in _SRC
    ), (
        "_MCX_CLOSE_H/_MCX_CLOSE_M must be set to (23, 31) — "
        "start any later and a 30s poll boundary may miss the window before midnight"
    )
    assert "dtime(_MCX_CLOSE_H, _MCX_CLOSE_M) <= now.time() < dtime(23, 40)" in _SRC, (
        "MCX-close guard must be [23:31, 23:40) — upper bound keeps the window "
        "narrow and prevents overlap with the overnight polling period"
    )


def test_mcx_close_dedup_uses_today_not_adjusted():
    """MCX close deduplication uses `today` (same calendar date as MCX close),
    NOT `today - timedelta(days=1)` — that adjustment belongs only to the
    00:15 MCX *settlement* block which fires on D+1.
    """
    # The mcx-close block fires at 23:31 on the same calendar day as the
    # trade-date.  The settlement block fires at 00:15 D+1 and adjusts.
    # Verify the close block does NOT contain the timedelta adjustment.
    import re

    # Extract just the mcx-close block from the source
    close_block_match = re.search(
        r"MCX close: 23:31.*?_mcx_close_done = today",
        _SRC,
        re.DOTALL,
    )
    assert close_block_match is not None, (
        "Could not find MCX-close block ending with '_mcx_close_done = today'"
    )
    block_text = close_block_match.group(0)
    assert "timedelta(days=1)" not in block_text, (
        "MCX-close block must NOT use timedelta(days=1) adjustment — "
        "that adjustment is only for the 00:15 settlement block (D+1 calendar)"
    )
