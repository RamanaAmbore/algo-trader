"""
Tests for prev_close unification in positions.py.

Context: SQL query sites were simplified from:
  COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) AS ref_close
to:
  ltp AS ref_close WHERE ltp IS NOT NULL AND ltp > 0

This is consistent with the rename refactor: close_price/previous_close →
prev_close throughout. The daily_book column is now `prev_close` and the
DataFrame column is also `prev_close`. The COALESCE fallback to close_price
was removed because the canonical source for prior-session settlement LTP is
now daily_book.ltp (not close_price, which is the stale BHAV copy).

Tests cover:
  1. SQL text assertions — verify the simplified ltp pattern appears in source
  2. Behavioral assertions — mock DB rows to verify correct prev_close population
  3. _apply_second_pass_fallback uses `prev_close` column (not `previous_close`)
  4. _positions_snapshot uses `db.prev_close AS previous_close` in SELECT
"""

from __future__ import annotations

import asyncio
import inspect
import pandas as pd
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ===========================================================================
# SQL Text Tests (source-level verification)
# ===========================================================================

class TestSQLTextPatterns:
    """Verify that the simplified ltp pattern appears in the SQL source."""

    @staticmethod
    def _read_sql_from_function(func) -> str:
        """Extract the SQL text from a function's source code."""
        src = inspect.getsource(func)
        return src

    def test_fetch_snapshot_close_map_sql_uses_ltp_as_ref_close(self):
        """_fetch_snapshot_close_map must use `ltp AS ref_close` for ref_close."""
        from backend.api.routes.positions import _fetch_snapshot_close_map

        src = self._read_sql_from_function(_fetch_snapshot_close_map)

        # Verify the simplified pattern is present — ltp is the canonical source
        assert "ltp AS ref_close" in src, (
            "_fetch_snapshot_close_map SQL must contain 'ltp AS ref_close'"
        )
        # Verify WHERE condition filters by ltp IS NOT NULL AND ltp > 0
        assert "ltp IS NOT NULL AND ltp > 0" in src, (
            "_fetch_snapshot_close_map WHERE clause must filter by "
            "'ltp IS NOT NULL AND ltp > 0'"
        )
        # Verify it queries positions kind
        assert "kind = 'positions'" in src, (
            "_fetch_snapshot_close_map must query kind = 'positions'"
        )

    def test_apply_second_pass_fallback_sql_uses_ltp_as_prev_close(self):
        """_apply_second_pass_fallback must use `ltp AS prev_close` in SELECT."""
        from backend.api.routes.positions import _apply_second_pass_fallback

        src = self._read_sql_from_function(_apply_second_pass_fallback)

        # Verify the simplified pattern is present
        assert "ltp AS prev_close" in src, (
            "_apply_second_pass_fallback SQL must contain 'ltp AS prev_close'"
        )
        # Verify WHERE condition filters by ltp IS NOT NULL AND ltp > 0
        assert "ltp IS NOT NULL AND ltp > 0" in src, (
            "_apply_second_pass_fallback WHERE clause must filter by "
            "'ltp IS NOT NULL AND ltp > 0'"
        )

    def test_positions_snapshot_sql_uses_db_prev_close(self):
        """_positions_snapshot SELECT must use `db.prev_close AS previous_close`."""
        from backend.api.routes.positions import _positions_snapshot

        src = self._read_sql_from_function(_positions_snapshot)

        # Verify the renamed column is used
        assert "db.prev_close AS previous_close" in src, (
            "_positions_snapshot must SELECT 'db.prev_close AS previous_close' "
            "(renamed from db.previous_close)"
        )


# ===========================================================================
# Behavioral Tests (mock DB to verify actual behavior)
# ===========================================================================

