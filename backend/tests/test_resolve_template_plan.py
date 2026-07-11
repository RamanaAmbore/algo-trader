"""
Characterization tests for resolve_template_plan (backend/api/algo/template_attach.py:547).

Pure-data plan resolver tests covering:
- Symbol resolution (equity, futures, options)
- Virtual symbol handling (MCX, CDS, BFO)
- Override merging (operator tweaks override template defaults)
- TP/SL/Wing computation (price math, triggers, sign conventions)
- Scale-out ladder placement (Phase 3A multi-step TP)
- Validation notes (non-positive %, invalid wings)
- Broker capability detection (OCO vs single-pair splits)

Five test dimensions:
  SSOT   — plan structure matches template + overrides + parent context
  Perf   — no async calls or broker round-trips (pure data)
  Stale  — old hardcoded branch paths are exercised
  Reuse  — common TP/SL/wing patterns reused across tests
  UX     — validation notes surface to operator via plan.notes
"""

from __future__ import annotations

import pytest
import json as _json

from backend.api.algo.template_attach import (
    TemplatePlan,
    GttSpec,
    WingSpec,
    resolve_template_plan,
)
from backend.brokers.capabilities import (
    BrokerCapabilities,
    KITE_CAPS,
    GROWW_CAPS,
)


# ── Test data helpers ────────────────────────────────────────────────

def _base_template(
    tp_pct: float | None = 10.0,
    sl_pct: float | None = 5.0,
    wing_premium_pct: float | None = None,
    wing_strike_offset: int | None = None,
    tp_order_type: str = "LIMIT",
    tp_scales_json: str | None = None,
    sl_trail_pct: float | None = None,
    applies_to: str = "buy_any",
) -> dict:
    """Build a minimal template dict."""
    return {
        "id": 1,
        "slug": "test-template",
        "name": "Test Template",
        "applies_to": applies_to,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "wing_premium_pct": wing_premium_pct,
        "wing_strike_offset": wing_strike_offset,
        "tp_order_type": tp_order_type,
        "tp_scales_json": tp_scales_json,
        "sl_trail_pct": sl_trail_pct,
    }


# ── SSOT: Plan structure matches template + overrides ──────────────

def test_resolve_plan_basic_tp_only():
    """TP-only template → single GTT with TP trigger."""
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

    assert len(plan.gtts) == 1, "Expected 1 GTT for TP-only template"
    assert plan.gtts[0].label == "TP"
    assert plan.gtts[0].trigger_type == "single"
    assert plan.gtts[0].trigger_values[0] == pytest.approx(2900.0 * 1.10, rel=0.01)


def test_resolve_plan_basic_sl_only():
    """SL-only template → single GTT with SL trigger."""
    template = _base_template(tp_pct=None, sl_pct=5.0)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=2900.0,
    )

    assert len(plan.gtts) == 1, "Expected 1 GTT for SL-only template"
    assert plan.gtts[0].label == "SL"
    assert plan.gtts[0].trigger_type == "single"
    assert plan.gtts[0].trigger_values[0] == pytest.approx(2900.0 * 0.95, rel=0.01)


def test_resolve_plan_tp_plus_sl_oco_with_oco_support():
    """TP+SL template with OCO-capable broker → two-leg GTT."""
    template = _base_template(tp_pct=10.0, sl_pct=5.0)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=2900.0,
        broker_caps=KITE_CAPS,
    )

    assert len(plan.gtts) == 1, "Expected 1 OCO GTT"
    assert plan.gtts[0].trigger_type == "two-leg"
    assert len(plan.gtts[0].orders) == 2, "OCO has TP + SL legs"
    assert plan.gtts[0].trigger_values == [
        pytest.approx(2900.0 * 1.10, rel=0.01),  # TP
        pytest.approx(2900.0 * 0.95, rel=0.01),  # SL
    ]


