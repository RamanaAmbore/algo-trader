"""
Tests for api/algo/lot_ledger.py — per-strategy FIFO lot attribution.
SSOT: open_lot and close_lot_fifo are the two public operations.
Perf: _pnl_sign is a sync pure helper (no DB); FIFO via opened_at ASC.
Stale: qty <= 0 guard short-circuits both operations.
Reuse: record_fill delegates to open_lot/close_lot_fifo.
UX: realized P&L sign: long (close>open=profit), short (open>close=profit).
"""
import pytest
from pathlib import Path

_SRC = Path("backend/api/algo/lot_ledger.py").read_text()


def test_open_lot_and_close_lot_fifo_exist():
    from backend.api.algo.lot_ledger import open_lot, close_lot_fifo
    import inspect
    assert inspect.iscoroutinefunction(open_lot), "open_lot must be async"
    assert inspect.iscoroutinefunction(close_lot_fifo), "close_lot_fifo must be async"


def test_pnl_sign_long_is_positive():
    from backend.api.algo.lot_ledger import _pnl_sign
    assert _pnl_sign("B") == 1, "Long lot (BUY-opened) must have +1 P&L sign"


def test_pnl_sign_short_is_negative():
    from backend.api.algo.lot_ledger import _pnl_sign
    assert _pnl_sign("S") == -1, "Short lot (SELL-opened) must have -1 P&L sign"


def test_record_fill_exists():
    from backend.api.algo.lot_ledger import record_fill
    import inspect
    assert inspect.iscoroutinefunction(record_fill), "record_fill must be async"


def test_fifo_ordering_in_source():
    """FIFO: opened_at ASC must be used to sort lots for oldest-first consumption."""
    assert "opened_at" in _SRC and "ASC" in _SRC.upper(), (
        "FIFO close must sort by opened_at ASC — oldest lots closed first"
    )
    assert "asc(StrategyLot.opened_at)" in _SRC or "opened_at.asc" in _SRC, (
        "close_lot_fifo must order open lots by asc(StrategyLot.opened_at)"
    )


def test_zero_qty_guard_in_open_lot():
    assert "qty <= 0" in _SRC, (
        "open_lot must guard against qty <= 0 — returns None early to skip ledger"
    )


def test_pnl_math_in_source():
    """P&L formula: (close_price - open_price) × consume × sign."""
    assert "close_price" in _SRC and "open_price" in _SRC, (
        "P&L computation must reference close_price and open_price"
    )
    assert "lot_pnl" in _SRC or "realized_pnl" in _SRC, (
        "Realized P&L accumulation must appear in close_lot_fifo"
    )


def test_long_position_pnl_sign():
    """Long lot: (close - open) × qty. Close above open → positive P&L."""
    from backend.api.algo.lot_ledger import _pnl_sign
    # Manually simulate P&L math:
    open_price = 100.0
    close_price = 150.0
    qty = 3
    sign = _pnl_sign("B")  # long
    pnl = (close_price - open_price) * qty * sign
    assert pnl == pytest.approx(150.0, rel=1e-6), f"Long P&L should be +150, got {pnl}"


def test_short_position_pnl_sign():
    """Short lot: open > close → profit. (close - open) × qty × -1 = positive."""
    from backend.api.algo.lot_ledger import _pnl_sign
    open_price = 100.0
    close_price = 80.0
    qty = 1
    sign = _pnl_sign("S")  # short = -1
    pnl = (close_price - open_price) * qty * sign
    assert pnl == pytest.approx(20.0, rel=1e-6), f"Short P&L should be +20, got {pnl}"
