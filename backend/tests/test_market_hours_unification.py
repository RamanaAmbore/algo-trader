"""
D2 (2026-10-20) — unify all `is_market_open()` call sites through
`_segment_is_open()`.

Verified defect: six call sites used the legacy positional
`is_market_open(now, holidays, start, end, exchange=...)` form, never
passing `sessions=`/`evening_open_on_holidays=` — so an MCX evening
session on an equity (not MCX) holiday always read CLOSED via those
sites, disagreeing with the two already-correct callers
(`_segment_is_open` itself, `market_lifecycle._exchange_is_open`).

This file constructs one fixture — "MCX holiday evening, in-window"
(an MCX-calendar holiday, 18:00 IST, inside the 17:00-23:30 evening
session) — and asserts all 6 unified call sites agree: MCX reads OPEN.

Also covers the simulator.py `_build_segments` → `_get_segments` ImportError
regression found while implementing this fix (confirmed live via direct
import before this fix; see commit history).
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

# Captured at module-import time, BEFORE the autouse fixture below patches
# `fetch_special_sessions` — the D3-Step-2 deadlock-guard tests near the
# bottom of this file need the REAL implementation (cache + guard
# behaviour), not the always-`[]` stub every other test in this file relies
# on, so they nest a `patch.object(..., side_effect=_REAL_FETCH_SPECIAL_SESSIONS)`
# back over the autouse patch for their own duration.
from backend.brokers.broker_apis import fetch_special_sessions as _REAL_FETCH_SPECIAL_SESSIONS

IST = ZoneInfo("Asia/Kolkata")

# MCX-holiday-evening fixture — 18:00 IST, a date we DECLARE as an MCX
# holiday via mock (no dependency on the real calendar), inside the
# 17:00-23:30 evening session window `evening_open_on_holidays` opens.
_FIXTURE_NOW = datetime(2026, 10, 22, 18, 0, tzinfo=IST)
_MCX_HOLIDAY_DATE = _FIXTURE_NOW.date()


def _fake_fetch_holidays(exchange: str) -> set:
    """MCX has a holiday today; NSE does not."""
    if exchange.upper() == "MCX":
        return {_MCX_HOLIDAY_DATE}
    return set()


def _fake_fetch_special_sessions(exchange: str) -> list:
    return []


@pytest.fixture(autouse=True)
def _patch_holiday_sources():
    """Patch at the ORIGIN module — every call site imports these names
    locally from `backend.brokers.broker_apis`, so one patch covers all
    6 unified sites uniformly."""
    with patch("backend.brokers.broker_apis.fetch_holidays", side_effect=_fake_fetch_holidays), \
         patch("backend.brokers.broker_apis.fetch_special_sessions", side_effect=_fake_fetch_special_sessions):
        yield


def _commodity_seg() -> dict:
    from backend.shared.helpers.utils import config as _cfg
    return (_cfg.get("market_segments", {}) or {}).get("commodity", {}) or {}


def _equity_seg() -> dict:
    from backend.shared.helpers.utils import config as _cfg
    return (_cfg.get("market_segments", {}) or {}).get("equity", {}) or {}


# ---------------------------------------------------------------------------
# Baseline — already-correct reference implementation.
# ---------------------------------------------------------------------------

def test_baseline_segment_is_open_mcx_evening_holiday():
    from backend.shared.helpers.date_time_utils import _segment_is_open
    assert _segment_is_open(_commodity_seg(), _FIXTURE_NOW) is True
    assert _segment_is_open(_equity_seg(), _FIXTURE_NOW) is False


# ---------------------------------------------------------------------------
# Site 1 — background._perf_probe_open_segments
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_perf_probe_open_segments_agrees():
    from backend.api.background import _perf_probe_open_segments

    mcx_gate = {"name": "MCX", "hours_start": dtime(8, 0), "hours_end": dtime(23, 30),
                "holiday_exchange": "MCX"}
    nse_gate = {"name": "NON-MCX", "hours_start": dtime(8, 0), "hours_end": dtime(15, 30),
                "holiday_exchange": "NSE"}
    open_segs = await _perf_probe_open_segments([mcx_gate, nse_gate], {}, _FIXTURE_NOW)
    open_names = {s["name"] for s in open_segs}
    assert "MCX" in open_names
    assert "NON-MCX" not in open_names


# ---------------------------------------------------------------------------
# Site 2 — background._watchdog_check_market_open
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watchdog_check_market_open_agrees():
    from backend.api import background as bg

    with patch.object(bg, "timestamp_indian", return_value=_FIXTURE_NOW), \
         patch.object(bg, "_get_segments", return_value=[
             {"name": "MCX", "hours_start": dtime(8, 0), "hours_end": dtime(23, 30),
              "holiday_exchange": "MCX"},
             {"name": "NON-MCX", "hours_start": dtime(8, 0), "hours_end": dtime(15, 30),
              "holiday_exchange": "NSE"},
         ]):
        any_open = await bg._watchdog_check_market_open(
            holiday_cache={}, holiday_year_ref=[_FIXTURE_NOW.year],
            fetch_holidays=_fake_fetch_holidays,
        )
    assert any_open is True  # MCX open covers it


# ---------------------------------------------------------------------------
# Site 3 — routes/market._compute_market_status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compute_market_status_agrees():
    from backend.api.routes.market import _compute_market_status

    with patch("backend.api.routes.market.timestamp_indian", return_value=_FIXTURE_NOW):
        status = await _compute_market_status()
    assert status.mcx_open is True
    assert status.nse_open is False
    assert status.any_open is True


# ---------------------------------------------------------------------------
# Site 4 — routes/quote._qt_is_seg_open
# ---------------------------------------------------------------------------

def test_qt_is_seg_open_agrees():
    from backend.api.routes.quote import _qt_is_seg_open

    result = _qt_is_seg_open(_commodity_seg(), "MCX", _FIXTURE_NOW, _fake_fetch_holidays)
    assert result is True


def test_qt_is_seg_open_returns_none_for_non_matching_exchange():
    """Exchange-membership matching (unique to this call site) is preserved."""
    from backend.api.routes.quote import _qt_is_seg_open

    result = _qt_is_seg_open(_commodity_seg(), "NSE", _FIXTURE_NOW, _fake_fetch_holidays)
    assert result is None


# ---------------------------------------------------------------------------
# Site 5 & 6 — routes/simulator._market_hours_block_check / _markets_currently_open
# ---------------------------------------------------------------------------

def test_simulator_markets_currently_open_agrees():
    from backend.api.routes import simulator as sim
    from backend.api import background as bg

    with patch.object(bg, "_get_segments", return_value=[
            {"name": "MCX", "hours_start": dtime(8, 0), "hours_end": dtime(23, 30),
             "holiday_exchange": "MCX"},
            {"name": "NON-MCX", "hours_start": dtime(8, 0), "hours_end": dtime(15, 30),
             "holiday_exchange": "NSE"},
         ]), \
         patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=_FIXTURE_NOW):
        assert sim._markets_currently_open() is True


def test_simulator_market_hours_block_check_agrees():
    """The 409 hard-block fires when MCX is open — this ALSO regression-
    tests the _build_segments -> _get_segments ImportError (confirmed
    live, every call, before this fix): pre-fix this raised ImportError
    (an unrelated exception) instead of the intended HTTPException(409)."""
    from backend.api.routes import simulator as sim
    from backend.api import background as bg
    from litestar.exceptions import HTTPException

    with patch("backend.shared.helpers.settings.get_bool", return_value=True), \
         patch.object(bg, "_get_segments", return_value=[
             {"name": "MCX", "hours_start": dtime(8, 0), "hours_end": dtime(23, 30),
              "holiday_exchange": "MCX"},
             {"name": "NON-MCX", "hours_start": dtime(8, 0), "hours_end": dtime(15, 30),
              "holiday_exchange": "NSE"},
         ]), \
         patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=_FIXTURE_NOW):
        with pytest.raises(HTTPException) as exc_info:
            sim._market_hours_block_check()
    assert exc_info.value.status_code == 409


def test_simulator_market_hours_block_check_import_does_not_raise_importerror():
    """Direct regression test for the confirmed-live ImportError: calling
    this function must never raise ImportError (the pre-fix symptom),
    regardless of market state."""
    from backend.api.routes import simulator as sim
    try:
        sim._market_hours_block_check()
    except ImportError:
        pytest.fail("_market_hours_block_check must not raise ImportError")
    except Exception:
        pass  # any other outcome (409, or no-op) is fine for this test


# ---------------------------------------------------------------------------
# Site 6 (route) — routes/watchlist._movers_probe_market_state
# ---------------------------------------------------------------------------

def test_movers_probe_market_state_agrees():
    from backend.api.routes.watchlist import _movers_probe_market_state

    nse_open, mcx_open = _movers_probe_market_state(_FIXTURE_NOW)
    assert mcx_open is True
    assert nse_open is False


# ---------------------------------------------------------------------------
# Cross-site agreement — all 6 sites must agree MCX is open at the fixture.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_six_sites_agree_mcx_open():
    from backend.api.background import _perf_probe_open_segments
    from backend.api.routes.market import _compute_market_status
    from backend.api.routes.quote import _qt_is_seg_open
    from backend.api.routes.watchlist import _movers_probe_market_state
    from backend.api.routes import simulator as sim
    from backend.api import background as bg

    mcx_gate = {"name": "MCX", "hours_start": dtime(8, 0), "hours_end": dtime(23, 30),
                "holiday_exchange": "MCX"}
    nse_gate = {"name": "NON-MCX", "hours_start": dtime(8, 0), "hours_end": dtime(15, 30),
                "holiday_exchange": "NSE"}

    verdicts = {}
    verdicts["perf_probe"] = bool(
        {s["name"] for s in await _perf_probe_open_segments([mcx_gate, nse_gate], {}, _FIXTURE_NOW)}
        & {"MCX"}
    )
    # _watchdog_check_market_open calls timestamp_indian() directly
    # (background.py:5809) and has no `now=` parameter — without
    # patching THIS module's own reference, it evaluates against the
    # REAL wall clock at whatever moment the test happens to run,
    # disagreeing with every other (correctly fixture-patched) verdict
    # purely by coincidence of real time. Confirmed live: this test
    # failed for real around IST midnight, when MCX's actual real-time
    # state differs from the fixture's intended "evening holiday open"
    # scenario (2026-10-20 fix).
    with patch("backend.api.background.timestamp_indian", return_value=_FIXTURE_NOW):
        verdicts["watchdog"] = await bg._watchdog_check_market_open(
            holiday_cache={}, holiday_year_ref=[_FIXTURE_NOW.year],
            fetch_holidays=_fake_fetch_holidays,
        )
    with patch("backend.api.routes.market.timestamp_indian", return_value=_FIXTURE_NOW):
        verdicts["market_status"] = (await _compute_market_status()).mcx_open
    verdicts["quote_seg"] = _qt_is_seg_open(
        _commodity_seg(), "MCX", _FIXTURE_NOW, _fake_fetch_holidays,
    )
    with patch.object(bg, "_get_segments", return_value=[mcx_gate, nse_gate]), \
         patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=_FIXTURE_NOW):
        verdicts["simulator"] = sim._markets_currently_open()
    verdicts["movers"] = _movers_probe_market_state(_FIXTURE_NOW)[1]

    assert all(verdicts.values()), f"all 6 sites must agree MCX is open: {verdicts}"


# ---------------------------------------------------------------------------
# Regression: gate-shaped sites (_perf_probe_open_segments,
# _watchdog_check_market_open, simulator's two functions) must use the
# GATE's OWN hours_start/hours_end (exchange_schedule-sourced, per-date-
# accurate — a future DST-adjusted MCX close or Muhurat override row is
# read from the SAME source) — NEVER the YAML's static `sessions` list,
# which would silently discard that per-date accuracy.
# ---------------------------------------------------------------------------

# A deliberately NON-standard gate window (not matching YAML's 09:00-23:30
# commodity default) — if any site silently substitutes the YAML sessions
# list, this window stops mattering and the assertions below fail.
_NONSTANDARD_MCX_GATE = {
    "name": "MCX", "hours_start": dtime(6, 0), "hours_end": dtime(22, 0),
    "holiday_exchange": "MCX",
}
# 05:45 IST — inside the gate's own 06:00-22:00 window? No — BEFORE it.
# Used to prove the gate's own hours (not YAML's 09:00 start) gate the result.
_BEFORE_GATE_OPEN = datetime(2026, 10, 21, 5, 45, tzinfo=IST)  # ordinary weekday, no holiday
_AFTER_GATE_OPEN = datetime(2026, 10, 21, 6, 30, tzinfo=IST)


@pytest.mark.asyncio
async def test_perf_probe_open_segments_uses_gate_own_hours_not_yaml():
    from backend.api.background import _perf_probe_open_segments

    # Before the gate's own 06:00 open (but after YAML's 09:00 default
    # would have mattered) — must read CLOSED only because of the gate's
    # own window, proving YAML sessions aren't silently substituted in.
    closed = await _perf_probe_open_segments([_NONSTANDARD_MCX_GATE], {}, _BEFORE_GATE_OPEN)
    assert closed == []

    opened = await _perf_probe_open_segments([_NONSTANDARD_MCX_GATE], {}, _AFTER_GATE_OPEN)
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_watchdog_check_market_open_uses_gate_own_hours_not_yaml():
    from backend.api import background as bg

    with patch.object(bg, "timestamp_indian", return_value=_BEFORE_GATE_OPEN), \
         patch.object(bg, "_get_segments", return_value=[_NONSTANDARD_MCX_GATE]):
        closed = await bg._watchdog_check_market_open(
            holiday_cache={}, holiday_year_ref=[_BEFORE_GATE_OPEN.year],
            fetch_holidays=_fake_fetch_holidays,
        )
    assert closed is False

    with patch.object(bg, "timestamp_indian", return_value=_AFTER_GATE_OPEN), \
         patch.object(bg, "_get_segments", return_value=[_NONSTANDARD_MCX_GATE]):
        opened = await bg._watchdog_check_market_open(
            holiday_cache={}, holiday_year_ref=[_AFTER_GATE_OPEN.year],
            fetch_holidays=_fake_fetch_holidays,
        )
    assert opened is True


def test_simulator_markets_currently_open_uses_gate_own_hours_not_yaml():
    from backend.api.routes import simulator as sim
    from backend.api import background as bg

    with patch.object(bg, "_get_segments", return_value=[_NONSTANDARD_MCX_GATE]), \
         patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=_BEFORE_GATE_OPEN):
        assert sim._markets_currently_open() is False

    with patch.object(bg, "_get_segments", return_value=[_NONSTANDARD_MCX_GATE]), \
         patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=_AFTER_GATE_OPEN):
        assert sim._markets_currently_open() is True


def test_perf_probe_open_segments_source_never_passes_sessions_kwarg():
    """Source-level guard — the gate-shaped sites must never pass the
    bare `sessions=` kwarg to is_market_open (that would substitute the
    YAML's static window for the gate's own per-date-accurate one).
    Scoped with a word-boundary regex so `special_sessions=` (a
    different, legitimate kwarg) doesn't false-positive."""
    import inspect
    import re
    from backend.api import background as bg

    pattern = re.compile(r"(?<![A-Za-z_])sessions=")

    for fn in (bg._perf_probe_open_segments, bg._watchdog_check_market_open):
        src = inspect.getsource(fn)
        assert not pattern.search(src), (
            f"{fn.__name__} must not pass sessions= — it would override "
            f"the gate's own exchange_schedule-sourced hours_start/hours_end"
        )

    from backend.api.routes import simulator as sim
    for fn in (sim._market_hours_block_check, sim._markets_currently_open):
        src = inspect.getsource(fn)
        assert not pattern.search(src)


