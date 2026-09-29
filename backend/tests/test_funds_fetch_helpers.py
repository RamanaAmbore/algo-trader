"""
Unit tests for the funds.py _fetch helper decomposition.

Covers the seven pure helpers extracted from _fetch during the
cyclomatic-complexity refactor (Jul 2026):
  _is_broker_outage
  _stale_since_map
  _rename_broker_cols
  _append_total_row
  _add_derived_columns
  _stale_flag_map
  _hydrate_row
"""

import pandas as pd
import polars as pl
import pytest

from backend.api.routes.funds import (
    _add_derived_columns,
    _append_total_row,
    _hydrate_row,
    _is_broker_outage,
    _rename_broker_cols,
    _stale_flag_map,
    _stale_since_map,
)
from backend.brokers import broker_apis


# ---------------------------------------------------------------------------
# _is_broker_outage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("msg", [
    "502 Bad Gateway", "503 Service Unavailable", "504 Gateway Timeout",
    "Kite responded: Bad Gateway", "kite gateway timeout retry",
])
def test_is_broker_outage_true(msg):
    assert _is_broker_outage(Exception(msg)) is True


@pytest.mark.parametrize("msg", [
    "connection refused", "generic error", "no data",
])
def test_is_broker_outage_false(msg):
    assert _is_broker_outage(Exception(msg)) is False


# ---------------------------------------------------------------------------
# _stale_since_map
# ---------------------------------------------------------------------------

def _mkdf(rows=None, **attrs):
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    for k, v in attrs.items():
        df.attrs[k] = v
    return df


def test_stale_since_map_empty_list():
    assert _stale_since_map([]) == {}


def test_stale_since_map_no_attr_ignored():
    df = _mkdf({"account": ["ZG0790"], "cash": [1000.0]})
    assert _stale_since_map([df]) == {}


def test_stale_since_map_empty_df_ignored():
    df = _mkdf(stale_since=1783137300)
    assert _stale_since_map([df]) == {}


def test_stale_since_map_missing_account_col_ignored():
    df = _mkdf({"cash": [100.0]}, stale_since=1783137300)
    assert _stale_since_map([df]) == {}


def test_stale_since_map_formats_hh_mm_ist():
    df = _mkdf({"account": ["ZG0790"], "cash": [1000.0]}, stale_since=1783137300)
    val = _stale_since_map([df])["ZG0790"]
    assert val.endswith("IST")
    hh, rest = val.split(":", 1)
    mm, _ = rest.split(" ", 1)
    assert 0 <= int(hh) <= 23
    assert 0 <= int(mm) <= 59


def test_stale_since_map_bad_timestamp_swallowed():
    df = _mkdf({"account": ["ZG0790"]}, stale_since="not-a-number")
    assert _stale_since_map([df]) == {}


# ---------------------------------------------------------------------------
# _rename_broker_cols
# ---------------------------------------------------------------------------

def test_rename_broker_cols_all_present():
    df = pl.DataFrame({
        "avail opening_balance": [100.0],
        "avail cash":            [50.0],
        "net":                   [200.0],
        "util debits":           [30.0],
        "util option_premium":   [5.0],
        "avail collateral":      [10.0],
        "account":               ["ZG0790"],
    })
    out = _rename_broker_cols(df)
    for col in ["cash", "live_cash", "avail_margin", "used_margin",
                "option_premium", "collateral"]:
        assert col in out.columns


def test_rename_broker_cols_partial():
    df = pl.DataFrame({"avail opening_balance": [100.0], "account": ["A"]})
    out = _rename_broker_cols(df)
    assert "cash" in out.columns
    assert "avail opening_balance" not in out.columns


# ---------------------------------------------------------------------------
# _append_total_row
# ---------------------------------------------------------------------------

def test_append_total_row_sums_rows():
    df = pl.DataFrame({
        "account": ["A", "B"],
        "cash":    [100.0, 200.0],
        "avail_margin": [50.0, 60.0],
    })
    out = _append_total_row(df, ["cash", "avail_margin"])
    total = out.filter(pl.col("account") == "TOTAL").row(0, named=True)
    assert total["cash"] == pytest.approx(300.0)
    assert total["avail_margin"] == pytest.approx(110.0)


