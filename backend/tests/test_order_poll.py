"""
Tests for the "Order poll" consolidation (2026-10-23):
`backend/api/algo/order_poll.py` — the shared `reconcile_row_and_followup`
wrapper used by the open-order watchdog, the new periodic sweep, and
postback's non-blocking follow-up verification.

Covers:
  - `reconcile_row_and_followup`: no-id / unavailable / not-yet-terminal
    all return None (nothing to report); a confirmed correction fires
    template-attach + ws-resubscribe on FILLED and an `order_correction`
    alert record (unless `fire_alert=False`); a chase-active row is a
    full no-op (checked BEFORE reading broker truth at all) — this is
    the fix for a false urgent-ntfy-on-a-routine-fill hazard found in
    advisor review (a low-tier chase can run ~15 min, well past the
    5-min age cutoff every automatic caller uses, so this function
    could otherwise race ahead of chase's own fill detection).
  - `_task_order_poll`: finds and corrects a seeded-ambiguous row, alerts
    on the correction; a clean pass (nothing to fix) does not alert;
    the candidate-row scope filter itself (mode='live', status in
    OPEN/CANCEL_FAILED, older than 5 min) against a real sqlite DB
    seeded with every adjacent row shape that must NOT match.
  - Postback's non-blocking follow-up verification: scheduling returns
    immediately (never awaits the sleep/verify itself); only a terminal
    postback schedules one at all (a non-terminal OPEN/UPDATE event has
    nothing to verify); the scheduled task eventually calls the
    canonical accessor for the same row.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio


def _row(**kw):
    kw.setdefault("id", 1)
    kw.setdefault("status", "OPEN")
    kw.setdefault("broker_order_id", "BO1")
    kw.setdefault("account", "ZG0790")
    kw.setdefault("symbol", "NIFTY24APR25000CE")
    kw.setdefault("exchange", "NFO")
    return SimpleNamespace(**kw)


def _db_session(row):
    """Shared fake session shape — supports BOTH `.scalar_one_or_none()`
    (apply_order_truth's own fetch, and _opo_postprocess's refetch) and
    `.scalars().first()`/`.scalars().all()` (not used here, but kept for
    parity with other test helpers in this file's sibling test modules)."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    result.scalars.return_value.all.return_value = [row] if row is not None else []
    result.scalars.return_value.first.return_value = row

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


# ── reconcile_row_and_followup ──────────────────────────────────────────

class TestReconcileRowAndFollowup:
    async def test_no_broker_order_id_returns_none(self):
        from backend.api.algo.order_poll import reconcile_row_and_followup
        r = _row(broker_order_id="")
        result = await reconcile_row_and_followup(r)
        assert result is None

    async def test_no_algo_order_id_returns_none(self):
        from backend.api.algo.order_poll import reconcile_row_and_followup
        r = _row(id=None)
        result = await reconcile_row_and_followup(r)
        assert result is None

    async def test_unavailable_truth_returns_none(self):
        from backend.api.algo.order_poll import reconcile_row_and_followup
        from backend.api.routes.orders_helpers import OrderTruth
        r = _row()
        with patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=OrderTruth(available=False)),
        ):
            result = await reconcile_row_and_followup(r)
        assert result is None

    async def test_not_yet_terminal_returns_none(self):
        from backend.api.algo.order_poll import reconcile_row_and_followup
        from backend.api.routes.orders_helpers import OrderTruth
        r = _row()
        truth = OrderTruth(available=True, algo_status=None, raw_broker_status="OPEN")
        with patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
        ):
            result = await reconcile_row_and_followup(r)
        assert result is None

    async def test_withheld_mid_chase_returns_none_no_alert(self):
        """apply_order_truth's own withhold_if_active=True gate (HELD /
        active-chase-retry) must suppress BOTH the write and the alert."""
        from backend.api.algo.order_poll import reconcile_row_and_followup
        from backend.api.routes.orders_helpers import OrderTruth
        r = _row()
        truth = OrderTruth(available=True, algo_status="CANCELLED", raw_broker_status="CANCELLED")
        with patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
        ), patch(
            "backend.api.routes.orders_helpers.apply_order_truth",
            new=AsyncMock(return_value=False),
        ), patch("backend.api.algo.order_poll.logger") as mock_log:
            result = await reconcile_row_and_followup(r)
        assert result is None
        mock_log.warning.assert_not_called()

    async def test_chase_active_row_is_a_full_noop_even_for_a_fill(self):
        """Advisor-flagged fix: a FILLED resolution is normally NEVER
        withheld by `apply_order_truth` (withhold_if_active only
        applies to non-fill terminal statuses) — so without this
        chase-active short-circuit, this function would apply FILLED
        and fire a false 'order_correction' urgent alert for a
        perfectly normal, self-resolving chase fill. Must not even
        call get_broker_order_truth."""
        from backend.api.algo.order_poll import reconcile_row_and_followup

        r = _row()

        with patch(
            "backend.api.algo.chase.is_chase_active", return_value=True,
        ), patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(),
        ) as mock_truth, patch(
            "backend.api.algo.order_poll.logger",
        ) as mock_log:
            result = await reconcile_row_and_followup(r)

        assert result is None
        mock_truth.assert_not_awaited()
        mock_log.warning.assert_not_called()

    async def test_chase_registry_lookup_failure_fails_open(self):
        """If the in-process registry check itself raises for any
        reason, proceed normally rather than silently stalling every
        reconcile forever."""
        from backend.api.algo.order_poll import reconcile_row_and_followup
        from backend.api.routes.orders_helpers import OrderTruth

        r = _row()
        truth = OrderTruth(available=False)

        with patch(
            "backend.api.algo.chase.is_chase_active",
            side_effect=RuntimeError("registry boom"),
        ), patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
        ) as mock_truth:
            result = await reconcile_row_and_followup(r)

        assert result is None
        mock_truth.assert_awaited_once()

    async def test_confirmed_fill_fires_attach_subscribe_and_alert(self):
        from backend.api.algo.order_poll import reconcile_row_and_followup
        from backend.api.routes.orders_helpers import OrderTruth

        r = _row(status="OPEN")
        fresh_row = _row(status="FILLED")
        fresh_row.fill_price = 101.5
        fresh_row.filled_quantity = 50

        truth = OrderTruth(available=True, algo_status="FILLED",
                           average_price=101.5, raw_broker_status="COMPLETE")

        attach_calls: list = []
        subscribe_calls: list = []

        with patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
        ), patch(
            "backend.api.routes.orders_helpers.apply_order_truth",
            new=AsyncMock(return_value=True),
        ), patch(
            "backend.api.database.async_session",
            return_value=_db_session(fresh_row),
        ), patch(
            "backend.api.routes.orders_place._maybe_fire_template_attach_for_reconcile",
            new=MagicMock(side_effect=lambda row: attach_calls.append(row)),
        ), patch(
            "backend.api.routes.orders._subscribe_filled_pairs",
            new=AsyncMock(side_effect=lambda pairs: subscribe_calls.append(pairs)),
        ), patch("backend.api.algo.order_poll.logger") as mock_log:
            result = await reconcile_row_and_followup(r, source="order-poll")

        assert result is truth
        assert attach_calls == [fresh_row]
        assert subscribe_calls == [[("NIFTY24APR25000CE", "NFO")]]
        mock_log.warning.assert_called_once()
        extra = mock_log.warning.call_args.kwargs["extra"]
        assert extra["tags"] == ["orders"]
        assert extra["alert_event"] == "order_correction"
        assert extra["action_required"] is True
        assert extra["algo_order_id"] == r.id
        assert extra["prior_status"] == "OPEN"
        assert extra["new_status"] == "FILLED"
        assert extra["source"] == "order-poll"
        # Row carries no agent_id by default (_row()'s SimpleNamespace
        # default) -> threaded through as None, resolving to "manual"
        # downstream in the renderer, never silently omitted.
        assert extra["agent_id"] is None

    async def test_order_correction_alert_threads_row_agent_id(self):
        """2026-10-23 fix — a corrected row that DOES have a real
        agent_id must carry it into the alert extra, so the renderer
        can resolve a real origin instead of "manual"."""
        from backend.api.algo.order_poll import reconcile_row_and_followup
        from backend.api.routes.orders_helpers import OrderTruth

        r = _row(status="OPEN", agent_id=42)
        fresh_row = _row(status="FILLED")

        truth = OrderTruth(available=True, algo_status="FILLED",
                           average_price=101.5, raw_broker_status="COMPLETE")

        with patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
        ), patch(
            "backend.api.routes.orders_helpers.apply_order_truth",
            new=AsyncMock(return_value=True),
        ), patch(
            "backend.api.database.async_session",
            return_value=_db_session(fresh_row),
        ), patch(
            "backend.api.routes.orders_place._maybe_fire_template_attach_for_reconcile",
        ), patch(
            "backend.api.routes.orders._subscribe_filled_pairs",
            new=AsyncMock(),
        ), patch("backend.api.algo.order_poll.logger") as mock_log:
            await reconcile_row_and_followup(r, source="order-poll")

        extra = mock_log.warning.call_args.kwargs["extra"]
        assert extra["agent_id"] == 42

    async def test_render_order_correction_leads_with_origin_header(self):
        """2026-10-23 "every alert leads with its origin" invariant
        (commit 70802eb5) extended to this new renderer — it was added
        the same day but originally skipped `format_notification_header`,
        the one thing every other order-scoped renderer in this file
        routes through. A real agent_id resolves to "Agent #{id}"; a
        missing one resolves to "manual" (`resolve_origin`'s own rule),
        never a silently-omitted origin line."""
        from backend.api.algo.event_agents import _render_order_correction

        rec_with_agent = {"extra": {
            "algo_order_id": 7, "masked": "ZG****90", "symbol": "NIFTY",
            "exchange": "NFO", "prior_status": "OPEN", "new_status": "FILLED",
            "source": "order-poll", "agent_id": 42,
        }}
        title, body = _render_order_correction(rec_with_agent)
        assert body.startswith("Agent #42")
        assert "Order corrected" in title

        rec_manual = dict(rec_with_agent)
        rec_manual["extra"] = {**rec_with_agent["extra"], "agent_id": None}
        _, manual_body = _render_order_correction(rec_manual)
        assert manual_body.startswith("Agent: manual")

    async def test_confirmed_cancel_fires_alert_without_attach_or_subscribe(self):
        from backend.api.algo.order_poll import reconcile_row_and_followup
        from backend.api.routes.orders_helpers import OrderTruth

        r = _row(status="OPEN")
        truth = OrderTruth(available=True, algo_status="CANCELLED", raw_broker_status="CANCELLED")

        attach_mock = MagicMock()
        subscribe_mock = AsyncMock()

        with patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
        ), patch(
            "backend.api.routes.orders_helpers.apply_order_truth",
            new=AsyncMock(return_value=True),
        ), patch(
            "backend.api.routes.orders_place._maybe_fire_template_attach_for_reconcile",
            new=attach_mock,
        ), patch(
            "backend.api.routes.orders._subscribe_filled_pairs",
            new=subscribe_mock,
        ), patch("backend.api.algo.order_poll.logger") as mock_log:
            result = await reconcile_row_and_followup(r)

        assert result is truth
        attach_mock.assert_not_called()
        subscribe_mock.assert_not_awaited()
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args.kwargs["extra"]["new_status"] == "CANCELLED"

    async def test_fire_alert_false_suppresses_the_alert_but_not_side_effects(self):
        from backend.api.algo.order_poll import reconcile_row_and_followup
        from backend.api.routes.orders_helpers import OrderTruth

        r = _row(status="OPEN")
        fresh_row = _row(status="FILLED")

        truth = OrderTruth(available=True, algo_status="FILLED",
                           average_price=10.0, raw_broker_status="COMPLETE")

        with patch(
            "backend.api.routes.orders_helpers.get_broker_order_truth",
            new=AsyncMock(return_value=truth),
        ), patch(
            "backend.api.routes.orders_helpers.apply_order_truth",
            new=AsyncMock(return_value=True),
        ), patch(
            "backend.api.database.async_session",
            return_value=_db_session(fresh_row),
        ), patch(
            "backend.api.routes.orders_place._maybe_fire_template_attach_for_reconcile",
        ) as attach_mock, patch(
            "backend.api.routes.orders._subscribe_filled_pairs",
            new=AsyncMock(),
        ) as subscribe_mock, patch(
            "backend.api.algo.order_poll.logger",
        ) as mock_log:
            result = await reconcile_row_and_followup(r, fire_alert=False)

        assert result is truth
        attach_mock.assert_called_once_with(fresh_row)
        subscribe_mock.assert_awaited_once()
        mock_log.warning.assert_not_called()


