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
