"""
Tests for the 2026-09 market/news scheduling redesign — market half
(commit A). Covers:

  - `_load_market_from_db` serves whatever row exists REGARDLESS of age
    (the pre-fix 24h gate reproduced the exact live-request blocking
    delay this redesign exists to eliminate — see docstring).
  - `_market_needs_refresh_today` — the NEW calendar-day-aware freshness
    check, used ONLY by the background/startup-catchup path.
  - `_perform_market_refresh_once` — one refresh attempt: success,
    Gemini-empty, and exception paths.
  - `_daily_content_refresh_cycle` — retry-every-30-min, hard stop 08:00
    IST, single shared cycle.
  - `_spawn_daily_content_refresh` — double-spawn guard.
  - `_task_market` — startup hydration + spawn, no more independent
    daily while-loop.
  - Source-level: `_spawn_daily_content_refresh()` is called INSIDE
    `_task_holiday_refresh`'s wake-up, BEFORE `_do_all()` (so a slow
    holiday-calendar retry can never delay the market/news spawn).
  - `market.py`'s `get_cycle_date` calls use `hours=0, mins=0` (midnight
    IST cutoff) — the old 8am default would mislabel a 05:30-generated
    report as yesterday's cycle.

Five quality dimensions:
  SSOT         — one shared `_daily_content_refresh_cycle`, one shared
                 wake-up trigger (source-level check against a second
                 independent clock)
  Correctness  — age-gate removal verified via a >24h-old row; freshness
                 check verified via IST-date comparison, not UTC
  Reachability — spawn guard actually prevents a second concurrent task
  Reuse       — `_perform_market_refresh_once` reused by both the 05:30
                 wake-up spawn and the startup catch-up path
  UX          — WS broadcast + cache invalidate still fire on success
"""
from __future__ import annotations

import re
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

_BG_PATH = pathlib.Path(__file__).parents[1] / "api" / "background.py"
_MARKET_PATH = pathlib.Path(__file__).parents[1] / "api" / "routes" / "market.py"


def _make_market_report(age_hours: float, cycle_date: str = "2026-09-25"):
    row = MagicMock()
    row.content = "some market content"
    row.cycle_date = cycle_date
    row.generated_at = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return row


def _session_ctx(row):
    """Build an async_session()-shaped context manager returning `row`
    from `.get(MarketReport, 1)`."""
    session = AsyncMock()
    session.get = AsyncMock(return_value=row)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# _load_market_from_db — regardless-of-age serving
# ---------------------------------------------------------------------------

class TestLoadMarketFromDbServesAnyAge:
    @pytest.mark.asyncio
    async def test_row_older_than_24h_is_still_served(self):
        from backend.api import background as bg

        row = _make_market_report(age_hours=48)
        with patch("backend.api.database.async_session", side_effect=lambda: _session_ctx(row)):
            result = await bg._load_market_from_db()

        assert result is not None, (
            "A 48h-old row must still be served — the pre-fix age gate here "
            "forced _db_or_gemini to fall through to a live blocking Gemini "
            "call on every cold cache hit until the proactive refresh caught up"
        )
        assert result.content == "some market content"

    @pytest.mark.asyncio
    async def test_no_row_at_all_returns_none(self):
        from backend.api import background as bg

        with patch("backend.api.database.async_session", side_effect=lambda: _session_ctx(None)):
            result = await bg._load_market_from_db()

        assert result is None


# ---------------------------------------------------------------------------
# _market_needs_refresh_today
# ---------------------------------------------------------------------------

class TestMarketNeedsRefreshToday:
    @pytest.mark.asyncio
    async def test_no_row_needs_refresh(self):
        from backend.api import background as bg

        with patch("backend.api.database.async_session", side_effect=lambda: _session_ctx(None)):
            assert await bg._market_needs_refresh_today() is True

    @pytest.mark.asyncio
    async def test_row_generated_today_ist_does_not_need_refresh(self):
        from backend.api import background as bg
        from backend.shared.helpers.date_time_utils import timestamp_indian

        row = MagicMock()
        row.generated_at = timestamp_indian().astimezone(timezone.utc)
        with patch("backend.api.database.async_session", side_effect=lambda: _session_ctx(row)):
            assert await bg._market_needs_refresh_today() is False

    @pytest.mark.asyncio
    async def test_row_generated_yesterday_ist_needs_refresh(self):
        from backend.api import background as bg
        from backend.shared.helpers.date_time_utils import timestamp_indian

        row = MagicMock()
        row.generated_at = (timestamp_indian() - timedelta(days=1)).astimezone(timezone.utc)
        with patch("backend.api.database.async_session", side_effect=lambda: _session_ctx(row)):
            assert await bg._market_needs_refresh_today() is True


# ---------------------------------------------------------------------------
# _perform_market_refresh_once
# ---------------------------------------------------------------------------

