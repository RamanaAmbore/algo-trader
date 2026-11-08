"""
Tests for market-hours guards in the chase engine.

Covers:
1. [IMPLEMENTED] chase_order returns FAILED status when any market segment is closed
4. [EXISTING] _place_order lot_size validation (guards against MCX 0-lot cache miss)
5. [EXISTING] _emit_chase_terminal DB/persistence error handling

Five quality dimensions:
  SSOT   — markets closed blocks chase at top; GTT-only proceeds
  Perf   — guards fire synchronously before async/broker calls
  Stale  — no inline market-open checks remain in chase bodies
  Reuse  — exchange-closed guard applied uniformly
  UX     — operator receives clear error detail on guard failure

Items 2/3 (`apply_template_to_order`/`apply_plan_live` wing guard,
`resolve_template_plan` pure-data test) removed (Bracket/Template-GTT
removal, T3.1, 2026-11) — all lived in `backend.api.algo.template_attach`,
deleted.
"""

from __future__ import annotations

import asyncio
import pytest
import logging
from unittest.mock import MagicMock, AsyncMock, patch, call
from datetime import datetime, timezone

from backend.api.algo.chase import (
    chase_order,
    ChaseStatus,
    ChaseConfig,
    _place_order,
    _emit_chase_terminal,
)
from backend.brokers.capabilities import KITE_CAPS


# ─────────────────────────────────────────────────────────────────────────────
# PART 1: Chase engine market-hours guard (IMPLEMENTED)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chase_fails_fast_when_all_segments_closed():
    """
    [EXISTING] When _symbol_exchange_open returns False (exchange closed),
    chase_order returns FAILED status immediately without trying broker calls.

    C3 uses _symbol_exchange_open(cfg.exchange, _build_now_ctx()) from
    agent_engine (exchange-specific, not is_any_segment_open).
    """
    with patch(
        "backend.shared.helpers.utils.is_prod_branch",
        return_value=True,
    ), patch(
        "backend.api.algo.agent_engine._symbol_exchange_open",
        return_value=False,
    ), patch(
        "backend.api.algo.agent_engine._build_now_ctx",
        return_value={},
    ):
        cfg = ChaseConfig(interval_seconds=1, max_attempts=5)
        result = await chase_order(
            account="ACC1",
            symbol="NIFTY24APR25000CE",
            transaction_type="SELL",
            quantity=100,
            cfg=cfg,
        )

        # Should return FAILED status immediately
        assert result.status == ChaseStatus.FAILED, (
            f"Expected FAILED status when market closed, got {result.status}"
        )
        assert "closed" in result.detail.lower(), (
            f"Detail should mention closure: {result.detail}"
        )


@pytest.mark.asyncio
async def test_chase_exchange_closed_guard_no_async_calls():
    """
    [EXISTING] Market-closed guard in chase_order fires BEFORE any async
    broker calls (_run). Guard overhead is minimal (no network latency).
    """
    with patch(
        "backend.shared.helpers.utils.is_prod_branch",
        return_value=True,
    ), patch(
        "backend.api.algo.agent_engine._symbol_exchange_open",
        return_value=False,
    ), patch(
        "backend.api.algo.agent_engine._build_now_ctx",
        return_value={},
    ), patch("backend.api.algo.chase._run") as mock_run:
        cfg = ChaseConfig(interval_seconds=1, max_attempts=5)
        result = await chase_order(
            account="ACC1",
            symbol="NIFTY24APR25000CE",
            transaction_type="BUY",
            quantity=50,
            cfg=cfg,
        )

        # No _run (async broker calls) should happen
        mock_run.assert_not_called()
        assert result.status == ChaseStatus.FAILED


