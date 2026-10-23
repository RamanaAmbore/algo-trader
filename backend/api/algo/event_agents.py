"""Event agents: each new log record is matched at once and sent without cooldown, latch, or cycle.

An event agent is an agents row with kind='event'. Its conditions are a log leaf, its
events list names the channels, and its actions carry a render key that picks the message
format. Only the API process dispatches, so each record is sent once.
"""
import asyncio
import html
import sys
import time
from datetime import timezone
from types import SimpleNamespace

from sqlalchemy import select

from backend.api.algo import fill_notify
from backend.api.algo.agent_evaluator import Context, evaluate
from backend.shared.helpers.error_alerts import RepeatGate, clean_message
from backend.shared.helpers.ramboq_logger import get_logger

# Logger name deliberately pinned to "backend.api.algo.events" (NOT this
# module's own __name__) — the "log" channel's send function below is a
# direct port of events.py's old _log_channel, and every LogEvent row it
# writes must keep showing up under that same logger name so stored rows
# look identical to what they did before the Sprint 6 move.
_EVENTS_LOGGER = get_logger("backend.api.algo.events")

_CACHE_S = 60.0
_cache: dict = {"at": float("-inf"), "agents": []}


def _render_fill(rec: dict) -> tuple[str, str]:
    """Order fill notification — side, qty, symbol, fill price."""
    extra = rec.get("extra") or {}
    row = SimpleNamespace(
        id=extra.get("order_id"), account=extra.get("account"), symbol=extra.get("symbol"),
        exchange=extra.get("exchange"), transaction_type=extra.get("transaction_type"),
        quantity=extra.get("quantity"), fill_price=extra.get("fill_price"), product=extra.get("product"),
        engine=extra.get("engine"), agent_name=extra.get("agent_name"),
    )
    when = rec.get("ts")
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return fill_notify.format_fill_message(row, when=when)


RENDERS: dict = {}
_unknown_renders: set[str] = set()
_gate = None


def _get_gate():
    global _gate
    if _gate is None:
        from backend.shared.helpers.alert_utils import _get_redis
        from backend.shared.helpers.error_alerts import SharedRepeatGate
        client = _get_redis()
        _gate = SharedRepeatGate(client) if client is not None else RepeatGate()
    return _gate


def _render_chase_cancel(rec: dict) -> tuple[str, str]:
    """Chase cancel-and-replace could not confirm the cancel landed."""
    from backend.shared.helpers.utils import mask_account_in_text
    from backend.shared.helpers.date_time_utils import timestamp_display
    from backend.shared.helpers.alert_utils import format_notification_header
    x = rec.get("extra") or {}
    # 2026-10 audit fix: the prior wording
    # "after attempt {attempt}/{quantity-remaining_qty} filled." reads
    # like a fraction (attempt N of M attempts), but the denominator is
    # actually "contracts already filled so far" — an unrelated number.
    # Spelled out unambiguously below.
    filled_so_far = x.get('quantity', 0) - x.get('remaining_qty', 0)
    # 2026-10 audit fix: origin header leads every alert (order-scoped —
    # `agent_id` threaded from the chase loop's own AlgoOrder context,
    # "manual" when the chased order was placed by a human).
    origin_header = format_notification_header(None, x.get('agent_id'), ist_display=timestamp_display())
    body = (
        f"{origin_header}\n"
        f"{x.get('transaction_type')} {x.get('symbol')} — cancel of order {x.get('order_id')} "
        f"on {x.get('account')} could not be confirmed after attempt "
        f"{x.get('attempt')} ({filled_so_far} contracts filled so far). "
        f"The chase has been ABORTED without placing a replacement order. "
        f"Manually verify the broker's order book — the old order may still be live."
    )
    return "Chase cancel unconfirmed — possible resting duplicate order", mask_account_in_text(body)


def _render_partial_gtt(rec: dict) -> tuple[str, str]:
    """Only some of a parent order's planned GTTs were placed."""
    from backend.shared.helpers.date_time_utils import timestamp_display
    from backend.shared.helpers.alert_utils import format_notification_header
    x = rec.get("extra") or {}
    # 2026-10 audit fix: origin header leads every alert.
    origin_header = format_notification_header(None, x.get('agent_id'), ist_display=timestamp_display())
    body = (
        f"{origin_header}\n"
        f"parent #{x.get('parent_row_id')} {x.get('parent_symbol')}: "
        f"{x.get('placed')}/{x.get('planned')} GTTs placed. "
        f"Errors: {'; '.join(x.get('errors') or [])}"
    )
    return "Partial GTT placement", body


def _render_template_attach(rec: dict) -> tuple[str, str]:
    """Template exit attach outcome — unprotected position, wing skip/reject."""
    from backend.shared.helpers.date_time_utils import timestamp_display
    x = rec.get("extra") or {}
    ev = x.get("alert_event")
    ts = timestamp_display()
    if ev == "wing_unprotected":
        return ("Unprotected SELL position",
                f"GTTs placed (ids: {x['gtt_ids_text']}) but wing failed: {x['reason']} | "
                f"order #{x['parent_order_id']} {x['symbol']} {x['exchange']}\n{ts}")
    if ev == "wing_hard_reject":
        return ("Wing scan hard-rejected",
                f"{x['reason']} | {x['symbol']} {x['exchange']} target ₹{x['target_premium']:.2f}\n{ts}")
    if ev == "wing_skip":
        return ("Wing attach skipped",
                f"{x['reason']} | order #{x['parent_order_id']} {x['symbol']} {x['exchange']}\n{ts}")
    if ev == "wing_offset_skip":
        return ("Wing offset attach skipped",
                f"{x['reason']} | order #{x['parent_order_id']} {x['symbol']} {x['exchange']}\n{ts}")
    return "Template attach", f"{x}\n{ts}"


