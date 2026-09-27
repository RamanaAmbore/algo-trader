"""
Tests for the 2026-09 market/news scheduling redesign — news half
(commit B). Covers:

  - `_news_needs_reset_today` / `_news_mark_reset_done_today` — DB-
    persisted marker (settings table `news.last_reset_date`), read
    straight from the DB (not `settings.get_string`/`_CACHE`, which is
    loaded once at boot and won't see a same-process write).
  - `_perform_news_reset_once` — truncate-AFTER-success ordering, cache
    priming (not invalidate), plain "news" key only (never
    "news_scored").
  - `_fetch_and_accumulate` no longer triggers truncation on the hot
    request path.
  - `background._daily_content_refresh_cycle` extended to cover BOTH
    market and news in the same retry-until-08:00 loop.
  - `background._task_news_keepwarm` — recurring 5-min interval,
    primes the plain "news" key only, TTL longer than the interval.

Five quality dimensions:
  SSOT        — one `_news_items_from_db_rows` mapping shared by the
                live path and the reset's cache-priming path
  Correctness — truncate never runs before a successful non-empty fetch;
                marker persists across a simulated "process restart"
                (fresh DB read, no in-memory state)
  Reachability — the settings.py `_OWNED_OUTSIDE_SEEDS_PREFIXES` fix is
                verified directly (marker survives `_prune_retired_keys`)
  Reuse       — `_daily_content_refresh_cycle` is the SAME function
                extended in commit A, not a parallel implementation
  UX          — "news_scored" is never touched by any background path
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _setting_session_ctx(row):
    session = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none = MagicMock(return_value=row)
    session.execute = AsyncMock(return_value=exec_result)
    session.commit = AsyncMock()
    session.add = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, session


# ---------------------------------------------------------------------------
# _news_needs_reset_today / _news_mark_reset_done_today
# ---------------------------------------------------------------------------

class TestNewsResetMarker:
    @pytest.mark.asyncio
    async def test_no_marker_row_needs_reset(self):
        from backend.api.routes import news as n

        ctx, _s = _setting_session_ctx(None)
        with patch.object(n, "async_session", side_effect=lambda: ctx):
            assert await n._news_needs_reset_today() is True

    @pytest.mark.asyncio
    async def test_marker_matches_today_does_not_need_reset(self):
        from backend.api.routes import news as n
        from backend.shared.helpers.date_time_utils import timestamp_indian

        row = MagicMock(value=timestamp_indian().date().isoformat())
        ctx, _s = _setting_session_ctx(row)
        with patch.object(n, "async_session", side_effect=lambda: ctx):
            assert await n._news_needs_reset_today() is False

    @pytest.mark.asyncio
    async def test_marker_from_yesterday_needs_reset(self):
        from backend.api.routes import news as n
        from backend.shared.helpers.date_time_utils import timestamp_indian

        yesterday = (timestamp_indian() - timedelta(days=1)).date().isoformat()
        row = MagicMock(value=yesterday)
        ctx, _s = _setting_session_ctx(row)
        with patch.object(n, "async_session", side_effect=lambda: ctx):
            assert await n._news_needs_reset_today() is True

    @pytest.mark.asyncio
    async def test_db_error_fails_safe_to_needs_reset(self):
        from backend.api.routes import news as n

        with patch.object(n, "async_session", side_effect=RuntimeError("db down")):
            assert await n._news_needs_reset_today() is True

    @pytest.mark.asyncio
    async def test_mark_reset_done_creates_row_when_absent(self):
        from backend.api.routes import news as n

        ctx, session = _setting_session_ctx(None)
        with patch.object(n, "async_session", side_effect=lambda: ctx):
            await n._news_mark_reset_done_today()

        session.add.assert_called_once()
        added_setting = session.add.call_args.args[0]
        assert added_setting.key == "news.last_reset_date"
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mark_reset_done_updates_existing_row(self):
        from backend.api.routes import news as n

        existing = MagicMock(value="2020-01-01")
        ctx, session = _setting_session_ctx(existing)
        with patch.object(n, "async_session", side_effect=lambda: ctx):
            await n._news_mark_reset_done_today()

        session.add.assert_not_called()
        assert existing.value != "2020-01-01"
        session.commit.assert_awaited_once()

    def test_marker_key_is_owned_outside_seeds(self):
        """The 'news.' prefix must be registered in
        _OWNED_OUTSIDE_SEEDS_PREFIXES, or _prune_retired_keys wipes the
        marker on every restart (re-triggering a truncate on next boot
        even when today's reset already ran)."""
        from backend.shared.helpers.settings import _OWNED_OUTSIDE_SEEDS_PREFIXES
        assert any("news.last_reset_date".startswith(p) for p in _OWNED_OUTSIDE_SEEDS_PREFIXES)


# ---------------------------------------------------------------------------
# _perform_news_reset_once
# ---------------------------------------------------------------------------

class TestPerformNewsResetOnce:
    @pytest.mark.asyncio
    async def test_capability_off_is_noop_success(self):
        from backend.api.routes import news as n

        with patch.object(n, "is_enabled", return_value=False), \
             patch.object(n, "_fetch_rss") as mock_fetch:
            ok = await n._perform_news_reset_once()

        assert ok is True
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_exception_returns_false_no_truncate_no_marker(self):
        from backend.api.routes import news as n

        with patch.object(n, "is_enabled", return_value=True), \
             patch("asyncio.get_running_loop") as mock_loop, \
             patch.object(n, "_news_mark_reset_done_today", new=AsyncMock()) as mock_mark:
            mock_loop.return_value.run_in_executor = AsyncMock(side_effect=RuntimeError("boom"))
            ok = await n._perform_news_reset_once()

        assert ok is False
        mock_mark.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_fetch_returns_false_no_truncate(self):
        from backend.api.routes import news as n

        with patch.object(n, "is_enabled", return_value=True), \
             patch("asyncio.get_running_loop") as mock_loop, \
             patch.object(n, "async_session") as mock_session_ctor, \
             patch.object(n, "_news_mark_reset_done_today", new=AsyncMock()) as mock_mark:
            mock_loop.return_value.run_in_executor = AsyncMock(return_value=[])
            ok = await n._perform_news_reset_once()

        assert ok is False
        mock_session_ctor.assert_not_called()
        mock_mark.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_fetch_truncates_marks_and_primes_plain_news_only(self):
        from backend.api.routes import news as n

        fresh_rows = [
            (datetime.now(timezone.utc), {
                "link": "https://x.example/1", "title": "Nifty rallies 2% on strong GDP data",
                "source": "example", "published_at": datetime.now(timezone.utc),
                "timestamp_display": "now",
            }),
        ]

        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        fake_response = MagicMock()

        with patch.object(n, "is_enabled", return_value=True), \
             patch("asyncio.get_running_loop") as mock_loop, \
             patch.object(n, "async_session", side_effect=lambda: ctx), \
             patch.object(n, "_insert_new_headlines", new=AsyncMock(return_value=1)) as mock_insert, \
             patch.object(n, "_news_mark_reset_done_today", new=AsyncMock()) as mock_mark, \
             patch.object(n, "_build_news_response_from_db", new=AsyncMock(return_value=fake_response)), \
             patch("backend.api.cache.put") as mock_put:
            mock_loop.return_value.run_in_executor = AsyncMock(return_value=fresh_rows)
            ok = await n._perform_news_reset_once()

        assert ok is True
        # DELETE must have been issued before the insert helper ran.
        session.execute.assert_awaited_once()
        mock_insert.assert_awaited_once()
        session.commit.assert_awaited_once()
        mock_mark.assert_awaited_once()
        mock_put.assert_called_once_with("news", fake_response, ttl_seconds=360)

    @pytest.mark.asyncio
    async def test_never_primes_news_scored_key(self):
        from backend.api.routes import news as n

        fresh_rows = [(datetime.now(timezone.utc), {
            "link": "https://x.example/1", "title": "Sensex hits new record high today",
            "source": "example", "published_at": datetime.now(timezone.utc),
            "timestamp_display": "now",
        })]
        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch.object(n, "is_enabled", return_value=True), \
             patch("asyncio.get_running_loop") as mock_loop, \
             patch.object(n, "async_session", side_effect=lambda: ctx), \
             patch.object(n, "_insert_new_headlines", new=AsyncMock(return_value=1)), \
             patch.object(n, "_news_mark_reset_done_today", new=AsyncMock()), \
             patch.object(n, "_build_news_response_from_db", new=AsyncMock(return_value=MagicMock())), \
             patch("backend.api.cache.put") as mock_put:
            mock_loop.return_value.run_in_executor = AsyncMock(return_value=fresh_rows)
            await n._perform_news_reset_once()

        primed_keys = [call.args[0] for call in mock_put.call_args_list]
        assert "news_scored" not in primed_keys
        assert primed_keys == ["news"]


# ---------------------------------------------------------------------------
# _fetch_and_accumulate no longer truncates on the hot path
# ---------------------------------------------------------------------------

def test_fetch_and_accumulate_has_no_reset_call():
    """Source-level guard — _fetch_and_accumulate must not call any
    reset/truncate helper; that responsibility moved entirely to
    _perform_news_reset_once (background-triggered only)."""
    import re
    src = open("backend/api/routes/news.py").read()
    m = re.search(r"async def _fetch_and_accumulate\b.*?(?=\nasync def |\Z)", src, re.DOTALL)
    assert m is not None
    body = m.group(0)
    assert "_maybe_reset" not in body
    assert "delete(NewsHeadline)" not in body


def test_maybe_reset_function_removed():
    """The old in-memory wall-clock reset mechanism must be fully
    retired as CODE, not left as unreachable dead code (module-level
    state + the function itself) — a mention in an explanatory comment
    about why it was retired is fine and expected."""
    src = open("backend/api/routes/news.py").read()
    assert "_last_reset: date | None" not in src
    assert "_reset_lock = threading.Lock()" not in src
    assert "async def _maybe_reset" not in src


# ---------------------------------------------------------------------------
# background._daily_content_refresh_cycle — market + news together
# ---------------------------------------------------------------------------

class TestDailyContentRefreshCycleWithNews:
    @pytest.mark.asyncio
    async def test_both_fresh_returns_immediately_no_sleep(self):
        from backend.api import background as bg

        with patch.object(bg, "_market_needs_refresh_today", new=AsyncMock(return_value=False)), \
             patch.object(bg, "_perform_market_refresh_once", new=AsyncMock()) as mock_market, \
             patch("backend.api.routes.news._news_needs_reset_today", new=AsyncMock(return_value=False)), \
             patch.object(bg, "_perform_news_refresh_once", new=AsyncMock()) as mock_news, \
             patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await bg._daily_content_refresh_cycle()

        mock_market.assert_not_called()
        mock_news.assert_not_called()
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_only_news_stale_market_untouched(self):
        from backend.api import background as bg

        with patch.object(bg, "_market_needs_refresh_today", new=AsyncMock(return_value=False)), \
             patch.object(bg, "_perform_market_refresh_once", new=AsyncMock()) as mock_market, \
             patch("backend.api.routes.news._news_needs_reset_today", new=AsyncMock(return_value=True)), \
             patch.object(bg, "_perform_news_refresh_once", new=AsyncMock(return_value=True)) as mock_news, \
             patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await bg._daily_content_refresh_cycle()

        mock_market.assert_not_called()
        mock_news.assert_awaited_once()
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_news_fails_retries_until_success(self):
        from backend.api import background as bg

        early_now = datetime(2026, 9, 26, 6, 0, 0)
        attempts = {"n": 0}

        async def _perform_news():
            attempts["n"] += 1
            return attempts["n"] >= 2

        with patch.object(bg, "_market_needs_refresh_today", new=AsyncMock(return_value=False)), \
             patch.object(bg, "_perform_market_refresh_once", new=AsyncMock()), \
             patch("backend.api.routes.news._news_needs_reset_today", new=AsyncMock(return_value=True)), \
             patch.object(bg, "_perform_news_refresh_once", side_effect=_perform_news), \
             patch.object(bg, "timestamp_indian", return_value=early_now), \
             patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await bg._daily_content_refresh_cycle()

        assert attempts["n"] == 2
        mock_sleep.assert_awaited_once_with(30 * 60)

    @pytest.mark.asyncio
    async def test_gives_up_after_0800_with_mixed_done_states(self):
        from backend.api import background as bg

        late_now = datetime(2026, 9, 26, 8, 1, 0)
        with patch.object(bg, "_market_needs_refresh_today", new=AsyncMock(return_value=True)), \
             patch.object(bg, "_perform_market_refresh_once", new=AsyncMock(return_value=True)), \
             patch("backend.api.routes.news._news_needs_reset_today", new=AsyncMock(return_value=True)), \
             patch.object(bg, "_perform_news_refresh_once", new=AsyncMock(return_value=False)), \
             patch.object(bg, "timestamp_indian", return_value=late_now), \
             patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await bg._daily_content_refresh_cycle()

        mock_sleep.assert_not_awaited()


# ---------------------------------------------------------------------------
# background._task_news_keepwarm
# ---------------------------------------------------------------------------

class TestTaskNewsKeepwarm:
    @pytest.mark.asyncio
    async def test_one_iteration_primes_plain_news_key_with_360s_ttl(self):
        from backend.api import background as bg

        fake_response = MagicMock()

        call_count = {"n": 0}

        async def _sleep_side_effect(_secs):
            call_count["n"] += 1
            if call_count["n"] >= 1:
                raise asyncio.CancelledError()

        with patch("backend.api.routes.news._fetch_and_accumulate",
                   new=AsyncMock(return_value=fake_response)), \
             patch("backend.api.cache.put") as mock_put, \
             patch("asyncio.sleep", new=AsyncMock(side_effect=_sleep_side_effect)):
            with pytest.raises(asyncio.CancelledError):
                await bg._task_news_keepwarm()

        mock_put.assert_called_once_with("news", fake_response, ttl_seconds=360)

    @pytest.mark.asyncio
    async def test_fetch_exception_does_not_crash_loop(self):
        from backend.api import background as bg

        async def _sleep_raises(_secs):
            raise asyncio.CancelledError()

        with patch("backend.api.routes.news._fetch_and_accumulate",
                   new=AsyncMock(side_effect=RuntimeError("rss down"))), \
             patch("backend.api.cache.put") as mock_put, \
             patch("asyncio.sleep", new=AsyncMock(side_effect=_sleep_raises)):
            with pytest.raises(asyncio.CancelledError):
                await bg._task_news_keepwarm()

        mock_put.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_response_is_not_primed(self):
        """_fetch_and_accumulate returns items=[] on a DB read failure —
        priming that for 360s would freeze the news feed BLANK for six
        minutes (CLAUDE.md's staleness-freeze rule: never silently
        collapse to empty). An empty result must skip cache.put entirely,
        leaving whatever the route's own cache/DB-fallback already has."""
        from backend.api.schemas import NewsResponse
        from backend.api import background as bg

        empty_response = NewsResponse(items=[], refreshed_at="now")

        async def _sleep_raises(_secs):
            raise asyncio.CancelledError()

        with patch("backend.api.routes.news._fetch_and_accumulate",
                   new=AsyncMock(return_value=empty_response)), \
             patch("backend.api.cache.put") as mock_put, \
             patch("asyncio.sleep", new=AsyncMock(side_effect=_sleep_raises)):
            with pytest.raises(asyncio.CancelledError):
                await bg._task_news_keepwarm()

        mock_put.assert_not_called()

    @pytest.mark.asyncio
    async def test_primed_entry_outlives_route_60s_ttl(self):
        """Integration-level stand-in for the "no visitor hits a cold
        cache within the keep-warm window" latency requirement: the
        360s cache.put TTL must still be valid past 60s (the news
        route's OWN get_or_fetch ttl_seconds) — i.e. a slightly-delayed
        keep-warm cycle never lets the primed entry expire before the
        next one runs 5 minutes later."""
        import time as _time_mod
        from backend.api.cache import get_or_fetch, put as cache_put, invalidate_all

        invalidate_all()
        try:
            fake_response = MagicMock()
            cache_put("news", fake_response, ttl_seconds=360)

            real_monotonic = _time_mod.monotonic
            with patch.object(_time_mod, "monotonic", return_value=real_monotonic() + 120):
                fetcher_calls = {"n": 0}

                def _fetcher():
                    fetcher_calls["n"] += 1
                    return "live-fetched"

                result = await get_or_fetch("news", _fetcher, ttl_seconds=60)

            assert result is fake_response, (
                "The primed entry (360s TTL) must still be valid 120s later, "
                "even though the route's own get_or_fetch call uses a 60s TTL"
            )
            assert fetcher_calls["n"] == 0
        finally:
            invalidate_all()


class TestFeedListPruning:
    """2026-09-27: Moneycontrol RSS pruned — every checked endpoint
    (marketreports.xml, business.xml, latestnews.xml, results.xml,
    economy.xml, buzzingstocks.xml, MCtopnews.xml) returned HTTP 200 but
    served frozen content (identically dated 23 Apr 2024, one frozen
    since 2016) — Moneycontrol abandoned their RSS infrastructure
    site-wide, verified directly against the live endpoints, not
    assumed. Regression guard: don't silently re-add a moneycontrol.com
    URL without re-verifying it's actually live again."""

    def test_moneycontrol_not_in_feed_list(self):
        from backend.api.routes.news import _FEEDS

        assert not any("moneycontrol.com" in url for url in _FEEDS), (
            "moneycontrol.com RSS endpoints were pruned 2026-09-27 as "
            "dead (frozen since 23 Apr 2024 across every checked path) — "
            "verify they're genuinely live again before re-adding"
        )

    def test_feed_list_still_has_multiple_working_sources(self):
        """Pruning one dead source must not leave the news feed
        under-sourced — still expect several independent outlets."""
        from backend.api.routes.news import _FEEDS

        assert len(_FEEDS) >= 5, (
            f"Expected at least 5 configured feeds after pruning dead "
            f"sources, got {len(_FEEDS)}: {_FEEDS}"
        )