class TestFetchSnapshotCloseMapBehavior:
    """Test _fetch_snapshot_close_map behavior — ltp-only logic."""

    @pytest.mark.asyncio
    async def test_includes_non_zero_ltp_row(self):
        """Row with ltp=2850.0 must be included in snapshot_map."""
        from backend.api.routes.positions import _fetch_snapshot_close_map

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'CRUDEOIL26JUL6900PE',
            'prev_close': 0.0,
        }])

        mock_result = MagicMock()
        mock_result.all.return_value = [
            ('ZG0790', 'CRUDEOIL26JUL6900PE', 2850.0, 500.0)  # ref_close, total_pnl
        ]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session):
            snapshot_map, prev_pnl_map = await _fetch_snapshot_close_map(
                raw,
                cutoff=None
            )

        assert ('ZG0790', 'CRUDEOIL26JUL6900PE') in snapshot_map, (
            "Row with ltp=2850.0 must be included in snapshot_map"
        )
        assert abs(snapshot_map[('ZG0790', 'CRUDEOIL26JUL6900PE')] - 2850.0) < 0.01, (
            f"ref_close must be 2850.0 (from ltp), got "
            f"{snapshot_map[('ZG0790', 'CRUDEOIL26JUL6900PE')]}"
        )

    @pytest.mark.asyncio
    async def test_ltp_used_as_ref_close(self):
        """Row with ltp=2800.0 must use ltp as ref_close."""
        from backend.api.routes.positions import _fetch_snapshot_close_map

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'CRUDEOIL26JUL6900PE',
            'prev_close': 0.0,
        }])

        mock_result = MagicMock()
        mock_result.all.return_value = [
            ('ZG0790', 'CRUDEOIL26JUL6900PE', 2800.0, 500.0)  # ref_close=ltp, total_pnl
        ]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session):
            snapshot_map, prev_pnl_map = await _fetch_snapshot_close_map(
                raw,
                cutoff=None
            )

        assert abs(snapshot_map[('ZG0790', 'CRUDEOIL26JUL6900PE')] - 2800.0) < 0.01, (
            f"ref_close must be 2800.0 (ltp), got "
            f"{snapshot_map[('ZG0790', 'CRUDEOIL26JUL6900PE')]}"
        )

    @pytest.mark.asyncio
    async def test_zero_ltp_excluded_by_db(self):
        """Row with ltp=0 or NULL is filtered out by the DB query (ltp > 0)."""
        from backend.api.routes.positions import _fetch_snapshot_close_map

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'SYMBOL',
            'prev_close': 0.0,
        }])

        # DB returns empty result because ltp IS NOT NULL AND ltp > 0 filtered it out
        mock_result = MagicMock()
        mock_result.all.return_value = []

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session):
            snapshot_map, prev_pnl_map = await _fetch_snapshot_close_map(
                raw,
                cutoff=None
            )

        assert ('ZG0790', 'SYMBOL') not in snapshot_map, (
            "Row with ltp=0 must be excluded (filtered by DB)"
        )

    @pytest.mark.asyncio
    async def test_total_pnl_stored_in_prev_pnl_map(self):
        """total_pnl must be stored in prev_pnl_map alongside ref_close."""
        from backend.api.routes.positions import _fetch_snapshot_close_map

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'SYMBOL',
            'prev_close': 0.0,
        }])

        mock_result = MagicMock()
        mock_result.all.return_value = [
            ('ZG0790', 'SYMBOL', 2850.0, 12500.0)  # ref_close, total_pnl
        ]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session):
            snapshot_map, prev_pnl_map = await _fetch_snapshot_close_map(
                raw,
                cutoff=None
            )

        assert abs(prev_pnl_map[('ZG0790', 'SYMBOL')] - 12500.0) < 0.01, (
            f"prev_pnl_map must have total_pnl=12500.0, got "
            f"{prev_pnl_map[('ZG0790', 'SYMBOL')]}"
        )


class TestApplySecondPassFallbackBehavior:
    """Test _apply_second_pass_fallback behavior — reads `prev_close` column."""

    @pytest.mark.asyncio
    async def test_patches_zero_prev_close_rows(self):
        """Second-pass: row with prev_close=0.0 must be patched with ltp from DB."""
        from backend.api.routes.positions import _apply_second_pass_fallback

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'CRUDEOIL26SEP7900PE',
            'prev_close': 0.0,  # renamed from previous_close
        }])

        mock_result = MagicMock()
        mock_result.all.return_value = [
            ('ZG0790', 'CRUDEOIL26SEP7900PE', 2850.0)  # prev_close from ltp
        ]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session):
            patched = await _apply_second_pass_fallback(raw)

        assert 0 in patched, "Row index 0 should be patched"
        assert abs(raw.at[0, 'prev_close'] - 2850.0) < 0.01, (
            f"prev_close must be 2850.0, got {raw.at[0, 'prev_close']}"
        )

    @pytest.mark.asyncio
    async def test_ltp_stored_as_prev_close_second_pass(self):
        """Second-pass: ltp=2800.0 from DB is stored as prev_close."""
        from backend.api.routes.positions import _apply_second_pass_fallback

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'SYMBOL',
            'prev_close': 0.0,
        }])

        mock_result = MagicMock()
        mock_result.all.return_value = [
            ('ZG0790', 'SYMBOL', 2800.0)  # ltp=2800.0 returned as prev_close
        ]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session):
            patched = await _apply_second_pass_fallback(raw)

        assert abs(raw.at[0, 'prev_close'] - 2800.0) < 0.01, (
            f"prev_close must be 2800.0 (from ltp), got {raw.at[0, 'prev_close']}"
        )

    @pytest.mark.asyncio
    async def test_no_db_result_leaves_prev_close_unchanged(self):
        """Second-pass: when DB returns nothing, prev_close stays at 0.0."""
        from backend.api.routes.positions import _apply_second_pass_fallback

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'SYMBOL',
            'prev_close': 0.0,
        }])

        # DB returns empty result
        mock_result = MagicMock()
        mock_result.all.return_value = []

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session):
            patched = await _apply_second_pass_fallback(raw)

        assert 0 not in patched, (
            "Row with no DB result must not be patched"
        )
        assert raw.at[0, 'prev_close'] == 0.0, (
            "prev_close must remain 0.0 (no fallback available)"
        )

    @pytest.mark.asyncio
    async def test_non_zero_prev_close_rows_skipped(self):
        """Second-pass: rows where prev_close != 0.0 are not patched (skipped early)."""
        from backend.api.routes.positions import _apply_second_pass_fallback

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'SYMBOL',
            'prev_close': 2700.0,  # already set — should not be queried
        }])

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=MagicMock())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session) as mock_db:
            patched = await _apply_second_pass_fallback(raw)

        # No patching should occur; DB should not be called
        assert 0 not in patched, "Row 0 should not be patched (prev_close was already set)"
        assert raw.at[0, 'prev_close'] == 2700.0, "prev_close must remain 2700.0"
        # async_session should NOT be called (zero_mask has no True values)
        assert mock_db.call_count == 0, (
            "_apply_second_pass_fallback must not query DB when no rows need patching"
        )

    @pytest.mark.asyncio
    async def test_multiple_rows_selective_patching(self):
        """Second-pass: only zero_mask rows are patched; others skipped."""
        from backend.api.routes.positions import _apply_second_pass_fallback

        raw = pd.DataFrame([
            {'account': 'ZG0790', 'tradingsymbol': 'SYM1', 'prev_close': 2700.0},  # idx 0: not zero
            {'account': 'ZG0790', 'tradingsymbol': 'SYM2', 'prev_close': 0.0},     # idx 1: zero
            {'account': 'ZG0790', 'tradingsymbol': 'SYM3', 'prev_close': 0.0},     # idx 2: zero
        ])

        mock_result = MagicMock()
        mock_result.all.return_value = [
            ('ZG0790', 'SYM2', 2800.0),
            ('ZG0790', 'SYM3', 2850.0),
        ]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session):
            patched = await _apply_second_pass_fallback(raw)

        # Row 0 not in patched (was already non-zero)
        assert 0 not in patched, "Row 0 should not be patched (prev_close was already set)"
        # Rows 1, 2 should be in patched
        assert 1 in patched and 2 in patched, "Rows 1 and 2 should be patched"
        # Verify values
        assert abs(raw.at[1, 'prev_close'] - 2800.0) < 0.01
        assert abs(raw.at[2, 'prev_close'] - 2850.0) < 0.01


