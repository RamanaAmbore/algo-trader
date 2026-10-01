"""
BUY-parent offset-SELL LIMIT leg (2026-10) — mirror of the existing
SELL-parent BUY-wing-MARKET hedge.

Operator-confirmed requirements:
  1. Strike selection reuses the SAME "closest to expected price" scan
     already in `_pick_wing_by_premium` (and the manual `wing_strike_offset`
     fallback via `_wing_symbol`) — no new strike-selection logic.
  2. Reuses the EXISTING `wing_strike_offset`/`wing_premium_pct` template
     fields — no new template parameters.
  3. The offset-SELL leg is ALWAYS order_type=LIMIT, never MARKET — this
     is explicitly different from the existing SELL-parent → BUY-wing
     direction, which stays MARKET.

Five test dimensions:
  SSOT   — WingSpec.transaction_type/order_type/limit_price match the
           operator-confirmed direction; parent_side drives `_wing_direction`
  Perf   — the manual-offset quote lookup is a SINGLE quote call, reusing
           `_wing_fetch_quotes` (no duplicate broker round-trips)
  Stale  — the pre-existing SELL-parent → BUY-wing-MARKET path is
           byte-identical (no `price` kwarg sent, order_type="MARKET")
  Reuse  — `_pick_wing_by_premium` / `_wing_symbol` are called unchanged,
           not duplicated, for the new direction
  UX     — a BUY-parent leg with no resolvable live price never reaches
           the broker as a price-less LIMIT order; it is skipped with a
           clear note instead
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.algo.template_attach import (
    AttachResult,
    TemplatePlan,
    WingSpec,
    _build_wing_spec,
    _maybe_fetch_wing_quote_for_offset,
    _place_wing_leg,
    _ta_sim_place_wing,
    _wing_direction,
    apply_plan_live,
    apply_plan_sim,
    resolve_template_plan,
)


# ── _wing_direction — SSOT for both directions ─────────────────────────

def test_wing_direction_sell_option_is_buy_market():
    assert _wing_direction("SELL", "NIFTY25APR22000CE") == ("BUY", "MARKET")


def test_wing_direction_buy_option_is_sell_limit():
    assert _wing_direction("BUY", "NIFTY25APR22000CE") == ("SELL", "LIMIT")


def test_wing_direction_none_for_futures_either_side():
    assert _wing_direction("BUY", "NIFTY26JUNFUT") is None
    assert _wing_direction("SELL", "NIFTY26JUNFUT") is None


def test_wing_direction_none_for_equity():
    assert _wing_direction("BUY", "RELIANCE") is None
    assert _wing_direction("SELL", "RELIANCE") is None


# ── resolve_template_plan — BUY-parent offset LIMIT, picked-symbol path ──

def test_resolve_plan_buy_option_offset_wing_from_picked_symbol():
    """BUY-parent + pre-resolved _wing_picked_symbol/_wing_picked_ltp
    (as apply_template_to_order seeds after the chain scan or the
    manual-offset quote lookup) → a real SELL/LIMIT WingSpec, price
    tick-snapped from the picked live LTP."""
    template = {"wing_strike_offset": None, "wing_premium_pct": 25.0, "tp_pct": 10.0}
    overrides = {
        "_wing_picked_symbol": "NIFTY25APR22500CE",
        "_wing_picked_ltp": 12.37,
    }
    plan = resolve_template_plan(
        template, overrides,
        parent_account="ACC1",
        parent_symbol="NIFTY25APR22000CE",
        parent_side="BUY",
        parent_qty=50,
        parent_exchange="NFO",
        parent_fill_price=100.0,
        parent_tick_size=0.05,
    )

    assert plan.wing is not None, "Offset leg should attach for BUY option"
    assert plan.wing.tradingsymbol == "NIFTY25APR22500CE"
    assert plan.wing.transaction_type == "SELL"
    assert plan.wing.order_type == "LIMIT"
    assert plan.wing.quantity == 50
    # 12.37 snapped to the 0.05 tick grid → 12.35 (round-half-to-even on
    # the scaled integer, matching `_snap_to_tick`'s own convention).
    assert plan.wing.limit_price == 12.35, plan.wing.limit_price
    assert plan.wing.estimated_price == 12.35


def test_resolve_plan_buy_option_offset_wing_pe_direction():
    """PE parent — same picked-symbol mechanism, direction unaffected by
    opt type (strike-direction math lives in `_wing_symbol`, untouched)."""
    overrides = {
        "_wing_picked_symbol": "NIFTY25APR21500PE",
        "_wing_picked_ltp": 40.0,
    }
    plan = resolve_template_plan(
        {"wing_strike_offset": None, "wing_premium_pct": 20.0}, overrides,
        parent_account="ACC1",
        parent_symbol="NIFTY25APR22000PE",
        parent_side="BUY",
        parent_qty=50,
        parent_exchange="NFO",
        parent_fill_price=200.0,
    )
    assert plan.wing is not None
    assert plan.wing.transaction_type == "SELL"
    assert plan.wing.order_type == "LIMIT"
    assert plan.wing.limit_price == 40.0


def test_resolve_plan_buy_option_offset_wing_no_price_skips():
    """BUY parent, picked symbol resolved but with NO usable LTP (e.g.
    quote returned 0/absent) → never build a price-less LIMIT WingSpec.
    Note explains why."""
    overrides = {
        "_wing_picked_symbol": "NIFTY25APR22500CE",
        "_wing_picked_ltp": 0.0,
    }
    plan = resolve_template_plan(
        {"wing_strike_offset": None, "wing_premium_pct": 25.0}, overrides,
        parent_account="ACC1",
        parent_symbol="NIFTY25APR22000CE",
        parent_side="BUY",
        parent_qty=50,
        parent_exchange="NFO",
        parent_fill_price=100.0,
    )
    assert plan.wing is None
    assert any("no valid live price" in n for n in plan.notes)


def test_resolve_plan_buy_option_manual_offset_without_picked_symbol_skips():
    """BUY parent with a manual wing_strike_offset but NO pre-resolved
    live quote (apply_template_to_order's offset-quote lookup either
    didn't run or failed) → no wing attached; never falls back to a
    cosmetic estimate as a real LIMIT price, never falls back to MARKET."""
    plan = resolve_template_plan(
        {"wing_strike_offset": 500, "wing_premium_pct": 20.0}, {},
        parent_account="ACC1",
        parent_symbol="NIFTY25APR22000CE",
        parent_side="BUY",
        parent_qty=50,
        parent_exchange="NFO",
        parent_fill_price=100.0,
    )
    assert plan.wing is None
    assert any("LIMIT offset leg" in n for n in plan.notes)


def test_resolve_plan_sell_option_hedge_unaffected():
    """Regression — the existing SELL-parent → BUY-wing-MARKET path is
    byte-identical: estimated_price from the picked LTP directly (no
    tick-snap applied on this branch), limit_price stays None."""
    overrides = {
        "_wing_picked_symbol": "NIFTY25APR22500CE",
        "_wing_picked_ltp": 45.50,
    }
    plan = resolve_template_plan(
        {"wing_strike_offset": 500}, overrides,
        parent_account="ACC1",
        parent_symbol="NIFTY25APR22000CE",
        parent_side="SELL",
        parent_qty=50,
        parent_exchange="NFO",
        parent_fill_price=125.0,
        parent_tick_size=0.05,
    )
    assert plan.wing is not None
    assert plan.wing.transaction_type == "BUY"
    assert plan.wing.order_type == "MARKET"
    assert plan.wing.estimated_price == 45.50
    assert plan.wing.limit_price is None


# ── _maybe_fetch_wing_quote_for_offset — manual-offset live quote ──────

@pytest.mark.asyncio
async def test_maybe_fetch_wing_quote_for_offset_seeds_overrides():
    """BUY parent + manual wing_strike_offset (no wing_premium_pct scan
    already ran) → fetches ONE live quote for the computed offset strike
    and seeds _wing_picked_symbol/_wing_picked_ltp, same channel the
    premium scan uses."""
    template = {"wing_strike_offset": 500, "wing_premium_pct": None}
    quote_payload = {"NFO:NIFTY25APR22500CE": {"last_price": 18.25}}

    with patch(
        "backend.api.algo.template_attach._wing_fetch_quotes",
        new=AsyncMock(return_value=(quote_payload, None)),
    ) as _mock_fetch:
        overrides, note, skip = await _maybe_fetch_wing_quote_for_offset(
            template, {}, "BUY", "NIFTY25APR22000CE", "NFO",
            parent_order_id=7,
        )

    assert skip is None
    assert overrides["_wing_picked_symbol"] == "NIFTY25APR22500CE"
    assert overrides["_wing_picked_ltp"] == 18.25
    assert note is not None
    # Single-symbol lookup — exactly one candidate passed through.
    _mock_fetch.assert_awaited_once()
    _candidates = _mock_fetch.call_args.args[0]
    assert _candidates == [{"exch": "NFO", "ts": "NIFTY25APR22500CE"}]


@pytest.mark.asyncio
async def test_maybe_fetch_wing_quote_for_offset_noop_for_sell_parent():
    """SELL-parent (MARKET hedge direction) never needs a live price via
    this path — no-op regardless of wing_strike_offset."""
    template = {"wing_strike_offset": 500}
    overrides, note, skip = await _maybe_fetch_wing_quote_for_offset(
        template, {}, "SELL", "NIFTY25APR22000CE", "NFO",
    )
    assert note is None and skip is None
    assert "_wing_picked_symbol" not in overrides


@pytest.mark.asyncio
async def test_maybe_fetch_wing_quote_for_offset_noop_when_already_picked():
    """Premium scan already resolved a symbol — this helper must not
    overwrite it with a second (manual-offset) lookup."""
    template = {"wing_strike_offset": 500}
    overrides_in = {"_wing_picked_symbol": "ALREADY", "_wing_picked_ltp": 99.0}
    overrides, note, skip = await _maybe_fetch_wing_quote_for_offset(
        template, overrides_in, "BUY", "NIFTY25APR22000CE", "NFO",
    )
    assert note is None and skip is None
    assert overrides["_wing_picked_symbol"] == "ALREADY"


@pytest.mark.asyncio
async def test_maybe_fetch_wing_quote_for_offset_skips_on_zero_quote():
    """Live quote unavailable/zero → skip reason set, nothing seeded,
    ntfy alert attempted (best-effort — doesn't raise on failure)."""
    template = {"wing_strike_offset": 500}
    quote_payload = {"NFO:NIFTY25APR22500CE": {"last_price": 0}}
    with patch(
        "backend.api.algo.template_attach._wing_fetch_quotes",
        new=AsyncMock(return_value=(quote_payload, None)),
    ), patch("backend.shared.helpers.alert_utils.send_ntfy_alert"):
        overrides, note, skip = await _maybe_fetch_wing_quote_for_offset(
            template, {}, "BUY", "NIFTY25APR22000CE", "NFO",
        )
    assert skip is not None
    assert "_wing_picked_symbol" not in overrides


# ── Broker-call-level — LIMIT actually reaches the SDK call ────────────

def _real_kite_broker(place_order_return="wing-live-1"):
    from backend.brokers.adapters.kite import KiteBroker
    mock_conn = MagicMock()
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = place_order_return
    mock_conn.get_kite_conn.return_value = mock_kite
    broker = KiteBroker(mock_conn)
    broker.translate_qty = lambda exch, qty, ls: qty  # NFO passthrough
    return broker, mock_kite


def test_place_wing_leg_limit_sends_price_to_kite_sdk():
    """Broker-call-level check (per task brief): a LIMIT WingSpec must
    reach KiteBroker.place_order with BOTH order_type="LIMIT" and a
    positive price= kwarg — exercising the REAL `_validate_kite_order_prices`
    guard inside KiteBroker.place_order, not a bypassed mock."""
    broker, mock_kite = _real_kite_broker()
    plan = TemplatePlan(
        template_id=5, template_name="T", template_slug="t",
        parent_account="ACC1", parent_symbol="NIFTY25APR22000CE",
        parent_side="BUY", parent_qty=50, parent_exchange="NFO",
        parent_fill_price=100.0, parent_lot_size=1,
    )
    plan.wing = WingSpec(
        tradingsymbol="NIFTY25APR22500CE",
        transaction_type="SELL",
        quantity=50,
        exchange="NFO",
        product="NRML",
        order_type="LIMIT",
        estimated_price=18.25,
        limit_price=18.25,
    )
    order_id = _place_wing_leg(broker, plan)

    assert order_id == "wing-live-1"
    assert mock_kite.place_order.called
    call_kwargs = mock_kite.place_order.call_args.kwargs
    assert call_kwargs["order_type"] == "LIMIT"
    assert call_kwargs["price"] == 18.25
    assert call_kwargs["transaction_type"] == "SELL"


def test_place_wing_leg_market_sends_no_price_kwarg():
    """Regression — the existing MARKET hedge direction sends NO `price`
    kwarg at all, byte-identical to pre-change behavior."""
    broker, mock_kite = _real_kite_broker()
    plan = TemplatePlan(
        template_id=5, template_name="T", template_slug="t",
        parent_account="ACC1", parent_symbol="NIFTY25APR22000CE",
        parent_side="SELL", parent_qty=50, parent_exchange="NFO",
        parent_fill_price=100.0, parent_lot_size=1,
    )
    plan.wing = WingSpec(
        tradingsymbol="NIFTY25APR22500CE",
        transaction_type="BUY",
        quantity=50,
        exchange="NFO",
        product="NRML",
        order_type="MARKET",
        estimated_price=20.0,
    )
    order_id = _place_wing_leg(broker, plan)

    assert order_id == "wing-live-1"
    call_kwargs = mock_kite.place_order.call_args.kwargs
    assert "price" not in call_kwargs
    assert call_kwargs["order_type"] == "MARKET"


def test_place_wing_leg_limit_without_price_raises_before_broker_call():
    """Defensive guard — a LIMIT WingSpec with no resolvable price must
    raise BEFORE the broker call, never silently reach Kite as a
    price-less LIMIT (which the real SDK would reject anyway)."""
    broker, mock_kite = _real_kite_broker()
    plan = TemplatePlan(
        template_id=5, template_name="T", template_slug="t",
        parent_account="ACC1", parent_symbol="NIFTY25APR22000CE",
        parent_side="BUY", parent_qty=50, parent_exchange="NFO",
        parent_fill_price=100.0, parent_lot_size=1,
    )
    plan.wing = WingSpec(
        tradingsymbol="NIFTY25APR22500CE",
        transaction_type="SELL",
        quantity=50,
        exchange="NFO",
        product="NRML",
        order_type="LIMIT",
        estimated_price=None,
        limit_price=None,
    )
    with pytest.raises(ValueError, match="WING-LIMIT-PRICE-GUARD"):
        _place_wing_leg(broker, plan)
    assert not mock_kite.place_order.called


def test_apply_plan_live_buy_offset_wing_end_to_end():
    """End-to-end via apply_plan_live — confirms AttachResult surfaces
    the wing order id and no errors for a well-formed BUY-parent offset
    leg."""
    broker, mock_kite = _real_kite_broker(place_order_return="wing-999")

    plan = TemplatePlan(
        template_id=9, template_name="T9", template_slug="t9",
        parent_account="ACC1", parent_symbol="NIFTY25APR22000CE",
        parent_side="BUY", parent_qty=50, parent_exchange="NFO",
        parent_fill_price=100.0, parent_lot_size=1,
        gtts=[],
    )
    plan.wing = WingSpec(
        tradingsymbol="NIFTY25APR22500CE",
        transaction_type="SELL",
        quantity=50,
        exchange="NFO",
        product="NRML",
        order_type="LIMIT",
        estimated_price=18.25,
        limit_price=18.25,
    )

    with patch(
        "backend.api.algo.agent_engine._symbol_exchange_open", return_value=True,
    ), patch(
        "backend.api.algo.agent_engine._build_now_ctx", return_value={},
    ):
        result = apply_plan_live(plan, broker)

    assert not result.errors, result.errors
    assert result.wing_order_id == "wing-999"
    call_kwargs = mock_kite.place_order.call_args.kwargs
    assert call_kwargs["order_type"] == "LIMIT"
    assert call_kwargs["price"] == 18.25


# ── Sim path — LIMIT offset leg behaves as a real resting limit ───────

def test_sim_place_wing_limit_sets_side_qty_limit_price():
    """LIMIT offset direction registers side/qty/limit_price explicitly
    so PaperTradeEngine evaluates a genuine resting SELL limit instead
    of the MARKET-direction's implicit-fill quirk."""
    plan = TemplatePlan(
        template_id=1, template_name="T", template_slug="t",
        parent_account="ACC1", parent_symbol="NIFTY25APR22000CE",
        parent_side="BUY", parent_qty=50, parent_exchange="NFO",
        parent_fill_price=100.0, parent_lot_size=1,
    )
    plan.wing = WingSpec(
        tradingsymbol="NIFTY25APR22500CE",
        transaction_type="SELL",
        quantity=50,
        exchange="NFO",
        product="NRML",
        order_type="LIMIT",
        estimated_price=18.25,
        limit_price=18.25,
    )
    driver = MagicMock()
    result = AttachResult(plan=plan)
    _ta_sim_place_wing(driver, plan, result)

    assert driver.register_open_order.called
    registered = driver.register_open_order.call_args.args[0]
    assert registered["side"] == "SELL"
    assert registered["qty"] == 50
    assert registered["limit_price"] == 18.25
    assert result.wing_order_id is not None


def test_sim_place_wing_limit_does_not_fill_below_bid():
    """Integration with the real PaperTradeEngine fill-eval helper —
    a SELL LIMIT @ 18.25 must NOT fill when the best bid is below it."""
    from backend.api.algo.paper import _paper_is_fillable
    fillable, _ = _paper_is_fillable("SELL", bid=18.00, ask=18.10, limit=18.25)
    assert fillable is False
    fillable2, fill_price = _paper_is_fillable("SELL", bid=18.30, ask=18.40, limit=18.25)
    assert fillable2 is True
    assert fill_price == 18.30


def test_sim_place_wing_market_direction_unaffected():
    """Regression — the existing MARKET hedge direction still registers
    without side/qty/limit_price keys (the pre-existing market-take
    sim approximation, untouched)."""
    plan = TemplatePlan(
        template_id=1, template_name="T", template_slug="t",
        parent_account="ACC1", parent_symbol="NIFTY25APR22000CE",
        parent_side="SELL", parent_qty=50, parent_exchange="NFO",
        parent_fill_price=100.0, parent_lot_size=1,
    )
    plan.wing = WingSpec(
        tradingsymbol="NIFTY25APR22500CE",
        transaction_type="BUY",
        quantity=50,
        exchange="NFO",
        product="NRML",
        order_type="MARKET",
        estimated_price=20.0,
    )
    driver = MagicMock()
    result = AttachResult(plan=plan)
    _ta_sim_place_wing(driver, plan, result)

    registered = driver.register_open_order.call_args.args[0]
    assert "side" not in registered
    assert "limit_price" not in registered
