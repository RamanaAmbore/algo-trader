"""Tests for grammar_tokens/action_send_market_status.py —
`send_market_status` action resolver (`_action_live_send_market_status`)
and its private helpers `_market_status_for_segment` / `_holiday_reason`
/ `_format_market_status_message`.

`backend/tests/test_market_status_alert.py` already carries extensive
coverage of these functions, imported via `backend.api.algo.actions_live`
(which keeps resolving via that module's backward-compatible re-export)
— not moved/duplicated here. This file provides fresh, location-anchored
baseline coverage at the new import path, per the per-token-file-plus-
test convention every other migrated token already follows.
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from backend.api.algo.grammar_tokens.action_send_market_status import (
    _market_status_for_segment,
    _format_market_status_message,
    _action_live_send_market_status,
)

IST = ZoneInfo("Asia/Kolkata")


def test_format_market_status_message_open_and_closed():
    now = datetime(2026, 10, 25, 5, 35, tzinfo=IST)
    msg = _format_market_status_message(
        now,
        {"open": True, "hours": "09:15–15:30 IST", "reason": None},
        {"open": False, "hours": None, "reason": "Weekend"},
    )
    assert "NSE/Equity: OPEN 09:15–15:30 IST" in msg
    assert "MCX/Commodity: CLOSED (Weekend)" in msg


@pytest.mark.asyncio
async def test_market_status_for_segment_open_weekday_reports_hours():
    now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)  # Thursday
    with patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
         patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])):
        eq = await _market_status_for_segment("equity", now)
    assert eq["open"] is True
    assert eq["hours"] == "09:15–15:30 IST"


@pytest.mark.asyncio
async def test_action_live_send_market_status_telegram_always_ntfy_conditional():
    now = datetime(2026, 10, 24, 5, 35, tzinfo=IST)  # Saturday — both closed
    with patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now), \
         patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
         patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])), \
         patch("backend.shared.helpers.alert_utils._send_telegram") as mock_tg, \
         patch("backend.shared.helpers.alert_utils.send_ntfy_alert") as mock_ntfy:
        await _action_live_send_market_status({}, {})
    mock_tg.assert_called_once()
    mock_ntfy.assert_not_called()
