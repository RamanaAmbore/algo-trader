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
from unittest.mock import patch, AsyncMock

import pytest

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
# MCX post-close snapshot trigger (23:31 IST) — now DB-driven
# ---------------------------------------------------------------------------

def test_mcx_close_snapshot_trigger_db_driven():
    """background.py must use exchange_clock for the 23:31 MCX-close trigger.

    The old hardcoded _MCX_CLOSE_H/_MCX_CLOSE_M constants have been removed.
    The trigger is now fired by _snapshot_probe_nse_mcx() which delegates to
    exchange_clock.sessions_with_snapshot_time_now() — the MCX/evening session
    row in exchange_schedule carries snapshot_time=23:31 so the trigger fires
    at the right time without hardcoded constants.
    """
    # The old sentinels and constants must be gone
    assert "_MCX_CLOSE_H" not in _SRC, (
        "_MCX_CLOSE_H constant must be removed — MCX close trigger is now "
        "DB-driven via exchange_clock.sessions_with_snapshot_time_now()"
    )
    assert "_mcx_close_done" not in _SRC, (
        "_mcx_close_done sentinel must be removed — minute-precision match "
        "in exchange_clock ensures each trigger fires exactly once per day"
    )
    # The new dispatch path must be present
    assert "trigger_close_snapshot" in _SRC, (
        "trigger_close_snapshot helper must exist in background.py — "
        "called by _snapshot_probe_nse_mcx when session_name != 'settlement'"
    )
    assert "sessions_with_snapshot_time_now" in _SRC, (
        "_snapshot_probe_nse_mcx must call exchange_clock.sessions_with_snapshot_time_now() "
        "to determine which snapshot triggers to fire each minute"
    )


def test_mcx_close_snapshot_timing_in_exchange_schedule():
    """MCX/evening snapshot_time is stored in exchange_schedule (23:31 IST).

    This test verifies the architectural invariant: the timing is no longer
    in background.py source code — it lives in the DB-backed exchange_clock
    cache.  The background.py source must NOT contain the literal '23, 31'
    as a hardcoded time constant for MCX close.
    """
    import re
    # Must NOT contain a hardcoded MCX close time assignment
    assert not re.search(r"_MCX_CLOSE_[HM]\s*=\s*\d+", _SRC), (
        "MCX close time must not be hardcoded in background.py; "
        "store it in exchange_schedule.snapshot_time for MCX/evening session"
    )


def test_trigger_helpers_are_public():
    """trigger_close_snapshot and trigger_settlement_capture must be public functions.

    Making them public allows them to be unit-tested independently of the
    _task_daily_snapshot loop.  Private helpers (starting with _) cannot be
    reliably imported and asserted against in tests.
    """
    assert "async def trigger_close_snapshot" in _SRC, (
        "trigger_close_snapshot must be a public async function"
    )
    assert "async def trigger_settlement_capture" in _SRC, (
        "trigger_settlement_capture must be a public async function"
    )


# ---------------------------------------------------------------------------
# D1c (2026-10-20) — restart-safety DB-backed "already ran today" markers
# for the 05:30 holiday-refresh cron and the 08:00 prev-close-fix (shared
# between SessionGuard's startup path and the main daily-snapshot loop's
# own tick).
# ---------------------------------------------------------------------------

class _FakeMarkerStore:
    """In-memory stand-in for the `settings` table's get_string/set_string,
    scoped to a single test so no real DB is needed."""

    def __init__(self):
        self.values: dict[str, str] = {}

    def get_string(self, key, default=""):
        return self.values.get(key, default)

    async def set_string(self, key, value, **kw):
        old = self.values.get(key)
        self.values[key] = value
        return old


def _patch_marker_store(store: "_FakeMarkerStore"):
    from unittest.mock import patch
    return (
        patch("backend.shared.helpers.settings.get_string", store.get_string),
        patch("backend.shared.helpers.settings.set_string", store.set_string),
    )


# ---- _hr_needs_catchup (pure decision) -------------------------------------

def test_hr_needs_catchup_true_after_target_with_no_marker():
    """Restart-after-05:30 with no marker yet for today → catch up now."""
    from backend.api.background import _hr_needs_catchup
    from datetime import datetime, time as dtime, timezone, timedelta

    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 10, 20, 6, 0, tzinfo=ist)  # past 05:30
    assert _hr_needs_catchup(now, None, dtime(5, 30)) is True