# ---------------------------------------------------------------------------
# D3 (2026-10-20) — `_ae_segment_flags` (the Phase-23 order/chase/basket/
# template-attach/agent market-open gate) now routes through
# `is_market_open()` with sessions=/evening_open_on_holidays= instead of a
# blind hours_start/hours_end comparison. Live incident: an MCX evening
# holiday session's ONLY path to ever read "open" was the live Kite
# bellwether probe fallback.
# ---------------------------------------------------------------------------


def test_ae_segment_flags_mcx_evening_holiday_open_without_probe():
    """MCX genuinely open in its evening (holiday) session must read open
    WITHOUT depending on the live Kite bellwether probe at all. The probe
    is mocked to raise AND call-counted — proving it's never consulted
    for this deterministic branch (the exact live-reported bug: the
    pre-fix code's ONLY path to "open" here was the probe)."""
    from backend.api.algo.agent_engine import _ae_segment_flags
    from backend.shared.helpers import market_probe

    def _boom(exchange):
        raise RuntimeError("probe must not be called for this deterministic path")

    with patch.object(market_probe, "probe_market_active", side_effect=_boom) as mock_probe:
        flags = _ae_segment_flags("commodity", _commodity_seg(), _FIXTURE_NOW)

    assert flags["mcx_open"] is True, flags
    assert mock_probe.call_count == 0, (
        "the evening-holiday-open verdict must never consult the probe"
    )


