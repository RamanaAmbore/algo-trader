"""
Item 1 (2026-10-22) — pin LTP to prev_close until each exchange segment's
real session opens.

Live incident: at 08:47 IST (past the system's broad 08:00 broker-
connectivity window but before MCX's real 09:00 session start), MCX GOLD
option legs already carried a live `last_price` diverging from
`prev_close`, producing a phantom Day P&L move purely from the gap between
the broad window and each segment's real trading-session hours.

Covers:
  - `snapshot_gate.is_segment_pending_open` — the exchange-to-segment
    matching + boundary semantics.
  - `positions.py:_pin_pending_open_rows` / `_apply_pending_open_tag`.
  - `holdings.py:_pin_pending_open_holdings` / `_apply_pending_open_tag_holdings`.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pandas as pd
import pytest

IST = ZoneInfo("Asia/Kolkata")


def _fake_holidays(exchange: str) -> set:
    return set()


def _fake_special_sessions(exchange: str) -> list:
    return []


def _patch_calendar():
    """Patch the holiday/special-session sources at their origin module so
    every call site (snapshot_gate → date_time_utils._segment_is_open →
    broker_apis.fetch_holidays/fetch_special_sessions) sees a clean,
    deterministic calendar."""
    return (
        patch("backend.brokers.broker_apis.fetch_holidays", side_effect=_fake_holidays),
        patch("backend.brokers.broker_apis.fetch_special_sessions", side_effect=_fake_special_sessions),
    )


class TestIsSegmentPendingOpen:
    def test_mcx_pending_before_0900(self):
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 10, 19, 8, 47, tzinfo=IST)  # Monday
        p1, p2 = _patch_calendar()
        with p1, p2:
            assert is_segment_pending_open("MCX", now) is True

    def test_mcx_open_at_exactly_0900(self):
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 10, 19, 9, 0, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2:
            assert is_segment_pending_open("MCX", now) is False

    def test_mcx_still_pending_one_minute_before_open(self):
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 10, 19, 8, 59, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2:
            assert is_segment_pending_open("MCX", now) is True

    def test_nse_open_at_exactly_0915(self):
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 10, 19, 9, 15, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2:
            assert is_segment_pending_open("NSE", now) is False

    def test_nfo_matches_equity_segment(self):
        """NFO (options) must match the equity segment's exchange list,
        not fall through to fail-open."""
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 10, 19, 8, 47, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2:
            assert is_segment_pending_open("NFO", now) is True

    def test_well_into_session_not_pending(self):
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 10, 19, 11, 0, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2:
            assert is_segment_pending_open("MCX", now) is False
            assert is_segment_pending_open("NSE", now) is False

    def test_unknown_exchange_fails_open(self):
        """An exchange matching no configured segment is never pinned."""
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 10, 19, 8, 47, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2:
            assert is_segment_pending_open("ZZZ", now) is False

    def test_holiday_calendar_respected_not_bypassed(self):
        """A real holiday on the matched exchange must still read closed
        (via `_segment_is_open`) — the pending-open check must not bypass
        holiday/special-session correctness."""
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 10, 19, 11, 0, tzinfo=IST)  # well into session
        holiday_date = now.date()
        with patch(
            "backend.brokers.broker_apis.fetch_holidays",
            side_effect=lambda exch: {holiday_date} if exch.upper() == "MCX" else set(),
        ), patch(
            "backend.brokers.broker_apis.fetch_special_sessions", side_effect=_fake_special_sessions,
        ):
            # MCX has evening_open_on_holidays=True but it's 11:00, not
            # past 17:00 — so a holiday keeps it "not open" -> pending.
            assert is_segment_pending_open("MCX", now) is True
            assert is_segment_pending_open("NSE", now) is False


class TestPositionsPendingOpenPin:
    def _row(self, **overrides) -> dict:
        base = dict(
            account="ZG0790", tradingsymbol="GOLD26OCT142000PE", exchange="MCX",
            last_price=439.0, prev_close=420.0, quantity=10, overnight_quantity=10,
            average_price=400.0, realised=0.0, pnl=390.0,
            day_change_val=190.0, day_change=19.0, day_change_percentage=4.5,
            pnl_percentage=9.75,
        )
        base.update(overrides)
        return base

    def test_pin_applied_when_segment_pending(self):
        from backend.api.routes.positions import (
            _resolve_pending_open_exchanges, _pin_pending_open_rows,
        )
        df = pd.DataFrame([self._row()])
        now = datetime(2026, 10, 19, 8, 47, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2, patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now,
        ):
            pending = _resolve_pending_open_exchanges(df)
            assert pending == {"MCX"}
            _pin_pending_open_rows(df, pending)

        assert df.at[0, "last_price"] == pytest.approx(420.0)
        # unrealised = (420 - 400) * 10 = 200; pnl = realised(0) + 200 = 200
        assert df.at[0, "pnl"] == pytest.approx(200.0)
        assert df.at[0, "unrealised"] if "unrealised" in df.columns else True
        assert df.at[0, "day_change_val"] == pytest.approx(0.0)
        assert df.at[0, "day_change"] == pytest.approx(0.0)
        assert df.at[0, "day_change_percentage"] == pytest.approx(0.0)

    def test_no_pin_once_session_genuinely_open(self):
        from backend.api.routes.positions import (
            _resolve_pending_open_exchanges, _pin_pending_open_rows,
        )
        df = pd.DataFrame([self._row()])
        now = datetime(2026, 10, 19, 11, 0, tzinfo=IST)  # well past 09:00
        p1, p2 = _patch_calendar()
        with p1, p2, patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now,
        ):
            pending = _resolve_pending_open_exchanges(df)
            assert pending == set()
            _pin_pending_open_rows(df, pending)

        # Untouched — original live values survive.
        assert df.at[0, "last_price"] == pytest.approx(439.0)
        assert df.at[0, "pnl"] == pytest.approx(390.0)
        assert df.at[0, "day_change_val"] == pytest.approx(190.0)

    def test_boundary_at_exact_open_time_not_pinned(self):
        from backend.api.routes.positions import (
            _resolve_pending_open_exchanges, _pin_pending_open_rows,
        )
        df = pd.DataFrame([self._row()])
        now = datetime(2026, 10, 19, 9, 0, tzinfo=IST)  # exactly MCX open
        p1, p2 = _patch_calendar()
        with p1, p2, patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now,
        ):
            pending = _resolve_pending_open_exchanges(df)
            assert pending == set()
            _pin_pending_open_rows(df, pending)
        assert df.at[0, "last_price"] == pytest.approx(439.0)

    def test_no_pin_without_resolved_prev_close(self):
        """A row with prev_close==0 (no prior-session snapshot) is left
        untouched even on a pending exchange — nothing to pin to."""
        from backend.api.routes.positions import (
            _resolve_pending_open_exchanges, _pin_pending_open_rows,
        )
        df = pd.DataFrame([self._row(prev_close=0.0)])
        now = datetime(2026, 10, 19, 8, 47, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2, patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now,
        ):
            pending = _resolve_pending_open_exchanges(df)
            _pin_pending_open_rows(df, pending)
        assert df.at[0, "last_price"] == pytest.approx(439.0)
        assert df.at[0, "day_change_val"] == pytest.approx(190.0)

    def test_holiday_correctness_preserved_not_bypassed(self):
        """A real NSE holiday must still read closed via `_segment_is_open`
        — the pin logic must not short-circuit holiday/special-session
        correctness."""
        from backend.api.routes.positions import (
            _resolve_pending_open_exchanges, _pin_pending_open_rows,
        )
        df = pd.DataFrame([self._row(exchange="NFO")])
        now = datetime(2026, 10, 19, 11, 0, tzinfo=IST)  # well into session
        holiday_date = now.date()
        with patch(
            "backend.brokers.broker_apis.fetch_holidays",
            side_effect=lambda exch: {holiday_date} if exch.upper() == "NSE" else set(),
        ), patch(
            "backend.brokers.broker_apis.fetch_special_sessions", side_effect=_fake_special_sessions,
        ), patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now,
        ):
            pending = _resolve_pending_open_exchanges(df)
            # equity has evening_open_on_holidays=False -> on a holiday,
            # _segment_is_open returns False (closed) regardless of
            # in-window time -> pending_open pin DOES apply here, which is
            # the correct behaviour (equity never opens on a holiday).
            assert pending == {"NFO"}
            _pin_pending_open_rows(df, pending)
        assert df.at[0, "last_price"] == pytest.approx(420.0)
        assert df.at[0, "day_change_val"] == pytest.approx(0.0)

    def test_apply_pending_open_tag_overrides_live_to_pending_open(self):
        from backend.api.routes.positions import _apply_pending_open_tag
        from backend.api.schemas import PositionRow

        row = PositionRow(
            account="ZG0790", tradingsymbol="GOLD26OCT142000PE", exchange="MCX",
            product="NRML", quantity=10, average_price=400.0, pnl=200.0,
            last_price=420.0, prev_close=420.0,
            price_source="live", current_price=420.0, is_animating=True,
        )
        now = datetime(2026, 10, 19, 8, 47, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2, patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now,
        ):
            out = _apply_pending_open_tag([row])
        assert out[0].price_source == "pending_open"
        assert out[0].is_animating is False
        assert out[0].current_price == pytest.approx(420.0)

    def test_apply_pending_open_tag_leaves_genuinely_live_rows_alone(self):
        from backend.api.routes.positions import _apply_pending_open_tag
        from backend.api.schemas import PositionRow

        row = PositionRow(
            account="ZG0790", tradingsymbol="GOLD26OCT142000PE", exchange="MCX",
            product="NRML", quantity=10, average_price=400.0, pnl=390.0,
            last_price=439.0, prev_close=420.0,
            price_source="live", current_price=439.0, is_animating=True,
        )
        now = datetime(2026, 10, 19, 11, 0, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2, patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now,
        ):
            out = _apply_pending_open_tag([row])
        assert out[0].price_source == "live"
        assert out[0].is_animating is True


class TestHoldingsPendingOpenPin:
    def _row(self, **overrides) -> dict:
        base = dict(
            account="ZG0790", tradingsymbol="GOLDBEES", exchange="NSE",
            last_price=55.0, prev_close=52.0, quantity=100,
            average_price=50.0, inv_val=5000.0, cur_val=5500.0,
            pnl=500.0, pnl_percentage=10.0,
            day_change_val=300.0, day_change=3.0, day_change_percentage=6.0,
        )
        base.update(overrides)
        return base

    def test_pin_applied_when_segment_pending(self):
        from backend.api.routes.holdings import _pin_pending_open_holdings
        df = pd.DataFrame([self._row()])
        now = datetime(2026, 10, 19, 8, 47, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2, patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now,
        ):
            _pin_pending_open_holdings(df)

        assert df.at[0, "last_price"] == pytest.approx(52.0)
        assert df.at[0, "cur_val"] == pytest.approx(5200.0)
        assert df.at[0, "pnl"] == pytest.approx(200.0)
        assert df.at[0, "day_change_val"] == pytest.approx(0.0)

    def test_no_pin_once_session_genuinely_open(self):
        from backend.api.routes.holdings import _pin_pending_open_holdings
        df = pd.DataFrame([self._row()])
        now = datetime(2026, 10, 19, 11, 0, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2, patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now,
        ):
            _pin_pending_open_holdings(df)
        assert df.at[0, "last_price"] == pytest.approx(55.0)
        assert df.at[0, "day_change_val"] == pytest.approx(300.0)

    def test_apply_pending_open_tag_holdings(self):
        from backend.api.routes.holdings import _apply_pending_open_tag_holdings
        from backend.api.schemas import HoldingRow

        row = HoldingRow(
            account="ZG0790", tradingsymbol="GOLDBEES", exchange="NSE",
            quantity=100, average_price=50.0, inv_val=5000.0, cur_val=5200.0,
            pnl=200.0, pnl_percentage=4.0, last_price=52.0, prev_close=52.0,
            price_source="live", current_price=52.0, is_animating=True,
        )
        now = datetime(2026, 10, 19, 8, 47, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2, patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now,
        ):
            out = _apply_pending_open_tag_holdings([row])
        assert out[0].price_source == "pending_open"
        assert out[0].is_animating is False


class TestPendingOpenPinIncidentReplication:
    """Replicates the exact live incident: GOLD26OCT142000PE,
    last_price=439 vs prev_close=420, at 08:47 IST on MCX before its real
    09:00 session start — confirmed via a real `/api/positions` pull
    (`price_source: "live"`). Asserts the pin reaches the REAL summary
    rollup (`_build_polars_summary`, the NavStrip P badge's data source),
    not just the per-row fields — `_build_polars_summary` discards the
    per-row `day_change_val` entirely and recomputes it from
    `realised + unrealised - prev_settlement_pnl`
    (`_with_baseline_diff_day_change`), so the pin's correctness hinges on
    `unrealised` being pinned consistently with `prev_settlement_pnl`, not
    on `day_change_val` being set to 0 directly.
    """

    def test_incident_row_reads_zero_through_the_real_summary_rollup(self):
        import polars as pl
        from backend.api.routes.positions import (
            _resolve_pending_open_exchanges, _pin_pending_open_rows,
            _build_polars_summary,
        )

        avg_price = 400.0
        qty = 10
        prev_close = 420.0
        live_ltp = 439.0  # the incident's diverging live price
        realised = 0.0
        # prev_settlement_pnl is yesterday's frozen total profit — for an
        # unchanged overnight position this is exactly
        # (prev_close - avg) * qty + realised, by construction.
        prev_settlement_pnl = (prev_close - avg_price) * qty + realised

        row = dict(
            account="ZG0790", tradingsymbol="GOLD26OCT142000PE", exchange="MCX",
            last_price=live_ltp, prev_close=prev_close, quantity=qty,
            overnight_quantity=qty, average_price=avg_price,
            realised=realised, unrealised=(live_ltp - avg_price) * qty,
            pnl=realised + (live_ltp - avg_price) * qty,
            prev_settlement_pnl=prev_settlement_pnl,
            day_change_val=(live_ltp - prev_close) * qty,  # the phantom move
            day_change=(live_ltp - prev_close), day_change_percentage=4.52,
            pnl_percentage=9.75,
        )
        df = pd.DataFrame([row])

        now = datetime(2026, 10, 22, 8, 47, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2, patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now,
        ):
            pending = _resolve_pending_open_exchanges(df)
            assert pending == {"MCX"}
            _pin_pending_open_rows(df, pending)

        # Per-row: last_price pinned, no phantom day move.
        assert df.at[0, "last_price"] == pytest.approx(prev_close)
        assert df.at[0, "day_change_val"] == pytest.approx(0.0)

        # The REAL summary path (NavStrip P badge SSOT) — day_change_val
        # here is independently recomputed from realised+unrealised-
        # prev_settlement_pnl, discarding the per-row value set above.
        pl_df = pl.from_pandas(df)
        summary = _build_polars_summary(pl_df)
        total_row = summary.filter(pl.col("account") == "TOTAL")
        assert total_row["day_change_val"][0] == pytest.approx(0.0), (
            "NavStrip P badge rollup must read 0 for a pending_open row — "
            "the exact -7k phantom-loss incident this item fixes"
        )
        acct_row = summary.filter(pl.col("account") == "ZG0790")
        assert acct_row["day_change_val"][0] == pytest.approx(0.0)


class TestIsSegmentPendingOpenAfterClose:
    """2026-11-27 live incident: account ZG0790's GOLD expiry-day book
    showed Exp P&L +39,400 against a real ~-6,89,550/-5,64,500/-6,03,900
    book because `is_segment_pending_open` returned True AFTER MCX's own
    23:55 (winter DST) close too — `not _segment_is_open(...)` is True
    both before today's open AND after today's close, with no
    distinction. The fix adds a lower bound: pending-open only applies
    before today's own resolved opening time, never after it.

    Confirmed live via the `positions: pending_open pin applied to
    21/21 rows (exchanges=['MCX'])` log line firing at 18:26:09 UTC
    (23:56:09 IST) on prod, well after MCX's real close."""

    def test_mcx_not_pending_right_after_winter_close(self):
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        # Friday 2026-11-27, winter DST window — real MCX close is 23:55,
        # not the summer-DST 23:30 (see CLAUDE.md "MCX DST-aware non-agri
        # close time"). 23:56 is the operator's own reported screenshot
        # time, 1 minute after close.
        now = datetime(2026, 11, 27, 23, 56, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2:
            assert is_segment_pending_open("MCX", now) is False

    def test_mcx_not_pending_well_after_close_past_midnight_gap(self):
        """Any point in the broad 08:00-23:31 connectivity window AFTER
        the segment's own session has already run today must read False
        — not just the exact minute after close."""
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 11, 27, 23, 25, tzinfo=IST)  # just before the
        # system's own 23:31 broker-disconnect, still well after MCX's
        # real intraday session has been running since 09:00.
        p1, p2 = _patch_calendar()
        with p1, p2:
            assert is_segment_pending_open("MCX", now) is False

    def test_nse_not_pending_after_1530_close(self):
        """The same post-close bug applied to equity/NFO too — this is
        not an MCX-only fix."""
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 11, 27, 15, 45, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2:
            assert is_segment_pending_open("NSE", now) is False
            assert is_segment_pending_open("NFO", now) is False

    def test_mcx_still_pending_before_real_0900_open_unaffected(self):
        """The ORIGINAL pre-open case this mechanism exists for is
        untouched by the lower-bound fix."""
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 11, 27, 8, 47, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2:
            assert is_segment_pending_open("MCX", now) is True

    def test_mcx_pending_true_all_day_on_a_plain_holiday(self):
        """A plain holiday (no evening session) never opens today at
        all — pending stays True the whole day (same semantics as the
        pre-existing `test_holiday_correctness_preserved_not_bypassed`
        equity case above), not flipped to False by the new lower bound."""
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 11, 27, 23, 0, tzinfo=IST)  # late in the day
        holiday_date = now.date()
        with patch(
            "backend.brokers.broker_apis.fetch_holidays",
            side_effect=lambda exch: {holiday_date} if exch.upper() == "NSE" else set(),
        ), patch(
            "backend.brokers.broker_apis.fetch_special_sessions", side_effect=_fake_special_sessions,
        ):
            assert is_segment_pending_open("NFO", now) is True

    def test_mcx_evening_holiday_not_pending_after_evening_close(self):
        """An MCX evening-open holiday (evening_open_on_holidays=True)
        opens at 17:00 and still closes at its normal evening close time
        — pending must go False after THAT close too, not stay True all
        night the way the pre-fix code would have."""
        from backend.api.helpers.snapshot_gate import is_segment_pending_open
        now = datetime(2026, 11, 27, 23, 56, tzinfo=IST)
        holiday_date = now.date()
        with patch(
            "backend.brokers.broker_apis.fetch_holidays",
            side_effect=lambda exch: {holiday_date} if exch.upper() == "MCX" else set(),
        ), patch(
            "backend.brokers.broker_apis.fetch_special_sessions", side_effect=_fake_special_sessions,
        ):
            assert is_segment_pending_open("MCX", now) is False


