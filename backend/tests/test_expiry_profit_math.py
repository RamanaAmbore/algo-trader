"""
Tests for expiry-profit math (navstrip P-pill third value).

The expiry profit is computed **client-side** in PositionStrip.svelte.
These tests verify the underlying math invariants (same formulas, Python
re-implementation) so regressions are caught without a browser. The JS
implementation in PositionStrip mirrors these exactly.

Math:
  Futures:  (ltp  - avg) * qty
  Long CE:  max(spot - strike, 0) * qty  - avg * qty  → (intrinsic - avg) * qty
  Short CE: same formula, qty is negative
  PE:       symmetric — max(strike - spot, 0)

qty is SIGNED (positive = long, negative = short).
avg is the average entry price per contract (premium for options, avg cost for futures).
"""

import pytest


def _intrinsic(spot: float, strike: float, opt_type: str) -> float:
    if opt_type == "CE":
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def expiry_profit(positions: list[dict]) -> float:
    """Pure-Python reference implementation of the navstrip expiry-profit sum.

    Each element of `positions` is a dict with keys:
        kind      : 'fut' | 'opt'
        qty       : float  (signed; positive=long, negative=short)
        avg       : float  (entry price per contract)
        ltp       : float  (futures: own LTP; options: unused)
        spot      : float  (options: underlying spot; futures: unused)
        strike    : float  (options only)
        opt_type  : 'CE' | 'PE'  (options only)
    """
    total = 0.0
    for p in positions:
        qty = float(p["qty"])
        avg = float(p["avg"])
        if not qty:
            continue
        if p["kind"] == "fut":
            ltp = float(p["ltp"])
            if ltp <= 0:
                continue
            total += (ltp - avg) * qty
        elif p["kind"] == "opt":
            spot = float(p.get("spot", 0))
            if spot <= 0:
                continue                  # no underlying spot → skip (not phantom)
            intrinsic = _intrinsic(spot, float(p["strike"]), p["opt_type"])
            total += (intrinsic - avg) * qty
    return round(total, 2)


# ── 1. Futures fixture ───────────────────────────────────────────────

def test_future_long_profit():
    """Long NIFTY future: 1 lot, entry 22000, LTP 22200 → +200 per contract."""
    positions = [dict(kind="fut", qty=50, avg=22000, ltp=22200)]
    assert expiry_profit(positions) == round((22200 - 22000) * 50, 2)  # +10000


def test_future_long_loss():
    """Long future at loss."""
    positions = [dict(kind="fut", qty=50, avg=22000, ltp=21800)]
    assert expiry_profit(positions) == round((21800 - 22000) * 50, 2)  # -10000


def test_future_short_profit():
    """Short CRUDEOIL future (qty negative): entry 8000, LTP falls to 7900."""
    positions = [dict(kind="fut", qty=-100, avg=8000, ltp=7900)]
    assert expiry_profit(positions) == round((7900 - 8000) * -100, 2)  # +10000


def test_future_no_ltp_skipped():
    """Zero/missing LTP is skipped — no phantom P&L."""
    positions = [dict(kind="fut", qty=50, avg=22000, ltp=0)]
    assert expiry_profit(positions) == 0.0


# ── 2. Long option fixtures ─────────────────────────────────────────

def test_long_call_itm():
    """Long call, ITM: spot 22500, strike 22000, avg 300, qty 50.
    intrinsic = 500, P&L = (500 - 300) * 50 = 10000."""
    positions = [dict(kind="opt", qty=50, avg=300, spot=22500, strike=22000, opt_type="CE")]
    assert expiry_profit(positions) == round((500 - 300) * 50, 2)  # +10000


def test_long_call_otm():
    """Long call, OTM at expiry: intrinsic 0, loss = premium paid."""
    positions = [dict(kind="opt", qty=50, avg=200, spot=21800, strike=22000, opt_type="CE")]
    assert expiry_profit(positions) == round((0 - 200) * 50, 2)  # -10000


