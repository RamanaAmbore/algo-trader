"""
Tests for the bad-payload guard added to daily_snapshot.py.

Root cause (2026-06-30): ZG0790's Kite token was invalid.  The broker returned
all-zero LTP rows for every holding/position.  The snapshot writer wrote those
zero rows, overwriting the previous day's good snapshot.  NavStrip P delta then
summed zeros → 0.0.

Guard behaviour:
  - If a row has ltp=0 AND day_pnl=0 AND total_pnl=0 AND avg_cost > 0,
    it is skipped (not written).
  - If ALL rows for an account were skipped, a WARNING is emitted and no
    upsert is performed — the prior snapshot is untouched.
  - Rows where avg_cost is zero or None are NOT filtered (they could be
    legitimately newly-opened positions).
  - Mid-session rows (ltp=None) are NOT filtered — they are handled by
    the existing mid-session guard, not this one.

Five quality dimensions:
  1. SSOT        — guard in _holdings_rows + _positions_rows; account-level
                   check in snapshot_daily_book.
  2. Performance — skipped rows produce zero DB writes; no extra queries.
  3. Stale code  — grep asserts _is_zero_payload_row present in daily_snapshot.py.
  4. Reusable    — _is_zero_payload_row is a standalone helper callable by tests.
  5. Correctness — zero rows skipped; prior snapshot preserved; valid rows pass.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Dimension 3 — static source checks
# ---------------------------------------------------------------------------

_SNAP_SRC = Path(__file__).parent.parent / "api" / "algo" / "daily_snapshot.py"


def _src() -> str:
    return _SNAP_SRC.read_text(encoding="utf-8")


def test_zero_payload_guard_helper_exists():
    """daily_snapshot.py defines _is_zero_payload_row helper."""
    src = _src()
    assert "_is_zero_payload_row" in src, (
        "daily_snapshot.py must define _is_zero_payload_row for the bad-payload guard"
    )


def test_holdings_rows_calls_guard():
    """_holdings_rows references _is_zero_payload_row."""
    src = _src()
    # Both _holdings_rows and _positions_rows should call it
    assert src.count("_is_zero_payload_row") >= 3, (
        "_is_zero_payload_row must appear in definition + _holdings_rows + _positions_rows"
    )


def test_all_filtered_warning_in_snapshot():
    """snapshot_daily_book emits 'Prior snapshot preserved' when all rows filtered."""
    src = _src()
    assert "Prior snapshot preserved" in src, (
        "snapshot_daily_book must log 'Prior snapshot preserved' when all rows are filtered"
    )


# ---------------------------------------------------------------------------
# Dimension 4 — unit: _is_zero_payload_row
# ---------------------------------------------------------------------------

class TestIsZeroPayloadRow:
    def _fn(self):
        from backend.api.algo.daily_snapshot import _is_zero_payload_row
        return _is_zero_payload_row

    def test_all_zeros_with_avg_cost_is_bad(self):
        """ltp=0, day_pnl=0, total_pnl=0 with avg_cost>0 → bad payload."""
        fn = self._fn()
        row = {"average_price": 1500.0}
        assert fn(row, ltp=0.0, day_pnl=0.0, total_pnl=0.0) is True

    def test_none_values_treated_as_zero(self):
        """None values for ltp/day_pnl/total_pnl are treated as 0.0."""
        fn = self._fn()
        row = {"average_price": 2800.0}
        assert fn(row, ltp=None, day_pnl=None, total_pnl=None) is True

    def test_non_zero_ltp_is_good(self):
        """Non-zero LTP means the broker returned real data."""
        fn = self._fn()
        row = {"average_price": 1500.0}
        assert fn(row, ltp=1560.0, day_pnl=60.0, total_pnl=600.0) is False

    def test_zero_avg_cost_is_not_filtered(self):
        """avg_cost=0 → could be a new position; never filter."""
        fn = self._fn()
        row = {"average_price": 0.0}
        assert fn(row, ltp=0.0, day_pnl=0.0, total_pnl=0.0) is False

    def test_missing_avg_cost_is_not_filtered(self):
        """avg_cost missing → skip filter (can't distinguish from new position)."""
        fn = self._fn()
        row = {}
        assert fn(row, ltp=0.0, day_pnl=0.0, total_pnl=0.0) is False

    def test_zero_total_pnl_but_nonzero_ltp_is_good(self):
        """total_pnl=0 alone is not a filter trigger — zero P&L is legitimate."""
        fn = self._fn()
        row = {"average_price": 1500.0}
        assert fn(row, ltp=1500.0, day_pnl=0.0, total_pnl=0.0) is False


# ---------------------------------------------------------------------------
# Dimension 5a — _holdings_rows skips bad rows, emits warning
# ---------------------------------------------------------------------------

class TestHoldingsRowsBadPayloadGuard:
    _D = date(2026, 6, 30)
    _NOW_EOD = datetime(2026, 6, 30, 23, 35)  # after NSE close

    def _build_zero_holdings(self, n: int = 2) -> list[dict]:
        return [
            {
                "tradingsymbol": f"STOCK{i}",
                "exchange": "NSE",
                "opening_quantity": 10,
                "average_price": 1500.0,
                "last_price": 0.0,
                "day_change": 0.0,
                "pnl": 0.0,
            }
            for i in range(n)
        ]

    def _build_good_holdings(self) -> list[dict]:
        return [
            {
                "tradingsymbol": "INFY",
                "exchange": "NSE",
                "opening_quantity": 10,
                "average_price": 1500.0,
                "last_price": 1560.0,
                "day_change": 60.0,
                "pnl": 600.0,
            }
        ]

    def test_all_zero_rows_are_skipped(self):
        """All-zero holdings rows (bad token) produce no output rows."""
        from backend.api.algo.daily_snapshot import _holdings_rows
        bad = self._build_zero_holdings(3)
        rows = _holdings_rows("ZG0790", self._D, bad, self._NOW_EOD)
        assert rows == [], f"Expected 0 rows, got {len(rows)}"

    def test_warning_logged_when_rows_skipped(self):
        """A WARNING is emitted when holdings rows are skipped.

        Uses MagicMock on logger.warning because ramboq_logger sets
        propagate=False, which prevents pytest caplog from receiving records.
        """
        from backend.api.algo import daily_snapshot as ds
        bad = self._build_zero_holdings(2)
        with patch.object(ds.logger, "warning") as mock_warn:
            ds._holdings_rows("ZG0790", self._D, bad, self._NOW_EOD)
        assert mock_warn.called, "logger.warning must be called when holdings rows are skipped"
        all_messages = " ".join(str(c) for c in mock_warn.call_args_list)
        assert "skipped" in all_messages and "holdings" in all_messages, (
            f"WARNING must mention 'skipped' and 'holdings'; got: {all_messages}"
        )

    def test_good_rows_pass_through(self):
        """Valid non-zero holdings rows are written normally."""
        from backend.api.algo.daily_snapshot import _holdings_rows
        good = self._build_good_holdings()
        rows = _holdings_rows("ZG0790", self._D, good, self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "INFY"
        assert rows[0]["ltp"] == 1560.0

    def test_mixed_rows_preserves_good_skips_bad(self):
        """Mixed good + bad holdings: good rows written, bad rows skipped."""
        from backend.api.algo.daily_snapshot import _holdings_rows
        bad = self._build_zero_holdings(2)
        good = self._build_good_holdings()
        rows = _holdings_rows("ZG0790", self._D, bad + good, self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "INFY"

    def test_zero_rows_during_mid_session_not_filtered(self):
        """Mid-session rows (ltp=None by design) are NOT filtered by bad-payload guard."""
        from backend.api.algo.daily_snapshot import _holdings_rows
        # NSE mid-session at 12:00 — ltp is set to None by mid-session guard,
        # not by the zero-payload guard. Row should still be emitted.
        now_mid = datetime(2026, 6, 30, 12, 0)
        good_mid = [
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "opening_quantity": 5,
                "average_price": 2800.0,
                "last_price": 0.0,   # broker zero during session
                "day_change": 0.0,
                "pnl": 0.0,
            }
        ]
        rows = _holdings_rows("ZG0790", self._D, good_mid, now_mid)
        # Mid-session: ltp emitted as None (not 0.0), so guard doesn't fire
        assert len(rows) == 1, "Mid-session row must not be filtered by bad-payload guard"
        assert rows[0]["ltp"] is None


# ---------------------------------------------------------------------------
# Dimension 5a — _positions_rows skips bad rows, emits warning
# ---------------------------------------------------------------------------

class TestPositionsRowsBadPayloadGuard:
    _D = date(2026, 6, 30)
    _NOW_EOD = datetime(2026, 6, 30, 23, 35)

    def _build_zero_positions(self, n: int = 1) -> list[dict]:
        return [
            {
                "tradingsymbol": f"NIFTY25JUL{i}FUT",
                "exchange": "NFO",
                "quantity": 50,
                "average_price": 23000.0,
                "last_price": 0.0,
                "close_price": 0.0,
                "pnl": 0.0,
            }
            for i in range(n)
        ]

    def test_all_zero_positions_skipped(self):
        """All-zero position rows are skipped (bad token)."""
        from backend.api.algo.daily_snapshot import _positions_rows
        bad = self._build_zero_positions(2)
        rows = _positions_rows("ZG0790", self._D, bad, self._NOW_EOD)
        assert rows == [], f"Expected 0 rows, got {len(rows)}"

    def test_warning_logged_for_skipped_positions(self):
        """WARNING is emitted when positions rows are skipped."""
        from backend.api.algo import daily_snapshot as ds
        bad = self._build_zero_positions(1)
        with patch.object(ds.logger, "warning") as mock_warn:
            ds._positions_rows("ZG0790", self._D, bad, self._NOW_EOD)
        assert mock_warn.called, "logger.warning must be called when positions rows are skipped"
        all_messages = " ".join(str(c) for c in mock_warn.call_args_list)
        assert "skipped" in all_messages and "positions" in all_messages, (
            f"WARNING must mention 'skipped' and 'positions'; got: {all_messages}"
        )

    def test_good_positions_pass_through(self):
        """Position with real LTP passes through the guard."""
        from backend.api.algo.daily_snapshot import _positions_rows
        good = [
            {
                "tradingsymbol": "NIFTY25JUNFUT",
                "exchange": "NFO",
                "quantity": 50,
                "average_price": 23000.0,
                "last_price": 23200.0,
                "close_price": 23100.0,
                "pnl": 10000.0,
            }
        ]
        rows = _positions_rows("ZG0790", self._D, good, self._NOW_EOD)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "NIFTY25JUNFUT"
        assert rows[0]["ltp"] == 23200.0


# ---------------------------------------------------------------------------
# Dimension 5b — snapshot_daily_book: all filtered → no upsert, warning logged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_all_filtered_no_upsert_emits_warning(caplog):
    """When every holdings + positions row is filtered for an account,
    _upsert_rows must NOT be called (no DB write) and a WARNING is logged
    explaining the prior snapshot is preserved.
    """
    from backend.api.algo import daily_snapshot as ds

    zero_holdings = [
        {
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "opening_quantity": 10,
            "average_price": 1500.0,
            "last_price": 0.0,
            "day_change": 0.0,
            "pnl": 0.0,
        }
    ]
    zero_positions = [
        {
            "tradingsymbol": "NIFTY25JUNFUT",
            "exchange": "NFO",
            "quantity": 50,
            "average_price": 23000.0,
            "last_price": 0.0,
            "close_price": 0.0,
            "pnl": 0.0,
        }
    ]

    mock_broker = MagicMock()
    mock_broker.account = "ZG0790"
    mock_broker.holdings.return_value = zero_holdings
    mock_broker.positions.return_value = {"net": zero_positions}
    mock_broker.trades.return_value = []
    mock_broker.margins.return_value = {}

    mock_upsert = AsyncMock(return_value=0)

    # Patch timestamp_indian so target_date == today (enabling trades fetch path)
    fixed_ist = datetime(2026, 6, 30, 23, 35, tzinfo=timezone.utc)

    with patch.object(ds, "_upsert_rows", mock_upsert), \
         patch.object(ds, "_get_connections", return_value=MagicMock(conn={"ZG0790": None})), \
         patch("backend.brokers.registry.all_brokers", return_value=[mock_broker]), \
         patch.object(ds, "timestamp_indian", return_value=fixed_ist), \
         patch.object(ds.logger, "warning") as mock_warn:

        result = await ds.snapshot_daily_book(target_date=date(2026, 6, 30))

    # _upsert_rows must NOT have been called with any substantive rows
    # (holdings + positions upserts skipped; trades/funds may still run
    # since they are not subject to the bad-payload guard for this path)
    holdings_upsert_calls = [
        c for c in mock_upsert.call_args_list
        if c.args and any(r.get("kind") == "holdings" for r in (c.args[0] or []))
    ]
    positions_upsert_calls = [
        c for c in mock_upsert.call_args_list
        if c.args and any(r.get("kind") == "positions" for r in (c.args[0] or []))
    ]
    assert holdings_upsert_calls == [], (
        "holdings upsert must NOT be called when all rows are filtered"
    )
    assert positions_upsert_calls == [], (
        "positions upsert must NOT be called when all rows are filtered"
    )

    # Account-level warning logged
    all_warn_msgs = " ".join(str(c) for c in mock_warn.call_args_list)
    assert "Prior snapshot preserved" in all_warn_msgs, (
        f"Expected 'Prior snapshot preserved' warning; got: {all_warn_msgs}"
    )

    # Account still in processed list (we don't mark it as error)
    assert "ZG0790" in result["accounts"]


# ---------------------------------------------------------------------------
# Dimension 5c — prior snapshot preserved (DB untouched)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_prior_snapshot_untouched_on_bad_payload():
    """When a bad-payload run fires, no DB rows are deleted or overwritten.

    We simulate this by verifying _upsert_rows is never called with the zero
    rows. Since _upsert_rows is an ON CONFLICT DO UPDATE upsert, any call
    would overwrite existing good rows — so call_count must be zero for
    holdings/positions.
    """
    from backend.api.algo import daily_snapshot as ds

    bad_holdings = [
        {"tradingsymbol": "TCS", "exchange": "NSE",
         "opening_quantity": 5, "average_price": 3400.0,
         "last_price": 0.0, "day_change": 0.0, "pnl": 0.0}
    ]

    mock_broker = MagicMock()
    mock_broker.account = "ZJ6294"
    mock_broker.holdings.return_value = bad_holdings
    mock_broker.positions.return_value = {"net": []}  # no positions
    mock_broker.trades.return_value = []
    mock_broker.margins.return_value = {}

    upsert_calls: list = []

    async def _spy_upsert(rows):
        if rows:
            upsert_calls.append(rows)
        return len(rows)

    fixed_ist = datetime(2026, 6, 30, 23, 35, tzinfo=timezone.utc)

    with patch.object(ds, "_upsert_rows", side_effect=_spy_upsert), \
         patch.object(ds, "_get_connections", return_value=MagicMock(conn={"ZJ6294": None})), \
         patch("backend.brokers.registry.all_brokers", return_value=[mock_broker]), \
         patch.object(ds, "timestamp_indian", return_value=fixed_ist):

        await ds.snapshot_daily_book(target_date=date(2026, 6, 30))

    # No upsert calls should have carried holdings rows
    holdings_writes = [
        rows for rows in upsert_calls
        if any(r.get("kind") == "holdings" for r in rows)
    ]
    assert holdings_writes == [], (
        f"No holdings upsert expected when all rows are bad-payload; "
        f"got {len(holdings_writes)} calls with holdings rows"
    )
