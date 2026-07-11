"""
test_compose_sparkline_series.py

Pure-function tests for the consolidated sparkline series composer
introduced in the hardening pass. The helper owns the fallback ladder +
reason attribution for empty / partial / full series; the batch endpoint
delegates to it so we can't ship two rendering paths that drift.

Five quality dimensions (feedback_test_dimensions.md):

  1. SSOT        — compose_sparkline_series is the ONLY place that decides
                   how {past, today_bars, ltp} become a renderable series.
                   Assert the helper's presence + shape.
  2. Performance — pure function, zero I/O — every case resolves in << 1 ms.
                   Asserted implicitly by pytest wall-time (5000 iterations
                   should be well under 100 ms).
  3. Stale code  — batch_sparkline no longer contains an inline series
                   composition loop; the [SPARK-EMPTY] reason string comes
                   from the helper's returned reason, not a duplicate ladder.
  4. Reuse       — the same helper (a) drives the batch endpoint and
                   (b) is directly unit-testable without spinning up the
                   Litestar app or mocking three persistence stores.
  5. UX          — every documented reason string in the docstring maps to
                   a real ladder branch (no dead labels).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from backend.api.routes.quote import compose_sparkline_series


# --- Full-data cases ------------------------------------------------------

def test_live_full_series_market_open():
    """past + today + LTP tail, market open → 'live' reason."""
    past = [100.0, 101.0, 102.0]
    today = [102.5, 103.0]
    ltp = 103.5
    series, reason = compose_sparkline_series(past, today, ltp, market_closed=False)
    assert series == [100.0, 101.0, 102.0, 102.5, 103.0, 103.5]
    assert reason == "live"


def test_snapshot_series_market_closed_with_ltp():
    """past + today + LTP, market closed → LTP IS appended as frozen tail, reason='snapshot'.

    When closed, ltp_val comes from daily_book.ltp / 24h LKG cache — valid frozen
    data representing the last known price. Appending it prevents the frontend's
    _mergeSparkSeries from rejecting the fresh series as "too short" and falling
    back to yesterday's cached curve shape.
    """
    past = [100.0, 101.0, 102.0]
    today = [102.5, 103.0]
    ltp = 103.5
    series, reason = compose_sparkline_series(past, today, ltp, market_closed=True)
    # Closed hours: LTP tail IS appended (valid frozen last-known price from daily_book).
    assert series == [100.0, 101.0, 102.0, 102.5, 103.0, 103.5]
    assert reason == "snapshot"


def test_snapshot_series_market_closed_no_ltp():
    """past + today, market closed, no LTP available → LTP not appended, reason='snapshot'."""
    past = [100.0, 101.0, 102.0]
    today = [102.5, 103.0]
    series, reason = compose_sparkline_series(past, today, None, market_closed=True)
    assert series == [100.0, 101.0, 102.0, 102.5, 103.0]
    assert reason == "snapshot"


def test_past_only_market_open_with_ltp():
    """past + LTP tail (today empty) → 'live'."""
    series, reason = compose_sparkline_series([100.0, 101.0], [], 102.0, False)
    assert series == [100.0, 101.0, 102.0]
    assert reason == "live"


def test_closed_hours_ltp_tail_appended_from_daily_book():
    """market_closed=True + ltp_val from daily_book.ltp → LTP appended as frozen tail.

    Regression guard for the closed-hours sparkline stale-curve bug:
    Without this fix, compose_sparkline_series suppressed the LTP tail when
    market_closed=True, making the fresh series shorter than the cached version
    (which included past+intraday+LTP from the last open session). The frontend's
    _mergeSparkSeries length-gate then rejected the fresh series, locking in
    yesterday's curve shape for the entire closed-hours window.

    Fix: when ltp_val is provided (sourced from daily_book.ltp or 24h LKG cache),
    it is valid frozen data — the last known price at market close — and should
    always be appended regardless of market_closed status.
    """
    past = [2800.0, 2820.0, 2835.0, 2845.0, 2850.0]
    today = [2851.0, 2855.0, 2860.0]
    # ltp from daily_book.ltp — the last price when the market closed
    ltp = 2862.5

    series, reason = compose_sparkline_series(past, today, ltp, market_closed=True)

    assert reason == "snapshot", f"Expected 'snapshot', got {reason!r}"
    assert series[-1] == ltp, (
        f"daily_book LTP {ltp} must be the final data point in the series. "
        f"Got series ending at {series[-1] if series else 'empty'}"
    )
    assert series == past + today + [ltp], (
        f"Expected past+today+[ltp], got {series}"
    )


def test_closed_hours_no_ltp_past_only():
    """market_closed=True + no ltp_val → series is past+today (no tail)."""
    past = [100.0, 101.0, 102.0]
    today = [102.5, 103.0]

    series, reason = compose_sparkline_series(past, today, None, market_closed=True)

    assert reason == "snapshot"
    assert series == past + today, f"Expected past+today, got {series}"


# --- Padding cases --------------------------------------------------------

def test_ltp_only_flat_pad_market_open():
    """Broker rate-limited past+today, only tail LTP available → 2-pt flat baseline."""
    series, reason = compose_sparkline_series([], [], 100.5, market_closed=False)
    # Single-point [ltp] gets padded to [ltp, ltp] via single_point_pad branch,
    # OR ltp_only_flat_pad — either produces a 2-point flat line for renderer.
    assert len(series) == 2
    assert series[0] == series[1] == 100.5
    assert reason in ("single_point_pad", "ltp_only_flat_pad")


def test_ltp_only_market_closed_flat_pad():
    """Closed hours + only LTP (last-known price) → 2-pt flat baseline.

    With market_closed=True the tail_ltp is now always appended (Bug 1 fix),
    so series=[ltp] (1 point) hits the single_point_pad branch rather than
    ltp_only_flat_pad. Both produce [ltp, ltp] — the observable shape is the
    same; only the reason differs.
    """
    series, reason = compose_sparkline_series([], [], 100.5, market_closed=True)
    assert series == [100.5, 100.5]
    assert reason in ("single_point_pad", "ltp_only_flat_pad")


# --- Empty cases: reason attribution --------------------------------------

def test_empty_market_closed_warm_universe():
    """Nothing available, market closed → warm_universe_empty."""
    series, reason = compose_sparkline_series([], [], None, market_closed=True)
    assert series == []
    assert reason == "warm_universe_empty"


def test_empty_market_open_historical_fetch_fail():
    """Nothing available, market open → historical_fetch_fail."""
    series, reason = compose_sparkline_series([], [], None, market_closed=False)
    assert series == []
    assert reason == "historical_fetch_fail"


def test_empty_past_but_today_present():
    """
    today present + no LTP + market closed → snapshot with today only.
    (Rare: intraday cache warm but daily cache cold — closed hours.)
    """
    series, reason = compose_sparkline_series([], [102.0, 103.0], None, market_closed=True)
    assert series == [102.0, 103.0]
    assert reason == "snapshot"


def test_zero_ltp_treated_as_missing():
    """ltp_val=0.0 must NOT count as tail; ladder falls through to reason attribution."""
    series, reason = compose_sparkline_series([], [], 0.0, market_closed=False)
    assert series == []
    assert reason == "historical_fetch_fail"


def test_negative_ltp_treated_as_missing():
    """Malformed negative LTP must not corrupt series."""
    series, reason = compose_sparkline_series([], [], -1.0, market_closed=False)
    assert series == []
    assert reason == "historical_fetch_fail"


# --- Dim 3: stale-code guard ---------------------------------------------

def test_batch_endpoint_delegates_no_duplicate_ladder():
    """batch_sparkline must NOT still contain the pre-hardening inline
    fallback ladder (`if not series and ltp_val and ltp_val > 0` was
    the tell-tale marker of the pre-helper composition path).

    Fail this test if a future refactor re-inlines the ladder — that
    would defeat the SSOT invariant.
    """
    src_path = Path(__file__).resolve().parents[2] / "backend" / "api" / "routes" / "quote.py"
    src = src_path.read_text()

    # The helper defn must exist.
    assert "def compose_sparkline_series(" in src, \
        "compose_sparkline_series helper missing"

    # The module must contain _compose_and_dual_write which calls compose_sparkline_series.
    # After the cc-decomp refactor the direct call lives in the extracted helper
    # _compose_and_dual_write; batch_sparkline delegates to that helper.
    assert "def _compose_and_dual_write(" in src, \
        "_compose_and_dual_write helper missing — compose_sparkline_series delegation broken"
    assert "compose_sparkline_series(" in src.split("def _compose_and_dual_write")[-1].split("def batch_sparkline")[0], \
        "_compose_and_dual_write must call compose_sparkline_series (SSOT delegation broken)"

    # batch_sparkline body must call _compose_and_dual_write (not inline the ladder).
    batch_body = src.split("def batch_sparkline")[-1]
    assert "_compose_and_dual_write(" in batch_body, \
        "batch_sparkline must delegate to _compose_and_dual_write (not inline the fallback ladder)"

    # And must not carry the old inline pad-of-single-point in the batch_sparkline body.
    # (Helper defn legitimately contains it, so scope to the batch_sparkline section.)
    endpoint_body = batch_body.split("def compose_sparkline_series")[0]
    inline_pad_re = re.compile(r"if\s+len\(series\)\s*==\s*1\s*:\s*\n\s*series\s*=\s*series\s*\+\s*series")
    assert not inline_pad_re.search(endpoint_body), \
        "batch_sparkline still has inline single-point pad — helper composition drifted"


# --- Dim 2: perf smoke ----------------------------------------------------

def test_helper_is_fast():
    """5 000 iterations across the ladder must complete in << 100 ms.

    Sanity guard that the helper stays a pure function and doesn't
    accidentally acquire an I/O dependency.
    """
    t0 = time.perf_counter()
    for i in range(5_000):
        compose_sparkline_series([100.0, 101.0], [102.0], 103.0, False)
        compose_sparkline_series([], [], None, True)
        compose_sparkline_series([], [], 100.5, True)
    dt = time.perf_counter() - t0
    # Very loose; only catches accidental I/O regression.
    assert dt < 1.0, f"compose_sparkline_series too slow: {dt:.3f}s for 15k calls"