class TestPerformMarketRefreshOnce:
    @pytest.mark.asyncio
    async def test_success_saves_invalidates_and_broadcasts(self):
        from backend.api import background as bg

        fake_resp = MagicMock(content="fresh", cycle_date="2026-09-26")

        with patch.object(bg, "_run", new=AsyncMock(return_value=fake_resp)), \
             patch.object(bg, "_save_market_to_db", new=AsyncMock()) as mock_save, \
             patch("backend.api.cache.invalidate") as mock_invalidate, \
             patch("backend.api.routes.ws.broadcast") as mock_broadcast:
            ok = await bg._perform_market_refresh_once()

        assert ok is True
        mock_save.assert_awaited_once_with(fake_resp)
        mock_invalidate.assert_called_once_with("market")
        mock_broadcast.assert_called_once()

    @pytest.mark.asyncio
    async def test_gemini_empty_returns_false_no_save(self):
        from backend.api import background as bg

        with patch.object(bg, "_run", new=AsyncMock(return_value=None)), \
             patch.object(bg, "_save_market_to_db", new=AsyncMock()) as mock_save:
            ok = await bg._perform_market_refresh_once()

        assert ok is False
        mock_save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetch_exception_returns_false(self):
        from backend.api import background as bg

        with patch.object(bg, "_run", new=AsyncMock(side_effect=RuntimeError("boom"))):
            ok = await bg._perform_market_refresh_once()

        assert ok is False

    @pytest.mark.asyncio
    async def test_db_save_exception_returns_false(self):
        from backend.api import background as bg

        fake_resp = MagicMock(content="fresh", cycle_date="2026-09-26")
        with patch.object(bg, "_run", new=AsyncMock(return_value=fake_resp)), \
             patch.object(bg, "_save_market_to_db", new=AsyncMock(side_effect=RuntimeError("db down"))):
            ok = await bg._perform_market_refresh_once()

        assert ok is False


# ---------------------------------------------------------------------------
# _daily_content_refresh_cycle
# ---------------------------------------------------------------------------

class TestDailyContentRefreshCycle:
    @pytest.mark.asyncio
    async def test_already_fresh_returns_immediately_no_sleep(self):
        from backend.api import background as bg

        with patch.object(bg, "_market_needs_refresh_today", new=AsyncMock(return_value=False)), \
             patch.object(bg, "_perform_market_refresh_once", new=AsyncMock()) as mock_perform, \
             patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await bg._daily_content_refresh_cycle()

        mock_perform.assert_not_called()
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt_no_retry(self):
        from backend.api import background as bg

        with patch.object(bg, "_market_needs_refresh_today", new=AsyncMock(return_value=True)), \
             patch.object(bg, "_perform_market_refresh_once", new=AsyncMock(return_value=True)), \
             patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await bg._daily_content_refresh_cycle()

        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gives_up_after_0800_ist_without_looping_forever(self):
        from backend.api import background as bg

        late_now = datetime(2026, 9, 26, 8, 5, 0)
        with patch.object(bg, "_market_needs_refresh_today", new=AsyncMock(return_value=True)), \
             patch.object(bg, "_perform_market_refresh_once", new=AsyncMock(return_value=False)), \
             patch.object(bg, "timestamp_indian", return_value=late_now), \
             patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await bg._daily_content_refresh_cycle()

        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retries_every_30_min_before_0800(self):
        from backend.api import background as bg

        early_now = datetime(2026, 9, 26, 6, 0, 0)
        attempts = {"n": 0}

        async def _perform():
            attempts["n"] += 1
            return attempts["n"] >= 2   # fail once, succeed on retry

        with patch.object(bg, "_market_needs_refresh_today", new=AsyncMock(return_value=True)), \
             patch.object(bg, "_perform_market_refresh_once", new=_perform), \
             patch.object(bg, "timestamp_indian", return_value=early_now), \
             patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await bg._daily_content_refresh_cycle()

        assert attempts["n"] == 2
        mock_sleep.assert_awaited_once_with(30 * 60)


# ---------------------------------------------------------------------------
# _spawn_daily_content_refresh — double-spawn guard
# ---------------------------------------------------------------------------

class TestSpawnDailyContentRefreshGuard:
    def teardown_method(self):
        from backend.api import background as bg
        bg._DAILY_CONTENT_TASK = None

    @pytest.mark.asyncio
    async def test_second_spawn_while_running_is_skipped(self):
        from backend.api import background as bg

        bg._DAILY_CONTENT_TASK = None

        async def _never_ending():
            import asyncio as _a
            await _a.sleep(3600)

        with patch.object(bg, "_daily_content_refresh_cycle", side_effect=_never_ending):
            bg._spawn_daily_content_refresh()
            first_task = bg._DAILY_CONTENT_TASK
            bg._spawn_daily_content_refresh()
            second_task = bg._DAILY_CONTENT_TASK

        assert first_task is second_task, (
            "A second spawn while the first task is still running must be "
            "a no-op — Gemini's free-tier budget and news's truncate+reload "
            "must never run twice concurrently"
        )
        first_task.cancel()

    @pytest.mark.asyncio
    async def test_spawn_again_after_prior_task_finished(self):
        from backend.api import background as bg

        bg._DAILY_CONTENT_TASK = None

        async def _quick():
            return None

        with patch.object(bg, "_daily_content_refresh_cycle", side_effect=_quick):
            bg._spawn_daily_content_refresh()
            first_task = bg._DAILY_CONTENT_TASK
            await first_task

            bg._spawn_daily_content_refresh()
            second_task = bg._DAILY_CONTENT_TASK
            await second_task

        assert first_task is not second_task, (
            "Once the prior cycle has finished, a new spawn must create a "
            "fresh task (e.g. the next day's 05:30 wake-up)"
        )


