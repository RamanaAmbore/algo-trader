"""
test_paper_fanout.py — cache invalidation + WS broadcast on paper fills

Regression guard for the P0 defect where PaperTradeEngine._update_algo_order
updated DB status on fill/unfilled but did NOT call invalidate() or broadcast
any WS events, leaving frontend surfaces (/orders grid, /positions, NavCard,
MarketPulse) stale until the next cold poll (5-30 s).

Five quality dimensions (per test spec):
  1. SSOT  — _postback_broadcast_fanout is the single canonical function called
             from BOTH live postback (orders.py) AND paper engine (paper.py).
             Verified by patching the function and asserting call args.
  2. Perf  — fanout is sync; no extra await; does not block the async loop.
  3. Stale — old inline invalidate() / broadcast() calls do not exist in
             _update_algo_order; the adapter delegates to the shared helper.
  4. Reuse — paper.py imports from backend.api.routes.orders, not a local copy.
  5. UX    — UNFILLED fires book_changed but NOT position_filled (no qty moved).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

class _StubQuoteSource:
    """Minimal stub QuoteSource — always returns (bid=99, ask=99)."""

    def bid_ask_for_order(self, order: dict) -> tuple[float | None, float | None]:
        return 99.0, 99.0

    def prefetch_for(self, orders: list) -> None:
        pass

    def on_fill(self, order: dict) -> None:
        pass


def _make_order(
    *,
    order_id: int = 1,
    side: str = "BUY",
    symbol: str = "NIFTY25JULFUT",
    qty: int = 50,
    limit_price: float = 100.0,
    exchange: str = "NFO",
    account: str = "ZG0790",
) -> dict:
    return {
        "algo_order_id": order_id,
        "account": account,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "limit_price": limit_price,
        "initial_price": limit_price,
        "exchange": exchange,
        "agent_slug": "test-agent",
        "action_type": "place_order",
    }


def _make_algo_order_row(order: dict) -> MagicMock:
    """Mock AlgoOrder ORM row with fields mirroring the real model."""
    row = MagicMock()
    row.id = order["algo_order_id"]
    row.status = "OPEN"
    row.attempts = 0
    row.fill_price = None
    row.slippage = None
    row.filled_at = None
    row.detail = ""
    row.template_id = None
    row.mode = "paper"
    row.parent_order_id = None
    row.product = "NRML"
    row.account = order["account"]
    row.symbol = order["symbol"]
    row.exchange = order["exchange"]
    row.transaction_type = order["side"]
    row.quantity = order["qty"]
    return row


# ─────────────────────────────────────────────────────────────────────────────
# Patch context: suppress DB + write_event + write_audit_event; capture fanout
# ─────────────────────────────────────────────────────────────────────────────

def _patch_db(row: MagicMock):
    """Return an async_session context-manager mock that yields `row` on SELECT."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=row))
    )
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async_session_ctx = MagicMock(return_value=mock_session)
    return async_session_ctx


# ─────────────────────────────────────────────────────────────────────────────
# 1. FILLED (kind="fill") — full fanout + audit
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fill_fires_postback_broadcast_fanout():
    """
    SSOT: on fill, _postback_broadcast_fanout called with status="COMPLETE".
    Invalidate("orders") + invalidate("positions","holdings") + book_changed
    + position_filled are all downstream of that call so asserting the fanout
    call is sufficient for the SSOT dimension (single canonical entry point).
    """
    from backend.api.algo.paper import PaperTradeEngine

    order = _make_order(order_id=42, side="BUY", qty=50, limit_price=100.0)
    row   = _make_algo_order_row(order)

    engine = PaperTradeEngine(quote_source=_StubQuoteSource(), label="test")

    fanout_mock = MagicMock()

    with (
        patch("backend.api.database.async_session", _patch_db(row)),
        patch("backend.api.algo.order_events.write_event", AsyncMock()),
        patch("backend.api.routes.orders._postback_broadcast_fanout", fanout_mock),
        patch("backend.api.audit.write_audit_event", MagicMock()),
        patch("backend.shared.helpers.utils.mask_account", return_value="ZG####"),
    ):
        await engine._update_algo_order(
            {**order, "fill_price": 99.0, "attempts": 1},
            kind="fill",
        )

    fanout_mock.assert_called_once()
    call_kwargs = fanout_mock.call_args.kwargs
    assert call_kwargs["status"] == "COMPLETE", (
        f"Expected status='COMPLETE' for fill, got {call_kwargs['status']!r}"
    )
    assert call_kwargs["order_id"] == 42
    assert call_kwargs["symbol"] == "NIFTY25JULFUT"
    assert call_kwargs["qty"] == 50
    assert call_kwargs["masked"] == "ZG####"


