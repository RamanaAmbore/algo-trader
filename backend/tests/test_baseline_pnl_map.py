"""Tests for `_fetch_baseline_pnl_map` (backend/api/routes/positions.py) —
the batch-anchored base_pnl lookup that replaced the old unbounded
`captured_at < today_08 ORDER BY captured_at DESC LIMIT 1` per-symbol query.

Covers:
  1. A symbol absent from the most-recent per-account batch returns no
     entry (base_pnl defaults to 0 downstream), not a stale historical row.
  2. kind='positions' takes precedence over kind='holdings' for the same
     (account, symbol) — the "holding sold into a CNC position" case.
  3. DB errors are swallowed and return {} (safe to call unconditionally).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from backend.api.routes.positions import _fetch_baseline_pnl_map

IST = ZoneInfo("Asia/Kolkata")


def _mock_session(rows):
    mock_result = MagicMock()
    mock_result.all.return_value = rows
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


class TestFetchBaselinePnlMap:
    def test_symbol_absent_from_batch_has_no_entry(self):
        """A symbol not returned by the anchored query has no key in the
        map — the caller treats a missing key as base_pnl=0, never reaching
        back to an arbitrarily old row (the bug this replaces)."""
        cutoff = datetime(2026, 9, 23, 8, 0, 0, tzinfo=IST)
        # DB returns only RELIANCE for TEST001 — INFY was NOT in the most
        # recent batch (e.g. opened fresh today with no prior snapshot).
        # SQL SELECTs from pnl_ranked WHERE rn=1 — 3-column (account, symbol,
        # total_pnl); the kind precedence / ranking happens IN the SQL
        # (ROW_NUMBER() ... ORDER BY CASE kind WHEN 'positions' THEN 0 ELSE 1),
        # so the Python layer only ever sees the already-winning row.
        mock_session = _mock_session([
            ("TEST001", "RELIANCE", 500.0),
        ])
        with patch("backend.api.database.async_session", return_value=mock_session):
            out = asyncio.run(_fetch_baseline_pnl_map(cutoff))

        assert out == {("TEST001", "RELIANCE"): 500.0}
        assert ("TEST001", "INFY") not in out, (
            "Symbol absent from the anchored batch must have no map entry "
            "(base_pnl=0), not a stale row from an older batch"
        )

    def test_positions_kind_wins_over_holdings_for_same_symbol(self):
        """Holdings sold into a CNC position: when both a 'positions' row
        and a 'holdings' row exist for the same (account, symbol) in their
        respective latest batches, 'positions' must win — the CNC row is
        the correct incremental baseline, not yesterday's full holding.

        The precedence itself is enforced by the SQL's
        `ROW_NUMBER() ... ORDER BY CASE kind WHEN 'positions' THEN 0 ELSE 1`
        + `WHERE rn = 1` — this test asserts the Python layer trusts that
        single winning row (positions' total_pnl=4500, not holdings' 4000).
        """
        cutoff = datetime(2026, 9, 23, 8, 0, 0, tzinfo=IST)
        mock_session = _mock_session([
            ("TEST001", "TCS", 4500.0),  # the SQL already picked 'positions'
        ])
        with patch("backend.api.database.async_session", return_value=mock_session):
            out = asyncio.run(_fetch_baseline_pnl_map(cutoff))

        assert out[("TEST001", "TCS")] == 4500.0, (
            "positions-kind row must win over holdings-kind row for the same "
            "(account, symbol) — see _fetch_baseline_pnl_map / "
            "_BASELINE_PNL_CTE_SQL docstring"
        )

    def test_holdings_only_symbol_uses_holdings_baseline(self):
        """A symbol only present as a holding (never traded as a position)
        must still resolve its baseline from the holdings-kind row."""
        cutoff = datetime(2026, 9, 23, 8, 0, 0, tzinfo=IST)
        mock_session = _mock_session([
            ("TEST001", "GOLDBEES", 1200.0),
        ])
        with patch("backend.api.database.async_session", return_value=mock_session):
            out = asyncio.run(_fetch_baseline_pnl_map(cutoff))

        assert out[("TEST001", "GOLDBEES")] == 1200.0

    def test_db_error_returns_empty_dict(self):
        cutoff = datetime(2026, 9, 23, 8, 0, 0, tzinfo=IST)
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        with patch("backend.api.database.async_session", return_value=mock_session):
            out = asyncio.run(_fetch_baseline_pnl_map(cutoff))
        assert out == {}

    def test_sql_anchors_per_account_per_kind_not_cross_kind(self):
        """Guard against regressing to a single cross-kind MAX(captured_at)
        anchor — verified against production data that positions/holdings
        snapshots for the same account never share an exact captured_at.

        The anchor CTE lives in the shared `_BASELINE_PNL_CTE_SQL` fragment
        (embedded into `_fetch_baseline_pnl_map`, `_fetch_snapshot_close_map`,
        and `_positions_snapshot` so each stays a single round trip).

        `daily_book.account`/`daily_book.kind` are qualified (not bare
        `account, kind`) since the fragment now JOINs to a caller-supplied
        `latest_batch` CTE (audit item #3 — per-account cutoff anchor) which
        also has an `account` column, making the bare name ambiguous."""
        from backend.api.routes import positions as positions_mod

        src = positions_mod._BASELINE_PNL_CTE_SQL
        assert "GROUP BY daily_book.account, daily_book.kind" in src, (
            "latest_pnl_batch CTE must anchor MAX(captured_at) per (account, kind), "
            "not a single cross-kind anchor per account"
        )
        assert "kind IN ('positions', 'holdings')" in src, (
            "baseline batch must consider both positions and holdings kinds "
            "(CNC-split correctness)"
        )

    def test_sql_requires_caller_supplied_latest_batch_cte(self):
        """The fragment JOINs to `latest_batch` (account, cutoff_ts) rather
        than a scalar `:baseline_cutoff` bind — every caller must define
        that CTE first, anchoring the baseline strictly before whatever
        batch is being used as "current" for display (audit item #3 fix:
        a fixed `:baseline_cutoff` collided with the display batch on a
        weekend/holiday morning, both resolving to the same trading day's
        close and collapsing Day P&L to 0)."""
        from backend.api.routes import positions as positions_mod

        src = positions_mod._BASELINE_PNL_CTE_SQL
        assert "JOIN latest_batch ON latest_batch.account = daily_book.account" in src
        assert "latest_batch.cutoff_ts" in src
        assert ":baseline_cutoff" not in src, (
            "the shared fragment must no longer bind a scalar :baseline_cutoff "
            "directly — callers supply the anchor via their own latest_batch CTE"
        )

    def test_sql_also_bounds_by_display_batch_max_at(self):
        """The baseline must ALSO be strictly older than the display batch's
        own `captured_at` (`latest_batch.max_at`), not just its `cutoff_ts`.

        A closed-hours snapshot run just after midnight IST writes a row
        whose `date` column is the NEXT calendar day (the writer stamps
        `date` from the wall-clock day, not the trading session). That
        row's own `cutoff_ts` (its date's 08:00 IST) then sits AFTER its
        own `captured_at` — without this extra `< max_at` bound the row
        would satisfy its own `cutoff_ts` test and become its own baseline
        (self-collision). Verified against live daily_book data during
        implementation."""
        from backend.api.routes import positions as positions_mod

        src = positions_mod._BASELINE_PNL_CTE_SQL
        assert "daily_book.captured_at < latest_batch.max_at" in src

    def test_sql_excludes_flat_rows_from_baseline(self):
        """pnl_ranked must exclude qty=0 (flat/fully-closed) rows — a flat
        historical row is not a genuine continuation of a position and must
        not serve as tomorrow's baseline for an unrelated fresh re-entry
        (audit item #5)."""
        from backend.api.routes import positions as positions_mod

        src = positions_mod._BASELINE_PNL_CTE_SQL
        assert "daily_book.qty != 0" in src

    def test_sql_pnl_final_exposes_kind_and_qty(self):
        """pnl_final must carry `kind` + `qty` alongside `total_pnl` so
        callers can detect a holdings-sourced baseline and gate/pro-rate it
        (audit item #4) instead of blindly promoting a holding's full
        lifetime P&L onto an unrelated same-symbol positions row."""
        from backend.api.routes import positions as positions_mod

        src = positions_mod._BASELINE_PNL_CTE_SQL
        assert "SELECT account, symbol, total_pnl, kind, qty" in src