# ── _task_order_poll — the periodic sweep ───────────────────────────────

class TestTaskOrderPoll:
    async def test_corrects_a_seeded_ambiguous_row_and_alerts(self):
        """A row the sweep finds genuinely resolved (broker says
        COMPLETE, DB still says OPEN) must be corrected AND alert."""
        from backend.api.algo import order_poll as op_mod
        from backend.api.routes.orders_helpers import OrderTruth

        stuck_row = _row(id=5, status="OPEN", broker_order_id="BO5")
        fresh_row = _row(id=5, status="FILLED", broker_order_id="BO5")

        truth = OrderTruth(available=True, algo_status="FILLED",
                           average_price=99.0, raw_broker_status="COMPLETE")

        sleep_calls = {"n": 0}

        async def _fake_sleep(_secs):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 1:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", new=_fake_sleep), \
             patch(
                 "backend.shared.helpers.utils.is_engine_idle",
                 return_value=False,
             ), \
             patch.object(
                 op_mod, "_order_poll_candidate_rows",
                 new=AsyncMock(return_value=[stuck_row]),
             ), \
             patch(
                 "backend.api.routes.orders_helpers._broker_order_snapshot",
                 new=AsyncMock(return_value={}),
             ), \
             patch(
                 "backend.api.routes.orders_helpers.get_broker_order_truth",
                 new=AsyncMock(return_value=truth),
             ), \
             patch(
                 "backend.api.routes.orders_helpers.apply_order_truth",
                 new=AsyncMock(return_value=True),
             ), \
             patch(
                 "backend.api.database.async_session",
                 return_value=_db_session(fresh_row),
             ), \
             patch("backend.api.routes.orders_place._maybe_fire_template_attach_for_reconcile"), \
             patch("backend.api.routes.orders._subscribe_filled_pairs", new=AsyncMock()), \
             patch.object(op_mod, "logger") as mock_log:
            with pytest.raises(asyncio.CancelledError):
                await op_mod._task_order_poll()

        # One `.warning` call for the order_correction alert (the
        # "scanned=.. corrected=.." summary is `.info`, not `.warning`).
        assert mock_log.warning.call_count == 1
        extra = mock_log.warning.call_args.kwargs["extra"]
        assert extra["alert_event"] == "order_correction"
        assert extra["new_status"] == "FILLED"
        assert extra["source"] == "order-poll"

    async def test_clean_pass_with_no_candidates_does_not_alert(self):
        from backend.api.algo import order_poll as op_mod

        sleep_calls = {"n": 0}

        async def _fake_sleep(_secs):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 1:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", new=_fake_sleep), \
             patch(
                 "backend.shared.helpers.utils.is_engine_idle",
                 return_value=False,
             ), \
             patch.object(
                 op_mod, "_order_poll_candidate_rows",
                 new=AsyncMock(return_value=[]),
             ), \
             patch.object(op_mod, "logger") as mock_log:
            with pytest.raises(asyncio.CancelledError):
                await op_mod._task_order_poll()

        mock_log.warning.assert_not_called()

    async def test_clean_pass_with_candidates_but_nothing_to_fix_does_not_alert(self):
        """Every candidate row is still genuinely OPEN at the broker —
        a real poll cycle finds nothing wrong, so it must not alert."""
        from backend.api.algo import order_poll as op_mod
        from backend.api.routes.orders_helpers import OrderTruth

        still_open_row = _row(id=9, status="OPEN", broker_order_id="BO9")
        truth = OrderTruth(available=True, algo_status=None, raw_broker_status="OPEN")

        sleep_calls = {"n": 0}

        async def _fake_sleep(_secs):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 1:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", new=_fake_sleep), \
             patch(
                 "backend.shared.helpers.utils.is_engine_idle",
                 return_value=False,
             ), \
             patch.object(
                 op_mod, "_order_poll_candidate_rows",
                 new=AsyncMock(return_value=[still_open_row]),
             ), \
             patch(
                 "backend.api.routes.orders_helpers._broker_order_snapshot",
                 new=AsyncMock(return_value={}),
             ), \
             patch(
                 "backend.api.routes.orders_helpers.get_broker_order_truth",
                 new=AsyncMock(return_value=truth),
             ), \
             patch.object(op_mod, "logger") as mock_log:
            with pytest.raises(asyncio.CancelledError):
                await op_mod._task_order_poll()

        mock_log.warning.assert_not_called()

    async def test_dev_idle_gate_skips_the_broker_round_trip(self):
        from backend.api.algo import order_poll as op_mod

        sleep_calls = {"n": 0}

        async def _fake_sleep(_secs):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 1:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", new=_fake_sleep), \
             patch(
                 "backend.shared.helpers.utils.is_engine_idle",
                 return_value=True,
             ), \
             patch.object(
                 op_mod, "_order_poll_candidate_rows",
                 new=AsyncMock(),
             ) as mock_candidates:
            with pytest.raises(asyncio.CancelledError):
                await op_mod._task_order_poll()

        mock_candidates.assert_not_called()


