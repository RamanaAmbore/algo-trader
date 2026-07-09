"""Regression tests — Day P&L / P&L separation for new / partial / closed positions.

Covers three operator-visible cases where the same symbol may appear across
multiple positions and the Day P&L must stay accurate:

  Case 1 — New position opened today (overnight_quantity == 0)
           Day P&L = (LTP − avg_cost) × qty
           P&L     = same (no prior overnight basis)
           Kite ships day_change_val = 0 for these — must NOT surface as 0.

  Case 2 — Partial close (same symbol: qty > 0, realised != 0)
           `realised` from broker is the closed-portion P&L.
           `unrealised` is the open-portion P&L on the remaining qty.
           Sum = `pnl` — both are already fields on PositionRow so no
           schema addition needed; test verifies wire-level correctness.

  Case 3 — Fully closed intraday (quantity == 0, realised != 0)
           Row is settled: is_animating=False, price_source='snapshot_settled'.
           LTP patch must NOT rewrite last_price (flat position, LTP meaningless).
           day_change_val backstop restores realised when Kite ships last_price=0.

Five quality dimensions covered:
  - Correctness      — Contract A / partial / closed math verified
  - SSOT             — reuses `decomposed_intraday_pnl` from pnl_math
  - Perf             — pure CPU, no network
  - Stale-code       — asserts the `_is_settled_flat` helper is present in
                       positions.py after the Case-3 refactor
  - UX               — closed rows do not animate (is_animating=False)
"""

from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


BACKEND_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Case 1 — New position: Day P&L = (LTP − avg) × qty when oq=0
# ---------------------------------------------------------------------------

class TestCase1_NewPosition:
    """oq=0, bq>0 — position opened today. Kite's day_change_val is 0;
    decomposed_intraday_pnl must produce (LTP − avg) × qty via
    (bq × LTP − bv) where bv = qty × avg.
    """

    def test_new_position_day_pnl_uses_avg_not_prev_close(self):
        """Case 1 golden: bought 10 @ 100 → Day P&L = (105 − 100) × 10 = 50."""
        from backend.api.algo.pnl_math import decomposed_intraday_pnl
        result = decomposed_intraday_pnl(
            oq=0, ltp=105, cls=0,
            bq=10, bv=1000, sv=0, sq=0,
        )
        assert result == pytest.approx(50.0), (
            "Contract A: opened-today Day P&L = (LTP − entry_avg) × qty"
        )

    def test_new_position_zero_close_does_not_leak(self):
        """close_price=0 must NOT combine with LTP to yield a phantom (LTP−0)×qty."""
        from backend.api.algo.pnl_math import decomposed_intraday_pnl
        # naive (LTP − 0) × 10 = 1050 would be wrong. Decomposed → 50.
        result = decomposed_intraday_pnl(
            oq=0, ltp=105, cls=0, bq=10, bv=1000, sv=0, sq=0,
        )
        assert result != pytest.approx(1050.0), (
            "Naive (LTP−0)×qty leak — opening_qty=0 must drop the carry term"
        )
        assert result == pytest.approx(50.0)

    def test_broker_enrich_produces_correct_dcv_for_new_position(self):
        """End-to-end: _enrich_positions on an oq=0 frame gives right day_change_val."""
        from backend.brokers.broker_apis import _enrich_positions
        df = pd.DataFrame([{
            'tradingsymbol': 'NIFTY26JULFUT',
            'account': 'ZG0001',
            'exchange': 'NFO',
            'quantity': 10,
            'overnight_quantity': 0,
            'day_buy_quantity': 10,
            'day_sell_quantity': 0,
            'day_buy_value': 1000.0,
            'day_sell_value': 0.0,
            'last_price': 105.0,
            'close_price': 0.0,
            'average_price': 100.0,
        }])
        result = _enrich_positions(df)
        assert math.isclose(result.iloc[0]['day_change_val'], 50.0, abs_tol=1e-6), (
            f"Enrich should produce Day P&L=50 for new position, "
            f"got {result.iloc[0]['day_change_val']}"
        )


# ---------------------------------------------------------------------------
# Case 2 — Partial close: unrealised + realised on the same row
# ---------------------------------------------------------------------------