def test_ae_segment_flags_minutes_since_open_anchored_to_evening_not_window_start():
    """minutes_since_mcx_open must anchor to the 17:00 evening-open time,
    not the session window's 09:00 start — else it reports ~540 at 18:00
    instead of ~60 (the literal pre-fix defect named in the incident)."""
    from backend.api.algo.agent_engine import _ae_segment_flags

    flags = _ae_segment_flags("commodity", _commodity_seg(), _FIXTURE_NOW)
    assert flags["mcx_open"] is True
    mins = flags["minutes_since_mcx_open"]
    assert mins < 120, f"minutes_since_mcx_open should be ~60 (anchored to 17:00), got {mins}"
    assert mins == 60, f"expected exactly 60 minutes since 17:00 at 18:00 IST, got {mins}"


def test_ae_segment_flags_equity_holiday_unaffected():
    """Equity segment (evening_open_on_holidays=False) must stay closed
    on the same MCX-only holiday fixture — scope check, no cross-talk."""
    from backend.api.algo.agent_engine import _ae_segment_flags

    flags = _ae_segment_flags("equity", _equity_seg(), _FIXTURE_NOW)
    assert flags["nse_open"] is False
    assert flags["minutes_since_nse_open"] == 0


# ---------------------------------------------------------------------------
# D6 (2026-10-20) — `_ae_segment_flags` special-session-aware `_closed` /
# `minutes_since_*` fix. `is_open` (via `is_market_open()`) is already
# correct for all 3 cases below; only the matched-window / had-session-today
# bookkeeping was wrong.
# ---------------------------------------------------------------------------

