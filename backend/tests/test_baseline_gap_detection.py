"""Tests for the baseline-gap detection fix (2026-11 Day P&L audit).

Bug: `_fetch_baseline_pnl_map` / `_fetch_snapshot_close_map` / the
`_BASELINE_PNL_CTE_SQL`-backed baseline lookup returns no entry for an
(account, symbol) whenever the most-recent anchored `daily_book` batch has
no matching row — every downstream caller then treats the missing key as
`base_pnl = 0`. That is CORRECT for a genuinely new position (opened today,
no prior session could have a baseline) and WRONG when the position was
already open before today but `daily_book` simply has a gap for it (the
snapshot writer missed a day, or never wrote a `kind='positions'`/
`'holdings'` row for this exact symbol at all) — in the gap case,
`day_pnl = current_total_profit - 0` silently renders the position's
entire LIFETIME P&L as if it were TODAY's P&L, with no visible indicator.

Fix: `pnl_math.is_baseline_gap(base_pnl, overnight_qty)` distinguishes the
two cases using the broker's own `overnight_quantity` signal (confirmed
non-zero overnight carry-over with no resolved baseline = a snapshot-writer
gap; zero/absent = genuinely new or genuinely-closed-then-reopened, not a
gap). `PositionRow.baseline_stale` carries this flag through both the
live-fetch path (`positions.py:_backfill_prev_settlement_pnl`) and the
closed-hours snapshot path (`positions_helpers.py:build_snapshot_position_row`
/ `build_row_from_snapshot_raw`).

Covers:
  1. `is_baseline_gap` — the pure predicate, every combination.
  2. Live-fetch path — gap case (overnight_quantity != 0, no snapshot match)
     vs genuinely-new-position case (unchanged, no flag).
  3. Closed-hours snapshot path — same two cases via
     `build_snapshot_position_row` / `build_row_from_snapshot_raw`.
  4. `PositionRow.baseline_stale` schema default + msgspec round-trip.
  5. `_fetch_baseline_pnl_map` itself is unchanged (still dead code with no
     production caller — this pins that its own existing behaviour/tests
     are untouched by this fix).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backend.api.algo.pnl_math import is_baseline_gap

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# 1. is_baseline_gap — pure predicate
# ---------------------------------------------------------------------------

class TestIsBaselineGap:
    def test_gap_case_flagged(self):
        """base_pnl unresolved (None) but broker confirms a non-zero
        overnight carry-over — a genuine daily_book snapshot-writer gap."""
        assert is_baseline_gap(None, 10) is True
        assert is_baseline_gap(None, -5) is True

    def test_genuinely_new_position_not_flagged(self):
        """base_pnl unresolved AND no overnight carry-over — a genuinely
        new position opened today. Must NOT be flagged."""
        assert is_baseline_gap(None, 0) is False
        assert is_baseline_gap(None, None) is False

    def test_resolved_base_pnl_never_flagged_regardless_of_overnight_qty(self):
        """Any resolved (non-None) base_pnl — including a legitimate 0.0 —
        is never a gap, whatever overnight_qty says."""
        assert is_baseline_gap(0.0, 10) is False
        assert is_baseline_gap(500.0, 10) is False
        assert is_baseline_gap(-250.0, 0) is False

    def test_closed_then_reentered_position_not_flagged(self):
        """A position that was fully closed (qty=0, hence overnight_qty=0
        by construction for a fresh re-entry) and re-entered today — base_pnl
        unresolved, overnight_qty=0 — correctly stays unflagged (this is
        the deliberate `qty != 0` baseline-exclusion business rule, not a
        writer gap)."""
        assert is_baseline_gap(None, 0) is False

    def test_unparseable_overnight_qty_is_safe(self):
        assert is_baseline_gap(None, "not-a-number") is False
        assert is_baseline_gap(None, object()) is False


# ---------------------------------------------------------------------------
# 2. Live-fetch path — _backfill_prev_settlement_pnl (positions.py)
# ---------------------------------------------------------------------------

def _make_position_row(
    account="ZG0790",
    symbol="CRUDEOIL26OCT8700CE",
    overnight_quantity=10,
    quantity=10,
    product="NRML",
    day_sell_quantity=0,
):
    return {
        "account": account,
        "tradingsymbol": symbol,
        "product": product,
        "quantity": quantity,
        "overnight_quantity": overnight_quantity,
        "day_sell_quantity": day_sell_quantity,
    }


class TestBackfillPrevSettlementPnlLivePath:
    """`_backfill_prev_settlement_pnl` is the live-fetch write site for both
    `prev_settlement_pnl` and the new `baseline_stale` flag."""

    def test_gap_case_flagged_stale_day_pnl_not_lifetime_pnl(self):
        """Position carried a non-zero overnight quantity (broker confirms
        it was open before today — representing real daily_book history on
        an earlier day, e.g. day N-3) but the anchored batch query found no
        matching baseline row for it (the gap — e.g. a missed day N-1/N
        snapshot). Must be flagged `baseline_stale=True`, and
        `prev_settlement_pnl` must stay None (never silently substituted
        with a wrong non-zero guess) — the caller-level day_pnl formula
        (`pnl_math.baseline_diff_day_pnl_with_fallback`) would otherwise
        render this row's full lifetime pnl as today's day_pnl.
        """
        from backend.api.routes.positions import _backfill_prev_settlement_pnl
        from backend.api.algo.pnl_math import baseline_diff_day_pnl_with_fallback

        raw = pd.DataFrame([_make_position_row(
            symbol="CRUDEOIL26OCT8700CE", overnight_quantity=10, quantity=10,
        )])
        # No entry in prev_pnl_map for this (account, symbol) — the batch-
        # anchored query found nothing (the gap).
        _backfill_prev_settlement_pnl(raw, prev_pnl_map={})

        assert bool(raw.at[0, "baseline_stale"]) is True
        assert pd.isna(raw.at[0, "prev_settlement_pnl"]) or raw.at[0, "prev_settlement_pnl"] is None

        # NEGATIVE CONTROL — this asserts the RAW, un-guarded primitive's
        # behaviour on a base_pnl=0 input, to demonstrate the bug this flag
        # exists to let callers avoid. It is NOT an assertion that the
        # system's real day_pnl output is 48250.0 — every real caller
        # (`positions_helpers._row_baseline_diff_day_pnl`,
        # `positions._with_baseline_diff_day_change`, and the frontend's
        # `nav.js:baseDayPnlForPosition`) checks `baseline_stale` FIRST and
        # short-circuits to 0.0 before ever calling this primitive with
        # base_pnl=0 — see
        # TestAggregateRollupsExcludeGapRowContribution below for the
        # guarded, real-path assertions.
        lifetime_pnl = 48250.0
        day_pnl = baseline_diff_day_pnl_with_fallback(
            realised=0.0, unrealised=lifetime_pnl, pnl=lifetime_pnl, base_pnl=0.0,
        )
        assert day_pnl == lifetime_pnl, (
            "sanity check: the raw primitive has no guard of its own — "
            "confirming why every real caller must check baseline_stale "
            "before calling it with a defaulted base_pnl=0"
        )

    def test_genuinely_new_position_unchanged_no_flag(self):
        """A brand-new position (overnight_quantity=0, no prior-session
        baseline) must behave EXACTLY as before this fix: base_pnl stays
        None (not substituted), and baseline_stale is False."""
        from backend.api.routes.positions import _backfill_prev_settlement_pnl

        raw = pd.DataFrame([_make_position_row(
            symbol="FRESH_TODAY", overnight_quantity=0, quantity=25,
        )])
        _backfill_prev_settlement_pnl(raw, prev_pnl_map={})

        assert bool(raw.at[0, "baseline_stale"]) is False
        assert pd.isna(raw.at[0, "prev_settlement_pnl"]) or raw.at[0, "prev_settlement_pnl"] is None

    def test_resolved_baseline_never_flagged(self):
        """When the batch-anchored query DOES find a matching baseline row,
        prev_settlement_pnl is set and baseline_stale stays False — even
        though overnight_quantity is non-zero."""
        from backend.api.routes.positions import _backfill_prev_settlement_pnl

        raw = pd.DataFrame([_make_position_row(
            account="ZG0790", symbol="GOLDM26AUGFUT", overnight_quantity=5, quantity=5,
        )])
        _backfill_prev_settlement_pnl(
            raw, prev_pnl_map={("ZG0790", "GOLDM26AUGFUT"): 1250.0},
        )

        assert raw.at[0, "prev_settlement_pnl"] == 1250.0
        assert bool(raw.at[0, "baseline_stale"]) is False

    def test_empty_prev_pnl_map_still_computes_flag(self):
        """Previously `_backfill_prev_settlement_pnl` early-returned
        unconditionally when prev_pnl_map was empty, so baseline_stale was
        never computed for a totally-empty map — exactly the scenario most
        likely to hide a systemic snapshot-writer gap. Must now still flag
        a genuinely-carried-over row."""
        from backend.api.routes.positions import _backfill_prev_settlement_pnl

        raw = pd.DataFrame([_make_position_row(overnight_quantity=3, quantity=3)])
        _backfill_prev_settlement_pnl(raw, prev_pnl_map={})
        assert bool(raw.at[0, "baseline_stale"]) is True

    def test_no_overnight_quantity_column_never_flags(self):
        """A caller/fixture without an overnight_quantity column at all must
        not raise and must never set baseline_stale=True (no signal to
        judge by — defaults to 'not a gap')."""
        from backend.api.routes.positions import _backfill_prev_settlement_pnl

        raw = pd.DataFrame([{"account": "ZG0790", "tradingsymbol": "X", "product": "NRML"}])
        _backfill_prev_settlement_pnl(raw, prev_pnl_map={})
        assert bool(raw.at[0, "baseline_stale"]) is False

    def test_empty_dataframe_no_op(self):
        from backend.api.routes.positions import _backfill_prev_settlement_pnl

        raw = pd.DataFrame()
        _backfill_prev_settlement_pnl(raw, prev_pnl_map={})
        assert raw.empty


# ---------------------------------------------------------------------------
# 3. Closed-hours snapshot path — positions_helpers.py
# ---------------------------------------------------------------------------

class TestBuildSnapshotPositionRowBaselineGap:
    def test_gap_case_flagged(self):
        from backend.api.routes.positions_helpers import build_snapshot_position_row

        row = build_snapshot_position_row(
            account="ZG0790", symbol="CRUDEOIL26OCT8700CE", exchange="MCX",
            qty=10, avg_cost=8600.0, ltp=8750.0,
            day_pnl=None, total_pnl=1500.0, extras={},
            previous_close=8700.0,
            prev_settlement_pnl=None,  # no match found in the anchored batch
            overnight_quantity=10,     # broker confirms carried-over qty
        )
        assert row.baseline_stale is True
        assert row.prev_settlement_pnl is None

    def test_genuinely_new_position_not_flagged(self):
        from backend.api.routes.positions_helpers import build_snapshot_position_row

        row = build_snapshot_position_row(
            account="ZG0790", symbol="FRESH_TODAY", exchange="MCX",
            qty=5, avg_cost=100.0, ltp=110.0,
            day_pnl=None, total_pnl=50.0, extras={},
            previous_close=None,
            prev_settlement_pnl=None,
            overnight_quantity=0,
        )
        assert row.baseline_stale is False

    def test_resolved_baseline_not_flagged(self):
        from backend.api.routes.positions_helpers import build_snapshot_position_row

        row = build_snapshot_position_row(
            account="ZG0790", symbol="GOLDM26AUGFUT", exchange="MCX",
            qty=5, avg_cost=6700.0, ltp=6800.0,
            day_pnl=None, total_pnl=500.0, extras={},
            previous_close=6750.0,
            prev_settlement_pnl=250.0,
            overnight_quantity=5,
        )
        assert row.baseline_stale is False

    def test_omitted_overnight_quantity_defaults_safe(self):
        """Existing call sites/tests that don't pass overnight_quantity at
        all must never get a false-positive flag — this is the backward-
        compat guarantee for the many pre-existing tests of this function
        that don't know about this new parameter."""
        from backend.api.routes.positions_helpers import build_snapshot_position_row

        row = build_snapshot_position_row(
            account="ZG0790", symbol="LEGACY_CALLER", exchange="NFO",
            qty=10, avg_cost=100.0, ltp=110.0,
            day_pnl=None, total_pnl=100.0, extras={},
            prev_settlement_pnl=None,
            # overnight_quantity omitted entirely
        )
        assert row.baseline_stale is False


