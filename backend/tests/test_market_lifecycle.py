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


def test_no_dead_last_seen_date_field():
    """_last_seen_date (the date-rollover reset trigger, removed 2026-10-20
    as part of the double-fire fix) must be fully gone — not left as a
    half-referenced dead field."""
    src = _src()
    assert "_last_seen_date" not in src, (
        "_last_seen_date should be fully removed, not left dangling"
    )


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
async def test_close_settled_fires_after_settled_offset(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """close_settled fires exactly `_settled_offset_minutes()` after
    close. Default is 15 min (post-operator update Jul 2026); the test
    pins the offset to a known value so it's independent of the default.
    """
    from backend.api.algo import market_lifecycle as ml_mod
    # Pin the offset to 15 min for a deterministic test — the settings
    # layer default is also 15 min but a future settings change should
    # not silently move this test.
    from unittest.mock import patch as _patch
    with _patch.object(ml_mod, "_settled_offset_minutes", lambda: 15):
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

        # Advance 10 min — not yet.
        freeze_now["now"] = freeze_now["now"] + timedelta(minutes=10)
        await lifecycle.poll()
        assert settled_called == [], "settled fired too early"

        # Advance to 16 min total — should fire.
        freeze_now["now"] = freeze_now["now"] + timedelta(minutes=6)
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
async def test_close_settled_does_not_refire_on_midnight_rollover(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """Regression test for the 2026-10-20 double-fire bug.

    Root cause: `poll()` used to unconditionally reset `_settled_fired` on
    every bare calendar-date rollover, even for an exchange that had
    already closed AND already settled hours earlier and never reopened.
    Since `_last_close_at` is intentionally left untouched across
    rollover (needed for MCX's close-23:30-plus-offset window spanning
    midnight), the settled-offset check (`now_ist >= last_close +
    offset`) was already trivially satisfied the instant the flag got
    cleared — causing `close_settled` to fire a SECOND time immediately
    after midnight and write a second, spuriously-dated `daily_book`
    snapshot row every single night (confirmed: 115/115 positions +
    holdings rows corrupted on 2026-10-20). The only valid re-arm point
    for `_settled_fired` is the exchange's own open transition (line 195),
    not the wall clock rolling over a date boundary.
    """
    from backend.api.algo import market_lifecycle as ml_mod
    with patch.object(ml_mod, "_settled_offset_minutes", lambda: 15):
        # Seed open, then close just before midnight (23:50 IST).
        freeze_now["now"] = freeze_now["now"].replace(hour=23, minute=0)
        ml_mod._test_state_fn = lambda exch, now: True
        await lifecycle.poll()  # seed open

        ml_mod._test_state_fn = lambda exch, now: False
        settled_called = []
        async def cb(e, t): settled_called.append((e, t))
        lifecycle.register("nse:close_settled", cb)

        freeze_now["now"] = freeze_now["now"].replace(minute=50)
        await lifecycle.poll()  # fires close at 23:50
        assert settled_called == []

        # Advance 16 minutes → 00:06 the NEXT calendar day (crosses
        # midnight) and satisfies the 15-min settled offset — fires once.
        freeze_now["now"] = freeze_now["now"] + timedelta(minutes=16)
        await lifecycle.poll()
        assert settled_called.count(("nse", "close_settled")) == 1

        # Advance further — still closed, still past midnight. Pre-fix,
        # the date-rollover reset block cleared _settled_fired here, and
        # because the offset window was already satisfied, the very next
        # poll fired a SECOND close_settled immediately.
        freeze_now["now"] = freeze_now["now"] + timedelta(minutes=5)
        await lifecycle.poll()
        await lifecycle.poll()
        assert settled_called.count(("nse", "close_settled")) == 1, (
            "close_settled re-fired after a bare midnight date rollover — "
            "the exact double-fire bug this regression test guards against"
        )


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


# ---------------------------------------------------------------------------
# D1b — restart hydration (_hydrate_from_audit)
# ---------------------------------------------------------------------------

class _FakeHydrateResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeHydrateSession:
    """Fake async-session context manager returning pre-seeded rows for
    the one `execute()` call `_hydrate_from_audit` makes."""

    def __init__(self, rows):
        self._rows = rows

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        return _FakeHydrateResult(self._rows)


def test_hydrate_lifecycle_from_rows_replay():
    """Pure replay helper — open clears a prior close cycle; close_settled
    marks the CURRENT cycle settled; a later close starts a fresh cycle."""
    from backend.api.algo.market_lifecycle import _hydrate_lifecycle_from_rows

    t0 = datetime(2026, 6, 28, 15, 30, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=15)
    t2 = t1 + timedelta(hours=10)  # next day's open — supersedes the cycle

    rows = [
        ("nse", "close", t0),
        ("nse", "close_settled", t1),
        ("mcx", "close", t0),
        ("nse", "open", t2),
    ]
    last_close, settled = _hydrate_lifecycle_from_rows(rows)
    assert "nse" not in last_close, "a later open must clear the superseded close cycle"
    assert settled.get("nse") is False
    assert last_close["mcx"] == t0
    assert settled.get("mcx") is False


def test_hydrate_lifecycle_from_rows_skips_malformed():
    """Rows with no exchange or no fired_at are skipped, not raised on."""
    from backend.api.algo.market_lifecycle import _hydrate_lifecycle_from_rows

    t0 = datetime(2026, 6, 28, 15, 30, tzinfo=timezone.utc)
    rows = [("", "close", t0), ("nse", "close", None), ("mcx", "close", t0)]
    last_close, settled = _hydrate_lifecycle_from_rows(rows)
    assert last_close == {"mcx": t0}


@pytest.mark.asyncio
async def test_hydrate_from_audit_restores_mid_window_state_and_fires_on_schedule(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """A restart landing inside the settled window restores last_close_at
    (so close_settled still fires later, on the normal schedule) WITHOUT
    firing it immediately on the hydrating poll."""
    from backend.api.algo import market_lifecycle as ml_mod

    freeze_now["now"] = freeze_now["now"].replace(hour=23, minute=50)
    close_ts = freeze_now["now"] - timedelta(minutes=5)  # closed 5 min ago
    rows = [("nse", "close", close_ts)]

    settled_called = []
    async def cb(e, t): settled_called.append((e, t))
    lifecycle.register("nse:close_settled", cb)

    with patch.object(ml_mod, "_settled_offset_minutes", lambda: 15):
        with patch("backend.api.database.async_session", _FakeHydrateSession(rows)):
            ml_mod._test_state_fn = lambda exch, now: False  # closed
            await lifecycle.poll()  # cold-start poll — triggers hydration

        state = lifecycle.get_state()
        assert state["last_close_at"]["nse"] is not None, (
            "last_close_at must be restored from the audit row"
        )
        assert state["settled_fired"]["nse"] is False, (
            "still within the settled window — must not be marked fired yet"
        )
        assert settled_called == [], "must not fire retroactively on the hydrating poll"

        # Advance 11 more minutes (16 total since close) — fires naturally.
        freeze_now["now"] = freeze_now["now"] + timedelta(minutes=11)
        await lifecycle.poll()
    assert ("nse", "close_settled") in settled_called


@pytest.mark.asyncio
async def test_hydrate_from_audit_does_not_retroactively_fire_past_window(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """A close episode whose settled window has ALREADY elapsed by
    hydration time (e.g. the process crashed before close_settled could
    fire, and the restart happened hours later) must be marked as handled
    instead of firing a stale snapshot write retroactively."""
    from backend.api.algo import market_lifecycle as ml_mod

    freeze_now["now"] = freeze_now["now"].replace(hour=10, minute=0)
    close_ts = freeze_now["now"] - timedelta(hours=2)  # 2h ago — way past 15 min
    rows = [("nse", "close", close_ts)]

    settled_called = []
    async def cb(e, t): settled_called.append((e, t))
    lifecycle.register("nse:close_settled", cb)

    with patch.object(ml_mod, "_settled_offset_minutes", lambda: 15):
        with patch("backend.api.database.async_session", _FakeHydrateSession(rows)):
            ml_mod._test_state_fn = lambda exch, now: False
            await lifecycle.poll()  # cold-start poll — triggers hydration

        assert settled_called == [], "must not retroactively fire on the hydrating poll"
        state = lifecycle.get_state()
        assert state["settled_fired"]["nse"] is True, (
            "a past-window close with no recorded settle must be marked "
            "handled so it never fires late"
        )

        # A further poll still must not fire it.
        freeze_now["now"] = freeze_now["now"] + timedelta(minutes=1)
        await lifecycle.poll()
    assert settled_called == []


@pytest.mark.asyncio
async def test_hydrate_from_audit_runs_once_per_process(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """_hydrated guards the DB query to once per process lifetime."""
    from backend.api.algo import market_lifecycle as ml_mod

    calls = {"n": 0}

    class _CountingSession(_FakeHydrateSession):
        async def execute(self, *a, **k):
            calls["n"] += 1
            return await super().execute(*a, **k)

    # D4 (2026-10-20): poll() now also pre-warms the special-sessions cache
    # per exchange on EVERY call (a separate, unrelated DB dependency from
    # hydration). Stub it out here so this test's call counter stays scoped
    # to hydration specifically, matching its own docstring/intent.
    with patch("backend.api.database.async_session", _CountingSession([])), \
         patch("backend.brokers.broker_apis.fetch_special_sessions_async",
               new=AsyncMock(return_value=[])):
        ml_mod._test_state_fn = lambda exch, now: False
        await lifecycle.poll()
        await lifecycle.poll()
        await lifecycle.poll()
    assert calls["n"] == 1, "_hydrate_from_audit must query the DB at most once per process"


@pytest.mark.asyncio
async def test_hydrate_from_audit_db_failure_is_non_fatal(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """A DB error during hydration logs and degrades to a cold start —
    poll() must still complete and seed state normally."""
    from backend.api.algo import market_lifecycle as ml_mod

    def _boom():
        raise RuntimeError("synthetic DB outage")

    with patch("backend.api.database.async_session", _boom):
        ml_mod._test_state_fn = lambda exch, now: False
        res = await lifecycle.poll()
    assert res["events"] == []
    assert lifecycle.get_state()["open"]["nse"] is False


@pytest.mark.asyncio
async def test_close_transition_resets_settled_fired_after_hydration(
    lifecycle, stub_segments, stub_holidays, freeze_audit, freeze_now,
):
    """Regression (2026-10-20, found during D1b review): a cold start
    that hydrates `_settled_fired=True` from the PREVIOUS close cycle
    (process restarted while the exchange was already open — no `:open`
    transition ever fires to clear it) must NOT suppress close_settled
    for the NEXT, genuinely new close cycle. The close transition must
    start a fresh settle cycle for itself.

    Fails against the pre-fix code (close transition didn't reset
    `_settled_fired`, so the hydrated True from yesterday's cycle
    survived and silently blocked today's close_settled forever).
    """
    from backend.api.algo import market_lifecycle as ml_mod

    # Hydration source: yesterday's close + close_settled, both well
    # outside today's settled window.
    close_ts_prev = freeze_now["now"].replace(day=27, hour=15, minute=30)
    settled_ts_prev = freeze_now["now"].replace(day=27, hour=15, minute=45)
    rows = [("nse", "close", close_ts_prev), ("nse", "close_settled", settled_ts_prev)]

    with patch.object(ml_mod, "_settled_offset_minutes", lambda: 15):
        with patch("backend.api.database.async_session", _FakeHydrateSession(rows)):
            # Cold start at 10:00 IST TODAY, exchange already open —
            # never fires an :open transition.
            ml_mod._test_state_fn = lambda exch, now: True
            await lifecycle.poll()

        state = lifecycle.get_state()
        assert state["settled_fired"]["nse"] is True, (
            "sanity check: hydration must have seeded settled=True from "
            "yesterday's already-settled cycle"
        )

        settled_called = []
        async def cb(e, t): settled_called.append((e, t))
        lifecycle.register("nse:close_settled", cb)

        # Today's genuine close.
        freeze_now["now"] = freeze_now["now"].replace(hour=15, minute=30)
        ml_mod._test_state_fn = lambda exch, now: False
        await lifecycle.poll()

        # 16 minutes later — past the 15-min settled offset.
        freeze_now["now"] = freeze_now["now"] + timedelta(minutes=16)
        await lifecycle.poll()

    assert settled_called.count(("nse", "close_settled")) == 1, (
        "close_settled must fire for TODAY's close cycle even though "
        "settled_fired was hydrated True from yesterday's cycle"
    )



# ---------------------------------------------------------------------------
# D4 (2026-10-20) — poll() must pre-warm the special-sessions cache before
# the per-exchange _exchange_is_open() loop, closing the same cold-cache/
# main-loop-self-deadlock gap documented in
# broker_apis.py:fetch_special_sessions for the OTHER genuinely-async call
# sites (ticket/basket/chase). poll() itself runs on the main event loop
# (`_task_market_lifecycle` -> `asyncio.create_task` -> `await
# market_lifecycle.poll()`), and is the mechanism that drives
# close_settled/daily_book snapshot writes -- the exact surface the
# 2026-10-20 prev_close-corruption incident was about.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_prewarms_special_sessions_before_exchange_probe(
    lifecycle, freeze_audit,
):
    """Reproduces the gap directly: a DB-confirmed MCX-evening-closed
    special-session row (zero-width, start==end) exists for today, MCX's
    segment has `evening_open_on_holidays=True`, today is a declared MCX
    holiday, and the wall clock is 18:00 IST (inside the 17:00-23:30
    evening window). `poll()` is simulated as genuinely running on the
    main event loop (same `write_queue._main_loop` monkeypatch pattern as
    `test_market_hours_unification.py`'s
    `test_cold_cache_on_main_loop_fails_open_without_poisoning` /
    `test_prewarm_closes_the_gap_for_the_3_genuinely_async_call_sites`).

    Without the prewarm, `_exchange_is_open()`'s sync
    `fetch_special_sessions()` call trips the main-loop self-deadlock
    guard and fails open to `[]` for a cold cache -- the DB-confirmed
    override becomes invisible, so `is_market_open()` falls through to
    the blanket `evening_open_on_holidays` rule and reads MCX as OPEN,
    flipping a previously-closed `_open_state["mcx"]` to open and firing
    a spurious `mcx:open` event. With the prewarm, the cache is warmed
    (via a real `await` of `_read_special_sessions_async`, no
    `run_on_main_loop` hop, no deadlock risk) before `_exchange_is_open()`
    runs, so the sync call hits a warm cache and sees the real
    zero-width "closed all day" override -- MCX correctly stays closed
    and no transition fires.
    """
    import asyncio
    from datetime import time as dtime_
    from unittest.mock import AsyncMock, patch
    from zoneinfo import ZoneInfo
    import backend.api.algo.market_lifecycle as ml_mod
    import backend.brokers.broker_apis as _ba
    import backend.api.persistence.write_queue as _wq

    IST = ZoneInfo("Asia/Kolkata")
    now_ist = datetime(2026, 11, 5, 18, 0, tzinfo=IST)
    today = now_ist.date()

    def _fake_enumerate():
        return {
            "mcx": {
                "name": "commodity", "exchange": "MCX",
                "sessions": [
                    {"start": dtime_(9, 0), "end": dtime_(17, 0)},
                    {"start": dtime_(17, 0), "end": dtime_(23, 30)},
                ],
                "hours_start": dtime_(9, 0), "hours_end": dtime_(23, 30),
                "evening_open_on_holidays": True,
            },
        }

    closed_row = [{"date": today, "start": dtime_(0, 0), "end": dtime_(0, 0)}]

    # Simulate MCX was closed earlier today (day session over, pre-evening)
    # — a real prior-closed state, not poll()'s own (buggy-seedable) cold
    # start, so the spurious transition this test guards against is
    # unambiguous.
    lifecycle._open_state["mcx"] = False
    lifecycle._settled_fired["mcx"] = False
    lifecycle._last_close_at["mcx"] = None

    loop = asyncio.get_running_loop()
    orig_main_loop = _wq._main_loop
    _ba._SPECIAL_SESSION_CACHE.clear()
    try:
        _wq._main_loop = loop  # poll() genuinely runs on the main loop
        with patch.object(ml_mod, "_enumerate_exchanges", _fake_enumerate), \
             patch.object(ml_mod, "timestamp_indian", lambda: now_ist), \
             patch("backend.brokers.broker_apis.fetch_holidays", lambda exch: {today}), \
             patch.object(_ba, "_read_special_sessions_async", new=AsyncMock(return_value=closed_row)):
            res = await ml_mod.market_lifecycle.poll()
    finally:
        _wq._main_loop = orig_main_loop
        _ba._SPECIAL_SESSION_CACHE.clear()

    assert res["events"] == [], (
        f"expected no transition — the DB-confirmed evening-closed "
        f"override must keep MCX closed despite evening_open_on_holidays, "
        f"got {res['events']}"
    )
    assert lifecycle.get_state()["open"]["mcx"] is False, (
        "MCX must stay closed; a cold special-sessions cache must not "
        "silently fall back to the blanket evening-open default"
    )


@pytest.mark.asyncio
async def test_poll_prewarm_key_matches_exchange_is_open_lookup_for_every_segment(
    lifecycle, freeze_audit,
):
    """Regression found during review: poll()'s prewarm must warm the
    special-sessions cache under the EXACT key `_exchange_is_open()`
    will later look up -- `segment["exchange"]` directly -- not a
    `holiday_exchange` resolution. The synthesized `cds` segment (from
    `_enumerate_exchanges()`) carries `exchange="CDS"`, which does NOT
    equal equity's own `holiday_exchange` of `"NSE"`. Prewarming via
    `agent_engine._prewarm_special_sessions_for_exchange` (which
    resolves `"CDS"` -> the `equity` segment -> `holiday_exchange`
    `"NSE"`) would warm the `"NSE"` cache entry while `_exchange_is_open`
    later calls `fetch_special_sessions("CDS")` — leaving CDS cold
    despite the prewarm having run. Calling
    `fetch_special_sessions_async(segment["exchange"])` directly avoids
    this mismatch for every exchange, not just CDS."""
    import asyncio
    from datetime import time as dtime_
    from unittest.mock import AsyncMock, patch
    from zoneinfo import ZoneInfo
    import backend.api.algo.market_lifecycle as ml_mod
    import backend.brokers.broker_apis as _ba
    import backend.api.persistence.write_queue as _wq

    IST = ZoneInfo("Asia/Kolkata")
    now_ist = datetime(2026, 11, 5, 10, 0, tzinfo=IST)

    def _fake_enumerate():
        return {
            "nse": {"name": "equity", "exchange": "NSE",
                    "sessions": [{"start": dtime_(9, 15), "end": dtime_(15, 30)}],
                    "hours_start": dtime_(9, 15), "hours_end": dtime_(15, 30),
                    "evening_open_on_holidays": False},
            "cds": {"name": "currency", "exchange": "CDS",
                    "sessions": [{"start": dtime_(9, 15), "end": dtime_(15, 30)}],
                    "hours_start": dtime_(9, 15), "hours_end": dtime_(15, 30),
                    "evening_open_on_holidays": False},
        }

    loop = asyncio.get_running_loop()
    orig_main_loop = _wq._main_loop
    _ba._SPECIAL_SESSION_CACHE.clear()
    cached_keys: set = set()
    try:
        _wq._main_loop = loop
        with patch.object(ml_mod, "_enumerate_exchanges", _fake_enumerate), \
             patch.object(ml_mod, "timestamp_indian", lambda: now_ist), \
             patch("backend.brokers.broker_apis.fetch_holidays", lambda exch: set()), \
             patch.object(_ba, "_read_special_sessions_async", new=AsyncMock(return_value=[])):
            await ml_mod.market_lifecycle.poll()
        cached_keys = set(_ba._SPECIAL_SESSION_CACHE.keys())
    finally:
        _wq._main_loop = orig_main_loop
        _ba._SPECIAL_SESSION_CACHE.clear()

    assert "CDS" in cached_keys, (
        f"poll() must warm the cache under segment['exchange'] directly "
        f"for every segment, including the synthesized cds entry — got "
        f"cached keys {cached_keys}"
    )
    assert "NSE" in cached_keys


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
    # 2026-10-28 reversal: the task is a no-op during closed hours — it must
    # not perform the old off-hours broker fetch or any segment check.
    assert "_fetch_margins_direct" not in body
    assert "is_any_segment_open" not in body
    # 30 min cadence (task still registered and still ticking)
    assert "30 * 60" in body or "1800" in body
