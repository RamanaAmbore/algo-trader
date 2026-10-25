"""Notify channel resolver — `websocket`: live push to connected /algo
dashboard clients.

This is the kind='cycle' agent-engine's own direct-dispatch channel
(`events.py:_dispatch_channel`), a separate mechanism from
`event_agents.py`'s `CHANNELS` table (which has no `websocket` entry —
kind='event' agents don't broadcast over this path).
"""

from __future__ import annotations


def _broadcast_websocket(broadcast_fn, agent, telegram_body, condition_text, sim_mode) -> None:
    broadcast_fn("agent_alert", {
        "slug": agent.slug,
        "message": telegram_body,
        "condition": condition_text,
        "sim_mode": sim_mode,
    })