# A plain weekday, not in the MCX-holiday fixture above (2026-10-22) and not
# an NSE holiday either — isolates these cases from the evening-holiday
# branch entirely.
_PLAIN_WEEKDAY = date(2026, 11, 5)


def test_ae_segment_flags_zero_width_override_reports_neither_open_nor_closed():
    """Case 1: an MCX evening-closed holiday (zero-width override row) at
    23:45 must report mcx_open=False AND mcx_closed=False — no session
    ran today at all, so neither flag should be True. Pre-fix:
    had_session_today derived from evening_open_on_holidays (True),
    wrongly reporting mcx_closed=True alongside mcx_open=False."""
    import backend.brokers.broker_apis as _ba
    from backend.api.algo.agent_engine import _ae_segment_flags

    now = datetime(2026, 11, 5, 23, 45, tzinfo=IST)
    zero_width_row = [{"date": _PLAIN_WEEKDAY, "start": dtime(0, 0), "end": dtime(0, 0)}]

    with patch.object(_ba, "fetch_special_sessions", return_value=zero_width_row):
        flags = _ae_segment_flags("commodity", _commodity_seg(), now)

    assert flags["mcx_open"] is False
    assert flags["mcx_closed"] is False, (
        f"no session ran today (zero-width override) — neither flag "
        f"should be True, got {flags}"
    )


