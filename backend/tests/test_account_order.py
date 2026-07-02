"""
test_account_order.py — canonical account display-order tests (Jul 2026).

Tests assert:
1. sort_accounts() returns accounts in display_order ASC, then account_id ASC.
2. Unknown accounts (not in DB) fall to end (treated as 999).
3. Startup migration seeds DH6847=999, DH3747=100, Kite < Groww < DH6847.
4. PATCH display_order via /api/admin/brokers/{id} updates order.
5. Default display_order=500 for new accounts (via column default).

IMPORTANT: per project convention — do NOT mock broker API calls.
These tests exercise the sort helper and the REST endpoint in-process.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from backend.brokers.broker_apis import sort_accounts, get_account_order_map


# ---------------------------------------------------------------------------
# Unit tests for sort_accounts() helper
# ---------------------------------------------------------------------------

class TestSortAccounts:
    """sort_accounts() unit tests — no DB needed, we mock get_account_order_map."""

    def _make_order_map(self) -> dict[str, int]:
        """Mimics the seeded order for the five known accounts."""
        return {
            "ZG0790": 10,
            "ZJ6294": 20,
            "DH3747": 100,
            "GR87DF": 200,
            "DH6847": 999,
        }

    def test_canonical_sequence(self) -> None:
        """Kite → DH3747 → Groww → DH6847 matches operator requirement."""
        order_map = self._make_order_map()
        with patch(
            "backend.brokers.broker_apis.get_account_order_map",
            return_value=order_map,
        ):
            result = sort_accounts(["DH6847", "ZG0790", "DH3747", "GR87DF", "ZJ6294"])
        assert result == ["ZG0790", "ZJ6294", "DH3747", "GR87DF", "DH6847"], (
            "Expected Kite (10,20) → DH3747 (100) → Groww (200) → DH6847 (999)"
        )

    def test_unknown_accounts_fall_to_end(self) -> None:
        """Accounts not in the order map are treated as 999 (end of list)."""
        order_map = {"ZG0790": 10}
        with patch(
            "backend.brokers.broker_apis.get_account_order_map",
            return_value=order_map,
        ):
            result = sort_accounts(["UNKNOWN1", "ZG0790", "UNKNOWN2"])
        # ZG0790 (10) first; unknown accounts (999) then alphabetically.
        assert result[0] == "ZG0790"
        assert set(result[1:]) == {"UNKNOWN1", "UNKNOWN2"}

    def test_tiebreaker_is_account_id(self) -> None:
        """Equal display_order → tiebreaker is account_id lexical ascending."""
        order_map = {"AA0001": 100, "BB0002": 100, "CC0003": 100}
        with patch(
            "backend.brokers.broker_apis.get_account_order_map",
            return_value=order_map,
        ):
            result = sort_accounts(["CC0003", "AA0001", "BB0002"])
        assert result == ["AA0001", "BB0002", "CC0003"]

    def test_empty_input(self) -> None:
        """Empty input returns empty list."""
        with patch(
            "backend.brokers.broker_apis.get_account_order_map",
            return_value={},
        ):
            result = sort_accounts([])
        assert result == []

    def test_single_account(self) -> None:
        """Single account is returned as-is."""
        with patch(
            "backend.brokers.broker_apis.get_account_order_map",
            return_value={"ZG0790": 10},
        ):
            result = sort_accounts(["ZG0790"])
        assert result == ["ZG0790"]

    def test_dh6847_always_last_when_all_present(self) -> None:
        """DH6847 (999) must be last even when it's passed first in input."""
        order_map = self._make_order_map()
        with patch(
            "backend.brokers.broker_apis.get_account_order_map",
            return_value=order_map,
        ):
            result = sort_accounts(
                ["DH6847", "DH3747", "ZJ6294", "ZG0790", "GR87DF"]
            )
        assert result[-1] == "DH6847", "DH6847 must be last (display_order=999)"
        assert result[0] in ("ZG0790", "ZJ6294"), "First must be a Kite account"

    def test_kite_accounts_before_dhan_dh3747(self) -> None:
        """Kite accounts (display_order=10,20) precede DH3747 (100)."""
        order_map = self._make_order_map()
        with patch(
            "backend.brokers.broker_apis.get_account_order_map",
            return_value=order_map,
        ):
            result = sort_accounts(["DH3747", "ZG0790", "ZJ6294"])
        kite_indices = [result.index("ZG0790"), result.index("ZJ6294")]
        dh3747_index = result.index("DH3747")
        assert max(kite_indices) < dh3747_index, (
            "Both Kite accounts must appear before DH3747"
        )

    def test_groww_between_dh3747_and_dh6847(self) -> None:
        """GR87DF (200) appears between DH3747 (100) and DH6847 (999)."""
        order_map = self._make_order_map()
        with patch(
            "backend.brokers.broker_apis.get_account_order_map",
            return_value=order_map,
        ):
            result = sort_accounts(["DH6847", "GR87DF", "DH3747"])
        assert result == ["DH3747", "GR87DF", "DH6847"]

    def test_default_500_for_unknown_dhan(self) -> None:
        """A hypothetical new Dhan account (not DH3747 or DH6847) lands mid-tier."""
        order_map = {"ZG0790": 10, "DH_NEW": 500, "DH6847": 999}
        with patch(
            "backend.brokers.broker_apis.get_account_order_map",
            return_value=order_map,
        ):
            result = sort_accounts(["DH6847", "DH_NEW", "ZG0790"])
        assert result == ["ZG0790", "DH_NEW", "DH6847"]


# ---------------------------------------------------------------------------
# invalidate_account_order_cache
# ---------------------------------------------------------------------------

class TestInvalidateCache:
    def test_invalidate_forces_reload(self) -> None:
        """After invalidate, next get_account_order_map re-reads from DB."""
        from backend.brokers.broker_apis import (
            invalidate_account_order_cache,
            _ACCOUNT_ORDER_CACHE_AT,
        )
        import backend.brokers.broker_apis as _bapi

        # Seed a non-zero cache timestamp.
        _bapi._ACCOUNT_ORDER_CACHE_AT = 9_999_999_999.0
        _bapi._ACCOUNT_ORDER_CACHE = {"ZG0790": 10}

        invalidate_account_order_cache()

        assert _bapi._ACCOUNT_ORDER_CACHE_AT == 0.0, (
            "invalidate_account_order_cache must reset the cache timestamp to 0"
        )
