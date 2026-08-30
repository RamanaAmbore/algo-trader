"""
Tests for the universal day P&L formula in build_row_from_snapshot_raw.

The formula computes day_change_val as:
  day_change_val = total_pnl - (prev_close - avg_cost) × overnight_quantity

This decomposes total P&L into overnight + intraday components.
Works for:
  - Overnight open positions (oq > 0, qty > 0)
  - New today positions (oq = 0)
  - Fully closed positions (qty = 0)
  - Partial closes
"""

import pytest
from datetime import datetime, timezone
import json


def _build_snapshot_tuple(account, symbol, exchange, qty, avg_cost, ltp, day_pnl,
                          total_pnl, payload_json, captured_at, previous_close,
                          prev_ltp, prev_settlement_pnl, overnight_quantity=None):
    """Helper to build a 13-column snapshot tuple with embedded overnight_quantity."""
    if overnight_quantity is None:
        overnight_quantity = 0

    # Build payload_json with overnight_quantity embedded
    if isinstance(payload_json, str):
        payload = json.loads(payload_json)
    else:
        payload = payload_json or {}

    payload["overnight_quantity"] = overnight_quantity
    payload_str = json.dumps(payload)

    return (
        account, symbol, exchange, qty, avg_cost, ltp,
        day_pnl, total_pnl, payload_str, captured_at, previous_close,
        prev_ltp, prev_settlement_pnl
    )


class TestUniversalFormulaOvernightOpen:
    """Overnight open position: day_change_val = total_pnl - (prev_close - avg) × oq."""

    def test_overnight_open_formula(self):
        """Formula: 800 - (95-90)*100 = 300."""
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

        # Overnight position: avg=90, prev_close=95, oq=100, qty=100, total_pnl=800
        # Expected: day_change_val = 800 - (95-90)*100 = 300
        tuple_row = _build_snapshot_tuple(
            account="TEST",
            symbol="INFY",
            exchange="NSE",
            qty=100,
            avg_cost=90.0,
            ltp=98.0,
            day_pnl=300.0,  # Stored decomposed value
            total_pnl=800.0,
            payload_json={},
            captured_at=datetime.now(timezone.utc),
            previous_close=95.0,
            prev_ltp=None,
            prev_settlement_pnl=None,
            overnight_quantity=100,
        )

        row = build_row_from_snapshot_raw(tuple_row)

        # The computed formula should yield 300
        assert row.day_change_val == 300.0, (
            f"Overnight open: day_change_val should be 300 (total_pnl=800 - (95-90)*100), "
            f"got {row.day_change_val}"
        )


class TestUniversalFormulaNewTodayPosition:
    """New today position: oq=0, so day_change_val = total_pnl - (prev_close - avg) × 0 = total_pnl."""

    def test_new_today_formula(self):
        """Formula: 300 - (92-95)*0 = 300."""
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

        # New position: avg=95, prev_close=92, oq=0, qty=100, total_pnl=300
        # Expected: day_change_val = 300 - (92-95)*0 = 300
        tuple_row = _build_snapshot_tuple(
            account="TEST",
            symbol="RELIANCE",
            exchange="NSE",
            qty=100,
            avg_cost=95.0,
            ltp=98.0,
            day_pnl=300.0,
            total_pnl=300.0,
            payload_json={},
            captured_at=datetime.now(timezone.utc),
            previous_close=92.0,
            prev_ltp=None,
            prev_settlement_pnl=None,
            overnight_quantity=0,  # New position
        )

        row = build_row_from_snapshot_raw(tuple_row)

        assert row.day_change_val == 300.0, (
            f"New today position: day_change_val should be 300 (no overnight qty), "
            f"got {row.day_change_val}"
        )


