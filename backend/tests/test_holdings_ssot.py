"""
Test suite for holdings SSOT refactor — _compute_holding_day_change helper.

Verifies the closed-hours overlay at backend/api/routes/holdings.py now calls
_compute_holding_day_change instead of recomputing day change value inline.
"""

import pytest
from backend.api.routes.holdings import _compute_holding_day_change


def test_holding_day_change_stored_eod_wins():
    """When snap_day_pnl is non-zero, it is authoritative (stored EOD value)."""
    snap_day_pnl = 500.0
    snap_price = 102.0
    close_px = 100.0
    prev_ltp = None
    qty = 10

    result = _compute_holding_day_change(snap_day_pnl, snap_price, close_px, prev_ltp, qty)

    # Stored EOD day P&L should win regardless of other prices
    assert result == 500.0, (
        f"expected stored day_pnl to be returned as-is, got {result}"
    )


def test_holding_day_change_recompute_from_close():
    """When snap_day_pnl=0.0, snap_price=102.0, close_px=100.0, qty=10
    → helper returns (102-100)*10 = 20.0."""
    snap_day_pnl = 0.0
    snap_price = 102.0
    close_px = 100.0
    prev_ltp = None
    qty = 10

    result = _compute_holding_day_change(snap_day_pnl, snap_price, close_px, prev_ltp, qty)

    # When no stored day_pnl, compute from previous_close
    assert result == 20.0, (
        f"expected (102-100)*10 = 20.0, got {result}"
    )


def test_holding_day_change_fallback_to_prev_ltp():
    """When snap_day_pnl=0.0, close_px=0.0 (no previous_close),
    but prev_ltp=99.0 exists → helper returns (102-99)*10 = 30.0."""
    snap_day_pnl = 0.0
    snap_price = 102.0
    close_px = 0.0  # no reference from previous_close
    prev_ltp = 99.0
    qty = 10

    result = _compute_holding_day_change(snap_day_pnl, snap_price, close_px, prev_ltp, qty)

    # Should fall back to prev_ltp when previous_close is 0
    assert result == 30.0, (
        f"expected (102-99)*10 = 30.0, got {result}"
    )


def test_holding_day_change_zero_when_no_reference():
    """When snap_day_pnl=0.0, close_px=0.0, prev_ltp=None (or missing)
    → helper returns 0.0 (no reference price available)."""
    snap_day_pnl = 0.0
    snap_price = 102.0
    close_px = 0.0
    prev_ltp = None
    qty = 10

    result = _compute_holding_day_change(snap_day_pnl, snap_price, close_px, prev_ltp, qty)

    # No reference price available
    assert result == 0.0, (
        f"expected 0.0 when no reference price, got {result}"
    )


def test_holding_day_change_none_day_pnl_returned_as_is():
    """When snap_day_pnl=None (NULL from DB), function returns it as-is
    (None is treated as non-zero because None != 0.0)."""
    snap_day_pnl = None
    snap_price = 105.0
    close_px = 100.0
    prev_ltp = None
    qty = 5

    result = _compute_holding_day_change(snap_day_pnl, snap_price, close_px, prev_ltp, qty)

    # None is returned as-is because None != 0.0
    assert result is None, (
        f"expected None when snap_day_pnl=None (None != 0.0), got {result}"
    )


def test_holding_day_change_negative_price_delta():
    """Negative price delta (stock down) returns negative day change value."""
    snap_day_pnl = 0.0
    snap_price = 98.0
    close_px = 100.0
    prev_ltp = None
    qty = 10

    result = _compute_holding_day_change(snap_day_pnl, snap_price, close_px, prev_ltp, qty)

    # Negative day change
    assert result == -20.0, (
        f"expected (98-100)*10 = -20.0, got {result}"
    )


def test_holding_day_change_zero_qty():
    """With qty=0, day change should be 0 regardless of price delta."""
    snap_day_pnl = 0.0
    snap_price = 102.0
    close_px = 100.0
    prev_ltp = None
    qty = 0

    result = _compute_holding_day_change(snap_day_pnl, snap_price, close_px, prev_ltp, qty)

    # Zero quantity → zero day change
    assert result == 0.0, (
        f"expected 0.0 with qty=0, got {result}"
    )


def test_hold_tag_closed_row_delegates_to_compute_helper():
    """_hold_tag_closed_row must delegate day-change computation to
    _compute_holding_day_change (not recompute inline). Verified by patching
    the helper and asserting it is called with the expected arguments.
    """
    from unittest.mock import patch
    from backend.api.routes.holdings import _hold_tag_closed_row
    from backend.api.schemas import HoldingRow
    import msgspec

    row = HoldingRow(
        account="ZG0001",
        tradingsymbol="INFY",
        exchange="NSE",
        quantity=50,
        opening_quantity=50,
        average_price=1800.0,
        close_price=1820.0,
        last_price=1820.0,
        inv_val=90000.0,
        cur_val=91000.0,
        pnl=1000.0,
        pnl_percentage=1.1,
        day_change_val=0.0,
        day_change=0.0,
        day_change_percentage=0.0,
    )
    # snap_data: (ltp=1850.0, day_pnl=1500.0)
    snap_data = (1850.0, 1500.0)

    captured_calls: list = []

    original_fn = __import__(
        "backend.api.routes.holdings", fromlist=["_compute_holding_day_change"]
    )._compute_holding_day_change

    def _spy(*args, **kwargs):
        captured_calls.append((args, kwargs))
        return original_fn(*args, **kwargs)

    with patch("backend.api.routes.holdings._compute_holding_day_change", side_effect=_spy):
        result = _hold_tag_closed_row(row, snap_data, msgspec)

    assert len(captured_calls) == 1, (
        f"_compute_holding_day_change must be called exactly once, called {len(captured_calls)} times"
    )
    call_args = captured_calls[0][0]
    # snap_day_pnl or 0.0 = 1500.0
    assert call_args[0] == 1500.0, f"first arg (day_pnl) expected 1500.0, got {call_args[0]}"
    # snap_price = 1850.0 (from resolve_current_price, but close_px=1820 so settlement wins)
    assert call_args[2] == 1820.0, f"third arg (close_px) expected 1820.0, got {call_args[2]}"
    assert call_args[3] is None, f"fourth arg (prev_ltp) must be None, got {call_args[3]}"
    assert call_args[4] == 50, f"fifth arg (qty) expected 50, got {call_args[4]}"
    # The returned row must carry the delegated value
    assert result.day_change_val == 1500.0, (
        f"day_change_val must equal helper's return value, got {result.day_change_val}"
    )
