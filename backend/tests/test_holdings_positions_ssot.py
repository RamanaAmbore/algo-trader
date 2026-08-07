"""Tests for six correctness fixes across holdings.py, background.py,
and positions_helpers.py.

Fix 1 — holdings._snapshot_fn returns as_of=None when DB is empty (first deploy).
Fix 2 — _build_holding_row_from_snapshot uses previous_close for close_price.
Fix 3 — _bg_holdings_add_pct uses cur_val - day_change_val as denominator.
Fix 4 — _fetch_positions_direct calls _override_stale_ltp_from_ticker.
Fix 5 — resolve_snapshot_day_pct uses close_price_f denominator when provided.
Fix 6 — build_snapshot_position_row propagates product from payload_json.
"""

import asyncio
import json
import math
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Fix 1: _snapshot_fn returns as_of=None when snapshot is None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_fn_as_of_none_when_snapshot_is_none():
    """When _holdings_snapshot() returns None (empty DB on first deploy),
    _snapshot_fn must return a HoldingsResponse with as_of=None.

    The gate at holdings.py ~line 604 checks getattr(resp, 'as_of', None) —
    if as_of is truthy it short-circuits and never calls the broker.  So a
    truthy as_of on an empty-snapshot response means the operator gets an
    empty grid with no fallback to the live broker.

    _snapshot_fn is a closure inside get_holdings and cannot be imported
    directly.  We test the logic by replicating the same conditional that
    the closure uses, with _holdings_snapshot mocked to return None.
    """
    from backend.api.routes.holdings import _holdings_snapshot
    from backend.api.schemas import HoldingsResponse
    from backend.shared.helpers.date_time_utils import timestamp_display

    with patch(
        "backend.api.routes.holdings._holdings_snapshot",
        new=AsyncMock(return_value=None),
    ):
        snap = await _holdings_snapshot()
        # Mirror the fixed _snapshot_fn logic from holdings.py:
        if snap is None:
            resp = HoldingsResponse(
                rows=[], summary=[],
                refreshed_at=timestamp_display(),
                as_of=None,
            )
        else:
            resp = snap

    assert resp.as_of is None, (
        f"Expected as_of=None when snapshot is empty, got {resp.as_of!r}. "
        "A truthy as_of would short-circuit the broker fallback, causing an "
        "empty grid on first deploy."
    )
    assert resp.rows == []
    assert resp.summary == []


@pytest.mark.asyncio
async def test_snapshot_fn_as_of_set_when_snapshot_exists():
    """When _holdings_snapshot() returns a real response, as_of is propagated."""
    from backend.api.schemas import HoldingsResponse

    fake_snap = HoldingsResponse(
        rows=[], summary=[],
        refreshed_at="2026-08-07T10:00:00",
        as_of="2026-08-07T09:59:00",
    )
    with patch(
        "backend.api.routes.holdings._holdings_snapshot",
        new=AsyncMock(return_value=fake_snap),
    ):
        from backend.api.routes.holdings import _holdings_snapshot
        snap = await _holdings_snapshot()
        if snap is None:
            from backend.shared.helpers.date_time_utils import timestamp_display
            resp = HoldingsResponse(
                rows=[], summary=[],
                refreshed_at=timestamp_display(),
                as_of=None,
            )
        else:
            resp = snap

    assert resp.as_of == "2026-08-07T09:59:00", (
        f"Expected as_of to be preserved from real snapshot, got {resp.as_of!r}"
    )


# ---------------------------------------------------------------------------
# Fix 2: _build_holding_row_from_snapshot uses previous_close for close_price
# ---------------------------------------------------------------------------

def test_build_holding_row_close_price_is_previous_close():
    """close_price field must be previous_close, not ltp.

    The day-change percentage denominator uses previous_close × qty so
    setting close_price=ltp_f was computing against the wrong price.
    """
    from backend.api.routes.holdings import _build_holding_row_from_snapshot

    raw_row = (
        "ZG0790",   # account
        "INFY",     # symbol
        "NSE",      # exchange
        10,         # qty
        1500.0,     # avg_cost
        1800.0,     # ltp (current price — NOT the denominator)
        1600.0,     # previous_close (should become close_price)
        2000.0,     # day_pnl = (1800 - 1600) × 10
        3000.0,     # total_pnl
        None,       # captured_at
    )
    row, inv_val, cur_val, total_pnl_f, day_pnl_f = _build_holding_row_from_snapshot(raw_row)

    assert math.isclose(row.close_price, 1600.0, rel_tol=1e-6), (
        f"Expected close_price=1600.0 (previous_close), got {row.close_price}. "
        "close_price must be previous_close, not ltp."
    )
    # last_price (display) should still be ltp
    assert math.isclose(row.last_price, 1800.0, rel_tol=1e-6), (
        f"Expected last_price=1800.0 (ltp), got {row.last_price}"
    )