def _render_order_failure(rec: dict) -> tuple:
    """A live order was rejected or failed to place."""
    from backend.shared.helpers.alert_utils import order_failure_messages
    from backend.shared.helpers.alert_utils import _html_to_plain
    x = rec.get("extra") or {}
    tg_body, subject, email_body = order_failure_messages(
        masked=x["masked"], symbol=x["symbol"], exchange=x["exchange"], side=x["side"],
        qty=x["qty"], mode=x["mode"], source=x["source"], error=x["error"],
        suppressed_count=x["suppressed_count"], ist_disp=x["ist_disp"],
        agent_id=x.get("agent_id"),
    )
    return f"Order Rejected: {x['symbol']} {x['side']}", _html_to_plain(tg_body), tg_body, (subject, email_body)


def _render_template_guard(rec: dict) -> tuple:
    """A template exit guard fired (held/released/blocked an exit).

    4th element (email subject, body) added 2026-10 audit fix —
    TEMPLATE_GUARD_AGENT now carries an `email` channel per
    `template_attach.py:_fire_guard_alert`'s own docstring promise
    ("Email lands in the alert inbox — durable record for end-of-day
    review"), which previously had no renderer support for it at all.
    """
    from backend.api.algo.template_attach import template_guard_message
    from backend.shared.helpers.alert_utils import _html_to_plain
    x = rec.get("extra") or {}
    keys = ("template_slug", "applies_to", "reason", "parent_order_id", "parent_side", "parent_qty",
            "parent_symbol", "parent_fill_price", "parent_account", "ist_label", "agent_id")
    tg = template_guard_message(**{k: x.get(k) for k in keys})
    title = "Template guard fired"
    email_subject = f"RamboQuant {title}: {x.get('parent_symbol') or ''}"
    email_body = f"<html><body style='font-family:sans-serif'>{tg}</body></html>"
    return title, _html_to_plain(tg), tg, (email_subject, email_body)


def _render_attach_fail(rec: dict) -> tuple:
    """A template's exit GTTs/wing failed to attach to the filled parent.

    4th element (email subject, body) added 2026-10 audit fix — see
    `_render_template_guard`'s docstring for the rationale (same
    "durable record" promise, same prior gap)."""
    from backend.api.algo.template_attach import template_attach_fail_message
    from backend.shared.helpers.alert_utils import _html_to_plain
    x = rec.get("extra") or {}
    tg = template_attach_fail_message(order_id=x.get("order_id"), symbol=x.get("symbol"),
                                      account=x.get("account"), err_summary=x.get("err_summary") or "",
                                      ist_label=x.get("ist_label"), agent_id=x.get("agent_id"))
    title = "Template attach failed"
    email_subject = f"RamboQuant {title}: {x.get('symbol') or ''}"
    email_body = f"<html><body style='font-family:sans-serif'>{tg}</body></html>"
    return title, _html_to_plain(tg), tg, (email_subject, email_body)


def _render_order_correction(rec: dict) -> tuple[str, str]:
    """2026-10-23 order-poll consolidation — a background corrector
    (the open-order watchdog, the periodic order-poll sweep, or
    postback's non-blocking follow-up verification) found and fixed a
    stuck AlgoOrder row. This firing is itself the signal that some
    live path already failed to do its job on its own — see
    `backend/api/algo/order_poll.py`'s module docstring for exactly
    which callers fire it (deliberately NOT `list_active_chases` or
    the operator-triggered admin reconcile routes).

    Leads with `format_notification_header()` (2026-10-23 "every alert
    leads with its origin" invariant — same convention every other
    order-scoped renderer in this file uses, e.g. `_render_order_failure`
    via `order_failure_messages`) so this renderer doesn't silently skip
    the origin line every other renderer carries. `agent_id` comes from
    the corrected AlgoOrder row itself (`order_poll.py:_opo_postprocess`'s
    `static_agent_id`) — `None` resolves to "manual" via `resolve_origin`,
    same as any other agent-less order."""
    from backend.shared.helpers.alert_utils import format_notification_header
    x = rec.get("extra") or {}
    header = format_notification_header(None, x.get("agent_id"))
    title = f"Order corrected: {x.get('symbol') or ''} #{x.get('algo_order_id')}"
    body = (
        f"{header}\n"
        f"AlgoOrder #{x.get('algo_order_id')} {x.get('masked') or ''} "
        f"{x.get('symbol') or ''} {x.get('exchange') or ''}: "
        f"{x.get('prior_status')} -> {x.get('new_status')} "
        f"(source={x.get('source') or 'order-poll'})"
    )
    return title, body


def _render_mcp_ping(rec: dict) -> tuple[str, str, str]:
    """An MCP tool call performed an audited action (place/cancel/modify/...)."""
    from backend.shared.helpers.alert_utils import _html_to_plain
    tg = (rec.get("extra") or {}).get("tg") or ""
    return "MCP", _html_to_plain(tg), tg


def _render_deploy_sync(rec: dict) -> tuple[str, str]:
    """dev/main branches or their deploy webhooks have drifted out of sync.

    Also registered as the "generic_notice" renderer (see
    `register_renderer("generic_notice")` below) — this function was
    already a pure (title, body) passthrough with no deploy-sync-
    specific logic, so it's reused as-is for the 2026-10 audit fix that
    migrated 5 background.py alert call sites (gtt_asymmetric,
    oco_double_fire, ticker_recovered, ticker_degraded, visitor_report)
    off the deleted `alert_utils._alert_route()` dispatcher onto the
    log-record + event-agent pipeline every other alert in this codebase
    already uses. Kept under its original "deploy_sync" render key too
    so DEPLOY_SYNC_AGENT's own behaviour is unchanged.
    """
    x = rec.get("extra") or {}
    return x.get("title") or "Deploy out of sync", x.get("body") or ""