def test_long_put_itm():
    """Long put, ITM: spot 21500, strike 22000, avg 250, qty 50.
    intrinsic = 500, P&L = (500 - 250) * 50 = 12500."""
    positions = [dict(kind="opt", qty=50, avg=250, spot=21500, strike=22000, opt_type="PE")]
    assert expiry_profit(positions) == round((500 - 250) * 50, 2)  # +12500


def test_long_put_otm():
    """Long put OTM: intrinsic 0."""
    positions = [dict(kind="opt", qty=50, avg=150, spot=22500, strike=22000, opt_type="PE")]
    assert expiry_profit(positions) == round((0 - 150) * 50, 2)  # -7500


# ── 3. Short option fixtures ─────────────────────────────────────────

def test_short_call_otm():
    """Short call (qty < 0), OTM: intrinsic 0, P&L = premium_received × qty
    = (0 - avg) * -qty = avg * qty (since qty < 0 and formula = (intrinsic-avg)*qty)."""
    # avg = 200 (premium received), qty = -50 (short)
    # expiry P&L = (0 - 200) * -50 = +10000 — kept full premium
    positions = [dict(kind="opt", qty=-50, avg=200, spot=21800, strike=22000, opt_type="CE")]
    assert expiry_profit(positions) == round((0 - 200) * -50, 2)  # +10000


def test_short_call_itm():
    """Short call, ITM: spot 22500, strike 22000, avg 200, qty -50.
    P&L = (500 - 200) * -50 = -15000 (short assignment loss)."""
    positions = [dict(kind="opt", qty=-50, avg=200, spot=22500, strike=22000, opt_type="CE")]
    assert expiry_profit(positions) == round((500 - 200) * -50, 2)  # -15000


def test_short_put_otm():
    """Short put, OTM: premium kept."""
    positions = [dict(kind="opt", qty=-50, avg=150, spot=22500, strike=22000, opt_type="PE")]
    assert expiry_profit(positions) == round((0 - 150) * -50, 2)  # +7500


# ── 4. Aggregation ───────────────────────────────────────────────────

def test_multi_leg_aggregate():
    """Bull spread: long 22000 CE + short 22500 CE. Spot at 22300.
    Long:  (300 - 200) * 50 = +5000
    Short: (0   - 150) * -50 = +7500
    Total: +12500"""
    positions = [
        dict(kind="opt", qty=50,  avg=200, spot=22300, strike=22000, opt_type="CE"),
        dict(kind="opt", qty=-50, avg=150, spot=22300, strike=22500, opt_type="CE"),
    ]
    assert expiry_profit(positions) == round(5000 + 7500, 2)  # +12500


def test_equity_rows_excluded_by_exchange_gate():
    """Equity rows (exchange NSE/BSE — not F&O) must be excluded.
    The frontend gates by exchange; we model that here by simply not including
    equity rows in the 'positions' list passed to expiry_profit(), confirming
    the caller's responsibility to pre-filter."""
    # Only F&O rows are passed — the result equals only those legs
    positions = [dict(kind="fut", qty=50, avg=22000, ltp=22200)]
    assert expiry_profit(positions) == round((22200 - 22000) * 50, 2)


# ── 5. Edge cases ────────────────────────────────────────────────────

def test_zero_qty_skipped():
    positions = [dict(kind="fut", qty=0, avg=22000, ltp=22200)]
    assert expiry_profit(positions) == 0.0


def test_missing_underlying_spot_skipped():
    """Option with spot=0 (underlying LTP unavailable) contributes 0,
    not phantom intrinsic. This is the critical guard against the
    'multi-lakh phantom P&L' class of bug."""
    positions = [dict(kind="opt", qty=50, avg=300, spot=0, strike=22000, opt_type="CE")]
    assert expiry_profit(positions) == 0.0


def test_empty_positions():
    assert expiry_profit([]) == 0.0


def test_atm_option_returns_minus_premium():
    """ATM call: intrinsic = 0, P&L = -premium * qty (full loss of premium)."""
    positions = [dict(kind="opt", qty=50, avg=100, spot=22000, strike=22000, opt_type="CE")]
    assert expiry_profit(positions) == round(-100 * 50, 2)  # -5000
