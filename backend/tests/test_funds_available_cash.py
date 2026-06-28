"""Tests for available_funds and available_cash columns in the funds route.

Five quality dimensions:
  1. SSOT     — available_cash = cash − option_premium; available_funds = avail_margin,
                both derived in one place (_fetch) and exposed in FundsRow.
  2. Perf     — _fetch adds only two derived Polars .with_columns() expressions, no extra I/O.
  3. Stale    — FundsRow fields are in schemas.py; derived columns computed in routes/funds.py.
  4. Reusable — FundsRow and FundsResponse imported from backend.api.schemas (single definition).
  5. Correct  — zero option_premium → available_cash = cash; non-zero → correctly subtracted;
                TOTAL row aggregates correctly; short options (negative premium) don't corrupt result.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pandas as pd
import polars as pl
import pytest

from backend.api.schemas import FundsRow, FundsResponse


# ---------------------------------------------------------------------------
# Helper — build a minimal margins DataFrame that looks like broker output
# ---------------------------------------------------------------------------

def _make_raw(*rows) -> pd.DataFrame:
    """Build a raw margins DataFrame from dicts of broker column names."""
    return pd.DataFrame(list(rows))


def _call_fetch(raw_df: pd.DataFrame) -> FundsResponse:
    """Call the funds._fetch() helper with mocked broker margins."""
    from backend.api.routes.funds import _fetch
    with patch('backend.api.routes.funds.broker_apis.fetch_margins', return_value=[raw_df]):
        return _fetch()


# ---------------------------------------------------------------------------
# 1. SSOT — derived fields live in a single place
# ---------------------------------------------------------------------------

class TestSSOT:
    def test_available_cash_field_on_fundsrow(self):
        """FundsRow exposes available_cash and available_funds as declared fields."""
        row = FundsRow(
            account='ZG0790',
            cash=500_000.0,
            avail_margin=480_000.0,
            used_margin=20_000.0,
            collateral=0.0,
            option_premium=30_000.0,
            available_funds=480_000.0,
            available_cash=470_000.0,
        )
        assert row.available_cash == 470_000.0
        assert row.available_funds == 480_000.0

    def test_available_cash_defaults_to_zero(self):
        """FundsRow defaults both derived fields to 0 for backward compat."""
        row = FundsRow(
            account='X', cash=0.0, avail_margin=0.0, used_margin=0.0, collateral=0.0,
        )
        assert row.available_cash == 0.0
        assert row.available_funds == 0.0

    def test_single_definition_in_schemas(self):
        """FundsRow is only defined in schemas.py — no duplicates."""
        import inspect, backend.api.schemas as s
        src = inspect.getsource(s)
        # Count class definitions named FundsRow
        assert src.count('class FundsRow') == 1

    def test_derivation_in_fetch_not_frontend(self):
        """routes/funds.py computes derived columns; no inline JS computation expected."""
        import inspect, backend.api.routes.funds as rf
        src = inspect.getsource(rf)
        assert 'available_cash' in src, "available_cash must be derived in the route"
        assert 'available_funds' in src, "available_funds must be derived in the route"


# ---------------------------------------------------------------------------
# 2. Correctness — arithmetic cases
# ---------------------------------------------------------------------------

class TestCorrectness:
    def test_zero_premium_available_cash_equals_cash(self):
        """When option_premium == 0, available_cash should equal cash."""
        raw = _make_raw({
            'account': 'ZG0790',
            'avail opening_balance': 1_000_000.0,
            'net': 950_000.0,
            'util debits': 50_000.0,
            'avail collateral': 0.0,
            'util option_premium': 0.0,
        })
        resp = _call_fetch(raw)
        data_row = next(r for r in resp.rows if r.account == 'ZG0790')
        assert data_row.available_cash == pytest.approx(data_row.cash, abs=0.01)

    def test_positive_premium_reduces_available_cash(self):
        """available_cash = cash − option_premium when premium > 0."""
        raw = _make_raw({
            'account': 'ZG0790',
            'avail opening_balance': 1_000_000.0,
            'net': 900_000.0,
            'util debits': 100_000.0,
            'avail collateral': 0.0,
            'util option_premium': 75_000.0,
        })
        resp = _call_fetch(raw)
        data_row = next(r for r in resp.rows if r.account == 'ZG0790')
        assert data_row.available_cash == pytest.approx(925_000.0, abs=0.01)
        assert data_row.cash == pytest.approx(1_000_000.0, abs=0.01)
        assert data_row.option_premium == pytest.approx(75_000.0, abs=0.01)

    def test_available_funds_equals_avail_margin(self):
        """available_funds must exactly mirror avail_margin (broker net)."""
        raw = _make_raw({
            'account': 'ZG0790',
            'avail opening_balance': 800_000.0,
            'net': 720_000.0,
            'util debits': 80_000.0,
            'avail collateral': 0.0,
            'util option_premium': 0.0,
        })
        resp = _call_fetch(raw)
        data_row = next(r for r in resp.rows if r.account == 'ZG0790')
        assert data_row.available_funds == pytest.approx(data_row.avail_margin, abs=0.01)

    def test_multi_account_total_row_derived_correct(self):
        """TOTAL row aggregates available_funds and available_cash correctly."""
        raw = _make_raw(
            {'account': 'ZG0790', 'avail opening_balance': 1_000_000.0, 'net': 900_000.0,
             'util debits': 100_000.0, 'avail collateral': 0.0, 'util option_premium': 50_000.0},
            {'account': 'ZJ6294', 'avail opening_balance': 500_000.0,  'net': 480_000.0,
             'util debits': 20_000.0,  'avail collateral': 0.0, 'util option_premium': 20_000.0},
        )
        resp = _call_fetch(raw)
        total = next(r for r in resp.rows if r.account == 'TOTAL')
        # available_cash TOTAL = (1_000_000 - 50_000) + (500_000 - 20_000) = 1_430_000
        assert total.available_cash == pytest.approx(1_430_000.0, abs=0.01)
        # available_funds TOTAL = 900_000 + 480_000 = 1_380_000
        assert total.available_funds == pytest.approx(1_380_000.0, abs=0.01)

    def test_missing_option_premium_column(self):
        """Broker adapter without option_premium column still yields available_cash = cash."""
        raw = _make_raw({
            'account': 'GR87DF',
            'avail opening_balance': 200_000.0,
            'net': 190_000.0,
            'util debits': 10_000.0,
            'avail collateral': 0.0,
            # No 'util option_premium' column — Groww adapter
        })
        resp = _call_fetch(raw)
        data_row = next(r for r in resp.rows if r.account == 'GR87DF')
        # option_premium defaults to 0 when column absent → available_cash = cash
        assert data_row.option_premium == 0.0
        assert data_row.available_cash == pytest.approx(data_row.cash, abs=0.01)

    def test_negative_premium_does_not_corrupt(self):
        """If broker returns negative option_premium (credit received), available_cash > cash.

        This is the short-option case — the broker deducts SPAN/exposure as
        used_margin rather than option_premium. A negative option_premium value
        is unusual but the arithmetic must still be numerically stable."""
        raw = _make_raw({
            'account': 'ZG0790',
            'avail opening_balance': 1_000_000.0,
            'net': 850_000.0,
            'util debits': 150_000.0,
            'avail collateral': 0.0,
            'util option_premium': -10_000.0,   # credit scenario (net premium received)
        })
        resp = _call_fetch(raw)
        data_row = next(r for r in resp.rows if r.account == 'ZG0790')
        # available_cash = 1_000_000 - (-10_000) = 1_010_000 — credit adds back
        assert data_row.available_cash == pytest.approx(1_010_000.0, abs=0.01)


# ---------------------------------------------------------------------------
# 3. Performance — no extra I/O in derived column computation
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_no_extra_broker_calls_for_derived_columns(self):
        """_fetch() calls fetch_margins exactly once regardless of derived columns."""
        raw = _make_raw({
            'account': 'ZG0790',
            'avail opening_balance': 1_000_000.0,
            'net': 900_000.0,
            'util debits': 100_000.0,
            'avail collateral': 0.0,
            'util option_premium': 50_000.0,
        })
        from backend.api.routes.funds import _fetch
        call_count = {'n': 0}

        def counting_fetch():
            call_count['n'] += 1
            return [raw]

        with patch('backend.api.routes.funds.broker_apis.fetch_margins', side_effect=counting_fetch):
            _fetch()

        assert call_count['n'] == 1, "fetch_margins must be called exactly once"


# ---------------------------------------------------------------------------
# 4. Reusable — schema is the canonical import
# ---------------------------------------------------------------------------

class TestReusable:
    def test_fundsrow_imported_from_schemas(self):
        """FundsRow and FundsResponse must come from backend.api.schemas."""
        from backend.api.schemas import FundsRow as FR, FundsResponse as FResp
        assert FR is FundsRow
        assert FResp is FundsResponse

    def test_fundsrow_has_all_expected_fields(self):
        """FundsRow exposes all expected fields including the two new derived ones."""
        expected_fields = {
            'account', 'cash', 'avail_margin', 'used_margin', 'collateral',
            'live_cash', 'option_premium', 'available_funds', 'available_cash',
        }
        actual_fields = set(FundsRow.__struct_fields__)
        assert expected_fields <= actual_fields, (
            f"Missing fields: {expected_fields - actual_fields}"
        )