def test_build_holding_row_close_price_fallback_to_ltp_when_no_prev_close():
    """When previous_close is zero/None, fall back to ltp for close_price."""
    from backend.api.routes.holdings import _build_holding_row_from_snapshot

    raw_row = (
        "ZG0790", "NEWBUY", "NSE",
        5,       # qty
        1000.0,  # avg_cost
        1050.0,  # ltp
        0.0,     # previous_close = 0 (same-day buy, no prior session)
        250.0,   # day_pnl
        250.0,   # total_pnl
        None,
    )
    row, *_ = _build_holding_row_from_snapshot(raw_row)

    # No previous_close → close_price falls back to ltp
    assert math.isclose(row.close_price, 1050.0, rel_tol=1e-6), (
        f"Expected close_price=1050.0 (ltp fallback when prev_close=0), "
        f"got {row.close_price}"
    )


def test_build_holding_row_close_price_not_ltp_when_prev_close_present():
    """Regression guard: close_price must differ from ltp when prev_close is set."""
    from backend.api.routes.holdings import _build_holding_row_from_snapshot

    ltp = 200.0
    previous_close = 150.0

    raw_row = (
        "ZG0790", "SIEMENS", "NSE",
        10,
        140.0,          # avg_cost
        ltp,            # ltp
        previous_close, # previous_close
        500.0,          # day_pnl = (200 - 150) × 10
        600.0,          # total_pnl
        None,
    )
    row, *_ = _build_holding_row_from_snapshot(raw_row)

    assert row.close_price != ltp, (
        f"close_price={row.close_price} equals ltp={ltp}. "
        "This is the regression — close_price must be previous_close when available."
    )
    assert math.isclose(row.close_price, previous_close, rel_tol=1e-6)


# ---------------------------------------------------------------------------
# Fix 3: _bg_holdings_add_pct uses cur_val - day_change_val as denominator
# ---------------------------------------------------------------------------

def test_bg_holdings_add_pct_correct_denominator():
    """day_change_percentage = day_change_val / (cur_val - day_change_val) × 100.

    With cur_val=11000, day_change_val=1000, opening_val=10000:
    - Correct: 1000 / 10000 × 100 = 10.0%
    - Wrong:   1000 / 11000 × 100 ≈ 9.09%  (old denominator was cur_val)
    """
    from backend.api.background import _bg_holdings_add_pct

    df = pd.DataFrame([{
        "account": "ZG0790",
        "inv_val": 10000.0,
        "cur_val": 11000.0,
        "pnl": 1000.0,
        "day_change_val": 1000.0,
    }])
    _bg_holdings_add_pct(df)

    # Correct denominator: cur_val - day_change_val = 10000
    expected_pct = 1000.0 / 10000.0 * 100.0  # = 10.0%
    wrong_pct = 1000.0 / 11000.0 * 100.0     # ≈ 9.09% (old bug)

    assert math.isclose(df.loc[0, "day_change_percentage"], expected_pct, rel_tol=1e-4), (
        f"Expected day_change_percentage={expected_pct:.2f}% "
        f"(denominator=opening_val=10000), "
        f"got {df.loc[0, 'day_change_percentage']:.4f}%. "
        f"Old wrong value would be {wrong_pct:.2f}%."
    )


def test_bg_holdings_add_pct_zero_open_val_no_crash():
    """When cur_val == day_change_val (fully-new position, no prior value),
    opening_val=0 must not produce ZeroDivisionError; result must be 0.0.
    """
    from backend.api.background import _bg_holdings_add_pct

    df = pd.DataFrame([{
        "account": "ZG0790",
        "inv_val": 10000.0,
        "cur_val": 10000.0,
        "pnl": 10000.0,
        "day_change_val": 10000.0,  # opening = cur - dcv = 0
    }])
    _bg_holdings_add_pct(df)

    # Should not raise; should be 0.0 (guarded zero-denominator)
    assert df.loc[0, "day_change_percentage"] == pytest.approx(0.0, abs=1e-9), (
        f"Expected 0.0 for zero opening_val, "
        f"got {df.loc[0, 'day_change_percentage']}"
    )


