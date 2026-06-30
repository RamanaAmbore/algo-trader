"""Day P&L correctness contracts — Contract A/B/C/D position state transitions.

Operator's correctness contracts:

  Contract A — OPENED today (no carry from yesterday).
       Day P&L = (LTP − entry_price) × buy_qty_today
       (NOT (LTP − prev_close) × qty — prev_close has no meaning for a
        position that did not exist yesterday).

  Contract B — OPENED and CLOSED today (intraday round-trip).
       Day P&L = (exit_price − entry_price) × closed_qty
       Sign: positive for profitable long round-trip.

  Contract C — CARRIED from yesterday (no transactions today).
       Day P&L = (LTP − prev_close) × opening_qty
       prev_close is the previous session's authoritative close
       (from `daily_book.ltp` — Kite's `close_price` lags overnight).

  Contract D — COMBINED (most common case: partial carry + intraday legs).
       Day P&L = opening_qty × (LTP − prev_close)
               + (day_buy_qty × LTP − day_buy_value)
               + (day_sell_value − day_sell_qty × LTP)

All four contracts must be satisfied by `decomposed_intraday_pnl` exactly —
no special-case branches. The polars expression in
`backend/brokers/broker_apis.py:_enrich_positions` and the pandas helper in
`backend/api/routes/positions.py:_compute_day_change_val` both delegate
to the SSOT, so this spec exercises the helper directly + asserts the
end-to-end behaviour of `_enrich_positions` on a multi-row reference
frame covering all four contracts (and edge cases).
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from backend.api.algo.pnl_math import decomposed_intraday_pnl, naive_day_pnl


# ---------------------------------------------------------------------------
# Contract A — Opened today (no carry)
# ---------------------------------------------------------------------------

class TestContractA_OpenedToday:
    """oq=0, bq>0, sq=0 — fresh long opened mid-session."""

    def test_long_opened_today_unrealized_gain(self):
        # Bought 10 @ 100, LTP now 105 → expected +50
        result = decomposed_intraday_pnl(
            oq=0, ltp=105, cls=0, bq=10, bv=1000, sv=0, sq=0,
        )
        assert result == pytest.approx(50.0), (
            "Contract A: opened-today Day P&L = (LTP − entry) × buy_qty"
        )

    def test_long_opened_today_unrealized_loss(self):
        # Bought 5 @ 200, LTP now 190 → expected -50
        result = decomposed_intraday_pnl(
            oq=0, ltp=190, cls=0, bq=5, bv=1000, sv=0, sq=0,
        )
        assert result == pytest.approx(-50.0)

    def test_short_opened_today(self):
        # Sold 10 @ 100 (open short), LTP now 95 (move down → profit)
        # No carry, no buys today. sv=1000, sq=10.
        result = decomposed_intraday_pnl(
            oq=0, ltp=95, cls=0, bq=0, bv=0, sv=1000, sq=10,
        )
        # = 0 + 0 + (1000 - 10*95) = 1000 - 950 = +50
        assert result == pytest.approx(50.0), (
            "Contract A: short-opened intraday profit = (entry − LTP) × sell_qty"
        )

    def test_prev_close_must_not_affect_opened_today(self):
        # If a stale `cls` slipped through from an unrelated symbol, the
        # formula should still come out right because oq=0 nullifies the
        # carry term entirely. Sanity-check the algebra.
        result_no_close = decomposed_intraday_pnl(
            oq=0, ltp=105, cls=0,  bq=10, bv=1000, sv=0, sq=0,
        )
        result_w_close = decomposed_intraday_pnl(
            oq=0, ltp=105, cls=200, bq=10, bv=1000, sv=0, sq=0,
        )
        assert result_no_close == result_w_close == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Contract B — Opened AND closed today (round-trip)
# ---------------------------------------------------------------------------

class TestContractB_RoundTripToday:
    """oq=0, bq>0, sq>0 — fresh long fully closed same day."""

    def test_long_round_trip_profitable(self):
        # Bought 10 @ 100, sold 10 @ 105 → +50 realised
        result = decomposed_intraday_pnl(
            oq=0, ltp=105, cls=0, bq=10, bv=1000, sv=1050, sq=10,
        )
        # = 0 + (10*105 - 1000) + (1050 - 10*105) = 50 + 0 = +50
        assert result == pytest.approx(50.0), (
            "Contract B: round-trip Day P&L = (exit − entry) × qty"
        )

    def test_long_round_trip_loss(self):
        # Bought 5 @ 200, sold 5 @ 190 → -50
        result = decomposed_intraday_pnl(
            oq=0, ltp=190, cls=0, bq=5, bv=1000, sv=950, sq=5,
        )
        assert result == pytest.approx(-50.0)

    def test_round_trip_ltp_irrelevant_when_flat(self):
        # When qty net is zero (fully closed), LTP only shows up as the
        # cancellation `bq×LTP − sq×LTP`. The formula naturally cancels
        # those terms because bq = sq.
        for ltp in (1.0, 50.0, 999.0):
            result = decomposed_intraday_pnl(
                oq=0, ltp=ltp, cls=0, bq=10, bv=1000, sv=1050, sq=10,
            )
            assert result == pytest.approx(50.0), (
                f"Round-trip P&L stable across LTP — got {result} at ltp={ltp}"
            )


# ---------------------------------------------------------------------------
# Contract C — Carried, no transactions today
# ---------------------------------------------------------------------------

class TestContractC_CarriedOnly:
    """oq>0, bq=0, sq=0 — pure overnight, no intraday legs."""

    def test_carried_up_move(self):
        # 100 carried, prev_close 100, LTP 102 → +200
        result = decomposed_intraday_pnl(
            oq=100, ltp=102, cls=100, bq=0, bv=0, sv=0, sq=0,
        )
        assert result == pytest.approx(200.0)

    def test_carried_down_move(self):
        result = decomposed_intraday_pnl(
            oq=50, ltp=98, cls=100, bq=0, bv=0, sv=0, sq=0,
        )
        assert result == pytest.approx(-100.0)


# ---------------------------------------------------------------------------
# Contract D — Combined (partial carry + intraday)
# ---------------------------------------------------------------------------

class TestContractD_Combined:
    """oq>0 AND (bq>0 OR sq>0) — carry plus today's legs."""

    def test_carry_plus_buy_more(self):
        # 100 carried @ unspecified entry (prev_close 100), bought 50 more
        # @ 101 today, LTP now 102.
        # carry term:    100 * (102 - 100) = +200
        # intraday-buy:  (50*102 - 50*101) = +50
        # total: +250
        result = decomposed_intraday_pnl(
            oq=100, ltp=102, cls=100, bq=50, bv=5050, sv=0, sq=0,
        )
        assert result == pytest.approx(250.0)

    def test_carry_plus_partial_close(self):
        # 100 carried, sold 40 today @ 102, LTP 101, prev_close 100.
        # carry term:    100 * (101 - 100) = +100
        # intraday-sell: (40*102 - 40*101) = +40
        # total: +140
        # net qty held = 60
        result = decomposed_intraday_pnl(
            oq=100, ltp=101, cls=100, bq=0, bv=0, sv=40*102, sq=40,
        )
        assert result == pytest.approx(140.0), (
            "Contract D partial-close: realised + unrealised sum"
        )

    def test_full_close_of_carry_with_extra_intraday_buy(self):
        # 100 carried, sold all 100 @ 105 today, then bought 50 fresh @ 103,
        # LTP 104. prev_close 100.
        # carry term:    100 * (104 - 100) = +400
        # intraday-buy:  (50*104  - 50*103) = +50
        # intraday-sell: (100*105 - 100*104) = +100
        # total: +550
        result = decomposed_intraday_pnl(
            oq=100, ltp=104, cls=100,
            bq=50, bv=50*103, sv=100*105, sq=100,
        )
        assert result == pytest.approx(550.0)