def test_resolve_plan_tp_plus_sl_two_singles_without_oco():
    """TP+SL template with no-OCO broker (Groww) → two single GTTs."""
    template = _base_template(tp_pct=10.0, sl_pct=5.0)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=2900.0,
        broker_caps=GROWW_CAPS,
    )

    assert len(plan.gtts) == 2, "Expected 2 single GTTs (TP + SL)"
    assert plan.gtts[0].label == "TP"
    assert plan.gtts[1].label == "SL"
    assert all(g.trigger_type == "single" for g in plan.gtts)
    assert "no OCO" in plan.notes[0], "Should document Groww OCO limitation"


def test_resolve_plan_parent_metadata():
    """Parent order metadata carried through to plan."""
    template = _base_template()
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC123",
        parent_symbol="NIFTY25APR22000CE",
        parent_side="SELL",
        parent_qty=50,
        parent_exchange="NFO",
        parent_fill_price=125.50,
        parent_product="MIS",
        parent_lot_size=75,
    )

    assert plan.parent_account == "ACC123"
    assert plan.parent_symbol == "NIFTY25APR22000CE"
    assert plan.parent_side == "SELL"
    assert plan.parent_qty == 50
    assert plan.parent_exchange == "NFO"
    assert plan.parent_fill_price == 125.50
    assert plan.parent_lot_size == 75


# ── Override merging: operator tweaks win ────────────────────────

