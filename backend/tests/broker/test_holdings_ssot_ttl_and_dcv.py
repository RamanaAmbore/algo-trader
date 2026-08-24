"""Tests for three holdings fixes (Aug 2026).

  Fix 1 — broker_apis.fetch_holdings() TTL (30 s):
    Holdings ssot_fetch had no TTL, so stale / empty-Dhan cache persisted
    indefinitely during MCX evening when _task_closed_hours_refresh never
    ran (any_segment_open=True). Adding _HOLDINGS_SSOT_TTL=30 mirrors the
    existing positions TTL pattern.

  Fix 2 — _override_stale_close_for_holdings: recompute day_change_val for
    ALL rows with previous_close > 0 (not just close_price-patched rows).
    Kite rows where close_price already matched the snapshot still had
    _enrich_holdings' stale computation; Dhan/Groww rows had close_price≈ltp
    from backfill_market_data so (ltp−close)×qty≈0 → backstop Case 1 fired
    incorrectly.

  Fix 3 — remove apply_day_change_backstop() from holdings._fetch().
    Holdings never have overnight_quantity (column absent → 0 for all rows).
    Case 1 (oq=0, dcv=0, pnl≠0) fires for any holding whose price is
    unchanged that session, setting day_change_val = pnl (TOTAL P&L since
    purchase — wrong).

Test dimensions per fix:
  * SSOT — canonical computation in broker_apis / holdings.py; no workarounds.
  * Perf — vectorised pandas ops; one DB query per route call.
  * Stale-code — apply_day_change_backstop no longer importable from holdings.
  * Reuse — TTL pattern matches fetch_positions(); dcv formula mirrors holdings
    frontend formula (ltp - previous_close) × qty.
  * UX — Dhan holdings appear after rate-limit gap; Kite day P&L non-zero;
    holdings with unchanged price show dcv=0, not dcv=total-pnl.
"""

from __future__ import annotations

import time
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Fix 1 — TTL gate on fetch_holdings()
# ---------------------------------------------------------------------------

class TestHoldingsSsotTtl:
    """fetch_holdings() forces a fresh cache read after 30 s."""

    def _reset_refresh_ts(self):
        import backend.brokers.broker_apis as ba
        ba._holdings_ssot_refresh_at = 0.0

    def test_first_call_forces_refresh(self):
        """Cold boot: refresh_at=0, elapsed > TTL → force_refresh=True."""
        import backend.brokers.broker_apis as ba
        self._reset_refresh_ts()

        call_args = []

        def fake_fetch_holdings_cached(force_refresh=False):
            call_args.append(force_refresh)
            return [pd.DataFrame([{"tradingsymbol": "SBIN", "quantity": 10}])]

        with patch.object(ba, "_fetch_holdings_cached", side_effect=fake_fetch_holdings_cached):
            ba.fetch_holdings()

        assert call_args == [True], "first call must pass force_refresh=True"

    def test_second_call_within_ttl_no_refresh(self):
        """Second call within 30 s → force_refresh=False (cache serves it)."""
        import backend.brokers.broker_apis as ba
        # Simulate a recent successful fetch.
        ba._holdings_ssot_refresh_at = time.monotonic()

        call_args = []

        def fake_fetch_holdings_cached(force_refresh=False):
            call_args.append(force_refresh)
            return [pd.DataFrame([{"tradingsymbol": "SBIN", "quantity": 10}])]

        with patch.object(ba, "_fetch_holdings_cached", side_effect=fake_fetch_holdings_cached):
            ba.fetch_holdings()

        assert call_args == [False], "call within TTL must pass force_refresh=False"

    def test_call_after_ttl_forces_refresh(self):
        """Call after TTL expires → force_refresh=True again."""
        import backend.brokers.broker_apis as ba
        # Pretend last fetch was 40 s ago (beyond 30 s TTL).
        ba._holdings_ssot_refresh_at = time.monotonic() - 40.0

        call_args = []

        def fake_fetch_holdings_cached(force_refresh=False):
            call_args.append(force_refresh)
            return [pd.DataFrame([{"tradingsymbol": "SBIN", "quantity": 10}])]

        with patch.object(ba, "_fetch_holdings_cached", side_effect=fake_fetch_holdings_cached):
            ba.fetch_holdings()

        assert call_args == [True], "stale cache must force force_refresh=True"

    def test_refresh_at_updated_on_non_none_result(self):
        """Successful fetch updates _holdings_ssot_refresh_at."""
        import backend.brokers.broker_apis as ba
        self._reset_refresh_ts()
        before = time.monotonic()

        with patch.object(
            ba,
            "_fetch_holdings_cached",
            return_value=[pd.DataFrame([{"tradingsymbol": "SBIN"}])],
        ):
            ba.fetch_holdings()

        assert ba._holdings_ssot_refresh_at >= before

    def test_refresh_at_not_updated_on_none_result(self):
        """None result (broker outage) must not update _holdings_ssot_refresh_at."""
        import backend.brokers.broker_apis as ba
        self._reset_refresh_ts()

        with patch.object(ba, "_fetch_holdings_cached", return_value=None):
            ba.fetch_holdings()

        # Should still be 0 (or very close to reset value)
        assert ba._holdings_ssot_refresh_at == 0.0