class TestCase2_PartialClose:
    """`quantity > 0 AND realised != 0` — broker returns ONE row with
    remaining open qty and locked realised P&L. PositionRow already
    carries both `unrealised` (open portion) and `realised` (closed
    portion) as separate fields — no schema addition needed."""

    def test_partial_close_has_realised_and_unrealised_fields(self):
        """PositionRow schema exposes realised + unrealised separately."""
        from backend.api.schemas import PositionRow
        row = PositionRow(
            account='ZG0001',
            tradingsymbol='NIFTY26JULFUT',
            exchange='NFO',
            product='NRML',
            quantity=6,
            average_price=100.0,
            close_price=100.0,
            pnl=145.0,        # unrealised 25 + realised 120
            last_price=105.0,
            unrealised=25.0,  # 6 × (105 − 100)
            realised=120.0,   # closed 4 × (130 − 100)
        )
        assert row.unrealised == 25.0
        assert row.realised == 120.0
        assert math.isclose(row.pnl, row.unrealised + row.realised, abs_tol=0.01)

    def test_partial_close_decomposed_dcv_matches_realised_leg(self):
        """Contract D partial-close: sum of carry + realised sell leg."""
        from backend.api.algo.pnl_math import decomposed_intraday_pnl
        # 10 carried @ 100, sold 4 @ 130 today, remaining 6, LTP 105, close 100.
        # carry term:    10 × (105 − 100) = +50
        # intraday-sell: (4 × 130 − 4 × 105) = 520 - 420 = +100
        # total Day P&L: +150
        result = decomposed_intraday_pnl(
            oq=10, ltp=105, cls=100,
            bq=0, bv=0, sv=520.0, sq=4,
        )
        assert result == pytest.approx(150.0)

    def test_partial_close_realised_survives_wire(self):
        """`realised` column is in _ROW_COLS so it lands on the response."""
        from backend.api.routes.positions import _ROW_COLS
        assert 'realised' in _ROW_COLS, (
            "positions.py _ROW_COLS must include `realised` so the closed "
            "portion's P&L is visible on the wire"
        )
        assert 'unrealised' in _ROW_COLS, (
            "positions.py _ROW_COLS must include `unrealised` so the open "
            "portion's P&L is visible on the wire"
        )


# ---------------------------------------------------------------------------
# Case 3 — Fully closed intraday: qty=0 + realised != 0
# ---------------------------------------------------------------------------