def _render_rich_alert(rec: dict) -> tuple:
    """A threshold ('cycle') agent fired — full rich alert table."""
    from backend.shared.helpers.alert_utils import dispatch_payload, _html_to_plain
    x = rec.get("extra") or {}
    p = dispatch_payload("alert", x["ist_display"], x["tg_table"], x["email_table_html"],
                         x["subject_detail"], sim_mode=bool(x.get("sim_mode")),
                         mode_tag=x.get("mode_tag") or "",
                         agent_name=x.get("agent_name"), agent_id=x.get("agent_id"))
    return p["title"], _html_to_plain(p["telegram_msg"]), p["telegram_msg"], (p["email_subject"], p["email_html"])


def _render_summary(rec: dict) -> tuple:
    """Open/close market performance summary."""
    from backend.shared.helpers.alert_utils import dispatch_payload, _html_to_plain
    x = rec.get("extra") or {}
    p = dispatch_payload(x["msg_type"], x["ist_display"], x["tg_table"], x["email_table_html"],
                         x["subject_detail"], sim_mode=False, mode_tag="")
    return p["title"], _html_to_plain(p["telegram_msg"]), p["telegram_msg"], (p["email_subject"], p["email_html"])


def _render_breach(rec: dict) -> tuple:
    """An agent-engine cycle took longer than its breach threshold."""
    x = rec.get("extra") or {}
    return (x.get("agent_name") or "", x.get("ntfy_body") or "", x.get("telegram_body") or "",
            (x.get("email_subject") or "", x.get("email_body") or ""), list(x.get("channels") or []))


def _render_gtt_not_accepted(rec: dict) -> tuple[str, str]:
    """A placed GTT was not accepted by the broker (status mismatch)."""
    from backend.shared.helpers.date_time_utils import timestamp_display
    from backend.shared.helpers.alert_utils import format_notification_header
    x = rec.get("extra") or {}
    # 2026-10 audit fix: origin header leads every alert.
    origin_header = format_notification_header(None, x.get('agent_id'), ist_display=timestamp_display())
    return ("GTT not accepted at broker",
            f"{origin_header}\n"
            f"{x.get('symbol')} {x.get('label')} GTT {x.get('gtt_id')}: {x.get('reason')}")


def _render_error(rec: dict) -> tuple[str, str, str]:
    """An ERROR+ log line was written somewhere in the app.

    2026-10 audit fix: this renderer is NOT order-scoped (a generic
    ERROR+ log line, from anywhere in the app, rarely carries an
    `agent_id` in its `extra` dict) — so the origin header below almost
    always resolves to "Agent: manual". That's an accepted, known
    imprecision ("no identifiable originating agent" is the honest
    answer for a generic error log), not a claim that the error was
    literally caused by a manual order. Any future caller that DOES
    attach a real `agent_id` to an error-tagged log record gets the
    real name for free.
    """
    from backend.shared.helpers.utils import mask_account_in_text
    from backend.shared.helpers.date_time_utils import timestamp_display
    from backend.shared.helpers.alert_utils import format_notification_header
    x = rec.get("extra") or {}
    name = rec.get("logger") or ""
    msg = clean_message(mask_account_in_text(rec.get("message") or "") or "")
    repeats = rec.get("repeats") or 0
    suffix = f" (+{repeats} repeats)" if repeats else ""
    ts = timestamp_display()
    origin_line = format_notification_header(None, x.get("agent_id"), ist_display=ts).splitlines()[0]
    tg = (f"{html.escape(origin_line)}\n<b>RamboQuant error</b>\n<code>{html.escape(name)}</code>\n"
          f"{html.escape(msg)}{html.escape(suffix)}\n{html.escape(ts)}")
    return "RamboQuant error", f"{origin_line}\n{name}\n{msg}{suffix}\n{ts}", tg


def register_renderer(name: str):
    def deco(fn):
        RENDERS[name] = fn
        return fn
    return deco


register_renderer("fill")(_render_fill)
register_renderer("error")(_render_error)
register_renderer("chase_cancel")(_render_chase_cancel)
register_renderer("partial_gtt")(_render_partial_gtt)
register_renderer("template_attach")(_render_template_attach)
register_renderer("order_failure")(_render_order_failure)
register_renderer("template_guard")(_render_template_guard)
register_renderer("template_attach_fail")(_render_attach_fail)
register_renderer("mcp_ping")(_render_mcp_ping)
register_renderer("deploy_sync")(_render_deploy_sync)
register_renderer("generic_notice")(_render_deploy_sync)
register_renderer("rich_alert")(_render_rich_alert)
register_renderer("summary")(_render_summary)
register_renderer("breach")(_render_breach)
register_renderer("gtt_not_accepted")(_render_gtt_not_accepted)
register_renderer("order_correction")(_render_order_correction)


def _send_ntfy(title: str, body: str, tg: str | None = None, priority: str | None = None) -> None:
    from backend.shared.helpers import alert_utils
    alert_utils.send_ntfy_alert(title, body, priority=priority)


def _send_telegram_html(title: str, body: str, tg: str | None = None) -> None:
    from backend.shared.helpers import alert_utils
    alert_utils._send_telegram(tg or f"<b>{html.escape(title)}</b>\n{html.escape(body)}")




def _render_key(agent) -> str | None:
    for action in agent.actions or []:
        if isinstance(action, dict) and action.get("type") == "render":
            return action.get("render")
    return None


def _channel_enabled(capability: str | None) -> bool:
    if capability is None:
        return True
    from backend.shared.helpers.utils import is_enabled
    return is_enabled(capability)


def resolve_email_recipients(recipients) -> list[str]:
    """'alert' (default) is the current alert list. A list is explicit addresses; anything without '@' is dropped."""
    from backend.shared.helpers.alert_utils import get_alert_recipients
    if recipients in (None, "alert"):
        return list(get_alert_recipients())
    if isinstance(recipients, (list, tuple)):
        return [a for a in recipients if isinstance(a, str) and "@" in a]
    return []


