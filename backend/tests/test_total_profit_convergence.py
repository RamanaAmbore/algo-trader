"""Regression guard: the two independent total-profit computations —
`backend/api/algo/daily_snapshot.py:_positions_row_total_pnl` (writes
daily_book.total_pnl, tomorrow's base_pnl) and
`backend/brokers/broker_apis.py:_enrich_positions` (the live-fetch `pnl`
column consumed by every route/NAV/rollup) — must agree on the same
synthetic broker row.

This guards against the two implementations drifting apart again, the
exact historical bug this Day P&L / Exp P&L redesign fixes (daily_snapshot
computed `pnl + realised`; broker_apis computed `pnl + realised` too —
both double-counted for Kite, whose native `pnl` already equals
`realised + unrealised`).
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backend.api.algo.daily_snapshot import _positions_row_total_pnl
from backend.api.algo.pnl_math import current_total_profit
from backend.brokers.broker_apis import _enrich_positions


def _kite_row(realised: float, unrealised: float) -> dict:
    """A synthetic Kite position row where `pnl` is native and, per Kite's
    confirmed invariant, exactly equals realised + unrealised."""
    pnl = realised + unrealised
    return {
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "quantity": 10,
        "overnight_quantity": 10,
        "average_price": 2400.0,
        "last_price": 2450.0,
        "prev_close": 2420.0,
        "close_price": 2420.0,
        "pnl": pnl,
        "realised": realised,
        "unrealised": unrealised,
        "day_buy_quantity": 0,
        "day_sell_quantity": 0,
        "day_buy_value": 0,
        "day_sell_value": 0,
        "multiplier": 1,
    }


class TestTotalProfitConvergence:
    def test_overnight_open_position(self):
        r = _kite_row(realised=0.0, unrealised=500.0)
        self._assert_converges(r)

    def test_partial_exit_with_realised_and_unrealised(self):
        r = _kite_row(realised=300.0, unrealised=900.0)
        self._assert_converges(r)

    def test_fully_closed_intraday(self):
        r = _kite_row(realised=800.0, unrealised=0.0)
        self._assert_converges(r)
        # Also matches the pnl_math SSOT directly.
        assert current_total_profit(800.0, 0.0) == 800.0

    def test_negative_pnl_loss_position(self):
        r = _kite_row(realised=-200.0, unrealised=-450.0)
        self._assert_converges(r)

    @staticmethod
    def _assert_converges(r: dict) -> None:
        # daily_snapshot.py path — writes daily_book.total_pnl.
        snapshot_total = _positions_row_total_pnl(r)

        # broker_apis.py path — live-fetch `pnl` column (Kite: native pnl,
        # which by Zerodha's confirmed invariant == realised + unrealised).
        df = pd.DataFrame([r])
        enriched = _enrich_positions(df, broker_kind="kite")
        broker_total = float(enriched.iloc[0]["pnl"])

        assert math.isclose(snapshot_total, broker_total, abs_tol=1e-6), (
            f"daily_snapshot total_pnl={snapshot_total} must equal "
            f"broker_apis pnl={broker_total} for the same synthetic row — "
            "the two total-profit writers must never drift apart again"
        )


# ---------------------------------------------------------------------------
# Strengthened regression guard (2026-09 Day P&L audit) — the fixture above
# constructs `pnl = realised + unrealised` BY HAND on every row, so it can
# never catch the class of bug where a downstream patch updates `pnl` but
# leaves `unrealised` stale (audit item #2 — `_override_stale_ltp_from_
# ticker` patched `pnl` additively but forgot to mirror the same delta onto
# `unrealised`). This test exercises the REAL patch function on a row where
# `pnl` and `realised + unrealised` legitimately diverge pre-patch (a fresh
# ticker LTP arrived, is still consistent with the fix), and asserts the
# post-patch invariant `pnl == realised + unrealised` holds — the exact
# invariant a reintroduced regression would break.
# ---------------------------------------------------------------------------

class TestStaleLtpPatchPreservesConvergence:
    def test_ticker_patch_keeps_pnl_equal_to_realised_plus_unrealised(self):
        """Auditor repro: Kite long qty=50, avg=100, REST ltp=102
        (unrealised=100, pnl=100, realised=0), ticker delivers a fresher
        ltp=110. After `_override_stale_ltp_from_ticker` patches both `pnl`
        and `unrealised` by the same additive delta, `realised+unrealised`
        must still equal `pnl` — proving the two never drift apart even
        through a live LTP correction (not just in the hand-built fixture
        above, which can't exercise this code path at all)."""
        from backend.api.routes.positions import _override_stale_ltp_from_ticker

        df = pd.DataFrame([{
            'tradingsymbol': 'RELIANCE', 'exchange': 'NSE',
            'last_price': 102.0, 'prev_close': 100.0,
            'quantity': 50, 'overnight_quantity': 50,
            'day_buy_quantity': 0, 'day_sell_quantity': 0,
            'day_buy_value': 0.0, 'day_sell_value': 0.0,
            'average_price': 100.0, 'realised': 0.0,
            'unrealised': 100.0, 'pnl': 100.0,
            'day_change_val': 0.0, 'day_change': 0.0,
        }])
        # Pre-patch fixture already satisfies pnl == realised+unrealised
        # (100 == 0+100) — the divergence this test guards against is
        # introduced BY the patch itself if `unrealised` isn't mirrored.
        mock_ticker = MagicMock()
        mock_ticker.get_ltp_by_sym.return_value = 110.0
        with patch('backend.brokers.kite_ticker.get_ticker', return_value=mock_ticker):
            _override_stale_ltp_from_ticker(df)

        pnl = float(df.at[0, 'pnl'])
        realised = float(df.at[0, 'realised'])
        unrealised = float(df.at[0, 'unrealised'])
        assert pnl == pytest.approx(500.0)
        assert unrealised == pytest.approx(500.0)
        assert math.isclose(pnl, realised + unrealised, abs_tol=1e-6), (
            f"pnl={pnl} must equal realised+unrealised={realised + unrealised} "
            f"after the ticker LTP patch — a regression that patches `pnl` "
            f"without mirroring `unrealised` would break this invariant "
            f"(the exact 2026-09 audit item #2 bug: pnl=500 but stale "
            f"unrealised=100, realised+unrealised=100 != pnl=500)"
        )
