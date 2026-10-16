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
        engine=extra.get("engine"),
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
    x = rec.get("extra") or {}
    body = (
        f"{x.get('transaction_type')} {x.get('symbol')} — cancel of order {x.get('order_id')} "
        f"on {x.get('account')} could not be confirmed after attempt "
        f"{x.get('attempt')}/{x.get('quantity', 0) - x.get('remaining_qty', 0)} filled. "
        f"The chase has been ABORTED without placing a replacement order. "
        f"Manually verify the broker's order book — the old order may still be live.\n"
        f"{timestamp_display()}"
    )
    return "Chase cancel unconfirmed — possible resting duplicate order", mask_account_in_text(body)


def _render_partial_gtt(rec: dict) -> tuple[str, str]:
    """Only some of a parent order's planned GTTs were placed."""
    from backend.shared.helpers.date_time_utils import timestamp_display
    x = rec.get("extra") or {}
    body = (
        f"parent #{x.get('parent_row_id')} {x.get('parent_symbol')}: "
        f"{x.get('placed')}/{x.get('planned')} GTTs placed. "
        f"Errors: {'; '.join(x.get('errors') or [])}\n"
        f"{timestamp_display()}"
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


def _render_template_guard(rec: dict) -> tuple[str, str, str]:
    """A template exit guard fired (held/released/blocked an exit)."""
    from backend.api.algo.template_attach import template_guard_message
    from backend.shared.helpers.alert_utils import _html_to_plain
    x = rec.get("extra") or {}
    keys = ("template_slug", "applies_to", "reason", "parent_order_id", "parent_side", "parent_qty",
            "parent_symbol", "parent_fill_price", "parent_account", "ist_label")
    tg = template_guard_message(**{k: x.get(k) for k in keys})
    return "Template guard fired", _html_to_plain(tg), tg


def _render_attach_fail(rec: dict) -> tuple[str, str, str]:
    """A template's exit GTTs/wing failed to attach to the filled parent."""
    from backend.api.algo.template_attach import template_attach_fail_message
    from backend.shared.helpers.alert_utils import _html_to_plain
    x = rec.get("extra") or {}
    tg = template_attach_fail_message(order_id=x.get("order_id"), symbol=x.get("symbol"),
                                      account=x.get("account"), err_summary=x.get("err_summary") or "",
                                      ist_label=x.get("ist_label"))
    return "Template attach failed", _html_to_plain(tg), tg


def _render_mcp_ping(rec: dict) -> tuple[str, str, str]:
    """An MCP tool call performed an audited action (place/cancel/modify/...)."""
    from backend.shared.helpers.alert_utils import _html_to_plain
    tg = (rec.get("extra") or {}).get("tg") or ""
    return "MCP", _html_to_plain(tg), tg


def _render_deploy_sync(rec: dict) -> tuple[str, str]:
    """dev/main branches or their deploy webhooks have drifted out of sync."""
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
    x = rec.get("extra") or {}
    return ("GTT not accepted at broker",
            f"{x.get('symbol')} {x.get('label')} GTT {x.get('gtt_id')}: {x.get('reason')}\n"
            f"{timestamp_display()}")


def _render_error(rec: dict) -> tuple[str, str, str]:
    """An ERROR+ log line was written somewhere in the app."""
    from backend.shared.helpers.utils import mask_account_in_text
    from backend.shared.helpers.date_time_utils import timestamp_display
    name = rec.get("logger") or ""
    msg = clean_message(mask_account_in_text(rec.get("message") or "") or "")
    repeats = rec.get("repeats") or 0
    suffix = f" (+{repeats} repeats)" if repeats else ""
    ts = timestamp_display()
    tg = (f"<b>RamboQuant error</b>\n<code>{html.escape(name)}</code>\n"
          f"{html.escape(msg)}{html.escape(suffix)}\n{html.escape(ts)}")
    return "RamboQuant error", f"{name}\n{msg}{suffix}\n{ts}", tg


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
register_renderer("rich_alert")(_render_rich_alert)
register_renderer("summary")(_render_summary)
register_renderer("breach")(_render_breach)
register_renderer("gtt_not_accepted")(_render_gtt_not_accepted)


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
        # 2026-10-16 fix — a record carrying a real triggering agent's
        # `tier` (currently only rich_alert records, via
        # _v2_send_rich_alert's logged extras) overrides ntfy priority
        # for THIS record, resolved from that real tier rather than
        # `agent`'s own (RICH_ALERT_AGENT is a generic multiplexer,
        # always tier="medium"). Only applies when the channel itself
        # sets no explicit priority — an operator-configured per-channel
        # priority (e.g. a future non-rich_alert event agent) still wins.
        rec_tier = (rec.get("extra") or {}).get("tier")
        rec_priority = None
        if rec_tier:
            from backend.shared.helpers.alert_utils import _TIER_TO_NTFY_PRIORITY
            rec_priority = _TIER_TO_NTFY_PRIORITY.get(rec_tier)
        for ch in channels:
            # A `_log_only` record is one the branch gate or sim-notify
            # gate would normally have suppressed entirely — 'log' is
            # exempt from both (local log write, no external notification
            # cost), every other channel in the SAME record still must not
            # fire. See dispatch_rows docstring for the full rationale.
            if log_only and (not isinstance(ch, dict) or ch.get("channel") != "log"):
                continue
            eff_ch = ch
            if (rec_priority and isinstance(ch, dict)
                    and ch.get("channel") == "ntfy" and not ch.get("priority")):
                eff_ch = {**ch, "priority": rec_priority}
            if await _send_channel(eff_ch, out, agent):
                sent += 1
    return sent


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
    "events": [{"channel": "ntfy", "enabled": True, "priority": "urgent"}],
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
    "events": [{"channel": "ntfy", "enabled": True, "priority": "urgent"}],
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

TEMPLATE_GUARD_AGENT = {
    "slug": "template-guard-alert",
    "name": "Template guard",
    "conditions": {"log": {"tag": "orders", "min_level": "INFO",
                           "where": {"alert_event": "template_guard"}}},
    "events": [
        {"channel": "telegram", "enabled": True, "gate": False},
        {"channel": "ntfy", "enabled": True, "priority": "high", "gate": False},
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
    "events": [{"channel": "telegram", "enabled": True}, {"channel": "ntfy", "enabled": True}],
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
    TEMPLATE_GUARD_AGENT, TEMPLATE_ATTACH_FAIL_AGENT, MCP_PING_AGENT,
    DEPLOY_SYNC_AGENT, RICH_ALERT_AGENT, SUMMARY_AGENT, BREACH_AGENT,
    GTT_NOT_ACCEPTED_AGENT,
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
