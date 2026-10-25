"""Tests for grammar_tokens/channel_inapp.py — `inapp` notify channel
(the kind='cycle' agent-engine's own direct-dispatch path,
`events.py:_dispatch_channel` — separate from `event_agents.py`'s
`CHANNELS` table)."""

from types import SimpleNamespace

from backend.api.algo.grammar_tokens.channel_inapp import _broadcast_inapp


def test_broadcasts_agent_inapp_notify_event_with_expected_shape():
    calls = []
    broadcast_fn = lambda event, payload: calls.append((event, payload))
    agent = SimpleNamespace(slug="loss-funds", name="Loss — funds negative", tier="critical", topic="risk")
    eval_result = SimpleNamespace(detail={"matches": [{"metric": "cash"}]})
    _broadcast_inapp(broadcast_fn, agent, "cash < 0", eval_result, "2026-10-25 10:00 IST", False, "main")
    assert len(calls) == 1
    event, payload = calls[0]
    assert event == "agent_inapp_notify"
    assert payload["slug"] == "loss-funds"
    assert payload["tier"] == "critical"
    assert payload["detail"] == {"matches": [{"metric": "cash"}]}
    assert payload["branch"] == "main"


def test_missing_tier_and_topic_default_gracefully():
    calls = []
    broadcast_fn = lambda event, payload: calls.append((event, payload))
    agent = SimpleNamespace(slug="x", name="X")
    eval_result = SimpleNamespace(detail=None)
    _broadcast_inapp(broadcast_fn, agent, "cond", eval_result, "", False, "main")
    _, payload = calls[0]
    assert payload["tier"] == "info"
    assert payload["topic"] is None
    assert payload["detail"] == {}
