"""Tests for grammar_tokens/channel_log.py — `log` notify channel."""

from backend.api.algo.grammar_tokens.channel_log import _send_log_channel, _EVENTS_LOGGER


def test_uses_prebuilt_log_line_and_extras(monkeypatch):
    calls = []
    monkeypatch.setattr(_EVENTS_LOGGER, "warning", lambda msg, **k: calls.append((msg, k)))
    _send_log_channel(
        "Title", "Body", log_line="PREBUILT LINE",
        agent_slug="loss-funds", sim_mode=True,
    )
    assert len(calls) == 1
    msg, kwargs = calls[0]
    assert msg == "PREBUILT LINE"
    assert kwargs["extra"] == {"tags": ["agent"], "agent_slug": "loss-funds", "sim_mode": True}


def test_falls_back_to_title_dash_body_without_prebuilt_line(monkeypatch):
    calls = []
    monkeypatch.setattr(_EVENTS_LOGGER, "warning", lambda msg, **k: calls.append((msg, k)))
    _send_log_channel("Some Title", "Some Body")
    assert calls[0][0] == "Some Title — Some Body"


def test_logger_name_is_backend_api_algo_events_not_this_modules_name():
    """Pinned so stored `LogEvent` rows look identical to before this
    channel moved off `events.py`'s old direct-dispatch `_log_channel`,
    and so the real `_EVENTS_LOGGER` singleton `event_agents.py` also
    uses for its own, unrelated purposes is the SAME object."""
    assert _EVENTS_LOGGER.name == "backend.api.algo.events"
