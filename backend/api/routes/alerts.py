"""
Alert history endpoint.

GET /api/admin/alerts/history — recent agent_events with agent name and
                                structured summary, newest-first.

Also merges in `LogEvent` rows tagged `"alert"` (2026-10) — the
`kind='event'` log-tag agent pipeline (`event_agents.py`'s
`dispatch()`/`_dispatch_agent`) delivers Telegram/ntfy/email directly
and writes NOTHING to any DB table, so before this merge those alerts
(order-failure, fill, chase-cancel, partial-gtt, template-attach,
gtt-not-accepted, order-correction, mcp-ping) were invisible in this
endpoint and the Alerts tab that reads it. `rich_alert`/`breach` log
records are deliberately NOT tagged `"alert"` — both already have a
real originating cycle-agent firing recorded as an `AgentEvent` row, so
tagging them here would double the same firing in the merged response.

Admin-only. No demo access.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import msgspec
from litestar import Controller, get
from litestar.exceptions import HTTPException
from sqlalchemy import select

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session
from backend.api.models import Agent, AgentEvent, LogEvent
from backend.shared.helpers.date_time_utils import format_dual_tz
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_VALID_EVENT_TYPES = {"triggered", "action_success", "action_failed", "cooldown"}

# `event_type` literal for a LogEvent-sourced row — distinguishes it
# from the four real AgentEvent lifecycle values above; never matched
# against `_VALID_EVENT_TYPES` since the LogEvent branch is skipped
# whenever an `event_type` filter is active (see `alert_history`).
_LOG_ALERT_EVENT_TYPE = "log_alert"

# `AlertEvent.agent_id` is a non-optional int (the AgentEvent FK) — a
# LogEvent-sourced row has no real Agent row to point at, so this
# reserved sentinel (0, never a real autoincrement id) fills the field
# rather than lying about an agent that doesn't exist.
_LOG_ALERT_AGENT_ID_SENTINEL = 0


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AlertEvent(msgspec.Struct):
    id: int
    agent_id: int
    agent_name: str
    event_type: str
    triggered_at: str   # dual-tz formatted via format_dual_tz()
    conditions_summary: str  # human-readable matched-condition summary
    channels_sent: list[str]  # e.g. ["telegram", "email", "websocket"]
    sim_mode: bool
    detail: str         # truncated to 240 chars


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conditions_summary(event: AgentEvent) -> str:
    """
    Extract a human-readable condition summary from the AgentEvent row.

    `trigger_condition` is a free-text string written by the agent engine
    at fire time (e.g. "pnl <= -50000 @ ZG#### (matched)"). Return it
    verbatim when present; fall back to the event_type if the column is
    empty (older rows predating the column).
    """
    raw = (event.trigger_condition or "").strip()
    if raw:
        return raw[:200]
    return event.event_type


def _channels_from_detail(event: AgentEvent) -> list[str]:
    """
    Derive notification channels from the event detail field.

    The agent engine writes detail strings that reference the delivery
    path (e.g. "[telegram] sent", "[email] sent"). Parse them out.
    When the detail is JSON, look for a 'channels' key written by
    action_success events. Falls back to an empty list — channels are
    best-effort metadata; the event is valid without them.
    """
    detail_raw = (event.detail or "").strip()
    if not detail_raw:
        return []

    # Try JSON first — action_success events from newer engine versions
    # may carry a structured payload with an explicit 'channels' list.
    try:
        parsed = json.loads(detail_raw)
        if isinstance(parsed, dict) and "channels" in parsed:
            raw_channels = parsed["channels"]
            if isinstance(raw_channels, list):
                return [str(c) for c in raw_channels]
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: scan the text for known channel names.
    channels: list[str] = []
    lower = detail_raw.lower()
    for ch in ("telegram", "email", "websocket", "log"):
        if ch in lower:
            channels.append(ch)
    return channels


def _truncate(text: Optional[str], limit: int = 240) -> str:
    if not text:
        return ""
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _build_history_stmt(
    sim_mode: bool,
    effective_limit: int,
    since_dt: Optional[datetime],
    agent_id: Optional[int],
    agent_slug: Optional[str],
    event_type: Optional[str],
):
    """Construct the SQLAlchemy select statement for alert_history queries."""
    stmt = (
        select(AgentEvent, Agent)
        .join(Agent, AgentEvent.agent_id == Agent.id)
        .where(AgentEvent.sim_mode == sim_mode)
        .order_by(AgentEvent.timestamp.desc())
        .limit(effective_limit)
    )
    if since_dt is not None:
        stmt = stmt.where(AgentEvent.timestamp >= since_dt)
    if agent_id is not None:
        stmt = stmt.where(AgentEvent.agent_id == agent_id)
    elif agent_slug:
        stmt = stmt.where(Agent.slug == agent_slug)
    if event_type:
        stmt = stmt.where(AgentEvent.event_type == event_type)
    return stmt


# ---------------------------------------------------------------------------
# LogEvent ("alert"-tagged) merge — the log-tag event-agent pipeline
# ---------------------------------------------------------------------------

def _build_log_alert_stmt(effective_limit: int, since_dt: Optional[datetime]):
    """Select statement for `LogEvent` rows tagged `"alert"`.

    `sim_mode` is NOT filtered in SQL — it lives inside the JSONB
    `extra` column (`extra->>'sim_mode'`), not a dedicated column the
    way `AgentEvent.sim_mode` is, so it is checked in Python after
    fetch (see `alert_history`). Same `since_dt`/`effective_limit`
    bounds as the AgentEvent query above, independently applied —
    both lists are merged and re-truncated to `effective_limit` after
    sorting by their real timestamps."""
    stmt = (
        select(LogEvent)
        .where(LogEvent.tags.overlap(["alert"]))
        .order_by(LogEvent.ts.desc())
        .limit(effective_limit)
    )
    if since_dt is not None:
        stmt = stmt.where(LogEvent.ts >= since_dt)
    return stmt


def _log_event_agent_label(logger_name: Optional[str]) -> str:
    """Best-effort `agent_name` for a LogEvent-sourced row — there is no
    real Agent row behind a log-tag alert, so this is the last dotted
    segment of the producing logger's name (e.g. "chase" for
    `backend.api.algo.chase`), matching `log_store.py:tags_for()`'s own
    logger-name fallback convention. Falls back to the literal "system"
    when even that's empty (defensive; every real logger name is
    non-empty in practice)."""
    name = (logger_name or "").rsplit(".", 1)[-1]
    return name or "system"


def _log_event_to_alert(event: LogEvent) -> AlertEvent:
    """Map one `LogEvent` row into the same `AlertEvent` struct the
    AgentEvent query returns, so `LogPanel.svelte`'s Alerts tab needs no
    frontend change at all — it already consumes whatever AlertEvent-
    shaped rows this endpoint returns, by field name."""
    extra = event.extra or {}
    return AlertEvent(
        id=-event.id,
        agent_id=_LOG_ALERT_AGENT_ID_SENTINEL,
        agent_name=_log_event_agent_label(event.logger),
        event_type=_LOG_ALERT_EVENT_TYPE,
        triggered_at=format_dual_tz(event.ts),
        conditions_summary=_truncate(event.message, 200),
        channels_sent=[],
        sim_mode=bool(extra.get("sim_mode", False)),
        detail=_truncate(event.message),
    )


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class AlertsController(Controller):
    path = "/api/admin/alerts"
    guards = [admin_guard]

    @get("/history")
    async def alert_history(
        self,
        limit: int = 100,
        agent_id: Optional[int] = None,
        agent_slug: Optional[str] = None,
        since_minutes: int = 1440,
        event_type: Optional[str] = None,
        sim_mode: bool = False,
    ) -> list[AlertEvent]:
        """
        Return recent agent_events ordered newest-first.

        Query params:
          limit          — max rows returned (capped at 1000).
          agent_id       — filter to a specific agent by id.
          agent_slug     — filter to a specific agent by slug. If both
                           agent_id and agent_slug are provided, agent_id
                           wins (more specific).
          since_minutes  — look-back window in minutes, default 1440 (24h).
                           Pass 0 (or any value < 1) to disable the time
                           filter entirely (return everything).
          event_type     — one of triggered/action_success/action_failed/cooldown
                           (omit for all types).
          sim_mode       — False (default) returns live events;
                           True returns simulator events.

        Also merges in `LogEvent` rows tagged `"alert"` (2026-10) — see
        the module docstring. Skipped entirely when `agent_id`,
        `agent_slug`, or `event_type` is set: a LogEvent-sourced row has
        no real Agent FK to filter by, and an agent-scoped or
        event_type-scoped view must not fill with unrelated rows.
        """
        if event_type and event_type not in _VALID_EVENT_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"event_type must be one of {sorted(_VALID_EVENT_TYPES)} or omitted",
            )

        effective_limit = max(1, min(int(limit), 1000))
        # since_minutes <= 0 disables the time filter entirely (the "All"
        # period option in the UI sends since_minutes=0 to mean no bound).
        apply_time_filter = since_minutes is not None and since_minutes > 0
        since_dt = (
            datetime.now(tz=timezone.utc) - timedelta(minutes=int(since_minutes))
            if apply_time_filter else None
        )

        try:
            async with async_session() as session:
                stmt = _build_history_stmt(
                    sim_mode, effective_limit, since_dt,
                    agent_id, agent_slug, event_type,
                )
                rows = (await session.execute(stmt)).all()
        except Exception as exc:
            logger.error(f"AlertsController.alert_history DB error: {exc}")
            raise HTTPException(status_code=500, detail="Failed to query alert history")

        # (real_ts, AlertEvent) pairs — sorted/truncated by the REAL
        # underlying timestamp below, never the already dual-tz-formatted
        # `triggered_at` display string.
        items: list[tuple[datetime, AlertEvent]] = []
        for event, agent in rows:
            items.append((event.timestamp, AlertEvent(
                id=event.id,
                agent_id=event.agent_id,
                agent_name=agent.name,
                event_type=event.event_type,
                triggered_at=format_dual_tz(event.timestamp),
                conditions_summary=_conditions_summary(event),
                channels_sent=_channels_from_detail(event),
                sim_mode=event.sim_mode,
                detail=_truncate(event.detail),
            )))

        if agent_id is None and agent_slug is None and event_type is None:
            try:
                async with async_session() as session:
                    log_stmt = _build_log_alert_stmt(effective_limit, since_dt)
                    log_rows = (await session.execute(log_stmt)).scalars().all()
            except Exception as exc:
                logger.error(f"AlertsController.alert_history LogEvent query failed: {exc}")
                log_rows = []
            for log_event in log_rows:
                extra = log_event.extra or {}
                # `sim_mode` lives in JSONB `extra`, not a dedicated
                # column — absent means False (CLAUDE.md missing-vs-zero
                # convention: a producer that never set it is a live
                # record, not an unknown one).
                if bool(extra.get("sim_mode", False)) != sim_mode:
                    continue
                items.append((log_event.ts, _log_event_to_alert(log_event)))

        items.sort(key=lambda pair: pair[0], reverse=True)
        return [alert for _, alert in items[:effective_limit]]
