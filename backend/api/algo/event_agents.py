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

_CACHE_S = 60.0
_cache: dict = {"at": float("-inf"), "agents": []}


def _render_fill(rec: dict) -> tuple[str, str]:
    extra = rec.get("extra") or {}
    row = SimpleNamespace(
        id=extra.get("order_id"), account=extra.get("account"), symbol=extra.get("symbol"),
        exchange=extra.get("exchange"), transaction_type=extra.get("transaction_type"),
        quantity=extra.get("quantity"), fill_price=extra.get("fill_price"), product=extra.get("product"),
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
    from backend.shared.helpers.utils import mask_account_in_text
    x = rec.get("extra") or {}
    body = (
        f"{x.get('transaction_type')} {x.get('symbol')} — cancel of order {x.get('order_id')} "
        f"on {x.get('account')} could not be confirmed after attempt "
        f"{x.get('attempt')}/{x.get('quantity', 0) - x.get('remaining_qty', 0)} filled. "
        f"The chase has been ABORTED without placing a replacement order. "
        f"Manually verify the broker's order book — the old order may still be live."
    )
    return "Chase cancel unconfirmed — possible resting duplicate order", mask_account_in_text(body)


def _render_partial_gtt(rec: dict) -> tuple[str, str]:
    x = rec.get("extra") or {}
    body = (
        f"parent #{x.get('parent_row_id')} {x.get('parent_symbol')}: "
        f"{x.get('placed')}/{x.get('planned')} GTTs placed. "
        f"Errors: {'; '.join(x.get('errors') or [])}"
    )
    return "Partial GTT placement", body


def _render_template_attach(rec: dict) -> tuple[str, str]:
    x = rec.get("extra") or {}
    ev = x.get("alert_event")
    if ev == "wing_unprotected":
        return ("Unprotected SELL position",
                f"GTTs placed (ids: {x['gtt_ids_text']}) but wing failed: {x['reason']} | "
                f"order #{x['parent_order_id']} {x['symbol']} {x['exchange']}")
    if ev == "wing_hard_reject":
        return ("Wing scan hard-rejected",
                f"{x['reason']} | {x['symbol']} {x['exchange']} target ₹{x['target_premium']:.2f}")
    if ev == "wing_skip":
        return ("Wing attach skipped",
                f"{x['reason']} | order #{x['parent_order_id']} {x['symbol']} {x['exchange']}")
    if ev == "wing_offset_skip":
        return ("Wing offset attach skipped",
                f"{x['reason']} | order #{x['parent_order_id']} {x['symbol']} {x['exchange']}")
    return "Template attach", str(x)


def _render_order_failure(rec: dict) -> tuple:
    from backend.shared.helpers.alert_utils import order_failure_messages
    from backend.shared.helpers.alert_utils import _html_to_plain
    x = rec.get("extra") or {}
    tg_body, subject, email_body = order_failure_messages(
        masked=x["masked"], symbol=x["symbol"], exchange=x["exchange"], side=x["side"],
        qty=x["qty"], mode=x["mode"], source=x["source"], error=x["error"],
        suppressed_count=x["suppressed_count"], ist_disp=x["ist_disp"],
    )
    return f"Order Rejected: {x['symbol']} {x['side']}", _html_to_plain(tg_body), tg_body, (subject, email_body)


def _render_template_guard(rec: dict) -> tuple[str, str, str]:
    from backend.api.algo.template_attach import template_guard_message
    from backend.shared.helpers.alert_utils import _html_to_plain
    x = rec.get("extra") or {}
    keys = ("template_slug", "applies_to", "reason", "parent_order_id", "parent_side", "parent_qty",
            "parent_symbol", "parent_fill_price", "parent_account", "ist_label")
    tg = template_guard_message(**{k: x.get(k) for k in keys})
    return "Template guard fired", _html_to_plain(tg), tg


def _render_attach_fail(rec: dict) -> tuple[str, str, str]:
    from backend.api.algo.template_attach import template_attach_fail_message
    from backend.shared.helpers.alert_utils import _html_to_plain
    x = rec.get("extra") or {}
    tg = template_attach_fail_message(order_id=x.get("order_id"), symbol=x.get("symbol"),
                                      account=x.get("account"), err_summary=x.get("err_summary") or "",
                                      ist_label=x.get("ist_label"))
    return "Template attach failed", _html_to_plain(tg), tg


def _render_mcp_ping(rec: dict) -> tuple[str, str, str]:
    from backend.shared.helpers.alert_utils import _html_to_plain
    tg = (rec.get("extra") or {}).get("tg") or ""
    return "MCP", _html_to_plain(tg), tg


def _render_deploy_sync(rec: dict) -> tuple[str, str]:
    x = rec.get("extra") or {}
    return x.get("title") or "Deploy out of sync", x.get("body") or ""


def _render_rich_alert(rec: dict) -> tuple:
    from backend.shared.helpers.alert_utils import dispatch_payload, _html_to_plain
    x = rec.get("extra") or {}
    p = dispatch_payload("alert", x["ist_display"], x["tg_table"], x["email_table_html"],
                         x["subject_detail"], sim_mode=bool(x.get("sim_mode")),
                         mode_tag=x.get("mode_tag") or "")
    return p["title"], _html_to_plain(p["telegram_msg"]), p["telegram_msg"], (p["email_subject"], p["email_html"])


def _render_summary(rec: dict) -> tuple:
    from backend.shared.helpers.alert_utils import dispatch_payload, _html_to_plain
    x = rec.get("extra") or {}
    p = dispatch_payload(x["msg_type"], x["ist_display"], x["tg_table"], x["email_table_html"],
                         x["subject_detail"], sim_mode=False, mode_tag="")
    return p["title"], _html_to_plain(p["telegram_msg"]), p["telegram_msg"], (p["email_subject"], p["email_html"])


def _render_breach(rec: dict) -> tuple:
    x = rec.get("extra") or {}
    return (x.get("agent_name") or "", x.get("ntfy_body") or "", x.get("telegram_body") or "",
            (x.get("email_subject") or "", x.get("email_body") or ""), list(x.get("channels") or []))


def _render_error(rec: dict) -> tuple[str, str, str]:
    from backend.shared.helpers.utils import mask_account_in_text
    name = rec.get("logger") or ""
    msg = clean_message(mask_account_in_text(rec.get("message") or "") or "")
    repeats = rec.get("repeats") or 0
    suffix = f" (+{repeats} repeats)" if repeats else ""
    tg = (f"<b>RamboQuant error</b>\n<code>{html.escape(name)}</code>\n"
          f"{html.escape(msg)}{html.escape(suffix)}")
    return "RamboQuant error", f"{name}\n{msg}{suffix}", tg


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


CHANNELS = {
    "ntfy": ("ntfy", _send_ntfy),
    "telegram": ("telegram", _send_telegram_html),
    "telegram_info": (None, _send_telegram_info_html),
    "email": ("mail", _send_email_channel),
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
    kwargs = {"priority": ch["priority"]} if ch.get("priority") else {}
    if ch.get("channel") == "email":
        kwargs["email"] = out[3] if len(out) > 3 else None
        kwargs["recipients"] = ch.get("recipients")
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
    sent = 0
    for rec in records:
        if not evaluate(agent.conditions, Context(log_records=[rec])):
            continue
        to_render = _gate_passes(agent, rec, gated)
        if to_render is None:
            continue
        out = render(to_render)
        channels = out[4] if len(out) > 4 else (agent.events or [])
        for ch in channels:
            if await _send_channel(ch, out, agent):
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
    if not _alerts_enabled_here():
        return
    rows = [{**r, "extra": {**(r.get("extra") or {}), **(r.get("_bulk") or {})}} for r in rows]
    rows = [r for r in rows if (r.get("extra") or {}).get("origin") in (None, "main")]
    if not _sim_notify_allowed():
        rows = [r for r in rows if not (r.get("extra") or {}).get("sim_mode")]
    if not rows:
        return
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
        {"channel": "ntfy", "enabled": True, "priority": "urgent", "gate": False},
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
)


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
                s.add(Agent(slug=spec["slug"], name=spec["name"], conditions=spec["conditions"],
                            events=spec["events"], actions=spec["actions"], kind="event",
                            status="active", scope="per_account", cooldown_minutes=0,
                            trade_mode="live", lifespan_type="persistent",
                            seed_version=SEED_VERSION))
            elif action == "update":
                row.conditions = spec["conditions"]
                row.events = spec["events"]
                row.actions = spec["actions"]
                row.kind = "event"
                row.seed_version = SEED_VERSION
        await s.commit()