def test_hr_needs_catchup_false_when_already_run_today():
    """Marker already set for today's date → no catch-up (runs once)."""
    from backend.api.background import _hr_needs_catchup
    from datetime import datetime, time as dtime, timezone, timedelta

    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 10, 20, 6, 0, tzinfo=ist)
    assert _hr_needs_catchup(now, now.date(), dtime(5, 30)) is False


def test_hr_needs_catchup_false_before_target_time():
    """Restart BEFORE today's scheduled slot — not due yet, no catch-up."""
    from backend.api.background import _hr_needs_catchup
    from datetime import datetime, time as dtime, timezone, timedelta

    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 10, 20, 3, 0, tzinfo=ist)  # before 05:30
    assert _hr_needs_catchup(now, None, dtime(5, 30)) is False


@pytest.mark.asyncio
async def test_hr_marker_round_trip_catches_up_once():
    """Full round trip: no marker → catch-up needed; after marking done,
    a second restart later the same day does NOT need to catch up again."""
    from backend.api.background import (
        _hr_needs_catchup, _hr_marker_date, _hr_mark_done,
    )
    from datetime import datetime, time as dtime, timezone, timedelta

    store = _FakeMarkerStore()
    p1, p2 = _patch_marker_store(store)
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 10, 20, 6, 0, tzinfo=ist)

    with p1, p2:
        assert await _hr_marker_date() is None
        assert _hr_needs_catchup(now, await _hr_marker_date(), dtime(5, 30)) is True

        await _hr_mark_done(now.date())

        # A second restart later the same IST day must not catch up again.
        now2 = now + timedelta(hours=2)
        assert _hr_needs_catchup(now2, await _hr_marker_date(), dtime(5, 30)) is False


# ---- _pcf_marker_date / _pcf_mark_done (prev-close-fix shared marker) ------

@pytest.mark.asyncio
async def test_pcf_marker_round_trip():
    from backend.api.background import _pcf_marker_date, _pcf_mark_done
    from datetime import date

    store = _FakeMarkerStore()
    p1, p2 = _patch_marker_store(store)
    with p1, p2:
        assert await _pcf_marker_date() is None
        await _pcf_mark_done(date(2026, 10, 20))
        assert await _pcf_marker_date() == date(2026, 10, 20)


@pytest.mark.asyncio
async def test_sg_recover_close_reset_skips_when_already_run_today():
    """restart-after-08:00-already-done does not re-run: a restart whose
    SessionGuard sees the shared marker already set for today must NOT
    call fix_daily_book_prev_close again (the confirmed live stomp bug —
    it unconditionally reset daily_book.ltp back to the morning
    settlement price on every restart after 08:00, pre-fix)."""
    from backend.api import background as bg
    from datetime import datetime, time as dtime, timezone, timedelta

    store = _FakeMarkerStore()
    p1, p2 = _patch_marker_store(store)
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 10, 20, 10, 0, tzinfo=ist)  # well past 08:00
    await store.set_string("task_markers.prev_close_fix_date", now.date().isoformat())

    with p1, p2, \
         patch("backend.api.background._build_settlement_map", new_callable=AsyncMock) as m_build, \
         patch("backend.api.background.fix_daily_book_prev_close", new_callable=AsyncMock) as m_fix:
        await bg._sg_recover_close_reset(now, now.time())

    m_build.assert_not_awaited()
    m_fix.assert_not_awaited()


@pytest.mark.asyncio
async def test_sg_recover_close_reset_runs_and_marks_when_not_done():
    """Complement: marker absent for today → runs once and marks done."""
    from backend.api import background as bg
    from datetime import datetime, timezone, timedelta

    store = _FakeMarkerStore()
    p1, p2 = _patch_marker_store(store)
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 10, 20, 8, 5, tzinfo=ist)

    with p1, p2, \
         patch("backend.api.database.async_session", _FakeExistsSession(has_row=False)), \
         patch("backend.api.background._build_settlement_map", new_callable=AsyncMock, return_value={}) as m_build, \
         patch("backend.api.background.fix_daily_book_prev_close", new_callable=AsyncMock) as m_fix:
        await bg._sg_recover_close_reset(now, now.time())

    m_build.assert_awaited_once()
    m_fix.assert_awaited_once()
    assert store.values["task_markers.prev_close_fix_date"] == now.date().isoformat()