def _send_email_channel(title: str, body: str, tg: str | None = None,
                        email: tuple | None = None, recipients=None, **_kw) -> None:
    from backend.shared.helpers.mail_utils import send_email
    if not email:
        return
    subject, html_body = email
    for addr in resolve_email_recipients(recipients):
        try:
            send_email("RamboQuant", addr, subject, html_body)
        except Exception as e:
            sys.stderr.write(f"event_agents: email to {addr} failed: {e}\n")


def _send_telegram_info_html(title: str, body: str, tg: str | None = None) -> None:
    from backend.shared.helpers import alert_utils
    alert_utils._send_telegram_info(tg or f"<b>{html.escape(title)}</b>\n{html.escape(body)}")


def _send_log_channel(title: str, body: str, tg: str | None = None, *,
                      log_line: str | None = None, agent_slug: str | None = None,
                      sim_mode: bool = False, **_kw) -> None:
    """Plain log write — the 'log' channel's entire delivery IS this
    logger.warning call. No broadcast_fn, no external service — fully
    self-contained, same as before this channel moved off
    events.py's old direct-dispatch `_log_channel`.

    `log_line`/`agent_slug`/`sim_mode` are pre-built by
    events.py:_classify_recorded_channel onto this channel's own record
    (the generic `out` render tuple only carries the SHARED telegram/ntfy/
    email bodies, not a per-channel one) and threaded through by
    `_send_channel`'s "log" branch below. Falls back to `title — body`
    when called directly without those (e.g. a future renderer that
    doesn't pre-build a log line).

    The `extra` dict here is deliberately identical to the old
    `_log_channel`'s shape — `tags`/`agent_slug`/`sim_mode` only, NEVER
    `alert_event` — adding `alert_event: "breach"` would make this very
    write re-match BREACH_AGENT's own log condition on the next flush and
    loop forever.
    """
    _EVENTS_LOGGER.warning(
        log_line or f"{title} — {body}",
        extra={"tags": ["agent"], "agent_slug": agent_slug, "sim_mode": bool(sim_mode)},
    )


CHANNELS = {
    "ntfy": ("ntfy", _send_ntfy),
    "telegram": ("telegram", _send_telegram_html),
    "telegram_info": (None, _send_telegram_info_html),
    "email": ("mail", _send_email_channel),
    "log": (None, _send_log_channel),
}


def _gate_spec(agent) -> bool:
    return any(isinstance(a, dict) and a.get("type") == "render" and a.get("gate")
               for a in agent.actions or [])


def _gate_passes(agent, rec: dict, gated: bool) -> dict | None:
    """Apply the agent's repeat gate. Returns the record to render, or None to skip it."""
    if not gated:
        return rec
    from backend.shared.helpers.utils import mask_account_in_text
    gate_key = f"{rec.get('logger')}|{clean_message(mask_account_in_text(rec.get('message') or '') or '')}"
    repeats = _get_gate().decide(gate_key, alert_now=bool((rec.get("extra") or {}).get("alert_now")))
    return None if repeats is None else {**rec, "repeats": repeats}


async def _send_channel(ch: dict, out: tuple, agent) -> bool:
    spec = CHANNELS.get(ch.get("channel")) if isinstance(ch, dict) else None
    if spec is None or not ch.get("enabled"):
        return False
    capability, send = spec
    if ch.get("gate", True) and not _channel_enabled(capability):
        return False
    title, body = out[0], out[1]
    tg = out[2] if len(out) > 2 else None
    priority = ch.get("priority")
    # Tier-driven default — ntfy ONLY. `_send_telegram_html`/
    # `_send_telegram_info_html` take no `priority` parameter at all (a
    # bare positional-only signature, no **kwargs) — passing one would
    # raise TypeError and silently break every telegram send for any
    # agent with a tier (i.e. every real agent, since Agent.tier is
    # NOT NULL default "medium"). `_send_email_channel` happens to
    # tolerate it via **_kw, but scoping to ntfy explicitly is the
    # correct fix regardless, matching priority's actual meaning (ntfy
    # notification sound/urgency — meaningless for telegram/email).
    # Explicit per-channel priority still wins; `agent` is the real
    # Agent row configured to render this log-tag type, so `.tier` is
    # always present for this path. Neither set -> omit the kwarg
    # entirely so send_ntfy_alert's own clock-based day/night default
    # applies (unchanged from before this change).
    if not priority and ch.get("channel") == "ntfy":
        from backend.shared.helpers.alert_utils import _TIER_TO_NTFY_PRIORITY
        priority = _TIER_TO_NTFY_PRIORITY.get(getattr(agent, "tier", None))
    kwargs = {"priority": priority} if priority else {}
    if ch.get("channel") == "email":
        kwargs["email"] = out[3] if len(out) > 3 else None
        kwargs["recipients"] = ch.get("recipients")
    if ch.get("channel") == "log":
        kwargs["log_line"] = ch.get("log_line")
        kwargs["agent_slug"] = ch.get("log_agent_slug")
        kwargs["sim_mode"] = ch.get("log_sim_mode", False)
    try:
        await asyncio.to_thread(send, title, body, tg, **kwargs)
        return True
    except Exception as e:
        sys.stderr.write(f"event_agents: {agent.slug} {capability} send failed: {e}\n")
        return False


async def _dispatch_record_channels(
    agent, rec: dict, out: tuple, channels: list, log_only: bool,
) -> int:
    """Send one rendered record to every channel in its (static or
    per-record) channel list. Extracted from `_dispatch_agent` (2026-
    10-23) purely to keep that function's cyclomatic complexity within
    the project's radon gate — no behavior change beyond the binary
    ntfy action-required gate itself.
    """
    rec_priority = _rec_tracked_ntfy_priority(rec)
    action_required = (rec.get("extra") or {}).get("action_required")
    sent = 0
    for ch in channels:
        # A `_log_only` record is one the branch gate or sim-notify
        # gate would normally have suppressed entirely — 'log' is
        # exempt from both (local log write, no external notification
        # cost), every other channel in the SAME record still must not
        # fire. See dispatch_rows docstring for the full rationale.
        if log_only and (not isinstance(ch, dict) or ch.get("channel") != "log"):
            continue
        eff_ch = _channel_with_tracked_priority(ch, rec_priority)
        # 2026-10-23 chase robustness (fix #4) — binary ntfy gate: skips
        # ntfy entirely for a pure-FYI record, forces urgent for an
        # action-required one, leaves every other channel/record type
        # unchanged. See `_apply_action_required_ntfy_gate`'s own
        # docstring.
        eff_ch = _apply_action_required_ntfy_gate(eff_ch, action_required)
        if eff_ch is None:
            continue
        if await _send_channel(eff_ch, out, agent):
            sent += 1
    return sent


