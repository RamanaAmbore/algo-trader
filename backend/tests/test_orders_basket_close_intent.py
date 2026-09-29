"""
Tests for orders_basket.py security/correctness fixes (2026-09-27 audit).

Covers three critical fixes to basket_order_handler:
  1. close-intent verification: _verify_close_intent is called before bypassing caps
  2. Cold instruments-cache: per-leg error instead of raising HTTPException(503)
  3. Preflight blocker demotion: ANY non-ok preflight rejects the leg (no demoted codes)

Pattern: Call basket_order_handler with mocked brokers, positions, and preflight.
Key insight: fetch_positions is patched to return real signed-quantity DataFrame,
so _verify_close_intent's sign+magnitude logic runs without mocking it separately.
"""

import asyncio
import pandas as pd
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from backend.api.schemas import (
    BasketOrderRequest,
    BasketGroup,
    BasketLeg,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_request():
    """Minimal Litestar request with is_demo=False."""
    req = MagicMock()
    req.state = SimpleNamespace(is_demo=False)
    return req


@pytest.fixture
def mock_lot_sizes():
    """Async lot_size resolver keyed by (exchange, symbol)."""
    lot_sizes = {
        ("NFO", "NIFTY25APRFUT"): 75,      # NSE F&O
        ("MCX", "CRUDEOIL26JANFUT"): 100,  # MCX contracts per lot
        ("NSE", "SBIN"): 1,                # Equity
    }

    async def _get_lot_size(exch, sym):
        return lot_sizes.get((exch.upper(), sym.upper()), 0)

    return _get_lot_size


@pytest.fixture
def mock_positions_df():
    """Factory for mock position DataFrames with signed quantities."""
    def _build(account, rows):
        """rows: list of dicts with 'tradingsymbol', 'quantity' (in contracts)."""
        defaults = {
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity": 0,
            "exchange": "NFO",
            "account": account,
        }
        combined = [dict(defaults, **r) for r in rows]
        return pd.DataFrame(combined)

    return _build


@pytest.fixture
def mock_broker():
    """Mock Kite broker with place_order and translate_qty."""
    broker = MagicMock()
    broker.place_order = AsyncMock(return_value="OID1")
    broker.translate_qty = MagicMock(side_effect=lambda exch, qty, ls: qty)
    broker.basket_order_margins = MagicMock(return_value=[])
    return broker


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1: close-intent verification
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketCloseIntentVerification:
    """
    Verify that _verify_close_intent is called to check sign and magnitude
    before bypassing the 5-lot F&O cap and MCX 20-lot cap.
    """

    @pytest.mark.asyncio
    async def test_close_claim_with_no_position_rejected_for_nfo(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        """
        NFO SELL 10 lots (claimed close), no matching position.
        Expected: leg rejected with "safety cap" error (5-lot cap applies).
        Verification fails; cap is NOT bypassed.
        """
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="SELL",
            quantity=10,
            order_type="LIMIT",
            price=22000.0,
            intent="close",  # Client claim: this is a close order
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])

        # Mock empty positions (no matching position)
        empty_df = mock_positions_df("TEST001", [])

        with patch("backend.api.routes.orders_basket.is_admin_request", return_value=True), \
             patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]), \
             patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0), \
             patch("backend.brokers.adapters.kite.get_lot_size", new=mock_lot_sizes), \
             patch("backend.brokers.broker_apis.fetch_positions", return_value=[empty_df]), \
             patch("backend.api.algo.actions.run_preflight", return_value={"ok": True, "blocked": []}), \
             patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
             patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.api.database.async_session") as mock_session, \
             patch("backend.api.routes.orders_basket._attach_basket_leg_template"):

            # Mock DB session for AlgoOrder persistence
            mock_session_inst = AsyncMock()
            mock_session_inst.__aenter__.return_value = mock_session_inst
            mock_session_inst.__aexit__.return_value = False
            mock_session_inst.add = MagicMock()
            mock_session_inst.commit = AsyncMock()
            mock_session.__return_value = mock_session_inst

            result = await basket_order_handler(request_data, mock_request)

            # Assert the leg was rejected
            assert len(result.groups) == 1
            assert len(result.groups[0].results) == 1
            leg_result = result.groups[0].results[0]
            assert leg_result.status == "error", f"Expected error status, got {leg_result.status}"
            assert "safety cap" in leg_result.error.lower() or "5" in leg_result.error, \
                f"Expected safety cap error, got {leg_result.error}"
            # place_order should NOT have been called
            mock_broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_claim_with_insufficient_position_rejected_for_nfo(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        """
        NFO SELL 10 lots (claimed close), but held LONG only 1 lot (75 contracts).
        Expected: leg rejected with "safety cap" error (qty exceeds held position).
        This is the magnitude check in _verify_close_intent.
        """
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="SELL",
            quantity=10,   # Trying to sell 10 lots = 750 contracts
            order_type="LIMIT",
            price=22000.0,
            intent="close",
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])

        # Mock position: LONG 1 lot (75 contracts)
        positions_df = mock_positions_df("TEST001", [
            {"tradingsymbol": "NIFTY25APRFUT", "quantity": 75}
        ])

        with patch("backend.api.routes.orders_basket.is_admin_request", return_value=True), \
             patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]), \
             patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0), \
             patch("backend.brokers.adapters.kite.get_lot_size", new=mock_lot_sizes), \
             patch("backend.brokers.broker_apis.fetch_positions", return_value=[positions_df]), \
             patch("backend.api.algo.actions.run_preflight", return_value={"ok": True, "blocked": []}), \
             patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
             patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.api.database.async_session"), \
             patch("backend.api.routes.orders_basket._attach_basket_leg_template"):

            result = await basket_order_handler(request_data, mock_request)

            leg_result = result.groups[0].results[0]
            assert leg_result.status == "error", \
                f"Expected error for qty exceeding position, got {leg_result.status}"
            assert "safety cap" in leg_result.error.lower() or "5" in leg_result.error, \
                f"Expected 5-lot cap error, got {leg_result.error}"
            mock_broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_intent_verified_bypasses_5lot_cap(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        """
        NFO SELL 10 lots (claimed close), held LONG ≥750 contracts.
        Expected: leg passes (5-lot cap bypassed), preflight and place_order called
        with intent="close".
        """
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="SELL",
            quantity=10,   # 10 lots = 750 contracts
            order_type="LIMIT",
            price=22000.0,
            intent="close",
            # This test exercises the direct broker.place_order path
            # (close-intent verification), not the chase feature — the
            # schema default chase=True would otherwise route this leg
            # through _start_live_chase instead. See test_orders_basket_
            # chase.py for the chase-path equivalent of this scenario.
            chase=False,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])

        # Mock position: LONG 20 lots (1500 contracts) — sufficient to close 10 lots
        positions_df = mock_positions_df("TEST001", [
            {"tradingsymbol": "NIFTY25APRFUT", "quantity": 1500}
        ])

        mock_run_preflight = AsyncMock(return_value={"ok": True, "blocked": []})

        with patch("backend.api.routes.orders_basket.is_admin_request", return_value=True), \
             patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]), \
             patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0), \
             patch("backend.brokers.adapters.kite.get_lot_size", new=mock_lot_sizes), \
             patch("backend.brokers.broker_apis.fetch_positions", return_value=[positions_df]), \
             patch("backend.api.algo.actions.run_preflight", new=mock_run_preflight), \
             patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
             patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.api.database.async_session") as mock_session, \
             patch("backend.api.routes.orders_basket._attach_basket_leg_template"):

            mock_session_inst = AsyncMock()
            mock_session_inst.__aenter__.return_value = mock_session_inst
            mock_session_inst.__aexit__.return_value = False
            mock_session_inst.add = MagicMock()
            mock_session_inst.commit = AsyncMock()
            mock_session_inst.id = 42
            mock_session.__return_value = mock_session_inst

            result = await basket_order_handler(request_data, mock_request)

            leg_result = result.groups[0].results[0]
            assert leg_result.status == "OPEN", \
                f"Expected OPEN status for verified close, got {leg_result.status}: {leg_result.error}"
            # Verify preflight was called with intent="close"
            assert mock_run_preflight.call_count >= 1
            preflight_call_kwargs = mock_run_preflight.call_args[0][1]  # 2nd positional arg
            assert preflight_call_kwargs.get("intent") == "close", \
                f"Expected preflight intent='close', got {preflight_call_kwargs.get('intent')}"
            # Verify place_order was called with intent="close"
            assert mock_broker.place_order.call_count == 1
            place_order_kwargs = mock_broker.place_order.call_args[1]
            assert place_order_kwargs.get("intent") == "close", \
                f"Expected place_order intent='close', got {place_order_kwargs.get('intent')}"

    @pytest.mark.asyncio
    async def test_close_claim_mcx_no_position_rejected(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        """
        MCX SELL 30 lots (claimed close), no matching position.
        Expected: leg rejected with "20-lot MCX" error.
        """
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="CRUDEOIL26JANFUT",
            exchange="MCX",
            transaction_type="SELL",
            quantity=30,   # 30 lots (MCX cap is 20)
            order_type="LIMIT",
            price=5500.0,
            intent="close",
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])

        empty_df = mock_positions_df("TEST001", [])

        with patch("backend.api.routes.orders_basket.is_admin_request", return_value=True), \
             patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]), \
             patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0), \
             patch("backend.brokers.adapters.kite.get_lot_size", new=mock_lot_sizes), \
             patch("backend.brokers.broker_apis.fetch_positions", return_value=[empty_df]), \
             patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
             patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.api.database.async_session"), \
             patch("backend.api.routes.orders_basket._attach_basket_leg_template"):

            result = await basket_order_handler(request_data, mock_request)

            leg_result = result.groups[0].results[0]
            assert leg_result.status == "error"
            assert "20" in leg_result.error and "mcx" in leg_result.error.lower(), \
                f"Expected 20-lot MCX cap error, got {leg_result.error}"
            mock_broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_intent_verified_bypasses_mcx_cap(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        """
        MCX SELL 30 lots (claimed close), held SHORT ≥ 3000 contracts (30 lots).
        Expected: leg passes (MCX 20-lot cap bypassed by verified close intent).
        """
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="CRUDEOIL26JANFUT",
            exchange="MCX",
            transaction_type="BUY",    # BUY to close SHORT
            quantity=30,   # 30 lots × 100 = 3000 contracts
            order_type="LIMIT",
            price=5500.0,
            intent="close",
            # Tests the direct place_order path (close-intent
            # verification), not chase — see test_orders_basket_chase.py.
            chase=False,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])

        # Mock position: SHORT 50 lots (5000 contracts) — sufficient to close 30 lots
        positions_df = mock_positions_df("TEST001", [
            {"tradingsymbol": "CRUDEOIL26JANFUT", "quantity": -5000}  # Negative = SHORT
        ])

        mock_run_preflight = AsyncMock(return_value={"ok": True, "blocked": []})

        with patch("backend.api.routes.orders_basket.is_admin_request", return_value=True), \
             patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]), \
             patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0), \
             patch("backend.brokers.adapters.kite.get_lot_size", new=mock_lot_sizes), \
             patch("backend.brokers.broker_apis.fetch_positions", return_value=[positions_df]), \
             patch("backend.api.algo.actions.run_preflight", new=mock_run_preflight), \
             patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
             patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.api.database.async_session") as mock_session, \
             patch("backend.api.routes.orders_basket._attach_basket_leg_template"):

            mock_session_inst = AsyncMock()
            mock_session_inst.__aenter__.return_value = mock_session_inst
            mock_session_inst.__aexit__.return_value = False
            mock_session_inst.add = MagicMock()
            mock_session_inst.commit = AsyncMock()
            mock_session_inst.id = 42
            mock_session.__return_value = mock_session_inst

            result = await basket_order_handler(request_data, mock_request)

            leg_result = result.groups[0].results[0]
            assert leg_result.status == "OPEN", \
                f"Expected OPEN for verified MCX close, got {leg_result.status}: {leg_result.error}"
            mock_broker.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_claim_unverified_uses_full_cap(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        """
        NFO SELL 3 lots (under 5-lot cap), claimed close, no position.
        Expected: leg proceeds (3 < 5), but preflight and place_order
        receive intent=None (unverified claim not propagated).
        This tests that _leg_verified_intent replaces the raw intent in all consumers.
        """
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="SELL",
            quantity=3,    # Under cap
            order_type="LIMIT",
            price=22000.0,
            intent="close",  # Claimed but unverified
            # Tests the direct place_order path (intent propagation),
            # not chase — see test_orders_basket_chase.py.
            chase=False,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])

        empty_df = mock_positions_df("TEST001", [])
        mock_run_preflight = AsyncMock(return_value={"ok": True, "blocked": []})

        with patch("backend.api.routes.orders_basket.is_admin_request", return_value=True), \
             patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]), \
             patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0), \
             patch("backend.brokers.adapters.kite.get_lot_size", new=mock_lot_sizes), \
             patch("backend.brokers.broker_apis.fetch_positions", return_value=[empty_df]), \
             patch("backend.api.algo.actions.run_preflight", new=mock_run_preflight), \
             patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
             patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.api.database.async_session") as mock_session, \
             patch("backend.api.routes.orders_basket._attach_basket_leg_template"):

            mock_session_inst = AsyncMock()
            mock_session_inst.__aenter__.return_value = mock_session_inst
            mock_session_inst.__aexit__.return_value = False
            mock_session_inst.add = MagicMock()
            mock_session_inst.commit = AsyncMock()
            mock_session_inst.id = 42
            mock_session.__return_value = mock_session_inst

            result = await basket_order_handler(request_data, mock_request)

            leg_result = result.groups[0].results[0]
            assert leg_result.status == "OPEN", \
                f"Expected OPEN for 3 lots (under cap), got {leg_result.status}"
            # Preflight should receive intent=None (unverified)
            preflight_kwargs = mock_run_preflight.call_args[0][1]
            assert preflight_kwargs.get("intent") is None, \
                f"Expected intent=None for unverified close, got {preflight_kwargs.get('intent')}"
            # place_order should also receive intent=None
            place_order_kwargs = mock_broker.place_order.call_args[1]
            assert place_order_kwargs.get("intent") is None, \
                f"Expected place_order intent=None for unverified, got {place_order_kwargs.get('intent')}"


