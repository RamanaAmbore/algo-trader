"""
test_mcx_dst_close_time_sync.py — `_sync_mcx_dst_close_time_coro()`
(`backend/brokers/broker_apis.py`) keeps `exchange_schedule`'s default
MCX ``close_time`` row correct across MCX's twice-yearly US-DST-tracking
revision (23:30 in-DST, 23:55 winter), mirroring
`_upsert_mcx_evening_closed_sessions_coro`'s discipline: it never
clobbers an operator-authored row, and it is a no-op (and silent) when
the stored value already matches the resolved one.

Also covers the `background.py` daily-refresh wiring (source-grep —
piggybacked onto the same 05:30 IST / restart-catch-up wake-up that
already syncs the MCX evening-holiday special sessions, never its own
separate cron).
"""

from __future__ import annotations

import inspect
from datetime import date, time as dtime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeSyncSession:
    """Minimal async-context-manager fake for the single UPDATE
    `_sync_mcx_dst_close_time_coro` issues. `rowcount` simulates whether
    the `source='system' AND close_time IS DISTINCT FROM :close_time`
    WHERE clause actually matched a row."""

    def __init__(self, rowcount: int):
        self.committed = False
        self.executed_params: dict | None = None
        self._rowcount = rowcount

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        self.executed_params = params
        result = MagicMock()
        result.rowcount = self._rowcount
        return result

    async def commit(self):
        self.committed = True


def _patch_exchange_clock_refresh():
    """Patches the module-level `_force_refresh` the sync function calls
    after a real write, so the test never issues a second real DB read."""
    from backend.api.helpers import exchange_clock
    return patch.object(exchange_clock, "_force_refresh", new=AsyncMock())


# ── Resolution feeds the real resolver, not a duplicated rule ──────────


@pytest.mark.asyncio
async def test_winter_date_resolves_and_writes_23_55():
    from backend.brokers import broker_apis

    fake_session = _FakeSyncSession(rowcount=1)
    with patch("backend.api.database.async_session", fake_session), \
         _patch_exchange_clock_refresh():
        resolved = await broker_apis._sync_mcx_dst_close_time_coro(date(2026, 11, 17))

    assert resolved == dtime(23, 55)
    assert fake_session.executed_params["close_time"] == dtime(23, 55)
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_dst_date_resolves_and_writes_23_30():
    from backend.brokers import broker_apis

    fake_session = _FakeSyncSession(rowcount=1)
    with patch("backend.api.database.async_session", fake_session), \
         _patch_exchange_clock_refresh():
        resolved = await broker_apis._sync_mcx_dst_close_time_coro(date(2026, 6, 1))

    assert resolved == dtime(23, 30)
    assert fake_session.executed_params["close_time"] == dtime(23, 30)


@pytest.mark.asyncio
async def test_defaults_to_today_when_no_date_given():
    """`today=None` must resolve via `timestamp_indian()`, not silently
    skip the sync."""
    from backend.brokers import broker_apis

    fake_session = _FakeSyncSession(rowcount=0)
    fixed_today = date(2026, 11, 17)
    # `timestamp_indian` is imported LOCALLY inside the function body
    # (`from ...date_time_utils import timestamp_indian`), re-resolved
    # fresh from the source module on every call — patch it there, not
    # on `broker_apis` (which never binds the name at module scope).
    with patch("backend.shared.helpers.date_time_utils.timestamp_indian",
               return_value=MagicMock(date=lambda: fixed_today)), \
         patch("backend.api.database.async_session", fake_session), \
         _patch_exchange_clock_refresh():
        resolved = await broker_apis._sync_mcx_dst_close_time_coro()

    assert resolved == dtime(23, 55)


# ── UPDATE scoping — only the system default row, never an override ───


def test_update_is_scoped_to_system_default_row():
    """Source-grep: the UPDATE must filter on gate='MCX', date IS NULL,
    session_name='regular', AND source='system' — matching
    `exchange_clock.seed_and_warm()`'s own MCX-migration guard, so an
    operator-edited row (any other `source` value) or a date-specific
    override row is never touched."""
    from backend.brokers import broker_apis

    src = inspect.getsource(broker_apis._sync_mcx_dst_close_time_coro)
    assert "gate = 'MCX'" in src
    assert "date IS NULL" in src
    assert "session_name = 'regular'" in src
    assert "source = 'system'" in src