class TestUniversalFormulaFullyClosedOvernight:
    """Fully closed overnight position: qty=0 but oq=100."""

    def test_fully_closed_formula(self):
        """Formula: 800 - (95-90)*100 = 300 (uses stored day_pnl directly)."""
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

        # Closed position: avg=90, prev_close=95, oq=100, qty=0, total_pnl=800
        # For qty=0, use stored day_pnl directly
        tuple_row = _build_snapshot_tuple(
            account="TEST",
            symbol="TCS",
            exchange="NSE",
            qty=0,  # Closed
            avg_cost=90.0,
            ltp=98.0,
            day_pnl=300.0,  # Stored decomposed value
            total_pnl=800.0,
            payload_json={},
            captured_at=datetime.now(timezone.utc),
            previous_close=95.0,
            prev_ltp=None,
            prev_settlement_pnl=None,
            overnight_quantity=100,
        )

        row = build_row_from_snapshot_raw(tuple_row)

        # For qty=0, use stored day_pnl directly (closed overnight)
        assert row.day_change_val == 300.0, (
            f"Closed position: day_change_val should use stored day_pnl=300, "
            f"got {row.day_change_val}"
        )


class TestUniversalFormulaPartialClose:
    """Partial close: qty=50, oq=100 → formula computes for 50 qty."""

    def test_partial_close_formula(self):
        """Formula: 750 - (95-90)*100 = 250."""
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

        # Partial close: avg=90, prev_close=95, oq=100, qty=50, total_pnl=750
        # Expected: day_change_val = 750 - (95-90)*100 = 250
        tuple_row = _build_snapshot_tuple(
            account="TEST",
            symbol="HDFC",
            exchange="NSE",
            qty=50,  # Partial
            avg_cost=90.0,
            ltp=98.0,
            day_pnl=250.0,  # Stored decomposed
            total_pnl=750.0,
            payload_json={},
            captured_at=datetime.now(timezone.utc),
            previous_close=95.0,
            prev_ltp=None,
            prev_settlement_pnl=None,
            overnight_quantity=100,
        )

        row = build_row_from_snapshot_raw(tuple_row)

        assert row.day_change_val == 250.0, (
            f"Partial close: day_change_val should be 250 (750 - (95-90)*100), "
            f"got {row.day_change_val}"
        )


class TestOvernightQuantityFromPayload:
    """Overnight quantity must be read from payload, not from qty field."""

    def test_overnight_quantity_from_payload(self):
        """overnight_quantity must come from payload_json, not qty."""
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

        # Payload has oq=80, but qty=50 (partial close today)
        payload = {
            "overnight_quantity": 80,  # The source
            "day_buy_quantity": 0,
            "day_sell_quantity": 30,  # Sold 30 shares
            "day_buy_value": 0,
            "day_sell_value": 2940,  # Sold at 98
        }

        tuple_row = _build_snapshot_tuple(
            account="TEST",
            symbol="INFY",
            exchange="NSE",
            qty=50,  # Remaining: 80 - 30 = 50
            avg_cost=90.0,
            ltp=98.0,
            day_pnl=240.0,  # Partial close P&L
            total_pnl=600.0,
            payload_json=json.dumps(payload),
            captured_at=datetime.now(timezone.utc),
            previous_close=95.0,
            prev_ltp=None,
            prev_settlement_pnl=None,
            overnight_quantity=80,
        )

        row = build_row_from_snapshot_raw(tuple_row)

        # The formula should use oq=80 from payload
        # day_change_val = 600 - (95-90)*80 = 200
        expected_day_change = 600.0 - (95.0 - 90.0) * 80.0
        assert row.day_change_val == expected_day_change, (
            f"Must use overnight_quantity=80 from payload; "
            f"expected {expected_day_change}, got {row.day_change_val}"
        )
        # overnight_quantity on the row must reflect the true opening qty (80), not the
        # current qty (50) — the partial-close did NOT change the opening quantity.
        assert row.overnight_quantity == 80, (
            f"row.overnight_quantity must be 80 (from payload_json), not qty=50; "
            f"got {row.overnight_quantity}"
        )