def test_append_total_row_no_null_when_all_sources_have_values():
    """No null anywhere when every source value is real (renamed from
    the pre-fix '..._fills_nulls_with_zero' — the fix removed the
    trailing `.fill_nan(0).fill_null(0)`, so this now documents the
    happy path has no nulls to begin with, not that nulls get filled)."""
    df = pl.DataFrame({"account": ["A"], "cash": [100.0]})
    out = _append_total_row(df, ["cash"])
    assert out.null_count().to_dicts()[0]["cash"] == 0


# ---------------------------------------------------------------------------
# _append_total_row — missing-vs-zero convention (2026-09 funds-route fix)
#
# `present` is always a funds-meaning column (subset of _SUM_COLS), so
# _append_total_row must NOT re-zero a genuinely-missing (null) value —
# unlike the pre-fix `.fill_nan(0).fill_null(0)` tail, which silently
# collapsed a broker-confirmed-absent field into a fake real zero.
# ---------------------------------------------------------------------------

def test_append_total_row_preserves_null_on_per_account_row():
    """A genuinely-missing funds field (null, not a real 0) on a
    per-account row must survive _append_total_row unchanged — it must
    NOT be silently coerced to 0.0."""
    df = pl.DataFrame({
        "account": ["A"],
        "avail_margin": pl.Series([None], dtype=pl.Float64),
    })
    out = _append_total_row(df, ["avail_margin"])
    a = out.filter(pl.col("account") == "A").row(0, named=True)
    assert a["avail_margin"] is None


def test_append_total_row_sum_skips_null_not_treated_as_zero_contribution():
    """TOTAL row's sum must exclude a null account's contribution
    (Polars' default `.sum()` skips nulls, matching pandas'
    `skipna=True`) rather than erroring or corrupting the mixed total —
    verified empirically: sum([null, 200.0]) == 200.0, not null."""
    df = pl.DataFrame({
        "account": ["A", "B"],
        "avail_margin": [None, 200.0],
    })
    out = _append_total_row(df, ["avail_margin"])
    total = out.filter(pl.col("account") == "TOTAL").row(0, named=True)
    assert total["avail_margin"] == pytest.approx(200.0)
    # The null account's own row is untouched, still null.
    a = out.filter(pl.col("account") == "A").row(0, named=True)
    assert a["avail_margin"] is None


def test_append_total_row_all_null_source_stays_null_not_fabricated_zero():
    """When EVERY account is null for a column (e.g. a single-account
    deployment on a broker that never surfaces the field), Polars'
    bare `.sum()` would return 0.0 — a fabricated "confirmed zero" that
    looks broker-real. TOTAL must stay null in that case, matching the
    per-account rows' own null state."""
    df = pl.DataFrame({
        "account": ["A", "B"],
        "avail_margin": pl.Series([None, None], dtype=pl.Float64),
    })
    # Sanity: bare .sum() on an all-null column IS 0.0, not null —
    # this is exactly the trap _append_total_row must guard against.
    assert df.select(["avail_margin"]).sum().item() == 0.0

    out = _append_total_row(df, ["avail_margin"])
    total = out.filter(pl.col("account") == "TOTAL").row(0, named=True)
    assert total["avail_margin"] is None


# ---------------------------------------------------------------------------
# _add_derived_columns
# ---------------------------------------------------------------------------

def test_add_derived_columns_available_funds_equals_avail_margin():
    df = pl.DataFrame({
        "account": ["A", "TOTAL"],
        "cash":    [100.0, 100.0],
        "option_premium": [10.0, 10.0],
        "avail_margin":   [50.0, 50.0],
    })
    out = _add_derived_columns(df)
    a = out.filter(pl.col("account") == "A").row(0, named=True)
    assert a["available_funds"] == pytest.approx(50.0)
    # available_cash = cash − option_premium = 100 − 10 = 90
    assert a["available_cash"] == pytest.approx(90.0)


def test_add_derived_columns_missing_cash_defaults_to_zero():
    df = pl.DataFrame({"account": ["A"], "option_premium": [10.0], "avail_margin": [50.0]})
    out = _add_derived_columns(df)
    a = out.row(0, named=True)
    # available_cash = 0 − 10 = −10 (default cash → 0.0)
    assert a["available_cash"] == pytest.approx(-10.0)


