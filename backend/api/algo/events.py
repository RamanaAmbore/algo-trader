"""
Agent event dispatcher — sends notifications through configured channels.

Each agent has an `events` list defining which channels to use:
  [{"channel": "telegram", "enabled": true}, {"channel": "email", "enabled": true}]

The alert message always includes the trigger condition text.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config, is_enabled

logger = get_logger(__name__)

# Module-level EventQueue for AgentEvent rows.
# _log_event enqueues here instead of opening a session per row.
# Started at app startup via start_event_queues() in app.py.
from backend.api.persistence.event_queue import EventQueue as _EventQueue

agent_event_queue: _EventQueue  # assigned below after import guard

def _make_agent_event_queue() -> _EventQueue:
    from backend.api.models import AgentEvent
    return _EventQueue(
        AgentEvent,
        name="agent_event",
        batch_size=500,
        flush_interval_s=1.0,
        max_queue=10_000,
        on_full="drop",
    )

agent_event_queue = _make_agent_event_queue()


@dataclass
class EvalResult:
    """Dispatch payload. The v2 grammar engine builds one of these from each
    fire so the existing dispatch() path keeps working unchanged."""
    triggered: bool
    condition_text: str
    detail: dict


def _build_dispatch_email_body(
    agent_name: str,
    sim_tag: str,
    branch: str,
    branch_tag: str,
    ist_display: str,
    condition_text: str,
    sim_mode: bool,
) -> str:
    """Build the HTML email body for an agent dispatch notification."""
    sim_banner = (
        "<p style='padding:8px;background:#fde4e4;border:1px solid #dc3545;"
        "border-radius:4px;color:#721c24'>🚨 <b>SIMULATOR RUN</b> — fabricated "
        "market data, not a real alert.</p>"
        if sim_mode else ""
    )
    branch_banner = (
        f"<p style='padding:8px;background:#fef3c7;border:1px solid #f59e0b;"
        f"border-radius:4px'>⚠ <b>Branch: {branch}</b></p>"
        if branch != "main" else ""
    )
    return (
        f"<html><body style='font-family:sans-serif'>"
        f"{sim_banner}{branch_banner}"
        f"<p><b>{sim_tag}Alert{branch_tag} — {agent_name}</b></p>"
        f"<p style='color:#666'>{ist_display}</p>"
        f"<p><b>Condition:</b> {condition_text}</p>"
        f"</body></html>"
    )


async def dispatch(agent, eval_result, broadcast_fn=None, sim_mode: bool = False):
    """
    Send alert through all enabled channels for an agent.

    Args:
        agent: Agent DB row
        eval_result: EvalResult built by the v2 agent engine when a condition fires
        broadcast_fn: optional WebSocket broadcast function
        sim_mode:    when True every surface (subjects, preambles, logs) is
                     prefixed with SIMULATOR so simulated fires are never
                     confused with real ones.

    Alert message format (same across channels):
        Alert [branch] — <Agent Name>
        Condition: <condition_text>

    The branch tag is shown only on non-main deploys.
    """
    from backend.shared.helpers.date_time_utils import timestamp_display

    branch = config.get("deploy_branch", "main")
    branch_tag = f" [{branch}]" if branch != "main" else ""
    sim_tag    = "SIMULATOR " if sim_mode else ""
    ist_display = timestamp_display()
    condition_text = eval_result.condition_text or ""

    # Single unified content shown across all channels
    body_lines = [
        f"{sim_tag}Alert{branch_tag} — {agent.name}",
        f"When: {ist_display}",
        f"Condition: {condition_text}",
    ]
    telegram_body = "\n".join(body_lines)
    email_subject = f"RamboQuant {sim_tag}Agent{branch_tag}: {agent.name}"
    email_body = _build_dispatch_email_body(
        agent.name, sim_tag, branch, branch_tag, ist_display, condition_text, sim_mode
    )

    # Resolve any `{"$ref": "<notify-fragment>"}` entries against the
    # fragment registry. Plain `{channel, enabled}` entries pass through
    # unchanged. Missing refs log a warning and are skipped — the rest
    # of the channels still fire.
    from backend.api.algo.template_registry import resolve_events
    channels = resolve_events(agent.events if isinstance(agent.events, list) else [])

    for ch in channels:
        if not ch.get("enabled", False):
            continue
        channel = ch.get("channel", "")

        try:
            await _dispatch_channel(
                channel, agent, telegram_body, email_subject, email_body,
                condition_text, ist_display, eval_result, broadcast_fn,
                sim_mode, branch, branch_tag,
            )
        except Exception as e:
            logger.error(f"Agent event dispatch failed ({channel}): {e}")

    # Persist to agent_events table (sim_mode flag flows through)
    await _log_event(agent, "triggered", condition_text, eval_result.detail,
                     sim_mode=sim_mode)


async def _dispatch_channel(
    channel: str, agent, telegram_body: str, email_subject: str,
    email_body: str, condition_text: str, ist_display: str,
    eval_result, broadcast_fn, sim_mode: bool, branch: str, branch_tag: str,
) -> None:
    """Route one channel event. Raises on error — caller wraps in try/except."""
    if channel == "telegram" and is_enabled("telegram"):
        await _send_telegram(telegram_body)
    elif channel == "email" and is_enabled("mail"):
        await _send_email_raw(email_subject, email_body)
    elif channel == "websocket" and broadcast_fn:
        broadcast_fn("agent_alert", {
            "slug": agent.slug,
            "message": telegram_body,
            "condition": condition_text,
            "sim_mode": sim_mode,
        })
    elif channel == "inapp" and broadcast_fn:
        broadcast_fn("agent_inapp_notify", {
            "slug":      agent.slug,
            "name":      agent.name,
            "tier":      getattr(agent, "tier", "info"),
            "topic":     getattr(agent, "topic", None),
            "condition": condition_text,
            "detail":    eval_result.detail or {},
            "when":      ist_display,
            "sim_mode":  sim_mode,
            "branch":    branch,
        })
    elif channel == "ntfy" and is_enabled("ntfy"):
        from backend.shared.helpers.alert_utils import send_ntfy_alert
        send_ntfy_alert(title=agent.name, message=telegram_body)
    elif channel == "log":
        log_sim_tag = "[SIM] " if sim_mode else ""
        logger.warning(f"{log_sim_tag}ALERT [{agent.slug}]{branch_tag}: {agent.name} — {condition_text}")


async def log_event(agent, event_type: str, condition_text: str = "",
                    detail: dict = None, sim_mode: bool = False):
    """Convenience wrapper for logging agent events."""
    await _log_event(agent, event_type, condition_text, detail, sim_mode=sim_mode)


async def _log_event(agent, event_type: str, condition_text: str = "",
                     detail: dict = None, sim_mode: bool = False):
    """Enqueue an agent_events row for batched INSERT (1 s flush cycle).

    No longer opens a session per call — `agent_event_queue` coalesces
    N fires per cycle into one bulk INSERT so run_cycle() bursts don't
    generate N individual round-trips.
    """
    try:
        await agent_event_queue.enqueue(
            agent_id=agent.id,
            event_type=event_type,
            trigger_condition=condition_text,
            detail=json.dumps(detail) if detail else None,
            sim_mode=sim_mode,
        )
    except Exception as e:
        logger.error(f"Agent event enqueue failed: {e}")


async def _send_telegram(message: str):
    """Send Telegram alert using existing infrastructure."""
    from backend.shared.helpers.alert_utils import _send_telegram as tg_send
    from concurrent.futures import ThreadPoolExecutor
    import asyncio

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, tg_send, message)


async def _send_email_raw(subject: str, html_body: str):
    """Send an HTML email to all alert recipients."""
    from backend.shared.helpers.alert_utils import get_alert_recipients
    from backend.shared.helpers.mail_utils import send_email
    import asyncio

    alert_emails = get_alert_recipients()
    loop = asyncio.get_running_loop()
    for email in alert_emails:
        try:
            await loop.run_in_executor(None, send_email, "RamboQuant", email, subject, html_body)
        except Exception as e:
            logger.error(f"Agent email failed to {email}: {e}")