class TestBuildRowFromSnapshotRawBaselineGap:
    """`build_row_from_snapshot_raw` is the real `_positions_snapshot`
    (closed-hours reader) call site — it always derives overnight_quantity
    from the displayed daily_book row's own payload_json via
    `_parse_overnight_qty`, so this exercises the real production wiring."""

    def _raw_row(self, *, prev_settlement_pnl, overnight_quantity, qty=10):
        import json
        payload_json = json.dumps({"overnight_quantity": overnight_quantity})
        return (
            "ZG0790", "CRUDEOIL26OCT8700CE", "MCX", qty, 8600.0, 8750.0,
            None, 1500.0, payload_json, None, 8700.0,
            None, prev_settlement_pnl,
        )

    def test_gap_case_flagged(self):
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

        row = build_row_from_snapshot_raw(
            self._raw_row(prev_settlement_pnl=None, overnight_quantity=10)
        )
        assert row.baseline_stale is True
        assert row.prev_settlement_pnl is None

    def test_genuinely_new_position_not_flagged(self):
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

        row = build_row_from_snapshot_raw(
            self._raw_row(prev_settlement_pnl=None, overnight_quantity=0)
        )
        assert row.baseline_stale is False

    def test_resolved_baseline_not_flagged(self):
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw

        row = build_row_from_snapshot_raw(
            self._raw_row(prev_settlement_pnl=250.0, overnight_quantity=10)
        )
        assert row.baseline_stale is False
        assert row.prev_settlement_pnl == 250.0


