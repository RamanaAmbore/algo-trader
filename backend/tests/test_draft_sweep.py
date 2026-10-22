"""
Item 2 (2026-10-22) — sweep unpromoted draft positions at the next
market-open 08:00 IST cycle.

Drafts (`AlgoOrder` rows, `mode='draft'`, `status='OPEN'`) have no
lifecycle today — nothing expires them. A draft's whole premise is "what
if I enter *now*": its `avg_cost` is frozen at creation-time LTP, so a
day-old draft no longer answers that question. `_sweep_stale_drafts()` /
`_ds_tick_draft_sweep()` (backend/api/background.py) auto-clear any draft
surviving to the next 08:00 IST trading-day boundary after its creation.

Does NOT mock broker API calls — uses a real in-process SQLite
`algo_orders` table (the real `AlgoOrder` model has no JSONB columns, so
it compiles cleanly on SQLite) instead of a DB mock, so the sweep's own
WHERE clause (mode/status/created_at) is exercised for real.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

IST = ZoneInfo("Asia/Kolkata")


@pytest_asyncio.fixture
async def sqlite_session_factory():
    """Async session factory backed by an in-process SQLite DB, seeded
    with the real `algo_orders` table only (AlgoOrder has no JSONB
    columns so it compiles cleanly on SQLite)."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from backend.api.models import AlgoOrder

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AlgoOrder.__table__.create)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _insert_order(factory, **overrides) -> int:
    from backend.api.models import AlgoOrder

    defaults = dict(
        account="ZG0790", symbol="GOLD26OCT142000PE", exchange="MCX",
        transaction_type="BUY", quantity=10, product="NRML",
        status="OPEN", mode="draft",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    async with factory() as s:
        row = AlgoOrder(**defaults)
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


class TestSweepStaleDrafts:
    async def test_draft_before_cutoff_is_swept(self, sqlite_session_factory):
        from backend.api.background import _sweep_stale_drafts

        cutoff = datetime(2026, 10, 22, 8, 0, tzinfo=IST)
        old_created = cutoff - timedelta(hours=2)
        draft_id = await _insert_order(sqlite_session_factory, created_at=old_created)

        with patch("backend.api.database.async_session", sqlite_session_factory), \
             patch("backend.api.helpers.exchange_clock.settlement_cutoff_for", AsyncMock(return_value=cutoff)):
            n = await _sweep_stale_drafts()

        assert n == 1
        async with sqlite_session_factory() as s:
            from sqlalchemy import select
            from backend.api.models import AlgoOrder
            remaining = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == draft_id))).scalar_one_or_none()
        assert remaining is None

    async def test_draft_created_after_cutoff_survives(self, sqlite_session_factory):
        from backend.api.background import _sweep_stale_drafts

        cutoff = datetime(2026, 10, 22, 8, 0, tzinfo=IST)
        new_created = cutoff + timedelta(hours=2)
        draft_id = await _insert_order(sqlite_session_factory, created_at=new_created)

        with patch("backend.api.database.async_session", sqlite_session_factory), \
             patch("backend.api.helpers.exchange_clock.settlement_cutoff_for", AsyncMock(return_value=cutoff)):
            n = await _sweep_stale_drafts()

        assert n == 0
        async with sqlite_session_factory() as s:
            from sqlalchemy import select
            from backend.api.models import AlgoOrder
            remaining = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == draft_id))).scalar_one_or_none()
        assert remaining is not None

    async def test_non_draft_row_never_touched_regardless_of_age(self, sqlite_session_factory):
        """A live/paper AlgoOrder row, however old, must survive the sweep
        unconditionally — the WHERE clause AND `_draft_delete_by_id`'s own
        guard both gate on mode=='draft'."""
        from backend.api.background import _sweep_stale_drafts

        cutoff = datetime(2026, 10, 22, 8, 0, tzinfo=IST)
        ancient = cutoff - timedelta(days=30)
        live_id = await _insert_order(
            sqlite_session_factory, mode="live", status="OPEN", created_at=ancient,
        )
        filled_draft_id = await _insert_order(
            sqlite_session_factory, mode="draft", status="FILLED", created_at=ancient,
        )

        with patch("backend.api.database.async_session", sqlite_session_factory), \
             patch("backend.api.helpers.exchange_clock.settlement_cutoff_for", AsyncMock(return_value=cutoff)):
            n = await _sweep_stale_drafts()

        # Neither row matches mode='draft' AND status='OPEN' together.
        assert n == 0
        async with sqlite_session_factory() as s:
            from sqlalchemy import select
            from backend.api.models import AlgoOrder
            live_row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == live_id))).scalar_one_or_none()
            draft_row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == filled_draft_id))).scalar_one_or_none()
        assert live_row is not None
        assert draft_row is not None

    async def test_query_failure_returns_zero_not_raise(self):
        from backend.api.background import _sweep_stale_drafts

        cutoff = datetime(2026, 10, 22, 8, 0, tzinfo=IST)
        broken_session = AsyncMock()
        broken_session.__aenter__ = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("backend.api.database.async_session", return_value=broken_session), \
             patch("backend.api.helpers.exchange_clock.settlement_cutoff_for", AsyncMock(return_value=cutoff)):
            n = await _sweep_stale_drafts()
        assert n == 0


