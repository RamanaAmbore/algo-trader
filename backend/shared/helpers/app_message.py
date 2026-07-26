"""
Unified AppMessage dispatcher.

Every operator-visible system event — deploy, summary, alert, error —
passes through here so the operator has a single queryable surface
(GET /api/messages) plus consistent push routing.

Usage
-----
Sync context (background tasks, CLI scripts):
    from backend.shared.helpers.app_message import AppMessage, fire
    fire(AppMessage(level="info", tags=["summary", "nse"], body="NSE close"))

Async context (route handlers, background coroutines):
    from backend.shared.helpers.app_message import AppMessage, dispatch
    await dispatch(AppMessage(level="info", tags=["summary"], body="..."))

Tags drive both sink routing and retention. Level takes priority for retention:
critical/error → permanent, warning → 90 days, info → tag-based TTL.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level async_session import (guarded — unavailable in some test envs)
# ---------------------------------------------------------------------------
try:
    from backend.api.database import async_session
except ImportError:
    async_session = None  # type: ignore

# ---------------------------------------------------------------------------
# Retention map — info-level messages only; tag → days kept.
# Shortest retention wins when multiple tags are present.
# critical/error are always permanent; warning is always 90 days.
# ---------------------------------------------------------------------------
_TAG_RETENTION_DAYS: dict[str, int] = {
    "order":    30,
    "chase":    30,
    "template": 30,
    "agent":    30,
    "broker":   14,
    "conn":     14,
    "system":    7,
    "deploy":    7,
    "market":    7,
}

# Tags that make a message ephemeral — skip DB write when ALL tags are
# in this set (no operator review value; would flood the table).
_EPHEMERAL_TAGS: frozenset[str] = frozenset({"news", "terminal", "simulator"})

# Tags that trigger ntfy routing regardless of level.
_NTFY_TAGS: frozenset[str] = frozenset({"deploy", "alert"})


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class AppMessage:
    """One operator-visible notification record.

    Attributes
    ----------
    level   : "debug" | "info" | "warning" | "error" | "critical"
    tags    : arbitrary labels for routing / filtering / retention
    body    : human-readable message body (required)
    title   : short title shown in push notifications (optional)
    account : masked broker account string, e.g. "AB####" (optional)
    symbol  : instrument symbol, e.g. "CRUDEOIL" (optional)
    data    : arbitrary JSON payload for downstream consumers (optional)
    """
    level:   str
    tags:    list[str]
    body:    str
    title:   Optional[str]           = None
    account: Optional[str]           = None
    symbol:  Optional[str]           = None
    data:    Optional[dict[str, Any]] = field(default=None)


# ---------------------------------------------------------------------------
# Retention helper
# ---------------------------------------------------------------------------

def _retain_until(msg: AppMessage) -> Optional[date]:
    """Return the retention cutoff date for this message.

    None  → keep forever (critical/error level, or untagged info).
    date  → delete on or after this date (nightly cleanup cron).

    Priority:
      1. critical/error level → permanent (None)
      2. warning level        → 90 days
      3. info/debug           → shortest tag-based TTL; None if no match
    """
    if msg.level in ("critical", "error"):
        return None
    if msg.level == "warning":
        return date.today() + timedelta(days=90)
    # info / debug — use shortest tag-based retention
    min_days: Optional[int] = None
    for tag in (msg.tags or []):
        if tag in _EPHEMERAL_TAGS:
            continue  # ephemeral tags won't be stored; skip for retention calc
        days = _TAG_RETENTION_DAYS.get(tag)
        if days is not None and (min_days is None or days < min_days):
            min_days = days
    return (date.today() + timedelta(days=min_days)) if min_days is not None else None


# ---------------------------------------------------------------------------
# Sink routing — ntfy push
# ---------------------------------------------------------------------------

async def _route_sinks(msg: AppMessage) -> None:
    """Fire ntfy push for messages that meet routing criteria (best-effort).

    Ntfy is sent for:
      - error or critical level
      - any tag in _NTFY_TAGS (deploy, alert)

    Generic info/system ticks and ephemeral (news) messages are skipped.
    """
    tag_set = frozenset(msg.tags or [])
    should_ntfy = msg.level in ("error", "critical") or bool(tag_set & _NTFY_TAGS)
    if not should_ntfy:
        return
    try:
        from backend.shared.helpers.alert_utils import send_ntfy_alert  # type: ignore
        title = msg.title or (msg.tags[0].capitalize() if msg.tags else msg.level.upper())
        await asyncio.to_thread(send_ntfy_alert, title, msg.body)
    except Exception as exc:
        logger.debug("AppMessage: ntfy sink failed: %s", exc)


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

async def _write_db(msg: AppMessage) -> None:
    """Persist AppMessage to the app_messages table (best-effort)."""
    try:
        if async_session is None:
            return
        from backend.api.models import AppMessage as AppMessageModel  # type: ignore
        row = AppMessageModel(
            level=msg.level,
            tags=msg.tags or [],
            title=msg.title,
            body=msg.body,
            account=msg.account,
            symbol=msg.symbol,
            data=msg.data,
            retain_until=_retain_until(msg),
        )
        async with async_session() as session:
            session.add(row)
            await session.commit()
    except Exception as exc:
        logger.warning("AppMessage: DB write failed: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def dispatch(msg: AppMessage) -> None:
    """Async dispatch — write to DB, then fire sinks as background task.

    DB write is skipped when the message is ephemeral (all tags in
    _EPHEMERAL_TAGS). Sink routing always fires as a background task.
    """
    tag_set = frozenset(msg.tags or [])
    is_ephemeral = bool(tag_set) and tag_set.issubset(_EPHEMERAL_TAGS)

    if not is_ephemeral:
        await _write_db(msg)
    asyncio.create_task(_route_sinks(msg))


def fire(msg: AppMessage) -> None:
    """Sync fire-and-forget — schedule dispatch on the running event loop.

    Safe to call from sync background-task helpers and CLI scripts.
    Silently no-ops when there is no running loop (e.g. subprocess
    contexts like notify_deploy.py where asyncio has not been started).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(dispatch(msg))
