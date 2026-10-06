"""ERROR alerts: persistent errors alert, transient ones stay warnings; messages are readable."""
import logging

import pytest

from backend.shared.helpers import error_alerts
from backend.shared.helpers.error_alerts import (
    COOLDOWN_S, REPEAT_THRESHOLD, ErrorAlertHandler, clean_message,
)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _record(level, msg, name="backend.api.test", **extra):
    rec = logging.LogRecord(name, level, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


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


def test_single_transient_error_is_not_alerted(harness):
    handler, sent, _, drain = harness
    handler.emit(_record(logging.ERROR, "502 from broker"))
    drain()
    assert sent == []


def test_error_alerts_once_it_repeats_beyond_the_threshold(harness):
    handler, sent, clock, drain = harness
    for i in range(REPEAT_THRESHOLD + 1):
        clock.t = i
        handler.emit(_record(logging.ERROR, "502 from broker"))
    drain()
    assert len(sent) == 1


def test_alert_now_sends_on_first_occurrence(harness):
    handler, sent, _, drain = harness
    handler.emit(_record(logging.ERROR, "auth rejected", alert_now=True))
    drain()
    assert len(sent) == 1


def test_repeat_inside_cooldown_is_counted_not_sent(harness):
    handler, sent, clock, drain = harness
    handler.emit(_record(logging.ERROR, "x", alert_now=True))
    clock.t = COOLDOWN_S - 1
    handler.emit(_record(logging.ERROR, "x", alert_now=True))
    handler.emit(_record(logging.ERROR, "x", alert_now=True))
    drain()
    assert len(sent) == 1


def test_after_cooldown_next_alert_reports_repeats(harness):
    handler, sent, clock, drain = harness
    handler.emit(_record(logging.ERROR, "x", alert_now=True))
    clock.t = 10.0
    handler.emit(_record(logging.ERROR, "x", alert_now=True))
    clock.t = COOLDOWN_S + 1
    handler.emit(_record(logging.ERROR, "x", alert_now=True))
    drain()
    assert [s[2] for s in sent] == [0, 1]


def test_warning_is_below_handler_level(harness):
    handler, _, _, _ = harness
    assert handler.level == logging.ERROR
    assert _record(logging.WARNING, "news feed 403").levelno < handler.level


def test_own_loggers_are_skipped(harness):
    handler, sent, _, drain = harness
    handler.emit(_record(logging.ERROR, "send failed", alert_now=True,
                         name="backend.shared.helpers.alert_utils"))
    drain()
    assert sent == []


def test_failing_delivery_never_raises(harness):
    handler, _, _, drain = harness

    def boom(name, msg, repeats):
        raise RuntimeError("ntfy down")

    handler._deliver = boom
    handler.emit(_record(logging.ERROR, "x", alert_now=True))
    drain()


def test_clean_message_collapses_html_gateway_page():
    raw = ("(b'\\r\\n\\r\\n\\r\\n<html>\\r\\n<head><title>502 Bad Gateway</title></head>\\r\\n"
           "<body>\\r\\n<center><h1>502 Bad Gateway</h1></center>\\r\\n</body>\\r\\n</html>\\r\\n')")
    out = clean_message(f"ZG0790.orders failed: Unknown Content-Type (text/html) with response: {raw}")
    assert out == "ZG0790.orders failed: Unknown Content-Type (text/html) with response: 502 Bad Gateway"
    assert "\\r" not in out and "\\n" not in out and "<" not in out


def test_clean_message_keeps_plain_text_and_caps_length():
    assert clean_message("plain   text\nline") == "plain text line"
    assert len(clean_message("x" * 1000)) <= 300


def test_default_delivery_respects_disabled_flags(monkeypatch):
    calls = []
    import backend.shared.helpers.alert_utils as au
    import backend.shared.helpers.utils as u

    monkeypatch.setattr(u, "is_enabled", lambda cap: False)
    monkeypatch.setattr(au, "send_ntfy_alert", lambda *a, **k: calls.append("ntfy"))
    monkeypatch.setattr(au, "_send_telegram", lambda *a, **k: calls.append("tg"))
    error_alerts._deliver_now("backend.x", "boom", 0)
    assert calls == []


def test_default_delivery_sends_readable_message(monkeypatch):
    calls = []
    import backend.shared.helpers.alert_utils as au
    import backend.shared.helpers.utils as u

    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(au, "send_ntfy_alert", lambda title, body: calls.append(("ntfy", title, body)))
    monkeypatch.setattr(au, "_send_telegram", lambda msg: calls.append(("tg", msg)))
    error_alerts._deliver_now("backend.x", "boom <bad>", 2)
    ntfy = next(c for c in calls if c[0] == "ntfy")
    assert ntfy[1] == "RamboQuant error"
    assert ntfy[2] == "backend.x\nboom <bad> (+2 repeats)"
    tg = next(c for c in calls if c[0] == "tg")
    assert "&lt;bad&gt;" in tg[1]
    assert "Traceback" not in tg[1]