async def _dispatch_agent(agent, records: list[dict]) -> int:
    key = _render_key(agent)
    render = RENDERS.get(key)
    if render is None:
        if key and key not in _unknown_renders:
            _unknown_renders.add(key)
            sys.stderr.write(f"event_agents: {agent.slug} names unknown renderer '{key}'\n")
        return 0
    gated = _gate_spec(agent)
    # Cheap backstop against the shared repeat-gate consuming its dedup
    # budget for rows that will end up fully suppressed anyway: a GATED
    # agent whose own static `events` carries no 'log' channel can never
    # rescue a `_log_only` row (see dispatch_rows docstring) — its only
    # possible channels are real notification channels, every one of
    # which will be dropped below regardless. BREACH_AGENT is the one
    # source of a dynamically-added 'log' channel (via out[4]) and is
    # deliberately ungated, so it always reaches render/_gate_passes.
    static_has_log = any(isinstance(e, dict) and e.get("channel") == "log" for e in (agent.events or []))
    sent = 0
    for rec in records:
        if not evaluate(agent.conditions, Context(log_records=[rec])):
            continue
        log_only = bool(rec.get("_log_only"))
        if log_only and gated and not static_has_log:
            continue
        to_render = _gate_passes(agent, rec, gated)
        if to_render is None:
            continue
        out = render(to_render)
        channels = out[4] if len(out) > 4 else (agent.events or [])
        sent += await _dispatch_record_channels(agent, rec, out, channels, log_only)
    return sent


def _rec_tracked_ntfy_priority(rec: dict) -> "str | None":
    """2026-10-16 fix — a record carrying a real triggering agent's
    `tier` (currently only rich_alert records, via
    _v2_send_rich_alert's logged extras) resolves an ntfy priority
    override for that record, from the REAL tier rather than the
    dispatching agent's own (RICH_ALERT_AGENT is a generic multiplexer,
    always tier="medium"). Extracted from `_dispatch_agent` to keep it
    below the complexity gate."""
    rec_tier = (rec.get("extra") or {}).get("tier")
    if not rec_tier:
        return None
    from backend.shared.helpers.alert_utils import _TIER_TO_NTFY_PRIORITY
    return _TIER_TO_NTFY_PRIORITY.get(rec_tier)


def _channel_with_tracked_priority(ch: dict, rec_priority: "str | None") -> dict:
    """Apply `rec_priority` to an ntfy channel entry that sets no
    explicit priority of its own — an operator-configured per-channel
    priority always wins. Extracted from `_dispatch_agent` to keep it
    below the complexity gate."""
    if (rec_priority and isinstance(ch, dict)
            and ch.get("channel") == "ntfy" and not ch.get("priority")):
        return {**ch, "priority": rec_priority}
    return ch


def _apply_action_required_ntfy_gate(ch: dict, action_required: "bool | None") -> "dict | None":
    """2026-10-23 chase robustness (fix #4) — binary ntfy gate, applied
    at DISPATCH time rather than by editing any agent's own stored
    `events` channel list (which would not retroactively change an
    already-seeded `agents` DB row on prod/dev — `load_agents()` reads
    from the DB, not from this module's seed dicts).

    `action_required` is read from the log record's own `extra` dict
    (set `False` by `fill_notify.notify_fills`, `True` by
    `send_order_failure_alert` and the chase cancel-unconfirmed record):
      - `False` (a pure-FYI outcome, e.g. a fill) -> this function
        returns `None` for an ntfy channel, meaning the caller must skip
        sending it entirely, regardless of how the agent's own channel
        list is configured.
      - `True` (operator must take action) -> forces `priority="urgent"`
        on an ntfy channel, overriding any existing per-channel priority
        — a simple binary, not a tier spread.
      - Absent (`None`, the default for every record type not yet
        tagged — ticker/gtt/deploy/etc.) -> `ch` passed through
        unchanged, preserving every existing agent's current behavior.
    Non-ntfy channels are always passed through unchanged.
    """
    if not isinstance(ch, dict) or ch.get("channel") != "ntfy":
        return ch
    if action_required is False:
        return None
    if action_required is True:
        return {**ch, "priority": "urgent"}
    return ch


async def dispatch(records: list[dict], agents: list) -> int:
    sent = 0
    for agent in agents:
        sent += await _dispatch_agent(agent, records)
    return sent


async def load_agents() -> list:
    now = time.monotonic()
    if now - _cache["at"] < _CACHE_S:
        return _cache["agents"]
    from backend.api.database import async_session
    from backend.api.models import Agent
    async with async_session() as s:
        rows = (await s.execute(
            select(Agent).where(Agent.kind == "event", Agent.status == "active")
        )).scalars().all()
    _cache.update(at=now, agents=list(rows))
    return _cache["agents"]


def _alerts_enabled_here() -> bool:
    from backend.shared.helpers.utils import config
    return config.get("deploy_branch", "main") == "main"


def _sim_notify_allowed() -> bool:
    from backend.shared.helpers.settings import get_bool
    return get_bool("simulator.notify_during_run", False)