# ---------------------------------------------------------------------------
# _task_market — startup hydration + spawn, no independent daily loop
# ---------------------------------------------------------------------------

class TestTaskMarketStartup:
    def teardown_method(self):
        from backend.api import background as bg
        bg._DAILY_CONTENT_TASK = None

    @pytest.mark.asyncio
    async def test_hydrates_stale_row_and_spawns(self):
        from backend.api import background as bg

        stale_row = MagicMock(content="old", cycle_date="2026-09-01")
        with patch.object(bg, "_load_market_from_db", new=AsyncMock(return_value=stale_row)), \
             patch.object(bg, "_spawn_daily_content_refresh") as mock_spawn:
            await bg._task_market({})

        mock_spawn.assert_called_once()
        from backend.api.cache import _store as _cache_store
        assert _cache_store.get("market") is not None
        assert _cache_store["market"][1] is stale_row

    @pytest.mark.asyncio
    async def test_no_row_at_all_still_spawns_no_inline_fetch(self):
        """First-ever boot (empty DB) must NOT do a synchronous inline
        Gemini fetch from _task_market itself — that fallback lives in
        market._db_or_gemini's own "no row at all" branch, triggered by
        the first real request, not duplicated here."""
        from backend.api import background as bg

        with patch.object(bg, "_load_market_from_db", new=AsyncMock(return_value=None)), \
             patch.object(bg, "_spawn_daily_content_refresh") as mock_spawn, \
             patch.object(bg, "_run") as mock_run:
            await bg._task_market({})

        mock_spawn.assert_called_once()
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Source-level: spawn point placement inside _task_holiday_refresh
# ---------------------------------------------------------------------------

def test_spawn_call_present_before_do_all_in_holiday_refresh_source():
    """`_spawn_daily_content_refresh()` must be called INSIDE the wake-up
    body, textually BEFORE `outcomes = await _do_all()` — placing it
    after would delay the market/news spawn until holiday-refresh's own
    (potentially hours-long) retry loop finishes."""
    src = _BG_PATH.read_text()
    m = re.search(
        r"async def _task_holiday_refresh\b.*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert m is not None, "_task_holiday_refresh not found in background.py"
    body = m.group(0)

    spawn_idx = body.find("_spawn_daily_content_refresh()")
    do_all_idx = body.find("await _do_all()")
    assert spawn_idx != -1, "_spawn_daily_content_refresh() call not found in _task_holiday_refresh"
    assert do_all_idx != -1, "_do_all() call not found in _task_holiday_refresh"
    assert spawn_idx < do_all_idx, (
        "_spawn_daily_content_refresh() must be called BEFORE _do_all() so "
        "a slow holiday-calendar retry loop can never delay the market/news spawn"
    )


def test_task_market_has_no_second_independent_while_loop():
    """_task_market must no longer contain its own daily `while True:`
    scheduling loop — that would be a second independent clock alongside
    _task_holiday_refresh's 05:30 wake-up, causing a duplicate Gemini run."""
    src = _BG_PATH.read_text()
    m = re.search(
        r"async def _task_market\b.*?(?=\nasync def |\n_DAILY_CONTENT_TASK|\Z)",
        src, re.DOTALL,
    )
    assert m is not None, "_task_market not found in background.py"
    body = m.group(0)
    assert "while True:" not in body, (
        "_task_market must not contain an independent scheduling loop"
    )


# ---------------------------------------------------------------------------
# market.py — get_cycle_date midnight-IST cutoff (mislabeling fix)
# ---------------------------------------------------------------------------

def test_market_py_uses_midnight_cutoff_for_cycle_date():
    """Both get_cycle_date() call sites in market.py must pass
    hours=0, mins=0 — the function's own 8am default would mislabel a
    05:30-generated report as YESTERDAY's cycle."""
    src = _MARKET_PATH.read_text()
    calls = re.findall(r"get_cycle_date\(([^)]*)\)", src)
    assert calls, "No get_cycle_date(...) calls found in market.py"
    for args in calls:
        assert "hours=0" in args and "mins=0" in args, (
            f"Expected get_cycle_date(hours=0, mins=0, ...) at every call "
            f"site in market.py, got get_cycle_date({args})"
        )
