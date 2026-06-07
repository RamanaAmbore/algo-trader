"""
Template-attachment contract tests.

These exercise the pure-data parts of backend.api.algo.template_attach
(plan resolution, override merging, wing-symbol computation, sign math
for TP/SL triggers). The sim/live apply paths are covered by integration
tests in test_sim_gtt_book + Playwright; this module locks the
template-to-plan translation in isolation.
"""
from __future__ import annotations

import pytest

from backend.api.algo.template_attach import (
    GttSpec,
    TemplatePlan,
    WingSpec,
    _close_side,
    _is_sell_option,
    _sl_trigger,
    _tp_trigger,
    _wing_symbol,
    build_adhoc_template,
    has_any_override,
    resolve_template_plan,
)


# ── Building blocks ─────────────────────────────────────────────────

def test_tp_trigger_buy_side_above_entry():
    """BUY parent: TP fires when price rises N% above entry."""
    assert _tp_trigger("BUY", 100.0, 30.0) == 130.0


def test_tp_trigger_sell_side_below_entry():
    """SELL parent (short): TP fires when price drops N% below entry."""
    assert _tp_trigger("SELL", 100.0, 30.0) == 70.0


def test_sl_trigger_buy_side_below_entry():
    """BUY parent: SL fires when price drops N% below entry."""
    assert _sl_trigger("BUY", 100.0, 20.0) == 80.0


def test_sl_trigger_sell_side_above_entry():
    """SELL parent: SL fires when price rises N% above entry."""
    assert _sl_trigger("SELL", 100.0, 20.0) == 120.0


def test_tp_sl_return_none_when_pct_is_none():
    assert _tp_trigger("BUY", 100.0, None) is None
    assert _sl_trigger("BUY", 100.0, None) is None


def test_close_side_inverts():
    assert _close_side("BUY") == "SELL"
    assert _close_side("SELL") == "BUY"


# ── Sell-option detection + wing maths ──────────────────────────────

def test_is_sell_option_recognises_kite_symbols():
    assert _is_sell_option("SELL", "NIFTY25APR22000CE") is True
    assert _is_sell_option("SELL", "BANKNIFTY25APR45000PE") is True
    assert _is_sell_option("SELL", "NIFTY2542422000CE") is True   # weekly
    # Side mismatch
    assert _is_sell_option("BUY", "NIFTY25APR22000CE") is False
    # Not an option
    assert _is_sell_option("SELL", "RELIANCE") is False
    assert _is_sell_option("SELL", "NIFTY26JUNFUT") is False


def test_wing_symbol_ce_adds_offset():
    """CE wing is bought at a HIGHER strike (caps upside damage)."""
    assert _wing_symbol("NIFTY25APR22000CE", 500) == "NIFTY25APR22500CE"


def test_wing_symbol_pe_subtracts_offset():
    """PE wing is bought at a LOWER strike (caps downside damage)."""
    assert _wing_symbol("NIFTY25APR22000PE", 500) == "NIFTY25APR21500PE"


def test_wing_symbol_returns_none_for_non_option():
    assert _wing_symbol("RELIANCE", 100) is None
    assert _wing_symbol("NIFTY26JUNFUT", 100) is None


def test_wing_symbol_rejects_zero_or_negative_strike():
    """A PE wing offset that drives strike to 0 or below is rejected."""
    assert _wing_symbol("NIFTY25APR1000PE", 5000) is None


# ── Override merging ────────────────────────────────────────────────

def test_overrides_win_over_template_defaults():
    """Operator's inline tweak supersedes the saved template."""
    template = {
        "id": 1, "slug": "default-bull", "name": "Default Bull",
        "applies_to": "buy_any",
        "tp_pct": 30.0, "sl_pct": 20.0,
        "wing_premium_pct": None, "wing_strike_offset": None,
    }
    overrides = {"tp_pct": 25.0, "sl_pct": None,
                 "wing_premium_pct": None, "wing_strike_offset": None}
    plan = resolve_template_plan(
        template, overrides,
        parent_account="A", parent_symbol="X", parent_side="BUY",
        parent_qty=10, parent_exchange="NSE", parent_fill_price=100.0,
    )
    # tp overridden to 25%, sl falls through to template's 20%.
    assert len(plan.gtts) == 1            # combined OCO
    g = plan.gtts[0]
    assert g.trigger_type == "two-leg"
    assert g.trigger_values == [125.0, 80.0]  # TP=100*1.25, SL=100*0.80


def test_template_none_means_no_attach():
    """The 'none' system template ships with all numerics NULL — plan
    has no GTTs and no wing."""
    template = {
        "id": 1, "slug": "none", "name": "None",
        "applies_to": "both",
        "tp_pct": None, "sl_pct": None,
        "wing_premium_pct": None, "wing_strike_offset": None,
    }
    plan = resolve_template_plan(
        template, {"tp_pct": None, "sl_pct": None,
                   "wing_premium_pct": None, "wing_strike_offset": None},
        parent_account="A", parent_symbol="X", parent_side="BUY",
        parent_qty=10, parent_exchange="NSE", parent_fill_price=100.0,
    )
    assert plan.gtts == []
    assert plan.wing is None


