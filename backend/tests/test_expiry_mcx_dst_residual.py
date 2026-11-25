"""
test_expiry_mcx_dst_residual.py — two residual MCX-DST-season hardcodes
found inside `backend/api/algo/expiry.py` after the base
`mcx_non_agri_close_time()` fix shipped (see CLAUDE.md "MCX DST-aware
non-agri close time"):

  1. `ExpiryEngine._compute_theta()` hardcoded `close_time = (23, 30)`
     for every MCX position regardless of season. During the winter
     (US-standard-time) window, MCX's real close is 23:55 IST — theta
     collapsed to 0 up to 25 minutes before the real close, corrupting
     `_net_commodity_group()`'s greedy theta-priority netting order at
     exactly the moment the `expiry-day-itm-auto-close` agent fires.

  2. `ExpiryEngine._parse_segment_close_times()` read the commodity
     segment's legacy YAML `hours_end` field directly — a static
     literal that always says "23:30", bypassing the DST-aware
     resolution entirely.

Both reference dates (winter / DST) match the convention established in
`test_mcx_non_agri_close_time.py` and `test_order_gate_mcx_dst_aware.py`.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from backend.api.algo.expiry import ExpiryEngine, OptionPosition
from backend.api.algo.derivatives import mcx_non_agri_close_time

_IST = ZoneInfo("Asia/Kolkata")

_WINTER_DATE = date(2026, 11, 17)   # real MCX non-agri close 23:55 IST
_DST_DATE = date(2026, 6, 1)        # real MCX non-agri close 23:30 IST


# ── 1. _compute_theta — per-position theta used by the greedy netting ──


def _mcx_pos(expiry: date) -> OptionPosition:
    return OptionPosition(
        account="test_account",
        tradingsymbol="CRUDEOIL26NOV5500CE",
        exchange="MCX",
        instrument_type="CE",
        underlying="CRUDEOIL",
        strike=5500.0,
        expiry=expiry,
        quantity=1,
        product="NRML",
        underlying_ltp=5500.0,
    )


def test_compute_theta_winter_window_resolves_close_via_mcx_non_agri_close_time():
    """At 23:37 IST on the real winter-window expiry date, theta must
    still reflect real time value (T > 0, real close 23:55) — not
    collapse to 0 as the old hardcoded (23, 30) would (T<=0 at 23:31)."""
    engine = ExpiryEngine()
    pos = _mcx_pos(_WINTER_DATE)
    ref = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 37, tzinfo=_IST)

    theta = engine._compute_theta(pos, ref=ref)

    assert theta != 0.0, (
        "at 23:37 IST on the real 2026-11-17 winter-window expiry, theta "
        "must be non-zero (18 real minutes remain before the true 23:55 "
        "close) — the old hardcoded 23:30 close would already report T<=0"
    )


def test_compute_theta_winter_window_sanity_check_old_hardcode_would_be_zero():
    """Sanity check — not a strawman: feeding the OLD hardcoded (23, 30)
    close into the same days_to_expiry() call really does report T<=0
    at the same instant, confirming this is a real regression the fix
    corrects."""
    from backend.api.algo.derivatives import days_to_expiry

    ref = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 37, tzinfo=_IST)
    stale_days = days_to_expiry(_WINTER_DATE, ref=ref, close_time=(23, 30))
    assert stale_days <= 0.0


def test_compute_theta_dst_window_close_unaffected():
    """During the DST window the real close is still 23:30 — theta at
    23:37 (7 minutes past the real close) must correctly read 0,
    confirming the fix is genuinely season-aware, not "always later"."""
    engine = ExpiryEngine()
    pos = _mcx_pos(_DST_DATE)
    ref = datetime(_DST_DATE.year, _DST_DATE.month, _DST_DATE.day, 23, 37, tzinfo=_IST)

    theta = engine._compute_theta(pos, ref=ref)

    assert theta == 0.0


def test_compute_theta_nfo_is_unaffected_by_mcx_resolver():
    """A non-MCX exchange must never call `mcx_non_agri_close_time()` at
    all — confirms no cross-exchange leakage from this fix."""
    from backend.api.algo import derivatives as deriv_mod

    engine = ExpiryEngine()
    pos = OptionPosition(
        account="test_account",
        tradingsymbol="NIFTY26NOV25000CE",
        exchange="NFO",
        instrument_type="CE",
        underlying="NIFTY",
        strike=25000.0,
        expiry=date.today() + timedelta(days=30),
        quantity=50,
        product="NRML",
        underlying_ltp=25000.0,
    )

    with patch.object(
        deriv_mod, "mcx_non_agri_close_time",
        side_effect=AssertionError("must not be called for a non-MCX position"),
    ):
        theta = engine._compute_theta(pos)

    assert theta != 0.0


# ── 2. _parse_segment_close_times — ExpiryEngine's own close-time read ──


def test_parse_segment_close_times_resolves_winter_mcx_close():
    engine = ExpiryEngine()
    equity_close, mcx_close = engine._parse_segment_close_times(_WINTER_DATE)
    assert mcx_close == dtime(23, 55), (
        "the commodity segment's resolved close must track the real "
        "winter-window MCX non-agri close (23:55), not the static "
        "legacy hours_end literal (23:30)"
    )
    assert equity_close == dtime(15, 30)


def test_parse_segment_close_times_resolves_dst_mcx_close():
    engine = ExpiryEngine()
    equity_close, mcx_close = engine._parse_segment_close_times(_DST_DATE)
    assert mcx_close == dtime(23, 30)
    assert equity_close == dtime(15, 30)


def test_parse_segment_close_times_equity_unaffected_by_mcx_seasonal_rule():
    """Equity/NSE must resolve the same close regardless of the MCX
    seasonal rule — confirms no cross-segment leakage."""
    engine = ExpiryEngine()
    _, winter_mcx = engine._parse_segment_close_times(_WINTER_DATE)
    equity_winter, _ = engine._parse_segment_close_times(_WINTER_DATE)
    equity_dst, _ = engine._parse_segment_close_times(_DST_DATE)
    assert equity_winter == equity_dst == dtime(15, 30)
    assert winter_mcx == dtime(23, 55)


def test_parse_segment_close_times_defaults_to_today_when_omitted():
    """Calling with no `today` argument must not raise — it resolves
    against the real IST clock (matching the original zero-arg call
    this method had before the fix)."""
    engine = ExpiryEngine()
    equity_close, mcx_close = engine._parse_segment_close_times()
    assert isinstance(equity_close, dtime)
    assert isinstance(mcx_close, dtime)


def test_parse_segment_close_times_falls_back_to_legacy_hours_end_without_sessions():
    """A hypothetical config with no `sessions` list at all (not the
    current production YAML shape, but a config this function must not
    mishandle) falls back to the legacy `hours_end` literal, matching
    the pre-fix default exactly."""
    engine = ExpiryEngine()
    fake_segments = {
        "equity": {"holiday_exchange": "NSE", "hours_end": "15:30"},
        "commodity": {"holiday_exchange": "MCX", "hours_end": "23:30"},
    }
    with patch(
        "backend.api.algo.expiry.config",
        {"market_segments": fake_segments},
    ):
        equity_close, mcx_close = engine._parse_segment_close_times(_WINTER_DATE)
    assert equity_close == dtime(15, 30)
    assert mcx_close == dtime(23, 30), (
        "with no sessions list at all, the resolver has nothing to "
        "resolve — must fall back to the static hours_end literal, "
        "exactly as the pre-fix code always did"
    )


def test_parse_segment_close_times_resolver_failure_falls_back_safely():
    """A resolver exception (e.g. a future zoneinfo/import failure) must
    never raise — it falls back to the raw static sessions list's own
    `end` value, same as `resolve_dst_aware_sessions()`'s own documented
    fail-safe behaviour."""
    engine = ExpiryEngine()
    with patch(
        "backend.api.algo.derivatives.mcx_non_agri_close_time",
        side_effect=RuntimeError("boom"),
    ):
        equity_close, mcx_close = engine._parse_segment_close_times(_WINTER_DATE)
    assert mcx_close == dtime(23, 30)
    assert equity_close == dtime(15, 30)


def test_close_positions_passes_todays_date_into_parse_segment_close_times():
    """`close_positions()` (the real, current caller of
    `_parse_segment_close_times()` — `run()` was removed as fully dead
    code, 2026-11-24, audit finding #15) must thread its own resolved
    `today` into it rather than letting it re-derive the date
    independently — confirmed via source inspection, since driving the
    whole `close_positions()` coroutine end-to-end needs a live
    scan/chase stack out of scope for this test."""
    import inspect
    src = inspect.getsource(ExpiryEngine.close_positions)
    assert "_parse_segment_close_times(today)" in src