def test_resolve_plan_overrides_tp_pct():
    """Operator override tp_pct supersedes template."""
    template = _base_template(tp_pct=10.0, sl_pct=5.0)
    overrides = {"tp_pct": 20.0}  # Operator bumped to 20%
    plan = resolve_template_plan(
        template, overrides,
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    tp_trigger = plan.gtts[0].trigger_values[0]
    assert tp_trigger == pytest.approx(1000.0 * 1.20, rel=0.01), (
        f"TP should use override 20%, got {tp_trigger}"
    )


def test_resolve_plan_overrides_disable_tp():
    """Operator override 0 for tp_pct is rejected (non-positive), SL kept."""
    template = _base_template(tp_pct=10.0, sl_pct=5.0)
    overrides = {"tp_pct": 0.0}  # Zero is non-positive, rejected
    plan = resolve_template_plan(
        template, overrides,
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    # Should have SL only (TP rejected as non-positive)
    assert len(plan.gtts) == 1
    assert plan.gtts[0].label == "SL"


def test_resolve_plan_overrides_none_dict_treated_as_empty():
    """overrides=None handled gracefully (some callers don't surface overrides)."""
    template = _base_template(tp_pct=10.0, sl_pct=5.0)
    plan = resolve_template_plan(
        template, None,  # Defensive: handle None
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    # Should fall back to template defaults
    assert len(plan.gtts) == 1  # OCO
    assert plan.gtts[0].trigger_type == "two-leg"


# ── Validation notes: non-positive % rejected ────────────────────

def test_resolve_plan_tp_zero_percent_rejected():
    """tp_pct=0 is non-positive → dropped, note added."""
    template = _base_template(tp_pct=0.0, sl_pct=5.0)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    # TP should be dropped, SL kept
    assert len(plan.gtts) == 1
    assert plan.gtts[0].label == "SL"
    assert any("tp_pct=0" in n for n in plan.notes), (
        f"Should document tp_pct=0 rejection in notes, got {plan.notes}"
    )


def test_resolve_plan_tp_negative_percent_rejected():
    """tp_pct=-5 is non-positive → dropped, note added."""
    template = _base_template(tp_pct=-5.0, sl_pct=5.0)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    assert len(plan.gtts) == 1
    assert plan.gtts[0].label == "SL"
    assert any("tp_pct=-5" in n for n in plan.notes)


def test_resolve_plan_sl_negative_percent_rejected():
    """sl_pct=-10 is non-positive → dropped, note added."""
    template = _base_template(tp_pct=10.0, sl_pct=-10.0)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    assert len(plan.gtts) == 1
    assert plan.gtts[0].label == "TP"
    assert any("sl_pct=-10" in n for n in plan.notes)


def test_resolve_plan_both_tp_sl_rejected():
    """Both TP and SL non-positive → no GTTs, both notes added."""
    template = _base_template(tp_pct=-1.0, sl_pct=0.0)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    assert len(plan.gtts) == 0, "Both TP and SL rejected"
    assert any("tp_pct=-1" in n for n in plan.notes)
    assert any("sl_pct=0" in n for n in plan.notes)


# ── Scale-out ladder (Phase 3A) ──────────────────────────────────

def test_resolve_plan_scale_out_single_step():
    """tp_scales_json with 1 step → single GTT at that scale."""
    template = _base_template(tp_pct=None, sl_pct=5.0)
    scales = [{"at_pct": 5.0, "close_pct": 100.0}]
    template["tp_scales_json"] = _json.dumps(scales)

    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=100,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    # Should have 2 GTTs: 1 scale-TP + 1 SL
    assert len(plan.gtts) == 2, f"Expected 2 GTTs (scale-TP + SL), got {len(plan.gtts)}"
    # First GTT is the scale-out TP
    scale_gtt = next((g for g in plan.gtts if "TP+" in g.label), None)
    assert scale_gtt is not None, "Should have scale-TP label"
    assert scale_gtt.trigger_values[0] == pytest.approx(1000.0 * 1.05, rel=0.01)
    # Second GTT is SL with full qty
    sl_gtt = next((g for g in plan.gtts if g.label == "SL"), None)
    assert sl_gtt is not None
    # Scale-TP orders are sized to close_pct of parent_qty
    assert scale_gtt.orders[0]["quantity"] == 100


def test_resolve_plan_scale_out_three_steps():
    """tp_scales_json with 3 steps → ladder of GTTs."""
    template = _base_template(tp_pct=None, sl_pct=None)
    scales = [
        {"at_pct": 5.0, "close_pct": 30.0},
        {"at_pct": 10.0, "close_pct": 40.0},
        {"at_pct": 15.0, "close_pct": 30.0},
    ]
    template["tp_scales_json"] = _json.dumps(scales)

    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=1000,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    # 3 GTTs for the 3 scales
    assert len(plan.gtts) == 3, f"Expected 3 scale GTTs, got {len(plan.gtts)}"
    # Check qty allocation: 30%, 40%, 30% of 1000 = 300, 400, 300
    assert plan.gtts[0].orders[0]["quantity"] == 300
    assert plan.gtts[1].orders[0]["quantity"] == 400
    assert plan.gtts[2].orders[0]["quantity"] == 300


def test_resolve_plan_scale_out_rounding_leftover():
    """Leftover qty from rounding goes to last scale."""
    template = _base_template(tp_pct=None, sl_pct=None)
    scales = [
        {"at_pct": 5.0, "close_pct": 33.33},
        {"at_pct": 10.0, "close_pct": 33.33},
        {"at_pct": 15.0, "close_pct": 33.33},
    ]
    template["tp_scales_json"] = _json.dumps(scales)

    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=100,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    # Verify sum equals parent_qty despite rounding
    total_closed = sum(g.orders[0]["quantity"] for g in plan.gtts)
    assert total_closed == 100, (
        f"Scale quantities must sum to parent_qty=100, got {total_closed}"
    )


def test_resolve_plan_scale_out_with_sl():
    """Scale-out TP ladder + SL on full qty (Phase 3A warning)."""
    template = _base_template(tp_pct=None, sl_pct=5.0)
    scales = [
        {"at_pct": 5.0, "close_pct": 50.0},
        {"at_pct": 10.0, "close_pct": 50.0},
    ]
    template["tp_scales_json"] = _json.dumps(scales)

    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=200,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    # 2 scale-TPs + 1 SL
    assert len(plan.gtts) == 3
    sl_gtt = next((g for g in plan.gtts if g.label == "SL"), None)
    assert sl_gtt is not None
    # SL is on FULL parent_qty (200), not residual
    assert sl_gtt.orders[0]["quantity"] == 200
    # Should have explicit warning about oversell risk
    assert any("SL is sized for full parent qty" in n for n in plan.notes), (
        f"Should warn about oversell risk, got {plan.notes}"
    )


def test_resolve_plan_scale_out_invalid_json():
    """Malformed tp_scales_json → scales ignored, note added."""
    template = _base_template(tp_pct=10.0, sl_pct=5.0)
    template["tp_scales_json"] = "NOT VALID JSON"

    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=100,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    # Should fall back to basic TP+SL (no scales)
    assert len(plan.gtts) == 1
    assert plan.gtts[0].trigger_type == "two-leg"


def test_resolve_plan_scale_out_skips_invalid_entries():
    """tp_scales_json with invalid entries skipped, valid used."""
    template = _base_template(tp_pct=None, sl_pct=None)
    scales = [
        {"at_pct": 5.0, "close_pct": 50.0},
        {"at_pct": "NOT_A_NUMBER", "close_pct": 50.0},  # Skip this
        {"at_pct": 10.0, "close_pct": 50.0},
    ]
    template["tp_scales_json"] = _json.dumps(scales)

    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=100,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    # 2 valid scales used
    assert len(plan.gtts) == 2


# ── Sell-option wing attachment ──────────────────────────────────

def test_resolve_plan_sell_option_wing_by_offset():
    """SELL option with wing_strike_offset → wing attached."""
    template = _base_template(
        tp_pct=10.0,
        sl_pct=5.0,
        wing_strike_offset=500,  # Buy CE 500pt higher
    )
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="NIFTY25APR22000CE",
        parent_side="SELL",
        parent_qty=50,
        parent_exchange="NFO",
        parent_fill_price=125.50,
    )

    assert plan.wing is not None, "Wing should be attached for SELL option"
    # Wing strike is +500 from parent (CE)
    assert plan.wing.tradingsymbol == "NIFTY25APR22500CE", (
        f"Wing should be 500pt higher for CE, got {plan.wing.tradingsymbol}"
    )
    assert plan.wing.transaction_type == "BUY"
    assert plan.wing.quantity == 50  # Matches parent qty


def test_resolve_plan_sell_option_wing_pe_negative_offset():
    """SELL PE option wing_strike_offset → negative offset subtracted."""
    template = _base_template(
        tp_pct=10.0,
        sl_pct=5.0,
        wing_strike_offset=500,
    )
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="NIFTY25APR22000PE",
        parent_side="SELL",
        parent_qty=50,
        parent_exchange="NFO",
        parent_fill_price=100.0,
    )

    assert plan.wing is not None
    # Wing strike is -500 from parent (PE)
    assert plan.wing.tradingsymbol == "NIFTY25APR21500PE", (
        f"Wing should be 500pt lower for PE, got {plan.wing.tradingsymbol}"
    )


def test_resolve_plan_sell_option_wing_premium_pct_estimate():
    """wing_premium_pct → estimated_price as fraction of parent fill."""
    template = _base_template(
        tp_pct=10.0,
        wing_strike_offset=100,
        wing_premium_pct=20.0,
    )
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="NIFTY25APR22000CE",
        parent_side="SELL",
        parent_qty=50,
        parent_exchange="NFO",
        parent_fill_price=100.0,
    )

    assert plan.wing is not None
    # Estimated price = parent_fill × wing_premium_pct / 100
    expected = round(100.0 * 20.0 / 100.0, 2)
    assert plan.wing.estimated_price == expected, (
        f"Wing estimate should be {expected}, got {plan.wing.estimated_price}"
    )


