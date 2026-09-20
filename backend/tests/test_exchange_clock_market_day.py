"""
Tests for new exchange_clock functions added to daily session lifecycle.

Covers:
  - _is_market_day_today() — market day detection from DB-backed cache
  - _effective_snapshot_time(row) — snapshot time derivation
  - is_any_segment_open() fail-closed fix

Quality dimensions:
  1. SSOT        — _is_market_day_today() reads from _CACHE via _effective_gate_rows
  2. Correctness — market day True/False for trading/non-trading days; fail-open when cache empty
  3. Performance — snapshot_time derivation is pure computation (no I/O)
  4. Stale code  — is_any_segment_open() fail-closed fix verified in source
  5. Edge cases  — midnight overflow for snapshot_time + timedelta handling
"""

from __future__ import annotations

from datetime import time, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gate_row(
    gate: str,
    open_time: time | None = None,
    close_time: time | None = None,
    snapshot_time: time | None = None,
) -> SimpleNamespace:
    """Build a minimal mock ExchangeSchedule row."""
    return SimpleNamespace(
        gate=gate,
        open_time=open_time,
        close_time=close_time,
        snapshot_time=snapshot_time,
        exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"] if gate == "NON-MCX" else ["MCX"],
    )


# ---------------------------------------------------------------------------
# _is_market_day_today() tests
# ---------------------------------------------------------------------------

def test_is_market_day_today_both_gates_open():
    """Return True when both NON-MCX and MCX have open_time."""
    non_mcx_row = _make_gate_row("NON-MCX", open_time=time(8, 0), close_time=time(15, 30))
    mcx_row = _make_gate_row("MCX", open_time=time(8, 0), close_time=time(23, 30))

    with patch("backend.api.helpers.exchange_clock._CACHE", [non_mcx_row, mcx_row]):
        with patch("backend.api.helpers.exchange_clock._effective_gate_rows") as mock_rows:
            def _rows(gate: str):
                if gate.upper() == "NON-MCX":
                    return [non_mcx_row]
                elif gate.upper() == "MCX":
                    return [mcx_row]
                return []

            mock_rows.side_effect = _rows

            from backend.api.helpers.exchange_clock import _is_market_day_today
            assert _is_market_day_today() is True


def test_is_market_day_today_non_mcx_only():
    """Return True when only NON-MCX is open."""
    non_mcx_row = _make_gate_row("NON-MCX", open_time=time(8, 0), close_time=time(15, 30))

    with patch("backend.api.helpers.exchange_clock._CACHE", [non_mcx_row]):
        with patch("backend.api.helpers.exchange_clock._effective_gate_rows") as mock_rows:
            def _rows(gate: str):
                if gate.upper() == "NON-MCX":
                    return [non_mcx_row]
                return []

            mock_rows.side_effect = _rows

            from backend.api.helpers.exchange_clock import _is_market_day_today
            assert _is_market_day_today() is True


def test_is_market_day_today_mcx_only():
    """Return True when only MCX is open."""
    mcx_row = _make_gate_row("MCX", open_time=time(8, 0), close_time=time(23, 30))

    with patch("backend.api.helpers.exchange_clock._CACHE", [mcx_row]):
        with patch("backend.api.helpers.exchange_clock._effective_gate_rows") as mock_rows:
            def _rows(gate: str):
                if gate.upper() == "MCX":
                    return [mcx_row]
                return []

            mock_rows.side_effect = _rows

            from backend.api.helpers.exchange_clock import _is_market_day_today
            assert _is_market_day_today() is True


def test_is_market_day_today_weekend():
    """Return False when both gates have empty rows (weekend)."""
    non_mcx_empty = []  # No rows on weekend
    mcx_empty = []

    # Create a dummy row so _CACHE is not empty (to avoid fail-open)
    dummy_row = _make_gate_row("DUMMY", open_time=time(8, 0), close_time=time(15, 30))

    with patch("backend.api.helpers.exchange_clock._CACHE", [dummy_row]):
        with patch("backend.api.helpers.exchange_clock._effective_gate_rows") as mock_rows:
            def _rows(gate: str):
                # Both gates return empty on weekends
                return []

            mock_rows.side_effect = _rows

            from backend.api.helpers.exchange_clock import _is_market_day_today
            assert _is_market_day_today() is False


def test_is_market_day_today_holiday_override():
    """Return False when holiday override row has open_time=None."""
    non_mcx_holiday = _make_gate_row("NON-MCX", open_time=None, close_time=None)
    mcx_holiday = _make_gate_row("MCX", open_time=None, close_time=None)

    with patch("backend.api.helpers.exchange_clock._CACHE", [non_mcx_holiday, mcx_holiday]):
        with patch("backend.api.helpers.exchange_clock._effective_gate_rows") as mock_rows:
            def _rows(gate: str):
                if gate.upper() == "NON-MCX":
                    return [non_mcx_holiday]
                elif gate.upper() == "MCX":
                    return [mcx_holiday]
                return []

            mock_rows.side_effect = _rows

            from backend.api.helpers.exchange_clock import _is_market_day_today
            assert _is_market_day_today() is False