# ---------------------------------------------------------------------------
# Edge cases mentioned in the operator's spec
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_zero_close_does_not_distort_opened_today(self):
        """Contract A guarantee: a missing `cls` (= 0) for a fresh-opened
        position must NOT translate into a phantom (LTP − 0) × qty value.
        Algebra: oq=0 kills the carry term, so cls drops out completely.
        """
        result_close_zero = decomposed_intraday_pnl(
            oq=0, ltp=105, cls=0, bq=10, bv=1000, sv=0, sq=0,
        )
        # The naive (LTP − close) × qty formula would yield 1050 here.
        # The decomposed formula must yield (105−100) × 10 = 50.
        assert result_close_zero == pytest.approx(50.0)
        assert result_close_zero != pytest.approx(1050.0), (
            "Naive (LTP−0)*qty leak — opening_qty=0 must drop the carry term"
        )

    def test_naive_fallback_for_carried_only(self):
        # `naive_day_pnl` is used only when the intraday columns aren't
        # present (Dhan / Groww adapters). It assumes the entire `qty` is
        # carried — same as `oq` in the decomposed path.
        assert naive_day_pnl(ltp=102, cls=100, qty=50) == pytest.approx(100.0)

    def test_short_carry_with_buy_to_cover(self):
        # Short 50 carried (oq = -50). Prev close 100. Bought 50 to cover
        # @ 95 today. LTP 96.
        # carry term:    -50 * (96 - 100) = +200
        # intraday-buy:  (50*96 - 50*95)  = +50
        # total: +250
        result = decomposed_intraday_pnl(
            oq=-50, ltp=96, cls=100, bq=50, bv=50*95, sv=0, sq=0,
        )
        assert result == pytest.approx(250.0)

    def test_multiple_legs_opened_today(self):
        # 0 carry, bought 5 @ 100, then 5 more @ 102, LTP 103.
        # bq=10, bv=5*100 + 5*102 = 1010
        # = (10*103 - 1010) = 1030 - 1010 = +20
        result = decomposed_intraday_pnl(
            oq=0, ltp=103, cls=0, bq=10, bv=1010, sv=0, sq=0,
        )
        assert result == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# End-to-end: full _enrich_positions pipeline produces correct day_change_val
