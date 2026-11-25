"""
Tests for Part A's DSL hedge leg wiring —
`backend.api.algo.actions_live._al_resolve_hedge_plan` /
`_al_hedge_close_guard` / `_al_prepare_hedge_leg` / `_al_handle_hedge_chase_outcome`, and
`_action_place_order`'s own hedge orchestration (concurrent chase via
`asyncio.gather`, hedge-failure isolation from the parent's result).

Covers:
  - A no-`hedge_pct` regression guard — the single most important test
    here, since it protects every existing agent that has never set
    this param: omitting it must produce byte-identical single-chase
    behavior to before Part A existed.
  - `_al_hedge_close_guard`'s tri-state fail-safe directly: a fetch
    exception, a full-batch outage, or the TARGET account's OWN frame
    being stale/fetch_failed all skip the hedge — scoped to that one
    account specifically, never inferred from a DIFFERENT account's
    health in the same batch (a genuinely flat, untagged account
    frame still proceeds). An offsetting position of ANY magnitude
    (including an oversized flip) skips the hedge. This is
    deliberately NOT `_verify_close_intent`'s own fail-open convention
    — see the dedicated audit-fix entry in `actions_live.py`'s
    docstrings for why that function is wrong for this gate.
  - `_al_detect_hedge_pairing_issue`: an orphaned naked hedge (parent
    didn't fully fill, hedge did) or an under-covered hedge (hedge
    only PARTIAL) each fire a dedicated alert, distinct from the
    plain "hedge leg failed outright" alert.
  - Hedge skipped when no strike resolves within tolerance.
  - Hedge skipped for a non-option symbol / non-NFO exchange.
  - Hedge skipped when the resolved option chain doesn't actually
    contain the parent's own symbol at its own strike (expiry-
    approximation mismatch sanity check).
  - Both `chase_order()` calls launched concurrently via
    `asyncio.gather`, not sequential awaits, when a hedge resolves.
  - A failed hedge-leg chase (raised exception OR a non-raising
    FAILED/CANCELLED/HELD `ChaseResult`) never blocks or changes the
    parent's own reported result; the parent's outcome alone drives
    `context["_action_result_detail"]`. A PARTIAL or FILLED result is
    not treated as a failure.
  - A hedge that never reaches the broker at all (preflight exception/
    block, DB-write failure) fires a real operator alert, not only a
    `self_alerted` log line.
"""
import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_conns_stub(account: str) -> MagicMock:
    c = MagicMock()
    c.conn = {account: object()}
    return c


def _make_broker_stub(*, ltp_value: float = 50.0) -> MagicMock:
    broker = MagicMock()
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "MCX", "BSE", "CDS"]}
    broker.instruments.return_value = []
    broker.basket_order_margins.return_value = [{"initial": {"total": 5_000.0}}]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": 500_000.0},
        "commodity": {"enabled": True, "net": 500_000.0},
    }
    broker.ltp.return_value = {"NFO:NIFTY26APR22500CE": {"last_price": ltp_value}}
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


_BASE_PARAMS = {
    "account":  "ZG0790",
    "symbol":   "NIFTY26APR22500CE",
    "exchange": "NFO",
    "transaction_type": "BUY",
    "quantity": 50,
    "price":    100.0,
}


def _place_order_patches(broker, conns, mock_chase, *, write_live_order_return=42):
    """The exact patch set `test_actions.py`'s own `_action_place_order`
    tests already establish as sufficient to clear preflight without a
    real broker — reused verbatim here so adding the hedge branch
    cannot regress the pre-existing, already-covered call path."""
    return [
        patch("backend.brokers.connections.Connections", return_value=conns),
        patch("backend.brokers.registry.get_broker", return_value=broker),
        patch("backend.brokers.adapters.kite.get_lot_size", new=AsyncMock(return_value=50)),
        patch("backend.api.algo.chase.chase_order", new=mock_chase),
        patch("backend.api.algo.actions._write_live_order",
              new=AsyncMock(return_value=write_live_order_return)),
        patch("backend.api.algo.actions_live._place_order_set_product_template", new=AsyncMock()),
        patch("backend.brokers.get_broker", return_value=broker),
        patch("backend.brokers.client.is_cutover_on", return_value=False),
    ]


def _apply(patches):
    from contextlib import ExitStack
    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


# ═══════════════════════════════════════════════════════════════════
#  Regression guard — no hedge_pct, byte-identical single-chase
# ═══════════════════════════════════════════════════════════════════