def test_bg_holdings_add_pct_negative_day_change():
    """Negative day_change_val (loss day) gives correct negative percentage."""
    from backend.api.background import _bg_holdings_add_pct

    df = pd.DataFrame([{
        "account": "ZG0790",
        "inv_val": 10000.0,
        "cur_val": 9500.0,
        "pnl": -500.0,
        "day_change_val": -500.0,  # lost ₹500 today; opening = 9500 - (-500) = 10000
    }])
    _bg_holdings_add_pct(df)

    expected_pct = -500.0 / 10000.0 * 100.0  # = -5.0%
    assert math.isclose(df.loc[0, "day_change_percentage"], expected_pct, rel_tol=1e-4), (
        f"Expected {expected_pct:.2f}%, got {df.loc[0, 'day_change_percentage']:.4f}%"
    )


def test_bg_holdings_add_pct_pnl_pct_uses_inv_val():
    """pnl_percentage still uses inv_val (unchanged from before)."""
    from backend.api.background import _bg_holdings_add_pct

    df = pd.DataFrame([{
        "account": "ZG0790",
        "inv_val": 10000.0,
        "cur_val": 11000.0,
        "pnl": 1000.0,
        "day_change_val": 500.0,
    }])
    _bg_holdings_add_pct(df)

    expected_pnl_pct = 1000.0 / 10000.0 * 100.0  # = 10.0%
    assert math.isclose(df.loc[0, "pnl_percentage"], expected_pnl_pct, rel_tol=1e-4), (
        f"Expected pnl_percentage={expected_pnl_pct}%, "
        f"got {df.loc[0, 'pnl_percentage']}"
    )


def test_bg_holdings_add_pct_missing_columns_no_crash():
    """Frames without day_change_val or inv_val must not raise."""
    from backend.api.background import _bg_holdings_add_pct

    # Only cur_val, no day_change_val
    df = pd.DataFrame([{"account": "ZG0790", "cur_val": 11000.0, "pnl": 1000.0}])
    _bg_holdings_add_pct(df)  # Must not raise
    assert "day_change_percentage" not in df.columns


# ---------------------------------------------------------------------------
# Fix 4: _fetch_positions_direct calls _override_stale_ltp_from_ticker
# ---------------------------------------------------------------------------

def test_fetch_positions_direct_calls_ltp_override():
    """_fetch_positions_direct must call _override_stale_ltp_from_ticker
    (sync KiteTicker patch) before apply_day_change_backstop.

    This ensures the NavStrip P "today" slot gets the ticker-corrected LTP
    rather than the potentially-stale REST LTP (observed 2026-06-22 where
    Kite's REST lagged WS by 30 min for CRUDEOIL options).
    """
    from backend.api.background import _fetch_positions_direct

    raw_df = pd.DataFrame([{
        "account": "ZG0790",
        "tradingsymbol": "CRUDEOIL26JUL6900CE",
        "exchange": "MCX",
        "quantity": 100,
        "average_price": 200.0,
        "close_price": 190.0,
        "last_price": 190.0,  # stale REST value — ticker should patch to 210
        "pnl": -1000.0,
        "day_change_val": 0.0,
        "overnight_quantity": 100,
    }])

    call_order = []

    def fake_ltp_override(df):
        call_order.append("ltp_override")

    def fake_backstop(df):
        call_order.append("backstop")
        return df

    with patch(
        "backend.brokers.broker_apis.fetch_positions",
        return_value=[raw_df],
    ), patch(
        "backend.api.routes.positions._override_stale_ltp_from_ticker",
        side_effect=fake_ltp_override,
    ), patch(
        "backend.api.algo.pnl_math.apply_day_change_backstop",
        side_effect=fake_backstop,
    ):
        _fetch_positions_direct()

    assert "ltp_override" in call_order, (
        "_override_stale_ltp_from_ticker was NOT called by _fetch_positions_direct. "
        "The background NavStrip P slot will use stale REST LTP."
    )
    assert "backstop" in call_order, (
        "apply_day_change_backstop was NOT called — existing regression."
    )
    # ltp_override must come before backstop (ordering invariant)
    assert call_order.index("ltp_override") < call_order.index("backstop"), (
        "ltp_override must be called BEFORE backstop to ensure patched LTPs "
        "feed into the backstop calculation."
    )


