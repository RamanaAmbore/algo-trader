"""
Sprint 8 (CLI/text grammar agent-authoring effort, step 2 investigation)
— confirmed and fixed a pre-existing gap: a `/api/orders/ticket` request
carrying ONLY ad-hoc override fields (`tp_pct_override`, `sl_pct_override`,
`wing_premium_pct_override`, `wing_strike_offset_override`, ...) and no
`template_id`/`template_slug` never actually armed TP/SL/Wing exits on a
real fill, despite `template_attach.build_adhoc_template` /
`apply_template_to_order` already supporting exactly this case (see
test_apply_template_to_order.py::test_apply_template_to_order_no_template_but_has_overrides,
which passes on both the old and new code — the RESOLVER layer always
worked).

Root cause: ALL FOUR real fill-detection dispatch sites gated on
`template_id` being truthy BEFORE ever considering whether ad-hoc
override fields were present, so `apply_template_to_order`'s ad-hoc
path was structurally unreachable from every live code path:

  1. `orders_postback._pb_wants_template_attach`  (Kite/Dhan/Groww postback)
  2. `chase._ch_maybe_fire_template_attach`        (chase terminal-fill)
  3. `paper.PaperTradeEngine._paper_maybe_fire_template_attach` (paper/sim/replay —
      itself a no-op for real broker calls via the `mode` gate, but still
      gated the SAME way structurally)
  4. `orders_place._opl_reconcile_attach_eligible` (admin reconcile sweep)

Fix: each gate now fires when EITHER `template_id` is set OR the row's
`template_overrides_json` is non-empty (ad-hoc overrides present), and
every corresponding `template_id=int(x.template_id)` cast — which would
raise `TypeError: int() argument must be ... not 'NoneType'` for an
ad-hoc-only row — was changed to a conditional that passes `None`
through safely (`apply_template_to_order` already handles `template_id=
None` correctly).

A secondary double-arm risk was also closed: `orders_postback.
_pb_wants_take_profit_arm` and `chase._ch_maybe_fire_auto_tp` (the
legacy v1 single-target-TP shims, which fire when `target_pct`/
`target_abs` is set and `template_id is None`) now ALSO require
`template_overrides_json` to be empty — otherwise a row carrying both a
legacy `target_pct` AND a v2 override field could get double-armed by
both the legacy shim and the new ad-hoc template-attach path.

Five quality dimensions:
  SSOT   — tests the real gate functions (`_pb_wants_template_attach`,
           `_ch_maybe_fire_template_attach`, `_opl_reconcile_attach_eligible`,
           `_paper_maybe_fire_template_attach`), not reimplementations.
  Perf   — n/a.
  Stale  — reproduces the exact "ad-hoc ticket, no template_id" scenario
           the task was asked to trace end-to-end; proves it was
           structurally unreachable before this fix (asserts the
           int(None) TypeError the old code would have raised is gone).
  Reuse  — same MagicMock-row-fixture pattern already used in
           test_template_findings.py / test_filled_quantity_fix.py for
           the postback+reconcile gates; same SimpleNamespace + mocked-
           session pattern already used in
           test_template_attach_paper_mode_safety.py for the paper engine.
  UX     — n/a (backend-only).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Shared row/snap factories ─────────────────────────────────────────────

def _pb_row(template_id=None, template_overrides_json=None, target_pct=None,
            target_abs=None, qty=50, filled=50, fill_price=100.0,
            mode="live", parent_order_id=None, intent=None, is_close_intent=False):
    r = MagicMock()
    r.id = 1
    r.quantity = qty
    r.filled_quantity = filled
    r.template_id = template_id
    r.template_overrides_json = template_overrides_json
    r.target_pct = target_pct
    r.target_abs = target_abs
    r.fill_price = fill_price
    r.mode = mode
    r.parent_order_id = parent_order_id
    r.intent = intent
    r.is_close_intent = is_close_intent
    return r


def _chase_snap(template_id=None, template_overrides_json=None, target_pct=None,
                 target_abs=None, parent_order_id=None):
    return dict(
        id=5, target_pct=target_pct, target_abs=target_abs,
        parent_order_id=parent_order_id, template_id=template_id,
        template_overrides_json=template_overrides_json,
        account="ZG0790", symbol="NIFTY24APR25000CE", exchange="NFO",
        transaction_type="SELL", product="NRML", mode="live",
        filled_quantity=50, quantity=50, intent="", is_close_intent=False,
    )


# ── 1. orders_postback._pb_wants_template_attach ──────────────────────────

class TestPostbackAdhocDispatch:
    def test_adhoc_overrides_no_template_id_now_dispatches(self):
        from backend.api.routes.orders_postback import _pb_wants_template_attach
        row = _pb_row(template_id=None, template_overrides_json='{"tp_pct": 20.0}')
        assert _pb_wants_template_attach(row) is True

    def test_no_template_id_and_no_overrides_still_skips(self):
        from backend.api.routes.orders_postback import _pb_wants_template_attach
        row = _pb_row(template_id=None, template_overrides_json=None)
        assert _pb_wants_template_attach(row) is False

    def test_dispatch_passes_template_id_none_without_raising(self):
        """Pre-fix this called `int(_r.template_id)` unconditionally —
        `int(None)` raises TypeError. Confirms the dispatch wrapper now
        passes `template_id=None` straight through to
        `_fire_template_attach_on_fill` for an ad-hoc-only row."""
        from backend.api.routes.orders_postback import _pb_dispatch_template_attach
        row = _pb_row(template_id=None, template_overrides_json='{"sl_pct": 5.0}')

        with patch(
            "backend.api.routes.orders_place._fire_template_attach_on_fill",
            new=AsyncMock(),
        ) as mock_fire, patch("asyncio.create_task") as mock_create_task:
            _pb_dispatch_template_attach(row)
            # asyncio.create_task was called with the coroutine — extract
            # and run it synchronously to inspect the kwargs passed through.
            assert mock_create_task.called
            coro = mock_create_task.call_args[0][0]
            asyncio.run(coro)

        assert mock_fire.called
        assert mock_fire.call_args.kwargs["template_id"] is None

    def test_legacy_tp_shim_does_not_double_arm_with_adhoc_overrides(self):
        """Secondary fix: a row with both a legacy target_pct AND v2
        override fields must not ALSO fire the legacy single-TP shim —
        only the ad-hoc template-attach path should fire."""
        from backend.api.routes.orders_postback import _pb_wants_take_profit_arm
        row = _pb_row(
            template_id=None, template_overrides_json='{"sl_pct": 5.0}',
            target_pct=0.30,
        )
        assert _pb_wants_take_profit_arm(row) is False

    def test_legacy_tp_shim_still_fires_for_pure_legacy_rows(self):
        """Regression guard: a PURE legacy row (target_pct set, no
        template_overrides_json at all) must still fire the legacy shim
        exactly as before this fix."""
        from backend.api.routes.orders_postback import _pb_wants_take_profit_arm
        row = _pb_row(
            template_id=None, template_overrides_json=None, target_pct=0.30,
        )
        assert _pb_wants_take_profit_arm(row) is True


# ── 2. chase._ch_maybe_fire_template_attach ───────────────────────────────

class TestChaseAdhocDispatch:
    def test_adhoc_overrides_no_template_id_now_dispatches(self):
        from backend.api.algo.chase import _ch_maybe_fire_template_attach
        snap = _chase_snap(template_id=None, template_overrides_json='{"tp_pct": 15.0}')
        with patch(
            "backend.api.algo.chase._ch_check_and_fire_template_attach",
            new=AsyncMock(),
        ) as mock_check, patch("asyncio.create_task") as mock_create_task:
            _ch_maybe_fire_template_attach(snap, 100.0)
            assert mock_create_task.called

    def test_no_template_id_and_no_overrides_still_skips(self):
        from backend.api.algo.chase import _ch_maybe_fire_template_attach
        snap = _chase_snap(template_id=None, template_overrides_json=None)
        with patch("asyncio.create_task") as mock_create_task:
            _ch_maybe_fire_template_attach(snap, 100.0)
            assert not mock_create_task.called

    @pytest.mark.asyncio
    async def test_check_and_fire_passes_template_id_none_without_raising(self):
        """Pre-fix `_ch_check_and_fire_template_attach` called
        `int(snap["template_id"])` unconditionally. Confirms it now
        passes `template_id=None` through for an ad-hoc-only snap."""
        from backend.api.algo.chase import _ch_check_and_fire_template_attach
        snap = _chase_snap(template_id=None, template_overrides_json='{"sl_pct": 5.0}')

        with patch(
            "backend.api.routes.orders_place._is_offsetting_position",
            new=AsyncMock(return_value=False),
        ), patch(
            "backend.api.routes.orders._fire_template_attach_on_fill",
            new=AsyncMock(),
        ) as mock_fire:
            await _ch_check_and_fire_template_attach(snap, 100.0)

        assert mock_fire.called
        assert mock_fire.call_args.kwargs["template_id"] is None

    def test_legacy_auto_tp_does_not_double_arm_with_adhoc_overrides(self):
        from backend.api.algo.chase import _ch_maybe_fire_auto_tp
        snap = _chase_snap(
            template_id=None, template_overrides_json='{"sl_pct": 5.0}',
            target_pct=0.30,
        )
        with patch("asyncio.create_task") as mock_create_task:
            _ch_maybe_fire_auto_tp(snap, 100.0)
            assert not mock_create_task.called

    def test_legacy_auto_tp_still_fires_for_pure_legacy_snaps(self):
        from backend.api.algo.chase import _ch_maybe_fire_auto_tp
        snap = _chase_snap(
            template_id=None, template_overrides_json=None, target_pct=0.30,
        )
        with patch("asyncio.create_task") as mock_create_task:
            _ch_maybe_fire_auto_tp(snap, 100.0)
            assert mock_create_task.called

    def test_snapshot_captures_template_overrides_json(self):
        """`_chase_snapshot_algo_row` must carry `template_overrides_json`
        through to the snap dict so the dispatch gate above can see it —
        uses getattr() so a test double lacking the attribute entirely
        doesn't raise (defensive, mirrors agent_id/is_close_intent)."""
        from backend.api.algo.chase import _chase_snapshot_algo_row
        row = SimpleNamespace(
            id=1, target_pct=None, target_abs=None, parent_order_id=None,
            template_id=None, account="A", symbol="S", exchange="NFO",
            transaction_type="BUY", product="NRML", mode="live",
            filled_quantity=0, quantity=10, intent="",
            template_overrides_json='{"tp_pct": 10.0}',
        )
        snap = _chase_snapshot_algo_row(row, "bo-1")
        assert snap["template_overrides_json"] == '{"tp_pct": 10.0}'

    def test_snapshot_defensive_against_missing_attribute(self):
        """A row/double with no `template_overrides_json` attribute at
        all must not raise — getattr default None."""
        from backend.api.algo.chase import _chase_snapshot_algo_row
        row = SimpleNamespace(
            id=1, target_pct=None, target_abs=None, parent_order_id=None,
            template_id=None, account="A", symbol="S", exchange="NFO",
            transaction_type="BUY", product="NRML", mode="live",
            filled_quantity=0, quantity=10, intent="",
        )
        snap = _chase_snapshot_algo_row(row, "bo-1")
        assert snap["template_overrides_json"] is None