# ─────────────────────────────────────────────────────────────────────────────
# Fix 2: Cold instruments-cache error handling
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketColdInstrumentsCacheErrorHandling:
    """
    Verify that when lot_size resolution fails (instruments cache cold),
    the handler returns a per-leg error instead of raising HTTPException(503)
    and abandoning earlier legs' results.
    """

    @pytest.mark.asyncio
    async def test_cold_cache_leg_error_preserves_earlier_leg_results(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        """
        Two legs in one group: leg 0 has resolvable lot_size (places successfully),
        leg 1 has unresolvable lot_size (returns error).
        Expected: Both results returned (not 503), leg 0 is OPEN, leg 1 is error.
        """
        from backend.api.routes.orders_basket import basket_order_handler

        leg0 = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="BUY",
            quantity=1,
            order_type="LIMIT",
            price=22000.0,
            # Tests the direct place_order path (cold-cache error
            # handling), not chase — see test_orders_basket_chase.py.
            chase=False,
        )
        leg1 = BasketLeg(
            tradingsymbol="UNKNOWN",  # Will have lot_size = 0
            exchange="NFO",
            transaction_type="BUY",
            quantity=1,
            order_type="LIMIT",
            price=22000.0,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg0, leg1])
        ])

        positions_df = mock_positions_df("TEST001", [])

        async def lot_size_keyed(exch, sym):
            if sym.upper() == "NIFTY25APRFUT":
                return 75
            # UNKNOWN returns 0 (cold cache)
            return 0

        mock_run_preflight = AsyncMock(return_value={"ok": True, "blocked": []})

        with patch("backend.api.routes.orders_basket.is_admin_request", return_value=True), \
             patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]), \
             patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0), \
             patch("backend.brokers.adapters.kite.get_lot_size", new_callable=lambda: lot_size_keyed), \
             patch("backend.brokers.broker_apis.fetch_positions", return_value=[positions_df]), \
             patch("backend.api.algo.actions.run_preflight", new=mock_run_preflight), \
             patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
             patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.api.database.async_session") as mock_session, \
             patch("backend.api.routes.orders_basket._attach_basket_leg_template"):

            mock_session_inst = AsyncMock()
            mock_session_inst.__aenter__.return_value = mock_session_inst
            mock_session_inst.__aexit__.return_value = False
            mock_session_inst.add = MagicMock()
            mock_session_inst.commit = AsyncMock()
            mock_session_inst.id = 42
            mock_session.__return_value = mock_session_inst

            result = await basket_order_handler(request_data, mock_request)

            # Assert both legs' results are present
            assert len(result.groups[0].results) == 2, \
                f"Expected 2 leg results, got {len(result.groups[0].results)}"

            leg0_result = result.groups[0].results[0]
            leg1_result = result.groups[0].results[1]

            # Leg 0 should be OPEN
            assert leg0_result.status == "OPEN", \
                f"Expected leg 0 OPEN, got {leg0_result.status}: {leg0_result.error}"
            # Leg 1 should be error
            assert leg1_result.status == "error", \
                f"Expected leg 1 error, got {leg1_result.status}"
            assert "instruments cache cold" in leg1_result.error.lower() or "unavailable" in leg1_result.error.lower(), \
                f"Expected cache cold error for leg 1, got {leg1_result.error}"

            # place_order should have been called exactly once (for leg 0 only)
            assert mock_broker.place_order.call_count == 1, \
                f"Expected 1 place_order call (leg 0 only), got {mock_broker.place_order.call_count}"


