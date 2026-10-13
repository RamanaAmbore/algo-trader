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
    agent_id: "int | None",
    sim_tag: str,
    branch: str,
    branch_tag: str,
    ist_display: str,
    condition_text: str,
    sim_mode: bool,
) -> str:
    """Build the HTML email body for an agent dispatch notification."""
    import html as _html
    from backend.shared.helpers.alert_utils import format_notification_header
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
    # format_notification_header's output replaces the bare ist_display
    # paragraph — it carries both the new "Agent: {name} (#{id})" line
    # AND the same dual-tz timestamp that used to be the only content
    # here, so nothing is lost and the timestamp isn't shown twice.
    header_html = "<br>".join(
        _html.escape(line)
        for line in format_notification_header(agent_name, agent_id, ist_display=ist_display).splitlines()
    )
    return (
        f"<html><body style='font-family:sans-serif'>"
        f"{sim_banner}{branch_banner}"
        f"<p><b>{sim_tag}Alert{branch_tag} — {agent_name}</b></p>"
        f"<p style='color:#666'>{header_html}</p>"
        f"<p><b>Condition:</b> {condition_text}</p>"
        f"</body></html>"
    )


def _build_dispatch_bodies(
    message_text: str, agent, sim_tag: str, branch: str, branch_tag: str,
    ist_display: str, sim_mode: bool,
) -> tuple[str, str, str, str]:
    """Build telegram/ntfy/email bodies for one alert message.

    Shared by the default (condition-text) group and each per-channel
    custom-message record (Sprint 6 `msg=` grammar keyword) so both render
    through identical formatting logic — only the one line that used to
    always be `condition_text` changes.

    The old "When:" line is replaced by format_notification_header's
    output — it still carries the same dual-tz ist_display (reused via the
    ist_display= kwarg so telegram/ntfy/email all share one timestamp
    instead of each calling timestamp_display() a moment apart) PLUS the
    "Agent: {name} (#{id})" identity line.
    """
    from backend.shared.helpers.alert_utils import format_notification_header
    body_lines = [
        f"{sim_tag}Alert{branch_tag} — {agent.name}",
        format_notification_header(agent.name, agent.id, ist_display=ist_display),
        f"Condition: {message_text}",
    ]
    telegram_body = "\n".join(body_lines)
    # Dedicated ntfy body — kept a separate variable (not just an alias for
    # telegram_body) even though the content is identical today. ntfy has no
    # HTML rendering; message_text is operator/user-authored (e.g.
    # "pnl < -5000") and must never be run through an HTML-tag-stripping
    # regex, since a bare comparison operator would be misread as a tag
    # boundary. Keeping this independent means if telegram_body ever grows
    # HTML styling (as alert_utils._dispatch's equivalent string did), ntfy
    # can't silently inherit literal <b>/<code> tags — see
    # backend/shared/helpers/alert_utils.py:_html_to_plain for the sibling
    # fix on that path.
    ntfy_body = "\n".join(body_lines)
    email_subject = f"RamboQuant {sim_tag}Agent{branch_tag}: {agent.name}"
    email_body = _build_dispatch_email_body(
        agent.name, agent.id, sim_tag, branch, branch_tag, ist_display, message_text, sim_mode
    )
    return telegram_body, ntfy_body, email_subject, email_body


def _describe_action_params(params: dict, keys: tuple[str, ...]) -> list[str]:
    """Render up to the given param keys (in order) as plain values,
    skipping any key that's absent/None/empty. A dict value (e.g. a
    `{"$row": "symbol"}` matched-row marker, or any other structured
    param) renders as `<name>` using its own first value rather than a
    raw Python repr, since this text can reach an operator-facing
    channel body."""
    out = []
    for k in keys:
        v = params.get(k)
        if v in (None, ""):
            continue
        if isinstance(v, dict):
            out.append(f"<{next(iter(v.values()), k)}>")
        else:
            out.append(str(v))
    return out