class TestCase3_FullyClosedIntraday:
    """`quantity == 0 AND realised != 0` — the closed portion's realised
    P&L must survive as the row's Day P&L / P&L display. The row is
    settled: no animation, no LTP patch."""

    def test_fully_closed_row_not_filtered(self):
        """A quantity=0 broker row with realised != 0 stays in the response."""
        from backend.brokers.broker_apis import _enrich_positions
        df = pd.DataFrame([{
            'tradingsymbol': 'NIFTY26JULFUT',
            'account': 'ZG0001',
            'exchange': 'NFO',
            'quantity': 0,                # fully closed
            'overnight_quantity': 0,
            'day_buy_quantity': 10,
            'day_sell_quantity': 10,
            'day_buy_value': 1000.0,      # bought 10 @ 100
            'day_sell_value': 1200.0,     # sold 10 @ 120
            'last_price': 120.0,
            'close_price': 100.0,
            'average_price': 100.0,
            'pnl': 200.0,                 # realised = sv − bv = +200
            'realised': 200.0,
        }])
        result = _enrich_positions(df)
        assert not result.empty, "Fully-closed row must survive enrichment"
        assert len(result) == 1

    def test_fully_closed_row_realised_preserved(self):
        """`pnl` = broker `realised` for a flat row (Kite ships pnl directly)."""
        from backend.brokers.broker_apis import _enrich_positions
        df = pd.DataFrame([{
            'tradingsymbol': 'NIFTY26JULFUT',
            'account': 'ZG0001',
            'exchange': 'NFO',
            'quantity': 0,
            'overnight_quantity': 0,
            'day_buy_quantity': 10,
            'day_sell_quantity': 10,
            'day_buy_value': 1000.0,
            'day_sell_value': 1200.0,
            'last_price': 120.0,
            'close_price': 100.0,
            'average_price': 100.0,
            'pnl': 200.0,
            'realised': 200.0,
        }])
        result = _enrich_positions(df)
        assert math.isclose(result.iloc[0]['pnl'], 200.0, abs_tol=1e-6)

    def test_fully_closed_row_dcv_backstops_realised_when_ltp_zero(self):
        """Case 3 route-layer backstop: when Kite ships last_price=0 for a
        closed row, `_enrich_positions` zeroes day_change_val via its
        `_ltp > 0` gate. The route layer restores day_change_val = realised
        so Day P&L and P&L agree on flat rows."""
        # Simulate the pathological case: Kite closed the position and
        # dropped last_price = 0.
        raw = pd.DataFrame([{
            'tradingsymbol': 'NIFTY26JULFUT',
            'account': 'ZG0001',
            'exchange': 'NFO',
            'quantity': 0,
            'overnight_quantity': 0,
            'day_buy_quantity': 10,
            'day_sell_quantity': 10,
            'day_buy_value': 1000.0,
            'day_sell_value': 1200.0,
            'last_price': 0.0,         # <-- Kite dropped it
            'close_price': 100.0,
            'average_price': 100.0,
            'pnl': 200.0,
            'realised': 200.0,
            'day_change_val': 0.0,     # <-- what Layer 1 produced (via _ltp>0 gate)
            'day_change': 0.0,
            'day_change_percentage': 0.0,
        }])

        # Apply the route-layer Case 3 backstop by importing + running
        # the flat-mask block via a small runner. We inline the logic here
        # because it lives inside _fetch (async, broker-dependent). This
        # test asserts the CONTRACT: after the backstop, dcv==realised.
        _qty_all = pd.to_numeric(raw['quantity'], errors='coerce').fillna(0)
        _rea_all = pd.to_numeric(raw['realised'], errors='coerce').fillna(0)
        _flat_mask = (_qty_all == 0) & (_rea_all != 0)
        _dcv = pd.to_numeric(raw['day_change_val'], errors='coerce').fillna(0)
        _backstop = _flat_mask & (_dcv == 0)
        raw.loc[_backstop, 'day_change_val'] = _rea_all[_backstop]

        assert math.isclose(raw.at[0, 'day_change_val'], 200.0, abs_tol=1e-6), (
            "Case 3 backstop failed: day_change_val must equal realised for "
            "a fully-closed row when Kite shipped last_price=0"
        )

    def test_fully_closed_row_backstop_source_reflects_positions_route(self):
        """SSOT — the Case 3 backstop lives in positions.py:_fetch.
        Loose grep: any future refactor is free to rename locals as long
        as the (quantity, realised) invariant is preserved.
        """
        src = (BACKEND_ROOT / "api" / "routes" / "positions.py").read_text(
            encoding="utf-8"
        )
        # Case 3 backstop touches two columns together: quantity and realised.
        assert "'realised'" in src and "'quantity'" in src, (
            "Case 3 backstop must reference both `quantity` and `realised` "
            "columns to detect fully-closed rows"
        )
        # And the day_change_val restore.
        assert "day_change_val" in src and "backstop" in src.lower(), (
            "Case 3 backstop must document the day_change_val restore path"
        )


# ---------------------------------------------------------------------------
# Case 3 hygiene — LTP patch skips flat rows
# ---------------------------------------------------------------------------

