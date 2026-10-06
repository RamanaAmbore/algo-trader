"""ERROR log forwarding: ERROR only, one alert per distinct message per cooldown."""
import logging

import pytest

from backend.shared.helpers import error_alerts
from backend.shared.helpers.error_alerts import COOLDOWN_S, ErrorAlertHandler


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _record(level, msg, name="backend.api.test"):
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


@pytest.fixture
def harness():
    sent = []
    clock = _Clock()
    handler = ErrorAlertHandler(
        deliver=lambda name, msg, repeats: sent.append((name, msg, repeats)),
        clock=clock,
    )

    def drain():
        while not handler._q.empty():
            handler._deliver_safely(handler._q.get_nowait())

    return handler, sent, clock, drain


def test_error_is_forwarded(harness):
    handler, sent, _, drain = harness
    handler.emit(_record(logging.ERROR, "broker down"))
    drain()
    assert sent == [("backend.api.test", "broker down", 0)]


def test_warning_is_below_handler_level(harness):
    handler, _, _, _ = harness
    assert handler.level == logging.ERROR
    assert _record(logging.WARNING, "news feed 403").levelno < handler.level


def test_repeat_inside_cooldown_is_counted_not_sent(harness):
    handler, sent, clock, drain = harness
    handler.emit(_record(logging.ERROR, "502 from broker"))
    clock.t = COOLDOWN_S - 1
    handler.emit(_record(logging.ERROR, "502 from broker"))
    handler.emit(_record(logging.ERROR, "502 from broker"))
    drain()
    assert len(sent) == 1


def test_after_cooldown_next_alert_reports_repeats(harness):
    handler, sent, clock, drain = harness
    handler.emit(_record(logging.ERROR, "502 from broker"))
    clock.t = 10.0
    handler.emit(_record(logging.ERROR, "502 from broker"))
    clock.t = COOLDOWN_S + 1
    handler.emit(_record(logging.ERROR, "502 from broker"))
    drain()
    assert [s[2] for s in sent] == [0, 1]


def test_distinct_messages_each_alert(harness):
    handler, sent, _, drain = harness
    handler.emit(_record(logging.ERROR, "first"))
    handler.emit(_record(logging.ERROR, "second"))
    drain()
    assert {s[1] for s in sent} == {"first", "second"}


def test_own_loggers_are_skipped(harness):
    handler, sent, _, drain = harness
    handler.emit(_record(logging.ERROR, "send failed",
                         name="backend.shared.helpers.alert_utils"))
    drain()
    assert sent == []


def test_failing_delivery_never_raises(harness):
    handler, _, _, drain = harness

    def boom(name, msg, repeats):
        raise RuntimeError("ntfy down")

    handler._deliver = boom
    handler.emit(_record(logging.ERROR, "x"))
    drain()


def test_default_delivery_respects_disabled_flags(monkeypatch):
    calls = []
    import backend.shared.helpers.alert_utils as au
    import backend.shared.helpers.utils as u

    monkeypatch.setattr(u, "is_enabled", lambda cap: False)
    monkeypatch.setattr(au, "send_ntfy_alert", lambda *a, **k: calls.append("ntfy"))
    monkeypatch.setattr(au, "_send_telegram", lambda *a, **k: calls.append("tg"))
    error_alerts._deliver_now("backend.x", "boom", 0)
    assert calls == []


def test_default_delivery_sends_message_only(monkeypatch):
    calls = []
    import backend.shared.helpers.alert_utils as au
    import backend.shared.helpers.utils as u

    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(au, "send_ntfy_alert", lambda title, body: calls.append(("ntfy", title, body)))
    monkeypatch.setattr(au, "_send_telegram", lambda msg: calls.append(("tg", msg)))
    error_alerts._deliver_now("backend.x", "boom <bad>", 2)
    ntfy = next(c for c in calls if c[0] == "ntfy")
    assert ntfy[1] == "RamboQuant error"
    assert ntfy[2] == "backend.x\nboom <bad>\n(+2 repeats)"
    tg = next(c for c in calls if c[0] == "tg")
    assert "&lt;bad&gt;" in tg[1]
    assert "Traceback" not in tg[1]
