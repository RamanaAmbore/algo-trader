"""
Tests for tick-size snapping in the template-attach TP/SL + LIMIT-offset
price math (backend/api/algo/template_attach.py).

Proactive hardening fix (2026-10): `_tp_trigger`, `_sl_trigger`, and
`_tp_limit_offset` previously only did `round(x, 2)` — never snapped to the
instrument's actual tick grid — so a TP/SL/limit price computed from a
percentage or fixed offset could land off-grid and be rejected outright by
Kite. This mirrors the bug CLASS already fixed once in the ticket/basket
order path (commit 4c61e47f), but was never applied to the template-attach
GTT chain at all.

Five test dimensions:
  SSOT   — snap behavior matches the tick grid exactly (integer tick-count
           math, no float floor/ceil residue)
  Perf   — tick_size resolution fails OPEN (0.0) on any lookup miss, never
           blocks the attach
  Stale  — the pre-fix plain round(x, 2) path is still exercised and
           byte-identical when tick_size is unknown (edge case c)
  Reuse  — reuses `_snap_to_tick` from orders_helpers.py (no duplicate
           tick-math implementation)
  UX     — a low-premium contract's trigger/limit never snaps to a
           broker-rejected 0 or negative price (edge case b)
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from backend.api.algo.template_attach import (
    _tp_trigger,
    _sl_trigger,
    _tp_limit_offset,
    _leg,
    resolve_template_plan,
    apply_template_to_order,
)


# ── _tp_trigger / _sl_trigger — tick-grid snap ──────────────────────

def test_tp_trigger_snaps_off_tick_value_to_grid():
    """fill=101.35, tp=10% → raw 111.485, tick=0.05 → snaps to 111.50."""
    trig = _tp_trigger("BUY", 101.35, 10.0, tick_size=0.05)
    assert trig == pytest.approx(111.50)
    # Confirm it really is an exact multiple of the tick.
    assert round(trig / 0.05) == pytest.approx(trig / 0.05)


def test_sl_trigger_snaps_off_tick_value_to_grid():
    """fill=101.35, sl=5% → raw 96.2825, tick=0.05 → snaps to 96.30."""
    trig = _sl_trigger("BUY", 101.35, 5.0, tick_size=0.05)
    assert trig == pytest.approx(96.30)


def test_tp_trigger_no_tick_size_preserves_plain_round(): # edge case (c)
    """tick_size unknown (default 0.0) → byte-identical pre-fix round(x, 2)."""
    trig = _tp_trigger("BUY", 101.35, 10.0)
    assert trig == round(101.35 * 1.10, 2)


def test_sl_trigger_no_tick_size_preserves_plain_round():
    """tick_size unknown (default 0.0) → byte-identical pre-fix round(x, 2)."""
    trig = _sl_trigger("BUY", 101.35, 5.0)
    assert trig == round(101.35 * 0.95, 2)


def test_tp_trigger_zero_tick_size_is_same_as_unset():
    """tick_size=0.0 explicitly passed behaves identically to the default."""
    assert _tp_trigger("BUY", 2900.0, 10.0, tick_size=0.0) == round(2900.0 * 1.10, 2)


# ── Floor-at-one-tick guard (edge case b) ───────────────────────────

def test_sl_trigger_low_premium_floor_at_one_tick_guard():
    """A ₹0.20 option, BUY parent, SL 90% → raw trigger = 0.02, which
    floors to exactly 0 on a 0.05 tick grid. Must clamp to one tick
    (0.05) above zero instead of reaching the broker as 0."""
    trig = _sl_trigger("BUY", 0.20, 90.0, tick_size=0.05)
    assert trig == pytest.approx(0.05)
    assert trig > 0


def test_tp_trigger_never_returns_non_positive_with_tick_size():
    """Defensive: no tp_pct magnitude should ever produce a non-positive
    snapped trigger when tick_size is known."""
    trig = _tp_trigger("SELL", 0.10, 95.0, tick_size=0.05)
    assert trig > 0


# ── _tp_limit_offset — directional snap, no collapse onto trigger ──

def test_tp_limit_offset_sell_exit_no_collapse_tight_offset():
    """SELL exit (BUY parent TP), tick=1.0, default offset=0.5 — a naive
    nearest-tick (banker's rounding) snap on 5999.5 would round to 6000
    (collapsing onto the trigger). Directional floor-by-ticks must land
    strictly below the trigger instead."""
    limit = _tp_limit_offset(6000.0, "SELL", "MCX", tick_size=1.0)
    assert limit == pytest.approx(5999.0)
    assert limit < 6000.0


def test_tp_limit_offset_buy_exit_no_collapse_tight_offset():
    """BUY exit (SELL parent TP), tick=1.0, default offset=0.5 — directional
    ceil-by-ticks must land strictly above the trigger."""
    limit = _tp_limit_offset(6000.0, "BUY", "MCX", tick_size=1.0)
    assert limit == pytest.approx(6001.0)
    assert limit > 6000.0


def test_tp_limit_offset_sell_exit_nfo_tick_grid():
    """NFO option, tick=0.05, offset=0.05 (default NFO offset) — one tick
    below trigger, exact grid value."""
    limit = _tp_limit_offset(112.35, "SELL", "NFO", tick_size=0.05)
    assert limit == pytest.approx(112.30)
    assert limit < 112.35


def test_tp_limit_offset_buy_exit_nfo_tick_grid():
    limit = _tp_limit_offset(112.35, "BUY", "NFO", tick_size=0.05)
    assert limit == pytest.approx(112.40)
    assert limit > 112.35


def test_tp_limit_offset_no_tick_size_preserves_legacy_offset():  # edge case (c)
    """tick_size unknown → byte-identical pre-fix fixed-offset + round(x, 2)."""
    sell_limit = _tp_limit_offset(6000.0, "SELL", "MCX")
    buy_limit = _tp_limit_offset(6000.0, "BUY", "MCX")
    assert sell_limit == round(6000.0 - 0.5, 2)
    assert buy_limit == round(6000.0 + 0.5, 2)


def test_tp_limit_offset_floor_at_one_tick_guard():
    """A near-zero trigger with a SELL-exit offset larger than the trigger
    itself must never produce a non-positive LIMIT price."""
    limit = _tp_limit_offset(0.05, "SELL", "MCX", tick_size=0.05)
    assert limit > 0
    assert limit == pytest.approx(0.05)


# ── _leg() threads tick_size into the offset computation ───────────

def test_leg_limit_tp_applies_tick_snap():
    leg = _leg("SELL", 10, 6000.0, "NRML", "LIMIT",
               tp_offset_exchange="MCX", tick_size=1.0)
    assert leg["price"] == pytest.approx(5999.0)


def test_leg_limit_sl_no_offset_passthrough_unaffected_by_tick():
    """SL legs don't carry tp_offset_exchange — tick_size is accepted but
    has no effect since _tp_limit_offset is never invoked for them."""
    leg = _leg("SELL", 10, 95.70, "NRML", "LIMIT", tick_size=0.05)
    assert leg["price"] == pytest.approx(95.70)


# ── resolve_template_plan — end-to-end wiring through the plan ─────

def _base_template(tp_pct=10.0, sl_pct=5.0, tp_order_type="LIMIT",
                    tp_scales_json=None):
    return {
        "id": 1,
        "slug": "test-template",
        "name": "Test Template",
        "applies_to": "buy_any",
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "wing_premium_pct": None,
        "wing_strike_offset": None,
        "tp_order_type": tp_order_type,
        "tp_scales_json": tp_scales_json,
        "sl_trail_pct": None,
    }


def test_resolve_plan_tp_sl_snap_to_tick_grid_end_to_end():
    """Full resolve_template_plan() call with parent_tick_size set — the
    TP leg's LIMIT price (trigger + directional offset) must land on an
    exact tick-grid value, and so must the SL trigger."""
    template = _base_template(tp_pct=10.0, sl_pct=5.0)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="NIFTY25JULFUT",
        parent_side="BUY",
        parent_qty=75,
        parent_exchange="NFO",
        parent_fill_price=101.35,
        parent_tick_size=0.05,
    )
    assert plan.parent_tick_size == 0.05
    assert len(plan.gtts) == 1
    tp_trig, sl_trig = plan.gtts[0].trigger_values
    assert tp_trig == pytest.approx(111.50)
    assert sl_trig == pytest.approx(96.30)
    # TP leg (index 0) carries the directionally-snapped LIMIT offset.
    tp_leg_price = plan.gtts[0].orders[0]["price"]
    assert tp_leg_price < tp_trig  # SELL exit on a BUY parent: limit below trigger
    assert round(tp_leg_price / 0.05) == pytest.approx(tp_leg_price / 0.05)


def test_resolve_plan_scale_out_ticks_snap_per_step():
    """Scale-out TP ladder — each step's trigger must independently snap
    to the tick grid."""
    template = _base_template(
        tp_pct=None, sl_pct=None,
        tp_scales_json='[{"at_pct": 7.3, "close_pct": 50}, {"at_pct": 13.7, "close_pct": 50}]',
    )
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="NIFTY25JULFUT",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NFO",
        parent_fill_price=101.35,
        parent_tick_size=0.05,
    )
    assert len(plan.gtts) == 2
    for gtt in plan.gtts:
        trig = gtt.trigger_values[0]
        assert round(trig / 0.05) == pytest.approx(trig / 0.05), (
            f"scale-out trigger {trig} is not on the 0.05 tick grid"
        )


def test_resolve_plan_without_tick_size_defaults_to_zero_noop():
    """No parent_tick_size kwarg passed (existing callers/fixtures) →
    plan.parent_tick_size defaults to 0.0 and triggers are plain round(x, 2)."""
    template = _base_template(tp_pct=10.0, sl_pct=None)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=2900.0,
    )
    assert plan.parent_tick_size == 0.0
    assert plan.gtts[0].trigger_values[0] == round(2900.0 * 1.10, 2)


# ── apply_template_to_order — resolver wiring + fail-open behavior ──

def _apply_template_base():
    return {
        "id": 1,
        "slug": "default-bull",
        "name": "Default Bull",
        "applies_to": "buy_any",
        "tp_pct": 10.0,
        "sl_pct": 5.0,
        "wing_premium_pct": None,
        "wing_strike_offset": None,
        "tp_order_type": "LIMIT",
        "tp_scales_json": None,
        "sl_trail_pct": None,
    }


@pytest.mark.asyncio
async def test_apply_template_to_order_resolves_tick_size_and_snaps():
    """`_resolve_tick_size_for_order` is called and its result flows all
    the way into the resolved plan's trigger prices."""
    template = _apply_template_base()
    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.api.algo.template_attach._resolve_tick_size_for_order",
        new=AsyncMock(return_value=0.05),
    ) as mock_tick:
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=101.35,
            apply_path="preview",
        )

    assert mock_tick.await_count == 1
    assert result is not None
    assert result.plan.parent_tick_size == 0.05
    tp_trig, sl_trig = result.plan.gtts[0].trigger_values
    assert tp_trig == pytest.approx(111.50)
    assert sl_trig == pytest.approx(96.30)