def test_ae_segment_flags_special_session_anchors_minutes_since_open_to_its_own_start():
    """Case 2: a Muhurat-style special session (18:15-19:15) on an
    otherwise normal weekday, checked at 18:30, must anchor
    minutes_since_nse_open to 18:15 (15 min), not the regular 09:15
    session start (~555 min)."""
    import backend.brokers.broker_apis as _ba
    from backend.api.algo.agent_engine import _ae_segment_flags

    now = datetime(2026, 11, 5, 18, 30, tzinfo=IST)
    muhurat_row = [{"date": _PLAIN_WEEKDAY, "start": dtime(18, 15), "end": dtime(19, 15)}]

    with patch.object(_ba, "fetch_special_sessions", return_value=muhurat_row):
        flags = _ae_segment_flags("equity", _equity_seg(), now)

    assert flags["nse_open"] is True
    assert flags["minutes_since_nse_open"] == 15, (
        f"expected 15 (anchored to the override's own 18:15 start), "
        f"got {flags['minutes_since_nse_open']}"
    )


def test_ae_segment_flags_special_session_past_regular_close_not_double_flagged():
    """Case 3: a special session (16:00-19:00) on an otherwise normal
    weekday, checked at 18:30 (after the regular 15:30 close but still
    inside the override window), must report nse_open=True AND
    nse_closed=False — not both True simultaneously."""
    import backend.brokers.broker_apis as _ba
    from backend.api.algo.agent_engine import _ae_segment_flags

    now = datetime(2026, 11, 5, 18, 30, tzinfo=IST)
    extended_row = [{"date": _PLAIN_WEEKDAY, "start": dtime(16, 0), "end": dtime(19, 0)}]

    with patch.object(_ba, "fetch_special_sessions", return_value=extended_row):
        flags = _ae_segment_flags("equity", _equity_seg(), now)

    assert flags["nse_open"] is True
    assert flags["nse_closed"] is False, (
        f"still inside the override window (ends 19:00) — nse_closed "
        f"must not also be True, got {flags}"
    )


def test_symbol_exchange_open_nco_mapped_to_commodity_segment():
    """`NCO` was missing from `_EXCHANGE_TO_SEGMENT` — `_segment_for_exchange`
    returned None for it, so `_symbol_exchange_open("NCO", ctx)` always
    returned False regardless of `ctx["mcx_open"]`, unconditionally
    rejecting every NCO order/basket/GTT-wing/chase as "market closed".
    Every other call site in the app (~25) already treats NCO identically
    to MCX — fixed by adding the mapping entry."""
    from backend.api.algo.agent_engine import _symbol_exchange_open

    assert _symbol_exchange_open("NCO", {"mcx_open": True, "nse_open": False}) is True
    assert _symbol_exchange_open("NCO", {"mcx_open": False, "nse_open": True}) is False
    # MCX itself must be unaffected by the fix.
    assert _symbol_exchange_open("MCX", {"mcx_open": True, "nse_open": False}) is True


# ---------------------------------------------------------------------------
# D3 Step 2 — `fetch_special_sessions()`'s self-deadlock-guard gap.
#
# `run_on_main_loop` returns a sentinel instead of running the DB query
# when the caller is a coroutine ALREADY running on the main event loop —
# exactly the shape of the 5 real call sites that reach `_ae_segment_flags`
# (ticket placement, basket, template attach ×2, chase). The fix doesn't
# bypass the guard (that would be a real deadlock) — it (a) stops caching
# the guard's default so a failed call never poisons the day, and (b)
# pre-warms the cache from async code (`warm_special_sessions_cache`,
# called at startup + after the daily 05:30 holiday refresh) so the sync
# callers hit a warm cache instead of the guard in the common case.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_special_session_override_reaches_on_loop_sync_caller_via_warm_cache():
    """Seed a market_special_sessions-shaped row marking MCX's evening
    explicitly CLOSED on the fixture date, warm the cache from async code,
    then call `_build_now_ctx()` from a context that mimics the
    previously-broken sync call sites: a coroutine already running on the
    SAME loop `write_queue._main_loop` points at. Assert `mcx_open=False`
    — the override reached the sync caller via the warm cache, not via
    bypassing (or silently tripping) the deadlock guard.

    Control (no override row): the exact same on-main-loop setup instead
    yields `mcx_open=True` (ordinary evening-open-on-holiday logic) —
    proving this test doesn't pass vacuously (e.g. via the weekday guard
    or some unrelated short-circuit).
    """
    import asyncio
    import backend.brokers.broker_apis as _ba
    import backend.api.persistence.write_queue as _wq
    from backend.api.algo import agent_engine as _ae

    closed_all_day_row = {
        "date": _FIXTURE_NOW.date(),
        "start": dtime(0, 0),
        "end": dtime(0, 0),   # zero-width — half-open [start, end) never matches
    }

    loop = asyncio.get_running_loop()
    orig_main_loop = _wq._main_loop
    _ba._SPECIAL_SESSION_CACHE.clear()

    def _boom(*a, **kw):
        raise AssertionError("must not reach run_on_main_loop — cache should be warm")

    try:
        # Nest the REAL fetch_special_sessions back over this file's
        # autouse always-`[]` stub — this test exercises the real
        # cache/guard mechanics, not the stub.
        with patch.object(_ba, "fetch_special_sessions", side_effect=_REAL_FETCH_SPECIAL_SESSIONS), \
             patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=_FIXTURE_NOW):

            # ── Override present ────────────────────────────────────
            with patch.object(_ba, "_read_special_sessions_async",
                               AsyncMock(return_value=[closed_all_day_row])):
                await _ba.warm_special_sessions_cache()

            _wq._main_loop = loop  # simulate "already on the main loop"

            # Prove the warm cache is what's being hit, not the guard:
            # if fetch_special_sessions fell through to
            # run_on_main_loop here, this would raise (deadlock-guard
            # path must never be reached when the cache is warm).
            with patch.object(_wq, "run_on_main_loop", side_effect=_boom), \
                 patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=_FIXTURE_NOW):
                ctx = _ae._build_now_ctx()
            assert ctx["mcx_open"] is False, (
                f"special-session override must close MCX all day — ctx={ctx}"
            )

            # ── Control: no override row → ordinary evening-open applies ──
            _ba._SPECIAL_SESSION_CACHE.clear()
            with patch.object(_ba, "_read_special_sessions_async",
                               AsyncMock(return_value=[])):
                await _ba.warm_special_sessions_cache()
            with patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=_FIXTURE_NOW):
                ctx_control = _ae._build_now_ctx()
            assert ctx_control["mcx_open"] is True, (
                f"control (no override) must fall back to evening-open-on-holiday — ctx={ctx_control}"
            )
    finally:
        _wq._main_loop = orig_main_loop
        _ba._SPECIAL_SESSION_CACHE.clear()