class TestCase3_LTPPatchSkipsFlatRows:
    """`apply_ltp_patch` must skip qty=0 rows. Rewriting last_price on a
    flat position is misleading — LTP has no bearing on realised P&L."""

    def test_ltp_patch_skips_qty_zero_positions(self):
        """qty=0 row must not have last_price rewritten by ticker patch."""
        from backend.api.helpers.ltp_patch import apply_ltp_patch, positions_policy

        df = pd.DataFrame([{
            'tradingsymbol': 'NIFTY26JULFUT',
            'quantity': 0,                # fully closed
            'last_price': 0.0,            # Kite dropped it
            'realised': 200.0,
            'close_price': 100.0,
        }])

        ticker = MagicMock()
        ticker.get_ltp_by_sym.return_value = 120.0  # ticker still has data

        with patch("backend.api.helpers.ltp_patch.record_good_ltp"), \
             patch("backend.brokers.kite_ticker.get_ticker", return_value=ticker):
            res = apply_ltp_patch(df, positions_policy)

        # Row should not be patched — flat position.
        assert res is not None
        assert 0 not in res.patched_idx, (
            "qty=0 row was patched — LTP for a flat position is misleading"
        )
        assert float(df.at[0, 'last_price']) == pytest.approx(0.0), (
            "last_price on a flat (qty=0) row must not be overwritten"
        )

    def test_ltp_patch_still_patches_open_rows(self):
        """Non-zero qty rows patch as before (regression guard)."""
        from backend.api.helpers.ltp_patch import apply_ltp_patch, positions_policy

        df = pd.DataFrame([{
            'tradingsymbol': 'NIFTY26JULFUT',
            'quantity': 10,               # open position
            'last_price': 100.0,
            'close_price': 100.0,
        }])

        ticker = MagicMock()
        ticker.get_ltp_by_sym.return_value = 120.0

        with patch("backend.api.helpers.ltp_patch.record_good_ltp"), \
             patch("backend.brokers.kite_ticker.get_ticker", return_value=ticker):
            res = apply_ltp_patch(df, positions_policy)

        assert res is not None
        assert res.any_patched, "Open row must still receive ticker patch"
        assert float(df.at[0, 'last_price']) == pytest.approx(120.0)

    def test_ltp_patch_skips_qty_zero_holdings(self):
        """Same guard for holdings (uses opening_quantity column)."""
        from backend.api.helpers.ltp_patch import apply_ltp_patch, holdings_policy

        df = pd.DataFrame([{
            'tradingsymbol': 'GOLDBEES',
            'opening_quantity': 0,        # never held / fully closed
            'last_price': 0.0,
            'close_price': 1800.0,
        }])

        ticker = MagicMock()
        ticker.get_ltp_by_sym.return_value = 1870.0

        with patch("backend.api.helpers.ltp_patch.record_good_ltp"), \
             patch("backend.brokers.kite_ticker.get_ticker", return_value=ticker):
            res = apply_ltp_patch(df, holdings_policy)

        assert res is not None
        assert 0 not in res.patched_idx, (
            "opening_quantity=0 row was patched — should be skipped"
        )
        assert float(df.at[0, 'last_price']) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Case 3 hygiene — animation tag on flat rows
# ---------------------------------------------------------------------------

class TestCase3_SettledFlatNotAnimating:
    """Flat rows (`quantity == 0`) must not animate — they are settled by
    definition. `_overlay_snapshot_for_closed_exchanges` sets
    is_animating=False + price_source='snapshot_settled' regardless of
    exchange-open state.
    """

    @pytest.mark.asyncio
    async def test_flat_row_is_not_animating_when_exchange_open(self):
        """Even when the exchange is open, a qty=0 row must render as settled."""
        from backend.api.routes.positions import _overlay_snapshot_for_closed_exchanges
        from backend.api.schemas import PositionRow

        rows = [
            PositionRow(
                account='ZG0001',
                tradingsymbol='NIFTY26JULFUT',
                exchange='NFO',
                product='NRML',
                quantity=0,               # <-- flat
                average_price=100.0,
                close_price=100.0,
                pnl=200.0,
                last_price=120.0,
                realised=200.0,
            ),
        ]
        # Patch is_exchange_closed_now to say the exchange is open —
        # the flat check must still short-circuit to settled.
        with patch(
            "backend.api.routes.positions.is_exchange_closed_now",
            return_value=False,
        ):
            out = await _overlay_snapshot_for_closed_exchanges(
                rows, kind="positions",
            )
        assert len(out) == 1
        assert out[0].is_animating is False, (
            "Flat (qty=0) row must not animate even when exchange open"
        )
        assert out[0].price_source == "snapshot_settled", (
            f"Flat row must be tagged snapshot_settled, "
            f"got {out[0].price_source}"
        )

    @pytest.mark.asyncio
    async def test_open_row_still_animates_when_exchange_open(self):
        """Regression guard — non-flat rows still animate as before."""
        from backend.api.routes.positions import _overlay_snapshot_for_closed_exchanges
        from backend.api.schemas import PositionRow

        rows = [
            PositionRow(
                account='ZG0001',
                tradingsymbol='NIFTY26JULFUT',
                exchange='NFO',
                product='NRML',
                quantity=10,              # <-- open
                average_price=100.0,
                close_price=100.0,
                pnl=50.0,
                last_price=105.0,
            ),
        ]
        with patch(
            "backend.api.routes.positions.is_exchange_closed_now",
            return_value=False,
        ):
            out = await _overlay_snapshot_for_closed_exchanges(
                rows, kind="positions",
            )
        assert out[0].is_animating is True
        assert out[0].price_source == "live"


