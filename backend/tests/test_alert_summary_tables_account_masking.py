"""2026-10 notification-masking audit, Wave 2 clusters 1+2 —
backend/shared/helpers/alert_utils.py's market-open/close summary table
builders (`_build_funds_rows`, `_summary_holdings_rows`,
`_summary_positions_rows`) and the partial-outage warning line
(`_partial_outage_warning_lines`) built their Telegram/email body text
with the raw, unmasked account code. These feed `send_summary()`'s
Telegram and email output directly — a real notification-facing surface,
not a log line Wave 1's centralized filter would already catch.

Each test computes the expected masked form at runtime via `mask_account`
rather than hardcoding a masked literal, since the module-global mask
registry's exact output for a given raw code depends on whether
`register_accounts()` has already run earlier in the test suite.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.shared.helpers.alert_utils import (
    _build_funds_rows,
    _summary_holdings_rows,
    _summary_positions_rows,
    _partial_outage_warning_lines,
)
from backend.shared.helpers.utils import mask_account

RAW_ACCOUNT = "ZJ6294"
RAW_ACCOUNT_2 = "DH3747"


class TestBuildFundsRowsMasksAccount:
    def test_account_masked_in_funds_row(self):
        df = pd.DataFrame([{
            "account": RAW_ACCOUNT,
            "avail opening_balance": 75000.0,
            "net": 75000.0,
            "util debits": 25000.0,
            "avail collateral": 10000.0,
        }])
        rows = _build_funds_rows(df)
        assert len(rows) == 1
        account = rows[0][0]
        assert account == mask_account(RAW_ACCOUNT)
        assert RAW_ACCOUNT not in account

    def test_total_row_passes_through_unmasked(self):
        """TOTAL is a virtual aggregate row, not a real account — must
        pass through `mask_account()` unchanged (see its own docstring)."""
        df = pd.DataFrame([{
            "account": "TOTAL",
            "avail opening_balance": 100000.0,
            "net": 100000.0,
            "util debits": 0.0,
            "avail collateral": 0.0,
        }])
        rows = _build_funds_rows(df)
        assert rows[0][0] == "TOTAL"


class TestSummaryHoldingsRowsMasksAccount:
    def test_account_masked_in_holdings_row(self):
        df = pd.DataFrame([{
            "account": RAW_ACCOUNT,
            "cur_val": 100000.0, "pnl": 5000.0, "pnl_percentage": 5.0,
            "day_change_val": 1000.0, "day_change_percentage": 1.0,
        }])
        rows = _summary_holdings_rows(df)
        assert len(rows) == 1
        account = rows[0][0]
        assert account == mask_account(RAW_ACCOUNT)
        assert RAW_ACCOUNT not in account


class TestSummaryPositionsRowsMasksAccount:
    def test_account_masked_in_positions_row(self):
        df = pd.DataFrame([{"account": RAW_ACCOUNT_2, "pnl": -2500.0}])
        rows = _summary_positions_rows(df)
        assert len(rows) == 1
        account = rows[0][0]
        assert account == mask_account(RAW_ACCOUNT_2)
        assert RAW_ACCOUNT_2 not in account


class TestPartialOutageWarningLinesMasksAccount:
    def test_masks_every_account_in_the_joined_line(self):
        df = pd.DataFrame([{"account": "TOTAL", "pnl": 0.0}])
        df.attrs["partial_outage"] = [RAW_ACCOUNT, RAW_ACCOUNT_2]
        lines = _partial_outage_warning_lines(df)
        assert lines is not None
        assert len(lines) == 1
        assert RAW_ACCOUNT not in lines[0]
        assert RAW_ACCOUNT_2 not in lines[0]
        assert mask_account(RAW_ACCOUNT) in lines[0]
        assert mask_account(RAW_ACCOUNT_2) in lines[0]

    def test_no_attrs_returns_none(self):
        df = pd.DataFrame([{"account": "TOTAL", "pnl": 0.0}])
        assert _partial_outage_warning_lines(df) is None