async def dispatch_rows(rows: list[dict]) -> None:
    """Flush one batch of tagged log rows to any matching event agent.

    Two independent gates normally suppress a row entirely: the deploy-
    branch gate (`_alerts_enabled_here`, real notifications only fire from
    the main-branch deploy) and the simulator notify-during-run gate
    (`_sim_notify_allowed`). Both exist to stop telegram/email/ntfy from
    paging a real phone/inbox from a dev instance or a fabricated
    simulator run.

    The 'log' channel is exempt from BOTH gates — it's a local log write
    with no external notification cost, and its whole documented purpose
    ("silent testing of new agents") requires it to keep working on dev
    and during simulator runs. Earlier (pre-fix) this function dropped
    the WHOLE row when a gate failed, which also silenced any 'log'
    channel sharing that row — including a MIXED record (e.g. one agent
    firing both 'telegram' and 'log' with no custom per-channel message,
    which land in the SAME shared record per events.py's message-grouping
    logic). Fix: never drop a row here for branch/sim reasons. Instead,
    stamp it `_log_only` when either gate would have suppressed it, and
    let `_dispatch_agent` filter at the CHANNEL level — 'log' always
    passes, every other channel in that same record still correctly
    stays dark.
    """
    rows = [{**r, "extra": {**(r.get("extra") or {}), **(r.get("_bulk") or {})}} for r in rows]
    rows = [r for r in rows if (r.get("extra") or {}).get("origin") in (None, "main")]
    if not rows:
        return
    branch_ok = _alerts_enabled_here()
    sim_ok = True
    if any((r.get("extra") or {}).get("sim_mode") for r in rows):
        sim_ok = _sim_notify_allowed()
    for r in rows:
        is_sim = bool((r.get("extra") or {}).get("sim_mode"))
        r["_log_only"] = (not branch_ok) or (is_sim and not sim_ok)
    try:
        agents = await load_agents()
        if agents and rows:
            await dispatch(rows, agents)
    except Exception as e:
        sys.stderr.write(f"event_agents: dispatch failed: {e}\n")


ERROR_AGENT = {
    "slug": "error-alert",
    "name": "Error alert",
    "conditions": {"log": {"tag": "error", "min_level": "ERROR"}},
    "events": [{"channel": "telegram", "enabled": True}, {"channel": "ntfy", "enabled": True}],
    "actions": [{"type": "render", "render": "error", "gate": True}],
}

CHASE_CANCEL_AGENT = {
    "slug": "chase-cancel-alert",
    "name": "Chase cancel unconfirmed",
    "conditions": {"log": {"tag": "chase", "min_level": "CRITICAL",
                           "where": {"alert_event": "cancel_unconfirmed"}}},
    "events": [
        # 2026-10 audit fix: this is one of the most severe alert types
        # (a possible duplicate live order resting unconfirmed) — ntfy
        # alone had no Telegram/email backstop. Telegram added.
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
    ],
    "actions": [{"type": "render", "render": "chase_cancel"}],
}

PARTIAL_GTT_AGENT = {
    "slug": "partial-gtt-alert",
    "name": "Partial GTT placement",
    "conditions": {"log": {"tag": "gtt", "min_level": "CRITICAL",
                           "where": {"alert_event": "partial_gtt"}}},
    "events": [{"channel": "ntfy", "enabled": True, "priority": "urgent"}],
    "actions": [{"type": "render", "render": "partial_gtt"}],
}

TEMPLATE_ATTACH_URGENT_AGENT = {
    "slug": "template-attach-urgent",
    "name": "Template attach urgent",
    "conditions": {"any": [
        {"log": {"tag": "gtt", "min_level": "WARNING", "where": {"alert_event": "wing_unprotected"}}},
        {"log": {"tag": "gtt", "min_level": "CRITICAL", "where": {"alert_event": "wing_hard_reject"}}},
    ]},
    "events": [
        # 2026-10 audit fix: wing_unprotected (an unprotected live SELL
        # position) is one of the most severe alert types — ntfy alone
        # had no Telegram/email backstop. Telegram added.
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy", "enabled": True, "priority": "urgent"},
    ],
    "actions": [{"type": "render", "render": "template_attach"}],
}

TEMPLATE_ATTACH_HIGH_AGENT = {
    "slug": "template-attach-high",
    "name": "Template attach high",
    "conditions": {"any": [
        {"log": {"tag": "gtt", "min_level": "WARNING", "where": {"alert_event": "wing_skip"}}},
        {"log": {"tag": "gtt", "min_level": "WARNING", "where": {"alert_event": "wing_offset_skip"}}},
    ]},
    "events": [{"channel": "ntfy", "enabled": True, "priority": "high"}],
    "actions": [{"type": "render", "render": "template_attach"}],
}

ORDER_FAILURE_AGENT = {
    "slug": "order-failure-alert",
    "name": "Order failure",
    "conditions": {"log": {"tag": "orders", "min_level": "WARNING",
                           "where": {"alert_event": "order_failure"}}},
    "events": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "priority": "urgent", "gate": False},
        {"channel": "email", "enabled": True, "gate": False},
    ],
    "actions": [{"type": "render", "render": "order_failure"}],
}

ORDER_CORRECTION_AGENT = {
    "slug": "order-correction-alert",
    "name": "Order poll correction",
    "conditions": {"log": {"tag": "orders", "min_level": "WARNING",
                           "where": {"alert_event": "order_correction"}}},
    "events": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "priority": "urgent", "gate": False},
    ],
    "actions": [{"type": "render", "render": "order_correction"}],
}

TEMPLATE_GUARD_AGENT = {
    "slug": "template-guard-alert",
    "name": "Template guard",
    "conditions": {"log": {"tag": "orders", "min_level": "INFO",
                           "where": {"alert_event": "template_guard"}}},
    "events": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "priority": "high", "gate": False},
        # 2026-10 audit fix: `template_attach.py:_fire_guard_alert`'s own
        # docstring already promised an email channel ("durable record
        # for end-of-day review") that was never actually wired up.
        {"channel": "email", "enabled": True, "gate": False},
    ],
    "actions": [{"type": "render", "render": "template_guard"}],
}