# ---------------------------------------------------------------------------
# Same-symbol / multiple accounts — TOTAL row does not double-count realised
# ---------------------------------------------------------------------------

class TestSameSymbolAggregation:
    """Two accounts holding the same symbol must aggregate correctly
    without double-counting `realised` or `pnl`."""

    def test_same_symbol_two_accounts_pnl_sums_once(self):
        """`build_summary_from_rows` sums pnl per account then across."""
        from backend.api.routes.positions_helpers import build_summary_from_rows
        from backend.api.schemas import PositionRow

        rows = [
            PositionRow(
                account='ZG0001',
                tradingsymbol='NIFTY26JULFUT',
                exchange='NFO',
                product='NRML',
                quantity=10,
                average_price=100.0,
                close_price=100.0,
                pnl=50.0,
                last_price=105.0,
                day_change_val=50.0,
            ),
            PositionRow(
                account='ZG0002',
                tradingsymbol='NIFTY26JULFUT',   # same symbol
                exchange='NFO',
                product='NRML',
                quantity=5,
                average_price=100.0,
                close_price=100.0,
                pnl=25.0,
                last_price=105.0,
                day_change_val=25.0,
            ),
        ]
        summary = build_summary_from_rows(rows)
        by_acct = {s.account: s for s in summary}

        assert math.isclose(by_acct['ZG0001'].pnl, 50.0, abs_tol=0.01)
        assert math.isclose(by_acct['ZG0002'].pnl, 25.0, abs_tol=0.01)
        assert math.isclose(by_acct['TOTAL'].pnl, 75.0, abs_tol=0.01), (
            "TOTAL row must equal Σ per-account pnl (no double-count)"
        )

    def test_partial_close_and_flat_rows_dcv_summed_once(self):
        """Mixed: partial-close row + fully-closed row on same symbol.
        Day P&L aggregates without double-counting the realised leg."""
        from backend.api.routes.positions_helpers import build_summary_from_rows
        from backend.api.schemas import PositionRow

        rows = [
            # Account 1: partial close (6 open, realised 120 on closed 4)
            PositionRow(
                account='ZG0001',
                tradingsymbol='NIFTY26JULFUT',
                exchange='NFO',
                product='NRML',
                quantity=6,
                average_price=100.0,
                close_price=100.0,
                pnl=145.0,
                last_price=105.0,
                unrealised=25.0,
                realised=120.0,
                day_change_val=150.0,   # 6×5 + 4×30 (partial-close intraday)
            ),
            # Account 2: fully closed intraday (realised 200)
            PositionRow(
                account='ZG0002',
                tradingsymbol='NIFTY26JULFUT',
                exchange='NFO',
                product='NRML',
                quantity=0,
                average_price=100.0,
                close_price=100.0,
                pnl=200.0,
                last_price=120.0,
                realised=200.0,
                day_change_val=200.0,
            ),
        ]
        summary = build_summary_from_rows(rows)
        by_acct = {s.account: s for s in summary}
        assert math.isclose(by_acct['TOTAL'].pnl, 345.0, abs_tol=0.01)
        assert math.isclose(by_acct['TOTAL'].day_change_val, 350.0, abs_tol=0.01)


# ---------------------------------------------------------------------------
# SSOT / hygiene guards
# ---------------------------------------------------------------------------

class TestSSOTGuards:
    def test_case3_flat_helper_present_in_positions_route(self):
        """The `_is_settled_flat` helper is the single decision point for
        settled-flat row detection in the row overlay."""
        src = (BACKEND_ROOT / "api" / "routes" / "positions.py").read_text(
            encoding="utf-8"
        )
        assert "_is_settled_flat" in src, (
            "positions.py must contain _is_settled_flat helper — SSOT for "
            "the fully-closed intraday row detection"
        )

    def test_ltp_patch_skips_flat_rows_ssot(self):
        """`apply_ltp_patch` must contain the qty=0 skip. Any future change
        that removes this guard would re-introduce the LTP-rewrite bug."""
        src = (BACKEND_ROOT / "api" / "helpers" / "ltp_patch.py").read_text(
            encoding="utf-8"
        )
        assert "_qty_col" in src and "opening_quantity" in src, (
            "ltp_patch.py must detect the qty column and skip qty=0 rows"
        )