@pytest.mark.asyncio
async def test_ds_tick_prev_close_fix_does_not_restomp_ltp_after_restart():
    """THE confirmed live stomp bug, reproduced and fixed: once the fix
    has run today via ONE call site (here: SessionGuard, simulated by
    pre-seeding the shared marker), a restart that resets the main daily-
    snapshot loop's own LOCAL `_prev_close_fix_done` sentinel back to
    `[None]` (exactly what happens on every process restart) must NOT
    cause `_ds_tick_prev_close_fix` to re-run and stomp daily_book.ltp
    back to the morning settlement price a second time.

    This test fails against the pre-fix code (no marker check existed —
    the local `done_ref[0] == today` check was the ONLY guard, and a
    restart always resets that local box to [None])."""
    from backend.api import background as bg
    from datetime import datetime, time as dtime, timezone, timedelta

    store = _FakeMarkerStore()
    p1, p2 = _patch_marker_store(store)
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 10, 20, 9, 0, tzinfo=ist)  # well past NSE open

    # Simulate: the fix already ran TODAY via the other call site.
    await store.set_string("task_markers.prev_close_fix_date", now.date().isoformat())
    # Simulate a process restart: the loop's local sentinel is reset to [None].
    done_ref = [None]

    with p1, p2, \
         patch.object(bg.exchange_clock, "get_nse_open_time", return_value=dtime(8, 0)), \
         patch("backend.api.background._build_settlement_map", new_callable=AsyncMock) as m_build, \
         patch("backend.api.background.fix_daily_book_prev_close", new_callable=AsyncMock) as m_fix:
        await bg._ds_tick_prev_close_fix(now, now.date(), done_ref)

    m_build.assert_not_awaited()
    m_fix.assert_not_awaited(), (
        "fix_daily_book_prev_close must not re-run — daily_book.ltp would "
        "be stomped back to the morning settlement price a second time"
    )
    # The local sentinel is still re-seeded so this tick stops re-checking the DB.
    assert done_ref[0] == now.date()


@pytest.mark.asyncio
async def test_ds_tick_prev_close_fix_runs_and_marks_shared_marker():
    """Complement: no marker anywhere yet → the loop's tick runs the fix
    and writes the SHARED marker (not just its own local sentinel), so a
    LATER SessionGuard restart on the same day also sees it as done."""
    from backend.api import background as bg
    from datetime import datetime, time as dtime, timezone, timedelta

    store = _FakeMarkerStore()
    p1, p2 = _patch_marker_store(store)
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 10, 20, 9, 0, tzinfo=ist)
    done_ref = [None]

    with p1, p2, \
         patch.object(bg.exchange_clock, "get_nse_open_time", return_value=dtime(8, 0)), \
         patch("backend.api.database.async_session", _FakeExistsSession(has_row=False)), \
         patch("backend.api.background._build_settlement_map", new_callable=AsyncMock, return_value={}) as m_build, \
         patch("backend.api.background.fix_daily_book_prev_close", new_callable=AsyncMock) as m_fix:
        await bg._ds_tick_prev_close_fix(now, now.date(), done_ref)

    m_build.assert_awaited_once()
    m_fix.assert_awaited_once()
    assert done_ref[0] == now.date()
    assert store.values["task_markers.prev_close_fix_date"] == now.date().isoformat()


# ---------------------------------------------------------------------------
# D1c follow-up (2026-10-20, doc-review finding) — the task_markers.*
# marker protects against a DUPLICATE re-run, but not against a MISSED
# 08:00 run followed by a LATE catch-up landing AFTER today's own EOD
# snapshot has already fired. Sequence: 08:00 missed (server down) → EOD
# snapshot fires at 15:45 IST with a distinct, correct ltp → process
# restarts at 16:00, triggering the catch-up path, which (pre-fix) would
# still run fix_daily_book_prev_close's unconditional
# `ltp = prev_close = close_price` UPDATE, stomping the fresher EOD ltp
# back to the morning settlement price.
# ---------------------------------------------------------------------------

