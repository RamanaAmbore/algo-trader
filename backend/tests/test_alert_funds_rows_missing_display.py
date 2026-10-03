"""
2026-10 audit fix: the alert funds table (`_build_funds_rows`,
backend/shared/helpers/alert_utils.py) used `float(x or 0)` at the
call site, which collapses a genuinely missing field into a
confirmed-looking "₹0" display — or, when a NaN survives the `or 0`
coercion uncaught (`float('nan') or 0` evaluates to `float('nan')`,
which is truthy), a raw "nan" string reaching the operator's Telegram
message / email table.

Fix: `_fmt_inr_or_missing` renders a clear "—" placeholder for
None/NaN, matching this codebase's missing-vs-zero convention, while
a broker-confirmed real zero still renders as "₹0".
"""
from __future__ import annotations

import math

import pandas as pd

from backend.shared.helpers.alert_utils import _build_funds_rows, _fmt_inr_or_missing


class TestFmtInrOrMissing:
    def test_none_renders_dash(self):
        assert _fmt_inr_or_missing(None) == "—"

    def test_nan_renders_dash(self):
        assert _fmt_inr_or_missing(float("nan")) == "—"

    def test_real_zero_renders_inr_zero(self):
        assert _fmt_inr_or_missing(0.0) == "₹0"

    def test_positive_value_formats_normally(self):
        assert _fmt_inr_or_missing(150_000) == "₹1.50L"

    def test_non_numeric_renders_dash(self):
        assert _fmt_inr_or_missing("not-a-number") == "—"


class TestBuildFundsRowsMissingFields:
    def test_missing_field_renders_dash_not_zero(self):
        df = pd.DataFrame([{
            "account": "GR87DF",
            "avail opening_balance": None,
            "net": 75000.0,
            "util debits": 25000.0,
            "avail collateral": None,
        }])
        rows = _build_funds_rows(df)
        assert len(rows) == 1
        account, cash, avail_net, used, collat = rows[0]
        assert account == "GR87DF"
        assert cash == "—", f"missing cash must render '—', got {cash!r}"
        assert collat == "—", f"missing collateral must render '—', got {collat!r}"
        assert avail_net == "₹75K"
        assert used == "₹25K"

    def test_nan_field_renders_dash_not_nan_string(self):
        df = pd.DataFrame([{
            "account": "GR87DF",
            "avail opening_balance": math.nan,
            "net": 75000.0,
            "util debits": 25000.0,
            "avail collateral": 10000.0,
        }])
        rows = _build_funds_rows(df)
        account, cash, avail_net, used, collat = rows[0]
        assert cash == "—", f"NaN cash must render '—', not the literal string, got {cash!r}"
        assert "nan" not in cash.lower()

    def test_confirmed_zero_still_renders_inr_zero(self):
        """A broker-confirmed real zero must NOT be rendered as '—' —
        only a genuinely missing field is."""
        df = pd.DataFrame([{
            "account": "ZG0790",
            "avail opening_balance": 0.0,
            "net": 0.0,
            "util debits": 0.0,
            "avail collateral": 0.0,
        }])
        rows = _build_funds_rows(df)
        account, cash, avail_net, used, collat = rows[0]
        assert cash == "₹0"
        assert avail_net == "₹0"
        assert used == "₹0"
        assert collat == "₹0"

    def test_empty_df_returns_empty_list(self):
        assert _build_funds_rows(pd.DataFrame()) == []
        assert _build_funds_rows(None) == []
