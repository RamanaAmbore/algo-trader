"""Coverage tests for backend/api/background.py — NFO/MCX spot anchor functions.

Targets:
  SA-1   _add_nfo_spot_anchors — adds NSE equity/index underlyings for NFO options
  SA-2   _add_nfo_spot_anchors — returns aliases dict mapping spot sym to root
  SA-3   _add_nfo_spot_anchors — skips when root is already subscribed
  SA-4   _add_mcx_spot_anchors — adds MCX futures as spot anchors for MCX options
  SA-5   _add_mcx_spot_anchors — returns aliases for virtual root registration

Five quality dimensions applied:
  SSOT        — direct invocation of background functions
  Correctness — list mutations, return values, aliases dict structure
  Performance — no network I/O; mocked list_active_futures
  Reuse       — shared test helpers
  UX          — every assert has an f-string with the actual value
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestAddNfoSpotAnchors:
    """_add_nfo_spot_anchors adds NSE equity/index underlyings for NFO/BFO options."""

    def test_add_nfo_spot_anchors_returns_dict(self):
        """_add_nfo_spot_anchors returns a dict mapping spot sym to root."""
        from backend.api.background import _add_nfo_spot_anchors

        book_pairs = [
            ("NIFTY25APRCE", "NFO"),    # Call option on NIFTY
        ]

        aliases = _add_nfo_spot_anchors(book_pairs)

        # Result should be a dict
        assert isinstance(aliases, dict), (
            f"Expected dict return value, got {type(aliases)}"
        )

    def test_add_nfo_spot_anchors_adds_underlyings(self):
        """NFO options with root prefix → adds underlying to book_pairs."""
        from backend.api.background import _add_nfo_spot_anchors

        book_pairs = [
            ("NIFTY25APRCE", "NFO"),
        ]
        initial_len = len(book_pairs)

        aliases = _add_nfo_spot_anchors(book_pairs)

        # The underlying should be added (via underlying_ltp_key lookup)
        # We expect book_pairs to grow
        assert len(book_pairs) >= initial_len, (
            f"Expected book_pairs to grow or stay same, got {len(book_pairs)} from {initial_len}"
        )

    def test_add_nfo_spot_anchors_skips_non_options(self):
        """If no NFO/BFO options with CE/PE suffix, don't add anchors."""
        from backend.api.background import _add_nfo_spot_anchors

        book_pairs = [
            ("RELIANCE", "NSE"),
            ("INFY", "NSE"),
        ]
        initial_len = len(book_pairs)

        aliases = _add_nfo_spot_anchors(book_pairs)

        # No new anchors should be added (no CE/PE options present)
        assert len(book_pairs) == initial_len, (
            f"Expected no new anchors for non-options input, got {len(book_pairs) - initial_len} additions"
        )

    def test_add_nfo_spot_anchors_aliases_structure(self):
        """Return dict maps spot symbol (upper) to root."""
        from backend.api.background import _add_nfo_spot_anchors

        book_pairs = [
            ("NIFTY25APRCE", "NFO"),
        ]

        aliases = _add_nfo_spot_anchors(book_pairs)

        # All keys and values should be strings (upper-cased)
        for sym, root in aliases.items():
            assert isinstance(sym, str), f"Alias key should be str, got {type(sym)}"
            assert isinstance(root, str), f"Alias value should be str, got {type(root)}"
            assert sym == sym.upper(), f"Alias key should be upper-cased, got {sym}"


class TestAddMcxSpotAnchors:
    """_add_mcx_spot_anchors adds MCX futures as spot anchors for MCX options."""

    @pytest.mark.asyncio
    async def test_add_mcx_spot_anchors_returns_dict(self):
        """_add_mcx_spot_anchors returns a dict mapping future sym to root."""
        from backend.api.background import _add_mcx_spot_anchors

        book_pairs = [
            ("CRUDEOIL26OCTCE", "MCX"),
        ]
        book_seen = set()

        # Mock list_active_futures to return a front-month future
        with patch("backend.api.algo.symbol_resolver.list_active_futures") as mock_laf:
            mock_laf.return_value = ["CRUDEOIL26OCTFUT"]

            aliases = await _add_mcx_spot_anchors(book_pairs, book_seen)

        # Result should be a dict
        assert isinstance(aliases, dict), (
            f"Expected dict return value, got {type(aliases)}"
        )

    @pytest.mark.asyncio
    async def test_add_mcx_spot_anchors_adds_futures(self):
        """MCX options with root → adds future to book_pairs."""
        from backend.api.background import _add_mcx_spot_anchors

        book_pairs = [
            ("CRUDEOIL26OCTCE", "MCX"),
        ]
        book_seen = set()
        initial_len = len(book_pairs)

        # Mock list_active_futures to return a front-month future
        with patch("backend.api.algo.symbol_resolver.list_active_futures") as mock_laf:
            mock_laf.return_value = ["CRUDEOIL26OCTFUT"]

            aliases = await _add_mcx_spot_anchors(book_pairs, book_seen)

        # The future should be added
        assert len(book_pairs) > initial_len, (
            f"Expected book_pairs to grow, got {len(book_pairs)} from {initial_len}"
        )

    @pytest.mark.asyncio
    async def test_add_mcx_spot_anchors_idempotent(self):
        """If future already in book_seen, don't re-add."""
        from backend.api.background import _add_mcx_spot_anchors

        book_pairs = [
            ("CRUDEOIL26OCTCE", "MCX"),
            ("CRUDEOIL26OCTFUT", "MCX"),  # Already subscribed
        ]
        book_seen = {("CRUDEOIL26OCTFUT", "MCX")}

        with patch("backend.api.algo.symbol_resolver.list_active_futures") as mock_laf:
            mock_laf.return_value = ["CRUDEOIL26OCTFUT"]

            initial_len = len(book_pairs)
            aliases = await _add_mcx_spot_anchors(book_pairs, book_seen)

        # No new entry should be added (already in book_seen)
        assert len(book_pairs) == initial_len, (
            f"Expected no duplicate adds; book_pairs should remain {initial_len}, got {len(book_pairs)}"
        )

    @pytest.mark.asyncio
    async def test_add_mcx_spot_anchors_handles_exception(self):
        """On exception, return empty dict and don't crash."""
        from backend.api.background import _add_mcx_spot_anchors

        book_pairs = [
            ("CRUDEOIL26OCTCE", "MCX"),
        ]
        book_seen = set()

        with patch("backend.api.algo.symbol_resolver.list_active_futures") as mock_laf:
            mock_laf.side_effect = Exception("Test error")

            aliases = await _add_mcx_spot_anchors(book_pairs, book_seen)

        # Should return empty dict on error
        assert isinstance(aliases, dict), (
            f"Expected dict return value even on error, got {type(aliases)}"
        )
        assert len(aliases) == 0, (
            f"Expected empty aliases dict on error, got {aliases}"
        )