# ── _order_poll_candidate_rows — scope filter against a real DB ────────
#
# Every test above patches `_order_poll_candidate_rows` itself, so the
# scope filter (mode='live', status in OPEN/CANCEL_FAILED, created_at
# older than 5 min) had zero coverage — a dropped `mode == "live"`
# filter, for example, would still pass every other test in this file.
# This drives the real function against an in-process sqlite DB seeded
# with every adjacent row shape that must NOT be selected (advisor
# review finding).

@pytest_asyncio.fixture
async def _op_sqlite_session_factory():
    from sqlalchemy import Column, Integer, String, DateTime
    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    class _Base(DeclarativeBase):
        pass

    class _AlgoOrder(_Base):
        __tablename__ = "algo_orders"
        id               = Column(Integer, primary_key=True, autoincrement=True)
        account          = Column(String(32), nullable=False, default="ZG0790")
        symbol           = Column(String(64), nullable=False, default="TCS")
        exchange         = Column(String(8),  nullable=False, default="NSE")
        transaction_type = Column(String(4),  nullable=False, default="BUY")
        quantity         = Column(Integer,    nullable=False, default=10)
        status           = Column(String(16), nullable=False, default="OPEN")
        mode             = Column(String(8),  nullable=False, default="live")
        broker_order_id  = Column(String(32), nullable=True)
        created_at       = Column(DateTime,   nullable=False)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    import backend.api.models as _models
    orig_order = _models.AlgoOrder
    _models.AlgoOrder = _AlgoOrder  # type: ignore[assignment]

    yield factory, _AlgoOrder

    _models.AlgoOrder = orig_order  # type: ignore[assignment]
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await engine.dispose()