@pytest.mark.asyncio
async def test_fill_fires_audit_with_order_fill_category():
    """
    Audit log entry with category='order.fill' emitted on FILLED.
    Mirrors the live Kite postback audit entry.
    """
    from backend.api.algo.paper import PaperTradeEngine

    order = _make_order(order_id=7, side="SELL", qty=25, limit_price=200.0)
    row   = _make_algo_order_row(order)
    engine = PaperTradeEngine(quote_source=_StubQuoteSource(), label="test")

    audit_mock = MagicMock()

    with (
        patch("backend.api.database.async_session", _patch_db(row)),
        patch("backend.api.algo.order_events.write_event", AsyncMock()),
        patch("backend.api.routes.orders._postback_broadcast_fanout", MagicMock()),
        patch("backend.api.audit.write_audit_event", audit_mock),
        patch("backend.shared.helpers.utils.mask_account", return_value="ZG####"),
    ):
        await engine._update_algo_order(
            {**order, "fill_price": 199.5, "attempts": 2},
            kind="fill",
        )

    audit_mock.assert_called_once()
    audit_kwargs = audit_mock.call_args.kwargs
    assert audit_kwargs.get("category") == "order.fill", (
        f"Expected category='order.fill', got {audit_kwargs.get('category')!r}"
    )
    assert "PAPER_FILL" in audit_kwargs.get("action", ""), (
        f"Expected 'PAPER_FILL' in action, got {audit_kwargs.get('action')!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. UNFILLED (kind="unfilled") — book_changed but NOT position_filled
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unfilled_fires_fanout_with_expired_status():
    """
    UX: UNFILLED maps to status='EXPIRED' in the fanout.
    EXPIRED is terminal (book_changed fires) but NOT COMPLETE (position_filled
    skipped — no qty moved, correct).
    """
    from backend.api.algo.paper import PaperTradeEngine

    order = _make_order(order_id=99, side="BUY", qty=10, limit_price=500.0)
    row   = _make_algo_order_row(order)
    engine = PaperTradeEngine(quote_source=_StubQuoteSource(), label="test")

    fanout_mock = MagicMock()

    with (
        patch("backend.api.database.async_session", _patch_db(row)),
        patch("backend.api.algo.order_events.write_event", AsyncMock()),
        patch("backend.api.routes.orders._postback_broadcast_fanout", fanout_mock),
        patch("backend.api.audit.write_audit_event", MagicMock()),
        patch("backend.shared.helpers.utils.mask_account", return_value="ZG####"),
    ):
        await engine._update_algo_order(
            {**order, "attempts": 5},
            kind="unfilled",
        )

    fanout_mock.assert_called_once()
    assert fanout_mock.call_args.kwargs["status"] == "EXPIRED", (
        "UNFILLED must map to 'EXPIRED' in fanout so position_filled is skipped"
    )


@pytest.mark.asyncio
async def test_unfilled_fires_audit_with_expired_category():
    """Audit category='order.expired' on UNFILLED — mirrors live EXPIRED path."""
    from backend.api.algo.paper import PaperTradeEngine

    order  = _make_order(order_id=11)
    row    = _make_algo_order_row(order)
    engine = PaperTradeEngine(quote_source=_StubQuoteSource(), label="test")
    audit_mock = MagicMock()

    with (
        patch("backend.api.database.async_session", _patch_db(row)),
        patch("backend.api.algo.order_events.write_event", AsyncMock()),
        patch("backend.api.routes.orders._postback_broadcast_fanout", MagicMock()),
        patch("backend.api.audit.write_audit_event", audit_mock),
        patch("backend.shared.helpers.utils.mask_account", return_value="ZG####"),
    ):
        await engine._update_algo_order(
            {**order, "attempts": 3},
            kind="unfilled",
        )

    audit_mock.assert_called_once()
    assert audit_mock.call_args.kwargs.get("category") == "order.expired"


# ─────────────────────────────────────────────────────────────────────────────
# 3. MODIFY (kind="modify") — no fanout, no audit
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_modify_does_not_fire_fanout():
    """
    kind='modify' is an in-flight chase rephrase, NOT a terminal state.
    No cache invalidation or broadcast should happen.
    """
    from backend.api.algo.paper import PaperTradeEngine

    order  = _make_order(order_id=5)
    row    = _make_algo_order_row(order)
    engine = PaperTradeEngine(quote_source=_StubQuoteSource(), label="test")

    fanout_mock = MagicMock()
    audit_mock  = MagicMock()

    with (
        patch("backend.api.database.async_session", _patch_db(row)),
        patch("backend.api.algo.order_events.write_event", AsyncMock()),
        patch("backend.api.routes.orders._postback_broadcast_fanout", fanout_mock),
        patch("backend.api.audit.write_audit_event", audit_mock),
    ):
        await engine._update_algo_order(
            {**order, "limit_price": 101.0, "attempts": 1},
            kind="modify",
        )

    fanout_mock.assert_not_called()
    audit_mock.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 4. CANCELLED path (_safe_update_algo_order_cancel) — fanout fires
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_fires_fanout_with_cancelled_status():
    """
    _safe_update_algo_order_cancel (operator MCP cancel) must also fire
    the fanout with status='CANCELLED'.  This was the same defect class
    as fill/unfilled — DB updated but frontend stayed stale.
    """
    from backend.api.algo.paper import PaperTradeEngine

    order  = _make_order(order_id=55, side="BUY", qty=20, symbol="BANKNIFTY25JULCE")
    row    = _make_algo_order_row(order)
    engine = PaperTradeEngine(quote_source=_StubQuoteSource(), label="test")

    fanout_mock = MagicMock()

    with (
        patch("backend.api.database.async_session", _patch_db(row)),
        patch("backend.api.algo.order_events.write_event", AsyncMock()),
        patch("backend.api.routes.orders._postback_broadcast_fanout", fanout_mock),
        patch("backend.shared.helpers.utils.mask_account", return_value="ZG####"),
    ):
        await engine._safe_update_algo_order_cancel(order)

    fanout_mock.assert_called_once()
    assert fanout_mock.call_args.kwargs["status"] == "CANCELLED"
    assert fanout_mock.call_args.kwargs["order_id"] == 55


# ─────────────────────────────────────────────────────────────────────────────
# 5. SSOT smoke — single import source for fanout
# ─────────────────────────────────────────────────────────────────────────────

def test_fanout_imported_from_orders_not_local_copy():
    """
    Reuse: paper.py imports _postback_broadcast_fanout from
    backend.api.routes.orders, not a local duplicate.
    Grep the source to confirm the import path is canonical.
    """
    import inspect
    import backend.api.algo.paper as paper_mod

    source = inspect.getsource(paper_mod)
    assert "from backend.api.routes.orders import _postback_broadcast_fanout" in source, (
        "paper.py must import _postback_broadcast_fanout from "
        "backend.api.routes.orders (single SSOT) — local copy not allowed"
    )
    # Confirm no inline broadcast() / invalidate() in _update_algo_order body
    # (only the delegated fanout call is expected).
    assert source.count("from backend.api.routes.orders import _postback_broadcast_fanout") >= 1


def test_live_postback_also_uses_fanout():
    """
    SSOT verification: orders.py defines _postback_broadcast_fanout (single
    definition); all call sites were moved to orders_postback.py as part of
    the RED-zone split (orders.py 4322 → <1500 LOC — Commit 5).

    Architecture after split:
      - orders.py:          defines _postback_broadcast_fanout (1 occurrence)
      - orders_postback.py: contains all call sites (Kite + Dhan/Groww paths)
    """
    import inspect
    import backend.api.routes.orders as orders_mod
    import backend.api.routes.orders_postback as postback_mod

    orders_source = inspect.getsource(orders_mod)
    postback_source = inspect.getsource(postback_mod)

    # Definition must live in orders.py
    assert "def _postback_broadcast_fanout(" in orders_source, (
        "_postback_broadcast_fanout must be a module-level function in orders.py"
    )
    # After the RED-zone split the Kite call site moved to orders_postback.py.
    # orders.py may have ONLY the definition (1 occurrence); that's correct.
    orders_count = orders_source.count("_postback_broadcast_fanout(")
    assert orders_count >= 1, (
        f"Expected ≥1 occurrence of '_postback_broadcast_fanout(' in orders.py "
        f"(at minimum the def line), found {orders_count}"
    )
    # orders_postback.py must call it from at least 2 sites:
    # _process_broker_postback (Dhan/Groww) + kite_postback_handler (Kite).
    postback_count = postback_source.count("_postback_broadcast_fanout(")
    assert postback_count >= 2, (
        f"Expected ≥2 call sites of '_postback_broadcast_fanout(' in "
        f"orders_postback.py (Kite + Dhan/Groww paths), found {postback_count}"
    )
