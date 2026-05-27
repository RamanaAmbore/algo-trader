"""
Item 1 / Phase 25 — expiry-aware grammar resolver tests.

Covers:
  _metric_days_until_expiry — parses tradingsymbol, returns days as float
  _metric_is_itm            — 1.0 ITM / 0.0 OTM, None when spot missing
  _metric_is_ntm            — 1.0 within ±1.5% / 0.0 otherwise
  _scope_positions_expiring_today — filters per-symbol rows
"""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

import pandas as pd

from backend.api.algo import grammar
from backend.api.algo.agent_evaluator import Context


def _ctx(*, position_rows=None, spot_prices=None, now=None) -> Context:
    return Context(
        sum_positions=pd.DataFrame(),
        position_rows=position_rows or [],
        spot_prices=spot_prices or {},
        now=now or datetime.now(timezone.utc),
    )


# ── days_until_expiry ──────────────────────────────────────────────

def test_days_until_expiry_parses_monthly_option():
    # NIFTY 25-APR-22000 CE (monthly) — expiry = last Thursday of April
    # 2025 = 24th April 2025.
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    ctx = _ctx(now=datetime(2025, 4, 24, 9, 0, tzinfo=timezone.utc))  # ~14:30 IST
    d = grammar._metric_days_until_expiry(ctx, row)
    assert d is not None
    # 24 Apr 2025 expiry at 15:30 IST; ref 09:00 UTC = 14:30 IST → ~1 hour to expiry
    assert 0 < d < 0.2


def test_days_until_expiry_floors_after_expiry():
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    ctx = _ctx(now=datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc))
    d = grammar._metric_days_until_expiry(ctx, row)
    assert d == 0.0


def test_days_until_expiry_none_for_equity():
    row = {"tradingsymbol": "RELIANCE", "exchange": "NSE"}
    assert grammar._metric_days_until_expiry(_ctx(), row) is None


def test_days_until_expiry_mcx_uses_2330_close():
    # CRUDEOILM expiry — MCX trades till 23:30 so an option expiring
    # today has more time-to-expiry than an NFO option at the same
    # wall-clock time.
    row = {"tradingsymbol": "CRUDEOILM25MAY5500CE", "exchange": "MCX"}
    # 29 May 2025 — last Thursday of May 2025. At 12:00 UTC = 17:30 IST,
    # 6 hours to the 23:30 IST MCX close → days_to_expiry ≈ 0.25.
    ctx = _ctx(now=datetime(2025, 5, 29, 12, 0, tzinfo=timezone.utc))
    d = grammar._metric_days_until_expiry(ctx, row)
    assert d is not None and 0.2 < d < 0.3


# ── is_itm ──────────────────────────────────────────────────────────

def test_is_itm_call_above_strike():
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 22150.0})
    assert grammar._metric_is_itm(ctx, row) == 1.0


def test_is_itm_call_below_strike():
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 21800.0})
    assert grammar._metric_is_itm(ctx, row) == 0.0


def test_is_itm_put_below_strike():
    row = {"tradingsymbol": "NIFTY25APR22000PE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 21800.0})
    assert grammar._metric_is_itm(ctx, row) == 1.0


def test_is_itm_put_above_strike():
    row = {"tradingsymbol": "NIFTY25APR22000PE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 22150.0})
    assert grammar._metric_is_itm(ctx, row) == 0.0


def test_is_itm_none_without_spot():
    row = {"tradingsymbol": "NIFTY25APR22000CE"}
    assert grammar._metric_is_itm(_ctx(), row) is None  # spot_prices empty


def test_is_itm_none_for_future():
    row = {"tradingsymbol": "NIFTY25APRFUT"}
    ctx = _ctx(spot_prices={"NIFTY": 22000.0})
    assert grammar._metric_is_itm(ctx, row) is None  # futures have no strike


# ── is_ntm ──────────────────────────────────────────────────────────

def test_is_ntm_within_band():
    # Spot 22000, strike 22050 → 0.227% away → NTM
    row = {"tradingsymbol": "NIFTY25APR22050CE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 22000.0})
    assert grammar._metric_is_ntm(ctx, row) == 1.0


def test_is_ntm_outside_band():
    # Spot 22000, strike 22500 → 2.27% away → outside ±1.5%
    row = {"tradingsymbol": "NIFTY25APR22500CE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 22000.0})
    assert grammar._metric_is_ntm(ctx, row) == 0.0


# ── positions.expiring_today scope ─────────────────────────────────

def test_scope_expiring_today_filters_to_today():
    # Build a fake position book: one expiring today, one next month,
    # one cash equity. Only the first should appear.
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        {"tradingsymbol": "NIFTY25MAY22500CE", "exchange": "NFO", "quantity": 50},
        {"tradingsymbol": "RELIANCE",          "exchange": "NSE", "quantity": 10},
    ])
    rows = grammar._scope_positions_expiring_today(ctx)
    assert len(rows) == 1
    assert rows[0]["tradingsymbol"] == "NIFTY25APR22000CE"


def test_scope_expiring_today_empty_when_no_position_rows():
    ctx = _ctx(position_rows=[])
    assert grammar._scope_positions_expiring_today(ctx) == []


# ── SYSTEM_TOKENS catalog wiring ───────────────────────────────────

def test_new_tokens_registered_in_system_catalog():
    tokens = {t['token'] for t in grammar.SYSTEM_TOKENS}
    for k in ('days_until_expiry', 'is_itm', 'is_ntm',
              'positions.expiring_today'):
        assert k in tokens, f"missing system token: {k}"
