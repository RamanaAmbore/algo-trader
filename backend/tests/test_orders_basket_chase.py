"""
Tests for orders_basket.py live-mode chase integration (2026-09-29).

Covers the fix that gives the Chain-tab basket live path real chase
parity with the Ticket-tab live path (orders_place.py):

  1. A chase-eligible LIMIT leg (chase=True, positive price) is
     dispatched via `_start_live_chase` INSTEAD OF `broker.place_order`.
  2. Non-eligible legs (MARKET order type, chase=False, or price<=0)
     are completely unaffected — still call `broker.place_order`
     directly, exactly as before this feature existed.
  3. The pre-persisted `AlgoOrder` row's id is passed as `algo_order_id`
     into `_start_live_chase`.
  4. A DB pre-persist failure fails the leg closed: neither
     `broker.place_order` nor `_start_live_chase` is ever called.
  5. `_start_live_chase` receives CONTRACTS (not translate_qty'd lots)
     for MCX legs — chase.py translates internally per attempt.
  6. An unverified close claim under the fat-finger cap still
     propagates the server-VERIFIED intent (None) into `_start_live_chase`,
     not the raw client claim.

Pattern follows test_orders_basket_close_intent.py's mocking conventions.
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from backend.api.schemas import BasketOrderRequest, BasketGroup, BasketLeg


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_request():
    req = MagicMock()
    req.state = SimpleNamespace(is_demo=False)
    return req


@pytest.fixture
def mock_lot_sizes():
    lot_sizes = {
        ("NFO", "NIFTY25APRFUT"): 75,
        ("MCX", "CRUDEOIL26JANFUT"): 100,
        ("NSE", "SBIN"): 1,
    }

    async def _get_lot_size(exch, sym):
        return lot_sizes.get((exch.upper(), sym.upper()), 0)

    return _get_lot_size


@pytest.fixture
def mock_positions_df():
    def _build(account, rows):
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
    """Mock broker. `translate_qty` divides MCX qty by lot_size (mirrors
    the real Kite adapter's lots-convention) so a test that accidentally
    feeds a translated qty into `_start_live_chase` (which expects
    CONTRACTS) is caught rather than silently masked by an identity
    side_effect."""
    broker = MagicMock()
    broker.place_order = AsyncMock(return_value="OID1")

    def _translate(exch, qty, lot_size):
        if exch.upper() in ("MCX", "NCO") and lot_size:
            return max(1, qty // lot_size)
        return qty

    broker.translate_qty = MagicMock(side_effect=_translate)
    broker.basket_order_margins = MagicMock(return_value=[])
    return broker


def _session_mock(assigned_id=42, commit_raises=False):
    """Build a mock for `backend.api.database.async_session` whose
    context-manager instance sets `.id` on the model passed to `.add()`
    right after commit — mirrors a real INSERT...RETURNING id."""
    inst = AsyncMock()
    inst.__aenter__.return_value = inst
    inst.__aexit__.return_value = False

    def _add(obj):
        obj.id = assigned_id

    inst.add = MagicMock(side_effect=_add)
    if commit_raises:
        inst.commit = AsyncMock(side_effect=RuntimeError("db insert failed"))
    else:
        inst.commit = AsyncMock()

    session_factory = MagicMock(return_value=inst)
    return session_factory, inst


def _common_patches(mock_broker, positions_df, lot_size_fn,
                     preflight_result=None, session_factory=None):
    """Common patch set shared by every chase test below."""
    preflight = preflight_result or AsyncMock(return_value={"ok": True, "blocked": []})
    patches = [
        patch("backend.api.routes.orders_basket.is_admin_request", return_value=True),
        patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker),
        patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]),
        patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0),
        patch("backend.brokers.adapters.kite.get_lot_size", new=lot_size_fn),
        patch("backend.brokers.broker_apis.fetch_positions", return_value=[positions_df]),
        patch("backend.api.algo.actions.run_preflight", new=preflight),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()),
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.shared.helpers.settings.get_bool", return_value=False),
        patch("backend.api.routes.orders_basket._attach_basket_leg_template"),
    ]
    if session_factory is not None:
        patches.append(patch("backend.api.database.async_session", session_factory))
    else:
        patches.append(patch("backend.api.database.async_session"))
    return patches


async def _run_with_patches(patches, coro_factory):
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return await coro_factory()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Chase-eligible leg dispatches via _start_live_chase, not place_order
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketChaseEligibleLeg:

    @pytest.mark.asyncio
    async def test_chase_eligible_leg_calls_start_live_chase_not_place_order(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="BUY",
            quantity=1,
            order_type="LIMIT",
            price=22000.0,
            chase=True,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])
        positions_df = mock_positions_df("TEST001", [])
        session_factory, sess_inst = _session_mock(assigned_id=42)

        mock_start_chase = AsyncMock(return_value="CHASE-OID-1")
        mock_seed = AsyncMock()

        patches = _common_patches(
            mock_broker, positions_df, mock_lot_sizes,
            session_factory=session_factory,
        ) + [
            patch("backend.api.routes.orders_basket._start_live_chase", mock_start_chase),
            patch("backend.api.routes.orders_basket._ticket_seed_broker_order_id", mock_seed),
        ]

        result = await _run_with_patches(
            patches, lambda: basket_order_handler(request_data, mock_request)
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "OPEN", leg_result.error
        assert leg_result.order_id == "CHASE-OID-1"

        mock_start_chase.assert_awaited_once()
        mock_broker.place_order.assert_not_called()

        # algo_order_id must be the pre-persisted row's id (fixture: 42).
        _, kwargs = mock_start_chase.call_args
        assert kwargs["algo_order_id"] == 42
        assert kwargs["account"] == "TEST001"
        assert kwargs["symbol"] == "NIFTY25APRFUT"
        assert kwargs["transaction_type"] == "BUY"
        # quantity into chase must be CONTRACTS: 1 lot x 75 = 75.
        assert kwargs["quantity"] == 75
        assert kwargs["aggressiveness"] == "low"

        mock_seed.assert_awaited_once_with(42, "CHASE-OID-1")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Non-eligible legs unaffected — regression guard
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketNonChaseEligibleLegUnaffected:

    @pytest.mark.asyncio
    async def test_market_order_leg_still_calls_place_order_directly(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="BUY",
            quantity=1,
            order_type="MARKET",
            price=None,
            chase=True,   # chase=True but MARKET order type disqualifies
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])
        positions_df = mock_positions_df("TEST001", [])
        session_factory, _ = _session_mock(assigned_id=99)
        mock_start_chase = AsyncMock()

        patches = _common_patches(
            mock_broker, positions_df, mock_lot_sizes,
            session_factory=session_factory,
        ) + [patch("backend.api.routes.orders_basket._start_live_chase", mock_start_chase)]

        result = await _run_with_patches(
            patches, lambda: basket_order_handler(request_data, mock_request)
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "OPEN", leg_result.error
        mock_broker.place_order.assert_called_once()
        mock_start_chase.assert_not_called()

    @pytest.mark.asyncio
    async def test_chase_false_leg_still_calls_place_order_directly(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="BUY",
            quantity=1,
            order_type="LIMIT",
            price=22000.0,
            chase=False,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])
        positions_df = mock_positions_df("TEST001", [])
        session_factory, _ = _session_mock(assigned_id=99)
        mock_start_chase = AsyncMock()

        patches = _common_patches(
            mock_broker, positions_df, mock_lot_sizes,
            session_factory=session_factory,
        ) + [patch("backend.api.routes.orders_basket._start_live_chase", mock_start_chase)]

        result = await _run_with_patches(
            patches, lambda: basket_order_handler(request_data, mock_request)
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "OPEN", leg_result.error
        mock_broker.place_order.assert_called_once()
        mock_start_chase.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_price_leg_still_calls_place_order_directly(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="BUY",
            quantity=1,
            order_type="LIMIT",
            price=0,
            chase=True,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])
        positions_df = mock_positions_df("TEST001", [])
        session_factory, _ = _session_mock(assigned_id=99)
        mock_start_chase = AsyncMock()

        patches = _common_patches(
            mock_broker, positions_df, mock_lot_sizes,
            session_factory=session_factory,
        ) + [patch("backend.api.routes.orders_basket._start_live_chase", mock_start_chase)]

        result = await _run_with_patches(
            patches, lambda: basket_order_handler(request_data, mock_request)
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "OPEN", leg_result.error
        mock_broker.place_order.assert_called_once()
        mock_start_chase.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 3+4. Fail-closed pre-persist guard
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketChasePrePersistFailClosed:

    @pytest.mark.asyncio
    async def test_db_prepersist_failure_rejects_leg_no_broker_call(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        """DB insert failure during pre-persist must refuse the leg
        outright — neither broker.place_order NOR _start_live_chase
        may ever be called (fail-closed invariant, commit 8fca413b
        pattern extended to basket)."""
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="BUY",
            quantity=1,
            order_type="LIMIT",
            price=22000.0,
            chase=True,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])
        positions_df = mock_positions_df("TEST001", [])
        session_factory, _ = _session_mock(commit_raises=True)
        mock_start_chase = AsyncMock()

        patches = _common_patches(
            mock_broker, positions_df, mock_lot_sizes,
            session_factory=session_factory,
        ) + [patch("backend.api.routes.orders_basket._start_live_chase", mock_start_chase)]

        result = await _run_with_patches(
            patches, lambda: basket_order_handler(request_data, mock_request)
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "error"
        assert "tracking row" in leg_result.error.lower() or "retry" in leg_result.error.lower()
        mock_broker.place_order.assert_not_called()
        mock_start_chase.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 5. MCX quantity into chase is CONTRACTS, not lots-translated
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketChaseMcxQuantityConvention:

    @pytest.mark.asyncio
    async def test_mcx_chase_leg_quantity_is_contracts_not_lots(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="CRUDEOIL26JANFUT",
            exchange="MCX",
            transaction_type="BUY",
            quantity=1,   # 1 lot x 100 = 100 contracts
            order_type="LIMIT",
            price=5500.0,
            chase=True,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])
        positions_df = mock_positions_df("TEST001", [])
        session_factory, _ = _session_mock(assigned_id=7)
        mock_start_chase = AsyncMock(return_value="CHASE-OID-MCX")
        mock_seed = AsyncMock()

        patches = _common_patches(
            mock_broker, positions_df, mock_lot_sizes,
            session_factory=session_factory,
        ) + [
            patch("backend.api.routes.orders_basket._start_live_chase", mock_start_chase),
            patch("backend.api.routes.orders_basket._ticket_seed_broker_order_id", mock_seed),
        ]

        result = await _run_with_patches(
            patches, lambda: basket_order_handler(request_data, mock_request)
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "OPEN", leg_result.error

        _, kwargs = mock_start_chase.call_args
        # Must be raw CONTRACTS (100), never divided by lot_size (which
        # would incorrectly yield 1) — chase.py translates internally.
        assert kwargs["quantity"] == 100, (
            f"expected contracts=100 into _start_live_chase, got {kwargs['quantity']} "
            "— MCX qty must not be pre-translated before the chase call"
        )
        # Note: broker.translate_qty IS still called once per leg by the
        # unconditional group-level margin-preview block at the end of
        # _dispatch_group (basket_order_margins payload) — that's
        # unrelated to the chase-vs-direct dispatch decision above, so
        # it is deliberately not asserted here.


# ─────────────────────────────────────────────────────────────────────────────
# 6. Verified-intent propagation into the chase call
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketChaseVerifiedIntentPropagation:

    @pytest.mark.asyncio
    async def test_unverified_close_claim_propagates_none_intent_to_chase(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        """A claimed-but-unverified close (no matching position) under
        the 5-lot cap still proceeds (3 lots < 5), but the server-
        VERIFIED intent (None, not the raw "close" claim) must reach
        _start_live_chase — mirrors the CLAUDE.md verified-intent
        propagation invariant already enforced on the direct place_order
        path."""
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="SELL",
            quantity=3,    # under the 5-lot cap
            order_type="LIMIT",
            price=22000.0,
            intent="close",   # claimed but unverified — no matching position
            chase=True,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])
        empty_df = mock_positions_df("TEST001", [])
        session_factory, _ = _session_mock(assigned_id=11)
        mock_start_chase = AsyncMock(return_value="CHASE-OID-2")
        mock_seed = AsyncMock()

        patches = _common_patches(
            mock_broker, empty_df, mock_lot_sizes,
            session_factory=session_factory,
        ) + [
            patch("backend.api.routes.orders_basket._start_live_chase", mock_start_chase),
            patch("backend.api.routes.orders_basket._ticket_seed_broker_order_id", mock_seed),
        ]

        result = await _run_with_patches(
            patches, lambda: basket_order_handler(request_data, mock_request)
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "OPEN", leg_result.error

        _, kwargs = mock_start_chase.call_args
        assert kwargs["intent"] is None, (
            f"expected verified intent=None (unverified claim), got {kwargs['intent']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Paper-mode chase_agg reads leg.chase_aggressiveness (not hardcoded "low")
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketPaperModeChaseAggressiveness:
    """Small, low-risk paper-branch fix: `chase_agg` passed to
    `register_open_order` must reflect the operator's actual per-leg
    `chase_aggressiveness` choice instead of a hardcoded "low"."""

    async def _run_paper_leg(self, mock_request, mock_lot_sizes,
                              mock_positions_df, mock_broker, leg):
        from backend.api.routes.orders_basket import basket_order_handler

        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])
        positions_df = mock_positions_df("TEST001", [])
        session_factory, _ = _session_mock(assigned_id=55)

        mock_engine = MagicMock()
        mock_engine.register_open_order = MagicMock()
        mock_get_engine = MagicMock(return_value=mock_engine)
        mock_get_agent_id = AsyncMock(return_value=1)

        patches = [
            patch("backend.api.routes.orders_basket.is_admin_request", return_value=True),
            patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker),
            patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]),
            patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0),
            patch("backend.brokers.adapters.kite.get_lot_size", new=mock_lot_sizes),
            patch("backend.brokers.broker_apis.fetch_positions", return_value=[positions_df]),
            # Non-prod branch forces paper mode regardless of DB flags.
            patch("backend.shared.helpers.utils.is_prod_branch", return_value=False),
            patch("backend.shared.helpers.settings.get_bool", return_value=False),
            patch("backend.api.routes.orders_basket._attach_basket_leg_template"),
            patch("backend.api.database.async_session", session_factory),
            patch("backend.api.algo.paper.get_prod_paper_engine", mock_get_engine),
            patch("backend.api.algo.agent_engine.get_agent_id_by_slug", mock_get_agent_id),
        ]

        result = await _run_with_patches(
            patches, lambda: basket_order_handler(request_data, mock_request)
        )
        return result, mock_engine

    @pytest.mark.asyncio
    async def test_paper_chase_agg_reflects_leg_choice(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="BUY",
            quantity=1,
            order_type="LIMIT",
            price=22000.0,
            chase_aggressiveness="high",
        )
        result, mock_engine = await self._run_paper_leg(
            mock_request, mock_lot_sizes, mock_positions_df, mock_broker, leg,
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "PAPER", leg_result.error
        mock_engine.register_open_order.assert_called_once()
        (call_kwargs,), _ = mock_engine.register_open_order.call_args
        assert call_kwargs["chase_agg"] == "high", (
            f"expected chase_agg='high' from leg.chase_aggressiveness, "
            f"got {call_kwargs['chase_agg']}"
        )

    @pytest.mark.asyncio
    async def test_paper_chase_agg_defaults_to_low(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="BUY",
            quantity=1,
            order_type="LIMIT",
            price=22000.0,
            # chase_aggressiveness left at schema default ("low").
        )
        result, mock_engine = await self._run_paper_leg(
            mock_request, mock_lot_sizes, mock_positions_df, mock_broker, leg,
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "PAPER", leg_result.error
        (call_kwargs,), _ = mock_engine.register_open_order.call_args
        assert call_kwargs["chase_agg"] == "low"
