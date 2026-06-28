"""
Dhan holdings normaliser — fallback computation tests.

History: Dhan's holdings endpoint returns avgCostPrice + lastTradedPrice
reliably but frequently omits previousClosePrice / unrealisedProfit /
dayChange (or returns them as 0). Without a derivation:
  close_price = 0  → day_change_pct = -100 % across every Dhan row
  pnl         = 0  → P&L column reads 0 even on big movers

Production data observed (DH3747 / TEJASNET): qty=15, avg=1332.9,
last=498.3 — but pnl came through as 0 and close_price as 0. The
normaliser now derives both when Dhan omits them.
"""

from __future__ import annotations

from backend.brokers.adapters.dhan import _normalise_holdings


def test_holdings_derives_pnl_when_omitted():
    # Replicates the DH3747 / TEJASNET production payload: only the
    # three base fields populated, everything else missing.
    resp = [{
        "tradingSymbol":  "TEJASNET",
        "exchange":       "NSE",
        "securityId":     "21131",
        "totalQty":       15,
        "avgCostPrice":   1332.9,
        "lastTradedPrice": 498.3,
    }]
    rows = _normalise_holdings({"data": resp})
    assert len(rows) == 1
    r = rows[0]
    assert r["quantity"] == 15
    assert r["average_price"] == 1332.9
    assert r["last_price"] == 498.3
    # Derived pnl: (498.3 - 1332.9) × 15 = -12519
    assert abs(r["pnl"] - (-12519.0)) < 0.01
    # When previousClosePrice is missing, close_price is left at 0 so
    # the broker_apis.backfill_market_data helper can batch-fetch a
    # real prior-day close via PriceBroker.quote() across every missing
    # row. Pre-fix fallback set close_price = last_price → day_change=0
    # which silently masked these rows from the backfill mask. Contract
    # changed Jun 2026 (audit fix); see _normalise_holdings docstring.
    assert r["close_price"] == 0.0
    # day_change is per-share delta (per Kite convention & CLAUDE.md).
    # When close_price=0, it's computed as (last_price - 0) = last_price
    # per-share. Downstream: day_change_val = day_change * qty.
    # The broker_apis.backfill_market_data pass recomputes day_change
    # after patching close_price from a PriceBroker.quote() call.
    assert abs(r["day_change"] - 498.3) < 0.01
    # day_change_percentage IS gated on close_price > 0 (avoid div-by-zero)
    # so the operator-facing % column reads 0 until backfill lands.
    assert r["day_change_percentage"] == 0.0


def test_holdings_uses_dhan_values_when_present():
    # When Dhan returns close_price + pnl + day_pct_raw, use them for
    # those fields. However, day_change is ALWAYS recomputed from
    # (last - close) to ensure per-share semantics (never qty-multiplied).
    # Dhan's dayChange field is TOTAL (qty-scaled) so we ignore it.
    resp = [{
        "tradingSymbol":  "RELIANCE",
        "exchange":       "NSE",
        "securityId":     "2885",
        "totalQty":       10,
        "avgCostPrice":   2400.0,
        "lastTradedPrice": 2500.0,
        "previousClosePrice": 2480.0,
        "unrealisedProfit": 999.99,    # operator-specific, not derived
        "dayChange":      200.0,       # IGNORED — we recompute per-share
        "dayChangePerc":  0.81,        # passed through as-is
    }]
    rows = _normalise_holdings({"data": resp})
    r = rows[0]
    assert r["close_price"] == 2480.0
    assert r["pnl"] == 999.99      # passed through, not derived
    # day_change = last - close = 2500 - 2480 = 20.0 per-share
    assert r["day_change"] == 20.0
    # day_change_percentage passed through from Dhan
    assert r["day_change_percentage"] == 0.81


def test_holdings_derives_day_change_with_close_price():
    # close_price IS present, day_change isn't. Derive day_change from
    # per-share difference (last - close), NOT qty-multiplied.
    resp = [{
        "tradingSymbol":  "TCS",
        "exchange":       "NSE",
        "totalQty":       5,
        "avgCostPrice":   3500.0,
        "lastTradedPrice": 3600.0,
        "previousClosePrice": 3550.0,
    }]
    rows = _normalise_holdings({"data": resp})
    r = rows[0]
    assert r["close_price"] == 3550.0
    # day_change = last - close = 3600 - 3550 = 50 per-share
    # downstream: day_change_val = 50 × 5 = 250
    assert abs(r["day_change"] - 50.0) < 0.01
    # day_change_pct = (3600 - 3550) / 3550 × 100 = 1.4084 %
    assert abs(r["day_change_percentage"] - 1.408450) < 0.001
    # pnl = (3600 - 3500) × 5 = 500 (derived since unrealisedProfit absent)
    assert abs(r["pnl"] - 500.0) < 0.01


def test_holdings_zero_qty_does_not_blow_up():
    resp = [{
        "tradingSymbol":  "SOLDOFF",
        "totalQty":       0,
        "avgCostPrice":   100.0,
        "lastTradedPrice": 95.0,
    }]
    rows = _normalise_holdings({"data": resp})
    r = rows[0]
    assert r["quantity"] == 0
    assert r["pnl"] == 0.0      # (95 - 100) × 0


def test_holdings_empty_response_returns_empty_list():
    assert _normalise_holdings([]) == []
    assert _normalise_holdings(None) == []
    assert _normalise_holdings({"data": []}) == []
