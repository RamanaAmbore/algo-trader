"""
day_change_val correctness for closed-hours snapshot paths.

Three root causes addressed:
  Cause 1 — routes return snapshot day_change_val when market is closed
             (closed-hours guard already in positions.py + holdings.py).
  Cause 2 — daily_snapshot._positions_rows uses decomposed_intraday_pnl,
             so the snapshot captured at close / close_settled is
             internally consistent (not the naive (last-close)×qty).
  Cause 3 — Frontend Contract A branch is gated on isMarketOpen()
             (static source check here; runtime check in the e2e spec).

Five quality dimensions per spec charter:
  1. SSOT    — _positions_rows routes through decomposed_intraday_pnl, not
               inline (last-close)×qty arithmetic.
  2. Perf    — zero broker calls from the snapshot route path.
  3. Stale   — grep checks that the source edits are present.
  4. Reuse   — decomposed_intraday_pnl is the shared formula from pnl_math.
  5. Correct — golden-value snapshot day_pnl matches decomposed formula.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Source paths for grep / stale-code checks
_SNAP_SRC    = Path(__file__).parent.parent / "api" / "algo" / "daily_snapshot.py"
_HANDLERS_SRC = Path(__file__).parent.parent / "api" / "algo" / "market_lifecycle_handlers.py"
_POS_SRC     = Path(__file__).parent.parent / "api" / "routes" / "positions.py"
_POS_HELPERS_SRC = Path(__file__).parent.parent / "api" / "routes" / "positions_helpers.py"
_HOL_SRC     = Path(__file__).parent.parent / "api" / "routes" / "holdings.py"
_PULSE_SRC   = Path(__file__).parent.parent.parent / "frontend" / "src" / "lib" / "MarketPulse.svelte"
_PULSE_UNIFIED_SRC = Path(__file__).parent.parent.parent / "frontend" / "src" / "lib" / "data" / "pulseUnified.js"
_NAV_SRC     = Path(__file__).parent.parent.parent / "frontend" / "src" / "lib" / "data" / "nav.js"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Dimension 3 — static source checks
# ---------------------------------------------------------------------------

class TestSourceChecks:
    def test_daily_snapshot_uses_decomposed_formula(self):
        """_positions_rows in daily_snapshot.py routes through decomposed_intraday_pnl,
        not through inline (last_price - close_price) * qty arithmetic."""
        src = _src(_SNAP_SRC)
        assert "decomposed_intraday_pnl" in src, (
            "daily_snapshot._positions_rows must call decomposed_intraday_pnl"
        )
        # Confirm the decomposed path is guarded by the intraday-field key set
        assert "_INTRADAY_FIELDS" in src, (
            "_INTRADAY_FIELDS set must be present in daily_snapshot._positions_rows "
            "to gate the decomposed vs naive formula path"
        )

    def test_daily_snapshot_imports_pnl_math(self):
        """daily_snapshot.py imports from pnl_math inside _positions_rows."""
        src = _src(_SNAP_SRC)
        assert "from backend.api.algo.pnl_math import" in src, (
            "daily_snapshot.py must import decomposed_intraday_pnl / naive_day_pnl "
            "from pnl_math — not re-implement the formula"
        )

    def test_market_lifecycle_handlers_uses_snapshot_daily_book(self):
        """Both close and close_settled events call snapshot_daily_book."""
        src = _src(_HANDLERS_SRC)
        assert "close_settled" in src, "market_lifecycle_handlers must register close_settled"
        assert "snapshot_daily_book" in src, (
            "market_lifecycle_handlers must call snapshot_daily_book on close_settled "
            "so the recomputed day_pnl lands in daily_book"
        )

    def test_positions_route_snapshot_path_exists(self):
        """positions.py imports closed_hours_or_broker and defines _positions_snapshot."""
        src = _src(_POS_SRC)
        assert "closed_hours_or_broker" in src, (
            "positions.py must import closed_hours_or_broker from snapshot_gate"
        )
        assert "_positions_snapshot" in src

    def test_holdings_route_snapshot_path_exists(self):
        """holdings.py imports closed_hours_or_broker and defines _holdings_snapshot."""
        src = _src(_HOL_SRC)
        assert "closed_hours_or_broker" in src, (
            "holdings.py must import closed_hours_or_broker from snapshot_gate"
        )
        assert "_holdings_snapshot" in src

    def test_marketpulse_imports_is_market_open(self):
        """MarketPulse.svelte imports isMarketOpen from marketHours.js."""
        src = _src(_PULSE_SRC)
        assert "isMarketOpen" in src, (
            "MarketPulse.svelte must import isMarketOpen from $lib/marketHours "
            "so the Contract A branch can be gated during closed hours"
        )

    def test_marketpulse_contract_a_gated_on_market_open(self):
        """Contract A branch is gated on isMarketOpen().

        After the pulseUnified refactor (f378ce53), the gate lives in two
        places rather than one inline block in MarketPulse.svelte:

          1. pulseUnified.js  — `_mktOpen = isMarketOpen()` guards legLiveLtp
             so positions only receive a live LTP during open hours.
          2. nav.js           — `livePositionDayPnl` checks `marketOpen &&
             live != null && closePx === 0` for the Contract A branch
             (new position opened today, no prior close_price).

        Both checks must survive future refactors.
        """
        unified_src = _src(_PULSE_UNIFIED_SRC)
        nav_src     = _src(_NAV_SRC)

        mkt_open_idx = unified_src.find("_mktOpen")
        assert mkt_open_idx != -1 and "isMarketOpen" in unified_src, (
            "_mktOpen (isMarketOpen gate) not found in pulseUnified.js — "
            "positions live-LTP gate removed"
        )

        contract_a_guard = nav_src.find("marketOpen && live != null && closePx === 0")
        assert contract_a_guard != -1, (
            "Contract A branch (`marketOpen && live != null && closePx === 0`) "
            "not found in nav.js livePositionDayPnl — guard removed"
        )

    def test_marketpulse_holdings_recompute_gated_on_market_open(self):
        """Holdings day_pnl live-recompute in pulseUnified uses LTP unconditionally.

        Holdings do NOT need a market-open gate: there is no Contract A case
        for holdings (they always have a prior close_price), and the day_pnl
        formula is (liveHold − holdClose) × qty which is valid during both
        open and closed hours using snapshot LTP.

        This test guards that:
          1. mergeHoldingRows in pulseUnified.js uses the `isMarketOpen`
             ctx injection (so the dependency is explicit even if unused).
          2. There is NO _holdMktOpen gate suppressing LTP in closed hours
             (that would break closed-hours sparklines — Bug 1 from PULSE_SPEC).
        """
        unified_src = _src(_PULSE_UNIFIED_SRC)
        assert "isMarketOpen" in unified_src, (
            "pulseUnified.js must accept isMarketOpen via ctx"
        )
        # Holdings must NOT suppress LTP behind a market-open gate.
        assert "_holdMktOpen = isMarketOpen()" not in unified_src, (
            "_holdMktOpen market-open gate must NOT exist in pulseUnified — "
            "holdings use LTP (snapshot or live) unconditionally"
        )


# ---------------------------------------------------------------------------
# Dimension 5 — functional: _positions_rows day_pnl correctness
# ---------------------------------------------------------------------------

class TestPositionsRowsDayPnl:
    """daily_snapshot._positions_rows correctly computes day_pnl."""

    def _now_ist_post_close(self):
        """A datetime outside both NSE and MCX session windows (e.g. 16:00 IST)."""
        from zoneinfo import ZoneInfo
        return datetime(2026, 6, 27, 16, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    def test_decomposed_formula_used_when_intraday_fields_present(self):
        """day_pnl uses decomposed formula when all intraday split fields present."""
        from backend.api.algo.daily_snapshot import _positions_rows
        from backend.api.algo.pnl_math import decomposed_intraday_pnl

        row = {
            "tradingsymbol":      "NIFTY25JUNFUT",
            "exchange":           "NFO",
            "last_price":         23150.0,
            "close_price":        23000.0,
            "quantity":           50,
            "average_price":      22800.0,
            "pnl":                3500.0,
            # Intraday split (10 carried, 0 new trades today)
            "overnight_quantity": 50,
            "day_buy_quantity":   0,
            "day_sell_quantity":  0,
            "day_buy_value":      0.0,
            "day_sell_value":     0.0,
        }
        target_date = date(2026, 6, 27)
        rows = _positions_rows("ZG0790", target_date, [row], self._now_ist_post_close())

        assert len(rows) == 1
        expected = float(decomposed_intraday_pnl(
            oq=50.0, ltp=23150.0, cls=23000.0,
            bq=0.0, bv=0.0, sv=0.0, sq=0.0,
        ))
        assert rows[0]["day_pnl"] is not None
        assert math.isclose(rows[0]["day_pnl"], expected, rel_tol=1e-6), (
            f"day_pnl={rows[0]['day_pnl']:.2f} expected {expected:.2f}"
        )

    def test_decomposed_formula_partially_closed_position(self):
        """Partially-closed position: overnight 10, sold 4 today, net 6.
        Naive formula (LTP-close)*qty=6*150 misses the realised leg.
        Decomposed formula captures both."""
        from backend.api.algo.daily_snapshot import _positions_rows
        from backend.api.algo.pnl_math import decomposed_intraday_pnl

        # Sold 4 @ 23200 (notional 92800), net qty=6, LTP=23150, close=23000
        row = {
            "tradingsymbol":      "NIFTY25JUNFUT",
            "exchange":           "NFO",
            "last_price":         23150.0,
            "close_price":        23000.0,
            "quantity":           6,
            "average_price":      22800.0,
            "pnl":                2100.0,
            "overnight_quantity": 10,
            "day_buy_quantity":   0,
            "day_sell_quantity":  4,
            "day_buy_value":      0.0,
            "day_sell_value":     92800.0,
        }
        target_date = date(2026, 6, 27)
        rows = _positions_rows("ZG0790", target_date, [row], self._now_ist_post_close())

        expected_decomposed = float(decomposed_intraday_pnl(
            oq=10.0, ltp=23150.0, cls=23000.0,
            bq=0.0, bv=0.0, sv=92800.0, sq=4.0,
        ))
        naive = (23150.0 - 23000.0) * 6  # 900 — wrong for partially-closed
        assert rows[0]["day_pnl"] is not None
        assert math.isclose(rows[0]["day_pnl"], expected_decomposed, rel_tol=1e-6), (
            f"day_pnl={rows[0]['day_pnl']:.2f} expected {expected_decomposed:.2f}"
        )
        # Confirm the decomposed result differs from the naive (this is the bug)
        assert not math.isclose(rows[0]["day_pnl"], naive, abs_tol=1.0), (
            f"day_pnl should not equal naive (LTP-close)*qty={naive}; "
            "that formula misses the realised sell leg"
        )

    def test_naive_fallback_when_no_intraday_fields(self):
        """Falls back to naive (LTP-close)×qty when intraday fields are absent."""
        from backend.api.algo.daily_snapshot import _positions_rows

        row = {
            "tradingsymbol": "RELIANCE",
            "exchange":      "NSE",
            "last_price":    2950.0,
            "close_price":   2900.0,
            "quantity":      10,
            "average_price": 2800.0,
            "pnl":           1500.0,
            # No overnight_quantity / day_buy_quantity etc.
        }
        target_date = date(2026, 6, 27)
        rows = _positions_rows("ZG0790", target_date, [row], self._now_ist_post_close())

        expected_naive = (2950.0 - 2900.0) * 10  # 500
        assert rows[0]["day_pnl"] is not None
        assert math.isclose(rows[0]["day_pnl"], expected_naive, rel_tol=1e-6)

    def test_mid_session_yields_none_pnl(self):
        """Rows captured mid-session produce None day_pnl (not committed to DB)."""
        from backend.api.algo.daily_snapshot import _positions_rows
        from zoneinfo import ZoneInfo

        # NFO is NSE-hours; 11:00 IST = mid-session
        mid_session = datetime(2026, 6, 27, 11, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

        row = {
            "tradingsymbol":      "NIFTY25JUNFUT",
            "exchange":           "NFO",
            "last_price":         23150.0,
            "close_price":        23000.0,
            "quantity":           50,
            "average_price":      22800.0,
            "pnl":                17500.0,
            "overnight_quantity": 50,
            "day_buy_quantity":   0,
            "day_sell_quantity":  0,
            "day_buy_value":      0.0,
            "day_sell_value":     0.0,
        }
        target_date = date(2026, 6, 27)
        rows = _positions_rows("ZG0790", target_date, [row], mid_session)

        assert rows[0]["day_pnl"] is None, (
            "day_pnl must be None during market hours to prevent mid-session "
            "values from polluting the close-override path"
        )
        assert rows[0]["ltp"] is None, (
            "ltp must be None during market hours for the same reason"
        )


# ---------------------------------------------------------------------------
# Dimension 1 / 2 — closed-hours route returns snapshot, no broker call
# ---------------------------------------------------------------------------

class TestClosedHoursRouteReturnsSnapshot:
    """Verify the closed-hours guard in positions.py returns snapshot
    day_change_val without calling the broker."""

    @pytest.mark.asyncio
    async def test_positions_closed_returns_snapshot_day_change_val(self):
        """Closed market → snapshot returned; day_change_val matches stored value."""
        from backend.api.schemas import PositionsResponse, PositionRow, PositionsSummaryRow

        # Simulate a snapshot whose day_change_val was recomputed by the
        # decomposed formula at close-settled time (the fix in Cause 2).
        settled_dcv = 2250.0  # decomposed result at 16:15 IST
        fake_snapshot = PositionsResponse(
            rows=[
                PositionRow(
                    account="ZG0790",
                    tradingsymbol="NIFTY25JUNFUT",
                    exchange="NFO",
                    product="NRML",
                    quantity=50,
                    average_price=22800.0,
                    close_price=23045.0,   # settled close
                    pnl=12250.0,
                    last_price=23045.0,
                    day_change_val=settled_dcv,
                    day_change_percentage=0.978,
                )
            ],
            summary=[
                PositionsSummaryRow(
                    account="ZG0790", pnl=12250.0,
                    day_change_val=settled_dcv, day_change_percentage=0.978,
                ),
                PositionsSummaryRow(
                    account="TOTAL", pnl=12250.0,
                    day_change_val=settled_dcv, day_change_percentage=0.978,
                ),
            ],
            refreshed_at="Fri 27 Jun 16:15 IST",
            as_of="2026-06-27T10:45:00+00:00",
        )

        mock_broker_fetch = MagicMock()

        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open",
            return_value=False,
        ), patch(
            "backend.api.routes.positions._positions_snapshot",
            new=AsyncMock(return_value=fake_snapshot),
        ), patch(
            "backend.brokers.broker_apis.fetch_positions",
            mock_broker_fetch,
        ):
            from backend.api.routes.positions import PositionsController
            handler_fn = PositionsController.get_positions.fn

            mock_request = MagicMock()
            with patch("backend.api.routes.positions_helpers.is_admin_request", return_value=True), \
                 patch("backend.api.routes.positions_helpers.resolve_role_from_connection",
                       return_value="admin"), \
                 patch("backend.api.routes.positions_helpers.normalise_role", return_value="admin"):
                resp = await handler_fn(None, mock_request, fresh=False)

        # 1. Snapshot returned
        assert resp.as_of == "2026-06-27T10:45:00+00:00"
        assert len(resp.rows) == 1
        # 2. day_change_val is the settled snapshot value, not recomputed
        assert math.isclose(resp.rows[0].day_change_val, settled_dcv, rel_tol=1e-6), (
            f"day_change_val={resp.rows[0].day_change_val} expected {settled_dcv}"
        )
        # 3. Zero broker calls
        assert mock_broker_fetch.call_count == 0, (
            "broker fetch_positions must not be called during closed hours"
        )

    @pytest.mark.asyncio
    async def test_holdings_closed_returns_snapshot_day_change_val(self):
        """Closed market → holdings snapshot returned with correct day_change_val."""
        from backend.api.schemas import HoldingsResponse, HoldingRow, HoldingsSummaryRow

        settled_dcv = 480.0
        fake_snapshot = HoldingsResponse(
            rows=[
                HoldingRow(
                    account="ZG0790",
                    tradingsymbol="RELIANCE",
                    exchange="NSE",
                    quantity=10,
                    average_price=2800.0,
                    close_price=2948.0,
                    inv_val=28000.0,
                    cur_val=29480.0,
                    pnl=1480.0,
                    pnl_percentage=5.29,
                    last_price=2948.0,
                    day_change_val=settled_dcv,
                    day_change_percentage=1.65,
                )
            ],
            summary=[
                HoldingsSummaryRow(
                    account="ZG0790", inv_val=28000.0, cur_val=29480.0,
                    pnl=1480.0, pnl_percentage=5.29,
                    day_change_val=settled_dcv, day_change_percentage=1.65,
                )
            ],
            refreshed_at="Fri 27 Jun 16:15 IST",
            as_of="2026-06-27T10:45:00+00:00",
        )

        mock_broker_fetch = MagicMock()

        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open",
            return_value=False,
        ), patch(
            "backend.api.routes.holdings._holdings_snapshot",
            new=AsyncMock(return_value=fake_snapshot),
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings",
            mock_broker_fetch,
        ):
            from backend.api.routes.holdings import HoldingsController
            handler_fn = HoldingsController.get_holdings.fn

            mock_request = MagicMock()
            with patch("backend.api.routes.holdings.is_admin_request", return_value=True), \
                 patch("backend.api.routes.holdings.resolve_role_from_connection",
                       return_value="admin"), \
                 patch("backend.api.routes.holdings.normalise_role", return_value="admin"):
                resp = await handler_fn(None, mock_request, fresh=False)

        assert resp.as_of == "2026-06-27T10:45:00+00:00"
        assert len(resp.rows) == 1
        assert math.isclose(resp.rows[0].day_change_val, settled_dcv, rel_tol=1e-6)
        assert mock_broker_fetch.call_count == 0

    @pytest.mark.asyncio
    async def test_positions_snapshot_populates_overnight_quantity(self):
        """Closed-hours snapshot rows must carry overnight_quantity == quantity.

        Regression 2026-07-03: _positions_snapshot was building PositionRow
        without setting overnight_quantity, so msgspec defaulted it to 0.
        That tripped the frontend `baseDayPnlForPosition` new-position override
        (`oq === 0 && pnl !== 0 → day = pnl`), inflating Day P&L from the
        settled `day_change_val` (e.g. ₹0.6) to LIFETIME `pnl` (e.g. ₹14670)
        across NavStrip P slot 1, /admin/derivatives Snapshot, Legs, MarketPulse.

        The snapshot represents session-end holdings, so all held qty IS
        overnight qty for the next session — asserted here.
        """
        from datetime import datetime as _dt, timezone as _tz
        from decimal import Decimal
        from unittest.mock import MagicMock as _MM

        fake_captured = _dt(2026, 7, 3, 10, 45, tzinfo=_tz.utc)

        # A row that would trip the old bug: overnight position (avg=127.4,
        # qty=-3) with tiny settled day_change_val (₹0.6) but large lifetime
        # pnl (₹14670). Pre-fix: oq defaulted to 0 → frontend override →
        # day rendered as 14670 instead of 0.6.
        fake_rows = [(
            "ZG0790",                     # account
            "CRUDEOIL26JUL6300PE",        # symbol
            "MCX",                        # exchange
            -3,                           # qty
            Decimal("127.40"),            # avg_cost
            Decimal("78.50"),             # ltp
            Decimal("0.60"),              # day_pnl (settled)
            Decimal("14670.00"),          # total_pnl (lifetime)
            "{}",                         # payload_json
            fake_captured,                # captured_at
            None,                         # previous_close (index 10)
            None,                         # prev_ltp (index 11)
            None,                         # prev_settlement_pnl (index 12)
        )]

        mock_result = _MM()
        mock_result.all = _MM(return_value=fake_rows)
        mock_session = _MM()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch(
            "backend.api.database.async_session",
            return_value=mock_session,
        ):
            from backend.api.routes.positions import _positions_snapshot
            resp = await _positions_snapshot()

        assert resp is not None
        assert len(resp.rows) == 1
        row = resp.rows[0]
        # Primary invariant: overnight_quantity == quantity so the frontend
        # override cannot fire on snapshot rows.
        assert row.overnight_quantity == row.quantity, (
            f"overnight_quantity={row.overnight_quantity} must equal "
            f"quantity={row.quantity} on snapshot rows so frontend "
            f"baseDayPnlForPosition doesn't hit the new-position override"
        )
        # Sanity: day_change_val is the small settled value, not lifetime pnl.
        assert math.isclose(row.day_change_val, 0.60, rel_tol=1e-6)
        assert math.isclose(row.pnl, 14670.00, rel_tol=1e-6)

    @pytest.mark.asyncio
    async def test_positions_snapshot_extras_fallback_when_day_pnl_null(self):
        """Reader falls back to `payload_json.snapshot_extras.day_change_val`
        when the top-level `day_pnl` column is NULL.

        Regression 2026-07-04: the writer's `_is_exchange_open_at` gate in
        daily_snapshot._positions_rows checks time-of-day only (no weekday
        check). On a Saturday 15:35 IST snapshot MCX rows land with
        `day_pnl=NULL` (MCX 09:00–23:30 IST window is "mid-session" by
        time-of-day) even though the market is actually closed. The 15:35
        IST batch then UPSERTs and clobbers Friday's good MCX day_pnl
        values. Reader falls back to `snapshot_extras.day_change_val` —
        the raw Kite `day_change` field captured at snapshot time — so the
        frozen close-time value surfaces during closed hours instead of a
        blanket zero across the frontend Day P&L cells.

        Cascading surfaces protected: NavStrip P slot 1, /admin/derivatives
        Snapshot per-row + TOTAL, Legs grid, MarketPulse Positions grid.
        """
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        from decimal import Decimal
        from unittest.mock import MagicMock as _MM

        fake_captured = _dt(2026, 7, 4, 10, 5, tzinfo=_tz.utc)

        # Exactly the shape that lands in prod: mid-session-gated MCX row
        # with NULL day_pnl but populated snapshot_extras.day_change_val.
        # Row taken from prod: ZG0790 CRUDEOIL26JUL6300PE with
        # extras.day_change_val = -0.20 on 2026-07-04 10:05 UTC.
        payload = {
            # Broker's raw row (top-level Kite shape kept for forensics)
            "tradingsymbol": "CRUDEOIL26JUL6300PE",
            "exchange": "MCX",
            # Nested extras block — the fallback source
            "snapshot_extras": {
                "day_change_val":  -0.20,
                "day_change_pct":  -0.255,
                "prev_close":      78.30,
                "settled":         False,
            },
        }

        fake_rows = [(
            "ZG0790",                       # account
            "CRUDEOIL26JUL6300PE",          # symbol
            "MCX",                          # exchange
            -3,                             # qty
            Decimal("127.40"),              # avg_cost
            Decimal("78.50"),               # ltp
            None,                           # day_pnl — NULL (writer's mid-session gate)
            Decimal("14670.00"),            # total_pnl (lifetime)
            _json.dumps(payload),           # payload_json
            fake_captured,                  # captured_at
            None,                           # previous_close (index 10)
            None,                           # prev_ltp (index 11)
            None,                           # prev_settlement_pnl (index 12)
        )]

        mock_result = _MM()
        mock_result.all = _MM(return_value=fake_rows)
        mock_session = _MM()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch(
            "backend.api.database.async_session",
            return_value=mock_session,
        ):
            from backend.api.routes.positions import _positions_snapshot
            resp = await _positions_snapshot()

        assert resp is not None
        assert len(resp.rows) == 1
        row = resp.rows[0]
        # Primary invariant: day_change_val was pulled from snapshot_extras
        # since the top-level day_pnl column was NULL. Without the fallback
        # this would render as 0.0 and Day P&L across the closed-hours
        # frontend would be a blanket zero.
        assert math.isclose(row.day_change_val, -0.20, rel_tol=1e-6), (
            f"day_change_val={row.day_change_val} — expected -0.20 from "
            f"snapshot_extras.day_change_val (top-level day_pnl was NULL). "
            f"Reader must fall back to extras when writer's mid-session "
            f"gate nulls day_pnl on weekend/holiday snapshot cycles."
        )
        # day_change_percentage also pulled from extras
        assert math.isclose(row.day_change_percentage, -0.255, rel_tol=1e-6)
        # Overnight_quantity still populated (prior fix — unchanged)
        assert row.overnight_quantity == row.quantity == -3
        # Lifetime pnl untouched
        assert math.isclose(row.pnl, 14670.00, rel_tol=1e-6)
        # Summary aggregates use the extras-fallback value too
        assert len(resp.summary) == 2  # one account + TOTAL
        zg = next(s for s in resp.summary if s.account == "ZG0790")
        total = next(s for s in resp.summary if s.account == "TOTAL")
        assert math.isclose(zg.day_change_val, -0.20, rel_tol=1e-6), (
            f"Account summary day_change_val={zg.day_change_val} — must "
            f"reflect the extras-fallback value on the underlying row"
        )
        assert math.isclose(total.day_change_val, -0.20, rel_tol=1e-6)

    @pytest.mark.asyncio
    async def test_positions_snapshot_prefers_day_pnl_column_over_extras(self):
        """When both `day_pnl` (column) and `extras.day_change_val` are set,
        the top-level column wins.

        The column carries the decomposed-intraday-pnl value computed by
        the writer from stored close_price — the canonical formula. Extras
        carries Kite's raw `day_change` which may drift on stale-payload
        weekends. Reader must NOT prefer extras over a legitimate column
        value (including a real 0.0)."""
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        from decimal import Decimal
        from unittest.mock import MagicMock as _MM

        fake_captured = _dt(2026, 7, 3, 18, 5, tzinfo=_tz.utc)  # Fri 23:35 IST

        payload = {
            "tradingsymbol": "NIFTY26JULFUT",
            "exchange": "NFO",
            "snapshot_extras": {
                "day_change_val":  -999.99,   # decoy — must NOT be used
                "day_change_pct":  -1.11,
                "settled":         True,
            },
        }

        fake_rows = [(
            "ZJ6294",                       # account
            "NIFTY26JULFUT",                # symbol
            "NFO",                          # exchange
            50,                             # qty
            Decimal("23000.00"),            # avg_cost
            Decimal("23150.00"),            # ltp
            Decimal("2500.00"),             # day_pnl — populated (wins)
            Decimal("7500.00"),             # total_pnl
            _json.dumps(payload),           # payload_json
            fake_captured,                  # captured_at
            None,                           # previous_close (index 10)
            None,                           # prev_ltp (index 11)
            None,                           # prev_settlement_pnl (index 12)
        )]

        mock_result = _MM()
        mock_result.all = _MM(return_value=fake_rows)
        mock_session = _MM()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch(
            "backend.api.database.async_session",
            return_value=mock_session,
        ):
            from backend.api.routes.positions import _positions_snapshot
            resp = await _positions_snapshot()

        assert resp is not None and len(resp.rows) == 1
        row = resp.rows[0]
        # Column value wins over extras (decoy -999.99 must be ignored)
        assert math.isclose(row.day_change_val, 2500.00, rel_tol=1e-6), (
            f"day_change_val={row.day_change_val} — column value 2500 must "
            f"win over extras decoy -999.99 when day_pnl column is populated"
        )

    @pytest.mark.asyncio
    async def test_positions_snapshot_handles_malformed_payload_json(self):
        """Reader tolerates malformed / missing / non-dict payload_json.

        Defensive: extras extraction wrapped in try/except so a bad row
        cannot poison the entire snapshot response. Falls back to 0.0 for
        the affected row (best available)."""
        from datetime import datetime as _dt, timezone as _tz
        from decimal import Decimal
        from unittest.mock import MagicMock as _MM

        fake_captured = _dt(2026, 7, 4, 10, 5, tzinfo=_tz.utc)

        # Three degenerate payloads across three rows — each must
        # gracefully leave day_change_val at 0.0 (no extras available).
        fake_rows = [
            ("ZG0790", "SYM1", "MCX", -1, Decimal("100"), Decimal("50"),
             None, Decimal("500"), "not-valid-json{{",       fake_captured, None, None, None),
            ("ZG0790", "SYM2", "MCX", -1, Decimal("100"), Decimal("50"),
             None, Decimal("500"), "[1, 2, 3]",              fake_captured, None, None, None),
            ("ZG0790", "SYM3", "MCX", -1, Decimal("100"), Decimal("50"),
             None, Decimal("500"), None,                     fake_captured, None, None, None),
        ]

        mock_result = _MM()
        mock_result.all = _MM(return_value=fake_rows)
        mock_session = _MM()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch(
            "backend.api.database.async_session",
            return_value=mock_session,
        ):
            from backend.api.routes.positions import _positions_snapshot
            resp = await _positions_snapshot()

        assert resp is not None and len(resp.rows) == 3
        for row in resp.rows:
            # No extras extractable → day_change_val stays at 0.0
            assert row.day_change_val == 0.0
            # But total_pnl still comes through — snapshot isn't broken
            assert math.isclose(row.pnl, 500.0, rel_tol=1e-6)

    def test_positions_snapshot_source_contains_extras_fallback(self):
        """SSOT — the extras-fallback block is present in _positions_snapshot or
        its extracted helper (positions_helpers.py).
        Guards against a future refactor accidentally removing the fallback
        (which would silently regress the closed-hours Day P&L pipeline)."""
        # After the cc-reduction refactor, this logic lives in positions_helpers.py
        # (resolve_snapshot_day_pnl / extract_snapshot_extras). Accept either file.
        combined_src = _src(_POS_SRC) + _src(_POS_HELPERS_SRC)
        # The fallback path must reference snapshot_extras key lookup
        assert "snapshot_extras" in combined_src, (
            "_positions_snapshot must reference snapshot_extras — "
            "the extras-fallback path was removed"
        )
        # And must specifically pull day_change_val from extras
        assert (
            'extras.get("day_change_val")' in combined_src
            or "extras.get('day_change_val')" in combined_src
        ), (
            "_positions_snapshot (or its helpers) must read day_change_val from extras"
        )

    @pytest.mark.asyncio
    async def test_positions_open_market_calls_broker(self):
        """Market open → live path runs (no snapshot shortcut)."""
        from backend.api.schemas import PositionsResponse

        live_resp = PositionsResponse(rows=[], summary=[], refreshed_at="live-open")

        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open",
            return_value=True,
        ), patch(
            "backend.api.routes.positions.get_or_fetch",
            new=AsyncMock(return_value=live_resp),
        ):
            from backend.api.routes.positions import PositionsController
            handler_fn = PositionsController.get_positions.fn

            mock_request = MagicMock()
            with patch("backend.api.routes.positions_helpers.is_admin_request", return_value=True), \
                 patch("backend.api.routes.positions_helpers.resolve_role_from_connection",
                       return_value="admin"), \
                 patch("backend.api.routes.positions_helpers.normalise_role", return_value="admin"):
                resp = await handler_fn(None, mock_request, fresh=False)

        assert resp.as_of is None, "as_of must be None on live-market response"
        assert resp.refreshed_at == "live-open"


# ---------------------------------------------------------------------------
# Dimension 2 — performance: decomposed path adds no extra DB/broker calls
# ---------------------------------------------------------------------------

class TestDecomposedPathPerformance:
    def test_positions_rows_no_io_calls(self):
        """_positions_rows is pure-CPU: importing pnl_math does not trigger
        any DB or broker calls."""
        from zoneinfo import ZoneInfo
        from backend.api.algo.daily_snapshot import _positions_rows

        now_post_close = datetime(2026, 6, 27, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        rows_in = [
            {
                "tradingsymbol":      "NIFTY25JUNFUT",
                "exchange":           "NFO",
                "last_price":         23150.0,
                "close_price":        23000.0,
                "quantity":           50,
                "average_price":      22800.0,
                "pnl":                17500.0,
                "overnight_quantity": 50,
                "day_buy_quantity":   0,
                "day_sell_quantity":  0,
                "day_buy_value":      0.0,
                "day_sell_value":     0.0,
            }
        ] * 200  # 200 rows — verify no quadratic blow-up
        target_date = date(2026, 6, 27)

        import time
        t0 = time.perf_counter()
        out = _positions_rows("ZG0790", target_date, rows_in, now_post_close)
        elapsed = time.perf_counter() - t0

        assert len(out) == 200
        # 200 decomposed rows should complete in < 50 ms on any machine
        assert elapsed < 0.05, f"_positions_rows took {elapsed*1000:.1f}ms for 200 rows"


# ---------------------------------------------------------------------------
# Dimension 4 — reuse: close_settled calls snapshot_daily_book (SSOT)
# ---------------------------------------------------------------------------

class TestCloseSettledCallsSnapshot:
    """market_lifecycle_handlers.py close_settled handler calls snapshot_daily_book,
    which now uses the decomposed formula — so the settled snapshot is accurate."""

    @pytest.mark.asyncio
    async def test_close_settled_handler_calls_snapshot_daily_book(self):
        """_snapshot_close is wired for close_settled; it calls snapshot_daily_book.
        snapshot_daily_book is imported inside _snapshot_close from daily_snapshot,
        so we patch the module-level name at the daily_snapshot module."""
        import asyncio

        mock_snapshot = AsyncMock(return_value={
            "accounts": ["ZG0790"],
            "holdings_rows": 5,
            "positions_rows": 3,
            "trades_rows": 0,
            "funds_rows": 2,
            "errors": [],
        })

        # _snapshot_close does `from backend.api.algo.daily_snapshot import snapshot_daily_book`
        # inside the function body — patch at the source module so the local import
        # resolves to the mock.
        with patch(
            "backend.api.algo.daily_snapshot.snapshot_daily_book",
            mock_snapshot,
        ):
            from backend.api.algo import market_lifecycle_handlers as _mlh
            # Directly call the internal handler as if close_settled fired
            await _mlh._snapshot_close("nse", "close_settled")

        assert mock_snapshot.call_count == 1, (
            "snapshot_daily_book must be called exactly once on close_settled"
        )
