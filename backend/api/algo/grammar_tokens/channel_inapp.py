"""Notify channel resolver — `inapp`: in-app rich popup + toast for the
AgentNotifications bell. Same delivery surface as the order-ticket
modal.

This is the kind='cycle' agent-engine's own direct-dispatch channel
(`events.py:_dispatch_channel`), a separate mechanism from
`event_agents.py`'s `CHANNELS` table (which has no `inapp` entry —
kind='event' agents don't broadcast over this path). Distinct from
`websocket` (the raw live-data feed every algo page subscribes to);
`inapp` is operator-facing — pair this channel with telegram/email/log
on loss/expiry agents so a Telegram-muted operator still sees the popup
in the browser tab.
"""

from __future__ import annotations


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
