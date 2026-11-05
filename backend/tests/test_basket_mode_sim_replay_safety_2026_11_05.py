"""test_basket_mode_sim_replay_safety_2026_11_05.py

CRITICAL money-safety audit finding (2026-11-05): a Chain basket
submitted while the navbar showed SIM or REPLAY placed real LIVE broker
orders on prod. `basket_order_handler`'s `eff_mode` was computed ENTIRELY
from server-side settings (execution.paper_trading_mode /
execution.shadow_mode / branch) — it never looked at the client's own
resolved mode, because `BasketOrderRequest` had no field to carry it at
all (msgspec silently dropped whatever `mode` the frontend sent).

Fix: `BasketOrderRequest.mode` (new field) + `_bk_resolve_effective_mode()`
(extracted pure function) — a client-supplied mode other than the
literal string "live" can only ADD a reason to go paper, never override
the server's own force-to-paper conditions, and never escalate to live.

Covers:
  1. `_bk_resolve_effective_mode` — every precedence branch, pure unit
     tests, no mocking.
  2. `basket_order_handler` end-to-end — `data.mode="sim"` on a
     prod-branch request with paper_trading_mode OFF must never reach
     `broker.place_order` (the exact live-money-risk scenario).

Frontend wiring (`_resolveBasketMode` normalising sim/replay/shadow to
'paper' in SymbolPanel.svelte, `placeBasket()` threading `mode` onto the
top-level request body in api.js, consoleDispatch.js's basket branch,
and the derivatives page's own separate basket path reading
`$executionMode` before ever attempting live) is covered by
frontend/e2e/basket_mode_sim_replay_safety.spec.js — source-grep
regression guards, same precedent as
order_ticket_draft_submit_label_and_chase_hidden.spec.js.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.routes.orders_basket import _bk_resolve_effective_mode


# ─────────────────────────────────────────────────────────────────────────
# 1. _bk_resolve_effective_mode — pure function, no mocking needed
# ─────────────────────────────────────────────────────────────────────────

class TestBkResolveEffectiveMode:
    def test_shadow_wins_over_everything(self):
        assert _bk_resolve_effective_mode(
            "live", paper_trading_mode=False, shadow_on=True, is_prod=True,
        ) == "shadow"
        assert _bk_resolve_effective_mode(
            None, paper_trading_mode=True, shadow_on=True, is_prod=False,
        ) == "shadow"

    def test_paper_trading_mode_forces_paper_even_when_client_says_live(self):
        assert _bk_resolve_effective_mode(
            "live", paper_trading_mode=True, shadow_on=False, is_prod=True,
        ) == "paper"

    def test_non_prod_branch_forces_paper_even_when_client_says_live(self):
        assert _bk_resolve_effective_mode(
            "live", paper_trading_mode=False, shadow_on=False, is_prod=False,
        ) == "paper"

    def test_default_no_client_mode_goes_live_on_prod_with_ptm_off(self):
        """Baseline — unchanged legacy behaviour for a caller that never
        sends `mode` at all (an older client, or a direct API caller)."""
        assert _bk_resolve_effective_mode(
            None, paper_trading_mode=False, shadow_on=False, is_prod=True,
        ) == "live"
        assert _bk_resolve_effective_mode(
            "", paper_trading_mode=False, shadow_on=False, is_prod=True,
        ) == "live"

    def test_client_mode_live_goes_live_on_prod_with_ptm_off(self):
        assert _bk_resolve_effective_mode(
            "live", paper_trading_mode=False, shadow_on=False, is_prod=True,
        ) == "live"

    @pytest.mark.parametrize("client_mode", ["sim", "replay", "shadow", "paper", "SIM", " Sim "])
    def test_any_non_live_client_mode_forces_paper_on_prod_with_ptm_off(self, client_mode):
        """THE core fix: this is the exact scenario a Chain basket hits
        while the navbar shows SIM/REPLAY on prod with paper_trading_mode
        off. Before the fix there was no `client_mode` parameter at all,
        and this would have gone 'live' — a real broker order placed
        while the operator believed they were in SIM."""
        assert _bk_resolve_effective_mode(
            client_mode, paper_trading_mode=False, shadow_on=False, is_prod=True,
        ) == "paper"

    def test_garbage_client_mode_also_fails_safe_to_paper(self):
        """Defense in depth — any value that isn't the literal 'live'
        string is treated as 'please don't go live', including a value
        that doesn't correspond to any known mode at all (a future
        client bug, or a malformed request)."""
        assert _bk_resolve_effective_mode(
            "totally-unknown-xyz", paper_trading_mode=False, shadow_on=False, is_prod=True,
        ) == "paper"


# ─────────────────────────────────────────────────────────────────────────
# 2. basket_order_handler end-to-end — the exact live-money-risk scenario
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_request():
    req = MagicMock()
    req.state = SimpleNamespace(is_demo=False)
    return req


@pytest.fixture
def mock_broker():
    broker = MagicMock()
    broker.place_order = AsyncMock(return_value="OID1")
    broker.translate_qty = MagicMock(side_effect=lambda exch, qty, ls: qty)
    broker.basket_order_margins = MagicMock(return_value=[])
    return broker


class _FakeAlgoOrderSession:
    """Minimal async-context-manager session stand-in for the real DB.

    `basket_order_handler`'s pre-persist step (2026-11-05 fix) relies on
    the real SQLAlchemy session assigning a real autoincrement `id` to
    a freshly-added `AlgoOrder` row on `commit()` — a bare, unconfigured
    `MagicMock()` session leaves `.id` at its model-default `None`
    forever, so the handler's own `if _pre_aid is None: ... continue`
    fail-closed guard refuses the leg before it ever reaches
    `broker.place_order`. This fake mimics just that one DB behaviour.
    """

    _next_id = 1000

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = _FakeAlgoOrderSession._next_id
            _FakeAlgoOrderSession._next_id += 1

    async def commit(self):
        pass

    async def execute(self, stmt):
        return MagicMock()


class TestBasketHandlerHonoursClientSimReplayMode:
    @pytest.mark.asyncio
    async def test_client_mode_sim_never_reaches_broker_place_order(
        self, mock_request, mock_broker,
    ):
        """THE regression guard for the live incident: a Chain basket
        submitted with mode='sim' (the navbar showing SIM) on a prod
        request with paper_trading_mode OFF must NEVER place a real
        broker order."""
        from backend.api.routes.orders_basket import basket_order_handler
        from backend.api.schemas import BasketOrderRequest, BasketGroup, BasketLeg

        leg = BasketLeg(
            tradingsymbol="RELIANCE", exchange="NSE",
            transaction_type="BUY", quantity=1, order_type="MARKET",
        )
        data = BasketOrderRequest(
            groups=[BasketGroup(account="TEST001", legs=[leg])],
            mode="sim",
        )

        async def _fake_get_lot_size(exch, sym):
            return 1

        with patch("backend.api.routes.orders_basket.is_admin_request", return_value=True), \
             patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]), \
             patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0), \
             patch("backend.brokers.adapters.kite.get_lot_size", new=_fake_get_lot_size), \
             patch("backend.api.algo.actions.run_preflight", new=AsyncMock(return_value={"ok": True, "blocked": []})), \
             patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
             patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.api.database.async_session"), \
             patch("backend.api.routes.orders_basket._attach_basket_leg_template"):
            await basket_order_handler(data, mock_request)

        mock_broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_client_mode_live_still_reaches_broker_place_order_when_server_allows_it(
        self, mock_request, mock_broker,
    ):
        """Baseline — confirms the fix didn't break the genuine LIVE
        path: mode='live' on a prod request with paper_trading_mode off
        must still place the real order, same as before this fix."""
        from backend.api.routes.orders_basket import basket_order_handler
        from backend.api.schemas import BasketOrderRequest, BasketGroup, BasketLeg

        leg = BasketLeg(
            tradingsymbol="RELIANCE", exchange="NSE",
            transaction_type="BUY", quantity=1, order_type="MARKET",
        )
        data = BasketOrderRequest(
            groups=[BasketGroup(account="TEST001", legs=[leg])],
            mode="live",
        )

        async def _fake_get_lot_size(exch, sym):
            return 1

        with patch("backend.api.routes.orders_basket.is_admin_request", return_value=True), \
             patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]), \
             patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0), \
             patch("backend.brokers.adapters.kite.get_lot_size", new=_fake_get_lot_size), \
             patch("backend.api.algo.actions.run_preflight", new=AsyncMock(return_value={"ok": True, "blocked": []})), \
             patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
             patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.api.database.async_session", side_effect=lambda: _FakeAlgoOrderSession()), \
             patch("backend.api.routes.orders_basket._attach_basket_leg_template"):
            await basket_order_handler(data, mock_request)

        mock_broker.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_mode_field_at_all_preserves_legacy_live_behaviour(
        self, mock_request, mock_broker,
    ):
        """Baseline — an older client (or a direct API caller) that
        never sends `mode` at all must behave exactly as before this
        fix: live on prod with paper_trading_mode off."""
        from backend.api.routes.orders_basket import basket_order_handler
        from backend.api.schemas import BasketOrderRequest, BasketGroup, BasketLeg

        leg = BasketLeg(
            tradingsymbol="RELIANCE", exchange="NSE",
            transaction_type="BUY", quantity=1, order_type="MARKET",
        )
        data = BasketOrderRequest(groups=[BasketGroup(account="TEST001", legs=[leg])])
        assert data.mode is None

        async def _fake_get_lot_size(exch, sym):
            return 1

        with patch("backend.api.routes.orders_basket.is_admin_request", return_value=True), \
             patch("backend.api.routes.orders_basket._broker_for", return_value=mock_broker), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["TEST001"]), \
             patch("backend.api.routes.orders_basket._resolve_target_pct", return_value=0.0), \
             patch("backend.brokers.adapters.kite.get_lot_size", new=_fake_get_lot_size), \
             patch("backend.api.algo.actions.run_preflight", new=AsyncMock(return_value={"ok": True, "blocked": []})), \
             patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True), \
             patch("backend.api.algo.agent_engine._build_now_ctx", return_value=MagicMock()), \
             patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.api.database.async_session", side_effect=lambda: _FakeAlgoOrderSession()), \
             patch("backend.api.routes.orders_basket._attach_basket_leg_template"):
            await basket_order_handler(data, mock_request)

        mock_broker.place_order.assert_called_once()
