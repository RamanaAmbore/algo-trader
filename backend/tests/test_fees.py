"""
Tests for shared/helpers/fees.py — Indian broker fee model.
SSOT: compute_order_fees is the single public function.
Perf: pure-math — no I/O, no DB, deterministic output.
Stale: STT rates are constants (_STT_OPT_SELL_PCT, _STT_FUT_SELL_PCT).
Reuse: same fee model used across sim, paper, and live paths.
UX: returns 0.0 for non-F&O or invalid orders (no crash).
"""
import pytest
from backend.shared.helpers.fees import compute_order_fees


def _opt_order(side, qty=50, price=100.0):
    return {
        "tradingsymbol": "NIFTY26JUL24000CE",
        "transaction_type": side,
        "quantity": qty,
        "fill_price": price,
    }


def _fut_order(side, qty=50, price=100.0):
    return {
        "tradingsymbol": "NIFTYJULFUT",
        "transaction_type": side,
        "quantity": qty,
        "fill_price": price,
    }


def test_compute_order_fees_returns_float():
    result = compute_order_fees(_opt_order("SELL"))
    assert isinstance(result, float), "compute_order_fees must return a float"
    assert result > 0, "Option SELL fees must be positive"


def test_option_sell_stt():
    """Option SELL: STT = 0.0625% × turnover (50 × 100 = 5000 → STT = 3.125)."""
    from backend.shared.helpers.fees import _compute_stt
    turnover = 50.0 * 100.0  # = 5000
    stt = _compute_stt(turnover, "SELL", is_option=True, is_future=False)
    assert stt == pytest.approx(3.125, rel=1e-6), (
        f"STT on option SELL must be 0.0625% × 5000 = 3.125, got {stt}"
    )


def test_option_buy_stt_is_zero():
    """Option BUY: no STT (STT only on option sell side)."""
    from backend.shared.helpers.fees import _compute_stt
    stt = _compute_stt(5000.0, "BUY", is_option=True, is_future=False)
    assert stt == 0.0, f"STT on option BUY must be 0, got {stt}"


def test_futures_sell_stt():
    """Futures SELL: STT = 0.0125% × turnover."""
    from backend.shared.helpers.fees import _compute_stt
    turnover = 50.0 * 100.0
    stt = _compute_stt(turnover, "SELL", is_option=False, is_future=True)
    assert stt == pytest.approx(turnover * 0.0125 / 100, rel=1e-6), (
        f"STT on futures SELL must be 0.0125% × turnover"
    )


def test_brokerage_is_flat_20():
    """F&O brokerage is always ₹20 flat (not a % of turnover)."""
    from backend.shared.helpers.fees import _BROKERAGE_PER_ORDER
    assert _BROKERAGE_PER_ORDER == 20.0, (
        f"Brokerage constant must be ₹20 flat, got {_BROKERAGE_PER_ORDER}"
    )


def test_gst_included_in_total():
    """Total = brokerage + STT + ancillary + 18% GST on (brokerage + ancillary)."""
    order = _opt_order("BUY", qty=50, price=100.0)
    total = compute_order_fees(order)
    # For BUY option: STT=0, brokerage=20, ancillary=5000*0.05/100=2.5, GST=(20+2.5)*0.18=4.05
    expected = 20.0 + 0.0 + 2.5 + 4.05
    assert total == pytest.approx(expected, rel=1e-3), (
        f"Total fees must include GST; expected≈{expected:.2f}, got {total}"
    )


def test_non_fno_returns_zero():
    """Non-F&O (equity symbol) returns 0.0."""
    order = {
        "tradingsymbol": "INFY",
        "transaction_type": "BUY",
        "quantity": 10,
        "fill_price": 1800.0,
    }
    assert compute_order_fees(order) == 0.0, "Equity order must return 0.0 fees"


def test_zero_qty_returns_zero():
    assert compute_order_fees(_opt_order("SELL", qty=0)) == 0.0, "Zero qty must return 0.0"
