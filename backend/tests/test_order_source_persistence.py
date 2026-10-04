"""
Sprint 2b — `AlgoOrder.source` persistence (docs/proposals/SPRINT2_LAYER_INTEGRATION.md
§3 / §4.4).

Before this fix, `TicketOrderRequest.source` (default "ticket") existed on the
request schema and was threaded into `record_manual_event()` (an `agent_events`
write), but every `AlgoOrder(...)` constructor call for ticket/basket placement
silently omitted `source=`, so the column stayed NULL forever regardless of
what the client sent. Covers every call site identified during the audit:

  - Ticket LIVE pre-persist (`_ticket_persist_live_algo_order`)
  - Ticket LIVE preflight-blocked REJECTED row (`_ticket_record_preflight_block`)
  - Ticket PAPER persist (`_opp_paper_persist_row`)
  - Basket LIVE chase-pre-persist leg
  - Basket LIVE direct-place leg
  - Basket SHADOW leg
  - Basket PAPER leg
  - Legacy v1 take-profit shim (`_opp_arm_tp_persist_row`) — fixed origin tag
    "take_profit" (no caller-supplied source to thread through)

Does NOT cover `expiry_auto_close`: verified during the audit that the legacy
`ExpiryEngine.close_positions()` → `chase_order()` path never constructs an
AlgoOrder row at all (no `algo_order_id` is ever passed into `chase_order()`
from that call site) — there is no write site to attribute a source to today.
That is a pre-existing tracking gap, not a `source`-persistence regression,
and is out of scope for this fix.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from backend.api.schemas import BasketOrderRequest, BasketGroup, BasketLeg


# ─────────────────────────────────────────────────────────────────────────────
# Shared basket-mode fixtures (mirrors test_orders_basket_chase.py's harness)
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
    broker = MagicMock()
    broker.place_order = AsyncMock(return_value="OID1")
    broker.translate_qty = MagicMock(side_effect=lambda exch, qty, lot_size: qty)
    broker.basket_order_margins = MagicMock(return_value=[])
    return broker


def _session_mock(assigned_id=42, commit_raises=False):
    """Mock for `backend.api.database.async_session` that stamps `.id` on the
    object passed to `.add()` and records every added row for inspection."""
    inst = AsyncMock()
    inst.__aenter__.return_value = inst
    inst.__aexit__.return_value = False

    added_rows: list = []

    def _add(obj):
        obj.id = assigned_id
        added_rows.append(obj)

    inst.add = MagicMock(side_effect=_add)
    if commit_raises:
        inst.commit = AsyncMock(side_effect=RuntimeError("db insert failed"))
    else:
        inst.commit = AsyncMock()

    session_factory = MagicMock(return_value=inst)
    return session_factory, inst, added_rows


async def _run_with_patches(patches, coro_factory):
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return await coro_factory()


def _basket_common_patches(mock_broker, positions_df, lot_size_fn,
                            preflight_result=None, session_factory=None,
                            shadow=False, paper_trading_mode=False,
                            is_prod_branch=True):
    preflight = preflight_result or AsyncMock(return_value={"ok": True, "blocked": []})

    def _get_bool(key, default=False):
        if key == "execution.shadow_mode":
            return shadow
        if key == "execution.paper_trading_mode":
            return paper_trading_mode
        return default

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
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=is_prod_branch),
        patch("backend.shared.helpers.settings.get_bool", side_effect=_get_bool),
        patch("backend.api.routes.orders_basket._attach_basket_leg_template"),
    ]
    if session_factory is not None:
        patches.append(patch("backend.api.database.async_session", session_factory))
    else:
        patches.append(patch("backend.api.database.async_session"))
    return patches


# ─────────────────────────────────────────────────────────────────────────────
# Ticket LIVE pre-persist — _ticket_persist_live_algo_order
# ─────────────────────────────────────────────────────────────────────────────

def _fake_ticket_data(**over):
    base = dict(
        mode="live", side="BUY", tradingsymbol="NIFTY25JUL24000CE",
        quantity=1, exchange="NFO", account="ZG0790",
        order_type="LIMIT", price=100.0, chase=True,
        chase_aggressiveness="low", product="NRML", variety="regular",
        target_pct=None, template_id=None, strategy_id=None,
        intent=None, source="ticket",
    )
    base.update(over)
    return MagicMock(**base)


class TestTicketLivePersistSource:

    @pytest.mark.asyncio
    async def test_live_persist_writes_default_ticket_source(self):
        from backend.api.routes.orders_place import _ticket_persist_live_algo_order

        data = _fake_ticket_data()
        request = MagicMock()
        request.scope = {"state": {"request_id": None}}

        session_factory, _inst, added_rows = _session_mock(assigned_id=101)

        with patch("backend.api.database.async_session", session_factory), \
             patch("backend.api.algo.agent_engine.get_agent_id_by_slug",
                   new=AsyncMock(return_value=1)), \
             patch("backend.api.routes.orders_helpers._resolve_target_pct",
                   return_value=0.0), \
             patch("backend.api.routes.orders_helpers._build_overrides_json",
                   return_value=None):
            row_id = await _ticket_persist_live_algo_order(
                data, request, "ZG0790", "NIFTY25JUL24000CE", "BUY", 50,
            )

        assert row_id == 101
        assert len(added_rows) == 1
        assert added_rows[0].source == "ticket"

    @pytest.mark.asyncio
    async def test_live_persist_writes_client_supplied_source(self):
        """A future chain/command caller passing source='chain' must reach
        the AlgoOrder row, not just agent_events."""
        from backend.api.routes.orders_place import _ticket_persist_live_algo_order

        data = _fake_ticket_data(source="chain")
        request = MagicMock()
        request.scope = {"state": {"request_id": None}}

        session_factory, _inst, added_rows = _session_mock(assigned_id=102)

        with patch("backend.api.database.async_session", session_factory), \
             patch("backend.api.algo.agent_engine.get_agent_id_by_slug",
                   new=AsyncMock(return_value=1)), \
             patch("backend.api.routes.orders_helpers._resolve_target_pct",
                   return_value=0.0), \
             patch("backend.api.routes.orders_helpers._build_overrides_json",
                   return_value=None):
            await _ticket_persist_live_algo_order(
                data, request, "ZG0790", "NIFTY25JUL24000CE", "BUY", 50,
            )

        assert added_rows[0].source == "chain"


# ─────────────────────────────────────────────────────────────────────────────
# Ticket LIVE preflight-blocked REJECTED row — _ticket_record_preflight_block
# ─────────────────────────────────────────────────────────────────────────────

class TestTicketPreflightBlockedSource:

    @pytest.mark.asyncio
    async def test_preflight_blocked_row_writes_source(self):
        from backend.api.routes.orders_place import _ticket_record_preflight_block

        data = _fake_ticket_data()
        pf = {"blocked": [{"code": "MARGIN_SHORTFALL", "reason": "insufficient margin"}]}

        session_factory, _inst, added_rows = _session_mock(assigned_id=201)

        with patch("backend.api.database.async_session", session_factory), \
             patch("backend.api.algo.agent_engine.get_agent_id_by_slug",
                   new=AsyncMock(return_value=1)), \
             patch("backend.api.algo.order_events.write_event", new=AsyncMock()), \
             patch("backend.api.audit.write_audit_event", new=MagicMock()):
            await _ticket_record_preflight_block(
                data, "ZG0790", "NIFTY25JUL24000CE", "BUY", 50, pf,
            )

        assert len(added_rows) == 1
        assert added_rows[0].source == "ticket"
        assert added_rows[0].status == "REJECTED"


# ─────────────────────────────────────────────────────────────────────────────
# Ticket PAPER persist — _opp_paper_persist_row
# ─────────────────────────────────────────────────────────────────────────────

class TestTicketPaperPersistSource:

    @pytest.mark.asyncio
    async def test_paper_persist_writes_default_ticket_source(self):
        from backend.api.routes.orders_place import _opp_paper_persist_row

        data = _fake_ticket_data(mode="paper")
        request = MagicMock()
        request.scope = {"state": {"request_id": None}}

        session_factory, _inst, added_rows = _session_mock(assigned_id=301)

        with patch("backend.api.database.async_session", session_factory), \
             patch("backend.api.algo.agent_engine.get_agent_id_by_slug",
                   new=AsyncMock(return_value=1)), \
             patch("backend.api.routes.orders_helpers._resolve_target_pct",
                   return_value=0.0), \
             patch("backend.api.routes.orders_helpers._build_overrides_json",
                   return_value=None):
            row_id = await _opp_paper_persist_row(
                data, request, "ZG0790", "NIFTY25JUL24000CE", "BUY", 50,
            )

        assert row_id == 301
        assert added_rows[0].source == "ticket"

    @pytest.mark.asyncio
    async def test_paper_persist_writes_client_supplied_source(self):
        from backend.api.routes.orders_place import _opp_paper_persist_row

        data = _fake_ticket_data(mode="paper", source="command")
        request = MagicMock()
        request.scope = {"state": {"request_id": None}}

        session_factory, _inst, added_rows = _session_mock(assigned_id=302)

        with patch("backend.api.database.async_session", session_factory), \
             patch("backend.api.algo.agent_engine.get_agent_id_by_slug",
                   new=AsyncMock(return_value=1)), \
             patch("backend.api.routes.orders_helpers._resolve_target_pct",
                   return_value=0.0), \
             patch("backend.api.routes.orders_helpers._build_overrides_json",
                   return_value=None):
            await _opp_paper_persist_row(
                data, request, "ZG0790", "NIFTY25JUL24000CE", "BUY", 50,
            )

        assert added_rows[0].source == "command"


# ─────────────────────────────────────────────────────────────────────────────
# Legacy v1 take-profit shim — _opp_arm_tp_persist_row
# ─────────────────────────────────────────────────────────────────────────────

class TestLegacyTakeProfitSource:

    @pytest.mark.asyncio
    async def test_tp_child_row_gets_fixed_take_profit_source(self):
        from backend.api.routes.orders_place import _opp_arm_tp_persist_row

        mock_parent = MagicMock()
        mock_parent.quantity = 100
        mock_parent.id = 7

        _parent_result = MagicMock()
        _parent_result.scalar_one_or_none.return_value = mock_parent

        _existing_result = MagicMock()
        _existing_result.scalar_one.return_value = 0

        execute_returns = iter([_parent_result, _existing_result])
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(side_effect=lambda *a, **kw: next(execute_returns))

        added_rows: list = []
        mock_session.add = MagicMock(side_effect=lambda row: added_rows.append(row))
        mock_session.commit = AsyncMock()

        with patch("backend.api.database.async_session", return_value=mock_session):
            result = await _opp_arm_tp_persist_row(
                7, "ZG0790", "NIFTY24APR25000CE", "NFO", "BUY",
                100.0, 0.05, None, "live",
            )

        assert result is not None
        assert added_rows[0].source == "take_profit"


# ─────────────────────────────────────────────────────────────────────────────
# Basket LIVE — chase-pre-persist leg
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketLiveChasePrePersistSource:

    @pytest.mark.asyncio
    async def test_chase_eligible_leg_row_gets_basket_source(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT", exchange="NFO",
            transaction_type="BUY", quantity=1, order_type="LIMIT",
            price=22000.0, chase=True,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])
        positions_df = mock_positions_df("TEST001", [])
        session_factory, _inst, added_rows = _session_mock(assigned_id=42)

        patches = _basket_common_patches(
            mock_broker, positions_df, mock_lot_sizes,
            session_factory=session_factory, is_prod_branch=True,
        ) + [
            patch("backend.api.routes.orders_basket._start_live_chase",
                  new=AsyncMock(return_value="CHASE-OID-1")),
            patch("backend.api.routes.orders_basket._ticket_seed_broker_order_id",
                  new=AsyncMock()),
        ]

        result = await _run_with_patches(
            patches, lambda: basket_order_handler(request_data, mock_request)
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "OPEN", leg_result.error
        assert len(added_rows) == 1
        assert added_rows[0].source == "basket"


# ─────────────────────────────────────────────────────────────────────────────
# Basket LIVE — direct broker.place_order leg (non-chase)
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketLiveDirectPlaceSource:

    @pytest.mark.asyncio
    async def test_market_leg_row_gets_basket_source(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="NIFTY25APRFUT", exchange="NFO",
            transaction_type="BUY", quantity=1, order_type="MARKET",
            price=None, chase=True,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])
        positions_df = mock_positions_df("TEST001", [])
        session_factory, _inst, added_rows = _session_mock(assigned_id=99)

        patches = _basket_common_patches(
            mock_broker, positions_df, mock_lot_sizes,
            session_factory=session_factory, is_prod_branch=True,
        )

        result = await _run_with_patches(
            patches, lambda: basket_order_handler(request_data, mock_request)
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "OPEN", leg_result.error
        assert len(added_rows) == 1
        assert added_rows[0].source == "basket"


# ─────────────────────────────────────────────────────────────────────────────
# Basket PAPER leg
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketPaperSource:

    @pytest.mark.asyncio
    async def test_paper_leg_row_gets_basket_source(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        from backend.api.routes.orders_basket import basket_order_handler

        # Equity leg — avoids the F&O lot-resolution path entirely.
        leg = BasketLeg(
            tradingsymbol="SBIN", exchange="NSE",
            transaction_type="BUY", quantity=10, order_type="LIMIT",
            price=500.0, chase=True,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])
        positions_df = mock_positions_df("TEST001", [])
        session_factory, _inst, added_rows = _session_mock(assigned_id=501)

        patches = _basket_common_patches(
            mock_broker, positions_df, mock_lot_sizes,
            session_factory=session_factory,
            is_prod_branch=False,   # non-prod branch → paper regardless of flags
        ) + [
            patch("backend.api.algo.paper.get_prod_paper_engine",
                  return_value=MagicMock(register_open_order=MagicMock())),
            patch("backend.api.algo.agent_engine.get_agent_id_by_slug",
                  new=AsyncMock(return_value=1)),
        ]

        result = await _run_with_patches(
            patches, lambda: basket_order_handler(request_data, mock_request)
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "PAPER", leg_result.error
        assert len(added_rows) == 1
        assert added_rows[0].source == "basket"
        assert added_rows[0].mode == "paper"


# ─────────────────────────────────────────────────────────────────────────────
# Basket SHADOW leg
# ─────────────────────────────────────────────────────────────────────────────

class TestBasketShadowSource:

    @pytest.mark.asyncio
    async def test_shadow_leg_row_gets_basket_source(
        self, mock_request, mock_lot_sizes, mock_positions_df, mock_broker
    ):
        from backend.api.routes.orders_basket import basket_order_handler

        leg = BasketLeg(
            tradingsymbol="SBIN", exchange="NSE",
            transaction_type="BUY", quantity=10, order_type="LIMIT",
            price=500.0, chase=True,
        )
        request_data = BasketOrderRequest(groups=[
            BasketGroup(account="TEST001", legs=[leg])
        ])
        positions_df = mock_positions_df("TEST001", [])
        session_factory, _inst, added_rows = _session_mock(assigned_id=601)

        patches = _basket_common_patches(
            mock_broker, positions_df, mock_lot_sizes,
            session_factory=session_factory,
            is_prod_branch=True, shadow=True,
        )

        result = await _run_with_patches(
            patches, lambda: basket_order_handler(request_data, mock_request)
        )

        leg_result = result.groups[0].results[0]
        assert leg_result.status == "SHADOW", leg_result.error
        assert len(added_rows) == 1
        assert added_rows[0].source == "basket"
        assert added_rows[0].mode == "shadow"
