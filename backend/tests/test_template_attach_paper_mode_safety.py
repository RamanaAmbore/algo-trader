"""
Regression tests for the paper/sim/replay template-attach real-broker-order
safety fix (2026-09-30).

Confirmed root cause: `_fire_template_attach_on_fill`
(backend/api/routes/orders_place.py) hardcoded `apply_path="live"` on every
call, regardless of which caller invoked it. Three of its four callers
(orders_postback.py, chase.py, the reconcile helper in orders_place.py) are
already structurally restricted to `AlgoOrder.mode == "live"` rows. The
fourth caller — `PaperTradeEngine._paper_maybe_fire_template_attach`
(backend/api/algo/paper.py) — is SHARED by all three non-live paper-engine
instances (`label="paper"` for mode-2 Paper, `label="sim"` for mode-1
Simulator, `label="replay"` for mode-4 Replay; see `recover_from_db`'s
`AlgoOrder.mode == self._label` invariant). A templated fill on ANY of
these reached the exact same hardcoded-live branch, calling
`apply_plan_live()` → `get_broker()` → REAL `broker.place_gtt` /
`broker.place_order` against a position that only exists in the paper
simulator.

Fix: `_fire_template_attach_on_fill` now takes a REQUIRED `mode` kwarg (no
default — a default would silently recreate the bug for any future
caller) and refuses to do any broker-touching work unless
`mode.lower() == "live"`. Every caller threads the AlgoOrder row's own
`mode` field through explicitly. A companion gap in the operator-triggered
`/retry-template` endpoint (`orders.py:_retry_precheck_row`) is fixed the
same way — otherwise a paper-mode fill's now-permanent "FILLED + template
attached, no attached_gtts_json" state would look exactly like a failed
attach worth retrying, and retrying it would place real broker GTTs.

Five test dimensions:
  SSOT   — the mode gate is the single chokepoint every caller must pass
  Perf   — non-live fills return before any DB/broker round-trip
  Stale  — old hardcoded apply_path="live" behaviour cannot resurface
           (mode is a required kwarg, no default)
  Reuse  — same gate protects ticket, basket, and agent-driven paper fills
           (all funnel through PaperTradeEngine._update_algo_order)
  UX     — a skipped attach logs a clear reason instead of failing silently
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════
# Layer 1 — direct unit tests of _fire_template_attach_on_fill's mode gate
# ═══════════════════════════════════════════════════════════════════════

class TestFireTemplateAttachModeGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["paper", "sim", "replay", "shadow", "draft", ""])
    async def test_non_live_modes_never_touch_db_or_broker(self, mode):
        """Non-live modes must return before EVEN reading the AlgoOrder row
        — i.e. before any chance of a broker call downstream."""
        from backend.api.routes.orders_place import _fire_template_attach_on_fill

        mock_broker = MagicMock()
        with patch(
            "backend.api.routes.orders_place._opp_load_row_for_attach",
            new=AsyncMock(return_value={"overrides": {}}),
        ) as mock_load, patch(
            "backend.brokers.registry.get_broker", return_value=mock_broker,
        ) as mock_get_broker, patch(
            "backend.api.routes.orders_place._opp_persist_attached_gtts",
            new=AsyncMock(),
        ) as mock_persist:
            await _fire_template_attach_on_fill(
                parent_row_id=1,
                parent_account="ZG0790",
                parent_symbol="NIFTY24APR25000CE",
                parent_exchange="NFO",
                parent_side="SELL",
                parent_qty=50,
                fill_price=100.0,
                template_id=7,
                parent_product="NRML",
                mode=mode,
            )

        mock_load.assert_not_called()
        mock_get_broker.assert_not_called()
        mock_broker.place_gtt.assert_not_called()
        mock_broker.place_order.assert_not_called()
        mock_persist.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_mode_still_reaches_broker(self, monkeypatch):
        """Regression guard — the fix must not break the genuine live
        template-attach feature. mode='live' reaches apply_template_to_order
        with apply_path='live', which routes to a real broker.place_gtt."""
        from backend.api.routes.orders_place import _fire_template_attach_on_fill
        # Template exits are held by default; this test covers the live path, so
        # release the hold for its duration.
        monkeypatch.setattr("backend.api.algo.order_hold_gate.template_exit_held", lambda override=None: False)
        monkeypatch.setattr("backend.api.algo.order_hold_gate.template_exit_override", AsyncMock(return_value=None))

        mock_broker = MagicMock()
        mock_broker.broker_id = "zerodha_kite"
        mock_broker.place_gtt = MagicMock(return_value="gtt-123")
        mock_broker.place_order = MagicMock(return_value="order-456")
        mock_broker.translate_qty.side_effect = lambda exch, qty, ls: qty
        # _ta_live_place_one_gtt now verifies broker-acceptance (a single
        # get_gtts() read-back) before recording the id as placed — this
        # mock must report the placed GTT as accepted, or the fix
        # correctly treats it as never-placed and skips the persist below.
        mock_broker.get_gtts = MagicMock(return_value=[{"id": "gtt-123", "status": "active"}])

        template = {
            "id": 7, "slug": "default-bull", "name": "Default Bull",
            "applies_to": "sell_any",
            "tp_pct": 10.0, "sl_pct": 5.0,
            "wing_premium_pct": None, "wing_strike_offset": None,
            "tp_order_type": "LIMIT", "tp_scales_json": None,
            "sl_trail_pct": None,
        }

        with patch(
            "backend.api.routes.orders_place._opp_load_row_for_attach",
            new=AsyncMock(return_value={"overrides": {}}),
        ), patch(
            "backend.api.routes.orders_place._opp_persist_attached_gtts",
            new=AsyncMock(),
        ) as mock_persist, patch(
            "backend.api.algo.template_attach.load_template_for_slug_or_id",
            new=AsyncMock(return_value=template),
        ), patch(
            "backend.brokers.adapters.kite.get_lot_size",
            new=AsyncMock(return_value=50),
        ), patch(
            "backend.brokers.registry.get_broker", return_value=mock_broker,
        ), patch(
            "backend.api.algo.agent_engine._symbol_exchange_open",
            return_value=True,
        ), patch(
            "backend.api.algo.agent_engine._build_now_ctx", return_value={},
        ):
            await _fire_template_attach_on_fill(
                parent_row_id=1,
                parent_account="ZG0790",
                parent_symbol="NIFTY24APR25000CE",
                parent_exchange="NFO",
                parent_side="SELL",
                parent_qty=50,
                fill_price=100.0,
                template_id=7,
                parent_product="NRML",
                mode="live",
            )

        assert mock_broker.place_gtt.called, (
            "live-mode fill must still place real GTTs via apply_plan_live "
            "— this is a load-bearing feature, not the bug being fixed"
        )
        mock_persist.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# Layer 2 — drives through the real PaperTradeEngine production entry
# point (_update_algo_order) so the test exercises the exact code path a
# live paper/sim/replay fill takes, with no test-only shortcuts on the
# paper.py side. THIS is the test that fails on the pre-fix code: before
# the fix, _update_algo_order → _paper_maybe_fire_template_attach →
# _fire_template_attach_on_fill(apply_path="live") unconditionally, so
# apply_template_to_order gets called (and would reach a real broker)
# regardless of the engine's label.
# ═══════════════════════════════════════════════════════════════════════

def _make_row(mode: str):
    """A lightweight stand-in for the AlgoOrder ORM row — plain attribute
    access/mutation like the real model, no ORM machinery required."""
    return SimpleNamespace(
        id=1,
        account="ZG0790",
        symbol="NIFTY24APR25000CE",
        exchange="NFO",
        transaction_type="SELL",
        quantity=50,
        initial_price=95.0,
        status="OPEN",
        template_id=7,
        parent_order_id=None,
        product="NRML",
        mode=mode,
        attempts=0,
        fill_price=None,
        filled_at=None,
        slippage=None,
        detail=None,
        filled_quantity=None,
        parent_account_original=None,
        attached_gtts_json=None,
        template_overrides_json=None,
    )


class _FakeQuoteSource:
    """Minimal QuoteSource stand-in — engine never calls it in this test."""


def _make_engine(label: str):
    from backend.api.algo.paper import PaperTradeEngine
    return PaperTradeEngine(quote_source=_FakeQuoteSource(), label=label)


async def _drain_pending_tasks(before: set) -> None:
    """Await every asyncio task spawned during the test body (the
    template-attach dispatch runs via asyncio.create_task)."""
    pending = [t for t in asyncio.all_tasks() if t not in before]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class TestPaperEngineFillNeverCallsRealBroker:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", ["paper", "sim", "replay"])
    async def test_fill_never_reaches_apply_template_to_order(self, label):
        row = _make_row(mode=label)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = row
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        engine = _make_engine(label)
        order = {
            "algo_order_id": 1,
            "symbol": "NIFTY24APR25000CE",
            "side": "SELL",
            "qty": 50,
            "fill_price": 100.0,
            "limit_price": 100.0,
            "attempts": 1,
            "agent_slug": "manual-ticket",
            "action_type": "place_order",
        }

        before_tasks = set(asyncio.all_tasks())
        with patch("backend.api.database.async_session", return_value=mock_session), \
             patch("backend.api.algo.order_events.write_event", new=AsyncMock()), \
             patch("backend.api.algo.paper._paper_fanout_terminal", new=MagicMock()), \
             patch("backend.api.algo.paper._paper_audit_terminal", new=MagicMock()), \
             patch(
                 "backend.api.algo.template_attach.apply_template_to_order",
                 new=AsyncMock(),
             ) as mock_apply, \
             patch(
                 "backend.brokers.registry.get_broker", new=MagicMock(),
             ) as mock_get_broker:
            await engine._update_algo_order(order, "fill")
            await _drain_pending_tasks(before_tasks)

        assert row.status == "FILLED"
        assert row.template_id == 7, "sanity: the row genuinely carries a template"
        mock_apply.assert_not_called(), (
            f"PaperTradeEngine[label={label!r}] fill must NEVER reach "
            "apply_template_to_order — that function is the one that can "
            "call a real broker"
        )
        mock_get_broker.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# Layer 3 — mode threading at the other three call sites (postback,
# chase, reconcile) — each already restricted to mode=='live' rows, but
# must now explicitly pass the row's own mode through to the shared
# chokepoint rather than relying on an implicit "this file is live-only"
# assumption.
# ═══════════════════════════════════════════════════════════════════════

class TestModeThreadingAtOtherCallers:
    @pytest.mark.asyncio
    async def test_postback_threads_row_mode(self):
        from backend.api.routes.orders_postback import _pb_dispatch_template_attach

        _r = SimpleNamespace(
            id=9, account="ZG0790", symbol="NIFTY24APR25000CE", exchange="NFO",
            transaction_type="SELL", fill_price=100.0, template_id=7,
            product="NRML", filled_quantity=50, quantity=50, mode="live",
        )
        before_tasks = set(asyncio.all_tasks())
        with patch(
            "backend.api.routes.orders_place._fire_template_attach_on_fill",
            new=AsyncMock(),
        ) as mock_fire:
            _pb_dispatch_template_attach(_r)
            await _drain_pending_tasks(before_tasks)
        assert mock_fire.await_args.kwargs["mode"] == "live"

    @pytest.mark.asyncio
    async def test_chase_threads_snap_mode(self):
        from backend.api.algo import chase as m

        snap = dict(
            id=5, template_id=3, account="ZG0790", symbol="NIFTY24APR25000CE",
            exchange="NFO", transaction_type="SELL", product="NRML",
            mode="live", filled_quantity=50, quantity=50,
            intent="", is_close_intent=False,
        )
        with patch(
            "backend.api.routes.orders_place._is_offsetting_position",
            new=AsyncMock(return_value=False),
        ), patch(
            "backend.api.routes.orders._fire_template_attach_on_fill",
            new=AsyncMock(),
        ) as mock_fire:
            await m._ch_check_and_fire_template_attach(snap, 100.0)
        assert mock_fire.await_args.kwargs["mode"] == "live"

    @pytest.mark.asyncio
    async def test_reconcile_threads_row_mode(self):
        from backend.api.routes.orders_place import _maybe_fire_template_attach_for_reconcile

        row = SimpleNamespace(
            id=11, account="ZG0790", symbol="NIFTY24APR25000CE", exchange="NFO",
            transaction_type="SELL", quantity=50, filled_quantity=50,
            fill_price=100.0, template_id=7, product="NRML", mode="live",
            intent="", is_close_intent=False, parent_order_id=None,
        )
        before_tasks = set(asyncio.all_tasks())
        with patch(
            "backend.api.routes.orders_place._fire_template_attach_on_fill",
            new=AsyncMock(),
        ) as mock_fire:
            _maybe_fire_template_attach_for_reconcile(row)
            await _drain_pending_tasks(before_tasks)
        assert mock_fire.await_args.kwargs["mode"] == "live"


# ═══════════════════════════════════════════════════════════════════════
# Layer 4 — companion fix: the operator-triggered /retry-template endpoint
# must refuse to retry-attach a non-live/non-sim row, since after this fix
# a "FILLED templated paper fill with no attached_gtts_json" is a genuine,
# permanent, SAFE resting state — not a failed attach.
# ═══════════════════════════════════════════════════════════════════════

class TestRetryTemplatePrecheckRefusesNonLiveModes:
    def _row(self, mode: str):
        return SimpleNamespace(
            template_id=7, attached_gtts_json=None, status="FILLED", mode=mode,
        )

    @pytest.mark.parametrize("mode", ["paper", "replay", "shadow", None, ""])
    def test_non_live_non_sim_modes_are_refused(self, mode):
        from backend.api.routes.orders import _retry_precheck_row
        result = _retry_precheck_row(self._row(mode))
        assert result is not None
        assert result["ok"] is False
        assert "mode" in result["reason"]

    @pytest.mark.parametrize("mode", ["live", "sim"])
    def test_live_and_sim_modes_pass_precheck(self, mode):
        from backend.api.routes.orders import _retry_precheck_row
        result = _retry_precheck_row(self._row(mode))
        assert result is None