class TestPositionsSnapshotSQLLogic:
    """Test that _positions_snapshot SELECT uses db.prev_close correctly."""

    @pytest.mark.asyncio
    async def test_snapshot_sql_uses_renamed_column(self):
        """Verify that _positions_snapshot's SELECT uses db.prev_close AS previous_close."""
        from backend.api.routes.positions import _positions_snapshot

        src = inspect.getsource(_positions_snapshot)
        assert "db.prev_close AS previous_close" in src, (
            "_positions_snapshot must SELECT 'db.prev_close AS previous_close' "
            "after the rename refactor"
        )
        # Verify it's not using the old column name directly
        # (The column is now `prev_close` in daily_book, not `previous_close`)
        assert "db.previous_close AS previous_close" not in src, (
            "_positions_snapshot must not use 'db.previous_close' — "
            "the DB column was renamed to prev_close"
        )


# ===========================================================================
# Integration: Verify current patterns are present
# ===========================================================================

def test_fetch_snapshot_close_map_uses_ltp_pattern():
    """Verify _fetch_snapshot_close_map uses ltp AS ref_close (not COALESCE)."""
    from backend.api.routes.positions import _fetch_snapshot_close_map

    src = inspect.getsource(_fetch_snapshot_close_map)

    # The function should use the simplified ltp-only pattern
    assert "ltp AS ref_close" in src, (
        "_fetch_snapshot_close_map must use 'ltp AS ref_close' "
        "(COALESCE fallback to close_price was removed)"
    )


def test_apply_second_pass_fallback_reads_prev_close_column():
    """Verify _apply_second_pass_fallback reads raw['prev_close'] not raw['previous_close']."""
    from backend.api.routes.positions import _apply_second_pass_fallback

    src = inspect.getsource(_apply_second_pass_fallback)

    # The function must reference the renamed column
    assert "prev_close" in src, (
        "_apply_second_pass_fallback must use 'prev_close' column (not 'previous_close')"
    )
    # The old column name must not appear as a DataFrame access
    assert "raw['previous_close']" not in src, (
        "_apply_second_pass_fallback must not access raw['previous_close'] — "
        "DataFrame column was renamed to prev_close"
    )


def test_positions_snapshot_uses_prev_close_db_column():
    """Verify _positions_snapshot uses db.prev_close from daily_book."""
    from backend.api.routes.positions import _positions_snapshot

    src = inspect.getsource(_positions_snapshot)
    assert "db.prev_close" in src, (
        "_positions_snapshot must reference 'db.prev_close' (renamed DB column)"
    )


def test_positions_snapshot_does_not_use_old_previous_close_column():
    """Guard: _positions_snapshot must not use db.previous_close (old column name)."""
    from backend.api.routes.positions import _positions_snapshot

    src = inspect.getsource(_positions_snapshot)
    # Specific guard: the old column reference pattern should not appear
    assert "db.previous_close AS previous_close" not in src, (
        "_positions_snapshot must not use db.previous_close — "
        "the daily_book column was renamed to prev_close"
    )