def test_add_derived_columns_missing_avail_margin_defaults_to_zero():
    df = pl.DataFrame({"account": ["A"], "cash": [100.0]})
    out = _add_derived_columns(df)
    a = out.row(0, named=True)
    assert a["available_funds"] == pytest.approx(0.0)


def test_add_derived_columns_null_avail_margin_coalesced_to_zero():
    """A genuinely-missing (null, column present) avail_margin must
    still produce a non-null available_funds — FundsRow.available_funds
    is a plain `float` (no `| None`), so this derived convenience
    column degrades to 0 rather than propagating a null into a
    non-nullable field."""
    df = pl.DataFrame({
        "account": ["A"],
        "avail_margin": pl.Series([None], dtype=pl.Float64),
    })
    out = _add_derived_columns(df)
    a = out.row(0, named=True)
    assert a["available_funds"] == pytest.approx(0.0)
    assert a["available_funds"] is not None


def test_add_derived_columns_null_cash_or_premium_coalesced_to_zero():
    """A null cash or option_premium must not propagate a null into
    available_cash (cash − option_premium)."""
    df = pl.DataFrame({
        "account": ["A"],
        "cash": pl.Series([None], dtype=pl.Float64),
        "option_premium": [10.0],
    })
    out = _add_derived_columns(df)
    a = out.row(0, named=True)
    # available_cash = 0 (coalesced) − 10 = −10
    assert a["available_cash"] == pytest.approx(-10.0)


# ---------------------------------------------------------------------------
# _stale_flag_map
# ---------------------------------------------------------------------------

def test_stale_flag_map_missing_col_empty():
    df = pl.DataFrame({"account": ["A"], "cash": [100.0]})
    assert _stale_flag_map(df) == {}


def test_stale_flag_map_only_true_rows_included():
    df = pl.DataFrame({
        "account":       ["A", "B", "C"],
        "cash":          [100.0, 200.0, 300.0],
        "account_stale": [True, False, True],
    })
    m = _stale_flag_map(df)
    assert set(m) == {"A", "C"}
    assert all(m.values()) is True


# ---------------------------------------------------------------------------
# _hydrate_row
# ---------------------------------------------------------------------------

def test_hydrate_row_total_row_untouched():
    r = {"account": "TOTAL", "cash": 100.0}
    out = _hydrate_row(dict(r), {"A": True}, {"A": "09:00 IST"})
    assert "account_stale" not in out
    assert "account_stale_since" not in out


def test_hydrate_row_stamps_stale_flag():
    r = {"account": "A", "cash": 100.0}
    out = _hydrate_row(dict(r), {"A": True}, {})
    assert out["account_stale"] is True


def test_hydrate_row_stamps_since_when_stale_and_present():
    r = {"account": "A", "cash": 100.0}
    out = _hydrate_row(dict(r), {"A": True}, {"A": "09:15 IST"})
    assert out["account_stale_since"] == "09:15 IST"


def test_hydrate_row_no_since_when_not_stale():
    r = {"account": "A", "cash": 100.0}
    out = _hydrate_row(dict(r), {}, {"A": "09:15 IST"})
    assert out["account_stale"] is False
    assert "account_stale_since" not in out


def test_hydrate_row_no_since_when_stale_but_absent_from_map():
    r = {"account": "A", "cash": 100.0}
    out = _hydrate_row(dict(r), {"A": True}, {"OTHER": "09:15 IST"})
    assert out["account_stale"] is True
    assert "account_stale_since" not in out


# ---------------------------------------------------------------------------
# stale_accounts masking/scoping — _funds_scope_trader, _funds_mask_accounts
#
# Same P0-adjacent gap already found and fixed in
# positions_helpers._apply_account_mask / _apply_trader_scope
# (test_positions_helpers.py) and holdings._hold_mask_account_in_resp /
# _scope_and_mask_holdings / _filter_holdings_by_account
# (test_holdings_fetch_helpers.py): the funds.py trader filter and
# account-ID mask previously touched only `rows`, leaving
# FundsResponse.stale_accounts unmasked/unscoped.
# ---------------------------------------------------------------------------

