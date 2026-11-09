"""
Originally: Sprint 8 fixed a gap where the four real fill-detection
dispatch sites (postback, chase, paper engine, reconcile) gated on
template_id being truthy before considering ad-hoc override fields, so
apply_template_to_order's ad-hoc path was structurally unreachable.

Bracket/Template-GTT removal (T3.1, 2026-11) deleted all four dispatch
gates (_pb_wants_template_attach, _ch_maybe_fire_template_attach,
_opl_reconcile_attach_eligible, PaperTradeEngine._paper_maybe_fire_
template_attach) along with backend.api.algo.template_attach itself —
their test coverage is removed with them. What survives, unaffected by
that removal:
  - The legacy v1 single-target-TP shims (_pb_wants_take_profit_arm,
    _ch_maybe_fire_auto_tp) and their own double-arm exclusion check
    against template_overrides_json.
  - _chase_snapshot_algo_row's snapshot of template_overrides_json
    (feeds the auto-TP shim's exclusion check above).

Bracket/Template-GTT removal, T3.2 (2026-11): _build_overrides_json
(orders_helpers.py) — the override-JSON serializer this file's own
TestTpOrderTypeOverridePersistence class used to cover, including the
tp_order_type-alone-does-not-persist guard — was itself deleted in this
round, along with the override fields it read from TicketOrderRequest.
That class is removed below with it.
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

class TestLegacyTpShimNotDoubleArmed:
    """orders_postback._pb_wants_take_profit_arm — generic legacy v1
    single-target-TP shim, unaffected by the Bracket/Template-GTT removal."""

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


class TestChaseLegacyAutoTpAndSnapshot:
    """chase._ch_maybe_fire_auto_tp (generic legacy v1 shim) and
    _chase_snapshot_algo_row (feeds its exclusion check) — both
    unaffected by the Bracket/Template-GTT removal."""

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

# `TestTpOrderTypeOverridePersistence` removed (Bracket/Template-GTT
# removal, T3.2, 2026-11) — it covered `orders_helpers._build_overrides_
# json` and `TicketOrderRequest.tp_order_type_override`/`tp_pct_override`,
# both deleted in this round.