# ── 3. orders_place._opl_reconcile_attach_eligible ────────────────────────

class TestReconcileAdhocDispatch:
    def test_adhoc_overrides_no_template_id_now_eligible(self):
        from backend.api.routes.orders_place import _opl_reconcile_attach_eligible
        row = _pb_row(template_id=None, template_overrides_json='{"tp_pct": 20.0}')
        assert _opl_reconcile_attach_eligible(row) is True

    def test_no_template_id_and_no_overrides_still_ineligible(self):
        from backend.api.routes.orders_place import _opl_reconcile_attach_eligible
        row = _pb_row(template_id=None, template_overrides_json=None)
        assert _opl_reconcile_attach_eligible(row) is False

    def test_dispatch_passes_template_id_none_without_raising(self):
        from backend.api.routes.orders_place import (
            _maybe_fire_template_attach_for_reconcile,
        )
        row = _pb_row(template_id=None, template_overrides_json='{"sl_pct": 5.0}')

        with patch(
            "backend.api.routes.orders_place._fire_template_attach_on_fill",
            new=AsyncMock(),
        ) as mock_fire, patch(
            "backend.api.routes.orders_place.asyncio"
        ) as mock_asyncio:
            _maybe_fire_template_attach_for_reconcile(row)
            assert mock_asyncio.create_task.called
            # Run the coroutine passed to create_task synchronously.
            coro = mock_asyncio.create_task.call_args[0][0]
            asyncio.run(coro)

        assert mock_fire.called
        assert mock_fire.call_args.kwargs["template_id"] is None


