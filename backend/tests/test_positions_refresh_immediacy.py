"""
Tests for the 2026-09 council audit fix — "immediate position refresh"
(perf + architect lenses, independently found both gaps).

Background:
  1. `_positions_refresh_after_fill` (orders.py) polls fetch_positions() up
     to 5x over ~7s after a fill, but called it with no `force_refresh` —
     fetch_positions() is memoized behind a 30s-TTL cache that
     `_rco_invalidate_terminal_caches` only busts ONCE, synchronously,
     right before this task is scheduled. So only the FIRST poll attempt
     was ever a real broker round-trip; every later attempt silently
     re-read that same first result. cur_qty could never differ from
     initial_qty under normal conditions, so the loop reliably timed out
     and `positions_refreshed` essentially never fired — the retry loop
     was dead code in practice.
  2. `_opp_live_handle_success` (orders_place.py, the ticket-placement
     success path) invalidated only the "orders" cache and never
     scheduled ANY positions-refresh poll — that machinery lived
     exclusively in the postback fan-out. Fine for Kite (reliable
     postback), but Dhan/Groww's postback is documented as
     unreliable/manually-configured, so their live orders had NO
     backstop faster than the 5-minute performance poll.

Five quality dimensions:
  1. SSOT   — tests the real functions, not reimplementations.
  2. Perf   — the whole point of this fix; asserts force_refresh is
              actually threaded through, not just that *a* call happens.
  3. Stale  — directly reproduces both reported gaps.
  4. Reuse  — same fetch_positions-mocking shape used elsewhere in this
              suite (return a list of one DataFrame).
  5. UX     — the ticket-success trigger must be scoped to Dhan/Groww
              only (Kite already gets this via its own postback) —
              tested explicitly so a future edit can't silently widen
              or narrow that scope without a test noticing.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── _positions_refresh_after_fill: force_refresh threading ────────────────

class TestPositionsRefreshAfterFillForcesRefresh:
    @pytest.mark.asyncio
    async def test_every_poll_attempt_passes_force_refresh_true(self):
        """The core fix: fetch_positions must be called with
        force_refresh=True on every attempt, not left to the default
        (False), which the 30s-TTL memoization would silently defeat."""
        import pandas as pd
        from backend.api.routes.orders import _positions_refresh_after_fill

        df = pd.DataFrame([{"tradingsymbol": "NIFTY25JUL24000CE", "quantity": 50}])
        calls = []

        def _fake_fetch_positions(*args, **kwargs):
            calls.append(kwargs)
            return [df]

        with patch("asyncio.sleep", new=AsyncMock()), \
             patch("backend.brokers.broker_apis.fetch_positions", side_effect=_fake_fetch_positions), \
             patch("backend.brokers.broker_apis._raw_cache_invalidate"), \
             patch("backend.api.routes.orders.broadcast"):
            await _positions_refresh_after_fill("ZG0001", "NIFTY25JUL24000CE", 50)

        assert len(calls) >= 1, "fetch_positions must be called at least once"
        for i, kwargs in enumerate(calls):
            assert kwargs.get("force_refresh") is True, (
                f"poll attempt {i} did not pass force_refresh=True — "
                f"this is the exact defect that made the retry loop dead code"
            )

    @pytest.mark.asyncio
    async def test_fires_positions_refreshed_when_qty_changes(self):
        """End-to-end: a genuine qty change (simulating a real fill landing
        between poll attempts) must still fire the broadcast — proving the
        fix doesn't just add force_refresh but preserves the actual
        change-detection behavior."""
        import pandas as pd
        from backend.api.routes.orders import _positions_refresh_after_fill

        # First call: no position yet. Second+: position now shows up.
        empty_df = pd.DataFrame([])
        filled_df = pd.DataFrame([{"tradingsymbol": "NIFTY25JUL24000CE", "quantity": 50}])
        call_count = {"n": 0}

        def _fake_fetch_positions(*args, **kwargs):
            call_count["n"] += 1
            return [empty_df] if call_count["n"] == 1 else [filled_df]

        broadcasts = []
        with patch("asyncio.sleep", new=AsyncMock()), \
             patch("backend.brokers.broker_apis.fetch_positions", side_effect=_fake_fetch_positions), \
             patch("backend.brokers.broker_apis._raw_cache_invalidate"), \
             patch("backend.api.routes.orders.broadcast", side_effect=lambda msg: broadcasts.append(msg)):
            await _positions_refresh_after_fill("ZG0001", "NIFTY25JUL24000CE", 50)

        assert any("positions_refreshed" in b for b in broadcasts), (
            "positions_refreshed must fire once a real quantity change is observed"
        )


# ── _opp_live_handle_success: Dhan/Groww proactive refresh trigger ─────────

class TestTicketSuccessTriggersRefreshForDhanGrowwOnly:
    async def _call_handle_success(self, account: str):
        from backend.api.routes.orders_place import _opp_live_handle_success

        data = MagicMock()
        data.chase_aggressiveness = None
        data.source = "ticket"
        data.exchange = "NFO"
        with patch("backend.api.cache.invalidate"), \
             patch("backend.api.algo.order_events.write_event", new=AsyncMock()), \
             patch("backend.api.algo.agent_engine.record_manual_event", new=AsyncMock()), \
             patch("backend.api.routes.orders_helpers._clear_rejections"):
            return await _opp_live_handle_success(
                data, account=account, sym="NIFTY25JUL24000CE", side="BUY",
                qty=50, order_id="ORD1", chase_eligible=False, bk_key="bk1",
                algo_order_id=None,
            )

    @pytest.mark.asyncio
    async def test_dhan_account_schedules_refresh(self):
        scheduled = {"called": False}

        async def _fake_refresh(account, sym, qty_delta):
            scheduled["called"] = True
            scheduled["args"] = (account, sym, qty_delta)

        with patch("backend.brokers.registry._broker_id_for", return_value="dhan"), \
             patch("backend.api.routes.orders._positions_refresh_after_fill", new=_fake_refresh):
            await self._call_handle_success("DH5555")
        # asyncio.create_task schedules but doesn't await inline — yield
        # control once so the task body actually runs before asserting.
        import asyncio
        await asyncio.sleep(0)
        assert scheduled["called"] is True
        assert scheduled["args"] == ("DH5555", "NIFTY25JUL24000CE", 50)

    @pytest.mark.asyncio
    async def test_groww_account_schedules_refresh(self):
        scheduled = {"called": False}

        async def _fake_refresh(account, sym, qty_delta):
            scheduled["called"] = True

        with patch("backend.brokers.registry._broker_id_for", return_value="groww"), \
             patch("backend.api.routes.orders._positions_refresh_after_fill", new=_fake_refresh):
            await self._call_handle_success("GR9999")
        import asyncio
        await asyncio.sleep(0)
        assert scheduled["called"] is True

    @pytest.mark.asyncio
    async def test_kite_account_does_not_schedule_refresh(self):
        """Kite already gets this via its own reliable postback — running
        it again here would just double broker calls for no benefit."""
        scheduled = {"called": False}

        async def _fake_refresh(account, sym, qty_delta):
            scheduled["called"] = True

        with patch("backend.brokers.registry._broker_id_for", return_value="kite"), \
             patch("backend.api.routes.orders._positions_refresh_after_fill", new=_fake_refresh):
            await self._call_handle_success("ZG0001")
        import asyncio
        await asyncio.sleep(0)
        assert scheduled["called"] is False

    @pytest.mark.asyncio
    async def test_registry_failure_does_not_break_order_success_response(self):
        """If _broker_id_for raises for any reason, the ticket success
        response must still be returned — this is a best-effort backstop,
        never allowed to break the primary order-placement response."""
        from backend.api.schemas import TicketOrderResponse

        with patch("backend.brokers.registry._broker_id_for", side_effect=RuntimeError("boom")):
            result = await self._call_handle_success("ZG0001")
        assert isinstance(result, TicketOrderResponse)
        assert result.order_id == "ORD1"
