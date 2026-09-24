"""Regression tests for `_compute_firm_nav`'s off-hours fallback path
(2026-09 Day P&L audit round 3, item #3 + its follow-up).

Bug (item #3, first pass): `_compute_firm_nav`'s off-hours fallback
(`backend/api/routes/auth.py`, used whenever `_intraday_equity` is empty
— e.g. right after a server restart) summed `sum_p['day_change_val']`
straight from `_fetch_positions_direct`'s raw output, which is built via
the legacy `apply_day_change_backstop` formula, not the new baseline-diff
formula (`current_total_profit - base_pnl`, pnl-fallback-aware). This
feeds `firm_day_pnl`, a user-facing NAV/Day-P&L figure served at
`GET /api/auth/me` and the public investor NAV endpoints.

First-pass fix: apply `_override_stale_close_for_holdings` /
`_override_stale_close_from_snapshot` (backfills `prev_settlement_pnl`
from `daily_book.total_pnl`) then rebuild the summary via
`_rebuild_holdings_summary` / `_rebuild_positions_summary` before reading
`total_h`/`total_p` — the same convergence `_perf_fetch_all_broker_data` /
`_run_close_once` (background.py) already apply.

Follow-up bug: that fix still called a LIVE broker fetch unconditionally
whenever `_intraday_equity` is empty — including when the market is
FULLY CLOSED (weekend/holiday/overnight), not just "server just
restarted, market open, deque not filled yet". `_override_stale_close_
from_snapshot` → `_fetch_snapshot_close_map` anchors its baseline to a
FIXED `today_08` cutoff (today's wall-clock 08:00 IST) — correct only
while a live session is genuinely in progress. When the market is fully
closed, "today" isn't a live trading session, and the most recent
close-reset `daily_book` row's `captured_at` is always < `today_08`, so
it gets picked as BOTH "current" (via the live broker fetch, which
returns the same frozen prior-session state) AND "baseline" — the same
collision class as item #1, just on the live-fetch baseline path.

Fix: gate on `_any_segment_open()` (CANONICAL closed-hours pattern —
CLAUDE.md "Closed-hours route gate": never call the live broker when
closed). When closed, reuse the already-anchored closed-hours snapshot
readers (`_positions_snapshot` / `_holdings_snapshot` — item #1's fixed
captured_at-derived baseline anchor) instead of a live fetch.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest


def _positions_df_groww_style() -> pd.DataFrame:
    """A single Groww-style row: realised=0, unrealised=0 (not populated
    natively), pnl=500 (the combined broker field) — must trigger the
    pnl-fallback in the baseline-diff rebuild, NOT the legacy backstop."""
    return pd.DataFrame([{
        "account": "GROWWACC",
        "tradingsymbol": "TCS",
        "exchange": "NSE",
        "quantity": 10,
        "overnight_quantity": 10,
        "realised": 0.0,
        "unrealised": 0.0,
        "pnl": 500.0,
        "prev_close": 100.0,
        "close_price": 100.0,
        "last_price": 150.0,
        # legacy backstop-derived value — must NOT be what firm_day_pnl uses.
        "day_change_val": 999.0,
    }])


def _legacy_summary_df(day_change_val: float) -> pd.DataFrame:
    """Mimics what `_fetch_positions_direct` would return as its summary —
    built via the legacy `apply_day_change_backstop`. Used as the value
    the fix must NOT surface (in the live-session branch)."""
    return pd.DataFrame([{
        "account": "TOTAL", "pnl": 500.0, "day_change_val": day_change_val,
        "day_change_percentage": 0.0,
    }])


class TestAuthLiveSessionBaselineDiff:
    """`_intraday_equity` empty but a session is genuinely open (e.g. the
    narrow window right after a restart during market hours) — the
    live-fetch + baseline-diff-override path applies."""

    def test_live_session_firm_day_pnl_uses_baseline_diff_not_legacy_backstop(self):
        from backend.api.routes import auth as auth_mod

        # Reset the module-level NAV memo cache so this test isn't served
        # a stale cached value from an earlier test/run.
        auth_mod._NAV_CACHE.update(ts=0.0, value=None)

        df_p = _positions_df_groww_style()
        df_h = pd.DataFrame(columns=["account"])  # no holdings — isolates positions math
        legacy_sum_p = _legacy_summary_df(day_change_val=999.0)
        legacy_sum_h = pd.DataFrame(columns=["account", "day_change_val", "pnl"])

        async def _fake_override_positions(raw: pd.DataFrame) -> None:
            # Simulate the daily_book backfill: base_pnl (yesterday's
            # total_pnl) = 200.0 for this row.
            raw["prev_settlement_pnl"] = 200.0

        async def _fake_override_holdings(raw: pd.DataFrame) -> None:
            return None

        async def _fake_compute_firm_nav():
            return {"nav": 1_000_000.0}

        with (
            patch("backend.api.background._fetch_holdings_direct", return_value=(df_h, legacy_sum_h)),
            patch("backend.api.background._fetch_positions_direct", return_value=(df_p, legacy_sum_p)),
            patch("backend.api.background._intraday_equity", []),
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True),
            patch(
                "backend.api.routes.positions._override_stale_close_from_snapshot",
                side_effect=_fake_override_positions,
            ) as mock_override_p,
            patch(
                "backend.api.routes.holdings._override_stale_close_for_holdings",
                side_effect=_fake_override_holdings,
            ) as mock_override_h,
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_fake_compute_firm_nav),
        ):
            firm_nav, firm_day_pnl, firm_cum_pnl, as_of_iso = asyncio.run(
                auth_mod._compute_firm_nav()
            )

        # The stale-close override must actually have been invoked on the
        # positions/holdings DataFrames — proves the wiring landed, not
        # just a coincidentally-matching number.
        assert mock_override_p.await_count == 1
        assert mock_override_h.await_count == 1

        # Baseline-diff-with-fallback: realised=0, unrealised=0 (both
        # exactly 0) → falls back to pnl=500 as the realised leg →
        # current_total_profit = 500; base_pnl (prev_settlement_pnl) = 200
        # → day_pnl = 500 - 200 = 300.
        assert firm_day_pnl == pytest.approx(300.0), (
            f"firm_day_pnl must use the baseline-diff-with-fallback formula "
            f"(500 - 200 = 300), not the legacy apply_day_change_backstop "
            f"value (999) from the raw _fetch_positions_direct summary — "
            f"got {firm_day_pnl}"
        )
        assert firm_nav == pytest.approx(1_000_000.0)

    def test_live_session_override_failure_falls_back_gracefully(self):
        """If the DB-backed override raises (e.g. transient DB error), the
        live-session branch must not crash `_compute_firm_nav` — it falls
        back to the legacy (pre-override) sums rather than propagating
        the exception into the NAV endpoint."""
        from backend.api.routes import auth as auth_mod

        auth_mod._NAV_CACHE.update(ts=0.0, value=None)

        df_p = _positions_df_groww_style()
        df_h = pd.DataFrame(columns=["account"])
        legacy_sum_p = _legacy_summary_df(day_change_val=777.0)
        legacy_sum_h = pd.DataFrame(columns=["account", "day_change_val", "pnl"])

        async def _fake_compute_firm_nav():
            return {"nav": 500_000.0}

        with (
            patch("backend.api.background._fetch_holdings_direct", return_value=(df_h, legacy_sum_h)),
            patch("backend.api.background._fetch_positions_direct", return_value=(df_p, legacy_sum_p)),
            patch("backend.api.background._intraday_equity", []),
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True),
            patch(
                "backend.api.routes.positions._override_stale_close_from_snapshot",
                side_effect=RuntimeError("db down"),
            ),
            patch(
                "backend.api.routes.holdings._override_stale_close_for_holdings",
                new=AsyncMock(return_value=None),
            ),
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_fake_compute_firm_nav),
        ):
            firm_nav, firm_day_pnl, firm_cum_pnl, as_of_iso = asyncio.run(
                auth_mod._compute_firm_nav()
            )

        # Must not raise — falls back to the legacy summary's day_change_val.
        assert firm_day_pnl == pytest.approx(777.0)
        assert firm_nav == pytest.approx(500_000.0)


class TestAuthClosedHoursUsesSnapshotNotLiveFetch:
    """Market fully closed — must NEVER call the live broker fetch for
    Day P&L; must reuse `_positions_snapshot` / `_holdings_snapshot`
    (item #1's fixed baseline anchor) instead."""

    def test_closed_market_uses_positions_and_holdings_snapshot_totals(self):
        from backend.api.routes import auth as auth_mod
        from backend.api.schemas import (
            PositionsResponse, PositionsSummaryRow,
            HoldingsResponse, HoldingsSummaryRow,
        )

        auth_mod._NAV_CACHE.update(ts=0.0, value=None)

        # Legacy live-fetch sums — must NOT be what firm_day_pnl reflects
        # when the market is closed (proves the live path is bypassed).
        df_p = _positions_df_groww_style()
        df_h = pd.DataFrame(columns=["account"])
        legacy_sum_p = _legacy_summary_df(day_change_val=99999.0)
        legacy_sum_h = pd.DataFrame(columns=["account", "day_change_val", "pnl"])

        pos_snap = PositionsResponse(
            rows=[],
            summary=[
                PositionsSummaryRow(account="ACC1", pnl=800.0, day_change_val=300.0,
                                     day_change_percentage=3.0, day_prev_val=10000.0),
                PositionsSummaryRow(account="TOTAL", pnl=800.0, day_change_val=300.0,
                                     day_change_percentage=3.0, day_prev_val=10000.0),
            ],
            refreshed_at="2026-09-20T04:00:00Z",
            as_of="2026-09-20T04:00:00Z",
        )
        hold_snap = HoldingsResponse(
            rows=[],
            summary=[
                HoldingsSummaryRow(account="TOTAL", inv_val=5000.0, cur_val=5200.0,
                                    pnl=200.0, pnl_percentage=4.0,
                                    day_change_val=50.0, day_change_percentage=1.0),
            ],
            refreshed_at="2026-09-20T04:00:00Z",
            as_of="2026-09-20T04:00:00Z",
        )

        async def _fake_positions_snapshot():
            return pos_snap

        async def _fake_holdings_snapshot():
            return hold_snap

        async def _fake_compute_firm_nav():
            return {"nav": 2_000_000.0}

        with (
            patch("backend.api.background._fetch_holdings_direct", return_value=(df_h, legacy_sum_h)),
            patch("backend.api.background._fetch_positions_direct", return_value=(df_p, legacy_sum_p)),
            patch("backend.api.background._intraday_equity", []),
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False),
            patch(
                "backend.api.routes.positions._positions_snapshot",
                side_effect=_fake_positions_snapshot,
            ) as mock_pos_snap,
            patch(
                "backend.api.routes.holdings._holdings_snapshot",
                side_effect=_fake_holdings_snapshot,
            ) as mock_hold_snap,
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_fake_compute_firm_nav),
        ):
            firm_nav, firm_day_pnl, firm_cum_pnl, as_of_iso = asyncio.run(
                auth_mod._compute_firm_nav()
            )

        assert mock_pos_snap.await_count == 1
        assert mock_hold_snap.await_count == 1

        # firm_day_pnl = positions TOTAL.day_change_val (300) +
        # holdings TOTAL.day_change_val (50) = 350 — NOT the legacy
        # live-fetch value (99999).
        assert firm_day_pnl == pytest.approx(350.0), (
            f"closed-market firm_day_pnl must come from the already-"
            f"anchored _positions_snapshot/_holdings_snapshot TOTAL rows "
            f"(300 + 50 = 350), not a live broker fetch — got {firm_day_pnl}"
        )
        assert firm_cum_pnl == pytest.approx(1000.0)  # 800 (positions) + 200 (holdings)
        assert firm_nav == pytest.approx(2_000_000.0)
        assert as_of_iso == "2026-09-20T04:00:00Z"

    def test_closed_market_both_snapshots_none_falls_back_to_legacy_sums(self):
        """`_positions_snapshot` / `_holdings_snapshot` catch their own DB
        errors internally and return `None` rather than raising (see each
        function's docstring) — a bare `except Exception` around them
        would NEVER actually observe a real DB failure, making that
        fallback branch unreachable in practice. The reachable failure
        path is BOTH readers returning `None` (e.g. first-ever-deploy,
        no snapshot exists yet, or a swallowed DB error inside either
        reader). Must fall back to the (pre-override) legacy sums rather
        than reporting firm_day_pnl=0."""
        from backend.api.routes import auth as auth_mod

        auth_mod._NAV_CACHE.update(ts=0.0, value=None)

        df_p = _positions_df_groww_style()
        df_h = pd.DataFrame(columns=["account"])
        legacy_sum_p = _legacy_summary_df(day_change_val=555.0)
        legacy_sum_h = pd.DataFrame(columns=["account", "day_change_val", "pnl"])

        async def _fake_compute_firm_nav():
            return {"nav": 100.0}

        async def _fake_none_snapshot():
            return None

        with (
            patch("backend.api.background._fetch_holdings_direct", return_value=(df_h, legacy_sum_h)),
            patch("backend.api.background._fetch_positions_direct", return_value=(df_p, legacy_sum_p)),
            patch("backend.api.background._intraday_equity", []),
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False),
            patch(
                "backend.api.routes.positions._positions_snapshot",
                side_effect=_fake_none_snapshot,
            ),
            patch(
                "backend.api.routes.holdings._holdings_snapshot",
                side_effect=_fake_none_snapshot,
            ),
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_fake_compute_firm_nav),
        ):
            firm_nav, firm_day_pnl, firm_cum_pnl, as_of_iso = asyncio.run(
                auth_mod._compute_firm_nav()
            )

        assert firm_day_pnl == pytest.approx(555.0), (
            f"both snapshot readers returning None must fall back to the "
            f"legacy sums (555), not report 0 — got {firm_day_pnl}"
        )
        assert firm_nav == pytest.approx(100.0)

    def test_closed_market_unexpected_exception_falls_back_to_legacy_sums(self):
        """A genuinely unexpected exception (e.g. an AttributeError from a
        bug, not a caught-internally DB error) must still be swallowed by
        the defensive `except Exception` — `_compute_firm_nav` must not
        crash — falling back to the (pre-override) legacy sums."""
        from backend.api.routes import auth as auth_mod

        auth_mod._NAV_CACHE.update(ts=0.0, value=None)

        df_p = _positions_df_groww_style()
        df_h = pd.DataFrame(columns=["account"])
        legacy_sum_p = _legacy_summary_df(day_change_val=555.0)
        legacy_sum_h = pd.DataFrame(columns=["account", "day_change_val", "pnl"])

        async def _fake_compute_firm_nav():
            return {"nav": 100.0}

        with (
            patch("backend.api.background._fetch_holdings_direct", return_value=(df_h, legacy_sum_h)),
            patch("backend.api.background._fetch_positions_direct", return_value=(df_p, legacy_sum_p)),
            patch("backend.api.background._intraday_equity", []),
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False),
            patch(
                "backend.api.routes.positions._positions_snapshot",
                side_effect=RuntimeError("unexpected bug"),
            ),
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_fake_compute_firm_nav),
        ):
            firm_nav, firm_day_pnl, firm_cum_pnl, as_of_iso = asyncio.run(
                auth_mod._compute_firm_nav()
            )

        assert firm_day_pnl == pytest.approx(555.0)
        assert firm_nav == pytest.approx(100.0)

    def test_closed_market_never_calls_live_broker_fetch_for_day_pnl(self):
        """Canonical closed-hours-gate invariant: when the market is
        closed, the live positions/holdings broker calls must never be
        the source of firm_day_pnl — only `_positions_snapshot` /
        `_holdings_snapshot` may be consulted."""
        from backend.api.routes import auth as auth_mod
        from backend.api.schemas import (
            PositionsResponse, PositionsSummaryRow,
            HoldingsResponse, HoldingsSummaryRow,
        )

        auth_mod._NAV_CACHE.update(ts=0.0, value=None)

        df_h = pd.DataFrame(columns=["account"])
        df_p = pd.DataFrame(columns=["account"])
        legacy_sum_h = pd.DataFrame(columns=["account", "day_change_val", "pnl"])
        legacy_sum_p = pd.DataFrame(columns=["account", "day_change_val", "pnl"])

        pos_snap = PositionsResponse(
            rows=[], summary=[PositionsSummaryRow(account="TOTAL", pnl=0.0, day_change_val=0.0,
                                                    day_change_percentage=0.0, day_prev_val=0.0)],
            refreshed_at="x", as_of="2026-09-20T04:00:00Z",
        )
        hold_snap = HoldingsResponse(
            rows=[], summary=[HoldingsSummaryRow(account="TOTAL", inv_val=0.0, cur_val=0.0,
                                                  pnl=0.0, pnl_percentage=0.0,
                                                  day_change_val=0.0, day_change_percentage=0.0)],
            refreshed_at="x", as_of="2026-09-20T04:00:00Z",
        )

        async def _fake_positions_snapshot():
            return pos_snap

        async def _fake_holdings_snapshot():
            return hold_snap

        async def _fake_compute_firm_nav():
            return {"nav": 0.0}

        with (
            patch(
                "backend.api.background._fetch_holdings_direct",
                return_value=(df_h, legacy_sum_h),
            ) as mock_fetch_h,
            patch(
                "backend.api.background._fetch_positions_direct",
                return_value=(df_p, legacy_sum_p),
            ) as mock_fetch_p,
            patch("backend.api.background._intraday_equity", []),
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False),
            patch(
                "backend.api.routes.positions._override_stale_close_from_snapshot",
            ) as mock_override_p,
            patch(
                "backend.api.routes.holdings._override_stale_close_for_holdings",
            ) as mock_override_h,
            patch("backend.api.routes.positions._positions_snapshot", side_effect=_fake_positions_snapshot),
            patch("backend.api.routes.holdings._holdings_snapshot", side_effect=_fake_holdings_snapshot),
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_fake_compute_firm_nav),
        ):
            asyncio.run(auth_mod._compute_firm_nav())

        # The stale-close override (which itself triggers a live-fetch-
        # anchored baseline query) must NEVER be invoked while the market
        # is closed — the closed-hours gate must short-circuit straight
        # to the snapshot readers.
        assert mock_override_p.await_count == 0
        assert mock_override_h.await_count == 0