def test_is_market_day_today_empty_cache_fail_closed():
    """Return False when cache is empty (fail-closed — schedule not loaded)."""
    with patch("backend.api.helpers.exchange_clock._CACHE", []):
        from backend.api.helpers.exchange_clock import _is_market_day_today
        assert _is_market_day_today() is False


# ---------------------------------------------------------------------------
# _effective_snapshot_time() tests
# ---------------------------------------------------------------------------

def test_effective_snapshot_time_explicit():
    """Return explicit snapshot_time when set."""
    row = _make_gate_row("NON-MCX", close_time=time(15, 30), snapshot_time=time(16, 0))

    from backend.api.helpers.exchange_clock import _effective_snapshot_time
    result = _effective_snapshot_time(row)
    assert result == time(16, 0)


def test_effective_snapshot_time_derived_nse():
    """Derive snapshot_time as close_time + 15 min for NON-MCX (15:30 → 15:45)."""
    row = _make_gate_row("NON-MCX", close_time=time(15, 30), snapshot_time=None)

    from backend.api.helpers.exchange_clock import _effective_snapshot_time
    result = _effective_snapshot_time(row)
    assert result == time(15, 45), f"expected 15:45, got {result}"


def test_effective_snapshot_time_derived_mcx():
    """Derive snapshot_time as close_time + 15 min for MCX (23:30 → 23:45)."""
    row = _make_gate_row("MCX", close_time=time(23, 30), snapshot_time=None)

    from backend.api.helpers.exchange_clock import _effective_snapshot_time
    result = _effective_snapshot_time(row)
    assert result == time(23, 45), f"expected 23:45, got {result}"


def test_effective_snapshot_time_midnight_overflow():
    """Handle midnight overflow: close_time 23:50 + 15 min → 00:05 next day."""
    row = _make_gate_row("MCX", close_time=time(23, 50), snapshot_time=None)

    from backend.api.helpers.exchange_clock import _effective_snapshot_time
    result = _effective_snapshot_time(row)
    # close_time 23:50 + 15 min = 00:05 next day
    assert result == time(0, 5), f"expected 00:05, got {result}"


def test_effective_snapshot_time_both_none():
    """Return None when both snapshot_time and close_time are None."""
    row = _make_gate_row("NON-MCX", close_time=None, snapshot_time=None)

    from backend.api.helpers.exchange_clock import _effective_snapshot_time
    result = _effective_snapshot_time(row)
    assert result is None


def test_effective_snapshot_time_close_time_none():
    """Return None when close_time is None (even if snapshot_time would be used)."""
    row = _make_gate_row("NON-MCX", close_time=None, snapshot_time=None)

    from backend.api.helpers.exchange_clock import _effective_snapshot_time
    result = _effective_snapshot_time(row)
    assert result is None


# ---------------------------------------------------------------------------
# is_any_segment_open() fail-closed fix
# ---------------------------------------------------------------------------

def test_is_any_segment_open_empty_cache_fail_closed():
    """Return False (fail-closed) when cache is empty."""
    with patch("backend.api.helpers.exchange_clock._CACHE", []):
        from backend.api.helpers.exchange_clock import is_any_segment_open
        assert is_any_segment_open() is False


def test_is_any_segment_open_cache_warmed_market_open():
    """Return True when cache is warm and a segment is open."""
    non_mcx_row = _make_gate_row("NON-MCX", open_time=time(8, 0), close_time=time(15, 30))

    with patch("backend.api.helpers.exchange_clock._CACHE", [non_mcx_row]):
        with patch("backend.api.helpers.exchange_clock._effective_gate_rows") as mock_rows:
            with patch("backend.api.helpers.exchange_clock._is_within_session", return_value=True):
                mock_rows.return_value = [non_mcx_row]

                from backend.api.helpers.exchange_clock import is_any_segment_open
                assert is_any_segment_open() is True


def test_is_any_segment_open_cache_warmed_market_closed():
    """Return False when cache is warm but no segments are open."""
    non_mcx_row = _make_gate_row("NON-MCX", open_time=time(8, 0), close_time=time(15, 30))

    with patch("backend.api.helpers.exchange_clock._CACHE", [non_mcx_row]):
        with patch("backend.api.helpers.exchange_clock._effective_gate_rows") as mock_rows:
            with patch("backend.api.helpers.exchange_clock._is_within_session", return_value=False):
                mock_rows.return_value = [non_mcx_row]

                from backend.api.helpers.exchange_clock import is_any_segment_open
                assert is_any_segment_open() is False


def test_is_any_segment_open_with_exchange_filter():
    """Filter by exchange codes when provided."""
    non_mcx_row = _make_gate_row("NON-MCX", open_time=time(8, 0), close_time=time(15, 30))

    with patch("backend.api.helpers.exchange_clock._CACHE", [non_mcx_row]):
        with patch("backend.api.helpers.exchange_clock._effective_gate_rows") as mock_rows:
            with patch("backend.api.helpers.exchange_clock._is_within_session", return_value=True):
                mock_rows.return_value = [non_mcx_row]

                from backend.api.helpers.exchange_clock import is_any_segment_open
                # NSE is in NON-MCX exchanges
                assert is_any_segment_open(["NSE"]) is True
                # MCX is not in NON-MCX exchanges
                assert is_any_segment_open(["MCX"]) is False