@pytest.mark.asyncio
async def test_cold_cache_on_main_loop_fails_open_without_poisoning():
    """A genuinely cold cache (never warmed) hit from a coroutine already
    on the main loop must fail open to `[]` for that call, and must NOT
    write anything into the cache — leaving the door open for a later
    call (e.g. after a successful warm) to get the real answer instead
    of being stuck with a falsely-cached empty result for the rest of
    the day."""
    import asyncio
    import backend.brokers.broker_apis as _ba
    import backend.api.persistence.write_queue as _wq

    loop = asyncio.get_running_loop()
    orig_main_loop = _wq._main_loop
    _ba._SPECIAL_SESSION_CACHE.clear()
    try:
        _wq._main_loop = loop  # simulate "already on the main loop"
        # Call the REAL implementation directly — this file's autouse
        # fixture stubs the module-level name, so bypass it via the
        # module-import-time-captured reference.
        result = _REAL_FETCH_SPECIAL_SESSIONS("MCX")
        assert result == []
        assert "MCX" not in _ba._SPECIAL_SESSION_CACHE, (
            "a guard-triggered failure must leave the cache untouched, "
            "not pre-populate it with a false empty result"
        )
    finally:
        _wq._main_loop = orig_main_loop
        _ba._SPECIAL_SESSION_CACHE.clear()


@pytest.mark.asyncio
async def test_prewarm_closes_the_gap_for_the_3_genuinely_async_call_sites():
    """2026-10-20 fix: for the 3 real call sites that are `async def` all
    the way up (ticket placement, basket, chase), the sync
    `fetch_special_sessions()` guard-triggered `[]` from the test above is
    NOT the end of the story — their actual production code path awaits
    `_prewarm_special_sessions_for_exchange()` FIRST, from the exact same
    async context, before ever calling the sync function. This proves
    that sequence genuinely closes the gap (reaches the real DB-shaped
    data), not just the mitigation."""
    import asyncio
    import backend.brokers.broker_apis as _ba
    import backend.api.persistence.write_queue as _wq
    from backend.api.algo.agent_engine import _prewarm_special_sessions_for_exchange

    loop = asyncio.get_running_loop()
    orig_main_loop = _wq._main_loop
    _ba._SPECIAL_SESSION_CACHE.clear()
    fake_rows = [{"date": date(2026, 11, 1), "start": dtime(18, 0), "end": dtime(19, 0)}]
    try:
        _wq._main_loop = loop  # simulate "already on the main loop"

        # Baseline: the guard-triggered call still returns [] (confirms
        # we really are in the deadlock-risk shape this test targets).
        assert _REAL_FETCH_SPECIAL_SESSIONS("MCX") == []

        # The fix: await the prewarm from this same async context —
        # exactly what the 3 real call sites now do before calling the
        # sync _build_now_ctx(). This goes through _read_special_sessions_async
        # directly (no run_on_main_loop hop), so the guard never applies.
        with patch.object(_ba, "_read_special_sessions_async", new=AsyncMock(return_value=fake_rows)):
            await _prewarm_special_sessions_for_exchange("MCX")

        # Now the sync call (as _ae_segment_flags/_build_now_ctx would
        # make it) must see the REAL pre-warmed rows, not the guard default.
        assert _REAL_FETCH_SPECIAL_SESSIONS("MCX") == fake_rows, (
            "the prewarm must leave the cache warm enough that the "
            "subsequent sync call sees the real data, closing the gap "
            "for this call-site shape"
        )
    finally:
        _wq._main_loop = orig_main_loop
        _ba._SPECIAL_SESSION_CACHE.clear()


# ---------------------------------------------------------------------------
# D3 Step 3 — ticket-placement 409 message mentions the evening-session
# override for segments configured with evening_open_on_holidays=True.
# ---------------------------------------------------------------------------


def test_ticket_closed_market_detail_mentions_evening_session_for_mcx():
    from backend.api.routes.orders_place import _ticket_closed_market_detail

    msg = _ticket_closed_market_detail("MCX", "CRUDEOILM26NOVFUT")
    assert "MCX" in msg
    assert "evening" in msg.lower(), f"MCX detail should mention its evening session: {msg}"


