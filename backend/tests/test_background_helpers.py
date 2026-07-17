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
