"""
2026-10 audit fix: NAV's cash leg silently filled a genuinely missing
cash figure to ₹0.0 (via `fill_null(0.0)` in `_funds_from_df` and
`float(getattr(row, "cash", 0) or 0)` in `_fetch_funds_from_cache`)
with no `_UNDERSTATED_TAG` added — so `write_nav_snapshot()`'s
skip-on-understated policy never kicked in for a genuine broker-side
gap (e.g. Dhan's `sodLimit` key absent from an otherwise-successful
response), and the resulting NAV silently looked like a confirmed,
clean number.

Covers:
  - `_funds_null_cash_accounts` (the new sibling helper) in isolation.
  - `_fetch_funds_from_broker`'s wiring of that helper into `errors`.
  - `_fetch_funds_from_cache`'s parallel check on a None `FundsRow.cash`.
  - The deliberate scope decision to exclude `option_premium` (Groww has
    no confirmed source field for it at all — see
    `groww.py:_groww_margin_utilised`'s docstring), so a null premium
    must NOT be tagged understated.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from backend.api.algo.nav import (
    _UNDERSTATED_TAG,
    _funds_null_cash_accounts,
    _fetch_funds_from_broker,
    _fetch_funds_from_cache,
)


# ---------------------------------------------------------------------------
# _funds_null_cash_accounts
# ---------------------------------------------------------------------------

class TestFundsNullCashAccounts:
    def test_flags_account_with_null_cash_column(self):
        df = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0},
            {"account": "GR87DF", "avail opening_balance": None},
        ])
        result = _funds_null_cash_accounts(df)
        assert result == ["GR87DF"]

    def test_falls_back_to_cash_column_when_opening_balance_absent(self):
        df = pd.DataFrame([
            {"account": "ZG0790", "cash": None},
        ])
        result = _funds_null_cash_accounts(df)
        assert result == ["ZG0790"]

    def test_no_flag_when_cash_is_a_real_zero(self):
        """A confirmed real ₹0 is NOT a gap — must not be flagged."""
        df = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 0.0},
        ])
        result = _funds_null_cash_accounts(df)
        assert result == []

    def test_empty_df_returns_empty_list(self):
        assert _funds_null_cash_accounts(pd.DataFrame()) == []
        assert _funds_null_cash_accounts(None) == []

    def test_no_account_column_returns_empty_list(self):
        df = pd.DataFrame([{"avail opening_balance": None}])
        assert _funds_null_cash_accounts(df) == []

    def test_total_row_never_flagged(self):
        df = pd.DataFrame([
            {"account": "TOTAL", "avail opening_balance": None},
        ])
        assert _funds_null_cash_accounts(df) == []

    def test_no_cash_column_at_all_returns_empty_list(self):
        """No cash-ish column present is a shape mismatch, not a
        per-account gap — _funds_from_df already treats the whole frame
        as lit(0.0); not reported here."""
        df = pd.DataFrame([{"account": "ZG0790", "util debits": 500.0}])
        assert _funds_null_cash_accounts(df) == []


# ---------------------------------------------------------------------------
# _fetch_funds_from_broker — wiring into `errors`
# ---------------------------------------------------------------------------

class TestFetchFundsFromBrokerUnderstatedTag:
    @pytest.mark.asyncio
    async def test_null_cash_account_tagged_understated(self):
        df = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0},
            {"account": "GR87DF", "avail opening_balance": None},
        ])
        errors: list[str] = []
        with patch("backend.brokers.broker_apis.fetch_margins",
                    return_value=[df]):
            total = await _fetch_funds_from_broker(
                accounts_in=[], errors=errors,
                by_account=None, expected_accounts=None,
            )
        assert total == pytest.approx(10000.0)
        tagged = [e for e in errors if e.startswith(_UNDERSTATED_TAG)]
        assert any("GR87DF" in e and "cash" in e for e in tagged), (
            f"expected an UNDERSTATED cash entry for GR87DF, got {errors}"
        )

    @pytest.mark.asyncio
    async def test_groww_structural_null_premium_not_tagged_understated(self):
        """Groww's option_premium is permanently None by design (no
        confirmed source field) — a null there must NEVER be tagged
        UNDERSTATED, or every NAV snapshot for a Groww-holding firm would
        be permanently stuck deferring/forcing."""
        df = pd.DataFrame([
            {"account": "GR87DF", "avail opening_balance": 5000.0,
             "util option_premium": None},
        ])
        errors: list[str] = []
        with patch("backend.brokers.broker_apis.fetch_margins",
                    return_value=[df]):
            total = await _fetch_funds_from_broker(
                accounts_in=[], errors=errors,
                by_account=None, expected_accounts=None,
            )
        assert total == pytest.approx(5000.0)
        tagged = [e for e in errors if e.startswith(_UNDERSTATED_TAG)]
        assert not any("GR87DF" in e for e in tagged), (
            f"premium-only null must not be tagged understated, got {errors}"
        )

    @pytest.mark.asyncio
    async def test_all_cash_present_no_understated_tag(self):
        df = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0},
        ])
        errors: list[str] = []
        with patch("backend.brokers.broker_apis.fetch_margins",
                    return_value=[df]):
            await _fetch_funds_from_broker(
                accounts_in=[], errors=errors,
                by_account=None, expected_accounts=None,
            )
        assert not any(e.startswith(_UNDERSTATED_TAG) for e in errors)


# ---------------------------------------------------------------------------
# _fetch_funds_from_cache — parallel check on FundsRow.cash
# ---------------------------------------------------------------------------

class TestFetchFundsFromCacheUnderstatedTag:
    def test_none_cash_row_tagged_understated(self):
        rows = [
            SimpleNamespace(account="ZG0790", cash=10000.0, option_premium=0.0),
            SimpleNamespace(account="GR87DF", cash=None, option_premium=0.0),
        ]
        cached_funds = SimpleNamespace(rows=rows, stale_accounts=None)
        errors: list[str] = []
        total = _fetch_funds_from_cache(
            cached_funds, accounts_in=[], errors=errors,
            by_account=None, expected_accounts=None,
        )
        assert total == pytest.approx(10000.0)
        tagged = [e for e in errors if e.startswith(_UNDERSTATED_TAG)]
        assert any("GR87DF" in e and "cash" in e for e in tagged)

    def test_real_zero_cash_row_not_tagged(self):
        rows = [
            SimpleNamespace(account="ZG0790", cash=0.0, option_premium=0.0),
        ]
        cached_funds = SimpleNamespace(rows=rows, stale_accounts=None)
        errors: list[str] = []
        _fetch_funds_from_cache(
            cached_funds, accounts_in=[], errors=errors,
            by_account=None, expected_accounts=None,
        )
        assert not any(e.startswith(_UNDERSTATED_TAG) for e in errors)

    def test_none_premium_alone_not_tagged(self):
        """Mirrors the broker-path scope decision — a None option_premium
        on a cached row must not be tagged (Groww structural absence)."""
        rows = [
            SimpleNamespace(account="GR87DF", cash=5000.0, option_premium=None),
        ]
        cached_funds = SimpleNamespace(rows=rows, stale_accounts=None)
        errors: list[str] = []
        _fetch_funds_from_cache(
            cached_funds, accounts_in=[], errors=errors,
            by_account=None, expected_accounts=None,
        )
        assert not any(e.startswith(_UNDERSTATED_TAG) for e in errors)
