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
_gate = RepeatGate()


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


def _send_ntfy(title: str, body: str, tg: str | None = None) -> None:
    from backend.shared.helpers import alert_utils
    alert_utils.send_ntfy_alert(title, body)


def _send_telegram_html(title: str, body: str, tg: str | None = None) -> None:
    from backend.shared.helpers import alert_utils
    alert_utils._send_telegram(tg or f"<b>{html.escape(title)}</b>\n{html.escape(body)}")


CHANNELS = {"ntfy": ("ntfy", _send_ntfy), "telegram": ("telegram", _send_telegram_html)}


def _render_key(agent) -> str | None:
    for action in agent.actions or []:
        if isinstance(action, dict) and action.get("type") == "render":
            return action.get("render")
    return None


def _channel_enabled(capability: str) -> bool:
    from backend.shared.helpers.utils import is_enabled
    return is_enabled(capability)


def _gate_spec(agent) -> bool:
    return any(isinstance(a, dict) and a.get("type") == "render" and a.get("gate")
               for a in agent.actions or [])


async def dispatch(records: list[dict], agents: list) -> int:
    sent = 0
    for agent in agents:
        key = _render_key(agent)
        render = RENDERS.get(key)
        if render is None:
            if key and key not in _unknown_renders:
                _unknown_renders.add(key)
                sys.stderr.write(f"event_agents: {agent.slug} names unknown renderer '{key}'\n")
            continue
        gated = _gate_spec(agent)
        for rec in records:
            if not evaluate(agent.conditions, Context(log_records=[rec])):
                continue
            if gated:
                from backend.shared.helpers.utils import mask_account_in_text
                gate_key = f"{rec.get('logger')}|{clean_message(mask_account_in_text(rec.get('message') or '') or '')}"
                repeats = _gate.decide(gate_key, alert_now=bool((rec.get("extra") or {}).get("alert_now")))
                if repeats is None:
                    continue
                rec = {**rec, "repeats": repeats}
            out = render(rec)
            title, body = out[0], out[1]
            tg = out[2] if len(out) > 2 else None
            for ch in agent.events or []:
                spec = CHANNELS.get(ch.get("channel")) if isinstance(ch, dict) else None
                if spec is None or not ch.get("enabled"):
                    continue
                capability, send = spec
                if not _channel_enabled(capability):
                    continue
                try:
                    await asyncio.to_thread(send, title, body, tg)
                    sent += 1
                except Exception as e:
                    sys.stderr.write(f"event_agents: {agent.slug} {capability} send failed: {e}\n")
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


async def dispatch_rows(rows: list[dict]) -> None:
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

FILL_AGENT = {
    "slug": "fill-alert",
    "name": "Fill alert",
    "conditions": {"log": {"tag": "orders", "min_level": "INFO",
                           "where": {"event": "filled", "mode": "live"}}},
    "events": [{"channel": "telegram", "enabled": True}, {"channel": "ntfy", "enabled": True}],
    "actions": [{"type": "render", "render": "fill"}],
}


async def seed_event_agents() -> None:
    from backend.api.database import async_session
    from backend.api.models import Agent
    async with async_session() as s:
        for spec in (FILL_AGENT, ERROR_AGENT):
            row = (await s.execute(select(Agent).where(Agent.slug == spec["slug"]))).scalar_one_or_none()
            if row is None:
                s.add(Agent(slug=spec["slug"], name=spec["name"], conditions=spec["conditions"],
                            events=spec["events"], actions=spec["actions"], kind="event",
                            status="active", scope="per_account", cooldown_minutes=0,
                            trade_mode="live", lifespan_type="persistent"))
            else:
                row.conditions = spec["conditions"]
                row.events = spec["events"]
                row.actions = spec["actions"]
                row.kind = "event"
        await s.commit()
