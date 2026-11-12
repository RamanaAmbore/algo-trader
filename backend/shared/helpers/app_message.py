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

from backend.shared.helpers.ramboq_logger import get_logger

# 2026-11 alert-agent audit fix — a bare `logging.getLogger(__name__)`
# has no `QueueHandler` of its own and is not guaranteed to reach
# `log_events`/the event-agent dispatch pipeline at all (see
# `ramboq_logger.get_logger()`'s own docstring for why propagation to a
# parent logger's handler cannot be relied on). `_route_sinks()` below
# now logs instead of calling `send_ntfy_alert()` directly, so this
# module needs the real queue-backed logger, not the stdlib default.
logger = get_logger(__name__)

# Level-name -> stdlib logging level, for `_route_sinks()`'s `logger.log()`
# call below.
_LEVEL_TO_LOGGING: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

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
    """Log-and-dispatch sink for messages that meet routing criteria.

    Routing criteria (unchanged from the pre-fix direct-ntfy gate):
      - error or critical level
      - any tag in _NTFY_TAGS (deploy, alert)

    Generic info/system ticks and ephemeral (news) messages are skipped
    — same no-op as before.

    2026-11 alert-agent audit fix — this used to call
    `send_ntfy_alert()` directly via `asyncio.to_thread`, a second,
    parallel notification pipeline outside `event_agents.py`'s control.
    It never fired in production (the only live caller, `background.
    py`'s close-summary cron, always passes level="info",
    tags=["summary", <seg>], which never satisfies the gate above), but
    it was reachable by any future caller. Fixed by logging a single
    tagged record instead of sending directly — no direct sender call
    remains in this module.

    `tags_for()` (`log_store.py`) already stamps every record's own
    level name as a tag (and, for CRITICAL, also "error"), so an
    error/critical AppMessage reaches `ERROR_AGENT`'s catch-all
    (`tag="error", min_level="ERROR"`) for free, with no synthetic tag
    added here. Deliberately NOT adding an "alert" tag unconditionally:
    doing so would make every error/critical AppMessage also look
    "alert-shaped" to `event_agents.py:_looks_alert_shaped()` and
    double-fire through the unrouted-catch-all self-alert on top of
    `ERROR_AGENT`. A caller that wants deploy/alert-style routing sets
    its own `tags` on the `AppMessage` (e.g. `["deploy"]`) — forwarded
    verbatim below — which, combined with WARNING+ level, is still
    picked up by `UNROUTED_ALERT_AGENT`'s catch-all the same way any
    other unmatched alert-shaped record is. No live caller passes such
    tags today (see this module's own docstring), so this is a
    correctly-shaped path for a future caller, not a currently
    exercised one. No dedicated seeded agent is added for
    `alert_event="app_message"` for the same reason — nothing fires at
    error/critical today, so there is nothing yet for a dedicated
    agent to cover beyond what `ERROR_AGENT`'s existing catch-all
    already provides.
    """
    tag_set = frozenset(msg.tags or [])
    should_alert = msg.level in ("error", "critical") or bool(tag_set & _NTFY_TAGS)
    if not should_alert:
        return
    try:
        title = msg.title or (msg.tags[0].capitalize() if msg.tags else msg.level.upper())
        level_no = _LEVEL_TO_LOGGING.get(msg.level, logging.INFO)
        logger.log(
            level_no, "AppMessage: %s", msg.body,
            extra={"tags": list(msg.tags or []), "alert_event": "app_message",
                   "title": title, "body": msg.body},
        )
    except Exception as exc:
        logger.debug("AppMessage: log-and-dispatch sink failed: %s", exc)


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
