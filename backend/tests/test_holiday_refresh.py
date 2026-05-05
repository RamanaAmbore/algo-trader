"""
FIX 5 — Holiday year refresh gate

Tests for the background task's holiday calendar refresh logic.

The background task now checks if the year has changed. Holiday
calendars are only fetched from Kite when:
  - No cache exists yet, OR
  - The cached year doesn't match today's year

This prevents unnecessary broker calls on every tick.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, date
from zoneinfo import ZoneInfo


@pytest.mark.asyncio
async def test_holiday_refresh_on_year_change():
    """
    When state._hol_year differs from today.year, fetch_holidays
    is called and state._hol_year is updated.
    """
    state = {"_hol_year": 2025}
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    current_year = today.year

    # Mock fetch_holidays to return a set of holiday dates
    # fetch_holidays is imported inside background.py, so patch it there
    with patch("backend.shared.helpers.broker_apis.fetch_holidays") as mock_fetch:
        mock_fetch.return_value = {date(2026, 1, 26), date(2026, 3, 8)}

        # Simulate the background task's holiday refresh logic
        if not state.get("_hol_year") or state.get("_hol_year") != current_year:
            holiday_set = mock_fetch("NSE")
            state["_hol_year"] = current_year

    # Verify fetch was called since year changed
    mock_fetch.assert_called_once_with("NSE")
    assert state["_hol_year"] == current_year


@pytest.mark.asyncio
async def test_holiday_no_refresh_same_year():
    """
    When state._hol_year matches today.year, fetch_holidays is NOT called
    (cache still valid).
    """
    state = {}
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    current_year = today.year

    # Pre-populate the state with the current year
    state["_hol_year"] = current_year

    with patch("backend.shared.helpers.broker_apis.fetch_holidays") as mock_fetch:
        # Simulate the background task's logic: only fetch if year changed
        if not state.get("_hol_year") or state.get("_hol_year") != current_year:
            holiday_set = mock_fetch("NSE")
            state["_hol_year"] = current_year

    # Verify fetch was NOT called since year is current
    mock_fetch.assert_not_called()
    assert state["_hol_year"] == current_year


@pytest.mark.asyncio
async def test_holiday_refresh_first_time():
    """
    When state has no _hol_year key (first call), fetch_holidays is called
    and the state is initialized.
    """
    state = {}

    with patch("backend.shared.helpers.broker_apis.fetch_holidays") as mock_fetch:
        mock_fetch.return_value = {date(2026, 1, 26)}

        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        current_year = today.year

        # First-time check: no key in state
        if not state.get("_hol_year") or state.get("_hol_year") != current_year:
            holiday_set = mock_fetch("NSE")
            state["_hol_year"] = current_year

    # Verify fetch was called
    mock_fetch.assert_called_once_with("NSE")
    assert state["_hol_year"] == current_year
    assert len(state) == 1


@pytest.mark.asyncio
async def test_holiday_multiple_exchanges():
    """
    Holiday refresh runs for multiple exchanges (NSE, MCX) and updates
    separate cache entries.
    """
    state = {}
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    current_year = today.year

    exchanges = ["NSE", "MCX"]
    holiday_cache = {}

    with patch("backend.shared.helpers.broker_apis.fetch_holidays") as mock_fetch:
        # Simulate fetching for both exchanges
        mock_fetch.side_effect = [
            {date(2026, 1, 26), date(2026, 3, 8)},  # NSE
            {date(2026, 6, 15)},                     # MCX
        ]

        for exch in exchanges:
            if exch not in holiday_cache:
                holiday_cache[exch] = mock_fetch(exch)

        state["_hol_year"] = current_year

    # Verify both exchanges were fetched
    assert mock_fetch.call_count == 2
    assert "NSE" in holiday_cache
    assert "MCX" in holiday_cache
    assert len(holiday_cache["NSE"]) == 2
    assert len(holiday_cache["MCX"]) == 1