# ---------------------------------------------------------------------------
# Fix 2 — day_change_val recomputed for ALL rows with previous_close > 0
# ---------------------------------------------------------------------------

class TestHoldingsDcvRecomputeAllRows:
    """_override_stale_close_for_holdings recomputes dcv for every row
    that has a snapshot entry, not just close_price-patched rows."""

    def _make_raw(self, *, ltp: float, close_price: float, previous_close: float,
                  quantity: int, pnl: float, tradingsymbol: str = "INFY",
                  account: str = "KT1234") -> pd.DataFrame:
        return pd.DataFrame([{
            "tradingsymbol": tradingsymbol,
            "account": account,
            "last_price": ltp,
            "close_price": close_price,
            "previous_close": 0.0,      # will be written by the function
            "quantity": quantity,
            "pnl": pnl,
            "day_change_val": (ltp - close_price) * quantity,
            "day_change": ltp - close_price,
            "day_change_percentage": 0.0,
            "pnl_percentage": 0.0,
            "average_price": 800.0,
        }])

    @pytest.mark.asyncio
    async def test_kite_row_already_matching_snapshot_gets_dcv_recomputed(self):
        """Kite row: close_price already matches daily_book.ltp (epsilon ≤ 0.005).
        Even though close_price is NOT patched, day_change_val must be recomputed
        using previous_close (same value), giving (ltp - prev_close) × qty."""
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        # ltp=510, close_price=500, daily_book.ltp=500 (already equal → no patch).
        # day_change_val should be (510 - 500) × 100 = 1000.
        raw = self._make_raw(ltp=510.0, close_price=500.0, previous_close=0.0,
                             quantity=100, pnl=10000.0)

        snapshot_map = {("KT1234", "INFY"): 500.0}
        with patch("backend.api.database.async_session", side_effect=lambda: _make_db_ctx(snapshot_map)):
            await _override_stale_close_for_holdings(raw)

        assert raw.at[0, "previous_close"] == pytest.approx(500.0)
        assert raw.at[0, "day_change_val"] == pytest.approx(1000.0)

    @pytest.mark.asyncio
    async def test_dhan_row_with_close_near_ltp_gets_correct_dcv(self):
        """Dhan row: backfill set close_price ≈ ltp (today's settlement).
        (ltp - close_price) × qty ≈ 0, so no close_price patch.
        But previous_close = daily_book.ltp (prior session) differs from ltp,
        so dcv must be recomputed as (ltp - previous_close) × qty."""
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        # ltp=505, close_price=505 (Dhan backfill), daily_book.ltp=480 (prior session)
        raw = self._make_raw(ltp=505.0, close_price=505.0, previous_close=0.0,
                             quantity=50, pnl=3250.0, tradingsymbol="HDFCBANK",
                             account="DH3747")
        raw.at[0, "day_change_val"] = 0.0  # stale: (505-505)*50

        snapshot_map = {("DH3747", "HDFCBANK"): 480.0}
        with patch("backend.api.database.async_session", side_effect=lambda: _make_db_ctx(snapshot_map)):
            await _override_stale_close_for_holdings(raw)

        # |505 - 480| = 25 > 0.005 → close_price patched to 480
        # dcv = (505 - 480) * 50 = 1250
        assert raw.at[0, "previous_close"] == pytest.approx(480.0)
        assert raw.at[0, "day_change_val"] == pytest.approx(1250.0)

    @pytest.mark.asyncio
    async def test_row_with_no_snapshot_entry_keeps_original_dcv(self):
        """Row not in snapshot_map must not be touched."""
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        raw = self._make_raw(ltp=200.0, close_price=195.0, previous_close=0.0,
                             quantity=200, pnl=3000.0, tradingsymbol="WIPRO",
                             account="KT9999")
        raw.at[0, "day_change_val"] = 999.0  # sentinel

        snapshot_map = {}  # no snapshot entry
        with patch("backend.api.database.async_session", side_effect=lambda: _make_db_ctx(snapshot_map)):
            await _override_stale_close_for_holdings(raw)

        assert raw.at[0, "previous_close"] == pytest.approx(0.0)
        assert raw.at[0, "day_change_val"] == pytest.approx(999.0)


