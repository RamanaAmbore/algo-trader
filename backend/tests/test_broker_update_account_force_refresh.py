"""2026-11-20 fix (F12 — operator explicit new requirement, live ZG0790
CAPTCHA incident). Re-enabling a disabled broker account's connection must
force an immediate, unconditional (non-market-hours-gated) refresh of that
one account's positions/holdings/margins/funds — not leave it stale until
the next routine poll cycle.

This file proves:
  - the False→True transition specifically triggers the forced refresh
    (as a background task, fire-and-forget);
  - a True→False (disable), a True→True (already active, re-saved), and
    any other field-only change do NOT trigger it;
  - `_force_refresh_one_account_after_reenable()` busts both the
    route-level and broker-layer raw caches and re-fetches positions/
    holdings/margins scoped to the one account.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.models import BrokerAccount
from backend.api.routes.brokers import BrokerAccountUpdate, BrokersController


def _make_row(account: str = "ZG0790", is_active: bool = False) -> BrokerAccount:
    row = BrokerAccount()
    row.id = 1
    row.account = account
    row.broker_id = "zerodha_kite"
    row.api_key = "k"
    row.client_id = None
    row.source_ip = None
    row.is_active = is_active
    row.notes = None
    row.priority = 100
    row.extra_config = {}
    row.historical_data_enabled = True
    row.created_at = None
    row.updated_at = None
    row.poll_priority = "hot"
    row.auto_downgrade_enabled = False
    row.auto_downgraded_at = None
    row.auto_downgrade_reason = None
    row.circuit_breaker_enabled = False
    row.display_order = 500
    row.access_token_enc = None
    return row


def _mock_session_for_row(row: BrokerAccount):
    session_mock = AsyncMock()
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    session_mock.execute = AsyncMock(return_value=result_mock)
    session_mock.commit = AsyncMock()
    session_mock.refresh = AsyncMock()
    factory = MagicMock(return_value=session_mock)
    return factory


async def _call_update_account(account: str, row: BrokerAccount, data: BrokerAccountUpdate):
    """Call the real `update_account` handler against a mocked DB session,
    with `_reload_connections` and `_loaded_accounts` neutralised, and
    capture whether/how `_force_refresh_one_account_after_reenable` was
    scheduled. Returns (response, refresh_calls, pending_tasks)."""
    import backend.api.routes.brokers as brokers_mod

    factory = _mock_session_for_row(row)
    refresh_calls: list[str] = []

    async def fake_force_refresh(acct: str) -> None:
        refresh_calls.append(acct)

    controller = BrokersController.__new__(BrokersController)
    update_account_fn = BrokersController.update_account.fn
    with patch.object(brokers_mod, "shared_async_session", factory), patch.object(
        brokers_mod, "_reload_connections", new=AsyncMock(),
    ), patch.object(
        brokers_mod, "_loaded_accounts", return_value=set(),
    ), patch.object(
        brokers_mod, "_force_refresh_one_account_after_reenable", new=fake_force_refresh,
    ):
        before_tasks = asyncio.all_tasks()
        resp = await update_account_fn(controller, account, data)
        new_tasks = asyncio.all_tasks() - before_tasks
        for t in new_tasks:
            await t
    return resp, refresh_calls


class TestForceRefreshOnlyOnFalseToTrueTransition:
    @pytest.mark.asyncio
    async def test_false_to_true_triggers_forced_refresh(self):
        row = _make_row(is_active=False)
        data = BrokerAccountUpdate(is_active=True)
        resp, refresh_calls = await _call_update_account("ZG0790", row, data)
        assert refresh_calls == ["ZG0790"], (
            f"expected the forced refresh to fire exactly once for ZG0790, got {refresh_calls!r}"
        )
        assert resp.is_active is True

    @pytest.mark.asyncio
    async def test_true_to_false_does_not_trigger_forced_refresh(self):
        """Disabling an account must never trigger this — only
        re-enabling (False→True) does."""
        row = _make_row(is_active=True)
        data = BrokerAccountUpdate(is_active=False)
        _resp, refresh_calls = await _call_update_account("ZG0790", row, data)
        assert refresh_calls == []

    @pytest.mark.asyncio
    async def test_already_active_resaved_true_does_not_trigger_forced_refresh(self):
        """A no-op is_active=True PATCH on an ALREADY-active account
        (e.g. the operator re-saving the same form) must not re-trigger
        the forced refresh — only a genuine False→True transition."""
        row = _make_row(is_active=True)
        data = BrokerAccountUpdate(is_active=True)
        _resp, refresh_calls = await _call_update_account("ZG0790", row, data)
        assert refresh_calls == []

    @pytest.mark.asyncio
    async def test_unrelated_field_change_does_not_trigger_forced_refresh(self):
        """A PATCH that changes some OTHER field (priority) on an
        already-disabled account, with `is_active` entirely omitted
        (None, not explicitly set), must not trigger the forced
        refresh — only an explicit is_active=True transition does."""
        row = _make_row(is_active=False)
        data = BrokerAccountUpdate(priority=50)
        _resp, refresh_calls = await _call_update_account("ZG0790", row, data)
        assert refresh_calls == []

    @pytest.mark.asyncio
    async def test_already_inactive_explicit_false_does_not_trigger(self):
        """Explicitly re-confirming is_active=False on an already-
        inactive account — no transition at all — must not trigger."""
        row = _make_row(is_active=False)
        data = BrokerAccountUpdate(is_active=False)
        _resp, refresh_calls = await _call_update_account("ZG0790", row, data)
        assert refresh_calls == []


class TestForceRefreshHelperBustsCachesAndRefetchesScopedToOneAccount:
    @pytest.mark.asyncio
    async def test_invalidates_route_and_raw_caches_and_refetches_one_account(self):
        from backend.api.routes.brokers import _force_refresh_one_account_after_reenable

        invalidated_route: list[str] = []
        invalidated_raw: list[str] = []
        fetch_calls: dict[str, list] = {"positions": [], "holdings": [], "margins": []}

        def fake_invalidate(key):
            invalidated_route.append(key)

        def fake_raw_invalidate(key):
            invalidated_raw.append(key)

        def fake_fetch_positions(*, force_refresh=False):
            fetch_calls["positions"].append(force_refresh)
            return []

        def fake_fetch_holdings():
            fetch_calls["holdings"].append(True)
            return []

        def fake_fetch_margins(*, force_refresh=False):
            fetch_calls["margins"].append(force_refresh)
            return []

        with patch("backend.api.cache.invalidate", fake_invalidate), patch(
            "backend.brokers.broker_apis._raw_cache_invalidate", fake_raw_invalidate,
        ), patch(
            "backend.brokers.broker_apis.fetch_positions", fake_fetch_positions,
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings", fake_fetch_holdings,
        ), patch(
            "backend.brokers.broker_apis.fetch_margins", fake_fetch_margins,
        ), patch(
            # Out of scope for this test — the daily_book snapshot
            # follow-up has its own dedicated test class below.
            "backend.api.routes.brokers._snapshot_books_if_closed", new=AsyncMock(),
        ):
            await _force_refresh_one_account_after_reenable("ZG0790")

        assert set(invalidated_route) == {"positions", "holdings", "funds"}
        assert set(invalidated_raw) == {"positions", "holdings", "margins"}
        # Zero-arg forms — the SSOT-cache-populating path, NOT the
        # single-account branch (which bypasses that cache entirely and
        # would leave its result discarded). See the advisor-driven
        # correction in `_force_refresh_one_account_after_reenable`'s
        # own docstring.
        assert fetch_calls["positions"] == [True]
        assert fetch_calls["holdings"] == [True]
        assert fetch_calls["margins"] == [True]

    @pytest.mark.asyncio
    async def test_never_calls_with_account_kwarg_the_cache_bypassing_branch(self):
        """Regression guard for the exact bug the advisor review caught:
        the single-account `account=` branch of each `fetch_*` function
        bypasses the all-accounts SSOT cache entirely, so a fetch made
        that way is thrown away and never reaches the next route read.
        Asserts positional/keyword call shape directly rather than only
        checking call counts."""
        from backend.api.routes.brokers import _force_refresh_one_account_after_reenable

        calls = {"positions": None, "holdings": None, "margins": None}

        def fake_fetch_positions(*args, **kwargs):
            calls["positions"] = (args, kwargs)
            return []

        def fake_fetch_holdings(*args, **kwargs):
            calls["holdings"] = (args, kwargs)
            return []

        def fake_fetch_margins(*args, **kwargs):
            calls["margins"] = (args, kwargs)
            return []

        with patch("backend.api.cache.invalidate"), patch(
            "backend.brokers.broker_apis._raw_cache_invalidate",
        ), patch(
            "backend.brokers.broker_apis.fetch_positions", fake_fetch_positions,
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings", fake_fetch_holdings,
        ), patch(
            "backend.brokers.broker_apis.fetch_margins", fake_fetch_margins,
        ), patch(
            "backend.api.routes.brokers._snapshot_books_if_closed", new=AsyncMock(),
        ):
            await _force_refresh_one_account_after_reenable("ZG0790")

        for name, (args, kwargs) in calls.items():
            assert "account" not in kwargs, (
                f"fetch_{name} was called with account= — this is the cache-"
                f"bypassing single-account branch, exactly the bug this test guards against"
            )
            assert args == (), f"fetch_{name} must be called with zero positional args too"

    @pytest.mark.asyncio
    async def test_a_broker_failure_during_refresh_never_raises(self):
        """Best-effort — a transient broker error during the forced
        refresh must never propagate (this runs as a detached background
        task; an unhandled exception there would be silently swallowed
        by asyncio anyway, but the function itself must degrade
        gracefully, matching every other best-effort helper in this
        file)."""
        from backend.api.routes.brokers import _force_refresh_one_account_after_reenable

        with patch(
            "backend.brokers.broker_apis.fetch_positions",
            side_effect=RuntimeError("broker down"),
        ), patch(
            "backend.api.routes.brokers._snapshot_books_if_closed", new=AsyncMock(),
        ):
            await _force_refresh_one_account_after_reenable("ZG0790")  # must not raise


class TestBooksRefreshLogging:
    """2026-11-20 follow-up (operator request, via the coordinator) —
    the forced refresh must leave an explicit, visible log trace: an
    INFO line when it starts, an INFO (or WARNING, on partial failure)
    line when it completes, and a WARNING naming any surface that
    failed individually — with the account masked everywhere, matching
    the codebase-wide `mask_account()` convention.

    `backend/api/routes/brokers.py`'s module logger is created via
    `get_logger()`, which sets `propagate=False` on a dedicated
    QueueHandler — pytest's `caplog` captures via the root logger by
    default and never sees these records (same documented gap as
    `test_bse_ticker_subscription.py::test_bse_empty_instruments_logs_
    warning`). These tests patch the module-level `logger` object
    directly instead, the established pattern for this exact gap."""

    def _recording_logger(self):
        calls = {"info": [], "warning": []}

        class _Recorder:
            def info(self, msg, *a, **k):
                calls["info"].append(msg % a if a else msg)

            def warning(self, msg, *a, **k):
                calls["warning"].append(msg % a if a else msg)

        return _Recorder(), calls

    @pytest.mark.asyncio
    async def test_logs_info_on_start_and_completion_with_masked_account(self):
        import backend.api.routes.brokers as brokers_mod
        from backend.api.routes.brokers import _force_refresh_one_account_after_reenable

        recorder, calls = self._recording_logger()

        with patch.object(brokers_mod, "logger", recorder), patch.object(
            brokers_mod, "_snapshot_books_if_closed", new=AsyncMock(),
        ), patch(
            "backend.api.cache.invalidate",
        ), patch(
            "backend.brokers.broker_apis._raw_cache_invalidate",
        ), patch(
            "backend.brokers.broker_apis.fetch_positions", return_value=[],
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings", return_value=[],
        ), patch(
            "backend.brokers.broker_apis.fetch_margins", return_value=[],
        ):
            await _force_refresh_one_account_after_reenable("ZG0790")

        start_lines = [m for m in calls["info"] if "connection re-enabled" in m]
        complete_lines = [m for m in calls["info"] if "forced refresh complete" in m]
        assert len(start_lines) == 1, f"expected exactly one start line, got: {calls!r}"
        assert len(complete_lines) == 1, f"expected exactly one completion line, got: {calls!r}"
        assert "ZG0790" not in start_lines[0] and "ZG0790" not in complete_lines[0], (
            "the raw account code must never appear in the log — only the masked form"
        )
        assert "ZG####" in start_lines[0]
        assert "ZG####" in complete_lines[0]
        assert "[BOOKS-REFRESH]" in start_lines[0]
        assert "bypassing closed-hours gate" in start_lines[0]
        # Completion is INFO, not WARNING, when every surface succeeded.
        assert calls["warning"] == [], f"no WARNING expected on a clean run, got: {calls['warning']!r}"

    @pytest.mark.asyncio
    async def test_one_failed_surface_does_not_skip_the_other_two(self):
        """Per-surface isolation — positions failing must not prevent
        holdings and margins from still being attempted. This is the
        regression this follow-up fixes: the original implementation
        wrapped all three calls in ONE try/except, so a positions
        failure silently aborted holdings/margins too."""
        import backend.api.routes.brokers as brokers_mod
        from backend.api.routes.brokers import _force_refresh_one_account_after_reenable

        recorder, calls = self._recording_logger()
        holdings_called = []
        margins_called = []

        def fake_holdings():
            holdings_called.append(True)
            return []

        def fake_margins(*, force_refresh=False):
            margins_called.append(force_refresh)
            return []

        with patch.object(brokers_mod, "logger", recorder), patch.object(
            brokers_mod, "_snapshot_books_if_closed", new=AsyncMock(),
        ), patch(
            "backend.api.cache.invalidate",
        ), patch(
            "backend.brokers.broker_apis._raw_cache_invalidate",
        ), patch(
            "backend.brokers.broker_apis.fetch_positions",
            side_effect=RuntimeError("positions broker down"),
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings", fake_holdings,
        ), patch(
            "backend.brokers.broker_apis.fetch_margins", fake_margins,
        ):
            await _force_refresh_one_account_after_reenable("ZG0790")

        assert holdings_called == [True], "holdings must still be attempted after positions failed"
        assert margins_called == [True], "margins must still be attempted after positions failed"

        positions_warnings = [m for m in calls["warning"] if "positions refresh failed" in m]
        assert len(positions_warnings) == 1, f"expected one WARNING naming positions, got: {calls!r}"
        assert "ZG####" in positions_warnings[0]
        assert "ZG0790" not in positions_warnings[0]

        # Completion still logs, but as a WARNING (partial failure), naming the failed surface.
        completion = [m for m in calls["warning"] if "forced refresh complete" in m]
        assert len(completion) == 1
        assert "positions" in completion[0]
        assert not any("forced refresh complete" in m for m in calls["info"]), (
            "completion must log as WARNING, not INFO, on a partial failure"
        )

    @pytest.mark.asyncio
    async def test_all_three_surfaces_failing_logs_three_distinct_warnings(self):
        import backend.api.routes.brokers as brokers_mod
        from backend.api.routes.brokers import _force_refresh_one_account_after_reenable

        recorder, calls = self._recording_logger()

        with patch.object(brokers_mod, "logger", recorder), patch.object(
            brokers_mod, "_snapshot_books_if_closed", new=AsyncMock(),
        ), patch(
            "backend.api.cache.invalidate",
        ), patch(
            "backend.brokers.broker_apis._raw_cache_invalidate",
        ), patch(
            "backend.brokers.broker_apis.fetch_positions",
            side_effect=RuntimeError("positions down"),
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings",
            side_effect=RuntimeError("holdings down"),
        ), patch(
            "backend.brokers.broker_apis.fetch_margins",
            side_effect=RuntimeError("margins down"),
        ):
            await _force_refresh_one_account_after_reenable("ZG0790")

        assert any("positions refresh failed" in m for m in calls["warning"])
        assert any("holdings refresh failed" in m for m in calls["warning"])
        assert any("margins/funds refresh failed" in m for m in calls["warning"])
        completion = [m for m in calls["warning"] if "forced refresh complete" in m]
        assert len(completion) == 1
        assert "positions" in completion[0] and "holdings" in completion[0] and "margins/funds" in completion[0]


class TestLastCompletedTradingSessionDate:
    """2026-11-20 fix (F12 follow-up — advisor-driven correction, live
    incident). `session_date_of(now)` alone advances to a NEW session
    date at every 08:00 IST boundary regardless of whether that date is
    a real trading day — the exact bug class `_task_closed_hours_
    refresh`'s own docstring documents (it stopped calling `snapshot_
    daily_book()` entirely for this reason: a Saturday-dated refresh
    row became `latest_batch`, collapsing Day P&L to zero)."""

    @pytest.mark.asyncio
    async def test_weekday_evening_after_close_resolves_to_the_same_day(self):
        import zoneinfo
        from backend.api.routes.brokers import _last_completed_trading_session_date

        IST = zoneinfo.ZoneInfo("Asia/Kolkata")
        now_ist = datetime(2026, 11, 20, 20, 0, tzinfo=IST)  # Friday evening

        with patch(
            "backend.brokers.broker_apis.fetch_holidays", return_value=set(),
        ):
            result = await _last_completed_trading_session_date(now_ist)

        assert result == date(2026, 11, 20), (
            f"a Friday-evening re-enable (same trading day, already closed) "
            f"must resolve to that same Friday, got {result}"
        )

    @pytest.mark.asyncio
    async def test_saturday_morning_floor_walks_back_to_friday(self):
        """THE key regression case — tested past midnight IST into a
        weekend is exactly the scenario the coordinator hit live."""
        import zoneinfo
        from backend.api.routes.brokers import _last_completed_trading_session_date

        IST = zoneinfo.ZoneInfo("Asia/Kolkata")
        now_ist = datetime(2026, 11, 21, 10, 0, tzinfo=IST)  # Saturday morning

        with patch(
            "backend.brokers.broker_apis.fetch_holidays", return_value=set(),
        ):
            result = await _last_completed_trading_session_date(now_ist)

        assert result == date(2026, 11, 20), (
            f"Saturday must floor-walk back to the real last trading day "
            f"(Friday), got {result} — writing a Saturday-dated row would "
            f"reproduce the exact _task_closed_hours_refresh regression"
        )

    @pytest.mark.asyncio
    async def test_sunday_morning_floor_walks_back_to_friday(self):
        import zoneinfo
        from backend.api.routes.brokers import _last_completed_trading_session_date

        IST = zoneinfo.ZoneInfo("Asia/Kolkata")
        now_ist = datetime(2026, 11, 22, 10, 0, tzinfo=IST)  # Sunday morning

        with patch(
            "backend.brokers.broker_apis.fetch_holidays", return_value=set(),
        ):
            result = await _last_completed_trading_session_date(now_ist)

        assert result == date(2026, 11, 20)

    @pytest.mark.asyncio
    async def test_a_declared_holiday_is_also_walked_past(self):
        import zoneinfo
        from backend.api.routes.brokers import _last_completed_trading_session_date

        IST = zoneinfo.ZoneInfo("Asia/Kolkata")
        # Friday 2026-11-20 declared a holiday -> must walk back to Thursday.
        now_ist = datetime(2026, 11, 20, 10, 0, tzinfo=IST)

        with patch(
            "backend.brokers.broker_apis.fetch_holidays",
            return_value={date(2026, 11, 20)},
        ):
            result = await _last_completed_trading_session_date(now_ist)

        assert result == date(2026, 11, 19)

    @pytest.mark.asyncio
    async def test_holiday_fetch_failure_degrades_to_weekday_only_check(self):
        """A `fetch_holidays` failure must not raise — degrades to an
        empty holiday set (weekday-only check), same fail-safe
        convention as every other best-effort helper in this file."""
        import zoneinfo
        from backend.api.routes.brokers import _last_completed_trading_session_date

        IST = zoneinfo.ZoneInfo("Asia/Kolkata")
        now_ist = datetime(2026, 11, 21, 10, 0, tzinfo=IST)  # Saturday

        with patch(
            "backend.brokers.broker_apis.fetch_holidays",
            side_effect=RuntimeError("holiday service down"),
        ):
            result = await _last_completed_trading_session_date(now_ist)

        assert result == date(2026, 11, 20)  # still walks back to Friday


class TestDailyBookRowExists:
    @pytest.mark.asyncio
    async def test_returns_true_when_a_row_exists(self):
        import backend.api.routes.brokers as brokers_mod
        from backend.api.routes.brokers import _daily_book_row_exists

        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        result_mock = MagicMock()
        result_mock.first.return_value = (1,)
        session_mock.execute = AsyncMock(return_value=result_mock)
        factory = MagicMock(return_value=session_mock)

        # Matches every other DB session open in this file —
        # `shared_async_session`, not the plain `async_session` (see
        # `test_shared_broker_db.py`'s source-grep guard).
        with patch.object(brokers_mod, "shared_async_session", factory):
            exists = await _daily_book_row_exists("ZG0790", date(2026, 11, 20))

        assert exists is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_row_exists(self):
        import backend.api.routes.brokers as brokers_mod
        from backend.api.routes.brokers import _daily_book_row_exists

        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        result_mock = MagicMock()
        result_mock.first.return_value = None
        session_mock.execute = AsyncMock(return_value=result_mock)
        factory = MagicMock(return_value=session_mock)

        with patch.object(brokers_mod, "shared_async_session", factory):
            exists = await _daily_book_row_exists("ZG0790", date(2026, 11, 20))

        assert exists is False


class TestSnapshotBooksIfClosed:
    """2026-11-20 fix (F12 follow-up — live, operator-confirmed: ZG0790
    was re-enabled but `daily_book`'s last row stayed dated 2026-11-19,
    because `closed_hours_or_broker()`'s route gate serves `daily_book`
    ONLY during closed hours and never consults the live cache this
    fix's earlier half refreshes)."""

    @pytest.mark.asyncio
    async def test_market_open_skips_the_snapshot_entirely(self):
        from backend.api.routes.brokers import _snapshot_books_if_closed

        snapshot_calls = []

        async def fake_snapshot(**kwargs):
            snapshot_calls.append(kwargs)
            return {"accounts": ["ZG0790"], "holdings_rows": 1, "positions_rows": 1, "errors": []}

        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=True,
        ), patch(
            "backend.api.algo.daily_snapshot.snapshot_daily_book", fake_snapshot,
        ):
            await _snapshot_books_if_closed("ZG0790", "ZG####")

        assert snapshot_calls == [], "market is open — no snapshot should be written at all"

    @pytest.mark.asyncio
    async def test_market_closed_no_existing_row_writes_one_scoped_to_this_account(self):
        from backend.api.routes.brokers import _snapshot_books_if_closed

        snapshot_calls = []

        async def fake_snapshot(**kwargs):
            snapshot_calls.append(kwargs)
            return {"accounts": ["ZG0790"], "holdings_rows": 3, "positions_rows": 2, "errors": []}

        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=False,
        ), patch(
            "backend.api.routes.brokers._last_completed_trading_session_date",
            new=AsyncMock(return_value=date(2026, 11, 20)),
        ), patch(
            "backend.api.routes.brokers._daily_book_row_exists",
            new=AsyncMock(return_value=False),
        ), patch(
            "backend.api.algo.daily_snapshot.snapshot_daily_book", fake_snapshot,
        ):
            await _snapshot_books_if_closed("ZG0790", "ZG####")

        assert len(snapshot_calls) == 1
        call = snapshot_calls[0]
        assert call["accounts"] == ["ZG0790"], "must scope to only this one account"
        assert call["target_date"] == date(2026, 11, 20)
        assert call["market_open"] is False
        assert "settled" not in call, (
            "settled is reserved for the real close_settled event, must not be set here"
        )

    @pytest.mark.asyncio
    async def test_market_closed_existing_row_skips_the_write(self):
        """Never overwrite an already-correct EOD row, and never risk
        satisfying `_pcf_eod_already_fired_today`'s account-unscoped
        existence check for a day whose real EOD hasn't run for every
        OTHER account yet."""
        from backend.api.routes.brokers import _snapshot_books_if_closed

        snapshot_calls = []

        async def fake_snapshot(**kwargs):
            snapshot_calls.append(kwargs)
            return {"accounts": ["ZG0790"], "holdings_rows": 0, "positions_rows": 0, "errors": []}

        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=False,
        ), patch(
            "backend.api.routes.brokers._last_completed_trading_session_date",
            new=AsyncMock(return_value=date(2026, 11, 20)),
        ), patch(
            "backend.api.routes.brokers._daily_book_row_exists",
            new=AsyncMock(return_value=True),
        ), patch(
            "backend.api.algo.daily_snapshot.snapshot_daily_book", fake_snapshot,
        ):
            await _snapshot_books_if_closed("ZG0790", "ZG####")

        assert snapshot_calls == [], "a row already exists for that session — must not overwrite it"

    @pytest.mark.asyncio
    async def test_account_missing_from_result_logs_warning_not_raise(self):
        from backend.api.routes.brokers import _snapshot_books_if_closed
        import backend.api.routes.brokers as brokers_mod

        async def fake_snapshot(**kwargs):
            return {"accounts": [], "holdings_rows": 0, "positions_rows": 0, "errors": ["boom"]}

        warnings = []

        class _Recorder:
            def info(self, msg, *a, **k):
                pass

            def warning(self, msg, *a, **k):
                warnings.append(msg)

        with patch.object(brokers_mod, "logger", _Recorder()), patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=False,
        ), patch(
            "backend.api.routes.brokers._last_completed_trading_session_date",
            new=AsyncMock(return_value=date(2026, 11, 20)),
        ), patch(
            "backend.api.routes.brokers._daily_book_row_exists",
            new=AsyncMock(return_value=False),
        ), patch(
            "backend.api.algo.daily_snapshot.snapshot_daily_book", fake_snapshot,
        ):
            await _snapshot_books_if_closed("ZG0790", "ZG####")  # must not raise

        assert any("did not complete cleanly" in m for m in warnings)

    @pytest.mark.asyncio
    async def test_any_exception_is_caught_and_logged_never_raised(self):
        from backend.api.routes.brokers import _snapshot_books_if_closed

        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open",
            side_effect=RuntimeError("gate check failed"),
        ):
            await _snapshot_books_if_closed("ZG0790", "ZG####")  # must not raise