class TestOrderPollCandidateRowsScope:
    async def test_scope_filter_against_real_db(self, _op_sqlite_session_factory):
        from backend.api.algo.order_poll import _order_poll_candidate_rows

        factory, AlgoOrder = _op_sqlite_session_factory
        now = datetime.now(timezone.utc)
        old = now - timedelta(minutes=10)
        fresh = now - timedelta(minutes=1)

        async with factory() as s:
            live_open_old = AlgoOrder(
                mode="live", status="OPEN", created_at=old, broker_order_id="BO1",
            )
            live_open_fresh = AlgoOrder(
                mode="live", status="OPEN", created_at=fresh, broker_order_id="BO2",
            )
            draft_open_old = AlgoOrder(
                mode="draft", status="OPEN", created_at=old, broker_order_id="BO3",
            )
            paper_open_old = AlgoOrder(
                mode="paper", status="OPEN", created_at=old, broker_order_id="BO4",
            )
            live_held_old = AlgoOrder(
                mode="live", status="HELD", created_at=old, broker_order_id="BO5",
            )
            live_cancelled_old = AlgoOrder(
                mode="live", status="CANCELLED", created_at=old, broker_order_id="BO6",
            )
            live_cancel_failed_old = AlgoOrder(
                mode="live", status="CANCEL_FAILED", created_at=old, broker_order_id="BO7",
            )
            for row in (
                live_open_old, live_open_fresh, draft_open_old, paper_open_old,
                live_held_old, live_cancelled_old, live_cancel_failed_old,
            ):
                s.add(row)
            await s.commit()
            expected_ids = {live_open_old.id, live_cancel_failed_old.id}

        with patch("backend.api.database.async_session", factory):
            cutoff = now - timedelta(minutes=5)
            candidates = await _order_poll_candidate_rows(cutoff)

        assert {r.id for r in candidates} == expected_ids, (
            "the sweep must select exactly the OLD live OPEN/CANCEL_FAILED "
            "rows — never draft, paper, HELD, CANCELLED, or a too-fresh "
            "live OPEN row"
        )


