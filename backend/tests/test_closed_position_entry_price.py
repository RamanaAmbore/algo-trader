"""test_closed_position_entry_price.py

2026-11-05 live operator report: "lots, price, avg for positions
closed are shown as zero. keep the correct values." Kite zeroes both
`average_price` and the lot count once a position's net quantity goes
flat (qty=0) — documented broker behaviour, not a signal that no entry
data exists. The row's own whole-life `buy_price`/`sell_price`/
`buy_quantity`/`sell_quantity` fields still carry the real entry price
and opening size for whichever side opened the position.

Covers all three call sites touched by the fix:
  1. pnl_math.closed_position_entry_price / closed_position_entry_lots
     — the shared derivation, unit-tested directly.
  2. daily_snapshot.py:_positions_build_row — the daily_book write path.
  3. positions.py:_restore_closed_position_avg_cost — the live-fetch
     read path (the one most likely active when the operator is looking
     at a closed position during market hours).
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.api.algo.pnl_math import (
    closed_position_entry_price,
    closed_position_entry_lots,
)


# ─────────────────────────────────────────────────────────────────────────
# 1. closed_position_entry_price / closed_position_entry_lots — pure
# ─────────────────────────────────────────────────────────────────────────

class TestClosedPositionEntryPrice:
    def test_long_position_entry_is_the_buy_side(self):
        """A long position (oq_raw >= 0) was OPENED by buying — entry
        price is the buy side's whole-life VWAP, not the sell side."""
        r = {"buy_quantity": 1, "buy_price": 455.2, "buy_value": 45520.0,
             "sell_quantity": 1, "sell_price": 80.1, "sell_value": 8010.0}
        assert closed_position_entry_price(r, 0.0, oq_raw=100) == 455.2

    def test_short_position_entry_is_the_sell_side(self):
        """A short position (oq_raw < 0) was OPENED by selling — entry
        price is the sell side's whole-life VWAP, not the buy side."""
        r = {"buy_quantity": 2, "buy_price": 46.8, "buy_value": 9360.0,
             "sell_quantity": 2, "sell_price": 225.3, "sell_value": 45060.0}
        assert closed_position_entry_price(r, 0.0, oq_raw=-200) == 225.3

    def test_nonzero_avg_px_is_returned_unchanged(self):
        """A genuinely non-zero average_price (not the broken flat-
        position case this fix targets) is trusted as-is — never
        overridden by the buy/sell-side derivation."""
        r = {"buy_quantity": 1, "buy_price": 999.0, "sell_quantity": 1, "sell_price": 1.0}
        assert closed_position_entry_price(r, 123.45, oq_raw=100) == 123.45

    def test_no_buy_sell_data_falls_back_to_avg_px(self):
        """Missing/zero buy-sell data must never fabricate an entry
        price — falls back to whatever avg_px already was (0 for the
        genuinely-broken case)."""
        assert closed_position_entry_price({}, 0.0, oq_raw=100) == 0.0
        assert closed_position_entry_price(
            {"buy_quantity": 0, "buy_price": 455.2}, 0.0, oq_raw=100,
        ) == 0.0

    def test_long_entry_lots_reads_buy_quantity(self):
        assert closed_position_entry_lots({"buy_quantity": 2, "sell_quantity": 5}, oq_raw=100) == 2.0

    def test_short_entry_lots_reads_sell_quantity(self):
        assert closed_position_entry_lots({"buy_quantity": 2, "sell_quantity": 5}, oq_raw=-50) == 5.0

    def test_entry_lots_none_when_missing(self):
        assert closed_position_entry_lots({}, oq_raw=100) is None


# ─────────────────────────────────────────────────────────────────────────
# 2. daily_snapshot.py:_positions_build_row — the daily_book write path
# ─────────────────────────────────────────────────────────────────────────