@pytest.mark.asyncio
async def test_no_op_when_close_time_already_correct():
    """A rowcount of 0 (the DISTINCT FROM guard matched nothing) must
    not raise and must still return the resolved time — the steady-state
    "nothing to do today" case."""
    from backend.brokers import broker_apis

    fake_session = _FakeSyncSession(rowcount=0)
    with patch("backend.api.database.async_session", fake_session), \
         _patch_exchange_clock_refresh():
        resolved = await broker_apis._sync_mcx_dst_close_time_coro(date(2026, 11, 17))

    assert resolved == dtime(23, 55)


# ── Fail-safe: a DB error never raises, returns None ───────────────────


class _BoomSession:
    def __call__(self):
        return self

    async def __aenter__(self):
        raise RuntimeError("DB unreachable")

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_db_failure_logs_and_returns_none_never_raises():
    from backend.brokers import broker_apis

    with patch("backend.api.database.async_session", _BoomSession()):
        resolved = await broker_apis._sync_mcx_dst_close_time_coro(date(2026, 11, 17))

    assert resolved is None


@pytest.mark.asyncio
async def test_exchange_clock_refresh_failure_is_non_fatal():
    """A failure refreshing the in-process exchange_clock cache after a
    successful DB write must not be surfaced as a sync failure — the
    write already committed; the cache self-heals within its own TTL."""
    from backend.brokers import broker_apis
    from backend.api.helpers import exchange_clock

    fake_session = _FakeSyncSession(rowcount=1)
    with patch("backend.api.database.async_session", fake_session), \
         patch.object(exchange_clock, "_force_refresh",
                     new=AsyncMock(side_effect=RuntimeError("cache boom"))):
        resolved = await broker_apis._sync_mcx_dst_close_time_coro(date(2026, 11, 17))

    assert resolved == dtime(23, 55)
    assert fake_session.committed is True


# ── background.py wiring — DELIBERATELY NOT wired yet ──────────────────
#
# A pre-ship review found a real, separate latent bug this coroutine
# would expose if wired in today: `exchange_clock._effective_snapshot_time()`
# derives the MCX close-snapshot trigger as `close_time + 15 min` with
# no date component. Writing `close_time='23:55'` (the winter value)
# makes that derived snapshot_time wrap to `00:10` THE NEXT CALENDAR
# DAY. `exchange_clock._row_matches_now()`'s weekday filter
# (`weekdays=[0,1,2,3,4]`) then checks the REAL weekday at that later
# moment — on a Friday MCX close, the matching instant is a Saturday,
# which fails the Mon-Fri filter. The MCX close-snapshot (which writes
# `daily_book.ltp`, the prev_close SSOT) would silently never fire on
# ANY Friday night throughout the winter window. See CLAUDE.md "MCX
# DST-aware non-agri close time" for the full writeup.
#
# `_sync_mcx_dst_close_time_coro()` itself is correct and fully tested
# above — these two tests are a tripwire: wiring it into
# `_task_holiday_refresh` without first fixing the weekday/midnight-wrap
# gap in `exchange_clock.py` would reintroduce this exact regression.


def test_sync_function_is_not_yet_wired_into_holiday_refresh():
    """Must stay unwired until the exchange_clock weekday/midnight-wrap
    gap above is fixed — see the module-level comment block."""
    import backend.api.background as background

    src = inspect.getsource(background._task_holiday_refresh)
    # The function name may still appear in an explanatory comment (see
    # the sibling test below) — what must be absent is an actual CALL.
    assert "await _sync_mcx_dst_close_time_coro(" not in src, (
        "_sync_mcx_dst_close_time_coro must not be called from "
        "_task_holiday_refresh yet — doing so before the "
        "exchange_clock.py weekday/midnight-wrap gap is fixed would "
        "silently skip the MCX close-snapshot (daily_book.ltp write) "
        "every Friday night during the winter DST window. See CLAUDE.md "
        "'MCX DST-aware non-agri close time'."
    )
    assert "import _sync_mcx_dst_close_time_coro" not in src, (
        "the function must not even be imported inside _task_holiday_refresh "
        "yet (a dead import would be a half-wired state, not a clean defer)"
    )


def test_background_flags_the_deferred_wiring_reason_in_comments():
    """The deferral reason must be documented inline in background.py,
    not only in this test file, so a future implementer sees it at the
    call site they'd naturally add the wiring back at."""
    import backend.api.background as background

    src = inspect.getsource(background._task_holiday_refresh)
    assert "_sync_mcx_dst_close_time_coro" in src, (
        "background.py should still name the deferred function in a "
        "comment at the natural call site, for discoverability"
    )
    assert "weekday" in src.lower() and "wrap" in src.lower(), (
        "the weekday/midnight-wrap hazard must be documented inline"
    )