TEMPLATE_ATTACH_FAIL_AGENT = {
    "slug": "template-attach-fail-alert",
    "name": "Template attach failed",
    "conditions": {"log": {"tag": "orders", "min_level": "WARNING",
                           "where": {"alert_event": "template_attach_fail"}}},
    "events": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "priority": "urgent", "gate": False},
        # 2026-10 audit fix: same missing email backstop as
        # TEMPLATE_GUARD_AGENT above.
        {"channel": "email", "enabled": True, "gate": False},
    ],
    "actions": [{"type": "render", "render": "template_attach_fail"}],
}

MCP_PING_AGENT = {
    "slug": "mcp-ping-alert",
    "name": "MCP audit ping",
    "conditions": {"log": {"tag": "mcp", "min_level": "INFO", "where": {"alert_event": "mcp_ping"}}},
    "events": [{"channel": "telegram", "enabled": True, "gate": False}],
    "actions": [{"type": "render", "render": "mcp_ping"}],
}

DEPLOY_SYNC_AGENT = {
    "slug": "deploy-sync-alert",
    "name": "Deploy out of sync",
    "conditions": {"log": {"tag": "deploy", "min_level": "WARNING",
                           "where": {"alert_event": "deploy_out_of_sync"}}},
    "events": [{"channel": "ntfy", "enabled": True, "priority": "high", "gate": False}],
    "actions": [{"type": "render", "render": "deploy_sync"}],
}

RICH_ALERT_AGENT = {
    "slug": "agent-alert-rich",
    "name": "Agent alert",
    "conditions": {"log": {"tag": "agent", "min_level": "INFO", "where": {"alert_event": "rich_alert"}}},
    "events": [
        {"channel": "telegram", "enabled": True, "gate": False},
        # 2026-10-16 fix — no hardcoded priority. This agent fans out
        # EVERY other agent's alerts (it's a generic multiplexer, always
        # tier="medium" itself), so a hardcoded "urgent" here forced
        # send_ntfy_alert's 3x-repeat-send behavior onto every single
        # alert regardless of the real triggering agent's own tier —
        # live incident: a routine info-tier scheduled notification
        # (market-open-nse) pushed 3 ntfy alerts. _dispatch_agent below
        # resolves the real priority from the record's own carried-
        # through `tier` (see _v2_send_rich_alert's logged extras).
        {"channel": "ntfy", "enabled": True, "gate": False},
        {"channel": "email", "enabled": True, "gate": False},
    ],
    "actions": [{"type": "render", "render": "rich_alert"}],
}

SUMMARY_AGENT = {
    "slug": "market-summary",
    "name": "Market summary",
    "conditions": {"log": {"tag": "summary", "min_level": "INFO", "where": {"alert_event": "summary"}}},
    "events": [
        {"channel": "telegram_info", "enabled": True, "gate": False},
        {"channel": "email", "enabled": True, "gate": False},
    ],
    "actions": [{"type": "render", "render": "summary"}],
}

BREACH_AGENT = {
    "slug": "cycle-breach-alert",
    "name": "Cycle breach",
    "conditions": {"log": {"tag": "agent", "min_level": "INFO", "where": {"alert_event": "breach"}}},
    "events": [],
    "actions": [{"type": "render", "render": "breach"}],
}

GTT_ASYMMETRIC_AGENT = {
    "slug": "gtt-asymmetric-alert",
    "name": "GTT asymmetric",
    "conditions": {"log": {"tag": "gtt", "min_level": "WARNING",
                           "where": {"alert_event": "gtt_asymmetric"}}},
    "events": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "priority": "high", "gate": False},
    ],
    "actions": [{"type": "render", "render": "generic_notice"}],
}

OCO_DOUBLE_FIRE_AGENT = {
    "slug": "oco-double-fire-alert",
    "name": "OCO double-fire",
    "conditions": {"log": {"tag": "gtt", "min_level": "WARNING",
                           "where": {"alert_event": "oco_double_fire"}}},
    "events": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "priority": "urgent", "gate": False},
    ],
    "actions": [{"type": "render", "render": "generic_notice"}],
}

TICKER_RECOVERED_AGENT = {
    "slug": "ticker-recovered-alert",
    "name": "Ticker recovered",
    "conditions": {"log": {"tag": "broker", "min_level": "INFO",
                           "where": {"alert_event": "ticker_recovered"}}},
    "events": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "priority": "high", "gate": False},
    ],
    "actions": [{"type": "render", "render": "generic_notice"}],
}

TICKER_DEGRADED_AGENT = {
    "slug": "ticker-degraded-alert",
    "name": "Ticker degraded",
    "conditions": {"log": {"tag": "broker", "min_level": "WARNING",
                           "where": {"alert_event": "ticker_degraded"}}},
    "events": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "priority": "urgent", "gate": False},
    ],
    "actions": [{"type": "render", "render": "generic_notice"}],
}

VISITOR_REPORT_AGENT = {
    "slug": "visitor-report-alert",
    "name": "Visitor report",
    "conditions": {"log": {"tag": "visitor", "min_level": "INFO",
                           "where": {"alert_event": "visitor_report"}}},
    "events": [
        {"channel": "telegram_info", "enabled": True, "gate": False},
    ],
    "actions": [{"type": "render", "render": "generic_notice"}],
}

GTT_NOT_ACCEPTED_AGENT = {
    "slug": "gtt-not-accepted-alert",
    "name": "GTT not accepted",
    "conditions": {"log": {"tag": "gtt", "min_level": "ERROR",
                           "where": {"alert_event": "gtt_not_accepted"}}},
    "events": [{"channel": "ntfy", "enabled": True, "priority": "urgent", "gate": False}],
    "actions": [{"type": "render", "render": "gtt_not_accepted"}],
}