def _make_funds_row(account: str):
    from backend.api.schemas import FundsRow
    return FundsRow(
        account=account, cash=1000.0, avail_margin=500.0,
        used_margin=200.0, collateral=0.0,
    )


def _make_funds_response(rows=None, stale_accounts=None):
    from backend.api.schemas import FundsResponse
    return FundsResponse(
        rows=rows or [],
        refreshed_at="2026-09-26T00:00:00Z",
        stale_accounts=list(stale_accounts or []),
    )


class TestFundsScopeTraderStaleAccounts:
    """_funds_scope_trader must narrow stale_accounts to the trader's
    allowed account set, same as rows."""

    def test_stale_accounts_scoped_to_allowed_set(self):
        from backend.api.routes.funds import _funds_scope_trader

        resp = _make_funds_response(
            rows=[_make_funds_row("ZG0790"), _make_funds_row("DH6847"),
                  _make_funds_row("TOTAL")],
            stale_accounts=["ZG0790", "DH6847"],
        )
        out = _funds_scope_trader(resp, {"ZG0790"})

        assert out.stale_accounts == ["ZG0790"]
        assert "DH6847" not in out.stale_accounts
        # Regression guard — TOTAL row preserved in rows, others filtered.
        assert {r.account for r in out.rows} == {"ZG0790", "TOTAL"}

    def test_stale_accounts_empty_when_none_allowed(self):
        from backend.api.routes.funds import _funds_scope_trader

        resp = _make_funds_response(stale_accounts=["ZG0790"])
        out = _funds_scope_trader(resp, set())
        assert out.stale_accounts == []

    def test_stale_accounts_empty_stays_empty(self):
        from backend.api.routes.funds import _funds_scope_trader

        resp = _make_funds_response(stale_accounts=[])
        out = _funds_scope_trader(resp, {"ZG0790"})
        assert out.stale_accounts == []


class TestFundsMaskAccountsStaleAccounts:
    """_funds_mask_accounts must mask stale_accounts the same way it
    masks rows[].account."""

    def test_stale_accounts_are_masked(self):
        from backend.api.routes.funds import _funds_mask_accounts
        from backend.shared.helpers.utils import mask_account

        resp = _make_funds_response(stale_accounts=["ZG0790", "DH6847"])
        out = _funds_mask_accounts(resp)

        assert set(out.stale_accounts) == {
            mask_account("ZG0790"), mask_account("DH6847"),
        }
        assert "ZG0790" not in out.stale_accounts
        assert "DH6847" not in out.stale_accounts

    def test_stale_accounts_empty_stays_empty(self):
        from backend.api.routes.funds import _funds_mask_accounts

        resp = _make_funds_response(stale_accounts=[])
        out = _funds_mask_accounts(resp)
        assert out.stale_accounts == []

    def test_same_prefix_collision_is_documented_conservative_behavior(
        self, monkeypatch,
    ):
        """mask_account's UNREGISTERED fallback collides same-prefix
        accounts (DH6847/DH3747 both -> DH####) — masking stale_accounts
        this way is intentionally conservative, matching the documented
        behavior in positions_helpers._apply_account_mask."""
        import backend.shared.helpers.utils as _utils_mod
        from backend.api.routes.funds import _funds_mask_accounts
        from backend.shared.helpers.utils import mask_account

        monkeypatch.setattr(_utils_mod, "_REGISTRY", {})
        assert mask_account("DH6847") == mask_account("DH3747") == "DH####"

        resp = _make_funds_response(stale_accounts=["DH6847", "DH3747"])
        out = _funds_mask_accounts(resp)
        assert out.stale_accounts == ["DH####"]

    def test_rows_still_masked_alongside_stale_accounts(self):
        from backend.api.routes.funds import _funds_mask_accounts
        from backend.shared.helpers.utils import mask_account

        row = _make_funds_row("ZG0790")
        total_row = _make_funds_row("TOTAL")
        resp = _make_funds_response(
            rows=[row, total_row], stale_accounts=["ZG0790"],
        )
        out = _funds_mask_accounts(resp)

        assert out.rows[0].account == mask_account("ZG0790")
        # TOTAL row is never masked (matches existing rows-masking behavior).
        assert out.rows[1].account == "TOTAL"
        assert out.stale_accounts == [mask_account("ZG0790")]


