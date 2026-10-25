"""Tests for grammar_tokens/channel_websocket.py — `websocket` notify
channel (the kind='cycle' agent-engine's own direct-dispatch path,
`events.py:_dispatch_channel` — separate from `event_agents.py`'s
`CHANNELS` table)."""

from types import SimpleNamespace

from backend.api.algo.grammar_tokens.channel_websocket import _broadcast_websocket


def test_broadcasts_agent_alert_event_with_expected_shape():
    calls = []
    broadcast_fn = lambda event, payload: calls.append((event, payload))
    agent = SimpleNamespace(slug="loss-funds")
    _broadcast_websocket(broadcast_fn, agent, "telegram body text", "cash < 0", sim_mode=True)
    assert len(calls) == 1
    event, payload = calls[0]
    assert event == "agent_alert"
    assert payload == {
        "slug": "loss-funds",
        "message": "telegram body text",
        "condition": "cash < 0",
        "sim_mode": True,
    }