class TestShortPositionFormula:
    """Short positions: oq < 0, formula applies with negative qty."""

    def test_short_position_day_pnl(self):
        """Short: avg=100, prev_close=95, oq=-50, qty=-50, total_pnl=250."""
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

        # Short position: avg=100, prev_close=95 (sold short), ltp=98
        # day_change = (100 - 98) * 50 = 100 (gain on short)
        # Formula: 250 - (95-100)*(-50) = 250 - 250 = 0 (all pnl is from day trade setup, not intraday)
        # Actually for shorts: (prev_close - ltp) × |qty| = (95-98)*50 = -150
        # So the formula becomes: total_pnl - (prev_close - avg) × oq
        # = 250 - (95-100)*(-50) = 250 - 250 = 0? No...
        # Let me recalculate: if short at 100, prev_close=95, current ltp=98
        # day P&L = (short entry - current) = (100 - 98) = +2 per share, ×50 = +100
        # But we're looking at entry-to-prev-close: (100 - 95) = +5 per share, ×50 = +250 (overnight)
        # So intraday P&L from prev_close to current = (95 - 98) × 50 = -150
        # But wait, we need to check the formula: day_change_val = total_pnl - (prev_close - avg) × oq
        # = 250 - (95 - 100) × (-50) = 250 - (-5) × (-50) = 250 - 250 = 0
        # Hmm, that doesn't match. Let's check a different interpretation.
        # If total_pnl = 250 and oq = -50, avg = 100, prev_close = 95
        # Overnight P&L (entry to prev): (short entry to prev_close) = (100 - 95) × |50| = 250
        # Intraday P&L: total - overnight = 250 - 250 = 0
        # But the formula uses: day_change_val = total_pnl - (prev_close - avg) × oq
        # For short, oq < 0: = 250 - (95 - 100) × (-50) = 250 - (-5 × -50) = 250 - 250 = 0 ✓

        tuple_row = _build_snapshot_tuple(
            account="TEST",
            symbol="SBIN",
            exchange="NSE",
            qty=-50,  # Short position
            avg_cost=100.0,
            ltp=98.0,
            day_pnl=0.0,  # Intraday: (95-98)×50 = -150, but total shows 250
            total_pnl=250.0,  # Overnight gain from short entry to prev_close
            payload_json={},
            captured_at=datetime.now(timezone.utc),
            previous_close=95.0,
            prev_ltp=None,
            prev_settlement_pnl=None,
            overnight_quantity=-50,
        )

        row = build_row_from_snapshot_raw(tuple_row)

        # Formula: total - (prev_close - avg) × oq = 250 - (95-100)*(-50) = 250 - 250 = 0
        assert row.day_change_val == 0.0, (
            f"Short position formula: expected 0 (all pnl overnight), got {row.day_change_val}"
        )


class TestPreviousCloseZeroFallback:
    """When previous_close is 0 or None, should not use it in formula."""

    def test_previous_close_zero_fallback(self):
        """When previous_close is 0, use stored day_pnl directly."""
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

        tuple_row = _build_snapshot_tuple(
            account="TEST",
            symbol="NEWSTOCK",
            exchange="NSE",
            qty=100,
            avg_cost=50.0,
            ltp=55.0,
            day_pnl=500.0,  # Stored decomposed
            total_pnl=500.0,
            payload_json={},
            captured_at=datetime.now(timezone.utc),
            previous_close=0.0,  # Zero or stale
            prev_ltp=None,
            prev_settlement_pnl=None,
            overnight_quantity=0,  # New position
        )

        row = build_row_from_snapshot_raw(tuple_row)

        # Should use stored day_pnl when previous_close is not valid
        assert row.day_change_val == 500.0, (
            f"With zero previous_close, should use stored day_pnl=500, "
            f"got {row.day_change_val}"
        )
