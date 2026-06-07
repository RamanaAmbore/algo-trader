"""
SimDriver recording-layer contract tests.

These exercise the in-memory event buffer + the _record hook in
isolation from the rest of the simulator (no DB writes, no scenario
loading, no event loop). The full end-to-end flush is covered by the
Playwright spec in Phase 2c — these tests pin the per-event accounting
that's easy to regress.

Driver state is reset between tests via a fresh instance — SimDriver is
a singleton in production but the class also supports direct
instantiation for testing.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def driver():
    """Fresh SimDriver, not the singleton, with recording on."""
    from backend.api.algo.sim.driver import SimDriver
    d = SimDriver()
    # Manually flip into recording mode + plant a started_at so _record's
    # timestamp math works. Bypassing start() so we don't need a real
    # scenario in scenarios.yaml.
    from datetime import datetime
    d._recording_active = True
    d._recording_started_at = datetime.now()
    d._recording_events = []
    # Wire the GTT book's recorder too — matches what start() does.
    d._gtt_book._on_record = d._record
    return d


def test_record_no_op_when_inactive():
    """_record returns silently when recording is off — never raises,
    never appends."""
    from backend.api.algo.sim.driver import SimDriver
    d = SimDriver()
    assert d._recording_active is False
    d._record("tick", {"x": 1})
    assert d._recording_events == []


def test_record_appends_with_t_offset(driver):
    driver._record("a", {"v": 1})
    driver._record("b", {"v": 2})
    assert len(driver._recording_events) == 2
    a, b = driver._recording_events
    assert a["kind"] == "a"
    assert a["payload"] == {"v": 1}
    assert b["kind"] == "b"
    # Relative timestamps are floats; both should be >= 0 and b >= a.
    assert isinstance(a["t"], float)
    assert isinstance(b["t"], float)
    assert b["t"] >= a["t"]


def test_record_payload_is_serialisable(driver):
    """The buffer is flushed to JSONB; payloads must be primitives /
    lists / dicts of the same. We don't enforce a schema here, just
    confirm dicts round-trip cleanly through json.dumps as a smoke
    test of the recipient's expectations."""
    import json
    driver._record("nested", {
        "list": [1, 2, {"a": "b"}],
        "bool": True,
        "null": None,
    })
    payload = driver._recording_events[0]["payload"]
    json.dumps(payload)  # raises if non-serialisable


def test_gtt_book_lifecycle_appends_events(driver):
    """The GTT book's on_record hook is wired to the driver's _record
    in our fixture. Placing a GTT and triggering it should land at
    least two events in the buffer (placed + triggered)."""
    gtt = driver._gtt_book.place(
        account="A", tradingsymbol="X", exchange="NSE",
        trigger_type="single", trigger_values=[100.0],
        orders=[{"transaction_type": "SELL", "quantity": 1, "price": 100.0}],
        last_price=95.0,
    )
    driver._gtt_book.check_triggers({("A", "X"): 105.0})
    kinds = [e["kind"] for e in driver._recording_events]
    assert "gtt_placed" in kinds
    assert "gtt_triggered" in kinds
    # The triggered event payload includes gtt_id + leg_index.
    triggered = [e for e in driver._recording_events if e["kind"] == "gtt_triggered"][0]
    assert triggered["payload"]["gtt_id"] == gtt.gtt_id
    assert triggered["payload"]["leg_index"] == 0


def test_recording_events_preserve_order(driver):
    """Replay determinism requires strict ordering — same events in,
    same order out. We don't sort by t anywhere."""
    for i in range(20):
        driver._record(f"evt_{i}", {"i": i})
    kinds = [e["kind"] for e in driver._recording_events]
    assert kinds == [f"evt_{i}" for i in range(20)]


def test_buffer_is_per_instance():
    """Two SimDriver instances must have independent buffers — we don't
    rely on class-level state for recording."""
    from backend.api.algo.sim.driver import SimDriver
    from datetime import datetime
    a = SimDriver()
    b = SimDriver()
    a._recording_active = True
    a._recording_started_at = datetime.now()
    a._recording_events = []
    b._recording_active = True
    b._recording_started_at = datetime.now()
    b._recording_events = []
    a._record("from_a", {})
    b._record("from_b", {})
    assert [e["kind"] for e in a._recording_events] == ["from_a"]
    assert [e["kind"] for e in b._recording_events] == ["from_b"]