# ---------------------------------------------------------------------------

class TestEnrichPositionsContracts:
    """Run a multi-row reference frame through the real _enrich_positions
    pipeline and verify day_change_val matches each contract.

    Note: _enrich_positions reads a `multiplier` column path in
    `_fetch_positions_local` to scale MCX lot-units, but that runs BEFORE
    `_enrich_positions`. The enrich helper itself only sees the
    already-scaled `quantity`, `overnight_quantity`, etc. So we can feed
    pre-scaled rows directly.
    """

    def _frame(self) -> pd.DataFrame:
        # Six rows cover: A long open, A short open, B round-trip,
        # C carry up, C carry down, D combined.
        return pd.DataFrame({
            'tradingsymbol':       ['A_LONG', 'A_SHORT', 'B_ROUND', 'C_UP', 'C_DOWN', 'D_COMBO'],
            'account':             ['ZG0001'] * 6,
            'exchange':            ['NSE'] * 6,
            'last_price':          [105.0,    95.0,    105.0,   102.0, 98.0,   101.0],
            'close_price':         [0.0,      0.0,     0.0,     100.0, 100.0,  100.0],
            'average_price':       [100.0,    100.0,   100.0,   100.0, 100.0,  100.0],
            'quantity':            [10,       -10,     0,       100,   50,     60],
            'overnight_quantity':  [0,        0,       0,       100,   50,     100],
            'day_buy_quantity':    [10,       0,       10,      0,     0,      0],
            'day_buy_value':       [1000.0,   0.0,     1000.0,  0.0,   0.0,    0.0],
            'day_sell_quantity':   [0,        10,      10,      0,     0,      40],
            'day_sell_value':      [0.0,      1000.0,  1050.0,  0.0,   0.0,    40*102.0],
        })

    def test_enrich_positions_produces_contract_values(self):
        from backend.brokers.broker_apis import _enrich_positions
        df = self._frame()
        result = _enrich_positions(df)

        # Map: tradingsymbol → expected day_change_val
        expected = {
            'A_LONG':  50.0,   # (105-100) * 10
            'A_SHORT': 50.0,   # (100-95) * 10
            'B_ROUND': 50.0,   # (105-100) * 10
            'C_UP':    200.0,  # (102-100) * 100
            'C_DOWN':  -100.0, # (98-100) * 50
            'D_COMBO': 140.0,  # carry 100 + sell-leg 40
        }
        actual = dict(zip(result['tradingsymbol'], result['day_change_val']))
        for sym, exp in expected.items():
            assert math.isclose(actual[sym], exp, abs_tol=1e-6), (
                f"{sym}: enrich produced day_change_val={actual[sym]}, "
                f"expected {exp}"
            )