def test_resolve_plan_sell_option_wing_override_picked():
    """_wing_picked_symbol override from apply_template_to_order used."""
    template = _base_template(tp_pct=10.0, wing_strike_offset=100)
    overrides = {
        "_wing_picked_symbol": "NIFTY25APR22500CE",
        "_wing_picked_ltp": 45.50,
    }
    plan = resolve_template_plan(
        template, overrides,
        parent_account="ACC1",
        parent_symbol="NIFTY25APR22000CE",
        parent_side="SELL",
        parent_qty=50,
        parent_exchange="NFO",
        parent_fill_price=125.0,
    )

    assert plan.wing is not None
    # Should use the picked symbol, not compute from offset
    assert plan.wing.tradingsymbol == "NIFTY25APR22500CE"
    assert plan.wing.estimated_price == 45.50


def test_resolve_plan_buy_option_no_wing():
    """BUY option (not SELL) → no wing regardless of offset."""
    template = _base_template(
        tp_pct=10.0,
        wing_strike_offset=500,
    )
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="NIFTY25APR22000CE",
        parent_side="BUY",  # Not SELL
        parent_qty=50,
        parent_exchange="NFO",
        parent_fill_price=50.0,
    )

    assert plan.wing is None, "Wing should not attach for BUY option"


def test_resolve_plan_equity_no_wing():
    """Equity (not option) → no wing regardless of offset."""
    template = _base_template(
        tp_pct=10.0,
        wing_strike_offset=500,
    )
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="SELL",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=2900.0,
    )

    assert plan.wing is None, "Wing should not attach for equity"