# ── Postback's non-blocking follow-up verification ──────────────────────

class TestPostbackFollowupVerify:
    async def test_schedule_returns_immediately_without_awaiting(self):
        """`_pb_schedule_followup_verify` must return before the
        scheduled task's own sleep/verify has had any chance to run —
        proving the webhook response path is never blocked by it."""
        from backend.api.routes import orders_postback as pb_mod

        ran: list = []

        async def _fake_followup(bid):
            ran.append(bid)

        with patch.object(pb_mod, "_pb_followup_verify", side_effect=_fake_followup):
            pb_mod._pb_schedule_followup_verify("BO-FOLLOWUP-1")
            # The scheduled task has not had a chance to run yet — a
            # bare create_task() does not execute synchronously.
            assert ran == []
            await asyncio.sleep(0)  # let the task actually start

        await asyncio.sleep(0)

    async def test_falsy_broker_order_id_is_a_noop(self):
        from backend.api.routes import orders_postback as pb_mod
        before = len(pb_mod._PB_FOLLOWUP_TASKS)
        pb_mod._pb_schedule_followup_verify("")
        assert len(pb_mod._PB_FOLLOWUP_TASKS) == before

    async def test_followup_verify_calls_the_canonical_accessor_for_the_same_row(self):
        from backend.api.routes import orders_postback as pb_mod

        row = _row(id=77, broker_order_id="BO-SAME-ROW")

        with patch.object(pb_mod, "_PB_FOLLOWUP_VERIFY_DELAY_S", 0), \
             patch("backend.api.database.async_session", return_value=_db_session(row)), \
             patch("backend.api.cache.invalidate") as mock_invalidate, \
             patch(
                 "backend.api.algo.order_poll.reconcile_row_and_followup",
                 new=AsyncMock(return_value=None),
             ) as mock_reconcile:
            await pb_mod._pb_followup_verify("BO-SAME-ROW")

        mock_invalidate.assert_called_once_with("orders")
        mock_reconcile.assert_awaited_once()
        called_row = mock_reconcile.await_args.args[0]
        assert called_row is row
        assert mock_reconcile.await_args.kwargs.get("source") == "postback-followup"

    async def test_followup_verify_noop_when_row_no_longer_exists(self):
        from backend.api.routes import orders_postback as pb_mod

        with patch.object(pb_mod, "_PB_FOLLOWUP_VERIFY_DELAY_S", 0), \
             patch("backend.api.database.async_session", return_value=_db_session(None)), \
             patch(
                 "backend.api.algo.order_poll.reconcile_row_and_followup",
                 new=AsyncMock(),
             ) as mock_reconcile:
            await pb_mod._pb_followup_verify("BO-GONE")

        mock_reconcile.assert_not_awaited()

    async def test_followup_verify_never_raises_on_internal_failure(self):
        from backend.api.routes import orders_postback as pb_mod

        with patch.object(pb_mod, "_PB_FOLLOWUP_VERIFY_DELAY_S", 0), \
             patch("backend.api.database.async_session", side_effect=RuntimeError("db down")):
            await pb_mod._pb_followup_verify("BO-ERR")  # must not raise

    async def test_process_broker_postback_schedules_followup_without_blocking(self):
        """`_process_broker_postback` (Dhan/Groww path) is awaited
        INLINE by its webhook handlers — the followup must be
        scheduled via create_task, not awaited, so the function itself
        returns before the followup's sleep ever runs."""
        from backend.api.routes import orders_postback as pb_mod

        scheduled: list = []

        with patch.object(
            pb_mod, "_pb_schedule_followup_verify",
            side_effect=lambda bid: scheduled.append(bid),
        ), \
             patch.object(pb_mod, "_sync_algo_order_rows", new=AsyncMock(return_value=[])), \
             patch("backend.api.audit.write_audit_event"), \
             patch("backend.api.routes.orders._postback_broadcast_fanout"):
            await pb_mod._process_broker_postback(
                broker_id="dhan", order_id="BO-DHAN-1", status="COMPLETE",
                account="ZG0790", symbol="NIFTY24APR25000CE", txn="BUY",
                qty=50, price=101.5,
            )

        assert scheduled == ["BO-DHAN-1"]

    async def test_non_terminal_shared_postback_does_not_schedule_followup(self):
        """Advisor review finding: scheduling a follow-up for a
        non-terminal event (OPEN/UPDATE) has nothing to verify and
        would needlessly bust the shared 'orders' cache on every
        intermediate postback, not just a genuinely terminal one."""
        from backend.api.routes import orders_postback as pb_mod

        scheduled: list = []

        with patch.object(
            pb_mod, "_pb_schedule_followup_verify",
            side_effect=lambda bid: scheduled.append(bid),
        ), \
             patch.object(pb_mod, "_sync_algo_order_rows", new=AsyncMock(return_value=[])), \
             patch("backend.api.audit.write_audit_event"), \
             patch("backend.api.routes.orders._postback_broadcast_fanout"):
            await pb_mod._process_broker_postback(
                broker_id="dhan", order_id="BO-DHAN-2", status="OPEN",
                account="ZG0790", symbol="NIFTY24APR25000CE", txn="BUY",
                qty=50, price=101.5,
            )

        assert scheduled == []

    async def test_kite_terminal_event_schedules_followup(self):
        from backend.api.routes import orders_postback as pb_mod

        scheduled: list = []
        row = _row(id=1, broker_order_id="BO-KITE-1", status="OPEN",
                  quantity=10, filled_quantity=0, fill_price=None,
                  filled_at=None, created_at=None, detail="")
        mock_session = _db_session(row)

        with patch.object(
            pb_mod, "_pb_schedule_followup_verify",
            side_effect=lambda bid: scheduled.append(bid),
        ), \
             patch("backend.api.database.async_session", return_value=mock_session), \
             patch.object(pb_mod, "_pb_write_ledger_fills", new=AsyncMock()), \
             patch.object(pb_mod, "_pb_write_timeline_events", new=AsyncMock()), \
             patch.object(pb_mod, "_pb_dispatch_take_profit_and_template"):
            await pb_mod._pb_event_kite(
                order_id="BO-KITE-1", tradingsymbol="TCS", txn="BUY",
                qty=10, price=100.0, status="COMPLETE", status_msg="",
                account="ZG0790",
            )

        assert scheduled == ["BO-KITE-1"]

    async def test_kite_non_terminal_event_does_not_schedule_followup(self):
        from backend.api.routes import orders_postback as pb_mod

        scheduled: list = []
        row = _row(id=1, broker_order_id="BO-KITE-2", status="OPEN",
                  quantity=10, filled_quantity=0, fill_price=None,
                  filled_at=None, created_at=None, detail="")
        mock_session = _db_session(row)

        with patch.object(
            pb_mod, "_pb_schedule_followup_verify",
            side_effect=lambda bid: scheduled.append(bid),
        ), \
             patch("backend.api.database.async_session", return_value=mock_session), \
             patch.object(pb_mod, "_pb_write_ledger_fills", new=AsyncMock()), \
             patch.object(pb_mod, "_pb_write_timeline_events", new=AsyncMock()), \
             patch.object(pb_mod, "_pb_dispatch_take_profit_and_template"):
            await pb_mod._pb_event_kite(
                order_id="BO-KITE-2", tradingsymbol="TCS", txn="BUY",
                qty=10, price=100.0, status="OPEN", status_msg="",
                account="ZG0790",
            )

        assert scheduled == []
