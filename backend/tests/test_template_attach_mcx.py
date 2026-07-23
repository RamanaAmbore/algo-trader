"""
MCX qty-as-lots defect tests for the GTT template-attach layer.

Incident (aad6e8cb4bd3a18e8): GTT exit legs were built with raw
parent_qty (contracts) and passed directly to broker.place_gtt without
calling translate_qty. For MCX CRUDEOIL (lot_size=100) a 1-lot position
had parent_qty=100 contracts → GTT sent quantity=100 to Kite → Kite
interpreted it as 100 lots → 100× oversize.

Fix verifies:
1. apply_plan_live translates each GTT leg's quantity via translate_qty.
2. Wing order leg is also translated.
3. KiteBroker.place_gtt adapter ceiling rejects MCX qty > 50 lots.
4. Lot-size unknown (cache miss) propagates as attach failure, not silent
   fall-through.
5. _MCX_LOT_OVERRIDES resolves space-variant names (e.g. "CRUDE OIL").

Five test dimensions per project convention:
  SSOT   — quantities match what translate_qty produces
  Perf   — no extra broker round-trips beyond what the plan dictates
  Stale  — old raw-qty path is dead (ceiling guard catches it)
  Reuse  — broker.translate_qty (Broker ABC method) is the shared path
  UX     — clear error surfaces to caller when lot_size unknown
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from backend.api.algo.template_attach import (
    TemplatePlan,
    GttSpec,
    WingSpec,
    AttachResult,
    apply_plan_live,
    resolve_template_plan,
)
from backend.brokers.adapters.kite import KiteBroker, to_kite_qty
from backend.api.routes.instruments import _MCX_LOT_OVERRIDES


# ── Helpers ──────────────────────────────────────────────────────────

_MCX_TEMPLATE = {
    "id": 99, "slug": "default-bull", "name": "Default Bull",
    "applies_to": "buy_any",
    "tp_pct": 10.0, "sl_pct": 5.0,
    "wing_premium_pct": None, "wing_strike_offset": None,
    "tp_order_type": "LIMIT",
    "tp_scales_json": None,
    "sl_trail_pct": None,
}

_MCX_OVERRIDES = {
    "tp_pct": None, "sl_pct": None,
    "wing_premium_pct": None, "wing_strike_offset": None,
}


def _make_mcx_plan(parent_qty: int, lot_size: int) -> TemplatePlan:
    """Build a TemplatePlan for MCX CRUDEOIL BUY with TP+SL OCO."""
    plan = resolve_template_plan(
        _MCX_TEMPLATE, _MCX_OVERRIDES,
        parent_account="ZG0790",
        parent_symbol="CRUDEOIL25AUGFUT",
        parent_side="BUY",
        parent_qty=parent_qty,
        parent_exchange="MCX",
        parent_fill_price=5000.0,
        parent_lot_size=lot_size,
    )
    return plan


def _make_mock_broker(lot_size: int) -> MagicMock:
    """Return a mock broker whose translate_qty delegates to to_kite_qty."""
    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.place_gtt.return_value = "gtt-123"
    broker.place_order.return_value = "order-456"
    broker.translate_qty.side_effect = lambda exch, qty, ls: to_kite_qty(exch, qty, ls)
    return broker


# ── SSOT: translate_qty produces correct lots ─────────────────────────

def test_to_kite_qty_crudeoil_1_lot():
    """100 contracts at lot_size=100 → 1 lot."""
    assert to_kite_qty("MCX", 100, 100) == 1


def test_to_kite_qty_crudeoil_2_lots():
    """200 contracts at lot_size=100 → 2 lots."""
    assert to_kite_qty("MCX", 200, 100) == 2


def test_to_kite_qty_nse_passthrough():
    """NSE contracts are passed through unchanged (no translation)."""
    assert to_kite_qty("NFO", 50, 50) == 50


def test_to_kite_qty_raises_on_lot_size_zero():
    """lot_size=0 means cache miss — must raise, not return garbage."""
    with pytest.raises(ValueError, match="lot_size"):
        to_kite_qty("MCX", 100, 0)


def test_to_kite_qty_raises_on_lot_size_one():
    """lot_size=1 on MCX is a cache sentinel — raise, not pass-through."""
    with pytest.raises(ValueError, match="lot_size"):
        to_kite_qty("MCX", 100, 1)


# ── SSOT: apply_plan_live translates GTT leg quantities ──────────────

def test_gtt_leg_qty_translated_1_lot():
    """1-lot CRUDEOIL: GTT legs must carry quantity=1, not 100."""
    lot_size = 100
    plan = _make_mcx_plan(parent_qty=100, lot_size=lot_size)
    broker = _make_mock_broker(lot_size)

    result = apply_plan_live(plan, broker)

    assert not result.errors, f"unexpected errors: {result.errors}"
    # Both TP and SL legs must arrive with translated qty=1
    call_args_list = broker.place_gtt.call_args_list
    assert len(call_args_list) >= 1
    for call in call_args_list:
        orders_sent = call.kwargs.get("orders") or call.args[0] if call.args else []
        for leg in orders_sent:
            assert leg["quantity"] == 1, (
                f"Expected lot qty=1, got {leg['quantity']} for leg {leg}"
            )


def test_gtt_leg_qty_translated_2_lots():
    """2-lot CRUDEOIL (qty=200): GTT legs must carry quantity=2."""
    lot_size = 100
    plan = _make_mcx_plan(parent_qty=200, lot_size=lot_size)
    broker = _make_mock_broker(lot_size)

    result = apply_plan_live(plan, broker)

    assert not result.errors, f"unexpected errors: {result.errors}"
    call_args_list = broker.place_gtt.call_args_list
    assert len(call_args_list) >= 1
    for call in call_args_list:
        orders_sent = call.kwargs.get("orders") or []
        for leg in orders_sent:
            assert leg["quantity"] == 2, (
                f"Expected lot qty=2, got {leg['quantity']}"
            )


def test_broker_translate_qty_called_per_leg():
    """translate_qty is called for every leg in every spec — not just once."""
    lot_size = 100
    plan = _make_mcx_plan(parent_qty=100, lot_size=lot_size)
    broker = _make_mock_broker(lot_size)

    apply_plan_live(plan, broker)

    # The TP+SL OCO plan has 1 GttSpec with 2 legs → translate_qty called twice
    total_legs = sum(len(spec.orders) for spec in plan.gtts)
    assert broker.translate_qty.call_count == total_legs, (
        f"Expected {total_legs} translate_qty calls (one per leg), "
        f"got {broker.translate_qty.call_count}"
    )


# ── Wing order translate_qty ──────────────────────────────────────────

def test_wing_order_qty_translated():
    """Wing leg quantity is also translated via translate_qty."""
    lot_size = 100
    plan = TemplatePlan(
        template_id=1,
        template_name="test",
        template_slug="test",
        parent_account="ZG0790",
        parent_symbol="CRUDEOIL25AUGFUT",
        parent_side="SELL",
        parent_qty=100,      # 1 lot in contracts
        parent_exchange="MCX",
        parent_fill_price=5000.0,
        parent_lot_size=lot_size,
    )
    plan.wing = WingSpec(
        tradingsymbol="CRUDEOIL25AUGSOMECALLOPT",
        transaction_type="BUY",
        quantity=100,  # raw contracts, must become 1 lot
        exchange="MCX",
        product="NRML",
        order_type="MARKET",
    )
    broker = _make_mock_broker(lot_size)

    result = apply_plan_live(plan, broker)

    assert not result.errors, f"unexpected errors: {result.errors}"
    assert broker.place_order.called
    call_kwargs = broker.place_order.call_args.kwargs
    assert call_kwargs["quantity"] == 1, (
        f"Wing order: expected translated qty=1, got {call_kwargs['quantity']}"
    )


# ── Adapter ceiling guard ─────────────────────────────────────────────

def test_kite_broker_place_gtt_rejects_mcx_qty_over_50():
    """KiteBroker.place_gtt must refuse MCX/NCO legs with qty above the
    configurable ceiling (default 200 lots per orders.mcx_gtt_lot_ceiling).
    Qty=300 is above the default and must be refused."""
    from backend.brokers.adapters.kite import KiteBroker

    mock_conn = MagicMock()
    kite_broker = KiteBroker(mock_conn)

    # Use 300 lots — above the new default 200-lot ceiling
    with pytest.raises(ValueError, match="absurd-value ceiling"):
        kite_broker.place_gtt(
            trigger_type="single",
            tradingsymbol="CRUDEOIL25AUGFUT",
            exchange="MCX",
            last_price=5000.0,
            trigger_values=[5500.0],
            orders=[{"transaction_type": "SELL", "quantity": 300,
                     "price": 5500.0, "order_type": "LIMIT", "product": "NRML"}],
        )


def test_kite_broker_place_gtt_allows_mcx_qty_50():
    """MCX qty=50 lots is well below the default 200-lot ceiling — must be allowed through."""
    from backend.brokers.adapters.kite import KiteBroker

    mock_conn = MagicMock()
    mock_kite = MagicMock()
    mock_kite.place_gtt.return_value = {"trigger_id": 42}
    mock_conn.get_kite_conn.return_value = mock_kite
    kite_broker = KiteBroker(mock_conn)

    # 50 lots should pass the ceiling
    result = kite_broker.place_gtt(
        trigger_type="single",
        tradingsymbol="CRUDEOIL25AUGFUT",
        exchange="MCX",
        last_price=5000.0,
        trigger_values=[5500.0],
        orders=[{"transaction_type": "SELL", "quantity": 50,
                 "price": 5500.0, "order_type": "LIMIT", "product": "NRML"}],
    )
    assert result == "42"


def test_kite_broker_place_gtt_rejects_nfo_qty_over_50000():
    """NFO GTT legs with qty > 50000 contracts must be refused."""
    from backend.brokers.adapters.kite import KiteBroker

    mock_conn = MagicMock()
    kite_broker = KiteBroker(mock_conn)

    with pytest.raises(ValueError, match="50000-contract absurd-value ceiling"):
        kite_broker.place_gtt(
            trigger_type="single",
            tradingsymbol="NIFTY25AUGFUT",
            exchange="NFO",
            last_price=22000.0,
            trigger_values=[24000.0],
            orders=[{"transaction_type": "SELL", "quantity": 60000,
                     "price": 24000.0, "order_type": "LIMIT", "product": "NRML"}],
        )


# ── Lot-size unknown (cache miss) ─────────────────────────────────────

def test_translate_qty_failure_propagates_as_attach_error():
    """translate_qty ValueError → error collected in AttachResult, no silent fallthrough."""
    lot_size = 100
    plan = _make_mcx_plan(parent_qty=100, lot_size=lot_size)

    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    # Simulate translate_qty raising (e.g. lot_size=1 cache miss)
    broker.translate_qty.side_effect = ValueError("lot_size<=1 cache miss")

    result = apply_plan_live(plan, broker)

    # Must surface an error, never call place_gtt
    assert result.errors, "Expected at least one error in AttachResult"
    assert not broker.place_gtt.called, (
        "place_gtt must not be called when translate_qty fails"
    )
    assert any("GTT-QTY-GUARD" in e or "translate_qty" in e.lower()
               for e in result.errors), (
        f"Error must reference translate_qty failure, got: {result.errors}"
    )


# ── _MCX_LOT_OVERRIDES: space-variant aliases ─────────────────────────

@pytest.mark.parametrize("name,expected_lot_size", [
    ("CRUDEOIL",   100),
    ("CRUDE OIL",  100),   # space-variant alias
    ("NATURALGAS", 1250),
    ("NATURAL GAS", 1250),  # space-variant alias
    ("SILVER MIC", 1),      # space-variant alias
    ("SILVERM",    5),
    ("SILVER M",   5),      # space-variant alias
    ("GOLD M",     10),     # space-variant alias
    ("MENTHA OIL", 360),    # space-variant alias
])
def test_mcx_lot_overrides_space_variants(name: str, expected_lot_size: int):
    """_MCX_LOT_OVERRIDES must resolve both compact and space-separated names."""
    # The lookup uses .upper() before the dict get — match that
    result = _MCX_LOT_OVERRIDES.get(name.upper())
    assert result == expected_lot_size, (
        f"_MCX_LOT_OVERRIDES[{name!r}] = {result!r}, expected {expected_lot_size}"
    )


def test_mcx_lot_overrides_all_keys_uppercase():
    """All keys in _MCX_LOT_OVERRIDES must be uppercase (lookup uses .upper())."""
    for key in _MCX_LOT_OVERRIDES:
        assert key == key.upper(), (
            f"Key {key!r} is not uppercase — lookup via .upper() would miss it"
        )


# ── Stale-code check: raw untranslated qty never reaches broker ───────

def test_raw_qty_blocked_by_ceiling_before_reaching_sdk():
    """Defense-in-depth: even if translate_qty was bypassed, the adapter
    ceiling in place_gtt must block absurd MCX contract qty from reaching
    the Kite SDK.  This test ensures the ceiling exists at the adapter layer.

    The ceiling is configurable (default 200 lots). We use 300 lots here
    which is above the default ceiling and represents an obvious fat-finger."""
    from backend.brokers.adapters.kite import KiteBroker

    mock_conn = MagicMock()
    mock_kite = MagicMock()
    mock_conn.get_kite_conn.return_value = mock_kite
    kite_broker = KiteBroker(mock_conn)

    # 300 lots — above the default 200-lot ceiling — must be blocked
    with pytest.raises(ValueError, match="absurd-value ceiling"):
        kite_broker.place_gtt(
            trigger_type="single",
            tradingsymbol="CRUDEOIL25AUGFUT",
            exchange="MCX",
            last_price=5000.0,
            trigger_values=[5500.0],
            orders=[{"transaction_type": "SELL", "quantity": 300,
                     "price": 5500.0, "order_type": "LIMIT", "product": "NRML"}],
        )

    # SDK must never have been called
    assert not mock_kite.place_gtt.called, (
        "Kite SDK place_gtt must not be called when ceiling fires"
    )


# ── G1 lot-multiple guard in apply_plan_live ─────────────────────────
# These tests verify the new early-return guard added at the top of
# apply_plan_live that fires BEFORE any broker call when a GTT leg or
# wing carries a qty that is not a multiple of parent_lot_size.

def test_g1_guard_rejects_sub_lot_gtt_qty():
    """Sub-lot qty on a GTT leg returns errors before calling place_gtt."""
    lot_size = 100
    # Build a plan with qty=50 (half a lot) — invalid
    plan = _make_mcx_plan(parent_qty=50, lot_size=lot_size)
    broker = _make_mock_broker(lot_size)

    result = apply_plan_live(plan, broker)

    assert result.errors, "Expected G1 error for sub-lot GTT qty"
    assert any("G1 lot-multiple guard" in e for e in result.errors), (
        f"Error must mention 'G1 lot-multiple guard', got: {result.errors}"
    )
    assert not broker.place_gtt.called, (
        "place_gtt must not be called when G1 fires"
    )


def test_g1_guard_passes_exact_multiple():
    """Exact multiple of lot_size must proceed to normal placement."""
    lot_size = 100
    plan = _make_mcx_plan(parent_qty=100, lot_size=lot_size)  # exactly 1 lot
    broker = _make_mock_broker(lot_size)

    result = apply_plan_live(plan, broker)

    assert not result.errors, f"Unexpected G1 errors: {result.errors}"
    assert broker.place_gtt.called, "place_gtt must be called for valid qty"


def test_g1_guard_passes_two_lots_exact():
    """2-lot exact multiple (qty=200) must proceed."""
    lot_size = 100
    plan = _make_mcx_plan(parent_qty=200, lot_size=lot_size)
    broker = _make_mock_broker(lot_size)

    result = apply_plan_live(plan, broker)

    assert not result.errors, f"Unexpected G1 errors: {result.errors}"


def test_g1_guard_rejects_wing_sub_lot():
    """Sub-lot wing qty also triggers G1 early return before place_order."""
    lot_size = 100
    plan = TemplatePlan(
        template_id=1,
        template_name="test",
        template_slug="test",
        parent_account="ZG0790",
        parent_symbol="CRUDEOIL25AUGFUT",
        parent_side="SELL",
        parent_qty=100,          # 1 lot — valid GTT qty
        parent_exchange="MCX",
        parent_fill_price=5000.0,
        parent_lot_size=lot_size,
    )
    plan.wing = WingSpec(
        tradingsymbol="CRUDEOIL25AUGSOMECALLOPT",
        transaction_type="BUY",
        quantity=50,             # half a lot — invalid
        exchange="MCX",
        product="NRML",
        order_type="MARKET",
    )
    broker = _make_mock_broker(lot_size)

    result = apply_plan_live(plan, broker)

    assert result.errors, "Expected G1 error for sub-lot wing qty"
    assert any("G1 lot-multiple guard" in e for e in result.errors), (
        f"Error must mention 'G1 lot-multiple guard', got: {result.errors}"
    )
    assert not broker.place_order.called, (
        "place_order must not be called when wing G1 fires"
    )


def test_g1_guard_skips_when_lot_size_one():
    """lot_size=1 (equity/micro sentinel) — G1 guard skips entirely."""
    plan = TemplatePlan(
        template_id=1,
        template_name="test",
        template_slug="test",
        parent_account="ZG0790",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=7,            # arbitrary — not a lot concern
        parent_exchange="NSE",
        parent_fill_price=2900.0,
        parent_lot_size=1,
    )
    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.place_gtt.return_value = "gtt-789"
    broker.translate_qty.side_effect = lambda exch, qty, ls: qty  # NSE passthrough

    # Should not error even if qty=7 (odd number)
    result = apply_plan_live(plan, broker)

    # No G1 errors (guard skips for lot_size=1)
    assert not any("G1 lot-multiple guard" in e for e in result.errors), (
        f"G1 guard must not fire for lot_size=1, got: {result.errors}"
    )
