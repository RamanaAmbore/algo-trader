"""
2026-10 Alerts-tab merge — `GET /api/admin/alerts/history` now also
surfaces `LogEvent` rows tagged `"alert"` (the `kind='event'` log-tag
agent pipeline, which writes no `AgentEvent` row at all) alongside real
`AgentEvent` rows from the threshold/cycle-agent engine, sorted by their
REAL underlying timestamp, newest-first.

Unit-level: calls the Litestar route handler's own `.fn` directly (same
pattern `backend/tests/test_lab_mcp_ping_account_masking_cluster3.py`
uses for `OrdersController.cancel_order.fn`), with `async_session`
patched to return two independently-scripted fake sessions — the first
for the AgentEvent+Agent join query, the second for the LogEvent query.
"""
from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

import pytest

from backend.api.models import Agent, AgentEvent, LogEvent
from backend.api.routes import alerts as alerts_mod
from backend.api.routes.alerts import AlertsController


class _JoinResult:
    """Stub for `select(AgentEvent, Agent)...` — `.all()` returns
    `(AgentEvent, Agent)` tuples, matching the real SQLAlchemy shape."""
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _LogResult:
    """Stub for `select(LogEvent)...` — `.scalars().all()` returns bare
    `LogEvent` rows."""
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarsResult(self._rows)


class _FakeSession:
    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return self._result


def _patch_two_sessions(monkeypatch, agent_rows, log_rows):
    """`async_session()` is called twice in `alert_history` — once for
    the AgentEvent+Agent join, once (conditionally) for the LogEvent
    query. Each call must return a FRESH session object scripted with
    the right result."""
    sessions = iter([_FakeSession(_JoinResult(agent_rows)), _FakeSession(_LogResult(log_rows))])
    monkeypatch.setattr(alerts_mod, "async_session", lambda: next(sessions))


@pytest.mark.asyncio
async def test_log_event_tagged_alert_merges_and_sorts_with_agent_event(monkeypatch):
    now = _dt.datetime.now(_dt.timezone.utc)

    agent_row = Agent(id=1, name="Account funds gone negative", slug="loss-funds")
    older_agent_event = AgentEvent(
        id=10, agent_id=1, event_type="triggered", trigger_condition="cash < 0",
        detail="fired", sim_mode=False, timestamp=now - _dt.timedelta(minutes=5),
    )
    newer_log_event = LogEvent(
        id=99, ts=now, process="api", level="CRITICAL",
        logger="backend.api.algo.chase", message="Chase NIFTY: cancel unconfirmed",
        tags=["chase", "alert"], extra={"alert_event": "cancel_unconfirmed"},
    )

    _patch_two_sessions(monkeypatch, [(older_agent_event, agent_row)], [newer_log_event])

    ctrl = AlertsController(owner=None)
    result = await AlertsController.alert_history.fn(
        ctrl, limit=100, agent_id=None, agent_slug=None,
        since_minutes=1440, event_type=None, sim_mode=False,
    )

    assert len(result) == 2
    # Newest-first: the LogEvent row (ts=now) sorts ahead of the
    # AgentEvent row (ts=now-5min) — proves sorting uses the REAL
    # timestamp, not display order or insertion order.
    assert result[0].event_type == "log_alert"
    assert result[0].id == -99, "LogEvent-sourced ids are negated, namespaced away from real AgentEvent ids"
    assert result[0].agent_id == 0, "sentinel — no real Agent row behind a log-tag alert"
    assert result[0].agent_name == "chase", "last dotted segment of the logger name"
    assert "cancel unconfirmed" in result[0].detail
    assert result[0].channels_sent == []

    assert result[1].id == 10
    assert result[1].agent_id == 1
    assert result[1].agent_name == "Account funds gone negative"
    assert result[1].event_type == "triggered"


@pytest.mark.asyncio
async def test_sim_mode_filter_applies_to_log_event_rows_too(monkeypatch):
    """`LogEvent.extra['sim_mode']` is checked in Python (JSONB, not a
    dedicated column) — a sim-tagged row must not leak into a
    `sim_mode=False` (live) response, and vice versa."""
    now = _dt.datetime.now(_dt.timezone.utc)
    live_log = LogEvent(
        id=1, ts=now, process="api", level="WARNING", logger="backend.api.algo.chase",
        message="live one", tags=["chase", "alert"], extra={},
    )
    sim_log = LogEvent(
        id=2, ts=now, process="api", level="WARNING", logger="backend.api.algo.chase",
        message="sim one", tags=["chase", "alert"], extra={"sim_mode": True},
    )

    _patch_two_sessions(monkeypatch, [], [live_log, sim_log])
    ctrl = AlertsController(owner=None)
    result = await AlertsController.alert_history.fn(
        ctrl, limit=100, agent_id=None, agent_slug=None,
        since_minutes=1440, event_type=None, sim_mode=False,
    )
    assert len(result) == 1
    assert result[0].detail == "live one"
    assert result[0].sim_mode is False


@pytest.mark.asyncio
async def test_agent_scoped_filter_skips_the_log_event_query_entirely(monkeypatch):
    """When `agent_id`/`agent_slug`/`event_type` narrows the view, a
    LogEvent-sourced row (which has no real Agent FK to filter against)
    must not pollute it. `async_session` is scripted to be called only
    ONCE in this case — a second call would mean the LogEvent branch ran
    anyway, raising StopIteration when `_patch_two_sessions`'s iterator
    is exhausted, which would fail this test loudly rather than silently
    returning an empty LogEvent list."""
    now = _dt.datetime.now(_dt.timezone.utc)
    agent_row = Agent(id=1, name="Loss funds", slug="loss-funds")
    agent_event = AgentEvent(
        id=10, agent_id=1, event_type="triggered", trigger_condition="cash < 0",
        detail="fired", sim_mode=False, timestamp=now,
    )
    sessions = iter([_FakeSession(_JoinResult([(agent_event, agent_row)]))])
    monkeypatch.setattr(alerts_mod, "async_session", lambda: next(sessions))

    ctrl = AlertsController(owner=None)
    result = await AlertsController.alert_history.fn(
        ctrl, limit=100, agent_id=1, agent_slug=None,
        since_minutes=1440, event_type=None, sim_mode=False,
    )
    assert len(result) == 1
    assert result[0].id == 10