# ─────────────────────────────────────────────────────────────────────────────
# Fix 3: Preflight blocker demotion removed
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketPreflightBlockerDemotionRemoved:
    """
    Verify that ANY non-ok preflight result rejects the leg,
    not just MARGIN_SHORTFALL and SEGMENT_INACTIVE.
    Previously demoted codes: QTY_FREEZE, INSUFFICIENT_FUNDS, LOT_MULTIPLE,
    LOT_SIZE_UNKNOWN, FAT_FINGER_5_LOT_CAP, ACCOUNT_UNKNOWN.
    """

    @pytest.mark.parametrize("blocked_code,reason", [
        ("QTY_FREEZE", "Order type not allowed"),
        ("INSUFFICIENT_FUNDS", "Insufficient funds"),
        ("LOT_MULTIPLE", "Order qty must be a multiple of lot size"),
        ("LOT_SIZE_UNKNOWN", "Lot size unknown for this symbol"),
        ("FAT_FINGER_5_LOT_CAP", "Order exceeds 5-lot safety cap"),
        ("ACCOUNT_UNKNOWN", "Account not recognized"),
    ])
    @pytest.mark.asyncio
    async def test_preflight_blocker_rejects_leg(
        self, blocked_code, reason, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        """
        Preflight returns ok=False with blocked=[{code, reason}].
        Expected: leg rejected with error containing the blocker's reason.
        place_order is NOT called.
        """
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="BUY",
            quantity=1,
            order_type="LIMIT",
            price=22000.0,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])

        positions_df = mock_positions_df("TEST001", [])

        mock_preflight = AsyncMock(return_value={
            "ok": False,
            "blocked": [{"code": blocked_code, "reason": reason}],
            "diagnostics": {},
        })

        with patch("backend.api.routes.orders_basket.is_admin_request", return_value=True), \
             patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]), \
             patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0), \
             patch("backend.brokers.adapters.kite.get_lot_size", new=mock_lot_sizes), \
             patch("backend.brokers.broker_apis.fetch_positions", return_value=[positions_df]), \
             patch("backend.api.algo.actions.run_preflight", new=mock_preflight), \
             patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
             patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.api.database.async_session"), \
             patch("backend.api.routes.orders_basket._attach_basket_leg_template"):

            result = await basket_order_handler(request_data, mock_request)

            leg_result = result.groups[0].results[0]
            assert leg_result.status == "error", \
                f"Expected error for blocked code {blocked_code}, got {leg_result.status}"
            assert reason in leg_result.error or blocked_code in leg_result.error, \
                f"Expected error containing {reason} or {blocked_code}, got {leg_result.error}"

            # place_order must NOT have been called
            mock_broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_preflight_ok_leg_proceeds(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        """
        Preflight returns ok=True.
        Expected: leg proceeds to place_order (positive case).
        """
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="BUY",
            quantity=1,
            order_type="LIMIT",
            price=22000.0,
            # Tests the direct place_order path (preflight-ok proceeds),
            # not chase — see test_orders_basket_chase.py.
            chase=False,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])

        positions_df = mock_positions_df("TEST001", [])
        mock_preflight = AsyncMock(return_value={"ok": True, "blocked": [], "diagnostics": {}})

        with patch("backend.api.routes.orders_basket.is_admin_request", return_value=True), \
             patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]), \
             patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0), \
             patch("backend.brokers.adapters.kite.get_lot_size", new=mock_lot_sizes), \
             patch("backend.brokers.broker_apis.fetch_positions", return_value=[positions_df]), \
             patch("backend.api.algo.actions.run_preflight", new=mock_preflight), \
             patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
             patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.api.database.async_session") as mock_session, \
             patch("backend.api.routes.orders_basket._attach_basket_leg_template"):

            mock_session_inst = AsyncMock()
            mock_session_inst.__aenter__.return_value = mock_session_inst
            mock_session_inst.__aexit__.return_value = False
            mock_session_inst.add = MagicMock()
            mock_session_inst.commit = AsyncMock()
            mock_session_inst.id = 42
            mock_session.__return_value = mock_session_inst

            result = await basket_order_handler(request_data, mock_request)

            leg_result = result.groups[0].results[0]
            assert leg_result.status == "OPEN", \
                f"Expected OPEN for ok=True preflight, got {leg_result.status}: {leg_result.error}"
            mock_broker.place_order.assert_called_once()