class TestPositionsBuildRowClosedEntry:
    def test_closed_mcx_short_position_gets_real_avg_cost_and_lots(self):
        """Live-shape repro: an MCX short CRUDEOIL leg closed out today.
        Kite reports average_price=0, quantity=0 (net flat) — the row
        must still carry the real entry avg_cost and lot count, not 0."""
        from datetime import date
        from backend.api.algo.daily_snapshot import _positions_build_row

        r = {
            "average_price": 0, "overnight_quantity": -2,
            "sell_quantity": 2, "sell_price": 225.3, "sell_value": 45060.0,
            "buy_quantity": 2, "buy_price": 46.8, "buy_value": 9360.0,
            "day_sell_quantity": 0, "day_sell_value": 0,
            "day_buy_quantity": 2, "day_buy_value": 9360.0,
        }
        row = _positions_build_row(
            r, account="ZG0790", target_date=date(2026, 11, 5),
            exchange="MCX", symbol="CRUDEOIL26NOV10000CE",
            qty_contracts=0, lots_val=0, lot_size_val=100,
            ltp_val=45.8, day_pnl=0.0,
            oq_raw=-200.0, avg_px=0.0, pos_close_ref=None,
            settled=True, multiplier=100,
        )
        assert row["avg_cost"] == 225.3, f"expected entry price 225.3, got {row['avg_cost']!r}"
        assert row["lots"] == 2, f"expected 2 lots preserved, got {row['lots']!r}"

    def test_open_position_avg_cost_is_untouched_passthrough(self):
        """A genuinely OPEN position (qty_contracts != 0) must keep the
        plain average_price passthrough — this fix only changes the
        qty_contracts == 0 branch."""
        from datetime import date
        from backend.api.algo.daily_snapshot import _positions_build_row

        r = {"average_price": 150.25, "overnight_quantity": 1}
        row = _positions_build_row(
            r, account="ZG0790", target_date=date(2026, 11, 5),
            exchange="NFO", symbol="NIFTY26NOV25000CE",
            qty_contracts=75, lots_val=1, lot_size_val=75,
            ltp_val=160.0, day_pnl=100.0,
            oq_raw=1.0, avg_px=150.25, pos_close_ref=None,
            settled=False, multiplier=1,
        )
        assert row["avg_cost"] == 150.25
        assert row["lots"] == 1


# ─────────────────────────────────────────────────────────────────────────
# 3. positions.py:_restore_closed_position_avg_cost — the live-fetch path
# ─────────────────────────────────────────────────────────────────────────

class TestRestoreClosedPositionAvgCostLive:
    def test_flat_row_average_price_restored_from_sell_side(self):
        from backend.api.routes.positions import _restore_closed_position_avg_cost

        raw = pd.DataFrame([{
            "quantity": 0, "average_price": 0.0, "overnight_quantity": -200,
            "buy_quantity": 2, "buy_price": 46.8,
            "sell_quantity": 2, "sell_price": 225.3,
        }])
        _restore_closed_position_avg_cost(raw)
        assert raw.loc[0, "average_price"] == pytest.approx(225.3)

    def test_open_row_is_never_touched(self):
        from backend.api.routes.positions import _restore_closed_position_avg_cost

        raw = pd.DataFrame([{
            "quantity": 75, "average_price": 150.25, "overnight_quantity": 1,
            "buy_quantity": 999, "buy_price": 1.0,
            "sell_quantity": 999, "sell_price": 1.0,
        }])
        _restore_closed_position_avg_cost(raw)
        assert raw.loc[0, "average_price"] == 150.25

    def test_missing_buy_sell_columns_is_a_safe_noop(self):
        from backend.api.routes.positions import _restore_closed_position_avg_cost

        raw = pd.DataFrame([{"quantity": 0, "average_price": 0.0}])
        _restore_closed_position_avg_cost(raw)  # must not raise
        assert raw.loc[0, "average_price"] == 0.0

    def test_empty_frame_is_a_safe_noop(self):
        from backend.api.routes.positions import _restore_closed_position_avg_cost

        raw = pd.DataFrame()
        _restore_closed_position_avg_cost(raw)  # must not raise


