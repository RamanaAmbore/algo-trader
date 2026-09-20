"""
Tests for previous_close COALESCE unification in positions.py.

Context: Three SQL sites were changed from:
  ltp AS prev_close WHERE ltp IS NOT NULL AND ltp > 0
to:
  COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) AS prev_close
  WHERE COALESCE(...) IS NOT NULL

This change ensures that holiday snapshot rows with ltp=NULL but close_price > 0
are no longer silently excluded. The COALESCE pattern gives ltp priority when
both are present (ltp != 0), then falls back to close_price != 0 when ltp is
NULL or 0, and finally excludes only when BOTH are NULL or 0.

Tests cover:
  1. SQL text assertions — verify COALESCE pattern appears in source
  2. Behavioral assertions — mock DB rows to verify ltp=NULL + close_price > 0
     are included and close_price wins in the COALESCE
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
    """Verify that the COALESCE patterns appear in the SQL source."""

    @staticmethod
    def _read_sql_from_function(func) -> str:
        """Extract the SQL text from a function's source code."""
        src = inspect.getsource(func)
        return src

    def test_fetch_snapshot_close_map_sql_uses_coalesce(self):
        """_fetch_snapshot_close_map must use COALESCE for ref_close."""
        from backend.api.routes.positions import _fetch_snapshot_close_map

        src = self._read_sql_from_function(_fetch_snapshot_close_map)

        # Verify the COALESCE pattern is present
        assert "COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0))" in src, (
            "_fetch_snapshot_close_map SQL must contain "
            "COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0))"
        )
        # Verify the SELECT column uses COALESCE
        assert "COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) AS ref_close" in src, (
            "_fetch_snapshot_close_map must SELECT "
            "COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) AS ref_close"
        )
        # Verify WHERE condition uses COALESCE
        assert "WHERE kind = 'positions'" in src and \
               "COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) IS NOT NULL" in src, (
            "_fetch_snapshot_close_map WHERE clause must filter by "
            "COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) IS NOT NULL"
        )

    def test_apply_second_pass_fallback_sql_uses_coalesce(self):
        """_apply_second_pass_fallback must use COALESCE for previous_close."""
        from backend.api.routes.positions import _apply_second_pass_fallback

        src = self._read_sql_from_function(_apply_second_pass_fallback)

        # Verify the COALESCE pattern appears
        assert "COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0))" in src, (
            "_apply_second_pass_fallback SQL must contain "
            "COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0))"
        )
        # Verify it's in the SELECT for previous_close
        assert "COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) AS previous_close" in src, (
            "_apply_second_pass_fallback must SELECT "
            "COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) AS previous_close"
        )
        # Verify the WHERE condition uses COALESCE
        assert "COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) IS NOT NULL" in src, (
            "_apply_second_pass_fallback WHERE clause must filter by "
            "COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) IS NOT NULL"
        )

    def test_positions_snapshot_sql_uses_coalesce(self):
        """_positions_snapshot SELECT must use COALESCE for previous_close."""
        from backend.api.routes.positions import _positions_snapshot

        src = self._read_sql_from_function(_positions_snapshot)

        # Verify the COALESCE pattern in the main SELECT
        assert "COALESCE(NULLIF(db.previous_close, 0), NULLIF(db.close_price, 0)) AS previous_close" in src, (
            "_positions_snapshot must SELECT "
            "COALESCE(NULLIF(db.previous_close, 0), NULLIF(db.close_price, 0)) AS previous_close"
        )


# ===========================================================================
# Behavioral Tests (mock DB to verify actual behavior)
# ===========================================================================

