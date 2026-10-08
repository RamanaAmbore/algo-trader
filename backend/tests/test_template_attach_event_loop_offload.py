"""
Event-loop offload regression tests for template_attach.py's LIVE apply path.

Background (P1 perf/availability fix, 2026-10): `_apply_template_to_order_impl`
called `_route_apply_path` -> `apply_plan_live` synchronously on the asyncio
event loop thread. `apply_plan_live` does 5-8 blocking broker round-trips
(translate_qty, place_gtt, get_gtts-with-retry via `_verify_gtt_accepted`,
place_order for the wing) plus up to `_GTT_VERIFY_RETRIES *
_GTT_VERIFY_BACKOFF_S` seconds of `time.sleep` — all of which stalled EVERY
route, WebSocket broadcast, and Kite postback ACK on the whole process for
that entire duration, once per templated fill.

Fix: `_route_apply_path` is now `async def` and offloads only the LIVE
branch's call into `apply_plan_live` via `asyncio.to_thread`. The sim branch
(`apply_plan_sim`, pure in-memory, no broker I/O) and the cheap in-memory
`get_broker()` lookup stay on the loop thread — only the broker-calling
`apply_plan_live` itself moves to a worker thread.

Pattern follows `backend/tests/test_event_loop_unblocked.py` (ticker-task
racing an `asyncio.to_thread` call) and `backend/tests/test_event_loop_offload.py`
(thread-identity assertions for FIX 6/7/8's `run_in_executor` offloads).
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.algo.template_attach import (
    AttachResult,
    TemplatePlan,
    _route_apply_path,
    apply_template_to_order,
)


def _make_plan(parent_account: str = "ACC1") -> TemplatePlan:
    """Minimal TemplatePlan — no GTTs/wing needed, apply_plan_live/sim are
    mocked out entirely in every test below."""
    return TemplatePlan(
        template_id=1,
        template_name="Default Bull",
        template_slug="default-bull",
        parent_account=parent_account,
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )


def _base_template() -> dict:
    return {
        "id": 1,
        "slug": "default-bull",
        "name": "Default Bull",
        "applies_to": "buy_any",
        "tp_pct": 10.0,
        "sl_pct": 5.0,
        "wing_premium_pct": None,
        "wing_strike_offset": None,
        "tp_order_type": "LIMIT",
        "tp_scales_json": None,
        "sl_trail_pct": None,
    }


class TestLiveBranchOffloadsToWorkerThread:
    """`_route_apply_path(apply_path='live')` must run `apply_plan_live` off
    the event-loop thread via `asyncio.to_thread`."""

    @pytest.mark.asyncio
    async def test_apply_plan_live_runs_on_worker_thread(self):
        plan = _make_plan()
        call_thread: threading.Thread | None = None
        loop_thread = threading.current_thread()

        def stub_apply_plan_live(plan_arg, broker, *, parent_order_id=None):
            nonlocal call_thread
            call_thread = threading.current_thread()
            return AttachResult(plan=plan_arg)

        with patch(
            "backend.api.algo.template_attach.apply_plan_live",
            side_effect=stub_apply_plan_live,
        ), patch(
            "backend.brokers.registry.get_broker",
            return_value=MagicMock(),
        ):
            result = await _route_apply_path(plan, "live", "ACC1", None)

        assert result is not None
        assert call_thread is not None, "apply_plan_live stub never ran"
        assert call_thread != loop_thread, (
            "apply_plan_live ran on the event loop thread — "
            "the asyncio.to_thread offload is missing/broken"
        )

    @pytest.mark.asyncio
    async def test_slow_apply_plan_live_does_not_block_event_loop(self):
        """Ticker-racing test (pattern: test_event_loop_unblocked.py).

        A 300ms-blocking `apply_plan_live` must not stall a concurrently
        scheduled 20ms ticker task — proving the event loop stayed
        responsive (every other route/WS/postback could still run)
        while the broker calls were "in flight" on the worker thread.
        """
        plan = _make_plan()

        def slow_apply_plan_live(plan_arg, broker, *, parent_order_id=None):
            time.sleep(0.3)
            return AttachResult(plan=plan_arg)

        tick_count = 0

        async def ticker():
            nonlocal tick_count
            while True:
                await asyncio.sleep(0.02)
                tick_count += 1

        t = asyncio.create_task(ticker())
        try:
            with patch(
                "backend.api.algo.template_attach.apply_plan_live",
                side_effect=slow_apply_plan_live,
            ), patch(
                "backend.brokers.registry.get_broker",
                return_value=MagicMock(),
            ):
                start = time.monotonic()
                result = await _route_apply_path(plan, "live", "ACC1", None)
                elapsed = time.monotonic() - start
        finally:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        assert result is not None
        assert (
            tick_count >= 8
        ), f"Event loop blocked — ticker ran {tick_count}x during {elapsed*1000:.0f}ms"
        assert elapsed >= 0.28, f"sync call too fast: {elapsed*1000:.0f}ms"

    @pytest.mark.asyncio
    async def test_exception_type_propagates_through_offload(self):
        """asyncio.to_thread must propagate the SAME exception type/message
        raised inside apply_plan_live back to the awaiting caller — callers
        further up the stack (e.g. _fire_template_attach_on_fill's broad
        `except Exception`) must still see a real exception, not a
        swallowed/mistyped one."""
        plan = _make_plan()

        def raising_apply_plan_live(plan_arg, broker, *, parent_order_id=None):
            raise ValueError("boom-from-worker-thread")

        with patch(
            "backend.api.algo.template_attach.apply_plan_live",
            side_effect=raising_apply_plan_live,
        ), patch(
            "backend.brokers.registry.get_broker",
            return_value=MagicMock(),
        ):
            with pytest.raises(ValueError, match="boom-from-worker-thread"):
                await _route_apply_path(plan, "live", "ACC1", None)


class TestSimAndPreviewStayOnLoopThread:
    """Only the LIVE branch is offloaded — sim is pure in-memory (no broker
    I/O, no sleeps) and preview never calls apply_plan_sim/apply_plan_live
    at all, so neither should pay a thread-hop cost."""

    @pytest.mark.asyncio
    async def test_apply_plan_sim_runs_on_loop_thread(self):
        plan = _make_plan()
        loop_thread = threading.current_thread()
        call_thread: threading.Thread | None = None

        def stub_apply_plan_sim(plan_arg, driver, *, parent_order_id=None):
            nonlocal call_thread
            call_thread = threading.current_thread()
            return AttachResult(plan=plan_arg)

        mock_driver = MagicMock()
        mock_driver.active = True

        with patch(
            "backend.api.algo.template_attach.apply_plan_sim",
            side_effect=stub_apply_plan_sim,
        ), patch(
            "backend.api.algo.sim.driver.SimDriver",
        ) as mock_sim_class:
            mock_sim_class.instance.return_value = mock_driver
            result = await _route_apply_path(plan, "sim", "ACC1", None)

        assert result is not None
        assert call_thread is not None, "apply_plan_sim stub never ran"
        assert call_thread == loop_thread, (
            "apply_plan_sim should stay on the event loop thread — it is "
            "pure in-memory with no broker I/O, offloading it is pure "
            "overhead (and could race SimDriver state against "
            "PaperTradeEngine.tick_loop's own executor-offloaded step())"
        )

    @pytest.mark.asyncio
    async def test_preview_short_circuits_without_touching_apply_plan_live_or_sim(self):
        plan = _make_plan()
        with patch(
            "backend.api.algo.template_attach.apply_plan_live",
        ) as mock_live, patch(
            "backend.api.algo.template_attach.apply_plan_sim",
        ) as mock_sim:
            result = await _route_apply_path(plan, "preview", "ACC1", None)

        assert result is not None
        assert result.plan is plan
        mock_live.assert_not_called()
        mock_sim.assert_not_called()


class TestEndToEndApplyTemplateToOrderOffload:
    """Full-stack proof through the public `apply_template_to_order` entry
    point (what every real caller — orders_place.py, orders.py, actions.py,
    chase.py — actually awaits)."""

    @pytest.mark.asyncio
    async def test_live_fill_path_does_not_block_event_loop(self):
        template = _base_template()

        def slow_apply_plan_live(plan_arg, broker, *, parent_order_id=None):
            time.sleep(0.3)
            result = AttachResult(plan=plan_arg)
            result.gtt_ids = ["gtt-1"]
            return result

        tick_count = 0

        async def ticker():
            nonlocal tick_count
            while True:
                await asyncio.sleep(0.02)
                tick_count += 1

        t = asyncio.create_task(ticker())
        try:
            with patch(
                "backend.api.algo.template_attach.load_template_for_slug_or_id",
                new=AsyncMock(return_value=template),
            ), patch(
                "backend.api.algo.template_attach.apply_plan_live",
                side_effect=slow_apply_plan_live,
            ), patch(
                "backend.brokers.registry.get_broker",
                return_value=MagicMock(),
            ), patch(
                "backend.api.algo.agent_engine._symbol_exchange_open",
                return_value=True,
            ), patch(
                "backend.api.algo.agent_engine._build_now_ctx",
                return_value={},
            ):
                start = time.monotonic()
                result = await apply_template_to_order(
                    template_id=1,
                    template_slug="default-bull",
                    overrides={},
                    parent_account="ACC1",
                    parent_symbol="RELIANCE",
                    parent_side="BUY",
                    parent_qty=10,
                    parent_exchange="NSE",
                    parent_fill_price=1000.0,
                    apply_path="live",
                )
                elapsed = time.monotonic() - start
        finally:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        assert result is not None
        assert tick_count >= 8, (
            f"Event loop blocked during templated live fill — ticker ran "
            f"{tick_count}x during {elapsed*1000:.0f}ms"
        )
