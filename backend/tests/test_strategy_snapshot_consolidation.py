"""
2026-10 audit fix: the per-strategy snapshot roll-up ran up to 2 extra
DB round-trips PER ACTIVE STRATEGY (an open-notional SELECT and an
AlgoOrder.pnl-fallback SELECT, each scoped to a single strategy_id),
and the whole writer function was duplicated near-verbatim in TWO
places in backend/api/background.py: `_task_strategy_snapshot` (an
unscheduled standalone-loop task — never started via `asyncio.create_task`,
confirmed dead) and `_run_strategy_snapshot_once` (the live path, called
every 30s from `_task_post_market_cron`).

Fix: `_task_strategy_snapshot` removed entirely (dead code). The two
per-strategy SELECTs are now each ONE grouped query
(`_strategy_snapshot_notional_by_id` /
`_strategy_snapshot_algo_pnl_fallback_by_id`) evaluated ONCE before the
per-strategy loop, keyed by strategy_id. `compute_strategy_pnl` /
`compute_unrealised_marked_to_ltp` stay per-strategy calls — shared
with routes/strategies.py's single-strategy detail view, and the
latter does a ticker/broker LTP lookup that isn't a pure SQL aggregate.
"""
from __future__ import annotations

import inspect
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.background import (
    _strategy_snapshot_notional_by_id,
    _strategy_snapshot_algo_pnl_fallback_by_id,
    _run_strategy_snapshot_once,
)


def test_task_strategy_snapshot_dead_code_removed():
    """The unscheduled duplicate must be gone entirely, not just unused."""
    import backend.api.background as bg
    assert not hasattr(bg, "_task_strategy_snapshot"), (
        "_task_strategy_snapshot was dead code (never asyncio.create_task'd) "
        "and must be removed, not merely left unscheduled"
    )


def test_task_strategy_snapshot_not_in_startup_create_task_list():
    """Belt-and-suspenders source check: no asyncio.create_task call must
    reference the removed function name."""
    import backend.api.background as bg
    src = inspect.getsource(bg)
    # Only the historical docstring mention in _task_post_market_cron may
    # remain; there must be no asyncio.create_task(_task_strategy_snapshot...
    assert "asyncio.create_task(_task_strategy_snapshot" not in src


class _Row(SimpleNamespace):
    """Minimal (strategy_id, total) row-tuple-like object."""
    def __iter__(self):
        return iter((self.strategy_id, self.total))


def _result_with_rows(rows: list[tuple]):
    result = MagicMock()
    result.all.return_value = rows
    return result


@pytest.mark.asyncio
class TestStrategySnapshotNotionalById:
    async def test_groups_by_strategy_id(self):
        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_result_with_rows([(1, 5000.0), (2, 1500.0)])
        )
        result = await _strategy_snapshot_notional_by_id(session, [1, 2])
        assert result == {1: 5000.0, 2: 1500.0}
        session.execute.assert_awaited_once()

    async def test_empty_strategy_ids_skips_query(self):
        session = AsyncMock()
        result = await _strategy_snapshot_notional_by_id(session, [])
        assert result == {}
        session.execute.assert_not_called()

    async def test_missing_strategy_id_not_in_result(self):
        """A strategy with no open lots simply has no entry — caller
        treats a missing key as 0.0 via .get(id, 0.0)."""
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_result_with_rows([(1, 5000.0)]))
        result = await _strategy_snapshot_notional_by_id(session, [1, 2])
        assert result.get(2, 0.0) == 0.0


@pytest.mark.asyncio
class TestStrategySnapshotAlgoPnlFallbackById:
    async def test_empty_strategy_ids_skips_query(self):
        session = AsyncMock()
        result = await _strategy_snapshot_algo_pnl_fallback_by_id(
            session, [], ("OPEN",),
        )
        assert result == {}
        session.execute.assert_not_called()

    async def test_degrades_to_empty_dict_instead_of_crashing(self):
        """2026-10 audit finding: AlgoOrder has NO `pnl` column on the
        current model (verified via introspection) — the query this
        wraps raises AttributeError when SQLAlchemy tries to resolve
        AlgoOrder.pnl as a column expression. Must degrade to {} (every
        strategy's fallback treated as 0.0 by the caller) rather than
        propagating and aborting the whole snapshot cycle."""
        from backend.api.models import AlgoOrder
        assert not hasattr(AlgoOrder, "pnl"), (
            "this test's premise (AlgoOrder.pnl doesn't exist) no longer "
            "holds — if a real pnl column was added, the try/except in "
            "_strategy_snapshot_algo_pnl_fallback_by_id is now dead code "
            "and the real GROUP BY query should be tested directly instead"
        )
        session = AsyncMock()
        result = await _strategy_snapshot_algo_pnl_fallback_by_id(
            session, [1, 2], ("OPEN", "CHASING", "PENDING"),
        )
        assert result == {}
        session.execute.assert_not_called()


@pytest.mark.asyncio
class TestRunStrategySnapshotOnceConsolidation:
    async def test_grouped_queries_called_once_regardless_of_strategy_count(self):
        """Core N+1 regression lock: with 3 active strategies, the notional
        and AlgoOrder.pnl-fallback grouped helpers must each be called
        exactly ONCE (not once per strategy)."""
        strategies = [
            SimpleNamespace(id=1, slug="s1", is_active=True),
            SimpleNamespace(id=2, slug="s2", is_active=True),
            SimpleNamespace(id=3, slug="s3", is_active=True),
        ]
        strategies_result = MagicMock()
        strategies_result.scalars.return_value.all.return_value = strategies

        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.execute = AsyncMock(side_effect=[
            strategies_result,  # SELECT Strategy
        ])
        session.commit = AsyncMock()

        notional_calls = []
        fallback_calls = []

        async def _fake_notional(s, ids):
            notional_calls.append(list(ids))
            return {1: 100.0, 2: 200.0, 3: 0.0}

        async def _fake_fallback(s, ids, open_states):
            fallback_calls.append(list(ids))
            return {1: -50.0, 2: 0.0, 3: 75.0}

        async def _fake_compute_strategy_pnl(s, strategy_id):
            return {"open_lots_count": 0, "realised_pnl": 10.0}

        with patch("backend.api.database.async_session", return_value=session), \
             patch("backend.api.background._strategy_snapshot_notional_by_id",
                   side_effect=_fake_notional), \
             patch("backend.api.background._strategy_snapshot_algo_pnl_fallback_by_id",
                   side_effect=_fake_fallback), \
             patch("backend.api.algo.lot_ledger.compute_strategy_pnl",
                   side_effect=_fake_compute_strategy_pnl):

            state: dict = {}
            from datetime import datetime, time as dtime
            from zoneinfo import ZoneInfo
            fake_now = datetime(2026, 10, 2, 16, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
            with patch("backend.api.background.timestamp_indian", return_value=fake_now):
                await _run_strategy_snapshot_once(state)

        assert len(notional_calls) == 1, (
            f"expected notional grouped query called exactly once, "
            f"got {len(notional_calls)} calls"
        )
        assert len(fallback_calls) == 1, (
            f"expected AlgoOrder.pnl fallback grouped query called exactly "
            f"once, got {len(fallback_calls)} calls"
        )
        assert sorted(notional_calls[0]) == [1, 2, 3]
        assert state.get("strategy_done") == fake_now.date()