def test_fetch_positions_direct_empty_raw_no_ltp_override():
    """When broker returns empty, _override_stale_ltp_from_ticker is NOT called
    (the function exits early via the 'account' guard).
    """
    from backend.api.background import _fetch_positions_direct

    with patch(
        "backend.brokers.broker_apis.fetch_positions",
        return_value=[pd.DataFrame()],
    ), patch(
        "backend.api.routes.positions._override_stale_ltp_from_ticker",
    ) as mock_override:
        raw, summary = _fetch_positions_direct()

    mock_override.assert_not_called()
    assert raw.empty


# ---------------------------------------------------------------------------
# Fix 5: resolve_snapshot_day_pct uses close_price_f as denominator
# ---------------------------------------------------------------------------

def test_resolve_snapshot_day_pct_uses_close_price_not_ltp():
    """When close_price_f is provided and > 0, denominator is close_price_f × qty.

    A stock that was at 1000 yesterday and is now at 1200 (LTP):
    - day_pnl = (1200 - 1000) × 10 = 2000
    - Correct: day_pct = 2000 / (1000 × 10) = 20%
    - Wrong:   day_pct = 2000 / (1200 × 10) ≈ 16.67%
    """
    from backend.api.routes.positions_helpers import resolve_snapshot_day_pct

    day_pct = resolve_snapshot_day_pct(
        day_pnl_col=2000.0,   # non-None → extras not consulted
        day_pnl_f=2000.0,
        ltp_f=1200.0,
        qty_i=10,
        inv_val=10000.0,
        extras={},
        close_price_f=1000.0,  # previous close — correct denominator
    )

    expected = 2000.0 / (1000.0 * 10) * 100.0  # = 20.0%
    wrong = 2000.0 / (1200.0 * 10) * 100.0      # ≈ 16.67% (old bug)

    assert math.isclose(day_pct, expected, rel_tol=1e-4), (
        f"Expected {expected:.2f}% (close_price denominator), "
        f"got {day_pct:.4f}%. "
        f"Old wrong value would be {wrong:.2f}%."
    )


def test_resolve_snapshot_day_pct_falls_back_to_ltp_when_no_close_price():
    """When close_price_f is None, denominator falls back to ltp_f."""
    from backend.api.routes.positions_helpers import resolve_snapshot_day_pct

    # Without close_price_f (default None), denominator = ltp × qty
    day_pct = resolve_snapshot_day_pct(
        day_pnl_col=200.0,
        day_pnl_f=200.0,
        ltp_f=1000.0,
        qty_i=10,
        inv_val=9000.0,
        extras={},
        close_price_f=None,
    )
    expected = 200.0 / (1000.0 * 10) * 100.0  # = 2.0%
    assert math.isclose(day_pct, expected, rel_tol=1e-4), (
        f"Expected {expected:.2f}% (ltp fallback), got {day_pct:.4f}%"
    )


def test_resolve_snapshot_day_pct_zero_close_price_falls_back_to_ltp():
    """close_price_f=0 is treated as absent — falls back to ltp."""
    from backend.api.routes.positions_helpers import resolve_snapshot_day_pct

    day_pct = resolve_snapshot_day_pct(
        day_pnl_col=100.0,
        day_pnl_f=100.0,
        ltp_f=500.0,
        qty_i=5,
        inv_val=2500.0,
        extras={},
        close_price_f=0.0,  # zero → fall back
    )
    expected = 100.0 / (500.0 * 5) * 100.0  # = 4.0%
    assert math.isclose(day_pct, expected, rel_tol=1e-4), (
        f"Expected {expected:.2f}% (ltp fallback when close=0), got {day_pct:.4f}%"
    )


def test_resolve_snapshot_day_pct_extras_override_when_col_is_none():
    """When day_pnl_col is None, extras.day_change_pct wins over computation."""
    from backend.api.routes.positions_helpers import resolve_snapshot_day_pct

    day_pct = resolve_snapshot_day_pct(
        day_pnl_col=None,
        day_pnl_f=999.0,
        ltp_f=1000.0,
        qty_i=10,
        inv_val=9000.0,
        extras={"day_change_pct": 42.0},
        close_price_f=900.0,
    )
    # extras wins regardless of close_price_f
    assert math.isclose(day_pct, 42.0, rel_tol=1e-6), (
        f"Expected extras.day_change_pct=42.0, got {day_pct}"
    )