# ─────────────────────────────────────────────────────────────────────────
# 4. positions.py:_restore_closed_position_lots — derivatives Legs grid fix
# ─────────────────────────────────────────────────────────────────────────

class TestRestoreClosedPositionLotsLive:
    """2026-11-06 live operator report: "lots and qty for close positions
    in derivatives legs grid is zero. can you keep the correct value."

    `quantity` genuinely IS 0 for a closed position (correct, unchanged).
    `lots` is the entry lot count — Kite zeroes it the same way it zeroes
    `average_price`, and `broker_apis.py:_annotate_lot_size` (which runs
    per-account, BEFORE this row reaches positions.py) computes `lots`
    from that already-zeroed `quantity`, so its own `lots` column is also
    wrongly 0 for a closed row — this is the live-fetch-path sibling of
    `_restore_closed_position_avg_cost` above, same mirrored derivation
    `daily_snapshot.py:_positions_build_row` already uses for the
    daily_book write path."""

    def test_closed_mcx_row_lots_restored_from_whole_life_lot_field(self):
        """Real MCX shape: multiplier>1 means Kite ships buy_quantity/
        sell_quantity in LOTS already (unscaled by _annotate_lot_size,
        which only scales overnight_quantity/day_buy_quantity/
        day_sell_quantity) — the entry lot count is that value directly,
        no further division by lot_size."""
        from backend.api.routes.positions import _restore_closed_position_lots

        raw = pd.DataFrame([{
            "quantity": 0, "lot_size": 100, "multiplier": 100,
            "overnight_quantity": -200,
            "buy_quantity": 2, "buy_price": 46.8,
            "sell_quantity": 2, "sell_price": 225.3,
        }])
        _restore_closed_position_lots(raw)
        assert raw.loc[0, "lots"] == 2, f"expected 2 entry lots, got {raw.loc[0, 'lots']!r}"

    def test_closed_nfo_row_lots_derived_via_lot_size_division(self):
        """Non-MCX (multiplier==1): buy_quantity/sell_quantity are
        already in contracts — entry lots = contracts // lot_size."""
        from backend.api.routes.positions import _restore_closed_position_lots

        raw = pd.DataFrame([{
            "quantity": 0, "lot_size": 75, "multiplier": 1,
            "overnight_quantity": 150,
            "buy_quantity": 150, "buy_price": 112.5,
            "sell_quantity": 0, "sell_price": 0.0,
        }])
        _restore_closed_position_lots(raw)
        assert raw.loc[0, "lots"] == 2, f"expected 150/75=2 entry lots, got {raw.loc[0, 'lots']!r}"

    def test_open_row_lots_is_never_touched(self):
        """A genuinely OPEN row (quantity != 0) keeps whatever `lots`
        value `_annotate_lot_size` already computed upstream — this fix
        only touches the flat (quantity == 0) branch."""
        from backend.api.routes.positions import _restore_closed_position_lots

        raw = pd.DataFrame([{
            "quantity": 75, "lots": 1, "lot_size": 75, "multiplier": 1,
            "overnight_quantity": 1,
            "buy_quantity": 1, "buy_price": 999.0,
            "sell_quantity": 0, "sell_price": 0.0,
        }])
        _restore_closed_position_lots(raw)
        assert raw.loc[0, "lots"] == 1

    def test_missing_lot_size_column_is_a_safe_noop(self):
        from backend.api.routes.positions import _restore_closed_position_lots

        raw = pd.DataFrame([{"quantity": 0}])
        _restore_closed_position_lots(raw)  # must not raise
        assert "lots" not in raw.columns or raw.loc[0, "lots"] is None

    def test_empty_frame_is_a_safe_noop(self):
        from backend.api.routes.positions import _restore_closed_position_lots

        raw = pd.DataFrame()
        _restore_closed_position_lots(raw)  # must not raise
