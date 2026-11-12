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

from datetime import datetime, time as dtime
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


def test_format_market_status_message_open_with_reason():
    """2026-10-31, operator: 'for market status, you need to send reason
    also' — an OPEN line with a non-None reason (e.g. a Muhurat special
    session) must show it in parens, same convention CLOSED already used."""
    now = datetime(2026, 11, 1, 5, 35, tzinfo=IST)
    msg = _format_market_status_message(
        now,
        {"open": True, "hours": "18:00–19:00 IST", "reason": "Diwali Muhurat 2026"},
        {"open": True, "hours": "18:00–19:00 IST", "reason": "Diwali Muhurat 2026 (MCX)"},
    )
    assert "NSE/Equity: OPEN 18:00–19:00 IST (Diwali Muhurat 2026)" in msg
    assert "MCX/Commodity: OPEN 18:00–19:00 IST (Diwali Muhurat 2026 (MCX))" in msg


def test_format_market_status_message_open_without_reason_unchanged():
    """A plain weekday open (reason=None) keeps the old bare-hours form —
    no stray parens/suffix for the common case."""
    now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
    msg = _format_market_status_message(
        now,
        {"open": True, "hours": "09:15–15:30 IST", "reason": None},
        {"open": False, "hours": None, "reason": "Weekend"},
    )
    assert "NSE/Equity: OPEN 09:15–15:30 IST" in msg
    assert "NSE/Equity: OPEN 09:15–15:30 IST (" not in msg


@pytest.mark.asyncio
async def test_market_status_for_segment_open_special_session_carries_db_reason():
    """A real market_special_sessions row's own `reason` column (e.g.
    'Diwali Muhurat 2026') must reach the returned dict's `reason` field
    for an OPEN special session — previously hardcoded to None, silently
    discarding the row's own reason text."""
    now = datetime(2026, 11, 1, 5, 35, tzinfo=IST)
    special_row = {
        "date": now.date(), "start": dtime(18, 0), "end": dtime(19, 0),
        "reason": "Diwali Muhurat 2026",
    }
    with patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
         patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[special_row])):
        eq = await _market_status_for_segment("equity", now)
    assert eq["open"] is True
    assert eq["reason"] == "Diwali Muhurat 2026"


@pytest.mark.asyncio
async def test_market_status_for_segment_closed_special_session_uses_db_reason_when_present():
    """A zero-width special-session row with its own reason (e.g. the
    nse_auto MCX-evening-closed rows, which DO carry a real reason) must
    surface that reason, not the generic 'Special closure' fallback."""
    now = datetime(2026, 11, 1, 5, 35, tzinfo=IST)
    special_row = {
        "date": now.date(), "start": dtime(0, 0), "end": dtime(0, 0),
        "reason": "MCX evening session closed (NSE holiday feed)",
    }
    with patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
         patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[special_row])):
        co = await _market_status_for_segment("commodity", now)
    assert co["open"] is False
    assert co["reason"] == "MCX evening session closed (NSE holiday feed)"


@pytest.mark.asyncio
async def test_market_status_for_segment_closed_special_session_no_reason_falls_back():
    """Regression guard: a special-session row with no reason column
    populated (legacy row, or a test fixture omitting it) still falls
    back to the generic 'Special closure' string — pre-existing tests
    constructing a bare {date,start,end} dict must keep passing."""
    now = datetime(2026, 11, 1, 5, 35, tzinfo=IST)
    special_row = {"date": now.date(), "start": dtime(0, 0), "end": dtime(0, 0)}
    with patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
         patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[special_row])):
        co = await _market_status_for_segment("commodity", now)
    assert co["open"] is False
    assert co["reason"] == "Special closure"


@pytest.mark.asyncio
async def test_action_live_send_market_status_no_per_record_channels_override():
    """2026-11-03 conversion — no more direct sends. 2026-11-05
    simplification — channel selection moved to `MARKET_STATUS_ALERT_
    AGENT.events` (plain static config); the logged record itself no
    longer carries a per-record `channels` override at all. See
    test_market_status_alert.py::TestActionLiveSendMarketStatus for the
    full set of open/closed/mixed/lookup-failure cases, and its
    `test_market_status_alert_agent_uses_static_events_not_a_per_
    record_override` for the telegram-always/no-ntfy assertion."""
    now = datetime(2026, 10, 24, 5, 35, tzinfo=IST)  # Saturday — both closed
    with patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now), \
         patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
         patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])), \
         patch("backend.api.algo.agent_engine.get_agent_id_by_slug", new=AsyncMock(return_value=99)), \
         patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
        await _action_live_send_market_status({}, {})
    mock_logger.info.assert_called_once()
    extra = mock_logger.info.call_args.kwargs["extra"]
    assert extra["alert_event"] == "market_status"
    assert "channels" not in extra


@pytest.mark.asyncio
async def test_action_live_send_market_status_cron_call_has_no_trigger_keys():
    """2026-11 audit fix — `_ds_tick_market_status_report` (background.py)
    calls this handler directly with `context={}` for the scheduled
    05:35 IST cron case. No `agent_id`/`agent_name` key must be present
    on the record at all (key-presence, not truthiness — a fabricated
    `None` would make `resolve_source()` render "triggered manually" for
    a genuinely un-caused scheduled job)."""
    now = datetime(2026, 10, 24, 5, 35, tzinfo=IST)  # Saturday — both closed
    with patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now), \
         patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
         patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])), \
         patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
        await _action_live_send_market_status({}, {})
    extra = mock_logger.info.call_args.kwargs["extra"]
    assert extra["source_type"] == "process"
    assert extra["source_name"] == "Market status report"
    assert "agent_id" not in extra
    assert "agent_name" not in extra


@pytest.mark.asyncio
async def test_action_live_send_market_status_threads_real_agent_trigger_through():
    """When a custom agent's own `DO send_market_status()` call reaches
    this handler via `actions.py:execute()`, `context` carries the REAL
    firing agent's `agent_id`/`agent_name` (the same mechanism
    `send_summary()`/`action_send_summary.py` already use) — this must
    be threaded through onto the record, alongside the unconditional
    `source_type="process"`, rather than discarded."""
    now = datetime(2026, 10, 24, 5, 35, tzinfo=IST)
    context = {"agent_id": 42, "agent_name": "custom-market-watcher"}
    with patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now), \
         patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
         patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])), \
         patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
        await _action_live_send_market_status(context, {})
    extra = mock_logger.info.call_args.kwargs["extra"]
    assert extra["source_type"] == "process"
    assert extra["source_name"] == "Market status report"
    assert extra["agent_id"] == 42
    assert extra["agent_name"] == "custom-market-watcher"