def _describe_action(action: dict) -> str:
    """Render one `agent.actions` entry (`{"type": ..., "params": {...}}`)
    as a short, human-readable summary for the action-aware default ALERT
    message (Part A, 2026-10-12). Special-cases the handful of common
    action types for a natural phrasing; anything else falls back to a
    generic `type(key=val, ...)` rendering using whatever params exist —
    this is deliberately NOT a full per-type registry.

    `agent.actions` here is the AGENT'S OWN COMPILED (not yet resolved/
    executed) action list — dispatch() runs before execute() for every
    survivor entry (agent_engine.py:_ae_dispatch_survivor_entry), so a
    param may still carry a raw `token_ref_ok` expression string or a
    `$row` matched-row marker rather than its final resolved value. This
    only renders what's already present in the stored params, same as
    every other fallback in this function — no I/O, no DB.

    Never masks account text itself — the caller (`_describe_actions`)
    masks the FULLY JOINED description text once, via the same
    `mask_account_in_text()` chokepoint every other alert surface already
    uses (see event_agents.py `_render_error`/`_gate_passes`).
    """
    if not isinstance(action, dict):
        return ""
    action_type = action.get("type") or ""
    params = action.get("params") if isinstance(action.get("params"), dict) else {}

    if action_type in ("place_order", "order"):
        bits = _describe_action_params(params, ("side", "qty", "lots", "symbol"))
        label = " ".join(bits)
        price = params.get("price")
        if price not in (None, ""):
            label = f"{label} @ {price}".strip()
        return f"{label} (order)".strip() if label else "order"
    if action_type == "close_position":
        bits = _describe_action_params(params, ("symbol",))
        return f"closed {bits[0]} (close_position)" if bits else "close_position"
    if action_type in ("chase_close", "chase_close_positions"):
        scope = params.get("scope") or params.get("account") or "total"
        return f"chase-close {scope} (chase_close_positions)"
    if action_type == "cancel_all_orders":
        scope = params.get("scope") or params.get("account") or "total"
        return f"cancel all orders {scope} (cancel_all_orders)"
    if action_type in ("cancel_order", "modify_order"):
        bits = _describe_action_params(params, ("broker_order_id",))
        return f"{action_type}({bits[0]})" if bits else action_type
    if action_type == "expiry_auto_close":
        bits = _describe_action_params(params, ("exchange",))
        return f"expiry-close {bits[0]} (expiry_auto_close)" if bits else "expiry_auto_close"

    # Generic fallback — type(key=val, ...) for anything not special-cased
    # above (emit_log, set_flag, monitor_order, deactivate_agent,
    # send_summary, or any future/custom token), using up to the first 3
    # params verbatim, in whatever order the stored dict carries them.
    if not params:
        return action_type
    parts = [f"{k}={v}" for k, v in list(params.items())[:3]]
    return f"{action_type}({', '.join(parts)})"


def _describe_actions(actions: "list | None") -> str:
    """Render an agent's full `DO` action list as one short summary,
    for the action-aware no-`msg=` default ALERT message (Part A,
    2026-10-12 — see docs/specs and the plan file's "Alert/notify
    messaging" section). Multiple actions join with '; '.

    Returns "" for an empty/absent action list (ALERT-only / event-kind
    agents with no DO clause) — the caller must then fall back to
    EXACTLY today's condition-breach-only text, no appended "()" noise.

    Masking: the joined text is passed through `mask_account_in_text()` —
    the SAME chokepoint `event_agents.py`'s `_render_error`/`_gate_passes`
    already use to scrub free-form alert text — before being returned, so
    a raw account code in an action's own `account=` param (e.g.
    `order(account="ZG0790", ...)`) can never reach an operator-facing
    channel body unmasked. No second/new masking mechanism is introduced.
    """
    if not actions:
        return ""
    try:
        descs = [d for d in (_describe_action(a) for a in actions) if d]
    except TypeError:
        # Defensive: a non-list-like `actions` (e.g. an unconfigured test
        # double that doesn't support iteration) degrades to "no actions"
        # rather than raising out of the alert-dispatch path.
        return ""
    if not descs:
        return ""
    from backend.shared.helpers.utils import mask_account_in_text
    return mask_account_in_text("; ".join(descs)) or ""