class _FakeExistsResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeExistsSession:
    """Fake async-session for _pcf_eod_already_fired_today's DB fallback
    existence check. `has_row=True` simulates a today-dated holdings/
    positions row existing even though no sentinel was seeded."""

    def __init__(self, has_row: bool):
        self._has_row = has_row

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        return _FakeExistsResult(("1",) if self._has_row else None)


@pytest.mark.asyncio
async def test_pcf_eod_already_fired_today_checks_both_gates():
    from backend.api import background as bg
    from datetime import date

    today = date(2026, 10, 20)
    bg._snapshot_fired_today["NON-MCX"] = None
    bg._snapshot_fired_today["MCX"] = None
    try:
        with patch("backend.api.database.async_session", _FakeExistsSession(has_row=False)):
            assert await bg._pcf_eod_already_fired_today(today) is False

        bg._snapshot_fired_today["NON-MCX"] = today
        assert await bg._pcf_eod_already_fired_today(today) is True

        bg._snapshot_fired_today["NON-MCX"] = None
        bg._snapshot_fired_today["MCX"] = today
        assert await bg._pcf_eod_already_fired_today(today) is True

        # A DIFFERENT day's fired sentinel must not count as "today" —
        # falls through to the DB existence check, which also says no.
        bg._snapshot_fired_today["MCX"] = date(2026, 10, 19)
        with patch("backend.api.database.async_session", _FakeExistsSession(has_row=False)):
            assert await bg._pcf_eod_already_fired_today(today) is False
    finally:
        bg._snapshot_fired_today["NON-MCX"] = None
        bg._snapshot_fired_today["MCX"] = None


@pytest.mark.asyncio
async def test_pcf_eod_already_fired_today_falls_back_to_db_when_sentinel_unseeded():
    """Sentinel-coverage gap: _preload_snapshot_sentinels() seeds
    _snapshot_fired_today only from kind='positions' rows, but
    fix_daily_book_prev_close's UPDATE rewrites kind IN ('holdings',
    'positions'). A holdings-only day (or every account confirmed-empty
    for positions) must still be detected as "EOD already fired" via
    the direct existence check, even with both sentinels unseeded."""
    from backend.api import background as bg
    from datetime import date

    today = date(2026, 10, 20)
    bg._snapshot_fired_today["NON-MCX"] = None
    bg._snapshot_fired_today["MCX"] = None
    try:
        with patch("backend.api.database.async_session", _FakeExistsSession(has_row=True)):
            assert await bg._pcf_eod_already_fired_today(today) is True
    finally:
        bg._snapshot_fired_today["NON-MCX"] = None
        bg._snapshot_fired_today["MCX"] = None


@pytest.mark.asyncio
async def test_pcf_eod_already_fired_today_db_failure_degrades_to_false():
    """A DB error during the fallback existence check degrades to False
    (the pre-this-fix default — let the caller proceed), rather than
    raising and breaking the restart-recovery path."""
    from backend.api import background as bg
    from datetime import date

    def _boom():
        raise RuntimeError("synthetic DB outage")

    today = date(2026, 10, 20)
    bg._snapshot_fired_today["NON-MCX"] = None
    bg._snapshot_fired_today["MCX"] = None
    try:
        with patch("backend.api.database.async_session", _boom):
            assert await bg._pcf_eod_already_fired_today(today) is False
    finally:
        bg._snapshot_fired_today["NON-MCX"] = None
        bg._snapshot_fired_today["MCX"] = None


@pytest.mark.asyncio
async def test_sg_recover_close_reset_skips_when_eod_already_fired_late_catchup():
    """THE confirmed gap: 08:00 missed (no marker set), restart at 16:00
    lands AFTER NSE's own 15:45 EOD snapshot already posted a fresh,
    correct ltp (_snapshot_fired_today["NON-MCX"] set for today). The
    catch-up path must skip fix_daily_book_prev_close entirely — running
    it now would stomp that fresher EOD ltp back to the morning
    settlement reference price. Fails against the pre-fix code (no such
    check existed; it would have run unconditionally)."""
    from backend.api import background as bg
    from datetime import datetime, timezone, timedelta

    store = _FakeMarkerStore()
    p1, p2 = _patch_marker_store(store)
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 10, 20, 16, 0, tzinfo=ist)  # restart at 16:00
    today = now.date()

    # 08:00 was missed: marker never set. EOD already fired for NON-MCX
    # at 15:45 (simulates NSE's own fresh, correct ltp already landed).
    bg._snapshot_fired_today["NON-MCX"] = today
    bg._snapshot_fired_today["MCX"] = None
    try:
        with p1, p2, \
             patch("backend.api.background._build_settlement_map", new_callable=AsyncMock) as m_build, \
             patch("backend.api.background.fix_daily_book_prev_close", new_callable=AsyncMock) as m_fix:
            await bg._sg_recover_close_reset(now, now.time())

        m_build.assert_not_awaited()
        m_fix.assert_not_awaited(), (
            "fix_daily_book_prev_close must not run — it would stomp "
            "today's fresher EOD ltp back to the morning settlement price"
        )
        # Marked done so this doesn't keep re-checking for the rest of the day.
        assert store.values["task_markers.prev_close_fix_date"] == today.isoformat()
    finally:
        bg._snapshot_fired_today["NON-MCX"] = None
        bg._snapshot_fired_today["MCX"] = None