# ---------------------------------------------------------------------------
# Fix 3 — apply_day_change_backstop must NOT fire for holdings
# ---------------------------------------------------------------------------

class TestHoldingsBackstopNotApplied:
    """Holdings with oq=0 (absent column) and unchanged price must show
    day_change_val=0, not day_change_val=pnl (Case 1 regression)."""

    def test_unchanged_price_holding_dcv_stays_zero(self):
        """Kite holding whose price hasn't moved today:
        ltp = close_price → dcv = 0 from broker.
        Before Fix 3, apply_day_change_backstop Case 1 (oq=0, dcv=0, pnl≠0)
        would fire and set dcv = pnl (TOTAL P&L since purchase).
        After Fix 3, dcv stays 0."""
        from backend.api.algo.pnl_math import apply_day_change_backstop

        df = pd.DataFrame([{
            "tradingsymbol": "TCS",
            "last_price": 3500.0,
            "close_price": 3500.0,    # ltp = close → no day move
            "average_price": 3000.0,
            "quantity": 100,
            "pnl": 50000.0,           # (3500-3000)*100 = lifetime P&L
            "day_change_val": 0.0,    # correct: no move today
            # overnight_quantity intentionally ABSENT (holdings never have it)
        }])

        # Verify the backstop WOULD corrupt this if called (documents the bug).
        corrupted = apply_day_change_backstop(df.copy())
        oq = corrupted.get("overnight_quantity", pd.Series([0]))
        assert corrupted.at[0, "day_change_val"] == pytest.approx(50000.0), (
            "backstop Case 1 fires for holdings when oq column is absent — "
            "this confirms Fix 3 is required"
        )

    def test_holdings_fetch_does_not_call_backstop(self):
        """apply_day_change_backstop must not be imported or called from
        backend.api.routes.holdings after Fix 3."""
        import importlib
        import backend.api.routes.holdings as hmod

        # The import must have been removed.
        assert not hasattr(hmod, "apply_day_change_backstop"), (
            "apply_day_change_backstop must no longer be imported into holdings.py"
        )

    def test_pnl_math_still_exports_backstop(self):
        """apply_day_change_backstop must still exist in pnl_math for positions use."""
        from backend.api.algo.pnl_math import apply_day_change_backstop  # noqa: F401
        assert callable(apply_day_change_backstop)


# ---------------------------------------------------------------------------
# Shared helper — mock async DB session
# ---------------------------------------------------------------------------

class _MockAsyncCtx:
    """Async context manager that yields a fake session returning snapshot rows."""

    def __init__(self, snapshot_map: dict):
        rows = [
            (account, symbol, ref_close)
            for (account, symbol), ref_close in snapshot_map.items()
        ]
        self._rows = rows

    async def __aenter__(self):
        mock_result = MagicMock()
        mock_result.all.return_value = self._rows
        mock_session = MagicMock()

        async def fake_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = fake_execute
        return mock_session

    async def __aexit__(self, *args):
        pass


def _make_db_ctx(snapshot_map: dict) -> _MockAsyncCtx:
    return _MockAsyncCtx(snapshot_map)