def _patch_no_holidays():
    """`fetch_holidays("NSE")` returns an empty set — the trading-day
    check then falls through purely to the weekday test."""
    return patch("backend.brokers.broker_apis.fetch_holidays", return_value=set())


class TestDraftSweepTick:
    """`_ds_tick_draft_sweep` gates on `_draft_sweep_is_trading_day` (real
    calendar check: weekday + `fetch_holidays("NSE")`), NOT
    `exchange_clock.get_nse_open_time()` — see `_draft_sweep_is_trading_day`'s
    own docstring for why that function is unsafe here (it returns 08:00,
    not None, on a bare weekend with no explicit holiday override row)."""

    async def test_fires_on_trading_day_at_0800(self):
        from backend.api.background import _ds_tick_draft_sweep

        now = datetime(2026, 10, 22, 8, 0, tzinfo=IST)  # Thursday
        today = now.date()
        done_ref = [None]
        with _patch_no_holidays(), \
             patch("backend.api.background._sweep_stale_drafts", AsyncMock(return_value=0)) as mock_sweep:
            await _ds_tick_draft_sweep(now, today, done_ref)
        mock_sweep.assert_awaited_once()
        assert done_ref[0] == today

    async def test_no_fire_before_0800(self):
        from backend.api.background import _ds_tick_draft_sweep

        now = datetime(2026, 10, 22, 7, 59, tzinfo=IST)
        today = now.date()
        done_ref = [None]
        with _patch_no_holidays(), \
             patch("backend.api.background._sweep_stale_drafts", AsyncMock(return_value=0)) as mock_sweep:
            await _ds_tick_draft_sweep(now, today, done_ref)
        mock_sweep.assert_not_awaited()
        assert done_ref[0] is None

    async def test_no_fire_on_real_weekend(self):
        """A real Saturday, with NO mocked `get_nse_open_time` override —
        `exchange_clock.get_nse_open_time()` would (incorrectly) return
        08:00 here, not None, which is exactly why the gate must NOT use
        it. `_draft_sweep_is_trading_day` catches this via the plain
        weekday check instead."""
        from backend.api.background import _ds_tick_draft_sweep

        now = datetime(2026, 10, 24, 9, 0, tzinfo=IST)  # Saturday
        today = now.date()
        done_ref = [None]
        with _patch_no_holidays(), \
             patch("backend.api.background._sweep_stale_drafts", AsyncMock(return_value=0)) as mock_sweep:
            await _ds_tick_draft_sweep(now, today, done_ref)
        mock_sweep.assert_not_awaited()
        assert done_ref[0] is None

    async def test_no_fire_on_real_holiday(self):
        """A weekday that `fetch_holidays("NSE")` reports as a holiday —
        `_draft_sweep_is_trading_day` must respect the real calendar, not
        just the weekday."""
        from backend.api.background import _ds_tick_draft_sweep

        now = datetime(2026, 10, 22, 9, 0, tzinfo=IST)  # Thursday
        today = now.date()
        done_ref = [None]
        with patch("backend.brokers.broker_apis.fetch_holidays", return_value={today}), \
             patch("backend.api.background._sweep_stale_drafts", AsyncMock(return_value=0)) as mock_sweep:
            await _ds_tick_draft_sweep(now, today, done_ref)
        mock_sweep.assert_not_awaited()
        assert done_ref[0] is None

    async def test_fires_only_once_per_day(self):
        from backend.api.background import _ds_tick_draft_sweep

        now = datetime(2026, 10, 22, 8, 30, tzinfo=IST)
        today = now.date()
        done_ref = [today]  # already fired today
        with _patch_no_holidays(), \
             patch("backend.api.background._sweep_stale_drafts", AsyncMock(return_value=0)) as mock_sweep:
            await _ds_tick_draft_sweep(now, today, done_ref)
        mock_sweep.assert_not_awaited()

    async def test_notifies_only_when_drafts_cleared(self):
        from backend.api.background import _ds_tick_draft_sweep

        now = datetime(2026, 10, 22, 8, 0, tzinfo=IST)
        today = now.date()
        done_ref = [None]
        with _patch_no_holidays(), \
             patch("backend.api.background._sweep_stale_drafts", AsyncMock(return_value=3)), \
             patch("backend.shared.helpers.alert_utils._send_telegram_info") as mock_tg:
            await _ds_tick_draft_sweep(now, today, done_ref)
        mock_tg.assert_called_once()
        assert "3" in mock_tg.call_args[0][0]

    async def test_no_notify_when_nothing_swept(self):
        from backend.api.background import _ds_tick_draft_sweep

        now = datetime(2026, 10, 22, 8, 0, tzinfo=IST)
        today = now.date()
        done_ref = [None]
        with _patch_no_holidays(), \
             patch("backend.api.background._sweep_stale_drafts", AsyncMock(return_value=0)), \
             patch("backend.shared.helpers.alert_utils._send_telegram_info") as mock_tg:
            await _ds_tick_draft_sweep(now, today, done_ref)
        mock_tg.assert_not_called()
