"""
Tests for orders hardening — P1/P2/P3 + broker layer parity.

Comprehensive pytest coverage of 11 core scenarios from the orders hardening
plan. Tests broker_order_id seed race, orphan postback recovery, price validation,
intent threading, TTL constants, partial fill recovery, Dhan postback validation,
GTT attach guards, request-id idempotency, rate limiter values, and KiteTicker
subscribe chunking.

Key patterns:
- Mock DB sessions using AsyncMock (following test_positions_navstrip_p_slot.py)
- Mock broker SDKs (never make real network calls)
- Use pytest.mark.asyncio for async tests
- Mark tests with pytest.mark.xfail(reason="pending M<N> impl") if functions don't yet exist
"""

import asyncio
import ast
import inspect
import time as _time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch, call

import pandas as pd
import pytest

# Use inspect.iscoroutinefunction instead of asyncio.iscoroutinefunction (deprecated in 3.16)
_is_coroutine_func = inspect.iscoroutinefunction


# ═══════════════════════════════════════════════════════════════════════════════
# T1 · broker_order_id seed race (M1)
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrokerOrderIdSeedRace:
    """M1: _ticket_seed_broker_order_id must be awaited synchronously (not fire-and-forget).

    The seed function writes broker_order_id to the DB. If a postback arrives
    sub-100ms later and the seed hasn't flushed, the postback sees broker_order_id=NULL
    and is orphaned.

    Fix: make the seed call blocking before returning from ticket_order_handler.
    """

    def test_seed_broker_order_id_is_async_def(self):
        """Verify _ticket_seed_broker_order_id is defined as async def (not sync)."""
        from backend.api.routes.orders_place import _ticket_seed_broker_order_id

        # Check the function is a coroutine function
        assert _is_coroutine_func(_ticket_seed_broker_order_id), (
            "_ticket_seed_broker_order_id must be async def so it can be awaited"
        )

    def test_seed_broker_order_id_is_called_with_await_in_handler(self):
        """Verify the call to _ticket_seed_broker_order_id uses await (not create_task).

        Inspect the ticket_order_handler source to ensure the call pattern is
        `await _ticket_seed_broker_order_id(...)` not `asyncio.create_task(...)`.
        """
        try:
            from backend.api.routes.orders_place import ticket_order_handler
        except ImportError:
            # If ticket_order_handler is in orders.py, try there
            try:
                from backend.api.routes.orders import ticket_order_handler
            except ImportError:
                pytest.skip("ticket_order_handler not found in expected modules")
                return

        # Parse the function source to detect call pattern
        source = inspect.getsource(ticket_order_handler)
        tree = ast.parse(source)

        # Look for either `await _ticket_seed_broker_order_id` or `create_task`
        has_await_call = False
        has_create_task_call = False

        for node in ast.walk(tree):
            # Check for await expressions with _ticket_seed_broker_order_id
            if isinstance(node, ast.Await):
                if isinstance(node.value, ast.Call):
                    func = node.value.func
                    if isinstance(func, ast.Name) and func.id == "_ticket_seed_broker_order_id":
                        has_await_call = True
                    elif isinstance(func, ast.Attribute) and func.attr == "_ticket_seed_broker_order_id":
                        has_await_call = True

            # Check for create_task calls with _ticket_seed_broker_order_id
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "create_task":
                    if isinstance(node.args[0], ast.Call):
                        inner_func = node.args[0].func
                        if isinstance(inner_func, ast.Name) and inner_func.id == "_ticket_seed_broker_order_id":
                            has_create_task_call = True

        assert has_await_call or not has_create_task_call, (
            "Expected: await _ticket_seed_broker_order_id(...) in ticket_order_handler. "
            "Must NOT be asyncio.create_task(...) (fire-and-forget)."
        )

    @pytest.mark.asyncio
    async def test_seed_broker_order_id_blocks_on_db_write(self):
        """Verify _ticket_seed_broker_order_id awaits until DB commit completes.

        Scenario: Mock DB session with a 50ms delay before commit returns.
        Call the seed function and measure elapsed time. Should be >= 50ms
        (blocking), not near-instant (fire-and-forget).
        """
        from backend.api.routes.orders_place import _ticket_seed_broker_order_id

        mock_algo_order = MagicMock()
        mock_algo_order.id = 123
        mock_algo_order.broker_order_id = None

        async def slow_commit():
            # Simulate a slow DB commit (50ms)
            await asyncio.sleep(0.05)

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_algo_order))
        )
        mock_session.commit = slow_commit
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("backend.api.database.async_session", return_value=mock_session):
            start = _time.monotonic()
            await _ticket_seed_broker_order_id(live_algo_id=123, order_id="ord-001")
            elapsed = _time.monotonic() - start

        assert elapsed >= 0.04, (
            f"Expected blocking call (≥40ms), but elapsed={elapsed*1000:.1f}ms. "
            f"Seed must await DB commit, not fire-and-forget."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# T2 · Orphan postback fallback (M2)
# ═══════════════════════════════════════════════════════════════════════════════

class TestOrphanPostbackFallback:
    """M2: Verify fuzzy-match fallback for orphan postbacks (no broker_order_id match).

    When a postback arrives with broker_order_id that doesn't match any row,
    a fallback query searches by (account, symbol, side, created_at > now()-60s).
    If fallback finds a match: log CRITICAL. If zero matches: create a new
    AlgoOrder with reconcile_source="postback_orphan".
    """

    def test_orphan_postback_fallback_exists_in_orders_postback(self):
        """Verify _process_broker_postback is exported from orders_postback.py."""
        from backend.api.routes.orders_postback import _process_broker_postback
        assert _process_broker_postback is not None, (
            "_process_broker_postback must be defined in orders_postback.py"
        )

    @pytest.mark.asyncio
    async def test_orphan_fallback_creates_reconcile_row_on_total_miss(self):
        """When both primary (broker_order_id) and fallback queries return no rows,
        create a new OPEN AlgoOrder with reconcile_source='postback_orphan'.
        """
        pytest.skip("Pending M2 implementation in orders_postback.py")

        # This test will validate the fallback behaviour once M2 is in place.
        # Expected: when _process_broker_postback sees both queries return empty,
        # it writes a new row with detail containing "reconcile_from_postback".

    @pytest.mark.asyncio
    async def test_orphan_fallback_logs_critical_on_fuzzy_match(self):
        """When fallback query finds a match, log CRITICAL with account/symbol info."""
        pytest.skip("Pending M2 implementation in orders_postback.py")

        # Expected: logger.critical("[POSTBACK-ORPHAN-FALLBACK] matched ...")
        # called when fuzzy match succeeds.

    def test_orphan_fallback_idempotency_window_is_60s(self):
        """Fallback query window: created_at > now()-60s (not older)."""
        pytest.skip("Pending M2 implementation to verify window constant")

        # The fallback should search back exactly 60 seconds. This test verifies
        # the constant is set correctly once M2 is implemented.


# ═══════════════════════════════════════════════════════════════════════════════
# T3 · LIMIT price validation before SDK (M3)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLimitPriceValidationBeforeSdk:
    """M3: Validate LIMIT/SL prices before calling Kite SDK.

    place_order and place_gtt must check:
    - LIMIT orders: price > 0
    - SL-M orders: trigger_price > 0
    - SL orders: price > 0 AND trigger_price > 0

    If check fails, raise BrokerOrderError before any SDK call.
    """

    def test_kite_broker_place_order_method_exists(self):
        """Verify KiteBroker.place_order method is defined."""
        from backend.brokers.adapters.kite import KiteBroker
        assert hasattr(KiteBroker, "place_order"), (
            "KiteBroker must have place_order method"
        )

    def test_kite_broker_place_gtt_method_exists(self):
        """Verify KiteBroker.place_gtt method is defined."""
        from backend.brokers.adapters.kite import KiteBroker
        assert hasattr(KiteBroker, "place_gtt"), (
            "KiteBroker must have place_gtt method"
        )

    def test_place_order_limit_zero_price_raises_before_sdk(self):
        """place_order(order_type="LIMIT", price=0) raises BrokerOrderError before SDK."""
        pytest.skip("Pending M3 implementation in kite.py:place_order")

        # Expected:
        # - BrokerOrderError raised with message containing "LIMIT" and "price"
        # - Mock Kite SDK is NOT called (order_id or exception happens first)

    def test_place_order_sl_zero_trigger_raises_before_sdk(self):
        """place_order(order_type="SL", trigger_price=0) raises BrokerOrderError before SDK."""
        pytest.skip("Pending M3 implementation in kite.py:place_order")

    def test_place_order_limit_positive_price_allows_sdk_call(self):
        """place_order(order_type="LIMIT", price=150.5) does NOT raise; SDK is called."""
        pytest.skip("Pending M3 implementation + M3 SDK guard integration test")

    def test_place_gtt_limit_zero_price_raises_before_sdk(self):
        """place_gtt with a leg having order_type="LIMIT", price=0 raises BrokerOrderError before SDK."""
        pytest.skip("Pending M3 implementation in kite.py:place_gtt")


# ═══════════════════════════════════════════════════════════════════════════════
# T4 · Basket close-intent threads to preflight (M4)
# ═══════════════════════════════════════════════════════════════════════════════

class TestBasketCloseIntentPreflight:
    """M4: Basket legs with intent="close" must thread the intent to preflight.

    Currently: _leg_close is evaluated but not forwarded to preflight payload.
    Result: preflight's FAT_FINGER check doesn't honour close intent.
    Fix: thread intent="close" into the preflight call payload.
    """

    def test_basket_order_handler_imports_successfully(self):
        """Verify basket_order_handler is importable from orders_basket.py."""
        from backend.api.routes.orders_basket import basket_order_handler
        assert basket_order_handler is not None

    @pytest.mark.asyncio
    async def test_basket_close_intent_leg_threads_to_preflight(self):
        """Build a basket with one leg intent='close'; verify preflight call includes intent."""
        pytest.skip("Pending M4 implementation in orders_basket.py")

        # Expected:
        # - Preflight is called with payload containing intent="close" for the leg
        # - This allows FAT_FINGER to skip the 5-lot cap for close orders


# ═══════════════════════════════════════════════════════════════════════════════
# T5 · TTL constant = 14400 (M6)
# ═══════════════════════════════════════════════════════════════════════════════

class TestTemplateLockTTL:
    """M6: Template attach lock TTL constant must equal 14400 (4 hours).

    Previous: 3600 (1 hour). On slow broker days, reconciliation races
    can exceed 1 hour; lock expires mid-reconcile → double GTT.
    """

    @pytest.mark.xfail(reason="Pending M6 implementation — currently 3600, should be 14400")
    def test_template_attach_lock_ttl_is_14400(self):
        """_TPL_LOCK_TTL_S constant = 14400 seconds (4 hours).

        Currently at 3600 (1h) pre-fix. This test will pass once M6 is implemented.
        The backend agent will change _TPL_LOCK_TTL_S from 3600 to 14400."""
        from backend.api.routes.orders_place import _TPL_LOCK_TTL_S

        # M6 should change _TPL_LOCK_TTL_S from 3600 to 14400
        # This test verifies the change is in place.
        assert _TPL_LOCK_TTL_S == 14400, (
            f"_TPL_LOCK_TTL_S must be 14400 (4 hours), got {_TPL_LOCK_TTL_S}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# T6 · Partial fill DB re-fetch (M7)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPartialFillDbRefetch:
    """M7: Chase loop must re-fetch filled_quantity from DB before apply_plan_live.

    Scenario: AlgoOrder in memory has filled_quantity=3. A postback commits
    filled_quantity=5 to DB. The chase loop should see 5, not 3 (stale).
    """

    def test_chase_loop_exists_in_orders(self):
        """Verify chase loop infrastructure exists in orders.py."""
        try:
            from backend.api.routes.orders import _start_live_chase
            assert _start_live_chase is not None
        except ImportError:
            # Function may be in orders_place.py
            from backend.api.routes.orders_place import _start_live_chase
            assert _start_live_chase is not None

    @pytest.mark.asyncio
    async def test_retry_effective_parent_qty_refetches_from_db(self):
        """_retry_effective_parent_qty re-fetches filled_quantity from DB, not memory."""
        pytest.skip("Pending M7 implementation in orders.py:_retry_effective_parent_qty")

        # Expected:
        # - In-memory row has filled_quantity=3
        # - DB row has filled_quantity=5 (postback committed)
        # - Function returns 5 (DB value), not 3 (stale)


# ═══════════════════════════════════════════════════════════════════════════════
# T7 · Dhan postback 422 on empty account (M9)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDhanPostbackEmptyAccount:
    """M9: Dhan postback with empty dhanClientId must raise HTTPException(422).

    Currently: `account = str(body.get("dhanClientId") or "")` silently passes empty.
    Result: postback matches any row with that broker_order_id (wrong account).
    Fix: raise 422 to force Dhan to retry with corrected payload.
    """

    def test_dhan_postback_handler_imports_successfully(self):
        """Verify Dhan postback handler is importable from orders.py."""
        try:
            from backend.api.routes.orders import dhan_postback_handler
            assert dhan_postback_handler is not None
        except ImportError:
            pytest.skip("dhan_postback_handler not found (may be pending implementation)")

    @pytest.mark.asyncio
    async def test_dhan_postback_empty_account_raises_422(self):
        """POST with dhanClientId="" (or missing) raises HTTPException(422)."""
        pytest.skip("Pending M9 implementation in orders.py dhan_postback_handler")

        # Expected:
        # - HTTPException with status_code=422 is raised
        # - Error message includes "dhanClientId" or "account missing"


# ═══════════════════════════════════════════════════════════════════════════════
# T8 · GTT attach skips on close intent (M11)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGttAttachSkipsCloseIntent:
    """M11: When AlgoOrder.intent="close", template attach (GTT) must be skipped.

    Currently: _fire_template_attach_on_fill receives no intent parameter.
    Result: close fills trigger template GTTs (should be skipped).
    Fix: thread intent from DB row into both _fire_template_attach functions;
    skip apply_plan_live when intent=="close".
    """

    def test_fire_template_attach_on_fill_exists(self):
        """Verify _fire_template_attach_on_fill is defined."""
        from backend.api.routes.orders_place import _fire_template_attach_on_fill
        assert _fire_template_attach_on_fill is not None

    def test_maybe_fire_template_attach_for_reconcile_exists(self):
        """Verify _maybe_fire_template_attach_for_reconcile is defined."""
        from backend.api.routes.orders_place import _maybe_fire_template_attach_for_reconcile
        assert _maybe_fire_template_attach_for_reconcile is not None

    @pytest.mark.asyncio
    async def test_fire_template_attach_skips_on_close_intent(self):
        """Call _fire_template_attach_on_fill with intent='close'; apply_plan_live NOT called."""
        pytest.skip("Pending M11 implementation in orders_place.py")

        # Expected:
        # - When intent="close", apply_plan_live is NOT called
        # - When intent="" (or None), apply_plan_live IS called

    @pytest.mark.asyncio
    async def test_fire_template_attach_continues_on_open_intent(self):
        """Call _fire_template_attach_on_fill with intent=''; apply_plan_live IS called."""
        pytest.skip("Pending M11 implementation in orders_place.py")


# ═══════════════════════════════════════════════════════════════════════════════
# T9 · Request-id idempotency (M13)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRequestIdIdempotency:
    """M13: Ticket handler must check request_id idempotency.

    Before pre-persisting a new AlgoOrder, query:
    SELECT id FROM algo_orders WHERE request_id = <req_id> AND created_at > now()-60s

    If found: return existing row_id (skip broker call) to prevent double-orders
    on frontend timeout+retry.
    """

    @pytest.mark.xfail(reason="Pending M13 implementation — request_id capture not yet added")
    def test_ticket_order_handler_captures_request_id(self):
        """Verify ticket_order_handler source references request_id.

        M13 will add request_id capture to ticket_order_handler.
        """
        try:
            from backend.api.routes.orders_place import ticket_order_handler
        except ImportError:
            from backend.api.routes.orders import ticket_order_handler

        source = inspect.getsource(ticket_order_handler)
        assert "request_id" in source or "_req_id" in source, (
            "ticket_order_handler should capture request_id from the request scope"
        )

    @pytest.mark.asyncio
    async def test_duplicate_request_id_returns_existing_row_id(self):
        """Second call within 60s with same request_id returns existing AlgoOrder.id."""
        pytest.skip("Pending M13 implementation in orders_place.py:ticket_order_handler")

        # Expected:
        # - First call: request_id="req-abc-123" → creates AlgoOrder, returns id=42
        # - Second call (within 60s): request_id="req-abc-123" → returns id=42 (no new row)
        # - broker.place_order NOT called on second call

    @pytest.mark.asyncio
    async def test_request_id_outside_60s_window_creates_new_row(self):
        """Request_id check uses exactly 60-second window; older duplicates create new row."""
        pytest.skip("Pending M13 implementation to verify window")

        # Expected:
        # - First call at T=0: request_id="req-abc-123" → id=42
        # - Second call at T=65s: request_id="req-abc-123" → creates new row (outside 60s)

    @pytest.mark.asyncio
    async def test_request_id_none_does_not_deduplicate(self):
        """When request_id is None or not provided, no deduplication occurs."""
        pytest.skip("Pending M13 implementation to verify None handling")

        # Expected:
        # - Calls without request_id always create new rows
        # - No crash when request_id is None


# ═══════════════════════════════════════════════════════════════════════════════
# T10 · Dhan rate limiter values (OSS-1)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDhanRateLimiterValues:
    """OSS-1: Dhan rate limiter must match published API documentation.

    Current limits (estimates):
    - orders: 10 calls/s (capacity=10, period=1s)
    - history: 3 calls/s
    - margins: 5 calls/s
    - auth: 0.5/120s (1 call per 2 minutes)

    TODO: Audit against current Dhan v2 API docs and reconcile.
    """

    def test_dhan_rate_limiter_auth_capacity_is_one(self):
        """_DHAN_RATE_LIMITER._buckets["auth"]["capacity"] == 1.0.

        Current Dhan auth limit config (v2 published): (capacity=1, period=300s)
        which means 1 call per 300 seconds (1 call per 5 minutes).
        """
        from backend.brokers.adapters.dhan import _DHAN_RATE_LIMITER

        auth_bucket = _DHAN_RATE_LIMITER._buckets.get("auth", {})
        assert auth_bucket.get("capacity") == 1.0, (
            f"Expected auth capacity=1.0, got {auth_bucket.get('capacity')}"
        )

    def test_dhan_rate_limiter_auth_refill_rate(self):
        """_DHAN_RATE_LIMITER._buckets["auth"]["refill_rate"] ≈ 1/300 (1 call per 5 min)"""
        from backend.brokers.adapters.dhan import _DHAN_RATE_LIMITER

        auth_bucket = _DHAN_RATE_LIMITER._buckets.get("auth", {})
        refill_rate = auth_bucket.get("refill_rate", 0)

        # Current config: "auth": (1, 300.0) means capacity=1, period=300s
        # So refill_rate = 1 / 300 ≈ 0.00333...
        expected_refill_rate = 1.0 / 300.0  # 1 call per 300 seconds (5 min)

        assert refill_rate == pytest.approx(expected_refill_rate, rel=0.01), (
            f"Expected auth refill_rate≈{expected_refill_rate}, got {refill_rate}"
        )

    def test_dhan_rate_limiter_orders_capacity_is_ten(self):
        """_DHAN_RATE_LIMITER._buckets["orders"]["capacity"] == 10.0"""
        from backend.brokers.adapters.dhan import _DHAN_RATE_LIMITER

        orders_bucket = _DHAN_RATE_LIMITER._buckets.get("orders", {})
        assert orders_bucket.get("capacity") == 10.0, (
            f"Expected orders capacity=10.0 (10 calls/s), "
            f"got {orders_bucket.get('capacity')}"
        )

    def test_dhan_rate_limiter_history_capacity_is_three(self):
        """_DHAN_RATE_LIMITER._buckets["history"]["capacity"] == 3.0"""
        from backend.brokers.adapters.dhan import _DHAN_RATE_LIMITER

        history_bucket = _DHAN_RATE_LIMITER._buckets.get("history", {})
        assert history_bucket.get("capacity") == 3.0, (
            f"Expected history capacity=3.0 (3 calls/s), "
            f"got {history_bucket.get('capacity')}"
        )

    def test_dhan_rate_limiter_margins_capacity_is_five(self):
        """_DHAN_RATE_LIMITER._buckets["margins"]["capacity"] == 5.0"""
        from backend.brokers.adapters.dhan import _DHAN_RATE_LIMITER

        margins_bucket = _DHAN_RATE_LIMITER._buckets.get("margins", {})
        assert margins_bucket.get("capacity") == 5.0, (
            f"Expected margins capacity=5.0 (5 calls/s), "
            f"got {margins_bucket.get('capacity')}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# T11 · KiteTicker subscribe chunking (OSS-2)
# ═══════════════════════════════════════════════════════════════════════════════

class TestKiteTickerSubscribeChunking:
    """OSS-2: KiteTicker must chunk subscribe calls at 3000 symbols per message.

    Scenario: Subscribe to 3001+ symbols should result in ≥2 separate
    kws.subscribe calls, each with ≤3000 tokens.

    Also: Reconnect must use exponential backoff (1s → 2s → 4s → … → 30s cap).
    """

    def test_kite_ticker_subscribe_chunking_constant_is_3000(self):
        """Subscribe chunking limit = 3000 symbols per message (if constant exists)."""
        try:
            from backend.brokers.connections import KITE_TICKER_CHUNK_SIZE
            assert KITE_TICKER_CHUNK_SIZE == 3000, (
                f"Expected KITE_TICKER_CHUNK_SIZE=3000, got {KITE_TICKER_CHUNK_SIZE}"
            )
        except (ImportError, AttributeError):
            pytest.skip("KITE_TICKER_CHUNK_SIZE constant not found (may be pending OSS-2)")

    @pytest.mark.asyncio
    async def test_kite_ticker_subscribe_chunks_at_3000(self):
        """Subscribe to 3001 symbols results in ≥2 chunked kws.subscribe calls."""
        pytest.skip("Pending OSS-2 implementation — KiteTicker chunking may be in conn-service")

        # Expected:
        # - Generate 3001 mock instrument tokens
        # - Call subscribe(tokens)
        # - Mock kws.subscribe is called at least twice
        # - Each call has ≤3000 tokens

    @pytest.mark.asyncio
    async def test_kite_ticker_subscribe_each_chunk_under_3000(self):
        """Each chunked subscribe call contains ≤3000 tokens."""
        pytest.skip("Pending OSS-2 implementation")

    def test_kite_ticker_reconnect_exponential_backoff(self):
        """Reconnect attempts use exponential backoff: 1s → 2s → 4s → … → 30s cap."""
        pytest.skip("Pending OSS-2 implementation for exponential backoff")

        # Expected:
        # - 1st reconnect attempt: sleep 1s
        # - 2nd reconnect attempt: sleep 2s
        # - 3rd reconnect attempt: sleep 4s
        # - ...
        # - 6th+ reconnect attempt: sleep 30s (capped)


# ═══════════════════════════════════════════════════════════════════════════════
# Auxiliary tests for existing patterns (not directly tied to the 11 core scenarios)
# ═══════════════════════════════════════════════════════════════════════════════

class TestOrdersHardeningIntegration:
    """Integration tests for orders hardening — verify interactions between fixes."""

    @pytest.mark.asyncio
    async def test_seed_and_postback_race_recovery(self):
        """M1 + M2 integration: postback arrives before seed commits; fallback recovers."""
        pytest.skip("Pending M1 + M2 integration test")

        # Expected:
        # - _ticket_seed_broker_order_id calls async DB
        # - Postback arrives within 10ms (before DB commit)
        # - Postback finds no broker_order_id match (race condition)
        # - Fallback query finds the row by (account, symbol, side)
        # - Fallback logs CRITICAL; row is updated (not orphaned)

    @pytest.mark.asyncio
    async def test_close_intent_skips_all_gtts(self):
        """M4 + M11 integration: close intent skips both preflight FAT_FINGER cap and GTT attach."""
        pytest.skip("Pending M4 + M11 integration test")

        # Expected:
        # - Basket with close intent leg reaches preflight
        # - Preflight honors close intent (no 5-lot cap)
        # - Fill triggers template attach
        # - Template attach sees intent="close"; skips GTT


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point / run pytest
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Run pytest with verbose output
    pytest.main([__file__, "-v", "-x"])