class TestNoHedgePctRegression:
    @pytest.mark.asyncio
    async def test_omitting_hedge_pct_calls_chase_exactly_once(self):
        """The most important test in this file: every agent that has
        never set hedge_pct must see exactly one chase_order() call,
        with no hedge resolution attempted at all."""
        from backend.api.algo.actions_live import _action_place_order

        broker = _make_broker_stub()
        conns = _make_conns_stub("ZG0790")
        agent = MagicMock()
        agent.slug = "test-agent"
        agent.id = 7
        context: dict = {}
        params = dict(_BASE_PARAMS)  # no hedge_pct key at all

        mock_chase = AsyncMock(return_value=MagicMock(status=MagicMock(value="FILLED"), detail=""))
        mock_resolve = AsyncMock(wraps=None)

        with _apply(_place_order_patches(broker, conns, mock_chase)), \
             patch("backend.api.algo.actions_live._al_resolve_hedge_plan",
                   new=AsyncMock(return_value=None)) as mock_resolve:
            await _action_place_order(agent, context, params)

        mock_chase.assert_called_once()
        # Hedge resolution was consulted (hedge_pct=None → it decides
        # None internally) but never produced a hedge context, so no
        # second chase call. Asserting the call happened at all guards
        # against the hedge codepath being skipped entirely by accident.
        mock_resolve.assert_called_once()
        assert mock_resolve.call_args.args[-1] is None  # hedge_pct arg

    @pytest.mark.asyncio
    async def test_hedge_pct_none_resolves_to_no_hedge_via_real_function(self):
        """Exercises the REAL `_al_resolve_hedge_plan` (not mocked) with
        hedge_pct absent — confirms the falsy-check short-circuit fires
        before any close-intent check, quote fetch, or strike search."""
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        context: dict = {}
        with patch("backend.api.algo.actions_live._al_hedge_close_guard") as mock_guard:
            result = await _al_resolve_hedge_plan(
                agent, context, "ZG0790", "NIFTY26APR22500CE", "NFO",
                "BUY", 50, 100.0, None,
            )
        assert result is None
        mock_guard.assert_not_called()

    @pytest.mark.asyncio
    async def test_hedge_pct_zero_resolves_to_no_hedge(self):
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        context: dict = {}
        result = await _al_resolve_hedge_plan(
            agent, context, "ZG0790", "NIFTY26APR22500CE", "NFO",
            "BUY", 50, 100.0, 0,
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════
#  _al_resolve_hedge_plan — skip conditions
# ═══════════════════════════════════════════════════════════════════

class TestHedgePlanSkipConditions:
    @pytest.mark.asyncio
    async def test_non_nfo_exchange_skips_hedge(self):
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        result = await _al_resolve_hedge_plan(
            agent, {}, "ZG0790", "CRUDEOIL26OCTFUT", "MCX",
            "BUY", 1, 5000.0, 50,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_non_option_symbol_skips_hedge(self):
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        result = await _al_resolve_hedge_plan(
            agent, {}, "ZG0790", "RELIANCE", "NFO",
            "BUY", 50, 2800.0, 50,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_future_symbol_on_nfo_skips_hedge(self):
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        result = await _al_resolve_hedge_plan(
            agent, {}, "ZG0790", "NIFTY26APRFUT", "NFO",
            "BUY", 50, 22500.0, 50,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_close_guard_false_skips_hedge(self):
        """`_al_hedge_close_guard` positively returns False (genuinely
        closing, or an untrustworthy fetch) → hedge must never be
        resolved, even if a perfect match would otherwise be found."""
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        with patch("backend.api.algo.actions_live._al_hedge_close_guard",
                   new=AsyncMock(return_value=False)) as mock_guard, \
             patch("backend.api.algo.hedge_strike.fetch_hedge_quotes") as mock_fetch, \
             patch("backend.api.algo.hedge_strike.resolve_hedge_strike") as mock_resolve:
            result = await _al_resolve_hedge_plan(
                agent, {}, "ZG0790", "NIFTY26APR22500CE", "NFO",
                "SELL", 50, 100.0, 50,
            )
        assert result is None
        mock_guard.assert_awaited_once()
        mock_fetch.assert_not_called()
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_guard_exception_fails_safe_to_no_hedge(self):
        """Fail-safe path (operator decision, Part A design): an
        exception from the close guard itself — the outer broad
        try/except in `_al_resolve_hedge_plan` — must be treated as
        'potential close, skip hedge', never as 'proceed, resolve a
        hedge'. `_al_hedge_close_guard` is designed not to raise, but
        this proves the outer safety net holds even if it somehow did."""
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        with patch("backend.api.algo.actions_live._al_hedge_close_guard",
                   new=AsyncMock(side_effect=RuntimeError("broker down"))), \
             patch("backend.api.algo.hedge_strike.fetch_hedge_quotes") as mock_fetch, \
             patch("backend.api.algo.hedge_strike.resolve_hedge_strike") as mock_resolve:
            result = await _al_resolve_hedge_plan(
                agent, {}, "ZG0790", "NIFTY26APR22500CE", "NFO",
                "BUY", 50, 100.0, 50,
            )
        assert result is None
        mock_fetch.assert_not_called()
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_strike_found_within_tolerance_skips_hedge(self):
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        with patch("backend.api.algo.actions_live._al_hedge_close_guard",
                   new=AsyncMock(return_value=True)), \
             patch("backend.api.algo.hedge_strike.resolve_parent_real_expiry",
                   new=AsyncMock(return_value="2026-04-28")), \
             patch("backend.api.algo.hedge_strike.candidates_match_parent_symbol",
                   return_value=True), \
             patch("backend.api.algo.hedge_strike.fetch_hedge_quotes",
                   new=AsyncMock(return_value={22600.0: {"ce": {"bid": 1.0, "ask": 1.0, "sym": "X"}}})):
            result = await _al_resolve_hedge_plan(
                agent, {}, "ZG0790", "NIFTY26APR22500CE", "NFO",
                "BUY", 50, 100.0, 50,
            )
        # ratio 1% vs target 50% is way outside tolerance — real
        # resolve_hedge_strike() runs unmocked here and must reject it.
        assert result is None

    @pytest.mark.asyncio
    async def test_resolved_strike_with_no_tradingsymbol_skips_hedge(self):
        """A pathological candidates dict where the winning strike's
        own quote entry carries no `sym` — must degrade to no-hedge
        rather than place an order with an empty symbol."""
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        with patch("backend.api.algo.actions_live._al_hedge_close_guard",
                   new=AsyncMock(return_value=True)), \
             patch("backend.api.algo.hedge_strike.resolve_parent_real_expiry",
                   new=AsyncMock(return_value="2026-04-28")), \
             patch("backend.api.algo.hedge_strike.candidates_match_parent_symbol",
                   return_value=True), \
             patch("backend.api.algo.hedge_strike.fetch_hedge_quotes",
                   new=AsyncMock(return_value={22600.0: {"ce": {"bid": 50.0, "ask": 50.0, "sym": ""}}})):
            result = await _al_resolve_hedge_plan(
                agent, {}, "ZG0790", "NIFTY26APR22500CE", "NFO",
                "BUY", 50, 100.0, 50,
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_parent_symbol_absent_from_resolved_chain_skips_hedge(self):
        """Real (unmocked) `candidates_match_parent_symbol` — the
        resolved chain contains ONLY the hedge candidate, not the
        parent's own strike/symbol, the exact shape a wrong-expiry
        fetch would produce — must skip the hedge."""
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        candidates = {22600.0: {"ce": {"bid": 48.0, "ask": 50.0, "sym": "NIFTY26APR22600CE"}}}
        with patch("backend.api.algo.actions_live._al_hedge_close_guard",
                   new=AsyncMock(return_value=True)), \
             patch("backend.api.algo.hedge_strike.resolve_parent_real_expiry",
                   new=AsyncMock(return_value="2026-04-28")), \
             patch("backend.api.algo.hedge_strike.fetch_hedge_quotes",
                   new=AsyncMock(return_value=candidates)):
            result = await _al_resolve_hedge_plan(
                agent, {}, "ZG0790", "NIFTY26APR22500CE", "NFO",
                "BUY", 50, 100.0, 50,
            )
        assert result is None


# ═══════════════════════════════════════════════════════════════════
#  _al_hedge_close_guard — ONE fetch_positions() pass, scoped to the
#  TARGET account specifically, never inferred from a different
#  account's health in the same batch
# ═══════════════════════════════════════════════════════════════════

class TestHedgeCloseGuard:
    @pytest.mark.asyncio
    async def test_fetch_exception_skips_hedge(self):
        from backend.api.algo.actions_live import _al_hedge_close_guard

        with patch("backend.brokers.broker_apis.fetch_positions",
                   side_effect=RuntimeError("broker down")), \
             patch("backend.api.routes.orders_place._fetch_net_position_qty") as mock_qty:
            result = await _al_hedge_close_guard("NIFTY26APR22500CE", "NFO", "BUY", "ZG0790", 50)
        assert result is False
        mock_qty.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_account_stale_while_another_account_is_fresh_skips(self):
        """The exact fail-open this guard must NOT have: one account's
        frame is stale/degraded, a DIFFERENT account's frame in the same
        batch is perfectly healthy — the target account's own
        degradation must still skip the hedge, never masked by the
        other account's health."""
        import pandas as pd
        from backend.api.algo.actions_live import _al_hedge_close_guard

        target_df = pd.DataFrame({"tradingsymbol": ["X"], "quantity": [1]})
        target_df.attrs["account"] = "ZG0790"
        target_df.attrs["stale"] = True
        other_df = pd.DataFrame({"account": ["ZJ6294"], "tradingsymbol": ["Y"], "quantity": [0]})

        with patch("backend.brokers.broker_apis.fetch_positions",
                   return_value=[target_df, other_df]), \
             patch("backend.api.routes.orders_place._fetch_net_position_qty") as mock_qty:
            result = await _al_hedge_close_guard("NIFTY26APR22500CE", "NFO", "BUY", "ZG0790", 50)
        assert result is False
        mock_qty.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_account_fetch_failed_while_another_account_is_fresh_skips(self):
        import pandas as pd
        from backend.api.algo.actions_live import _al_hedge_close_guard

        target_df = pd.DataFrame()
        target_df.attrs["account"] = "ZG0790"
        target_df.attrs["fetch_failed"] = True
        other_df = pd.DataFrame({"account": ["ZJ6294"], "tradingsymbol": ["Y"], "quantity": [0]})

        with patch("backend.brokers.broker_apis.fetch_positions",
                   return_value=[target_df, other_df]), \
             patch("backend.api.routes.orders_place._fetch_net_position_qty") as mock_qty:
            result = await _al_hedge_close_guard("NIFTY26APR22500CE", "NFO", "BUY", "ZG0790", 50)
        assert result is False
        mock_qty.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_accounts_healthy_and_empty_proceeds(self):
        """The common, genuinely-flat case — e.g. the first trade of
        the day for a fresh account — must not be refused just because
        every frame came back with zero rows and no explicit tag."""
        import pandas as pd
        from backend.api.algo.actions_live import _al_hedge_close_guard

        df = pd.DataFrame()  # no rows, no attrs at all — a confirmed-healthy empty fetch
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]), \
             patch("backend.api.routes.orders_place._fetch_net_position_qty",
                   new=AsyncMock(return_value=None)):
            result = await _al_hedge_close_guard("NIFTY26APR22500CE", "NFO", "BUY", "ZG0790", 50)
        assert result is True

    @pytest.mark.asyncio
    async def test_target_flat_inside_a_non_empty_healthy_frame_proceeds(self):
        """The target account genuinely holds OTHER symbols (its frame
        is non-empty and healthy), but has no position in THIS symbol
        — still a safe, confirmed-flat case for this order."""
        import pandas as pd
        from backend.api.algo.actions_live import _al_hedge_close_guard

        df = pd.DataFrame({"account": ["ZG0790"], "tradingsymbol": ["SOME_OTHER_SYMBOL"], "quantity": [10]})
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]), \
             patch("backend.api.routes.orders_place._fetch_net_position_qty",
                   new=AsyncMock(return_value=None)):
            result = await _al_hedge_close_guard("NIFTY26APR22500CE", "NFO", "BUY", "ZG0790", 50)
        assert result is True

    @pytest.mark.asyncio
    async def test_full_batch_outage_skips_even_with_no_per_account_tag(self):
        """An empty per-account list while accounts are genuinely
        configured is `_is_positions_outage`'s own "full outage" shape
        — must skip regardless of what (nothing) the signal says about
        the target account specifically."""
        from backend.api.algo.actions_live import _al_hedge_close_guard

        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[]), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["ZG0790"]), \
             patch("backend.api.routes.orders_place._fetch_net_position_qty") as mock_qty:
            result = await _al_hedge_close_guard("NIFTY26APR22500CE", "NFO", "BUY", "ZG0790", 50)
        assert result is False
        mock_qty.assert_not_called()

    @pytest.mark.asyncio
    async def test_buy_order_against_held_short_skips_at_any_magnitude(self):
        """A BUY order against a held short position is at least
        partly closing — including an OVERSIZED order (a flip past
        flat), which `_verify_close_intent`'s own magnitude-bounded
        check would have let through as 'not closing'."""
        import pandas as pd
        from backend.api.algo.actions_live import _al_hedge_close_guard

        df = pd.DataFrame({"account": ["ZG0790"], "tradingsymbol": ["NIFTY26APR22500CE"], "quantity": [-10]})
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]), \
             patch("backend.api.routes.orders_place._fetch_net_position_qty",
                   new=AsyncMock(return_value=-10.0)):
            # Order qty (500) far exceeds the held short (10) — a flip.
            result = await _al_hedge_close_guard("NIFTY26APR22500CE", "NFO", "BUY", "ZG0790", 500)
        assert result is False

    @pytest.mark.asyncio
    async def test_sell_order_against_held_long_skips_at_any_magnitude(self):
        import pandas as pd
        from backend.api.algo.actions_live import _al_hedge_close_guard

        df = pd.DataFrame({"account": ["ZG0790"], "tradingsymbol": ["NIFTY26APR22500CE"], "quantity": [10]})
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]), \
             patch("backend.api.routes.orders_place._fetch_net_position_qty",
                   new=AsyncMock(return_value=10.0)):
            result = await _al_hedge_close_guard("NIFTY26APR22500CE", "NFO", "SELL", "ZG0790", 500)
        assert result is False

    @pytest.mark.asyncio
    async def test_same_direction_position_is_safe(self):
        """A BUY order against an existing LONG position is adding to
        it, not closing — the hedge may safely resolve."""
        import pandas as pd
        from backend.api.algo.actions_live import _al_hedge_close_guard

        df = pd.DataFrame({"account": ["ZG0790"], "tradingsymbol": ["NIFTY26APR22500CE"], "quantity": [10]})
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]), \
             patch("backend.api.routes.orders_place._fetch_net_position_qty",
                   new=AsyncMock(return_value=10.0)):
            result = await _al_hedge_close_guard("NIFTY26APR22500CE", "NFO", "BUY", "ZG0790", 50)
        assert result is True

    @pytest.mark.asyncio
    async def test_qty_fetch_exception_fails_safe(self):
        import pandas as pd
        from backend.api.algo.actions_live import _al_hedge_close_guard

        df = pd.DataFrame({"account": ["ZG0790"], "tradingsymbol": ["OTHER"], "quantity": [0]})
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]), \
             patch("backend.api.routes.orders_place._fetch_net_position_qty",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await _al_hedge_close_guard("NIFTY26APR22500CE", "NFO", "BUY", "ZG0790", 50)
        assert result is False


# ═══════════════════════════════════════════════════════════════════
#  _al_resolve_hedge_plan — happy path
# ═══════════════════════════════════════════════════════════════════

class TestHedgePlanHappyPath:
    @pytest.mark.asyncio
    async def test_resolves_hedge_leg_opposite_side_same_qty(self):
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        candidates = {22600.0: {"ce": {"bid": 48.0, "ask": 50.0, "sym": "NIFTY26APR22600CE"}}}
        with patch("backend.api.algo.actions_live._al_hedge_close_guard",
                   new=AsyncMock(return_value=True)), \
             patch("backend.api.algo.hedge_strike.resolve_parent_real_expiry",
                   new=AsyncMock(return_value="2026-04-28")), \
             patch("backend.api.algo.hedge_strike.candidates_match_parent_symbol",
                   return_value=True), \
             patch("backend.api.algo.hedge_strike.fetch_hedge_quotes",
                   new=AsyncMock(return_value=candidates)) as mock_fetch:
            result = await _al_resolve_hedge_plan(
                agent, {}, "ZG0790", "NIFTY26APR22500CE", "NFO",
                "BUY", 50, 100.0, 50,
            )
        assert result == {
            "symbol": "NIFTY26APR22600CE",
            "exchange": "NFO",
            "side": "SELL",
            "qty": 50,
            "price": 48.0,
        }
        mock_fetch.assert_awaited_once()
        call_args = mock_fetch.call_args.args
        assert call_args[0] == "NIFTY"
        assert call_args[1] == "2026-04-28"

    @pytest.mark.asyncio
    async def test_sell_parent_resolves_buy_hedge(self):
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        candidates = {22400.0: {"pe": {"bid": 23.0, "ask": 25.0, "sym": "NIFTY26APR22400PE"}}}
        with patch("backend.api.algo.actions_live._al_hedge_close_guard",
                   new=AsyncMock(return_value=True)), \
             patch("backend.api.algo.hedge_strike.resolve_parent_real_expiry",
                   new=AsyncMock(return_value="2026-04-28")), \
             patch("backend.api.algo.hedge_strike.candidates_match_parent_symbol",
                   return_value=True), \
             patch("backend.api.algo.hedge_strike.fetch_hedge_quotes",
                   new=AsyncMock(return_value=candidates)):
            result = await _al_resolve_hedge_plan(
                agent, {}, "ZG0790", "NIFTY26APR22500PE", "NFO",
                "SELL", 50, 100.0, 25,
            )
        assert result["side"] == "BUY"
        assert result["symbol"] == "NIFTY26APR22400PE"
        assert result["price"] == 25.0  # BUY hedge reads ask

    @pytest.mark.asyncio
    async def test_real_sanity_check_passes_with_consistent_chain(self):
        """Uses the REAL (unmocked) `candidates_match_parent_symbol` and
        `resolve_parent_real_expiry` — a candidates dict that includes
        BOTH the parent's own strike/symbol and the hedge candidate
        resolves correctly end to end."""
        from backend.api.algo.actions_live import _al_resolve_hedge_plan

        agent = MagicMock()
        candidates = {
            22500.0: {"ce": {"bid": 95.0, "ask": 100.0, "sym": "NIFTY26APR22500CE"}},
            22600.0: {"ce": {"bid": 48.0, "ask": 50.0, "sym": "NIFTY26APR22600CE"}},
        }
        with patch("backend.api.algo.actions_live._al_hedge_close_guard",
                   new=AsyncMock(return_value=True)), \
             patch("backend.api.cache.peek", return_value=None), \
             patch("backend.api.algo.hedge_strike.fetch_hedge_quotes",
                   new=AsyncMock(return_value=candidates)):
            result = await _al_resolve_hedge_plan(
                agent, {}, "ZG0790", "NIFTY26APR22500CE", "NFO",
                "BUY", 50, 100.0, 50,
            )
        assert result == {
            "symbol": "NIFTY26APR22600CE",
            "exchange": "NFO",
            "side": "SELL",
            "qty": 50,
            "price": 48.0,
        }


# ═══════════════════════════════════════════════════════════════════
#  _al_prepare_hedge_leg — preflight + persist for the hedge row
# ═══════════════════════════════════════════════════════════════════

class TestPrepareHedgeLeg:
    @pytest.mark.asyncio
    async def test_preflight_blocked_returns_none_and_alerts(self):
        from backend.api.algo.actions_live import _al_prepare_hedge_leg

        agent_shim = MagicMock()
        agent_shim.id = 7
        hedge_ctx = {"symbol": "NIFTY26APR22600CE", "exchange": "NFO",
                     "side": "SELL", "qty": 50, "price": 48.0}
        mock_alert = MagicMock()
        with patch("backend.api.algo.actions_live.run_preflight",
                   new=AsyncMock(return_value={"ok": False, "blocked": [{"reason": "margin"}]})), \
             patch("backend.api.algo.actions_live._place_order_write_intent") as mock_persist, \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert", new=mock_alert):
            result = await _al_prepare_hedge_leg(
                agent_shim, "ZG0790", "NFO", hedge_ctx, "NRML", "med",
            )
        assert result is None
        mock_persist.assert_not_called()
        # A hedge that never reaches the broker still leaves the parent
        # naked — the operator must be alerted, not only logged.
        mock_alert.assert_called_once()
        assert "never placed" in mock_alert.call_args.kwargs.get("error", "")
        # The alert must attribute itself to the same agent the hedge
        # leg's own AlgoOrder row carries — never defaulting to manual.
        assert mock_alert.call_args.kwargs.get("agent_id") == 7

    @pytest.mark.asyncio
    async def test_preflight_exception_returns_none_and_alerts(self):
        from backend.api.algo.actions_live import _al_prepare_hedge_leg

        agent_shim = MagicMock()
        hedge_ctx = {"symbol": "NIFTY26APR22600CE", "exchange": "NFO",
                     "side": "SELL", "qty": 50, "price": 48.0}
        mock_alert = MagicMock()
        with patch("backend.api.algo.actions_live.run_preflight",
                   new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert", new=mock_alert):
            result = await _al_prepare_hedge_leg(
                agent_shim, "ZG0790", "NFO", hedge_ctx, "NRML", "med",
            )
        assert result is None
        mock_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_write_failure_returns_none_and_alerts(self):
        from backend.api.algo.actions_live import _al_prepare_hedge_leg

        agent_shim = MagicMock()
        hedge_ctx = {"symbol": "NIFTY26APR22600CE", "exchange": "NFO",
                     "side": "SELL", "qty": 50, "price": 48.0}
        mock_alert = MagicMock()
        with patch("backend.api.algo.actions_live.run_preflight",
                   new=AsyncMock(return_value={"ok": True, "diagnostics": {}})), \
             patch("backend.api.algo.actions_live._place_order_write_intent",
                   new=AsyncMock(return_value=None)), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert", new=mock_alert):
            result = await _al_prepare_hedge_leg(
                agent_shim, "ZG0790", "NFO", hedge_ctx, "NRML", "med",
            )
        assert result is None
        mock_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_happy_path_returns_oid_and_cfg(self):
        from backend.api.algo.actions_live import _al_prepare_hedge_leg
        from backend.api.routes.orders_helpers import _live_chase_config

        agent_shim = MagicMock()
        hedge_ctx = {"symbol": "NIFTY26APR22600CE", "exchange": "NFO",
                     "side": "SELL", "qty": 50, "price": 48.0}
        with patch("backend.api.algo.actions_live.run_preflight",
                   new=AsyncMock(return_value={"ok": True, "diagnostics": {}})), \
             patch("backend.api.algo.actions_live._place_order_write_intent",
                   new=AsyncMock(return_value=99)) as mock_persist:
            result = await _al_prepare_hedge_leg(
                agent_shim, "ZG0790", "NFO", hedge_ctx, "NRML", "high",
            )
        assert result is not None
        oid, cfg = result
        assert oid == 99
        expected = _live_chase_config("high", product="NRML")
        assert cfg.interval_seconds == expected.interval_seconds
        assert cfg.exchange == "NFO"
        mock_persist.assert_awaited_once()
        assert mock_persist.call_args.kwargs.get("action_type") == "place_order_hedge"


# ═══════════════════════════════════════════════════════════════════
#  _al_chase_outcome_is_failure — non-raising ChaseResult failures
# ═══════════════════════════════════════════════════════════════════

class TestHedgeChaseOutcomeIsFailure:
    def test_exception_is_a_failure(self):
        from backend.api.algo.actions_live import _al_chase_outcome_is_failure
        assert _al_chase_outcome_is_failure(RuntimeError("x")) is True

    def test_filled_chase_status_is_not_a_failure(self):
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_chase_outcome_is_failure
        outcome = MagicMock(status=ChaseStatus.FILLED)
        assert _al_chase_outcome_is_failure(outcome) is False

    def test_partial_chase_status_is_not_a_failure(self):
        """A partial fill is still progress toward the hedge, not a
        failure worth paging the operator over."""
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_chase_outcome_is_failure
        outcome = MagicMock(status=ChaseStatus.PARTIAL)
        assert _al_chase_outcome_is_failure(outcome) is False

    def test_failed_chase_status_is_a_failure(self):
        """The position-resolution lock's contended-fast-fail path, and
        ordinary attempt exhaustion, both return a FAILED ChaseResult
        WITHOUT raising — must still be caught as a failure."""
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_chase_outcome_is_failure
        outcome = MagicMock(status=ChaseStatus.FAILED)
        assert _al_chase_outcome_is_failure(outcome) is True

    def test_cancelled_and_held_chase_status_are_failures(self):
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_chase_outcome_is_failure
        assert _al_chase_outcome_is_failure(MagicMock(status=ChaseStatus.CANCELLED)) is True
        assert _al_chase_outcome_is_failure(MagicMock(status=ChaseStatus.HELD)) is True

    def test_bare_string_status_test_double_still_works(self):
        """A hand-built test double with a bare string status (no real
        enum) must compare the same way as a real ChaseStatus."""
        from backend.api.algo.actions_live import _al_chase_outcome_is_failure
        assert _al_chase_outcome_is_failure(MagicMock(status="FILLED")) is False
        assert _al_chase_outcome_is_failure(MagicMock(status="FAILED")) is True

    @pytest.mark.asyncio
    async def test_handle_outcome_alerts_on_non_raising_failed_result(self):
        """`_al_handle_hedge_chase_outcome` must alert even when the
        hedge's own chase_order() call returned a FAILED result rather
        than raising — e.g. the position-lock contended fast-fail."""
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_handle_hedge_chase_outcome

        agent_shim = MagicMock()
        agent_shim.slug = "test-agent"
        agent_shim.id = 7
        hedge_ctx = {"symbol": "NIFTY26APR22600CE", "exchange": "NFO",
                     "side": "SELL", "qty": 50, "price": 48.0}
        outcome = MagicMock(status=ChaseStatus.FAILED, detail="position lock contended")

        mock_alert = MagicMock()
        with patch("backend.shared.helpers.alert_utils.send_order_failure_alert", new=mock_alert):
            await _al_handle_hedge_chase_outcome(agent_shim, "ZG0790", hedge_ctx, outcome)
        mock_alert.assert_called_once()
        # The alert must attribute itself to the same agent the hedge
        # leg's own AlgoOrder row carries — never defaulting to manual.
        assert mock_alert.call_args.kwargs.get("agent_id") == 7

    @pytest.mark.asyncio
    async def test_handle_outcome_does_not_alert_on_partial_fill(self):
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_handle_hedge_chase_outcome

        agent_shim = MagicMock()
        hedge_ctx = {"symbol": "NIFTY26APR22600CE", "exchange": "NFO",
                     "side": "SELL", "qty": 50, "price": 48.0}
        outcome = MagicMock(status=ChaseStatus.PARTIAL, detail="")

        mock_alert = MagicMock()
        with patch("backend.shared.helpers.alert_utils.send_order_failure_alert", new=mock_alert):
            await _al_handle_hedge_chase_outcome(agent_shim, "ZG0790", hedge_ctx, outcome)
        mock_alert.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
#  _al_detect_hedge_pairing_issue — orphan-hedge / under-coverage
# ═══════════════════════════════════════════════════════════════════

class TestDetectHedgePairingIssue:
    def test_both_filled_is_fine(self):
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_detect_hedge_pairing_issue

        parent = MagicMock(status=ChaseStatus.FILLED)
        hedge = MagicMock(status=ChaseStatus.FILLED)
        assert _al_detect_hedge_pairing_issue(parent, hedge) is None

    def test_parent_filled_hedge_dead_is_not_a_pairing_issue(self):
        """A dead hedge (FAILED/CANCELLED/HELD/exception) is already
        covered by `_al_handle_hedge_chase_outcome`'s own alert — this
        function must not ALSO fire for it."""
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_detect_hedge_pairing_issue

        parent = MagicMock(status=ChaseStatus.FILLED)
        hedge = MagicMock(status=ChaseStatus.FAILED)
        assert _al_detect_hedge_pairing_issue(parent, hedge) is None

    def test_parent_exception_hedge_filled_is_an_orphan(self):
        """The clearest, most dangerous case: the parent never placed
        at all, but the hedge is live — a naked, unintended position."""
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_detect_hedge_pairing_issue

        parent = RuntimeError("parent broker rejected")
        hedge = MagicMock(status=ChaseStatus.FILLED)
        reason = _al_detect_hedge_pairing_issue(parent, hedge)
        assert reason is not None
        assert "orphan" in reason.lower()

    def test_parent_failed_hedge_partial_is_an_orphan(self):
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_detect_hedge_pairing_issue

        parent = MagicMock(status=ChaseStatus.FAILED)
        hedge = MagicMock(status=ChaseStatus.PARTIAL)
        reason = _al_detect_hedge_pairing_issue(parent, hedge)
        assert reason is not None
        assert "orphan" in reason.lower()

    def test_parent_partial_hedge_filled_is_flagged(self):
        """The parent itself only partially filled while the hedge is
        at its full intended size — a mismatched, over-hedged pairing,
        covered by the same 'parent did not fully fill' branch."""
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_detect_hedge_pairing_issue

        parent = MagicMock(status=ChaseStatus.PARTIAL)
        hedge = MagicMock(status=ChaseStatus.FILLED)
        reason = _al_detect_hedge_pairing_issue(parent, hedge)
        assert reason is not None

    def test_parent_filled_hedge_partial_is_under_covered(self):
        """Even a fully filled parent is flagged when the hedge itself
        only partially filled — part of the intended coverage is
        missing."""
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_detect_hedge_pairing_issue

        parent = MagicMock(status=ChaseStatus.FILLED)
        hedge = MagicMock(status=ChaseStatus.PARTIAL)
        reason = _al_detect_hedge_pairing_issue(parent, hedge)
        assert reason is not None
        assert "partial" in reason.lower()

    def test_both_dead_is_not_a_pairing_issue(self):
        """Nothing was placed on either leg — no orphan, nothing to
        pair."""
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _al_detect_hedge_pairing_issue

        parent = MagicMock(status=ChaseStatus.CANCELLED)
        hedge = MagicMock(status=ChaseStatus.FAILED)
        assert _al_detect_hedge_pairing_issue(parent, hedge) is None

    @pytest.mark.asyncio
    async def test_orchestration_alerts_on_orphan_hedge(self):
        """End-to-end through `_action_place_order`: the parent's own
        chase raises while the hedge's chase fills — the parent still
        re-raises (existing contract) AND the orphan-pairing alert
        fires before it does."""
        from backend.api.algo.chase import ChaseStatus
        from backend.api.algo.actions_live import _action_place_order

        broker = _make_broker_stub()
        conns = _make_conns_stub("ZG0790")
        agent = MagicMock()
        agent.slug = "test-agent"
        agent.id = 7
        context: dict = {}
        params = dict(_BASE_PARAMS)
        params["hedge_pct"] = 50

        hedge_result = MagicMock(status=ChaseStatus.FILLED, detail="")

        async def _chase_side_effect(**kwargs):
            if kwargs["symbol"] == "NIFTY26APR22500CE":
                raise RuntimeError("parent broker rejected")
            return hedge_result

        mock_chase = AsyncMock(side_effect=_chase_side_effect)
        hedge_ctx = {"symbol": "NIFTY26APR22600CE", "exchange": "NFO",
                     "side": "SELL", "qty": 50, "price": 48.0}
        mock_alert = MagicMock()

        with _apply(_place_order_patches(broker, conns, mock_chase)), \
             patch("backend.api.algo.actions_live._al_resolve_hedge_plan",
                   new=AsyncMock(return_value=hedge_ctx)), \
             patch("backend.api.algo.actions_live._al_prepare_hedge_leg",
                   new=AsyncMock(return_value=(99, MagicMock(exchange="NFO")))), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
                   new=mock_alert):
            with pytest.raises(RuntimeError, match="parent broker rejected"):
                await _action_place_order(agent, context, params)

        # The hedge-outcome alert path (hedge itself did NOT fail) is a
        # no-op, but the EXISTING parent-failure alert (`_place_order_
        # on_failure`) still fires — that pre-existing contract must
        # not regress. The NEW orphan-pairing alert fires alongside it.
        errors = [c.kwargs.get("error", "") for c in mock_alert.call_args_list]
        assert any("orphan" in e.lower() for e in errors), errors
        # The orphan-pairing alert carries the real agent_id — same
        # agent the hedge leg's own AlgoOrder row is tagged with.
        orphan_call = next(c for c in mock_alert.call_args_list if "orphan" in c.kwargs.get("error", "").lower())
        assert orphan_call.kwargs.get("agent_id") == 7


# ═══════════════════════════════════════════════════════════════════
#  _action_place_order orchestration — concurrency + failure isolation
# ═══════════════════════════════════════════════════════════════════

class TestActionPlaceOrderHedgeOrchestration:
    @pytest.mark.asyncio
    async def test_both_chase_calls_launched_concurrently_via_gather(self):
        """Proves asyncio.gather is actually used (both legs' chase
        calls overlap in wall-clock time), not two sequential awaits —
        a sequential await would leave the position naked for the
        parent's entire chase window, defeating the hedge's purpose."""
        from backend.api.algo.actions_live import _action_place_order

        broker = _make_broker_stub()
        conns = _make_conns_stub("ZG0790")
        agent = MagicMock()
        agent.slug = "test-agent"
        agent.id = 7
        context: dict = {}
        params = dict(_BASE_PARAMS)
        params["hedge_pct"] = 50

        call_windows = {}

        async def _slow_chase(**kwargs):
            symbol = kwargs["symbol"]
            start = time.monotonic()
            await asyncio.sleep(0.08)
            call_windows[symbol] = (start, time.monotonic())
            return MagicMock(status=MagicMock(value="FILLED"), detail="")

        mock_chase = AsyncMock(side_effect=_slow_chase)
        hedge_ctx = {"symbol": "NIFTY26APR22600CE", "exchange": "NFO",
                     "side": "SELL", "qty": 50, "price": 48.0}

        with _apply(_place_order_patches(broker, conns, mock_chase)), \
             patch("backend.api.algo.actions_live._al_resolve_hedge_plan",
                   new=AsyncMock(return_value=hedge_ctx)), \
             patch("backend.api.algo.actions_live._al_prepare_hedge_leg",
                   new=AsyncMock(return_value=(99, MagicMock(exchange="NFO")))):
            await _action_place_order(agent, context, params)

        assert mock_chase.await_count == 2
        # Deliberately NOT a wall-clock assertion (e.g. elapsed < N) —
        # under real CPU contention (concurrent sibling test runs on a
        # shared machine) even a generous threshold flakes. The window-
        # overlap assertion below proves concurrency unconditionally:
        # it only holds if both calls were genuinely in flight at once,
        # regardless of how slowly either one actually ran.
        parent_window = call_windows["NIFTY26APR22500CE"]
        hedge_window = call_windows["NIFTY26APR22600CE"]
        # The two call windows must overlap: parent started before the
        # hedge call finished, and vice versa.
        assert parent_window[0] < hedge_window[1]
        assert hedge_window[0] < parent_window[1]

    @pytest.mark.asyncio
    async def test_asyncio_gather_is_the_actual_mechanism(self):
        """Directly asserts asyncio.gather is invoked with both coros
        when a hedge resolves, rather than inferring it from timing."""
        from backend.api.algo.actions_live import _action_place_order

        broker = _make_broker_stub()
        conns = _make_conns_stub("ZG0790")
        agent = MagicMock()
        agent.slug = "test-agent"
        agent.id = 7
        context: dict = {}
        params = dict(_BASE_PARAMS)
        params["hedge_pct"] = 50

        mock_chase = AsyncMock(return_value=MagicMock(status=MagicMock(value="FILLED"), detail=""))
        hedge_ctx = {"symbol": "NIFTY26APR22600CE", "exchange": "NFO",
                     "side": "SELL", "qty": 50, "price": 48.0}

        real_gather = asyncio.gather
        gather_calls = []

        async def _spy_gather(*coros, **kwargs):
            gather_calls.append((len(coros), kwargs.get("return_exceptions")))
            return await real_gather(*coros, **kwargs)

        with _apply(_place_order_patches(broker, conns, mock_chase)), \
             patch("backend.api.algo.actions_live._al_resolve_hedge_plan",
                   new=AsyncMock(return_value=hedge_ctx)), \
             patch("backend.api.algo.actions_live._al_prepare_hedge_leg",
                   new=AsyncMock(return_value=(99, MagicMock(exchange="NFO")))), \
             patch("backend.api.algo.actions_live.asyncio.gather", new=_spy_gather):
            await _action_place_order(agent, context, params)

        assert (2, True) in gather_calls, gather_calls
        assert mock_chase.await_count == 2

    @pytest.mark.asyncio
    async def test_hedge_chase_failure_does_not_block_parent_result(self):
        """The hedge leg's own chase_order() raises; the parent's own
        successful result is still what drives
        context['_action_result_detail'] — the action overall must not
        raise."""
        from backend.api.algo.actions_live import _action_place_order

        broker = _make_broker_stub()
        conns = _make_conns_stub("ZG0790")
        agent = MagicMock()
        agent.slug = "test-agent"
        agent.id = 7
        context: dict = {}
        params = dict(_BASE_PARAMS)
        params["hedge_pct"] = 50

        parent_result = MagicMock()
        parent_result.status = MagicMock(value="FILLED")
        parent_result.detail = "ok"

        async def _chase_side_effect(**kwargs):
            if kwargs["symbol"] == "NIFTY26APR22600CE":
                raise RuntimeError("hedge broker rejected")
            return parent_result

        mock_chase = AsyncMock(side_effect=_chase_side_effect)
        hedge_ctx = {"symbol": "NIFTY26APR22600CE", "exchange": "NFO",
                     "side": "SELL", "qty": 50, "price": 48.0}
        mock_alert = MagicMock()

        with _apply(_place_order_patches(broker, conns, mock_chase)), \
             patch("backend.api.algo.actions_live._al_resolve_hedge_plan",
                   new=AsyncMock(return_value=hedge_ctx)), \
             patch("backend.api.algo.actions_live._al_prepare_hedge_leg",
                   new=AsyncMock(return_value=(99, MagicMock(exchange="NFO")))), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
                   new=mock_alert):
            # Must not raise — the parent placed correctly.
            await _action_place_order(agent, context, params)

        assert context["_action_result_detail"]["status"] == "FILLED"
        assert context["_action_result_detail"]["detail"] == "ok"
        mock_alert.assert_called_once()
        assert "hedge" in mock_alert.call_args.kwargs.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_parent_chase_failure_still_raises_even_with_hedge(self):
        """The hedge branch must not swallow a genuine PARENT failure —
        existing failure-alert + re-raise contract is preserved."""
        from backend.api.algo.actions_live import _action_place_order

        broker = _make_broker_stub()
        conns = _make_conns_stub("ZG0790")
        agent = MagicMock()
        agent.slug = "test-agent"
        agent.id = 7
        context: dict = {}
        params = dict(_BASE_PARAMS)
        params["hedge_pct"] = 50

        hedge_result = MagicMock(status=MagicMock(value="FILLED"), detail="")

        async def _chase_side_effect(**kwargs):
            if kwargs["symbol"] == "NIFTY26APR22500CE":
                raise RuntimeError("parent broker rejected")
            return hedge_result

        mock_chase = AsyncMock(side_effect=_chase_side_effect)
        hedge_ctx = {"symbol": "NIFTY26APR22600CE", "exchange": "NFO",
                     "side": "SELL", "qty": 50, "price": 48.0}

        with _apply(_place_order_patches(broker, conns, mock_chase)), \
             patch("backend.api.algo.actions_live._al_resolve_hedge_plan",
                   new=AsyncMock(return_value=hedge_ctx)), \
             patch("backend.api.algo.actions_live._al_prepare_hedge_leg",
                   new=AsyncMock(return_value=(99, MagicMock(exchange="NFO")))), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
                   new=MagicMock()):
            with pytest.raises(RuntimeError, match="parent broker rejected"):
                await _action_place_order(agent, context, params)

    @pytest.mark.asyncio
    async def test_hedge_resolution_returning_none_calls_chase_once_only(self):
        """When a hedge resolves to None (e.g. no match), the parent
        must still place normally via a single, non-gathered chase
        call — confirms the orchestration branch choice is correct,
        not just that a hedge failure is isolated."""
        from backend.api.algo.actions_live import _action_place_order

        broker = _make_broker_stub()
        conns = _make_conns_stub("ZG0790")
        agent = MagicMock()
        agent.slug = "test-agent"
        agent.id = 7
        context: dict = {}
        params = dict(_BASE_PARAMS)
        params["hedge_pct"] = 50

        mock_chase = AsyncMock(return_value=MagicMock(status=MagicMock(value="FILLED"), detail=""))

        with _apply(_place_order_patches(broker, conns, mock_chase)), \
             patch("backend.api.algo.actions_live._al_resolve_hedge_plan",
                   new=AsyncMock(return_value=None)):
            await _action_place_order(agent, context, params)

        mock_chase.assert_called_once()

    @pytest.mark.asyncio
    async def test_hedge_prepare_failure_calls_chase_once_only(self):
        """A resolved hedge plan whose preflight/persist step
        (`_al_prepare_hedge_leg`) itself fails must also degrade to a
        single, non-gathered parent chase call."""
        from backend.api.algo.actions_live import _action_place_order

        broker = _make_broker_stub()
        conns = _make_conns_stub("ZG0790")
        agent = MagicMock()
        agent.slug = "test-agent"
        agent.id = 7
        context: dict = {}
        params = dict(_BASE_PARAMS)
        params["hedge_pct"] = 50

        mock_chase = AsyncMock(return_value=MagicMock(status=MagicMock(value="FILLED"), detail=""))
        hedge_ctx = {"symbol": "NIFTY26APR22600CE", "exchange": "NFO",
                     "side": "SELL", "qty": 50, "price": 48.0}

        with _apply(_place_order_patches(broker, conns, mock_chase)), \
             patch("backend.api.algo.actions_live._al_resolve_hedge_plan",
                   new=AsyncMock(return_value=hedge_ctx)), \
             patch("backend.api.algo.actions_live._al_prepare_hedge_leg",
                   new=AsyncMock(return_value=None)):
            await _action_place_order(agent, context, params)

        mock_chase.assert_called_once()