def test_tp_only_template_yields_single_gtt():
    template = {
        "id": 1, "slug": "tp-only", "name": "TP only",
        "applies_to": "buy_any",
        "tp_pct": 50.0, "sl_pct": None,
        "wing_premium_pct": None, "wing_strike_offset": None,
    }
    plan = resolve_template_plan(
        template, {"tp_pct": None, "sl_pct": None,
                   "wing_premium_pct": None, "wing_strike_offset": None},
        parent_account="A", parent_symbol="X", parent_side="BUY",
        parent_qty=10, parent_exchange="NSE", parent_fill_price=100.0,
    )
    assert len(plan.gtts) == 1
    assert plan.gtts[0].trigger_type == "single"
    assert plan.gtts[0].label == "TP"
    assert plan.gtts[0].trigger_values == [150.0]


def test_short_vol_template_includes_wing():
    """Default Short Vol — SELL CE 22000 → wing buy 22500CE."""
    template = {
        "id": 1, "slug": "default-short-vol", "name": "Default Short Vol",
        "applies_to": "sell_option",
        "tp_pct": 50.0, "sl_pct": None,
        "wing_premium_pct": None, "wing_strike_offset": 500,
    }
    plan = resolve_template_plan(
        template, {"tp_pct": None, "sl_pct": None,
                   "wing_premium_pct": None, "wing_strike_offset": None},
        parent_account="A", parent_symbol="NIFTY25APR22000CE",
        parent_side="SELL", parent_qty=50,
        parent_exchange="NFO", parent_fill_price=85.0,
    )
    assert plan.wing is not None
    assert plan.wing.tradingsymbol == "NIFTY25APR22500CE"
    assert plan.wing.transaction_type == "BUY"
    assert plan.wing.quantity == 50
    # TP=50%, SELL parent → TP fires when price drops 50%
    assert any(g.label == "TP" for g in plan.gtts)
    tp = [g for g in plan.gtts if g.label == "TP"][0]
    assert tp.trigger_values == [round(85.0 * 0.50, 2)]


def test_buy_side_skips_wing_even_when_offset_set():
    """Wing is sell-option-only; a BUY position never gets a wing."""
    template = {
        "id": 1, "slug": "weird", "name": "weird",
        "applies_to": "both",
        "tp_pct": 30.0, "sl_pct": None,
        "wing_premium_pct": None, "wing_strike_offset": 500,
    }
    plan = resolve_template_plan(
        template, {"tp_pct": None, "sl_pct": None,
                   "wing_premium_pct": None, "wing_strike_offset": None},
        parent_account="A", parent_symbol="NIFTY25APR22000CE",
        parent_side="BUY", parent_qty=50,
        parent_exchange="NFO", parent_fill_price=85.0,
    )
    assert plan.wing is None


# ── Capability-aware OCO split (Groww emulation) ────────────────────

def test_oco_capable_broker_packs_two_leg():
    from backend.shared.brokers.capabilities import KITE_CAPS
    template = {
        "id": 1, "slug": "default-bull", "name": "Default Bull",
        "applies_to": "buy_any",
        "tp_pct": 30.0, "sl_pct": 20.0,
        "wing_premium_pct": None, "wing_strike_offset": None,
    }
    plan = resolve_template_plan(
        template, {"tp_pct": None, "sl_pct": None,
                   "wing_premium_pct": None, "wing_strike_offset": None},
        parent_account="A", parent_symbol="X", parent_side="BUY",
        parent_qty=10, parent_exchange="NSE", parent_fill_price=100.0,
        broker_caps=KITE_CAPS,
    )
    assert len(plan.gtts) == 1
    assert plan.gtts[0].trigger_type == "two-leg"


def test_oco_unsupported_broker_splits_into_two_singles():
    """Groww has no OCO — emulation path: two single GTTs that the
    sim/live applier links via pair_with."""
    from backend.shared.brokers.capabilities import GROWW_CAPS
    template = {
        "id": 1, "slug": "default-bull", "name": "Default Bull",
        "applies_to": "buy_any",
        "tp_pct": 30.0, "sl_pct": 20.0,
        "wing_premium_pct": None, "wing_strike_offset": None,
    }
    plan = resolve_template_plan(
        template, {"tp_pct": None, "sl_pct": None,
                   "wing_premium_pct": None, "wing_strike_offset": None},
        parent_account="A", parent_symbol="X", parent_side="BUY",
        parent_qty=10, parent_exchange="NSE", parent_fill_price=100.0,
        broker_caps=GROWW_CAPS,
    )
    assert len(plan.gtts) == 2
    assert plan.gtts[0].trigger_type == "single"
    assert plan.gtts[1].trigger_type == "single"
    assert any("no OCO" in n for n in plan.notes)


# ── Ad-hoc template + override-detection helpers ────────────────────

def test_build_adhoc_template_from_overrides():
    overrides = {"tp_pct": 25.0, "sl_pct": 15.0,
                 "wing_premium_pct": None, "wing_strike_offset": None}
    t = build_adhoc_template(overrides)
    assert t["slug"] is None
    assert t["id"] is None
    assert t["tp_pct"] == 25.0
    assert t["sl_pct"] == 15.0


def test_has_any_override_detects_set_fields():
    assert has_any_override({"tp_pct": 30.0, "sl_pct": None,
                             "wing_premium_pct": None,
                             "wing_strike_offset": None}) is True
    assert has_any_override({"tp_pct": None, "sl_pct": None,
                             "wing_premium_pct": None,
                             "wing_strike_offset": None}) is False
