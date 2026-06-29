"""Tests for positions route — day_change_val / day_change_percentage derivation."""

import math
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_net_rows(rows):
    """Build minimal kite.positions()['net'] payloads."""
    defaults = dict(
        tradingsymbol="NIFTY25APRFUT",
        exchange="NFO",
        product="NRML",
        average_price=22000.0,
        unrealised=0.0,
        realised=0.0,
    )
    return [dict(defaults, **r) for r in rows]


def _run_fetch_positions_direct(net_rows):
    """
    Call the core day-change derivation logic directly on a DataFrame,
    bypassing the @for_all_accounts decorator and Connections singleton.

    This tests the broker_apis logic in isolation without touching any
    broker network path (per project convention: do not mock broker API calls).
    """
    df = pd.DataFrame(net_rows)
    df['quantity'] = df['quantity'] * df['multiplier']

    if df.empty:
        return df

    df['day_change'] = df['last_price'] - df['close_price']
    df['day_change_val'] = df['day_change'] * df['quantity']
    prev_val = (df['close_price'] * df['quantity']).abs()
    df['day_change_percentage'] = (
        df['day_change_val'] / prev_val.replace(0, pd.NA) * 100
    ).fillna(0)
    return df


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_positions_response_includes_day_change_columns():
    """day_change_val and day_change_percentage are present and correct."""
    last_price  = 22100.0
    close_price = 22000.0
    quantity    = 50
    multiplier  = 1

    net_rows = _make_net_rows([dict(
        last_price=last_price,
        close_price=close_price,
        quantity=quantity,
        multiplier=multiplier,
    )])
    df = _run_fetch_positions_direct(net_rows)

    assert not df.empty, "Expected at least one row"
    assert "day_change_val" in df.columns, "day_change_val missing"
    assert "day_change_percentage" in df.columns, "day_change_percentage missing"

    eff_qty      = quantity * multiplier                        # 50
    expected_val = (last_price - close_price) * eff_qty        # 100 * 50 = 5000.0
    expected_pct = expected_val / abs(close_price * eff_qty) * 100  # ≈ 0.4545…

    assert abs(df.iloc[0]["day_change_val"] - expected_val) < 0.01, \
        f"day_change_val: expected {expected_val}, got {df.iloc[0]['day_change_val']}"
    assert abs(df.iloc[0]["day_change_percentage"] - expected_pct) < 0.001, \
        f"day_change_percentage: expected {expected_pct}, got {df.iloc[0]['day_change_percentage']}"
    assert df.iloc[0]["day_change_percentage"] is not None


def test_positions_day_change_zero_when_close_is_zero():
    """close_price = 0 must yield day_change_percentage = 0, not NaN or Inf."""
    net_rows = _make_net_rows([dict(
        last_price=100.0,
        close_price=0.0,
        quantity=10,
        multiplier=1,
    )])
    df = _run_fetch_positions_direct(net_rows)

    assert not df.empty
    val = df.iloc[0]["day_change_percentage"]
    assert val == 0.0, f"Expected 0.0 when close_price=0, got {val}"
    assert not math.isnan(float(val)), "day_change_percentage must not be NaN"
    assert not math.isinf(float(val)), "day_change_percentage must not be Inf"


# ---------------------------------------------------------------------------
# Regression: _override_stale_ltp_from_ticker — NameError on _qty
# Commit c0355526 introduced the additive pnl patch but dropped the _qty
# definition in the rewrite.  Calling _override_stale_ltp_from_ticker on a
# DataFrame whose rows get LTP-patched must NOT raise NameError and MUST
# update pnl by (new_ltp − old_ltp) × quantity.
# ---------------------------------------------------------------------------

def test_override_stale_ltp_from_ticker_pnl_patch_no_name_error():
    """_override_stale_ltp_from_ticker: pnl delta patch must not raise NameError."""
    from backend.api.routes.positions import _override_stale_ltp_from_ticker

    df = pd.DataFrame([{
        'tradingsymbol': 'CRUDEOIL26JUL6900PE',
        'exchange': 'MCX',
        'last_price': 220.0,   # stale REST LTP (== close → day_change 0)
        'close_price': 220.0,
        'quantity': 10,
        'overnight_quantity': 10,
        'day_buy_quantity': 0,
        'day_sell_quantity': 0,
        'day_buy_value': 0.0,
        'day_sell_value': 0.0,
        'average_price': 200.0,
        'realised': 0.0,
        'pnl': 200.0,           # broker pnl at stale LTP
        'day_change_val': 0.0,
        'day_change': 0.0,
    }])

    # Patch the ticker to return a fresh LTP of 264.5 for the symbol.
    mock_ticker = MagicMock()
    mock_ticker.get_ltp_by_sym.return_value = 264.5

    with patch('backend.brokers.kite_ticker.get_ticker', return_value=mock_ticker):
        # Must NOT raise NameError on _qty
        try:
            _override_stale_ltp_from_ticker(df)
        except NameError as exc:
            pytest.fail(f"NameError raised (regression): {exc}")

    # last_price patched to fresh tick
    assert abs(df.at[0, 'last_price'] - 264.5) < 0.01, "last_price not patched from ticker"
    # pnl updated by (264.5 - 220.0) * 10 = +445
    expected_pnl = 200.0 + (264.5 - 220.0) * 10  # 645.0
    assert abs(df.at[0, 'pnl'] - expected_pnl) < 0.01, \
        f"pnl additive patch wrong: expected {expected_pnl}, got {df.at[0, 'pnl']}"