# ── 4. paper.PaperTradeEngine._paper_maybe_fire_template_attach ───────────

class _FakeQuoteSource:
    """Minimal QuoteSource stand-in — engine never calls it in this test."""


class TestPaperEngineAdhocDispatch:
    def _make_engine(self):
        from backend.api.algo.paper import PaperTradeEngine
        return PaperTradeEngine(quote_source=_FakeQuoteSource(), label="paper")

    @pytest.mark.asyncio
    async def test_adhoc_overrides_no_template_id_now_dispatches(self):
        engine = self._make_engine()
        order = {"algo_order_id": 1, "fill_price": 100.0}

        with patch(
            "backend.api.routes.orders._fire_template_attach_on_fill",
            new=AsyncMock(),
        ) as mock_fire:
            before = set(asyncio.all_tasks())
            engine._paper_maybe_fire_template_attach(
                order, None, None, "ZG0790", "NIFTY24APR25000CE", "NFO",
                "SELL", 50, "NRML", "paper",
                template_overrides_json='{"tp_pct": 15.0}',
            )
            pending = [t for t in asyncio.all_tasks() if t not in before]
            await asyncio.gather(*pending, return_exceptions=True)

        assert mock_fire.called
        assert mock_fire.call_args.kwargs["template_id"] is None

    @pytest.mark.asyncio
    async def test_no_template_id_and_no_overrides_still_skips(self):
        engine = self._make_engine()
        order = {"algo_order_id": 1, "fill_price": 100.0}

        with patch(
            "backend.api.routes.orders._fire_template_attach_on_fill",
            new=AsyncMock(),
        ) as mock_fire:
            before = set(asyncio.all_tasks())
            engine._paper_maybe_fire_template_attach(
                order, None, None, "ZG0790", "NIFTY24APR25000CE", "NFO",
                "SELL", 50, "NRML", "paper",
                template_overrides_json=None,
            )
            pending = [t for t in asyncio.all_tasks() if t not in before]
            await asyncio.gather(*pending, return_exceptions=True)

        assert not mock_fire.called

    @pytest.mark.asyncio
    async def test_saved_template_id_still_dispatches_as_before(self):
        """Regression guard: the existing saved-template path (template_id
        set, no override JSON) must be completely unaffected."""
        engine = self._make_engine()
        order = {"algo_order_id": 1, "fill_price": 100.0}

        with patch(
            "backend.api.routes.orders._fire_template_attach_on_fill",
            new=AsyncMock(),
        ) as mock_fire:
            before = set(asyncio.all_tasks())
            engine._paper_maybe_fire_template_attach(
                order, 7, None, "ZG0790", "NIFTY24APR25000CE", "NFO",
                "SELL", 50, "NRML", "paper",
            )
            pending = [t for t in asyncio.all_tasks() if t not in before]
            await asyncio.gather(*pending, return_exceptions=True)

        assert mock_fire.called
        assert mock_fire.call_args.kwargs["template_id"] == 7


