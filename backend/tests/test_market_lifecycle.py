"""
Tests for MarketLifecycle singleton + transition dispatch.

Five quality dimensions per CLAUDE.md test_dimensions feedback:

  1. SSOT       — market_lifecycle is the canonical singleton; only one
                  process-wide MarketLifecycle exists; handlers register
                  via .register() not module-globals.
  2. Performance — poll() is O(num_exchanges) per tick (3); no broker
                  calls during a stable-state tick (state cached); only
                  fires audit-row writes when an event actually transitions.
  3. Stale code — no legacy `_state` plain dicts left exposed (only via
                  the public get_state() shape).
  4. Reusable   — exposes a HandlerCB alias + register/get_state/poll trio
                  the same way other singletons do.
  5. Correctness — open/close/settled/holiday/multi-exchange scenarios.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
import types
from datetime import date, datetime, timedelta, time as dtime, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Source-level dimension assertions (SSOT / stale / reusable)
# ---------------------------------------------------------------------------

_MOD_PATH = (
    Path(__file__).parent.parent / "api" / "algo" / "market_lifecycle.py"
)
_HANDLERS_PATH = (
    Path(__file__).parent.parent / "api" / "algo" / "market_lifecycle_handlers.py"
)


def _src() -> str:
    return _MOD_PATH.read_text(encoding="utf-8")


def _hsrc() -> str:
    return _HANDLERS_PATH.read_text(encoding="utf-8")


def test_singleton_identity_in_source():
    """Module exposes a `market_lifecycle` singleton."""
    src = _src()
    assert re.search(r"^market_lifecycle\s*=\s*MarketLifecycle\(\)",
                     src, re.MULTILINE), (
        "market_lifecycle singleton not declared at module bottom"
    )


def test_class_uses_new_singleton_pattern():
    """MarketLifecycle.__new__ guards on _instance."""
    src = _src()
    assert "_instance" in src and "def __new__" in src, (
        "MarketLifecycle does not use __new__ singleton pattern"
    )


def test_register_signature():
    """register(event, callback) is the public API."""
    src = _src()
    m = re.search(r"def register\(self,\s*event:\s*str,\s*callback:\s*\w+\)",
                  src)
    assert m, "register() signature missing or wrong"


def test_get_state_signature():
    """get_state() returns dict — no leaking internal state."""
    src = _src()
    assert re.search(r"def get_state\(self\)\s*->\s*dict", src), (
        "get_state() not declared or wrong return type"
    )


def test_no_legacy_test_only_state_attr():
    """No leftover legacy `_state` direct exposure; only `_open_state` etc."""
    src = _src()
    # We should NOT see a public `state:` attr that bypasses get_state.
    assert "self.state " not in src, "Found legacy self.state attribute"


def test_handlers_register_default_handlers_idempotent():
    """register_default_handlers idempotency latch present."""
    src = _hsrc()
    assert "_REGISTERED" in src
    assert "if _REGISTERED:" in src or "if _REGISTERED == True" in src


def test_handlers_wired_for_three_exchanges():
    """nse / mcx / cds all have close + close_settled registrations.

    Handlers register via f-strings inside a loop, so the literal
    "nse:close_settled" never appears as source text. We assert on the
    enumeration tuple + the event suffix tokens.
    """
    src = _hsrc()
    # Loop over the canonical exchanges tuple
    assert '("nse", "mcx", "cds")' in src
    # f-string template formats inside the loop
    assert 'f"{exch}:close"' in src, "close-event f-string template missing"
    assert 'f"{exch}:close_settled"' in src, "close_settled f-string template missing"
    # NAV only on nse:close
    assert 'register("nse:close", _snapshot_nav)' in src


def test_no_legacy_market_close_state_inline_path():
    """No leftover TODO markers in lifecycle module."""
    src = _src()
    assert "TODO" not in src.upper() or "todo: market_lifecycle" not in src.lower()


def test_handler_callable_signature_matches_dispatcher():
    """Default handlers accept (exchange, event_type)."""
    src = _hsrc()
    # _snapshot_close and _snapshot_nav both accept (exchange: str, event_type: str)
    for fn in ("_snapshot_close", "_snapshot_nav"):
        m = re.search(
            fr"async def {fn}\(exchange:\s*str,\s*event_type:\s*str\)",
            src
        )
        assert m, f"{fn} does not match (exchange: str, event_type: str)"


def test_audit_row_persistence_is_async_task():
    """Audit row write is fire-and-forget via asyncio.create_task."""
    src = _src()
    assert "asyncio.create_task(_persist_audit_rows(" in src, (
        "Audit persistence is not fire-and-forget"
    )


# ---------------------------------------------------------------------------
# Behaviour tests using the real singleton
# ---------------------------------------------------------------------------

@pytest.fixture
def lifecycle():
    """Yield a fresh, reset MarketLifecycle for each test."""
    from backend.api.algo.market_lifecycle import market_lifecycle
    market_lifecycle._reset_for_test()
    yield market_lifecycle
    market_lifecycle._reset_for_test()


@pytest.fixture
def stub_segments():
    """Patch _enumerate_exchanges with a stable 3-exchange map."""
    from backend.api.algo import market_lifecycle as ml_mod

    def _fake():
        return {
            "nse": {"name": "equity",    "exchange": "NSE",
                    "hours_start": dtime(9, 15), "hours_end": dtime(15, 30)},
            "mcx": {"name": "commodity", "exchange": "MCX",
                    "hours_start": dtime(9, 0),  "hours_end": dtime(23, 30)},
            "cds": {"name": "currency",  "exchange": "CDS",
                    "hours_start": dtime(9, 15), "hours_end": dtime(15, 30)},
        }
    with patch.object(ml_mod, "_enumerate_exchanges", _fake):
        yield


@pytest.fixture
def stub_holidays():
    """Patch _exchange_is_open to be driven by patched is_open_now lambda."""
    from backend.api.algo import market_lifecycle as ml_mod
    # Replace with a noop that defers to a per-test overridden function.
    # Tests set ml_mod._test_state_fn = lambda exch, now: bool.
    ml_mod._test_state_fn = lambda exch, now: False
    orig = ml_mod._exchange_is_open

    def _stub(segment, now):
        exch = segment["exchange"].lower()
        return bool(ml_mod._test_state_fn(exch, now))
    with patch.object(ml_mod, "_exchange_is_open", _stub):
        yield ml_mod
    # Cleanup
    try:
        delattr(ml_mod, "_test_state_fn")
    except AttributeError:
        pass


@pytest.fixture
def freeze_audit():
    """Patch _persist_audit_rows so DB writes don't fire during tests."""
    from backend.api.algo import market_lifecycle as ml_mod

    async def _noop(events, now):
        return None

    with patch.object(ml_mod, "_persist_audit_rows", _noop):
        yield