# ---------------------------------------------------------------------------
# _fetch() end-to-end — missing-vs-zero convention (2026-09 funds-route fix)
#
# Prior to this fix, `_fetch()`'s blanket `raw[numeric_cols].fillna(0)`
# destroyed the missing-vs-zero distinction the Dhan/Groww adapter layer
# (backend/tests/broker/test_funds_missing_vs_zero.py) already correctly
# implements, before the value ever reached the API response. These
# tests prove the fix holds at the route layer, in BOTH directions:
# a genuinely-absent field must reach FundsRow as None, and a real
# broker-confirmed 0 must still reach it as 0.0 (not collapsed to None).
# ---------------------------------------------------------------------------

from unittest.mock import patch  # noqa: E402


def _make_raw(*rows) -> pd.DataFrame:
    """Build a raw margins DataFrame from dicts of broker column names
    (mirrors test_funds_available_cash.py's helper)."""
    return pd.DataFrame(list(rows))


def _call_fetch(raw_df: pd.DataFrame):
    from backend.api.routes.funds import _fetch
    with patch('backend.api.routes.funds.broker_apis.fetch_margins', return_value=[raw_df]):
        return _fetch()


class TestFetchMissingVsZero:
    def test_missing_field_survives_as_none_not_zero(self):
        """A genuinely-missing 'net' column value (NaN — what a Dhan/
        Groww adapter produces for an unmapped field via
        _dhan_num_or_none/_gf_or_none) must reach the API response's
        FundsRow.avail_margin as None, not a coerced 0.0."""
        raw = _make_raw(
            {
                'account': 'DH_TEST',
                'avail opening_balance': 100_000.0,
                'net': float('nan'),
                'util debits': 20_000.0,
                'avail collateral': 0.0,
                'util option_premium': 0.0,
            },
        )
        resp = _call_fetch(raw)
        row = next(r for r in resp.rows if r.account == 'DH_TEST')
        assert row.avail_margin is None

    def test_real_zero_still_survives_as_zero_not_none(self):
        """Regression guard — the missing-vs-zero distinction must work
        in BOTH directions: a real broker-confirmed 0 on a healthy
        account must still come through as 0.0, never None."""
        raw = _make_raw(
            {
                'account': 'ZG0790',
                'avail opening_balance': 100_000.0,
                'net': 0.0,
                'util debits': 0.0,
                'avail collateral': 0.0,
                'util option_premium': 0.0,
            },
        )
        resp = _call_fetch(raw)
        row = next(r for r in resp.rows if r.account == 'ZG0790')
        assert row.avail_margin == 0.0
        assert row.avail_margin is not None
        assert row.used_margin == 0.0
        assert row.collateral == 0.0

    def test_total_row_sums_only_known_value_when_one_account_is_null(self):
        """TOTAL row across a null account + a real-value account sums
        only the known value — Polars' `.sum()` skips nulls by default
        (verified empirically: sum([null, X]) == X, not null/error)."""
        raw = _make_raw(
            {
                'account': 'DH_TEST',
                'avail opening_balance': 100_000.0,
                'net': float('nan'),
                'util debits': 20_000.0,
                'avail collateral': 0.0,
                'util option_premium': 0.0,
            },
            {
                'account': 'ZG0790',
                'avail opening_balance': 200_000.0,
                'net': 180_000.0,
                'util debits': 20_000.0,
                'avail collateral': 0.0,
                'util option_premium': 0.0,
            },
        )
        resp = _call_fetch(raw)
        total = next(r for r in resp.rows if r.account == 'TOTAL')
        # Only ZG0790's 180_000.0 contributes — DH_TEST's null is
        # excluded, not treated as a 0 contribution.
        assert total.avail_margin == pytest.approx(180_000.0)
        # Per-account rows are unaffected by the aggregate.
        dh_row = next(r for r in resp.rows if r.account == 'DH_TEST')
        assert dh_row.avail_margin is None
        zg_row = next(r for r in resp.rows if r.account == 'ZG0790')
        assert zg_row.avail_margin == pytest.approx(180_000.0)

    def test_available_funds_derived_field_never_none(self):
        """Even when avail_margin (source) is null, available_funds
        (derived convenience field, plain float on FundsRow) must never
        be None — it degrades to 0 instead of propagating the null."""
        raw = _make_raw(
            {
                'account': 'DH_TEST',
                'avail opening_balance': 100_000.0,
                'net': float('nan'),
                'util debits': 20_000.0,
                'avail collateral': 0.0,
                'util option_premium': 0.0,
            },
        )
        resp = _call_fetch(raw)
        row = next(r for r in resp.rows if r.account == 'DH_TEST')
        assert row.available_funds == 0.0
        assert row.available_funds is not None