def test_ticket_closed_market_detail_equity_has_no_evening_mention():
    from backend.api.routes.orders_place import _ticket_closed_market_detail

    msg = _ticket_closed_market_detail("NSE", "RELIANCE")
    assert "NSE" in msg
    assert "evening" not in msg.lower(), f"NSE (no evening override) should not mention it: {msg}"


def test_ticket_closed_market_detail_stays_reasonably_short():
    from backend.api.routes.orders_place import _ticket_closed_market_detail

    msg = _ticket_closed_market_detail("MCX", "CRUDEOILM26NOVFUT")
    assert len(msg) < 200, f"detail message should stay short, got {len(msg)} chars: {msg}"


# ---------------------------------------------------------------------------
# D5 (2026-10-20) — template_attach.py's `_check_offhours_wing_gate` call
# site, in its only (async) caller `_apply_template_to_order_impl`, now also
# pre-warms the special-sessions cache before the gate's sync
# `_build_now_ctx()` call — mirrors
# `test_prewarm_closes_the_gap_for_the_3_genuinely_async_call_sites` for
# this one remaining real order-adjacent call site. After this fix, every
# genuine order/template/chase path is closed; only the never-exposed
# `apply_plan_live` (runs via `asyncio.to_thread`, confirmed by
# `write_queue.py`'s guard) and display/dry-run paths remain on the
# warm-at-startup mitigation only.
# ---------------------------------------------------------------------------

def test_apply_template_to_order_impl_prewarms_before_offhours_gate():
    """Source-order assertion: the prewarm call must appear textually
    BEFORE the `_check_offhours_wing_gate(` call inside
    `_apply_template_to_order_impl`'s body — calling it after would be a
    no-op (the gate's own sync `_build_now_ctx()` call would already have
    run and failed open)."""
    import inspect
    from backend.api.algo import template_attach as _ta

    src = inspect.getsource(_ta._apply_template_to_order_impl)
    prewarm_idx = src.find("_prewarm_special_sessions_for_exchange(parent_exchange)")
    gate_idx = src.find("_check_offhours_wing_gate(")
    assert prewarm_idx != -1, "prewarm call not found in _apply_template_to_order_impl"
    assert gate_idx != -1, "_check_offhours_wing_gate call not found"
    assert prewarm_idx < gate_idx, (
        "prewarm must run BEFORE the offhours wing gate's own sync "
        "_build_now_ctx() call, or it closes the gap too late"
    )