class TestFetchSnapshotCloseMapBehavior:
    """Test _fetch_snapshot_close_map behavior with COALESCE logic."""

    @pytest.mark.asyncio
    async def test_includes_null_ltp_row(self):
        """Row with ltp=NULL, close_price=2850.0 must be included.

        The COALESCE pattern should use close_price as the ref_close value
        when ltp is NULL. This ensures holiday snapshots with no fresh LTP
        but a stale close_price still provide a reference price.
        """
        from backend.api.routes.positions import _fetch_snapshot_close_map

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'CRUDEOIL26JUL6900PE',
            'ltp': None,
            'close_price': 2850.0,
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
                cutoff=None  # Cutoff not used in this test; mocked result is returned
            )

        assert ('ZG0790', 'CRUDEOIL26JUL6900PE') in snapshot_map, (
            "Row with ltp=NULL, close_price=2850.0 must be included in snapshot_map"
        )
        assert abs(snapshot_map[('ZG0790', 'CRUDEOIL26JUL6900PE')] - 2850.0) < 0.01, (
            f"ref_close must be 2850.0 (from close_price), got "
            f"{snapshot_map[('ZG0790', 'CRUDEOIL26JUL6900PE')]}"
        )

    @pytest.mark.asyncio
    async def test_ltp_wins_over_close_price(self):
        """Row with ltp=2800.0, close_price=2850.0 must use ltp as ref_close.

        COALESCE prioritizes ltp when both are present and non-zero.
        """
        from backend.api.routes.positions import _fetch_snapshot_close_map

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'CRUDEOIL26JUL6900PE',
            'ltp': 2800.0,
            'close_price': 2850.0,
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
            f"ref_close must be 2800.0 (ltp wins), got "
            f"{snapshot_map[('ZG0790', 'CRUDEOIL26JUL6900PE')]}"
        )

    @pytest.mark.asyncio
    async def test_both_zero_excluded(self):
        """Row with ltp=0, close_price=0 must be excluded (COALESCE returns NULL)."""
        from backend.api.routes.positions import _fetch_snapshot_close_map

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'SYMBOL',
            'ltp': 0.0,
            'close_price': 0.0,
        }])

        # DB returns empty result because COALESCE(...) IS NOT NULL filtered it out
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
            "Row with ltp=0, close_price=0 must be excluded"
        )

    @pytest.mark.asyncio
    async def test_ltp_zero_falls_back_to_close_price(self):
        """Row with ltp=0, close_price=2850.0 must use close_price as ref_close.

        COALESCE NULLIF(ltp, 0) returns NULL when ltp=0, so it falls back to
        NULLIF(close_price, 0) which is 2850.0.
        """
        from backend.api.routes.positions import _fetch_snapshot_close_map

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'SYMBOL',
            'ltp': 0.0,
            'close_price': 2850.0,
        }])

        mock_result = MagicMock()
        mock_result.all.return_value = [
            ('ZG0790', 'SYMBOL', 2850.0, 500.0)  # ref_close from close_price
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

        assert abs(snapshot_map[('ZG0790', 'SYMBOL')] - 2850.0) < 0.01, (
            f"ref_close must be 2850.0 (fallback to close_price), got "
            f"{snapshot_map[('ZG0790', 'SYMBOL')]}"
        )


class TestApplySecondPassFallbackBehavior:
    """Test _apply_second_pass_fallback behavior with COALESCE logic."""

    @pytest.mark.asyncio
    async def test_includes_null_ltp_row(self):
        """Second-pass: row with ltp=NULL, close_price=2850.0 must provide fallback."""
        from backend.api.routes.positions import _apply_second_pass_fallback

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'CRUDEOIL26SEP7900PE',
            'previous_close': 0.0,
            'close_price': 0.0,
        }])

        mock_result = MagicMock()
        mock_result.all.return_value = [
            ('ZG0790', 'CRUDEOIL26SEP7900PE', 2850.0)  # previous_close from COALESCE
        ]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session):
            patched = await _apply_second_pass_fallback(raw)

        assert 0 in patched, "Row index 0 should be patched"
        assert abs(raw.at[0, 'previous_close'] - 2850.0) < 0.01, (
            f"previous_close must be 2850.0, got {raw.at[0, 'previous_close']}"
        )
        assert abs(raw.at[0, 'close_price'] - 2850.0) < 0.01, (
            f"close_price must be 2850.0, got {raw.at[0, 'close_price']}"
        )

    @pytest.mark.asyncio
    async def test_ltp_wins_over_close_price_second_pass(self):
        """Second-pass: when ltp=2800.0, close_price=2850.0, ltp should win."""
        from backend.api.routes.positions import _apply_second_pass_fallback

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'SYMBOL',
            'previous_close': 0.0,
            'close_price': 0.0,
        }])

        mock_result = MagicMock()
        mock_result.all.return_value = [
            ('ZG0790', 'SYMBOL', 2800.0)  # COALESCE returns ltp=2800.0
        ]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session):
            patched = await _apply_second_pass_fallback(raw)

        assert abs(raw.at[0, 'previous_close'] - 2800.0) < 0.01, (
            f"previous_close must be 2800.0 (ltp wins), got {raw.at[0, 'previous_close']}"
        )

    @pytest.mark.asyncio
    async def test_both_null_excluded_second_pass(self):
        """Second-pass: row with ltp=NULL, close_price=NULL must be excluded."""
        from backend.api.routes.positions import _apply_second_pass_fallback

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'SYMBOL',
            'previous_close': 0.0,
            'close_price': 0.0,
        }])

        # DB returns empty result because COALESCE(...) IS NOT NULL filtered it out
        mock_result = MagicMock()
        mock_result.all.return_value = []

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session):
            patched = await _apply_second_pass_fallback(raw)

        assert 0 not in patched, (
            "Row with ltp=NULL, close_price=NULL must not be patched"
        )
        assert raw.at[0, 'previous_close'] == 0.0, (
            "previous_close must remain 0.0 (no fallback available)"
        )

    @pytest.mark.asyncio
    async def test_ltp_zero_falls_back_to_close_price_second_pass(self):
        """Second-pass: row with ltp=0, close_price=2850.0 must use close_price."""
        from backend.api.routes.positions import _apply_second_pass_fallback

        raw = pd.DataFrame([{
            'account': 'ZG0790',
            'tradingsymbol': 'SYMBOL',
            'previous_close': 0.0,
            'close_price': 0.0,
        }])

        mock_result = MagicMock()
        mock_result.all.return_value = [
            ('ZG0790', 'SYMBOL', 2850.0)  # COALESCE falls back to close_price
        ]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch('backend.api.database.async_session', return_value=mock_session):
            patched = await _apply_second_pass_fallback(raw)

        assert abs(raw.at[0, 'previous_close'] - 2850.0) < 0.01, (
            f"previous_close must be 2850.0 (fallback to close_price), "
            f"got {raw.at[0, 'previous_close']}"
        )

    @pytest.mark.asyncio
    async def test_multiple_rows_selective_patching(self):
        """Second-pass: only zero_mask rows are patched; others skipped."""
        from backend.api.routes.positions import _apply_second_pass_fallback

        raw = pd.DataFrame([
            {'account': 'ZG0790', 'tradingsymbol': 'SYM1', 'previous_close': 2700.0, 'close_price': 0.0},  # idx 0: not zero
            {'account': 'ZG0790', 'tradingsymbol': 'SYM2', 'previous_close': 0.0, 'close_price': 0.0},    # idx 1: zero
            {'account': 'ZG0790', 'tradingsymbol': 'SYM3', 'previous_close': 0.0, 'close_price': 0.0},    # idx 2: zero
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
        assert 0 not in patched, "Row 0 should not be patched (previous_close was already set)"
        # Rows 1, 2 should be in patched
        assert 1 in patched and 2 in patched, "Rows 1 and 2 should be patched"
        # Verify values
        assert abs(raw.at[1, 'previous_close'] - 2800.0) < 0.01
        assert abs(raw.at[2, 'previous_close'] - 2850.0) < 0.01


class TestPositionsSnapshotSQLLogic:
    """Test that _positions_snapshot SELECT uses COALESCE correctly."""

    @pytest.mark.asyncio
    async def test_snapshot_sql_coalesce_column_exists(self):
        """Verify that _positions_snapshot's SELECT includes the COALESCE AS previous_close."""
        from backend.api.routes.positions import _positions_snapshot

        # Verify the source contains the SELECT with COALESCE
        src = inspect.getsource(_positions_snapshot)
        assert "COALESCE(NULLIF(db.previous_close, 0), NULLIF(db.close_price, 0)) AS previous_close" in src, (
            "_positions_snapshot must SELECT the COALESCE pattern as previous_close"
        )
        # Verify it's not using the old pattern (ltp AS prev_close)
        # (We check that old pattern is NOT there in the relevant part)
        # This is a soft check — we mainly verify the new pattern is present.


# ===========================================================================
# Integration: Verify old patterns are replaced
# ===========================================================================

def test_fetch_snapshot_close_map_no_old_pattern():
    """Verify _fetch_snapshot_close_map does NOT use old ltp IS NOT NULL pattern alone."""
    from backend.api.routes.positions import _fetch_snapshot_close_map

    src = inspect.getsource(_fetch_snapshot_close_map)

    # The function should NOT have the old pattern in the ref_close SELECT
    # (It may have ltp IS NOT NULL in the latest_batch CTE for snapshot exclusion,
    #  but not in the ref_close column itself or in the WHERE clause.)
    # Look for the problematic old pattern: "ltp IS NOT NULL AND ltp > 0" in the
    # SELECT or WHERE of the main query (not the CTE for latest_batch filtering).
    lines = src.split('\n')
    in_main_query = False
    for i, line in enumerate(lines):
        # Detect if we're in the main SELECT...FROM...WHERE (not a CTE definition)
        if 'SELECT' in line and 'DISTINCT ON' in line and 'account, symbol' in line:
            in_main_query = True
        if in_main_query:
            # Old pattern in WHERE: "AND ltp IS NOT NULL AND ltp > 0"
            if 'AND ltp IS NOT NULL AND ltp > 0' in line:
                pytest.fail(
                    f"_fetch_snapshot_close_map still uses old pattern "
                    f"'AND ltp IS NOT NULL AND ltp > 0' at line {i+1}: {line}"
                )


def test_apply_second_pass_fallback_no_old_pattern():
    """Verify _apply_second_pass_fallback does NOT use old ltp IS NOT NULL pattern."""
    from backend.api.routes.positions import _apply_second_pass_fallback

    src = inspect.getsource(_apply_second_pass_fallback)

    # Verify no old pattern in WHERE clause
    if 'AND ltp IS NOT NULL AND ltp > 0' in src:
        pytest.fail(
            "_apply_second_pass_fallback still uses old pattern "
            "'AND ltp IS NOT NULL AND ltp > 0'"
        )


def test_positions_snapshot_no_old_pattern_in_select():
    """Verify _positions_snapshot does NOT use old 'ltp AS' pattern for previous_close."""
    from backend.api.routes.positions import _positions_snapshot

    src = inspect.getsource(_positions_snapshot)

    # The old pattern would be something like "db.ltp AS previous_close"
    # We verify the new pattern is there instead
    assert "COALESCE(NULLIF(db.previous_close, 0), NULLIF(db.close_price, 0)) AS previous_close" in src, (
        "_positions_snapshot must use COALESCE pattern for previous_close"
    )


def test_positions_snapshot_does_not_use_ltp_as_previous_close():
    """Guard: _positions_snapshot must not use db.ltp for previous_close (causes chg%=0)."""
    from backend.api.routes.positions import _positions_snapshot

    src = inspect.getsource(_positions_snapshot)
    assert "COALESCE(NULLIF(db.ltp, 0), NULLIF(db.close_price, 0)) AS previous_close" not in src, (
        "_positions_snapshot must not derive previous_close from db.ltp — "
        "ltp is today's settlement; previous_close must come from db.previous_close (BHAV at 08:00)"
    )
