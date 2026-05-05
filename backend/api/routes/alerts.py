"""
Alert history endpoint.

GET /api/admin/alerts/history — recent agent_events with agent name and
                                structured summary, newest-first.

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
from backend.api.models import Agent, AgentEvent
from backend.shared.helpers.date_time_utils import format_dual_tz
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_VALID_EVENT_TYPES = {"triggered", "action_success", "action_failed", "cooldown"}


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

                rows = (await session.execute(stmt)).all()
        except Exception as exc:
            logger.error(f"AlertsController.alert_history DB error: {exc}")
            raise HTTPException(status_code=500, detail="Failed to query alert history")

        result: list[AlertEvent] = []
        for event, agent in rows:
            result.append(AlertEvent(
                id=event.id,
                agent_id=event.agent_id,
                agent_name=agent.name,
                event_type=event.event_type,
                triggered_at=format_dual_tz(event.timestamp),
                conditions_summary=_conditions_summary(event),
                channels_sent=_channels_from_detail(event),
                sim_mode=event.sim_mode,
                detail=_truncate(event.detail),
            ))
        return result