def _default_message_text(condition_text: str, action_desc: str) -> str:
    """Combine the condition-breach text with the action-aware summary
    for the no-`msg=` default ALERT body. Either half may be empty
    (no action_desc -> byte-identical to pre-change behavior; no
    condition_text -> action_desc alone, no stray leading separator)."""
    if condition_text and action_desc:
        return f"{condition_text} — {action_desc}"
    return action_desc or condition_text


def _classify_recorded_channel(
    ch: dict, agent, eval_result, condition_text: str, branch_tag: str, sim_mode: bool,
) -> "tuple | None":
    """Build one channel's resolved record for the _RECORDED_CHANNELS path.

    Returns None when the channel's tags don't match this fire. Otherwise
    returns (custom_message_or_None, rec_ch). custom_message is None when
    this channel carries no operator-authored `message` override (an empty
    string normalizes to None too) — the caller groups every such channel
    into ONE shared logger.info() record, byte-identical to the
    pre-grouping (Sprint 6) behaviour. A channel with a real custom message
    gets its own separate record (built by the caller via
    _build_dispatch_bodies) instead of sharing the condition_text body.
    """
    if not _tags_match(ch, eval_result):
        return None
    rec_ch = {k: v for k, v in ch.items() if k in ("channel", "priority", "tags", "message")}
    custom_message = rec_ch.pop("message", None) or None
    # Tier-driven ntfy priority default — only when the operator hasn't
    # hand-configured an explicit priority for this channel, and only for
    # ntfy (telegram/email sends don't accept a priority kwarg; injecting
    # one there would make every send silently fail). `agent` here is the
    # REAL firing Agent row (not a dispatching meta-agent), so `.tier`
    # genuinely reflects this alert's severity.
    if rec_ch.get("channel") == "ntfy" and not rec_ch.get("priority"):
        from backend.shared.helpers.alert_utils import _TIER_TO_NTFY_PRIORITY
        tier_priority = _TIER_TO_NTFY_PRIORITY.get(getattr(agent, "tier", None))
        if tier_priority:
            rec_ch["priority"] = tier_priority
    rec_ch = rec_ch | {"enabled": True, "gate": True}
    if rec_ch.get("channel") == "log":
        # event_agents.py's `log` CHANNELS entry has no per-channel render
        # output of its own (BREACH_AGENT's renderer only carries the
        # SHARED telegram/ntfy/email bodies) — so the fully rendered log
        # line, plus the fields the old events.py `_log_channel` used
        # (agent slug, sim_mode), are pre-baked onto this channel's own
        # record here and read back by event_agents._send_channel's "log"
        # branch. Deliberately omits "alert_event" from anything this
        # produces downstream — event_agents._send_log_channel's own
        # logger.warning call must never carry "alert_event": "breach" or
        # it would re-match BREACH_AGENT's own condition on the next log
        # flush and loop forever.
        log_msg_text = custom_message if custom_message is not None else condition_text
        log_sim_tag = "[SIM] " if sim_mode else ""
        rec_ch["log_line"] = (
            f"{log_sim_tag}ALERT [{agent.slug}]{branch_tag}: {agent.name} — {log_msg_text}"
        )
        rec_ch["log_agent_slug"] = agent.slug
        rec_ch["log_sim_mode"] = bool(sim_mode)
    # Put a REAL (non-empty) custom message back on the record — popped
    # above only to decide grouping, but an operator reading `channels` in
    # a persisted agent_events row should still see which message this
    # channel actually rendered. A messageless channel's record keeps no
    # "message" key at all (byte-identical to pre-Sprint-6 shape).
    if custom_message is not None:
        rec_ch["message"] = custom_message
    return custom_message, rec_ch


def _emit_breach_record(
    agent, branch_tag: str, condition_text: str, channels: list,
    telegram_body: str, ntfy_body: str, email_subject: str, email_body: str,
    sim_mode: bool,
) -> None:
    """One 'ALERT recorded' log line — the single hand-off point into
    event_agents.py's BREACH_AGENT renderer. Shared by the default
    (messageless) group and each per-channel custom-message record so
    both paths produce byte-identical extra shapes (only `channels` /
    the three body fields differ)."""
    logger.info(
        f"ALERT recorded [{agent.slug}]{branch_tag}: {agent.name}",
        extra={
            "tags": ["agent"], "alert_event": "breach", "agent_slug": agent.slug,
            "agent_name": agent.name, "agent_id": agent.id, "condition_text": condition_text,
            "channels": channels, "telegram_body": telegram_body,
            "ntfy_body": ntfy_body, "email_subject": email_subject,
            "email_body": email_body, "sim_mode": bool(sim_mode),
        },
    )


