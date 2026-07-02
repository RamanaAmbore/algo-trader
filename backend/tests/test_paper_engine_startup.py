"""
test_paper_engine_startup.py — paper engine tick_loop scheduled on ANY branch

Regression guard for the defect where the paper engine's `tick_loop` was
gated behind `is_prod_branch()`, causing every paper order placed on
dev.ramboq.com to hang OPEN indefinitely (no tick → no fill/UNFILLED).

Three test dimensions:
  1. SSOT: `on_startup` schedules "bg-paper-chase" on BOTH main (prod) and
     non-main (dev) branches — is_prod_branch() value must not affect the gate.
  2. Recovery: `recover_from_db()` is called on startup on both branches.
  3. Integration-lite: a PaperTradeEngine with a stub QuoteSource registers
     a fake order, calls step() once, and the order transitions OPEN → FILLED
     when bid/ask cross the limit.
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_app_mock() -> MagicMock:
    """Minimal app mock that satisfies on_startup's state attribute writes."""
    app = MagicMock()
    app.state = MagicMock()
    app.state.bg_tasks = []
    return app


class _StubQuoteSource:
    """
    Minimal QuoteSource stub that always returns a fixed (bid, ask) pair.
    Both sides are set so BUY orders (ask <= limit) and SELL (bid >= limit)
    can be exercised without a real broker.
    """

    def __init__(self, bid: float, ask: float) -> None:
        self._bid = bid
        self._ask = ask

    def bid_ask_for_order(self, order: dict) -> tuple[float | None, float | None]:
        return self._bid, self._ask

    def prefetch_for(self, orders: list[dict]) -> None:
        return None

    def on_fill(self, order: dict) -> None:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. SSOT: tick_loop task registered regardless of branch
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_paper_engine_scheduled_on_prod_branch():
    """
    on_startup registers 'bg-paper-chase' when is_prod_branch() is True
    (main / prod).
    """
    from backend.api.background import on_startup

    mock_engine = MagicMock()
    mock_engine.recover_from_db = AsyncMock(return_value=0)
    mock_engine.tick_loop = MagicMock(return_value=_noop_coro())

    app = _make_app_mock()

    with patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
         patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_engine), \
         patch("backend.api.routes.algo.start_persist_flush"), \
         _patch_all_bg_tasks():
        await on_startup(app)

    task_names = [t.get_name() for t in app.state.bg_tasks]
    assert "bg-paper-chase" in task_names, (
        f"bg-paper-chase missing from bg_tasks on prod branch. Tasks: {task_names}"
    )


@pytest.mark.asyncio
async def test_paper_engine_scheduled_on_dev_branch():
    """
    on_startup registers 'bg-paper-chase' when is_prod_branch() is False
    (dev / non-main).  This is the regression case that was broken.
    """
    from backend.api.background import on_startup

    mock_engine = MagicMock()
    mock_engine.recover_from_db = AsyncMock(return_value=0)
    mock_engine.tick_loop = MagicMock(return_value=_noop_coro())

    app = _make_app_mock()

    with patch("backend.shared.helpers.utils.is_prod_branch", return_value=False), \
         patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_engine), \
         patch("backend.api.routes.algo.start_persist_flush"), \
         _patch_all_bg_tasks():
        await on_startup(app)

    task_names = [t.get_name() for t in app.state.bg_tasks]
    assert "bg-paper-chase" in task_names, (
        f"bg-paper-chase missing from bg_tasks on dev branch. Tasks: {task_names}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Recovery: recover_from_db() called on both branches
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recover_from_db_called_on_prod():
    """recover_from_db() is awaited during on_startup on prod branch."""
    from backend.api.background import on_startup

    mock_engine = MagicMock()
    mock_engine.recover_from_db = AsyncMock(return_value=2)
    mock_engine.tick_loop = MagicMock(return_value=_noop_coro())

    app = _make_app_mock()

    with patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
         patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_engine), \
         patch("backend.api.routes.algo.start_persist_flush"), \
         _patch_all_bg_tasks():
        await on_startup(app)

    mock_engine.recover_from_db.assert_awaited_once()


@pytest.mark.asyncio
async def test_recover_from_db_called_on_dev():
    """recover_from_db() is awaited during on_startup on dev branch."""
    from backend.api.background import on_startup

    mock_engine = MagicMock()
    mock_engine.recover_from_db = AsyncMock(return_value=0)
    mock_engine.tick_loop = MagicMock(return_value=_noop_coro())

    app = _make_app_mock()

    with patch("backend.shared.helpers.utils.is_prod_branch", return_value=False), \
         patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_engine), \
         patch("backend.api.routes.algo.start_persist_flush"), \
         _patch_all_bg_tasks():
        await on_startup(app)

    mock_engine.recover_from_db.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# 3. Integration-lite: order transitions OPEN → FILLED on a single step()
