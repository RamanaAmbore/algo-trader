"""Repeat gate: persistent errors alert, transient ones do not; cleaned messages stay readable."""
import pytest

from backend.shared.helpers import error_alerts
from backend.shared.helpers.error_alerts import (
    COOLDOWN_S, REPEAT_THRESHOLD, RepeatGate, clean_message,
)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def gate():
    clock = _Clock()
    return RepeatGate(clock=clock), clock


def test_single_transient_error_does_not_alert(gate):
    g, _ = gate
    assert g.decide("m") is None


def test_error_alerts_once_it_repeats_beyond_the_threshold(gate):
    g, clock = gate
    results = []
    for i in range(REPEAT_THRESHOLD + 1):
        clock.t = i
        results.append(g.decide("m"))
    assert results[:-1] == [None] * REPEAT_THRESHOLD
    assert results[-1] == 0


def test_alert_now_sends_on_first_occurrence(gate):
    g, _ = gate
    assert g.decide("auth", alert_now=True) == 0


def test_repeat_inside_cooldown_is_counted_not_sent(gate):
    g, clock = gate
    assert g.decide("x", alert_now=True) == 0
    clock.t = COOLDOWN_S - 1
    assert g.decide("x", alert_now=True) is None
    assert g.decide("x", alert_now=True) is None


def test_after_cooldown_next_alert_reports_repeats(gate):
    g, clock = gate
    assert g.decide("x", alert_now=True) == 0
    clock.t = 10.0
    assert g.decide("x", alert_now=True) is None
    clock.t = COOLDOWN_S + 1
    assert g.decide("x", alert_now=True) == 1


def test_keys_are_independent(gate):
    g, _ = gate
    assert g.decide("a", alert_now=True) == 0
    assert g.decide("b", alert_now=True) == 0


def test_clean_message_collapses_html_gateway_page():
    raw = ("(b'\\r\\n\\r\\n\\r\\n<html>\\r\\n<head><title>502 Bad Gateway</title></head>\\r\\n"
           "<body>\\r\\n<center><h1>502 Bad Gateway</h1></center>\\r\\n</body>\\r\\n</html>\\r\\n')")
    out = clean_message(f"ZG0790.orders failed: Unknown Content-Type (text/html) with response: {raw}")
    assert out == "ZG0790.orders failed: Unknown Content-Type (text/html) with response: 502 Bad Gateway"
    assert "\\r" not in out and "\\n" not in out and "<" not in out


def test_clean_message_keeps_plain_text_and_caps_length():
    assert clean_message("plain   text\nline") == "plain text line"
    assert len(clean_message("x" * 1000)) <= 300


def test_error_agent_is_seeded_for_error_records_only():
    from backend.api.algo import event_agents
    spec = event_agents.ERROR_AGENT
    assert spec["conditions"] == {"log": {"tag": "error", "min_level": "ERROR"}}
    assert spec["actions"] == [{"type": "render", "render": "error", "gate": True}]


class _FakeRedis:
    def __init__(self):
        self.kv, self.ttl = {}, {}

    def incr(self, k):
        self.kv[k] = int(self.kv.get(k, 0)) + 1
        return self.kv[k]

    def expire(self, k, s):
        self.ttl[k] = s

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.kv:
            return None
        self.kv[k] = v
        return True

    def getdel(self, k):
        return self.kv.pop(k, None)


def test_shared_gate_matches_local_gate_across_processes():
    from backend.shared.helpers.error_alerts import SharedRepeatGate
    r = _FakeRedis()
    a, b = SharedRepeatGate(r), SharedRepeatGate(r)
    assert a.decide("m") is None
    assert b.decide("m") is None
    assert b.decide("m") is None
    assert a.decide("m") == 0
    assert b.decide("m") is None
    r.kv = {k: v for k, v in r.kv.items() if "cool" not in k}
    assert a.decide("m", alert_now=True) == 1


def test_shared_gate_alert_now_sends_first_occurrence():
    from backend.shared.helpers.error_alerts import SharedRepeatGate
    assert SharedRepeatGate(_FakeRedis()).decide("x", alert_now=True) == 0