class TestPostCloseExpiryPinRegression:
    """Direct reproduction of the 2026-11-27 GOLD expiry-night incident
    using two of the real six ZG0790 GOLD26NOV* legs (confirmed via a
    read-only prod `daily_book`/live-fetch-shape query): a leg closed
    TODAY (qty=0, Kite's own `realised` field stuck at 0, real lifetime
    `pnl`=115,550) and a leg still open today (qty=-300, real avg_cost
    846.6667, real mark pnl=-123,100). Before the fix, BOTH were
    re-pinned to yesterday's (2026-11-26) settlement at 23:56 IST,
    erasing the closed leg's entire realised gain and re-marking the
    open leg at yesterday's close instead of today's real exit/mark.
    """

    def _closed_leg(self) -> dict:
        return dict(
            account="ZG0790", tradingsymbol="GOLD26NOV123000CE", exchange="MCX",
            quantity=0, overnight_quantity=-100, average_price=0.0,
            last_price=1650.0, prev_close=713.0,  # Nov-26 EOD ltp (yesterday's settlement)
            realised=0.0, pnl=115550.0,
            day_change_val=0.0, day_change=0.0, day_change_percentage=0.0,
            pnl_percentage=0.0,
        )

    def _open_leg(self) -> dict:
        return dict(
            account="ZG0790", tradingsymbol="GOLD26NOV124000CE", exchange="MCX",
            quantity=-300, overnight_quantity=0, average_price=846.6666666666666,
            last_price=1257.0, prev_close=435.5,  # Nov-26 EOD ltp (yesterday's settlement)
            realised=0.0, pnl=-123100.0,
            day_change_val=208.5, day_change=208.5, day_change_percentage=0.0,
            pnl_percentage=0.0,
        )

    def test_post_close_no_longer_pinned_realised_pnl_survives(self):
        from backend.api.routes.positions import (
            _resolve_pending_open_exchanges, _pin_pending_open_rows,
        )
        df = pd.DataFrame([self._closed_leg(), self._open_leg()])
        now = datetime(2026, 11, 27, 23, 56, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2, patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now,
        ):
            pending = _resolve_pending_open_exchanges(df)
            assert pending == set(), (
                "MCX must not be reported pending-open 1 minute after its "
                "own winter-DST 23:55 close"
            )
            _pin_pending_open_rows(df, pending)

        closed = df.iloc[0]
        open_ = df.iloc[1]
        # Untouched — the closed leg's real realised gain survives, the
        # open leg's real mark-to-market survives.
        assert closed["pnl"] == pytest.approx(115550.0)
        assert closed["last_price"] == pytest.approx(1650.0)
        assert open_["pnl"] == pytest.approx(-123100.0)
        assert open_["last_price"] == pytest.approx(1257.0)

    def test_pre_fix_behaviour_would_have_erased_the_realised_gain(self):
        """Documents the bug this fix closes: replaying the EXACT same
        `is_segment_pending_open` predicate the pre-fix code used (`not
        _segment_is_open(...)`, no lower bound) against this fixture
        reproduces the exact pnl=0 / pnl=+1,23,350 corruption."""
        from backend.api.routes.positions import _pin_pending_open_rows
        from backend.shared.helpers.date_time_utils import _segment_is_open

        df = pd.DataFrame([self._closed_leg(), self._open_leg()])
        now = datetime(2026, 11, 27, 23, 56, tzinfo=IST)
        p1, p2 = _patch_calendar()
        with p1, p2:
            from backend.shared.helpers.utils import config as _cfg
            seg_cfg = next(
                s for s in _cfg.get("market_segments", {}).values()
                if "MCX" in {e.upper() for e in (s.get("exchanges") or [s.get("holiday_exchange", "")])}
                or (s.get("holiday_exchange") or "").upper() == "MCX"
            )
            pre_fix_pending = not _segment_is_open(seg_cfg, now)
            assert pre_fix_pending is True  # the bug's own trigger condition
            _pin_pending_open_rows(df, {"MCX"})

        closed = df.iloc[0]
        open_ = df.iloc[1]
        # The exact corruption: realised gain erased to 0, open leg
        # re-marked at yesterday's close.
        assert closed["pnl"] == pytest.approx(0.0)
        assert open_["pnl"] == pytest.approx((435.5 - 846.6666666666666) * -300)
