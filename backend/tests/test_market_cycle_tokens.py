"""
Market-cycle event tokens (2026-10-22) — condition/metric grammar tokens
over `agent_engine._ae_segment_flags()`'s per-segment computed dict:
`is_mcx_open`, `is_nse_open`, `minutes_since_mcx_open`,
`minutes_since_nse_open`, `minutes_until_mcx_close`,
`minutes_until_nse_close`, `minutes_until_mcx_open`,
`minutes_until_nse_open`, `is_past_holiday_refresh`, `is_past_window_open`.

These are the first real condition-tree consumers of
`_ae_segment_flags()`'s dict — previously computed every tick but read by
no grammar token at all (see CLAUDE.md's "Segment flags honour
overrides" note). Three layers of coverage, per that note's own
"dormant, no live reader" callout — a resolver-only test would pass even
if the engine never actually threads the data through:

  1. `_ae_segment_flags` boundary tests — MCX's configured open time
     (09:00 IST) is read LIVE from `backend_config.yaml` via the
     module-level `config` dict (loaded once at process start, no
     separate caching layer — see `backend/shared/helpers/utils.py`),
     not a stale literal.
  2. Resolver-level true/false/missing tests on a hand-built `Context`.
  3. A `run_cycle` wiring test proving `context["segment_flags"]` is
     actually populated from `_build_context()`'s output — the gap this
     whole feature closes.
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")

# A plain weekday — not a holiday on NSE or MCX — isolates these tests
# from the evening-holiday branch entirely. Mirrors the sibling
# `_PLAIN_WEEKDAY` fixture convention in test_market_hours_unification.py.
_PLAIN_WEEKDAY = date(2026, 11, 5)  # Thursday


def _fake_fetch_holidays(exchange: str) -> set:
    return set()


def _fake_fetch_special_sessions(exchange: str) -> list:
    return []


@pytest.fixture(autouse=True)
def _patch_holiday_sources():
    with patch("backend.brokers.broker_apis.fetch_holidays", side_effect=_fake_fetch_holidays), \
         patch("backend.brokers.broker_apis.fetch_special_sessions", side_effect=_fake_fetch_special_sessions):
        yield


def _commodity_seg() -> dict:
    from backend.shared.helpers.utils import config as _cfg
    return (_cfg.get("market_segments", {}) or {}).get("commodity", {}) or {}


def _equity_seg() -> dict:
    from backend.shared.helpers.utils import config as _cfg
    return (_cfg.get("market_segments", {}) or {}).get("equity", {}) or {}


def _at(hh: int, mm: int) -> datetime:
    return datetime(_PLAIN_WEEKDAY.year, _PLAIN_WEEKDAY.month, _PLAIN_WEEKDAY.day,
                     hh, mm, tzinfo=IST)


# ---------------------------------------------------------------------------
# Layer 1 — `_ae_segment_flags` boundary tests (empirical 09:00 proof +
# the new minutes_until_* fields).
# ---------------------------------------------------------------------------

def test_mcx_still_closed_one_minute_before_the_configured_open_time():
    """Pre-flight verification target: MCX's configured open is 09:00 IST,
    read live from backend_config.yaml — not a stale literal. At 08:59
    MCX must read closed."""
    from backend.api.algo.agent_engine import _ae_segment_flags

    flags = _ae_segment_flags("commodity", _commodity_seg(), _at(8, 59))
    assert flags["mcx_open"] is False, flags
    assert flags["minutes_since_mcx_open"] == 0
    assert flags["minutes_until_mcx_close"] is None
    assert flags["minutes_until_mcx_open"] == 1, flags


def test_mcx_open_one_minute_after_the_configured_open_time():
    from backend.api.algo.agent_engine import _ae_segment_flags

    flags = _ae_segment_flags("commodity", _commodity_seg(), _at(9, 1))
    assert flags["mcx_open"] is True, flags
    assert flags["minutes_since_mcx_open"] == 1
    assert flags["minutes_until_mcx_open"] == 0
    # 23:30 close - 09:01 = 869 minutes.
    assert flags["minutes_until_mcx_close"] == 869, flags


def test_mcx_minutes_until_close_counts_down_while_open():
    from backend.api.algo.agent_engine import _ae_segment_flags

    flags = _ae_segment_flags("commodity", _commodity_seg(), _at(23, 0))
    assert flags["mcx_open"] is True
    assert flags["minutes_until_mcx_close"] == 30, flags


def test_mcx_minutes_until_open_and_close_are_none_after_todays_close():
    """After MCX's own close, there is no more "today" open to count
    down to — both minutes_until_* fields must be None, not a negative
    number or a coerced 0 masquerading as "closed forever"."""
    from backend.api.algo.agent_engine import _ae_segment_flags

    flags = _ae_segment_flags("commodity", _commodity_seg(), _at(23, 45))
    assert flags["mcx_open"] is False
    assert flags["minutes_until_mcx_open"] is None, flags
    assert flags["minutes_until_mcx_close"] is None, flags


def test_nse_segment_mirrors_mcx_boundary_shape_at_its_own_09_15():
    from backend.api.algo.agent_engine import _ae_segment_flags

    before = _ae_segment_flags("equity", _equity_seg(), _at(9, 14))
    after = _ae_segment_flags("equity", _equity_seg(), _at(9, 16))
    assert before["nse_open"] is False
    assert before["minutes_until_nse_open"] == 1
    assert after["nse_open"] is True
    assert after["minutes_until_nse_open"] == 0
    assert after["minutes_since_nse_open"] == 1


# ---------------------------------------------------------------------------
# Layer 2 — resolver-level true/false/missing tests on a hand-built
# Context (no DB, no agent_engine involved — isolates grammar.py's
# resolver logic from the wiring that populates ctx.segment_flags).
# ---------------------------------------------------------------------------

def _ctx(segment_flags: dict | None = None, now=None):
    from backend.api.algo.agent_evaluator import Context
    return Context(segment_flags=segment_flags or {}, now=now)


class TestIsOpenTokens:
    def test_is_mcx_open_true(self):
        from backend.api.algo.grammar import _metric_is_mcx_open
        assert _metric_is_mcx_open(_ctx({"mcx_open": True}), {}) == 1.0

    def test_is_mcx_open_false(self):
        from backend.api.algo.grammar import _metric_is_mcx_open
        assert _metric_is_mcx_open(_ctx({"mcx_open": False}), {}) == 0.0

    def test_is_mcx_open_missing_is_none(self):
        from backend.api.algo.grammar import _metric_is_mcx_open
        assert _metric_is_mcx_open(_ctx({}), {}) is None

    def test_is_nse_open_true(self):
        from backend.api.algo.grammar import _metric_is_nse_open
        assert _metric_is_nse_open(_ctx({"nse_open": True}), {}) == 1.0

    def test_is_nse_open_false(self):
        from backend.api.algo.grammar import _metric_is_nse_open
        assert _metric_is_nse_open(_ctx({"nse_open": False}), {}) == 0.0


class TestMinutesSinceOpenGating:
    """These resolvers must gate on the open flag rather than trusting
    the raw `minutes_since_*_open` field, which `_ae_segment_flags`
    coerces to 0 while closed (a diagnostic convention for its other
    callers) — ungated, that 0 would read as "just opened" all night."""

    def test_minutes_since_mcx_open_when_open(self):
        from backend.api.algo.grammar import _metric_minutes_since_mcx_open
        ctx = _ctx({"mcx_open": True, "minutes_since_mcx_open": 42})
        assert _metric_minutes_since_mcx_open(ctx, {}) == 42

    def test_minutes_since_mcx_open_when_closed_is_none_not_zero(self):
        from backend.api.algo.grammar import _metric_minutes_since_mcx_open
        ctx = _ctx({"mcx_open": False, "minutes_since_mcx_open": 0})
        assert _metric_minutes_since_mcx_open(ctx, {}) is None

    def test_minutes_since_nse_open_when_open(self):
        from backend.api.algo.grammar import _metric_minutes_since_nse_open
        ctx = _ctx({"nse_open": True, "minutes_since_nse_open": 5})
        assert _metric_minutes_since_nse_open(ctx, {}) == 5


class TestMinutesUntilCloseGating:
    def test_minutes_until_mcx_close_when_open(self):
        from backend.api.algo.grammar import _metric_minutes_until_mcx_close
        ctx = _ctx({"mcx_open": True, "minutes_until_mcx_close": 120})
        assert _metric_minutes_until_mcx_close(ctx, {}) == 120

    def test_minutes_until_mcx_close_when_closed_is_none(self):
        from backend.api.algo.grammar import _metric_minutes_until_mcx_close
        ctx = _ctx({"mcx_open": False, "minutes_until_mcx_close": None})
        assert _metric_minutes_until_mcx_close(ctx, {}) is None

    def test_minutes_until_nse_close_when_open(self):
        from backend.api.algo.grammar import _metric_minutes_until_nse_close
        ctx = _ctx({"nse_open": True, "minutes_until_nse_close": 15})
        assert _metric_minutes_until_nse_close(ctx, {}) == 15


class TestMinutesUntilOpen:
    def test_minutes_until_mcx_open_passthrough(self):
        from backend.api.algo.grammar import _metric_minutes_until_mcx_open
        ctx = _ctx({"minutes_until_mcx_open": 10})
        assert _metric_minutes_until_mcx_open(ctx, {}) == 10

    def test_minutes_until_mcx_open_zero_when_already_open(self):
        from backend.api.algo.grammar import _metric_minutes_until_mcx_open
        ctx = _ctx({"minutes_until_mcx_open": 0})
        assert _metric_minutes_until_mcx_open(ctx, {}) == 0

    def test_minutes_until_nse_open_none_when_no_session_today(self):
        from backend.api.algo.grammar import _metric_minutes_until_nse_open
        ctx = _ctx({"minutes_until_nse_open": None})
        assert _metric_minutes_until_nse_open(ctx, {}) is None


class TestIsPastHolidayRefresh:
    def test_before_boundary_is_false(self):
        from backend.api.algo.grammar import _metric_is_past_holiday_refresh
        ctx = _ctx(now=_at(5, 29))
        assert _metric_is_past_holiday_refresh(ctx, {}) == 0.0

    def test_after_boundary_is_true(self):
        from backend.api.algo.grammar import _metric_is_past_holiday_refresh
        ctx = _ctx(now=_at(5, 31))
        assert _metric_is_past_holiday_refresh(ctx, {}) == 1.0

    def test_at_exact_boundary_is_true(self):
        from backend.api.algo.grammar import _metric_is_past_holiday_refresh
        ctx = _ctx(now=_at(5, 30))
        assert _metric_is_past_holiday_refresh(ctx, {}) == 1.0

    def test_reads_configured_time_not_a_hardcoded_0530(self):
        """`holiday_refresh_time` is operator-tunable — the resolver must
        read it live, not hardcode 05:30."""
        from backend.api.algo.grammar import _metric_is_past_holiday_refresh
        from backend.shared.helpers.utils import config as _cfg
        with patch.dict(_cfg, {"holiday_refresh_time": "06:00"}):
            ctx = _ctx(now=_at(5, 45))
            assert _metric_is_past_holiday_refresh(ctx, {}) == 0.0
            ctx2 = _ctx(now=_at(6, 1))
            assert _metric_is_past_holiday_refresh(ctx2, {}) == 1.0

    def test_missing_now_is_none(self):
        from backend.api.algo.grammar import _metric_is_past_holiday_refresh
        assert _metric_is_past_holiday_refresh(_ctx(now=None), {}) is None


class TestIsPastWindowOpen:
    def test_before_0800_is_false(self):
        from backend.api.algo.grammar import _metric_is_past_window_open
        ctx = _ctx(now=_at(7, 59))
        assert _metric_is_past_window_open(ctx, {}) == 0.0

    def test_at_and_after_0800_is_true(self):
        from backend.api.algo.grammar import _metric_is_past_window_open
        ctx = _ctx(now=_at(8, 0))
        assert _metric_is_past_window_open(ctx, {}) == 1.0

    def test_late_evening_same_session_is_still_true(self):
        """23:50 IST is still TODAY's session (the closed overnight
        window doesn't start until 23:31) — must read True, proving the
        resolver uses the session-date convention rather than a bare
        `now.time() >= 08:00` check that would coincidentally also pass
        here but for the wrong reason."""
        from backend.api.algo.grammar import _metric_is_past_window_open
        ctx = _ctx(now=_at(23, 50))
        assert _metric_is_past_window_open(ctx, {}) == 1.0

    def test_after_midnight_before_next_0800_is_false(self):
        """00:30 IST the NEXT calendar day — still inside the closed
        overnight window, must read False. A bare `date()` compare
        against "today" would wrongly flip this True at midnight."""
        from backend.api.algo.grammar import _metric_is_past_window_open
        next_day = datetime(_PLAIN_WEEKDAY.year, _PLAIN_WEEKDAY.month,
                             _PLAIN_WEEKDAY.day + 1, 0, 30, tzinfo=IST)
        ctx = _ctx(now=next_day)
        assert _metric_is_past_window_open(ctx, {}) == 0.0

    def test_missing_now_is_none(self):
        from backend.api.algo.grammar import _metric_is_past_window_open
        assert _metric_is_past_window_open(_ctx(now=None), {}) is None


# ---------------------------------------------------------------------------
# Layer 3 — the actual wiring: `run_cycle` must populate
# `context["segment_flags"]` from `_build_context()`'s output. A
# resolver-only test (Layer 2) would stay green even if this line were
# deleted — this is the gap the feature closes (CLAUDE.md's "dormant, no
# live reader" callout).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_cycle_wires_segment_flags_into_context():
    from backend.api.algo import agent_engine as ae

    now = _at(10, 0)
    captured: dict = {}

    class _FakeAgent:
        id = 1

    async def _fake_load_agents(only_agent_ids):
        return [_FakeAgent()]

    async def _fake_process_agent(agent, **kwargs):
        captured["segment_flags"] = kwargs["context"].get("segment_flags")

    with patch.object(ae, "_v2_hydrate_latch", new=AsyncMock(return_value=None)), \
         patch.object(ae, "_cycle_load_agents", new=_fake_load_agents), \
         patch.object(ae, "_cycle_process_agent", new=_fake_process_agent):
        await ae.run_cycle({"now": now}, bypass_schedule=True)

    flags = captured.get("segment_flags")
    assert flags is not None, "context['segment_flags'] was never populated"
    assert "mcx_open" in flags and "nse_open" in flags
    assert "minutes_until_mcx_open" in flags
    assert "minutes_until_nse_close" in flags


class TestCycleEvaluateAgentThreadsSegmentFlags:
    """One level deeper than the `run_cycle` wiring test above:
    `_cycle_evaluate_agent` must pass `context['segment_flags']` through
    to the V2Context it builds, so a real condition tree using
    `is_mcx_open` resolves against live data end-to-end (not just that
    the dict key exists on `context`).

    REGISTRY is normally populated from the DB at app startup
    (`REGISTRY.reload()`) — wire the real grammar.py resolvers in
    directly via monkeypatch, same pattern as
    `test_agent_evaluator.py::TestAllJoinsSameAccount._wire_registry`.
    """

    @pytest.fixture(autouse=True)
    def _wire_registry(self, monkeypatch):
        from backend.api.algo.grammar_registry import REGISTRY
        from backend.api.algo.grammar import (
            _metric_is_mcx_open, _scope_funds_total, OPERATORS,
        )
        monkeypatch.setitem(REGISTRY.metrics, 'is_mcx_open', _metric_is_mcx_open)
        monkeypatch.setitem(REGISTRY.scopes, 'funds.total', _scope_funds_total)
        monkeypatch.setitem(REGISTRY.operators, '==', OPERATORS['=='])

    def test_is_mcx_open_leaf_fires_through_full_wiring(self):
        from backend.api.algo import agent_engine as ae
        import pandas as pd

        now = _at(10, 0)  # MCX open on this plain weekday fixture
        flags = ae._ae_segment_flags("commodity", _commodity_seg(), now)
        context = {
            "now": now,
            "segment_flags": flags,
            "sum_holdings": None, "sum_positions": None,
            "df_margins": pd.DataFrame([{"account": "TOTAL"}]),
        }
        cfg = {"rate_window_min": 10, "baseline_offset_min": 15}

        class _FakeAgent:
            id = 1
            slug = "test-mcx-open-probe"
            conditions = {"metric": "is_mcx_open", "scope": "funds.total",
                          "op": "==", "value": 1.0}
            given = None

        matches, _obs = ae._cycle_evaluate_agent(
            _FakeAgent(), context, cfg, now, alert_state={},
            bypass_schedule=True,
        )
        assert matches, (
            "is_mcx_open leaf did not fire even though MCX is open at "
            "10:00 on this fixture — segment_flags wiring is broken"
        )

    def test_is_mcx_open_leaf_does_not_fire_when_closed(self):
        from backend.api.algo import agent_engine as ae
        import pandas as pd

        now = _at(23, 45)  # MCX closed on this plain weekday fixture
        flags = ae._ae_segment_flags("commodity", _commodity_seg(), now)
        context = {
            "now": now,
            "segment_flags": flags,
            "sum_holdings": None, "sum_positions": None,
            "df_margins": pd.DataFrame([{"account": "TOTAL"}]),
        }
        cfg = {"rate_window_min": 10, "baseline_offset_min": 15}

        class _FakeAgent:
            id = 1
            slug = "test-mcx-open-probe"
            conditions = {"metric": "is_mcx_open", "scope": "funds.total",
                          "op": "==", "value": 1.0}
            given = None

        matches, _obs = ae._cycle_evaluate_agent(
            _FakeAgent(), context, cfg, now, alert_state={},
            bypass_schedule=True,
        )
        assert matches == []