# ---------------------------------------------------------------------------
# 4. PositionRow schema
# ---------------------------------------------------------------------------

class TestPositionRowBaselineStaleSchema:
    def test_defaults_to_false(self):
        from backend.api.schemas import PositionRow

        row = PositionRow(
            account="ZG0790", tradingsymbol="X", exchange="MCX", product="NRML",
            quantity=1, average_price=1.0, pnl=0.0,
        )
        assert row.baseline_stale is False

    def test_msgspec_round_trip(self):
        from backend.api.schemas import PositionRow
        import msgspec

        row = PositionRow(
            account="ZG0790", tradingsymbol="X", exchange="MCX", product="NRML",
            quantity=1, average_price=1.0, pnl=0.0, baseline_stale=True,
        )
        encoder = msgspec.json.Encoder()
        decoder = msgspec.json.Decoder(type=PositionRow)
        decoded = decoder.decode(encoder.encode(row))
        assert decoded.baseline_stale is True


# ---------------------------------------------------------------------------
# 5. _fetch_baseline_pnl_map unchanged (still unused dead code — see report)
# ---------------------------------------------------------------------------

class TestFetchBaselinePnlMapUnchanged:
    """`_fetch_baseline_pnl_map` has zero production callers (confirmed via
    grep across backend/api, backend/brokers, backend/shared — only its own
    definition and test file reference it). The real production baseline
    lookups are `_fetch_snapshot_close_map` (live-fetch) and the inline
    `_BASELINE_PNL_CTE_SQL` query inside `_positions_snapshot` (closed-
    hours), both covered above via their actual write sites. This test
    pins that `_fetch_baseline_pnl_map`'s own pre-existing contract
    (dict[tuple, float], unchanged) was intentionally left untouched."""

    def test_return_shape_unchanged(self):
        from backend.api.routes.positions import _fetch_baseline_pnl_map

        cutoff = datetime(2026, 9, 23, 8, 0, 0, tzinfo=IST)
        mock_result = MagicMock()
        mock_result.all.return_value = [("TEST001", "RELIANCE", 500.0)]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        with patch("backend.api.database.async_session", return_value=mock_session):
            out = asyncio.run(_fetch_baseline_pnl_map(cutoff))
        assert out == {("TEST001", "RELIANCE"): 500.0}