async def dispatch(agent, eval_result, broadcast_fn=None, sim_mode: bool = False,
                   skip_channels: frozenset = frozenset()):
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

    A channel entry carrying a `message` key (set by the frontend CLI
    compiler's `msg=` keyword) gets ITS OWN separate alert record, rendered
    from that message text instead of condition_text — see
    _classify_recorded_channel / _build_dispatch_bodies. Every other
    channel (the majority of production traffic) still shares ONE record,
    byte-identical to pre-Sprint-6 output.
    """
    if not is_enabled('agent_alerts'):
        return

    from backend.shared.helpers.date_time_utils import timestamp_display

    branch = config.get("deploy_branch", "main")
    branch_tag = f" [{branch}]" if branch != "main" else ""
    sim_tag    = "SIMULATOR " if sim_mode else ""
    ist_display = timestamp_display()
    condition_text = eval_result.condition_text or ""

    # Part A (2026-10-12) — action-aware default message. Only affects the
    # SHARED/default body built here (messageless telegram/email/ntfy/log
    # channels, plus websocket/inapp's own direct dispatch below) — a
    # channel/clause with an explicit `msg=` never reaches this variable at
    # all; it renders from `custom_message` via the separate loop further
    # down, byte-identical to pre-change behavior. `condition_text` itself
    # (used for persistence, websocket/inapp's "condition" field, and
    # _tags_match) is deliberately left unchanged.
    action_desc = _describe_actions(getattr(agent, "actions", None))
    default_message_text = _default_message_text(condition_text, action_desc)

    telegram_body, ntfy_body, email_subject, email_body = _build_dispatch_bodies(
        default_message_text, agent, sim_tag, branch, branch_tag, ist_display, sim_mode,
    )

    # Resolve any `{"$ref": "<notify-fragment>"}` entries against the
    # fragment registry. Plain `{channel, enabled}` entries pass through
    # unchanged. Missing refs log a warning and are skipped — the rest
    # of the channels still fire.
    from backend.api.algo.template_registry import resolve_events
    channels = resolve_events(agent.events if isinstance(agent.events, list) else [])

    # Telegram, email, ntfy, and log are sent by the event path from one or
    # more tagged records (grouped by message — see _classify_recorded_
    # channel), so their text and routing are decided in one place.
    # Websocket and in-app stay here: they are UI broadcasts, not something
    # the log-driven BREACH_AGENT renderer delivers.
    recorded_channels = []
    custom_channels: list[tuple[str, dict]] = []
    for ch in channels:
        if not ch.get("enabled", False):
            continue
        if ch.get("channel") in skip_channels:
            continue
        if ch.get("channel") in _RECORDED_CHANNELS:
            classified = _classify_recorded_channel(
                ch, agent, eval_result, condition_text, branch_tag, sim_mode,
            )
            if classified is not None:
                custom_message, rec_ch = classified
                if custom_message is None:
                    recorded_channels.append(rec_ch)
                else:
                    custom_channels.append((custom_message, rec_ch))
            continue
        try:
            await _dispatch_channel(
                ch, agent, telegram_body, email_subject, email_body,
                condition_text, ist_display, eval_result, broadcast_fn,
                sim_mode, branch, branch_tag, ntfy_body=ntfy_body,
            )
        except Exception as e:
            logger.error(f"Agent event dispatch failed ({ch.get('channel', '')}): {e}")

    if recorded_channels:
        _emit_breach_record(
            agent, branch_tag, condition_text, recorded_channels,
            telegram_body, ntfy_body, email_subject, email_body, sim_mode,
        )

    for custom_message, rec_ch in custom_channels:
        msg_telegram_body, msg_ntfy_body, _msg_subject, msg_email_body = _build_dispatch_bodies(
            custom_message, agent, sim_tag, branch, branch_tag, ist_display, sim_mode,
        )
        _emit_breach_record(
            agent, branch_tag, condition_text, [rec_ch],
            msg_telegram_body, msg_ntfy_body, email_subject, msg_email_body, sim_mode,
        )

    # Persist to agent_events table (sim_mode flag flows through)
    await _log_event(agent, "triggered", condition_text, eval_result.detail,
                     sim_mode=sim_mode)


_RECORDED_CHANNELS = frozenset({"telegram", "email", "ntfy", "log"})


def _tags_match(ch: dict, eval_result) -> bool:
    wanted = set(ch.get("tags") or [])
    if not wanted:
        return True
    matched = {t for m in (eval_result.detail or {}).get("matches", []) for t in (m.get("tags") or [])}
    return bool(wanted & matched)


def _broadcast_websocket(broadcast_fn, agent, telegram_body, condition_text, sim_mode) -> None:
    broadcast_fn("agent_alert", {
        "slug": agent.slug,
        "message": telegram_body,
        "condition": condition_text,
        "sim_mode": sim_mode,
    })


def _broadcast_inapp(broadcast_fn, agent, condition_text, eval_result, ist_display, sim_mode, branch) -> None:
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


async def _send_ntfy_channel(ch: dict, agent, telegram_body: str, ntfy_body: str | None) -> None:
    from backend.shared.helpers.alert_utils import send_ntfy_alert, _TIER_TO_NTFY_PRIORITY
    import asyncio
    loop = asyncio.get_running_loop()
    # ntfy_body is kept independent of telegram_body so an HTML-styled telegram body can
    # never leak literal tags into ntfy. Legacy direct-call sites fall back to telegram_body.
    _ntfy_msg = ntfy_body if ntfy_body is not None else telegram_body
    # Explicit per-channel priority wins; otherwise fall back to this
    # agent's own tier (critical/high/medium/low -> urgent/high/default/
    # low). `agent` here is the real firing Agent row. Neither set ->
    # priority stays None, same as before this change, and
    # send_ntfy_alert's own clock-based day/night default applies.
    priority = ch.get("priority") or _TIER_TO_NTFY_PRIORITY.get(getattr(agent, "tier", None))
    await loop.run_in_executor(None, lambda: send_ntfy_alert(title=agent.name, message=_ntfy_msg, priority=priority))


async def _dispatch_channel(
    ch: dict, agent, telegram_body: str, email_subject: str,
    email_body: str, condition_text: str, ist_display: str,
    eval_result, broadcast_fn, sim_mode: bool, branch: str, branch_tag: str,
    ntfy_body: str | None = None,
) -> None:
    """Route one channel event. Raises on error — caller wraps in try/except.

    `branch_tag` is kept in the signature for call-site/positional-arg
    stability (websocket/inapp callers and existing tests) even though no
    branch in this function body reads it any more — `log` moved to
    _RECORDED_CHANNELS / event_agents.py's CHANNELS table (Sprint 6) and
    was the only consumer.
    """
    channel = ch.get("channel", "")
    if not _tags_match(ch, eval_result):
        return
    if channel == "telegram" and is_enabled("telegram"):
        await _send_telegram(telegram_body)
    elif channel == "email" and is_enabled("mail"):
        await _send_email_raw(email_subject, email_body)
    elif channel == "websocket" and broadcast_fn:
        _broadcast_websocket(broadcast_fn, agent, telegram_body, condition_text, sim_mode)
    elif channel == "inapp" and broadcast_fn:
        _broadcast_inapp(broadcast_fn, agent, condition_text, eval_result, ist_display, sim_mode, branch)
    elif channel == "ntfy" and is_enabled("ntfy"):
        await _send_ntfy_channel(ch, agent, telegram_body, ntfy_body)


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
    """Send Telegram alert as plain text — agent condition text may contain < > operators."""
    from backend.shared.helpers.alert_utils import _send_telegram as tg_send
    import asyncio

    loop = asyncio.get_running_loop()
    # pass parse_mode="" so Telegram treats message as plain text;
    # avoids 400 "Unsupported start tag" when condition text contains < or >.
    await loop.run_in_executor(None, tg_send, message, "")


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