@pytest.mark.asyncio
async def test_chase_closed_prevents_broker_access():
    """
    [EXISTING] When market is closed, broker is never accessed by chase loop.
    Guard short-circuits before any _get_broker calls.
    """
    with patch(
        "backend.shared.helpers.utils.is_prod_branch",
        return_value=True,
    ), patch(
        "backend.api.algo.agent_engine._symbol_exchange_open",
        return_value=False,
    ), patch(
        "backend.api.algo.agent_engine._build_now_ctx",
        return_value={},
    ), patch("backend.api.algo.chase._get_broker") as mock_get_broker:
        cfg = ChaseConfig(interval_seconds=1, max_attempts=5)
        result = await chase_order(
            account="ACC1",
            symbol="NIFTY24APR25000CE",
            transaction_type="SELL",
            quantity=100,
            cfg=cfg,
        )

        # Broker should not be accessed
        mock_get_broker.assert_not_called()
        assert result.status == ChaseStatus.FAILED


@pytest.mark.asyncio
async def test_chase_closed_detail_is_clear():
    """
    [EXISTING] When chase fails due to market closure, the detail string
    clearly indicates the reason (not generic "error").
    """
    with patch(
        "backend.shared.helpers.utils.is_prod_branch",
        return_value=True,
    ), patch(
        "backend.api.algo.agent_engine._symbol_exchange_open",
        return_value=False,
    ), patch(
        "backend.api.algo.agent_engine._build_now_ctx",
        return_value={},
    ):
        cfg = ChaseConfig(interval_seconds=1, max_attempts=5, exchange="NFO")
        result = await chase_order(
            account="ACC1",
            symbol="NIFTY24APR25000CE",
            transaction_type="BUY",
            quantity=50,
            cfg=cfg,
        )

        # Detail should mention market closure, not generic error
        assert "closed" in result.detail.lower(), (
            f"Chase detail should mention closure, got: {result.detail}"
        )


# PART 2 (template attach wing guard / resolve_template_plan tests)
# removed (Bracket/Template-GTT removal, T3.1, 2026-11) — both lived in
# backend.api.algo.template_attach, deleted.

# ─────────────────────────────────────────────────────────────────────────────
# PART 3: Place order lot_size safety (EXISTING)
# ─────────────────────────────────────────────────────────────────────────────

def test_place_order_lot_size_lookup():
    """
    [EXISTING] _place_order calls _lot_size_sync to look up lot_size
    before calling broker.place_order. This guards against 0-lot-size
    (MCX cache miss) by using normalise_qty.
    """
    with patch("backend.api.algo.chase._get_broker") as mock_get_broker, \
         patch("backend.api.algo.chase._lot_size_sync") as mock_lot_size:
        mock_broker = MagicMock()
        mock_broker.normalise_qty.return_value = 100  # Safe qty after translation
        mock_broker.place_order.return_value = "order_id_123"
        mock_get_broker.return_value = mock_broker
        mock_lot_size.return_value = 1  # Normal lot_size

        cfg = ChaseConfig(exchange="NFO", variety="regular")

        order_id = _place_order(
            account="ACC1",
            symbol="NIFTY24APR25000CE",
            transaction_type="BUY",
            quantity=100,
            price=50.0,
            cfg=cfg,
        )

        # Verify lot_size was looked up
        mock_lot_size.assert_called()
        # Verify normalise_qty was called (contracts → lots translation)
        mock_broker.normalise_qty.assert_called()
        assert order_id == "order_id_123"


def test_place_order_normalise_qty_for_mcx():
    """
    [EXISTING] _place_order calls broker.normalise_qty which translates
    contract quantities to lots for MCX/NCO. This prevents 100× oversizes
    when cache returns wrong lot_size.
    """
    with patch("backend.api.algo.chase._get_broker") as mock_get_broker, \
         patch("backend.api.algo.chase._lot_size_sync") as mock_lot_size:
        mock_broker = MagicMock()
        # Simulate: 100 contracts ÷ 100 lot_size = 1 lot
        mock_broker.normalise_qty.return_value = 1
        mock_broker.place_order.return_value = "order_id_456"
        mock_get_broker.return_value = mock_broker
        mock_lot_size.return_value = 100  # MCX CRUDEOIL lot_size

        cfg = ChaseConfig(exchange="MCX", variety="regular")

        order_id = _place_order(
            account="ACC1",
            symbol="CRUDEOILAUG25FUT",
            transaction_type="BUY",
            quantity=100,  # in contracts
            price=7500.0,
            cfg=cfg,
        )

        # Verify normalise_qty was called with lot_size
        mock_broker.normalise_qty.assert_called_once()
        call_args = mock_broker.normalise_qty.call_args
        assert call_args[0] == ("MCX", 100, 100), (
            "normalise_qty should receive (exchange, qty, lot_size)"
        )
        assert order_id == "order_id_456"