@pytest.mark.asyncio
async def test_offhours_wing_gate_prewarm_closes_the_gap():
    """2026-10-20 fix, same shape as
    test_prewarm_closes_the_gap_for_the_3_genuinely_async_call_sites but
    for `_check_offhours_wing_gate` specifically: a DB-confirmed
    zero-width (always-closed) MCX special-session override is invisible
    to a cold cache hit from a coroutine already on the main loop —
    `_check_offhours_wing_gate` wrongly concludes the exchange is open and
    lets a wing-bearing template proceed. Awaiting
    `_prewarm_special_sessions_for_exchange` first (exactly what
    `_apply_template_to_order_impl` now does immediately before calling
    the gate) closes the gap: the gate correctly defers the wing attach.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch
    import backend.brokers.broker_apis as _ba
    import backend.api.persistence.write_queue as _wq
    from backend.api.algo.agent_engine import _prewarm_special_sessions_for_exchange
    from backend.api.algo.template_attach import _check_offhours_wing_gate

    template_with_wing = {"wing_strike_offset": 0}
    closed_all_day_row = [{
        "date": _FIXTURE_NOW.date(),
        "start": dtime(0, 0),
        "end": dtime(0, 0),   # zero-width — half-open [start, end) never matches
    }]

    loop = asyncio.get_running_loop()
    orig_main_loop = _wq._main_loop
    _ba._SPECIAL_SESSION_CACHE.clear()
    try:
        with patch.object(_ba, "fetch_special_sessions", side_effect=_REAL_FETCH_SPECIAL_SESSIONS), \
             patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=_FIXTURE_NOW):
            _wq._main_loop = loop  # simulate "already on the main loop"

            # ── Pre-fix shape: calling the gate WITHOUT the prewarm first —
            # cold cache + on-main-loop fails open to [], override invisible,
            # gate (wrongly) concludes MCX is open. _FIXTURE_NOW's date IS a
            # declared MCX holiday per this file's autouse
            # `_fake_fetch_holidays` fixture (`_MCX_HOLIDAY_DATE ==
            # _FIXTURE_NOW.date()`) — with the special-session override
            # invisible, `is_market_open()` falls through to its
            # `evening_open_on_holidays=True` holiday-evening carve-out
            # (18:00 >= 17:00, weekday), which reads MCX as open. The
            # override, once visible, supersedes that carve-out entirely —
            # this is exactly the scenario the fix closes.
            with patch.object(_ba, "_read_special_sessions_async",
                               AsyncMock(return_value=closed_all_day_row)):
                early_no_prewarm, _ = _check_offhours_wing_gate(
                    "live", "MCX", template_with_wing, 1, "ZG0001",
                    "CRUDEOILM26NOVFUT", "BUY", 1, 100.0, None,
                )
            assert early_no_prewarm is None, (
                "sanity check: without the prewarm, the cold cache must "
                "fail open and the gate must NOT see the override"
            )

            # ── The fix: await the prewarm from this same async context,
            # exactly as _apply_template_to_order_impl now does, immediately
            # before calling the gate.
            _ba._SPECIAL_SESSION_CACHE.clear()
            with patch.object(_ba, "_read_special_sessions_async",
                               AsyncMock(return_value=closed_all_day_row)):
                await _prewarm_special_sessions_for_exchange("MCX")

            early_with_prewarm, offhours_note = _check_offhours_wing_gate(
                "live", "MCX", template_with_wing, 1, "ZG0001",
                "CRUDEOILM26NOVFUT", "BUY", 1, 100.0, None,
            )
            assert early_with_prewarm is not None, (
                "the prewarm must let the gate see the real DB override "
                "and defer the wing-bearing attach — gap not closed"
            )
    finally:
        _wq._main_loop = orig_main_loop
        _ba._SPECIAL_SESSION_CACHE.clear()


# ---------------------------------------------------------------------------
# 4-site residual sweep (2026-10-20) — the 4 sites CLAUDE.md's "Accepted
# residual, not fixed" bullet previously listed: routes/agents.py's
# dry-run, routes/watchlist.py's movers gate, routes/quote.py's segment
# check, routes/simulator.py's guards. All 4 confirmed genuinely exposed
# (each reached from a coroutine already on the main loop, no
# asyncio.to_thread in between) and fixed with the same prewarm pattern
# established above.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Item 1 — routes/agents.py's `_dry_run_context`. `async def`, calls the
# sync `_build_context(now)` INLINE — this coroutine IS the main loop.
# `_build_context` fans out over every configured `market_segments` entry
# via `_ae_segment_flags()`, which calls the sync `fetch_special_sessions()`
# per segment. A dry-run doesn't know in advance which single exchange it
# needs (it builds context for every segment at once), so the fix awaits
# the full-dataset `warm_special_sessions_cache()` once at the top, before
# `_build_context` runs.
# ---------------------------------------------------------------------------

def test_dry_run_context_prewarms_before_build_context():
    """Source-order assertion: the prewarm call must appear textually
    BEFORE the `_build_context(` call inside `_dry_run_context`'s body —
    calling it after would be a no-op."""
    import inspect
    from backend.api.routes import agents as agents_mod

    src = inspect.getsource(agents_mod._dry_run_context)
    prewarm_idx = src.find("await broker_apis.warm_special_sessions_cache()")
    build_ctx_idx = src.find("base = _build_context(now)")
    assert prewarm_idx != -1, "prewarm call not found in _dry_run_context"
    assert build_ctx_idx != -1, "_build_context(now) call not found"
    assert prewarm_idx < build_ctx_idx, (
        "prewarm must run BEFORE _build_context(now), or it closes the "
        "gap too late"
    )


@pytest.mark.asyncio
async def test_dry_run_context_prewarm_closes_special_session_gap_for_mcx():
    """DB-confirmed zero-width (always-closed) MCX special-session
    override, on a coroutine already on the main loop with a cold cache:
    without the fix, `_build_context(now)`'s sync
    `fetch_special_sessions()` call fails open to `[]`, the override is
    invisible, and `_ae_segment_flags` falls through to the
    evening-open-on-holiday carve-out (`mcx_open=True`, wrong). The fix —
    awaiting `warm_special_sessions_cache()` first — makes the override
    visible, so `mcx_open` correctly reads `False`."""
    import asyncio
    import pandas as pd
    import backend.brokers.broker_apis as _ba
    import backend.api.persistence.write_queue as _wq
    from backend.api.routes import agents as agents_mod

    closed_all_day_row = [{
        "date": _FIXTURE_NOW.date(),
        "start": dtime(0, 0),
        "end": dtime(0, 0),   # zero-width — half-open [start, end) never matches
    }]
    empty_df_pair = (pd.DataFrame(), pd.DataFrame())

    loop = asyncio.get_running_loop()
    orig_main_loop = _wq._main_loop
    _ba._SPECIAL_SESSION_CACHE.clear()
    try:
        with patch.object(_ba, "fetch_special_sessions", side_effect=_REAL_FETCH_SPECIAL_SESSIONS), \
             patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=_FIXTURE_NOW), \
             patch("backend.api.background._fetch_holdings_direct", return_value=empty_df_pair), \
             patch("backend.api.background._fetch_positions_direct", return_value=empty_df_pair), \
             patch("backend.brokers.broker_apis.fetch_margins", return_value=[]), \
             patch.object(_ba, "_read_special_sessions_async",
                           AsyncMock(return_value=closed_all_day_row)):
            _wq._main_loop = loop  # simulate "already on the main loop"
            ctx = await agents_mod._dry_run_context(_FIXTURE_NOW)
        assert ctx["mcx_open"] is False, (
            f"the prewarm must let _build_context see the real DB "
            f"override — ctx={ctx}"
        )
    finally:
        _wq._main_loop = orig_main_loop
        _ba._SPECIAL_SESSION_CACHE.clear()