def test_resolve_plan_wing_symbol_parsing_fails():
    """Wing symbol parsing fails (invalid option pattern) → no wing attached."""
    template = _base_template(
        tp_pct=10.0,
        wing_strike_offset=500,
    )
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="GARBLED_SYMBOL_12345",  # Not a valid option symbol
        parent_side="SELL",
        parent_qty=50,
        parent_exchange="NFO",
        parent_fill_price=100.0,
    )

    # Parent is not recognized as an option, so no wing attach attempted
    assert plan.wing is None


# ── Trailing stop (Phase 3B) ─────────────────────────────────────

def test_resolve_plan_sl_trail_pct():
    """sl_trail_pct carried through to SL GTT spec."""
    template = _base_template(
        tp_pct=10.0,
        sl_pct=5.0,
        sl_trail_pct=0.5,  # Trail by 0.5%
    )
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    sl_gtt = plan.gtts[0]  # OCO has SL as index 1, but check exists
    # Trail pct should be in the spec (used by background poller)
    assert sl_gtt.sl_trail_pct == 0.5, (
        f"SL trail pct should carry through, got {sl_gtt.sl_trail_pct}"
    )


def test_resolve_plan_sl_trail_pct_override():
    """Operator override sl_trail_pct."""
    template = _base_template(
        tp_pct=10.0,
        sl_pct=5.0,
        sl_trail_pct=0.3,
    )
    overrides = {"sl_trail_pct": 1.0}  # Increase trail
    plan = resolve_template_plan(
        template, overrides,
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    sl_gtt = plan.gtts[0]
    assert sl_gtt.sl_trail_pct == 1.0, "Override should win"


# ── TP order type (LIMIT vs MARKET) ──────────────────────────────

def test_resolve_plan_tp_order_type_limit_default():
    """tp_order_type defaults to LIMIT."""
    template = _base_template(tp_pct=10.0, sl_pct=5.0, tp_order_type=None)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    # OCO has TP at index 0
    tp_leg = plan.gtts[0].orders[0]
    assert tp_leg["order_type"] == "LIMIT"


def test_resolve_plan_tp_order_type_market():
    """tp_order_type MARKET → market-take TP."""
    template = _base_template(
        tp_pct=10.0,
        sl_pct=5.0,
        tp_order_type="MARKET",
    )
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    tp_leg = plan.gtts[0].orders[0]
    assert tp_leg["order_type"] == "MARKET"


def test_resolve_plan_tp_order_type_override():
    """Operator override tp_order_type."""
    template = _base_template(
        tp_pct=10.0,
        tp_order_type="LIMIT",
    )
    overrides = {"tp_order_type": "MARKET"}
    plan = resolve_template_plan(
        template, overrides,
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    tp_leg = plan.gtts[0].orders[0]
    assert tp_leg["order_type"] == "MARKET", "Override should win"


def test_resolve_plan_tp_order_type_invalid_defaults_to_limit():
    """Invalid tp_order_type (typo) → default to LIMIT."""
    template = _base_template(
        tp_pct=10.0,
        tp_order_type="INVALID_TYPE",
    )
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    tp_leg = plan.gtts[0].orders[0]
    assert tp_leg["order_type"] == "LIMIT", (
        "Invalid tp_order_type should default to LIMIT"
    )


# ── Side math (BUY/SELL) ─────────────────────────────────────────

def test_resolve_plan_sell_side_tp_below_entry():
    """SELL (short): TP fires when price drops below entry."""
    template = _base_template(tp_pct=10.0, sl_pct=None)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="SELL",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    tp_trigger = plan.gtts[0].trigger_values[0]
    expected = 1000.0 * (1 - 0.10)  # 10% BELOW for short
    assert tp_trigger == pytest.approx(expected, rel=0.01), (
        f"SELL TP should be below entry, got {tp_trigger}"
    )


def test_resolve_plan_sell_side_exit_uses_buy():
    """SELL parent → exit uses BUY side."""
    template = _base_template(tp_pct=10.0, sl_pct=None)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="SELL",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    exit_leg = plan.gtts[0].orders[0]
    assert exit_leg["transaction_type"] == "BUY", (
        "SELL parent → exit is BUY to flatten short"
    )


# ── Lot size handling ────────────────────────────────────────────

def test_resolve_plan_parent_lot_size_mcx():
    """MCX parent_lot_size=100 → plan.parent_lot_size=100."""
    template = _base_template(tp_pct=10.0, sl_pct=5.0)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="CRUDEOIL25AUGFUT",
        parent_side="BUY",
        parent_qty=100,
        parent_exchange="MCX",
        parent_fill_price=5000.0,
        parent_lot_size=100,
    )

    assert plan.parent_lot_size == 100, (
        "MCX lot_size should be preserved in plan"
    )


def test_resolve_plan_parent_lot_size_one_stays_one():
    """parent_lot_size=1 → plan.parent_lot_size=1 (NSE equity)."""
    template = _base_template(tp_pct=10.0, sl_pct=5.0)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=2900.0,
        parent_lot_size=1,
    )

    assert plan.parent_lot_size == 1


def test_resolve_plan_parent_lot_size_zero_clamped_to_one():
    """parent_lot_size=0 (invalid) → clamped to 1."""
    template = _base_template(tp_pct=10.0, sl_pct=5.0)
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="CRUDEOIL25AUGFUT",
        parent_side="BUY",
        parent_qty=100,
        parent_exchange="MCX",
        parent_fill_price=5000.0,
        parent_lot_size=0,  # Invalid
    )

    assert plan.parent_lot_size == 1, (
        "Invalid lot_size should be clamped to 1"
    )


# ── Template metadata ────────────────────────────────────────────

def test_resolve_plan_template_id_carried():
    """template_id from template dict → plan.template_id."""
    template = _base_template()
    template["id"] = 42
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    assert plan.template_id == 42


def test_resolve_plan_template_slug_carried():
    """template_slug → plan.template_slug."""
    template = _base_template()
    template["slug"] = "default-bull-4-min"
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    assert plan.template_slug == "default-bull-4-min"


def test_resolve_plan_template_name_default():
    """Missing template name → defaults to '(unnamed)'."""
    template = _base_template()
    template["name"] = None
    plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
    )

    assert plan.template_name == "(unnamed)"