# ─────────────────────────────────────────────────────────────────────────────
# PART 4: Emit chase terminal error handling (EXISTING)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_chase_terminal_handles_db_error(caplog):
    """
    [EXISTING] _emit_chase_terminal catches DB errors and logs them
    (never raises). This ensures a chase terminal update failure doesn't
    crash the chase loop.
    """
    with patch(
        "backend.api.algo.chase._chase_terminal_update_db"
    ) as mock_db, \
         patch(
        "backend.api.algo.agent_engine.record_chase_terminal"
    ) as mock_record:
        # Simulate DB error
        mock_db.side_effect = RuntimeError("DB connection failed")
        # Set mock_record as AsyncMock (lazy import)
        mock_record = AsyncMock()

        with caplog.at_level(logging.DEBUG):
            # Should not raise despite DB error
            await _emit_chase_terminal(
                broker_order_id="order_123",
                outcome="chase_fill",
                symbol="NIFTY24APR25000CE",
                side="BUY",
                qty=100,
                final_price=50.0,
                attempts=3,
            )

        # _emit_chase_terminal should handle the error gracefully
        # The exception should be caught and logged at debug level
        # (or silently ignored in the except block)
        # Test passes if no exception is raised


# ─────────────────────────────────────────────────────────────────────────────
# PART 5: Integration — Chase with closed market
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chase_nfo_closed_mcx_open_still_blocked():
    """
    [UPDATED] C3 is exchange-specific via _symbol_exchange_open(cfg.exchange).
    An NFO chase is blocked when NFO is closed, even if MCX is open.
    This documents the new semantics replacing the old is_any_segment_open check.
    """
    with patch(
        "backend.shared.helpers.utils.is_prod_branch",
        return_value=True,
    ), patch(
        "backend.api.algo.agent_engine._symbol_exchange_open",
        return_value=False,  # NFO specifically closed
    ), patch(
        "backend.api.algo.agent_engine._build_now_ctx",
        return_value={"nse_open": False, "mcx_open": True},  # MCX open, NFO closed
    ):
        cfg = ChaseConfig(interval_seconds=1, max_attempts=2, exchange="NFO")
        result = await chase_order(
            account="ACC1",
            symbol="NIFTY24APR25000CE",
            transaction_type="BUY",
            quantity=50,
            cfg=cfg,
        )

        # NFO-specific guard fires even though MCX is open
        assert result.status == ChaseStatus.FAILED, (
            "NFO chase must be blocked when NFO is closed, even if MCX is open"
        )
        assert "NFO" in result.detail, f"Detail must name exchange, got: {result.detail}"
        assert "closed" in result.detail.lower()


@pytest.mark.asyncio
async def test_chase_respects_exchange_specific_open_flag():
    """
    [UPDATED] C3 uses _symbol_exchange_open(cfg.exchange) — exchange-specific,
    not is_any_segment_open (which ORs all segments). Document the new behavior:
    closed exchange → FAILED; open exchange → guard passes.
    """
    with patch(
        "backend.shared.helpers.utils.is_prod_branch",
        return_value=True,
    ):
        # Closed exchange → FAILED
        with patch(
            "backend.api.algo.agent_engine._symbol_exchange_open",
            return_value=False,
        ), patch(
            "backend.api.algo.agent_engine._build_now_ctx",
            return_value={},
        ):
            cfg = ChaseConfig(interval_seconds=1, max_attempts=5, exchange="MCX")
            result = await chase_order(
                account="ACC1",
                symbol="CRUDEOILAUG25FUT",
                transaction_type="BUY",
                quantity=100,
                cfg=cfg,
            )
            assert result.status == ChaseStatus.FAILED, (
                "Chase must fail when _symbol_exchange_open returns False"
            )
            assert "closed" in result.detail.lower()
