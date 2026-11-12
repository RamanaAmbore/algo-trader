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

# Tags that trigger ntfy routing regardless of level (deliberate — see
# `_route_sinks()`'s 2026-11-08 audit fix (item 6): the floor lives at
# the CONSUMER, `event_agents.APP_MESSAGE_AGENT`, not here).
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

    2026-11-08 audit fix (item 9) — a CRITICAL `AppMessage` was
    confirmed silently NEVER reaching any channel. Root cause:
    `tags_for()` (`log_store.py`) stamps every CRITICAL record with
    "error" too, so `ERROR_AGENT`'s blanket catch-all
    (`tag="error", min_level="ERROR"`) condition always MATCHED a
    CRITICAL `AppMessage` — which is enough to satisfy
    `_record_matches_any_agent()`'s "is this record routed to
    SOMETHING" check, so `_run_unrouted_catch_all()` never fired its own
    self-alert either. But `ERROR_AGENT`'s OWN dispatch then refused to
    actually send, because `event_agents._error_agent_self_alerted()`
    treats any CRITICAL record carrying a truthy `alert_event` as
    "already routed to its own dedicated agent" — an assumption that
    held for every OTHER CRITICAL producer in this codebase (chase's
    `cancel_unconfirmed`, the GTT wing agents), each of which really
    does have its own dedicated agent, but was FALSE for
    `alert_event="app_message"`, which had none. Both "this looks
    routed" signals fired on a record nothing actually delivered.

    Fixed with a real dedicated agent
    (`event_agents.APP_MESSAGE_AGENT`, slug `app-message-alert`,
    matching `tag="alert", min_level="WARNING",
    where={"alert_event": "app_message"}` — the same shape every other
    migrated `generic_notice` agent uses) PLUS `self_alerted: True` on
    every record this sink emits (the SAME opt-out
    chase.py/actions_live.py already use for their own dedicated CRITICAL
    paths) — the self-alerted flag is what stops `ERROR_AGENT` from
    ALSO firing for the same record once the dedicated agent exists
    (`_error_agent_self_alerted` keys on that flag OR the CRITICAL+
    alert_event combination, and is scoped to `ERROR_AGENT`'s own slug
    only, so no other agent is affected). The "alert" tag is now added
    so the new agent's `tag="alert"` condition can match — the
    argument against adding it unconditionally (doubling through the
    unrouted catch-all) no longer applies, because a real dedicated
    agent now matches this record first, which is exactly what
    `_record_matches_any_agent()` checks.

    2026-11-08 audit fix (item 3) — the dedicated agent's DELIVERY was
    fixed above, but the record it dispatches still carried no
    `source_type`/`source_name` at all, so `_render_deploy_sync`
    (`APP_MESSAGE_AGENT`'s own renderer, registered under
    `"generic_notice"`) skipped building a header entirely — a CRITICAL
    `AppMessage` reached Telegram/ntfy/email with no identity line and
    no timestamp. `_route_sinks()` is itself the "process" that
    observed this condition and decided to alert — it genuinely has no
    agent/trigger concept (an `AppMessage` can originate from any
    caller, sync or async, with no `agent_id` ever threaded through the
    `AppMessage` dataclass) — so this is a real `source_type="process"`
    event, never a fabricated "manual".

    2026-11-08 audit fix (item 6) — a structural gap, closed at the
    CONSUMER, not at this gate. `should_alert` deliberately treats a
    bare `_NTFY_TAGS` match ("deploy"/"alert") as alert-worthy at ANY
    level, including INFO — this is explicit, tested intent
    (`test_app_message.py::test_route_sinks_logs_tagged_record_on_
    deploy_tag`, which pins an INFO-level "deploy"-tagged message as
    logged), not an oversight, and this function's own gate is
    therefore left unchanged. The real gap was one level down:
    `event_agents.APP_MESSAGE_AGENT` (the one real consumer of the
    record this sink dispatches) required `min_level="WARNING"`, so an
    INFO-level record that legitimately passed this gate (and the
    unrouted catch-all's own WARNING+ floor) still reached NO channel —
    silently dropped between "this sink decided to alert" and "nothing
    downstream was willing to deliver it". Not reachable by today's one
    real caller (`background.py`'s close-summary cron always logs
    `level="info"` with unrelated tags, never "deploy"/"alert"), but a
    future caller that tags "deploy"/"alert" at INFO would hit it. Fix:
    `APP_MESSAGE_AGENT["min_level"]` is lowered to `"INFO"` in
    `event_agents.py`'s `_GENERIC_NOTICE_SPECS`, matching this gate's
    own real floor exactly (there is no lower floor to match — the
    separate, unconditional DEBUG structural exemption in
    `_dispatch_agent` already drops every DEBUG-level record before any
    agent's `min_level` is even consulted).
    """
    tag_set = frozenset(msg.tags or [])
    should_alert = msg.level in ("error", "critical") or bool(tag_set & _NTFY_TAGS)
    if not should_alert:
        return
    try:
        title = msg.title or (msg.tags[0].capitalize() if msg.tags else msg.level.upper())
        level_no = _LEVEL_TO_LOGGING.get(msg.level, logging.INFO)
        tags = list(msg.tags or [])
        if "alert" not in tags:
            tags.append("alert")
        logger.log(
            level_no, "AppMessage: %s", msg.body,
            extra={"tags": tags, "alert_event": "app_message",
                   "title": title, "body": msg.body,
                   "source_type": "process", "source_name": "App message",
                   # Stops ERROR_AGENT's blanket catch-all from
                   # double-firing alongside APP_MESSAGE_AGENT — see
                   # this function's own docstring above.
                   "self_alerted": True},
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