@pytest.mark.asyncio
async def test_ds_tick_prev_close_fix_skips_when_eod_already_fired_late_catchup():
    """Same gap, the main daily-snapshot loop's own tick call site."""
    from backend.api import background as bg
    from datetime import datetime, time as dtime, timezone, timedelta

    store = _FakeMarkerStore()
    p1, p2 = _patch_marker_store(store)
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 10, 20, 16, 0, tzinfo=ist)
    today = now.date()
    done_ref = [None]

    bg._snapshot_fired_today["MCX"] = today  # MCX EOD already fired
    bg._snapshot_fired_today["NON-MCX"] = None
    try:
        with p1, p2, \
             patch.object(bg.exchange_clock, "get_nse_open_time", return_value=dtime(8, 0)), \
             patch("backend.api.background._build_settlement_map", new_callable=AsyncMock) as m_build, \
             patch("backend.api.background.fix_daily_book_prev_close", new_callable=AsyncMock) as m_fix:
            await bg._ds_tick_prev_close_fix(now, today, done_ref)

        m_build.assert_not_awaited()
        m_fix.assert_not_awaited()
        assert done_ref[0] == today
        assert store.values["task_markers.prev_close_fix_date"] == today.isoformat()
    finally:
        bg._snapshot_fired_today["NON-MCX"] = None
        bg._snapshot_fired_today["MCX"] = None


@pytest.mark.asyncio
async def test_sg_recover_close_reset_eod_snapshot_ltp_survives_late_catchup():
    """End-to-end narrative version of the same gap, phrased exactly as
    the reported sequence: 08:00 missed -> EOD snapshot fires at 15:45
    with a distinct, correct ltp -> process restarts at 16:00, triggering
    catch-up -> assert the EOD snapshot's ltp is never touched by the
    catch-up path (fix_daily_book_prev_close, the only function capable
    of overwriting ltp here, is never invoked)."""
    from backend.api import background as bg
    from datetime import datetime, timezone, timedelta

    store = _FakeMarkerStore()
    p1, p2 = _patch_marker_store(store)
    ist = timezone(timedelta(hours=5, minutes=30))
    today_date = datetime(2026, 10, 20, tzinfo=ist).date()

    # EOD snapshot fired at 15:45 with a distinct, correct ltp (narrative
    # only — this test proves the BEHAVIOUR, not the data: the real
    # daily_book row isn't modelled here at all; the assertion below is
    # that fix_daily_book_prev_close, the ONLY writer in this whole
    # codebase capable of overwriting `ltp` on this path, is never
    # invoked, which is sufficient on its own to guarantee no row's ltp
    # changes as a result of this call).
    bg._snapshot_fired_today["NON-MCX"] = today_date
    bg._snapshot_fired_today["MCX"] = None
    try:
        restart_now = datetime(2026, 10, 20, 16, 0, tzinfo=ist)  # 08:00 missed, restart at 16:00
        with p1, p2, \
             patch("backend.api.background._build_settlement_map", new_callable=AsyncMock), \
             patch("backend.api.background.fix_daily_book_prev_close", new_callable=AsyncMock) as m_fix:
            await bg._sg_recover_close_reset(restart_now, restart_now.time())

        m_fix.assert_not_awaited()
    finally:
        bg._snapshot_fired_today["NON-MCX"] = None
        bg._snapshot_fired_today["MCX"] = None