FILL_AGENT = {
    "slug": "fill-alert",
    "name": "Fill alert",
    "conditions": {"log": {"tag": "orders", "min_level": "INFO",
                           "where": {"alert_event": "filled", "mode": "live"}}},
    # 2026-10-23 chase robustness (fix #4) — a fill is FYI, never
    # action-required; ntfy channel removed from this seed (cosmetic —
    # the REAL enforcement is the binary action_required gate in
    # _dispatch_agent/_apply_action_required_ntfy_gate, which skips ntfy
    # for every fill record regardless of what's already seeded in the
    # `agents` DB table on an existing deployment).
    "events": [{"channel": "telegram", "enabled": True}],
    "actions": [{"type": "render", "render": "fill"}],
}


SEED_VERSION = 1


def validate_seed_spec(spec: dict) -> list[str]:
    """Errors that would make a seeded agent silently never fire or never render."""
    from backend.api.algo.grammar import LOG_TAG_TOKENS
    known_tags = {t["token"] for t in LOG_TAG_TOKENS}
    errors: list[str] = []
    if not spec.get("slug"):
        errors.append("missing slug")
    if spec.get("kind", "event") != "event":
        errors.append("seeded agents must be kind 'event'")
    for leaf in _log_leaves(spec.get("conditions") or {}):
        tag = (leaf.get("tag") or "")
        if tag not in known_tags:
            errors.append(f"unknown log tag '{tag}'")
        if str(leaf.get("min_level", "INFO")).upper() not in _LEVEL_NAMES:
            errors.append(f"unknown min_level '{leaf.get('min_level')}'")
    for action in spec.get("actions") or []:
        if isinstance(action, dict) and action.get("type") == "render" and action.get("render") not in RENDERS:
            errors.append(f"unknown renderer '{action.get('render')}'")
    for ch in spec.get("events") or []:
        if not isinstance(ch, dict) or ch.get("channel") not in CHANNELS:
            errors.append(f"unknown channel {ch!r}")
    return errors


_LEVEL_NAMES = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _log_leaves(cond) -> list[dict]:
    if not isinstance(cond, dict):
        return []
    if "log" in cond and isinstance(cond["log"], dict):
        return [cond["log"]]
    out: list[dict] = []
    for key in ("all", "any"):
        for child in cond.get(key) or []:
            out.extend(_log_leaves(child))
    if "not" in cond:
        out.extend(_log_leaves(cond["not"]))
    return out

SEEDED_AGENTS = (
    FILL_AGENT, ERROR_AGENT, CHASE_CANCEL_AGENT, PARTIAL_GTT_AGENT,
    TEMPLATE_ATTACH_URGENT_AGENT, TEMPLATE_ATTACH_HIGH_AGENT, ORDER_FAILURE_AGENT,
    TEMPLATE_GUARD_AGENT, TEMPLATE_ATTACH_FAIL_AGENT, MCP_PING_AGENT, ORDER_CORRECTION_AGENT,
    DEPLOY_SYNC_AGENT, RICH_ALERT_AGENT, SUMMARY_AGENT, BREACH_AGENT,
    GTT_NOT_ACCEPTED_AGENT, GTT_ASYMMETRIC_AGENT, OCO_DOUBLE_FIRE_AGENT,
    TICKER_RECOVERED_AGENT, TICKER_DEGRADED_AGENT, VISITOR_REPORT_AGENT,
)


def event_agent_row_fields(
    conditions: dict, events: list, actions: list, *,
    scope: str = "per_account", cooldown_minutes: int = 0,
    trade_mode: str = "live", lifespan_type: str = "persistent",
) -> dict:
    """Shared Agent-row field shape for kind='event' agents — conditions/
    events/actions/kind plus the defaults a log-driven agent needs for
    the columns the engine still requires (scope/cooldown_minutes/
    trade_mode/lifespan_type are meaningless for dispatch — event agents
    bypass agent_engine.run_cycle() entirely — but the Agent model has no
    NULL-able escape hatch for them).

    Reused by `seed_event_agents()` (code-seeded built-in agents, which
    default to active+live+per_account) and the agents API's create/update
    path for operator-authored event agents (which pass their own
    scope/cooldown_minutes/trade_mode through instead of these defaults).
    Callers still own `slug` / `name` / `status` / `seed_version`, which
    differ by caller and don't belong in a shared row-shape helper.
    """
    return {
        "conditions": conditions or {}, "events": events or [], "actions": actions or [],
        "kind": "event", "scope": scope, "cooldown_minutes": cooldown_minutes,
        "trade_mode": trade_mode, "lifespan_type": lifespan_type,
    }


def seed_action(row, current_version: int = SEED_VERSION) -> str:
    """Decide what seeding does to one agent row: 'insert', 'update', or 'keep'."""
    if row is None:
        return "insert"
    if int(row.seed_version or 0) < current_version:
        return "update"
    return "keep"


async def seed_event_agents() -> None:
    """Insert missing seeded agents. Rewrite an existing row only when the code's seed version is newer."""
    from backend.api.database import async_session
    from backend.api.models import Agent
    async with async_session() as s:
        for spec in SEEDED_AGENTS:
            problems = validate_seed_spec(spec)
            if problems:
                sys.stderr.write(f"event_agents: seed '{spec.get('slug')}' skipped: {'; '.join(problems)}\n")
                continue
            row = (await s.execute(select(Agent).where(Agent.slug == spec["slug"]))).scalar_one_or_none()
            action = seed_action(row)
            if action == "insert":
                s.add(Agent(slug=spec["slug"], name=spec["name"], status="active",
                            seed_version=SEED_VERSION,
                            **event_agent_row_fields(spec["conditions"], spec["events"],
                                                      spec["actions"])))
            elif action == "update":
                row.conditions = spec["conditions"]
                row.events = spec["events"]
                row.actions = spec["actions"]
                row.kind = "event"
                row.seed_version = SEED_VERSION
        await s.commit()