# ---------------------------------------------------------------------------
# _fetch() end-to-end — real Python-`None` object-dtype shape
#
# A hand-built `float('nan')` DataFrame (used above) is faithful to a
# MULTI-account concat where at least one account has a real value —
# pandas upcasts the None-bearing column to float64/NaN at concat time.
# But `_fetch_margins_local` (broker_apis.py) builds each per-account
# frame from `pd.DataFrame([margins_data])`, a single-row dict — when
# EVERY value in a column across ALL concatenated accounts is Python
# `None` (e.g. a single-account deployment on a broker that never
# surfaces a field), pandas keeps that column as `object` dtype
# (`select_dtypes(include='number')` never sees it), and `pl.from_pandas`
# in turn infers Utf8 (String), not Float64 — a genuinely different
# failure mode than the NaN-in-a-numeric-column case above. These tests
# reproduce that exact real shape via the actual adapter → broker_apis
# path (not hand-rolled), matching
# backend/tests/broker/test_funds_missing_vs_zero.py's
# TestMissingFundsFieldSurvivesFullFetchPath pattern.
# ---------------------------------------------------------------------------

class TestFetchRealObjectDtypeNoneShape:
    def _dhan_frame_missing_net(self):
        """Build the exact frame shape `_fetch_margins_local` produces
        for a Dhan account whose fund_limits response has neither
        `availabelBalance` nor `availableBalance` — `net` genuinely
        None for every row of the (single-account) concatenated frame."""
        from backend.brokers.adapters.dhan import _normalise_margins as _dhan_normalise_margins

        resp = {"data": {"sodLimit": 100_000.0, "utilizedAmount": 20_000.0}}
        margins_data = _dhan_normalise_margins(resp, segment=None)
        assert margins_data["net"] is None  # sanity on the normaliser

        class _StubBroker:
            def margins(self, segment="equity"):
                return margins_data

        df = broker_apis._fetch_margins_local.__wrapped__(
            connections=None, account="DH_TEST", kite=None, broker=_StubBroker(),
        )
        # Sanity on the real production shape: object dtype, not numeric.
        assert df["net"].dtype == object
        return df

    def test_single_account_all_none_column_does_not_become_string(self):
        """Regression guard for the dtype-instability trap: without an
        explicit Float64 cast, pl.from_pandas infers Utf8 for an
        all-None object column, and downstream arithmetic
        (`_add_derived_columns`' `.fill_null(0)`) would silently
        produce a STRING '0' instead of the float 0.0 for
        available_funds. Must never reach the API response as a
        string."""
        df = self._dhan_frame_missing_net()

        with patch(
            'backend.api.routes.funds.broker_apis.fetch_margins', return_value=[df],
        ):
            from backend.api.routes.funds import _fetch
            resp = _fetch()

        row = next(r for r in resp.rows if r.account == 'DH_TEST')
        assert row.avail_margin is None
        assert isinstance(row.available_funds, float)
        assert row.available_funds == 0.0
        assert row.cash == pytest.approx(100_000.0)
        assert row.used_margin == pytest.approx(20_000.0)

    def test_single_account_all_none_total_row_stays_null_not_fabricated_zero(self):
        """The TOTAL row for a single-account, all-None column must
        stay None too — not the fabricated 0.0 that a bare Polars
        `.sum()` over an all-null column would otherwise produce."""
        df = self._dhan_frame_missing_net()

        with patch(
            'backend.api.routes.funds.broker_apis.fetch_margins', return_value=[df],
        ):
            from backend.api.routes.funds import _fetch
            resp = _fetch()

        total = next(r for r in resp.rows if r.account == 'TOTAL')
        assert total.avail_margin is None