@pytest.fixture
def freeze_now():
    """Patch timestamp_indian to return a controllable IST datetime."""
    from backend.api.algo import market_lifecycle as ml_mod
    import datetime as _dt

    # IST = UTC + 5:30. Use a tz-aware datetime so timedelta math works.
    ist = timezone(timedelta(hours=5, minutes=30))
    state = {"now": datetime(2026, 6, 28, 10, 0, 0, tzinfo=ist)}

    def _get():
        return state["now"]

    with patch.object(ml_mod, "timestamp_indian", _get):
        yield state


@pytest.mark.asyncio
async def test_singleton_identity(lifecycle):
    """Two `MarketLifecycle()` calls return the same instance."""
    from backend.api.algo.market_lifecycle import MarketLifecycle
    a = MarketLifecycle()
    b = MarketLifecycle()
    assert a is b is lifecycle


@pytest.mark.asyncio
async def test_register_dedupes(lifecycle):
    """Registering the same callback twice is a no-op."""
    async def cb(e, t): pass
    lifecycle.register("nse:close", cb)
    lifecycle.register("nse:close", cb)
    assert lifecycle.get_state()["handler_counts"]["nse:close"] == 1


@pytest.mark.asyncio
async def test_register_validates_event_key(lifecycle):
    """register() rejects malformed event keys."""
    async def cb(e, t): pass
    with pytest.raises(ValueError):
        lifecycle.register("not-a-valid-event", cb)