# ─────────────────────────────────────────────────────────────────────────────

def test_step_fills_buy_order_when_ask_crosses_limit():
    """
    Register a BUY order at limit ₹100.  stub quote returns ask=99 (≤ limit).
    After one step() the order status must be FILLED and fill_price set.
    """
    from backend.api.algo.paper import PaperTradeEngine

    quote = _StubQuoteSource(bid=99.0, ask=99.0)
    engine = PaperTradeEngine(quote_source=quote, label="test")

    order: dict = {
        "algo_order_id":  None,   # no DB writes without an id
        "account":        "ZG0790",
        "symbol":         "NIFTY25JULFUT",
        "side":           "BUY",
        "qty":            50,
        "limit_price":    100.0,
        "initial_price":  100.0,
        "exchange":       "NFO",
        "agent_slug":     "test-agent",
        "action_type":    "place_order",
    }
    engine.register_open_order(order)

    # Confirm the engine holds it as OPEN.
    with engine._lock:
        assert len(engine._open_orders) == 1
        assert engine._open_orders[0]["status"] == "OPEN"

    # No real event loop so no DB writes; step() is sync.
    engine.step()

    with engine._lock:
        filled = engine._open_orders[0]
    assert filled["status"] == "FILLED", f"Expected FILLED, got {filled['status']}"
    assert filled.get("fill_price") is not None, "fill_price must be set after fill"


def test_step_marks_unfilled_when_attempts_exhausted():
    """
    Register a SELL order at limit ₹200 but stub quote returns bid=100 (never
    crosses) and max_attempts=1.

    The chase loop increments `attempts` on the first step() (modify path, 0 < 1).
    On the second step() `attempts == max_attempts` so the order is marked UNFILLED.
    Two steps are required; one step only re-quotes.
    """
    from backend.api.algo.paper import PaperTradeEngine

    quote = _StubQuoteSource(bid=100.0, ask=105.0)
    engine = PaperTradeEngine(
        quote_source=quote,
        label="test",
        get_max_attempts=lambda: 1,
    )

    order: dict = {
        "algo_order_id":  None,
        "account":        "ZG0790",
        "symbol":         "NIFTY25JULCE",
        "side":           "SELL",
        "qty":            50,
        "limit_price":    200.0,
        "initial_price":  200.0,
        "exchange":       "NFO",
        "agent_slug":     "test-agent",
        "action_type":    "place_order",
    }
    engine.register_open_order(order)

    # Step 1: miss → modify (attempts 0 → 1, still OPEN).
    engine.step()
    with engine._lock:
        assert engine._open_orders[0]["status"] == "OPEN", \
            "After step 1 (modify), order should still be OPEN"

    # Step 2: attempts(1) >= max_attempts(1) → UNFILLED.
    engine.step()

    with engine._lock:
        result = engine._open_orders[0]
    assert result["status"] == "UNFILLED", f"Expected UNFILLED, got {result['status']}"


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _noop_coro(*args, **kwargs):
    """Coroutine that does nothing — used as tick_loop stand-in and bg_task stub."""
    return


def _patch_all_bg_tasks():
    """
    Patch every individual background task coroutine launched by on_startup
    so they return immediately without hitting DB, broker, or file-system.
    We only care that the paper engine task is scheduled.
    """
    from contextlib import ExitStack
    import contextlib

    _TASK_NAMES = [
        "_task_market",
        "_task_performance",
        "_task_close",
        "_task_expiry_check",
        "_task_instruments",
        "_task_daily_snapshot",
        "_task_sim_cleanup",
        "_task_mcp_audit_cleanup",
        "_task_visitor_log_daily",
        "_task_sparkline_warm",
        "_task_ticker_watchdog",
        "_task_hedge_proxy_regression",
        "_task_trail_stop",
        "_task_oco_pair_watcher",
        "_task_strategy_snapshot",
        "_task_monthly_statement",
        "_task_nav_compute",
        "_task_purge_persistence_caches",
        "_task_purge_audit_log",
        "_task_market_lifecycle",
        "_task_funds_offhours",
        "_task_warm_backfill",
    ]

    @contextlib.contextmanager
    def _multi_patch():
        with ExitStack() as stack:
            for name in _TASK_NAMES:
                stack.enter_context(
                    patch(
                        f"backend.api.background.{name}",
                        side_effect=_noop_coro,
                    )
                )
            yield

    return _multi_patch()