# ---------------------------------------------------------------------------
# 6. Aggregate rollups must not re-introduce the lifetime-pnl leak
#    (`_row_baseline_diff_day_pnl` / `build_summary_from_rows` /
#    `_build_polars_summary` / `_with_baseline_diff_day_change`)
# ---------------------------------------------------------------------------
#
# A per-row `baseline_stale` flag alone is not enough: `PositionsResponse
# .summary` (closed-hours path, and the live-fetch polars account/symbol
# rollup) is built independently of the frontend's row-level
# `baseDayPnlForPosition` and would otherwise STILL sum the row's full
# `current_total_profit - 0` into the account/TOTAL Day P&L, silently
# disagreeing with the (now-correct) per-row display.

class TestAggregateRollupsExcludeGapRowContribution:
    def test_row_baseline_diff_day_pnl_short_circuits_to_zero(self):
        from backend.api.routes.positions_helpers import _row_baseline_diff_day_pnl
        from backend.api.schemas import PositionRow

        row = PositionRow(
            account="ZG0790", tradingsymbol="CRUDEOIL26OCT8700CE", exchange="MCX",
            product="NRML", quantity=10, average_price=8600.0,
            pnl=48250.0, realised=0.0, unrealised=48250.0,
            prev_settlement_pnl=None, baseline_stale=True,
        )
        assert _row_baseline_diff_day_pnl(row) == 0.0

    def test_row_baseline_diff_day_pnl_unchanged_for_new_position(self):
        from backend.api.routes.positions_helpers import _row_baseline_diff_day_pnl
        from backend.api.schemas import PositionRow

        row = PositionRow(
            account="ZG0790", tradingsymbol="FRESH_TODAY", exchange="MCX",
            product="NRML", quantity=5, average_price=100.0,
            pnl=1000.0, realised=0.0, unrealised=1000.0,
            prev_settlement_pnl=None, baseline_stale=False,
        )
        assert _row_baseline_diff_day_pnl(row) == 1000.0

    def test_build_summary_from_rows_excludes_gap_row_lifetime_pnl(self):
        """The account + TOTAL summary's day_change_val must reflect only
        the genuinely-new row's contribution — never the gap row's
        48250.0 lifetime gain."""
        from backend.api.routes.positions_helpers import build_summary_from_rows
        from backend.api.schemas import PositionRow

        gap_row = PositionRow(
            account="ZG0790", tradingsymbol="CRUDEOIL26OCT8700CE", exchange="MCX",
            product="NRML", quantity=10, average_price=8600.0,
            pnl=48250.0, realised=0.0, unrealised=48250.0,
            prev_settlement_pnl=None, baseline_stale=True, prev_close=8700.0,
        )
        new_row = PositionRow(
            account="ZG0790", tradingsymbol="FRESH_TODAY", exchange="MCX",
            product="NRML", quantity=5, average_price=100.0,
            pnl=1000.0, realised=0.0, unrealised=1000.0,
            prev_settlement_pnl=None, baseline_stale=False, prev_close=0.0,
        )
        summary = build_summary_from_rows([gap_row, new_row])
        acct_row = next(s for s in summary if s.account == "ZG0790")
        total_row = next(s for s in summary if s.account == "TOTAL")

        assert acct_row.day_change_val == 1000.0, (
            "the gap row's lifetime pnl (48250.0) must NOT be summed into "
            "the account Day P&L rollup"
        )
        assert total_row.day_change_val == 1000.0

    def test_with_baseline_diff_day_change_polars_short_circuits_gap_rows(self):
        """The live-fetch polars summary path
        (_with_baseline_diff_day_change → _build_polars_summary) must agree
        with the row-level path above on the same underlying data."""
        import polars as pl
        from backend.api.routes.positions import _with_baseline_diff_day_change

        df = pl.DataFrame({
            "account": ["ZG0790", "ZG0790"],
            "tradingsymbol": ["CRUDEOIL26OCT8700CE", "FRESH_TODAY"],
            "realised": [0.0, 0.0],
            "unrealised": [48250.0, 1000.0],
            "pnl": [48250.0, 1000.0],
            "prev_settlement_pnl": [None, None],
            "baseline_stale": [True, False],
        })
        out = _with_baseline_diff_day_change(df)
        values = out["day_change_val"].to_list()
        assert values == [0.0, 1000.0], (
            "the gap row (baseline_stale=True) must contribute 0.0, not "
            "its full lifetime pnl (48250.0), to day_change_val"
        )

    def test_with_baseline_diff_day_change_no_baseline_stale_column_unchanged(self):
        """Callers/fixtures without a baseline_stale column (e.g. paper
        synthetic rows) must behave exactly as before this fix."""
        import polars as pl
        from backend.api.routes.positions import _with_baseline_diff_day_change

        df = pl.DataFrame({
            "realised": [0.0],
            "unrealised": [500.0],
            "pnl": [500.0],
            "prev_settlement_pnl": [None],
        })
        out = _with_baseline_diff_day_change(df)
        assert out["day_change_val"].to_list() == [500.0]

    def test_rebuild_positions_summary_excludes_gap_row_lifetime_pnl(self):
        """`background.py:_rebuild_positions_summary` feeds the performance-
        refresh `sum_positions` table — in turn read by NavStrip's P slot
        AND the `day_val`/`day_pct` grammar metrics that drive
        `loss-positions-total` (critical tier). A gap row's lifetime pnl
        must not leak into that account/TOTAL rollup either."""
        from backend.api.background import _rebuild_positions_summary

        raw = pd.DataFrame([
            {
                "account": "ZG0790", "realised": 0.0, "unrealised": 48250.0,
                "pnl": 48250.0, "prev_settlement_pnl": None,
                "baseline_stale": True, "prev_close": 8700.0, "quantity": 10,
            },
            {
                "account": "ZG0790", "realised": 0.0, "unrealised": 1000.0,
                "pnl": 1000.0, "prev_settlement_pnl": None,
                "baseline_stale": False, "prev_close": 0.0, "quantity": 5,
            },
        ])
        summary = _rebuild_positions_summary(raw)
        acct_total = summary.loc[summary["account"] == "ZG0790", "day_change_val"].iloc[0]
        assert acct_total == 1000.0, (
            "the gap row's lifetime pnl (48250.0) must not be summed into "
            "the performance-refresh day_change_val rollup"
        )

    def test_rebuild_positions_summary_no_baseline_stale_column_unchanged(self):
        """Callers without a baseline_stale column (pre-existing fixtures)
        must behave exactly as before this fix."""
        from backend.api.background import _rebuild_positions_summary

        raw = pd.DataFrame([
            {
                "account": "ZG0790", "realised": 0.0, "unrealised": 500.0,
                "pnl": 500.0, "prev_settlement_pnl": None,
                "prev_close": 0.0, "quantity": 5,
            },
        ])
        summary = _rebuild_positions_summary(raw)
        assert summary.loc[summary["account"] == "ZG0790", "day_change_val"].iloc[0] == 500.0