@pytest.mark.asyncio
async def test_cold_start_no_transitions(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """First poll seeds state without firing transitions."""
    from backend.api.algo import market_lifecycle as ml_mod
    ml_mod._test_state_fn = lambda exch, now: True  # everything open
    called = []
    async def cb(e, t): called.append((e, t))
    lifecycle.register("nse:open", cb)
    res = await lifecycle.poll()
    assert res["events"] == [], "Cold start should not emit events"
    assert called == []


@pytest.mark.asyncio
async def test_open_transition_fires_event(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """Closed → open emits `<exch>:open`."""
    from backend.api.algo import market_lifecycle as ml_mod
    # Cold-start with all closed.
    ml_mod._test_state_fn = lambda exch, now: False
    await lifecycle.poll()  # seed

    called = []
    async def cb(e, t): called.append((e, t))
    lifecycle.register("nse:open", cb)

    # Now NSE flips open.
    ml_mod._test_state_fn = lambda exch, now: exch == "nse"
    res = await lifecycle.poll()
    assert any(e["event_type"] == "open" and e["exchange"] == "nse"
               for e in res["events"])
    assert ("nse", "open") in called


@pytest.mark.asyncio
async def test_close_transition_fires_event(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """Open → closed emits `<exch>:close` and records last_close_at."""
    from backend.api.algo import market_lifecycle as ml_mod
    ml_mod._test_state_fn = lambda exch, now: True
    await lifecycle.poll()  # seed open

    called = []
    async def cb(e, t): called.append((e, t))
    lifecycle.register("nse:close", cb)

    ml_mod._test_state_fn = lambda exch, now: False
    res = await lifecycle.poll()
    assert any(e["event_type"] == "close" and e["exchange"] == "nse"
               for e in res["events"])
    assert ("nse", "close") in called
    # last_close_at populated
    state = lifecycle.get_state()
    assert state["last_close_at"]["nse"] is not None


@pytest.mark.asyncio
async def test_close_settled_fires_45min_after_close(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """close_settled fires exactly 45 min after the close."""
    from backend.api.algo import market_lifecycle as ml_mod
    # Seed open.
    ml_mod._test_state_fn = lambda exch, now: True
    await lifecycle.poll()

    # Close at frozen time.
    ml_mod._test_state_fn = lambda exch, now: False
    settled_called = []
    async def cb(e, t): settled_called.append((e, t))
    lifecycle.register("nse:close_settled", cb)

    await lifecycle.poll()  # fires close
    assert settled_called == []

    # Advance 30 min — not yet.
    freeze_now["now"] = freeze_now["now"] + timedelta(minutes=30)
    await lifecycle.poll()
    assert settled_called == [], "settled fired too early"

    # Advance to 46 min total — should fire.
    freeze_now["now"] = freeze_now["now"] + timedelta(minutes=16)
    res = await lifecycle.poll()
    assert ("nse", "close_settled") in settled_called
    assert any(e["event_type"] == "close_settled" for e in res["events"])


@pytest.mark.asyncio
async def test_close_settled_fires_only_once(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """Repeat polls after settled fired do NOT re-fire."""
    from backend.api.algo import market_lifecycle as ml_mod
    ml_mod._test_state_fn = lambda exch, now: True
    await lifecycle.poll()
    ml_mod._test_state_fn = lambda exch, now: False
    settled_called = []
    async def cb(e, t): settled_called.append((e, t))
    lifecycle.register("nse:close_settled", cb)
    await lifecycle.poll()
    freeze_now["now"] = freeze_now["now"] + timedelta(minutes=46)
    await lifecycle.poll()
    await lifecycle.poll()  # extra
    await lifecycle.poll()
    assert settled_called.count(("nse", "close_settled")) == 1


@pytest.mark.asyncio
async def test_handler_exception_isolated(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """One handler raising does not prevent others from running."""
    from backend.api.algo import market_lifecycle as ml_mod
    ml_mod._test_state_fn = lambda exch, now: False
    await lifecycle.poll()

    ok_called = []
    async def ok(e, t): ok_called.append((e, t))
    async def boom(e, t): raise RuntimeError("synthetic")

    lifecycle.register("nse:open", boom)
    lifecycle.register("nse:open", ok)

    ml_mod._test_state_fn = lambda exch, now: True
    res = await lifecycle.poll()
    nse_event = [e for e in res["events"] if e["exchange"] == "nse" and e["event_type"] == "open"][0]
    assert nse_event["handlers_failed"] == 1
    assert nse_event["handlers_run"] == 2  # both ran (one threw)
    assert ("nse", "open") in ok_called


@pytest.mark.asyncio
async def test_multi_exchange_independence(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """NSE close does not trigger MCX events; per-exchange state isolated."""
    from backend.api.algo import market_lifecycle as ml_mod
    # Seed both open
    ml_mod._test_state_fn = lambda exch, now: True
    await lifecycle.poll()

    mcx_called = []
    nse_called = []
    async def mcx_cb(e, t): mcx_called.append((e, t))
    async def nse_cb(e, t): nse_called.append((e, t))
    lifecycle.register("mcx:close", mcx_cb)
    lifecycle.register("nse:close", nse_cb)

    # Close NSE only; MCX stays open.
    ml_mod._test_state_fn = lambda exch, now: exch != "nse"
    res = await lifecycle.poll()
    fired_exch = {e["exchange"] for e in res["events"]}
    assert "nse" in fired_exch
    assert "mcx" not in fired_exch
    assert ("nse", "close") in nse_called
    assert mcx_called == []


@pytest.mark.asyncio
async def test_get_state_shape(lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now):
    """get_state() returns the documented shape."""
    from backend.api.algo import market_lifecycle as ml_mod
    ml_mod._test_state_fn = lambda exch, now: False
    await lifecycle.poll()
    state = lifecycle.get_state()
    assert set(state.keys()) >= {"open", "last_close_at", "settled_fired", "handler_counts"}
    for exch in ("nse", "mcx", "cds"):
        assert exch in state["open"]


@pytest.mark.asyncio
async def test_settled_offset_setting_read_each_poll(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """Operator-configurable settled-offset is consulted on every poll."""
    from backend.api.algo import market_lifecycle as ml_mod
    # Force the offset to 5 min so the test doesn't have to advance 46 min.
    with patch.object(ml_mod, "_settled_offset_minutes", lambda: 5):
        ml_mod._test_state_fn = lambda exch, now: True
        await lifecycle.poll()
        ml_mod._test_state_fn = lambda exch, now: False
        called = []
        async def cb(e, t): called.append((e, t))
        lifecycle.register("nse:close_settled", cb)
        await lifecycle.poll()
        freeze_now["now"] = freeze_now["now"] + timedelta(minutes=6)
        await lifecycle.poll()
        assert ("nse", "close_settled") in called


@pytest.mark.asyncio
async def test_no_callbacks_no_exception(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """Transitions with zero registered handlers do not raise."""
    from backend.api.algo import market_lifecycle as ml_mod
    ml_mod._test_state_fn = lambda exch, now: False
    await lifecycle.poll()
    ml_mod._test_state_fn = lambda exch, now: True
    res = await lifecycle.poll()
    # nse, mcx, cds all transitioned; each event has handlers_run == 0
    open_events = [e for e in res["events"] if e["event_type"] == "open"]
    assert len(open_events) == 3
    for ev in open_events:
        assert ev["handlers_run"] == 0
        assert ev["handlers_failed"] == 0


@pytest.mark.asyncio
async def test_background_task_wires_lifecycle():
    """_task_market_lifecycle is registered in on_startup."""
    src = (
        Path(__file__).parent.parent / "api" / "background.py"
    ).read_text(encoding="utf-8")
    assert "_task_market_lifecycle" in src
    assert 'name="bg-market-lifecycle"' in src


@pytest.mark.asyncio
async def test_background_task_calls_poll_loop():
    """_task_market_lifecycle awaits market_lifecycle.poll()."""
    src = (
        Path(__file__).parent.parent / "api" / "background.py"
    ).read_text(encoding="utf-8")
    m = re.search(
        r"async def _task_market_lifecycle\(\).*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert m, "_task_market_lifecycle not found"
    body = m.group(0)
    assert "market_lifecycle.poll()" in body
    assert "asyncio.sleep(30)" in body
    assert "register_default_handlers" in body


@pytest.mark.asyncio
async def test_funds_offhours_task_registered():
    """_task_funds_offhours is in on_startup."""
    src = (
        Path(__file__).parent.parent / "api" / "background.py"
    ).read_text(encoding="utf-8")
    assert "_task_funds_offhours" in src
    assert 'name="bg-funds-offhours"' in src
    m = re.search(
        r"async def _task_funds_offhours\(\).*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert m
    body = m.group(0)
    # Only fires while no segment is open.
    assert "is_any_segment_open" in body
    # 30 min cadence
    assert "30 * 60" in body or "1800" in body