# ── End-to-end resolver confirmation (already-passing baseline) ──────────
# Confirms the RESOLVER layer this fix now makes REACHABLE actually
# arms a real GTT for the override-only, no-template_id case — the
# "did it work end-to-end" question from the task brief, answered via
# the real apply_template_to_order() call the fixed gates now reach.

class TestAdhocOverrideResolverEndToEnd:
    @pytest.mark.asyncio
    async def test_tp_and_sl_overrides_with_no_template_id_arm_real_gtts(self):
        from backend.api.algo.template_attach import apply_template_to_order

        result = await apply_template_to_order(
            template_id=None,
            template_slug=None,
            overrides={"tp_pct": 20.0, "sl_pct": 10.0},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

        assert result is not None
        assert not result.errors
        assert len(result.plan.gtts) == 1
        assert result.plan.gtts[0].trigger_type == "two-leg"


# ── tp_order_type_override API-boundary gap (follow-up to Sprint 8) ──────
#
# A parallel frontend agent found that TicketOrderRequest had no
# tp_order_type_override field at all, even though the RESOLVER layer
# (`_parse_template_overrides` in template_attach.py) already supported
# a "tp_order_type" key in the overrides dict — the gap was purely at
# the API boundary: no field existed to carry the operator's override
# from the request into that dict. Fixed by adding the field to
# TicketOrderRequest + TicketPreviewRequest (schemas.py) and threading
# it through both places `tp_pct_override` is threaded:
#   - `_ticket_overrides_dict` (feeds /ticket/preview + the paper-ticket
#     resolver preview call — `_maybe_attach_template_to_ticket`)
#   - `_build_overrides_json` (feeds the REAL fill path — persisted on
#     AlgoOrder.template_overrides_json, re-parsed by the postback /
#     chase / paper-engine / reconcile dispatch sites tested above)
class TestTpOrderTypeOverrideApiGap:
    @staticmethod
    def _req(**kw):
        from backend.api.schemas import TicketOrderRequest
        base = dict(
            mode="live", side="BUY", tradingsymbol="RELIANCE",
            quantity=10, exchange="NSE",
        )
        base.update(kw)
        return TicketOrderRequest(**base)

    @pytest.mark.asyncio
    async def test_preview_path_honours_tp_order_type_override(self):
        """_ticket_overrides_dict → apply_template_to_order(preview):
        tp_order_type_override='MARKET' produces a MARKET TP leg."""
        from backend.api.algo.template_attach import apply_template_to_order
        from backend.api.routes.orders_helpers import _ticket_overrides_dict

        req = self._req(tp_pct_override=20.0, tp_order_type_override="MARKET")
        result = await apply_template_to_order(
            template_id=None,
            template_slug=None,
            overrides=_ticket_overrides_dict(req),
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

        assert result is not None
        assert not result.errors
        assert len(result.plan.gtts) == 1
        assert result.plan.gtts[0].orders[0]["order_type"] == "MARKET"

    @pytest.mark.asyncio
    async def test_preview_path_defaults_to_limit_without_override(self):
        """Control — same request minus tp_order_type_override → LIMIT."""
        from backend.api.algo.template_attach import apply_template_to_order
        from backend.api.routes.orders_helpers import _ticket_overrides_dict

        req = self._req(tp_pct_override=20.0)
        result = await apply_template_to_order(
            template_id=None,
            template_slug=None,
            overrides=_ticket_overrides_dict(req),
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

        assert result is not None
        assert result.plan.gtts[0].orders[0]["order_type"] == "LIMIT"

    @pytest.mark.asyncio
    async def test_real_fill_path_honours_tp_order_type_override(self):
        """_build_overrides_json → json.loads → apply_template_to_order:
        the REAL fill-detection path (postback/chase/paper/reconcile all
        rebuild `overrides` this way from AlgoOrder.template_overrides_json)
        must produce the SAME MARKET TP leg the preview showed — not just
        the preview path."""
        import json
        from backend.api.algo.template_attach import apply_template_to_order
        from backend.api.routes.orders_helpers import _build_overrides_json

        req = self._req(tp_pct_override=20.0, tp_order_type_override="MARKET")
        overrides_json = _build_overrides_json(req)
        assert overrides_json is not None
        overrides = json.loads(overrides_json)
        assert overrides["tp_order_type"] == "MARKET"

        result = await apply_template_to_order(
            template_id=None,
            template_slug=None,
            overrides=overrides,
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

        assert result is not None
        assert not result.errors
        assert result.plan.gtts[0].orders[0]["order_type"] == "MARKET"

    def test_tp_order_type_alone_does_not_persist_as_an_override(self):
        """Regression guard: tp_order_type_override with NO tp_pct_override
        (and no other TP/SL/wing override) must NOT make
        template_overrides_json non-empty — tp_order_type only ever
        MODIFIES an existing TP leg, it never creates one
        (`has_any_override` deliberately excludes it). Without this
        guard, a ticket combining the legacy v1 `target_pct` field with
        only tp_order_type_override would persist a non-empty override
        JSON that (a) suppresses the legacy single-TP shim and (b) the
        ad-hoc path still builds nothing for — silently dropping the
        take-profit that would otherwise have armed at `target_pct`."""
        from backend.api.routes.orders_helpers import _build_overrides_json

        req = self._req(tp_order_type_override="MARKET")
        assert _build_overrides_json(req) is None

    def test_tp_order_type_with_tp_pct_does_persist(self):
        """Sanity complement to the guard above — once a real TP-defining
        override is present too, tp_order_type DOES persist alongside it."""
        import json
        from backend.api.routes.orders_helpers import _build_overrides_json

        req = self._req(tp_pct_override=20.0, tp_order_type_override="MARKET")
        payload = json.loads(_build_overrides_json(req))
        assert payload["tp_order_type"] == "MARKET"
        assert payload["tp_pct"] == 20.0

    def test_legacy_target_pct_shim_not_suppressed_by_tp_order_type_alone(self):
        """End-to-end of the guard above through the real postback gate:
        a row with legacy target_pct + ONLY tp_order_type_override must
        still fire the legacy single-TP shim (template_overrides_json
        stays None), exactly as if tp_order_type_override had never been
        sent."""
        from backend.api.routes.orders_postback import (
            _pb_wants_take_profit_arm,
            _pb_wants_template_attach,
        )
        from backend.api.routes.orders_helpers import _build_overrides_json

        req = self._req(tp_order_type_override="MARKET")
        row = _pb_row(
            template_id=None,
            template_overrides_json=_build_overrides_json(req),
            target_pct=0.30,
        )

        assert _pb_wants_take_profit_arm(row) is True
        assert _pb_wants_template_attach(row) is False