@pytest.mark.asyncio
async def test_apply_template_to_order_tick_size_unknown_fails_open():
    """Tick-size resolver returning 0.0 (cache miss / lookup failure) must
    NOT refuse the attach — the plan still resolves with the legacy plain
    round(x, 2) trigger values (edge case c)."""
    template = _apply_template_base()
    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.api.algo.template_attach._resolve_tick_size_for_order",
        new=AsyncMock(return_value=0.0),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=101.35,
            apply_path="preview",
        )

    assert result is not None
    assert not result.errors
    assert result.plan.parent_tick_size == 0.0
    tp_trig, sl_trig = result.plan.gtts[0].trigger_values
    assert tp_trig == round(101.35 * 1.10, 2)
    assert sl_trig == round(101.35 * 0.95, 2)


@pytest.mark.asyncio
async def test_resolve_tick_size_for_order_exception_fails_open():
    """A raised exception inside the instruments-cache lookup must still
    return 0.0, never propagate — tick-size lookup is best-effort only."""
    from backend.api.algo.template_attach import _resolve_tick_size_for_order

    with patch(
        "backend.api.routes.orders_helpers._ensure_tick_index",
        new=AsyncMock(side_effect=RuntimeError("cache unavailable")),
    ):
        tick = await _resolve_tick_size_for_order("NSE", "RELIANCE")

    assert tick == 0.0
