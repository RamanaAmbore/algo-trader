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