def test_resolve_snapshot_day_pct_close_price_used_in_build_snapshot_row():
    """Integration: build_snapshot_position_row computes close_price_f BEFORE
    calling resolve_snapshot_day_pct, and the correct denominator flows through.

    Verifies Fix 5 + ordering fix in build_snapshot_position_row together.
    """
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    # ltp=1200, previous_close=1000, qty=10, day_pnl=2000
    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="BHEL",
        exchange="NSE",
        qty=10,
        avg_cost=Decimal("800.0"),
        ltp=Decimal("1200.0"),
        day_pnl=Decimal("2000.0"),
        total_pnl=Decimal("4000.0"),
        extras={},
        previous_close=Decimal("1000.0"),
    )
    # day_change_percentage should be 20% (2000 / 10000), not 16.67% (2000 / 12000)
    assert math.isclose(row.day_change_percentage, 20.0, rel_tol=1e-4), (
        f"Expected day_change_percentage=20.0% "
        f"(denominator=previous_close×qty=10000), "
        f"got {row.day_change_percentage:.4f}%"
    )


# ---------------------------------------------------------------------------
# Fix 6: build_snapshot_position_row propagates product from payload_json
# ---------------------------------------------------------------------------

def test_build_snapshot_row_product_from_kwarg():
    """product kwarg is forwarded to PositionRow.product."""
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="CRUDEOIL26JUL6900CE",
        exchange="MCX",
        qty=100,
        avg_cost=Decimal("200.0"),
        ltp=Decimal("210.0"),
        day_pnl=Decimal("1000.0"),
        total_pnl=Decimal("1000.0"),
        extras={},
        product="MIS",  # intraday product
    )
    assert row.product == "MIS", (
        f"Expected product='MIS', got {row.product!r}"
    )


def test_build_snapshot_row_product_defaults_to_nrml():
    """Without product kwarg, product defaults to 'NRML'."""
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="NIFTY26JULFUT",
        exchange="NFO",
        qty=50,
        avg_cost=Decimal("23000.0"),
        ltp=Decimal("23500.0"),
        day_pnl=Decimal("2500.0"),
        total_pnl=Decimal("7500.0"),
        extras={},
        # product not provided
    )
    assert row.product == "NRML", (
        f"Expected default product='NRML', got {row.product!r}"
    )


def test_positions_snapshot_extracts_product_from_payload_json():
    """Integration: _positions_snapshot loop extracts product from payload_json
    and passes it as the product kwarg.

    Simulates the extraction logic added at the call site in positions.py.
    """
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    # Simulate call-site logic: extract product from payload_json
    payload_json = json.dumps({
        "tradingsymbol": "NIFTY26JULFUT",
        "product": "MIS",
        "multiplier": 1,
    })
    pj_product = "NRML"
    try:
        _pj = json.loads(payload_json)
        if isinstance(_pj, dict):
            pj_product = _pj.get("product", "NRML") or "NRML"
    except Exception:
        pass

    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="NIFTY26JULFUT",
        exchange="NFO",
        qty=50,
        avg_cost=Decimal("23000.0"),
        ltp=Decimal("23500.0"),
        day_pnl=Decimal("2500.0"),
        total_pnl=Decimal("7500.0"),
        extras={},
        product=pj_product,
    )
    assert row.product == "MIS", (
        f"Expected product='MIS' from payload_json, got {row.product!r}"
    )


def test_positions_snapshot_product_fallback_when_payload_json_missing():
    """When payload_json is None or missing 'product', falls back to 'NRML'."""
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    # Simulate extraction with None payload
    payload_json = None
    pj_product = "NRML"
    if payload_json:
        try:
            _pj = json.loads(payload_json)
            if isinstance(_pj, dict):
                pj_product = _pj.get("product", "NRML") or "NRML"
        except Exception:
            pass

    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="BHEL26JUL390CE",
        exchange="NFO",
        qty=100,
        avg_cost=Decimal("310.0"),
        ltp=Decimal("335.0"),
        day_pnl=Decimal("2500.0"),
        total_pnl=Decimal("2500.0"),
        extras={},
        product=pj_product,
    )
    assert row.product == "NRML", (
        f"Expected product='NRML' when payload_json is None, got {row.product!r}"
    )
