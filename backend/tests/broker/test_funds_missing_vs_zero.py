"""
Regression tests for the Dhan/Groww funds-field missing-vs-zero bug
(PLAN.md defect #3, broker-side half).

Background: `_dhan_margins_available` / `_dhan_margins_utilised` /
`_normalise_margins` (dhan.py) and `_groww_margin_available` /
`_groww_margin_utilised` / `_normalise_margins` (groww.py) used to
default every field to `0.0` whenever its source key was absent from
the broker's raw response — collapsing "field genuinely missing/
unmapped" into "broker reported an actual zero balance". Several
accounts permanently surfaced `avail_margin=0.00` this way, firing 61
false `loss-margin-low` alerts in 60 days on nothing but an unmapped
field (grammar.py's `_metric_avail_margin` reads `row['net']`, which
came from exactly this defaulted-to-0.0 path).

Covers five quality dimensions:
  SSOT        — single normaliser per broker owns the funds shape;
                fix lives in the normaliser, not duplicated per caller.
  Correctness — genuinely-missing key → None; genuinely-present-zero
                key → 0.0 (never collapsed together in either direction).
  Reuse       — `_dhan_num_or_none` / `_gf_or_none` are the same helper
                shape as the already-shipped `_gf_native_or_none`
                Groww convention (pnl vs realised+unrealised).
  Data path   — None survives into the dict `grammar.py`/`agent_engine.py`
                read (`net`, `available.opening_balance`, `utilised.debits`,
                `available.collateral`) — the exact fields the alert
                metrics consume.
  UX/alerting — a broker-supported field that is genuinely absent must
                not be indistinguishable, downstream, from a real 0
                that SHOULD still be able to alert.
"""

from __future__ import annotations

from backend.brokers.adapters.dhan import (
    _dhan_num_or_none,
    _dhan_margins_available,
    _dhan_margins_utilised,
    _normalise_margins as _dhan_normalise_margins,
)
from backend.brokers.adapters.groww import (
    _gf_or_none,
    _groww_margin_available,
    _groww_margin_utilised,
    _normalise_margins as _groww_normalise_margins,
)
from backend.brokers import broker_apis


# ---------------------------------------------------------------------------
# Dhan — helper unit tests
# ---------------------------------------------------------------------------

class TestDhanNumOrNone:
    def test_all_candidates_absent_returns_none(self):
        assert _dhan_num_or_none(None, None, None) is None

    def test_empty_string_candidate_treated_as_absent(self):
        assert _dhan_num_or_none("", None) is None

    def test_first_candidate_present_and_zero_is_a_real_zero_not_skipped(self):
        # Pre-fix code used `a or b`, which skips a real 0 on the
        # higher-priority key in favour of a lower-priority one. The
        # fixed helper must NOT do that.
        assert _dhan_num_or_none(0, 999) == 0.0

    def test_falls_through_to_later_candidate_when_earlier_absent(self):
        assert _dhan_num_or_none(None, 12345.0) == 12345.0

    def test_invalid_value_falls_through(self):
        assert _dhan_num_or_none("not-a-number", 500.0) == 500.0


# ---------------------------------------------------------------------------
# Dhan — margins available/utilised/normalise
# ---------------------------------------------------------------------------

class TestDhanMarginsMissingVsZero:
    def test_avail_margin_none_when_neither_spelling_present(self):
        """Repro of the exact prod bug: an account whose fund_limits
        response uses neither `availabelBalance` nor `availableBalance`
        must surface avail_margin (`net`) as None, not a coerced 0.00 —
        so grammar.py's _metric_avail_margin skips the leaf instead of
        reading a false breach."""
        resp = {"data": {"sodLimit": 100000.0, "utilizedAmount": 20000.0}}
        result = _dhan_normalise_margins(resp, segment=None)
        assert result["net"] is None
        assert result["available"]["cash"] is None
        assert result["available"]["live_balance"] is None

    def test_avail_margin_real_zero_preserved(self):
        """A genuine zero balance (key present, value 0) must still
        surface as 0.0 — only MISSING data is suppressed, per the
        plan's explicit "a true 0 must still be allowed to alert"."""
        resp = {"data": {"availableBalance": 0.0}}
        result = _dhan_normalise_margins(resp, segment=None)
        assert result["net"] == 0.0

    def test_typo_spelling_still_resolves(self):
        resp = {"data": {"availabelBalance": 42000.0}}
        result = _dhan_normalise_margins(resp, segment=None)
        assert result["net"] == 42000.0

    def test_correct_spelling_still_resolves(self):
        resp = {"data": {"availableBalance": 42000.0}}
        result = _dhan_normalise_margins(resp, segment=None)
        assert result["net"] == 42000.0

    def test_realised_pnl_none_when_no_spelling_present(self):
        resp = {"data": {"availableBalance": 1000.0}}
        result = _dhan_normalise_margins(resp, segment=None)
        assert result["utilised"]["m2m_realised"] is None

    def test_option_premium_none_when_no_field_present(self):
        resp = {"data": {"availableBalance": 1000.0}}
        result = _dhan_normalise_margins(resp, segment=None)
        assert result["utilised"]["option_premium"] is None

    def test_used_margin_debits_none_when_utilized_amount_absent(self):
        """`util debits` feeds grammar.py's `_metric_used_margin` — same
        missing-vs-zero class of bug as avail_margin/net."""
        resp = {"data": {"availableBalance": 1000.0}}
        result = _dhan_normalise_margins(resp, segment=None)
        assert result["utilised"]["debits"] is None

    def test_used_margin_debits_real_zero_preserved(self):
        resp = {"data": {"availableBalance": 1000.0, "utilizedAmount": 0.0}}
        result = _dhan_normalise_margins(resp, segment=None)
        assert result["utilised"]["debits"] == 0.0

    def test_opening_balance_none_when_sod_limit_absent(self):
        """`avail opening_balance` feeds grammar.py's `_metric_cash`."""
        resp = {"data": {"availableBalance": 1000.0}}
        result = _dhan_normalise_margins(resp, segment=None)
        assert result["available"]["opening_balance"] is None

    def test_collateral_none_when_absent(self):
        """`avail collateral` feeds grammar.py's `_metric_collateral`."""
        resp = {"data": {"availableBalance": 1000.0}}
        result = _dhan_normalise_margins(resp, segment=None)
        assert result["available"]["collateral"] is None
        assert result["utilised"]["stock_collateral"] is None

    def test_fields_with_no_dhan_source_are_none_not_hardcoded_zero(self):
        """Fields Dhan's fund_limits endpoint genuinely does not expose
        (exposure, m2m_unrealised, span, holding_sales, turnover,
        liquid_collateral, intraday_payin) must be None, honestly
        reflecting "not supported by this broker" rather than a
        fabricated 0.0 that looks like a real broker-reported value."""
        resp = {"data": {"availableBalance": 1000.0, "sodLimit": 500.0}}
        result = _dhan_normalise_margins(resp, segment=None)
        assert result["available"]["intraday_payin"] is None
        u = result["utilised"]
        assert u["exposure"] is None
        assert u["m2m_unrealised"] is None
        assert u["span"] is None
        assert u["holding_sales"] is None
        assert u["turnover"] is None
        assert u["liquid_collateral"] is None

    def test_fully_populated_response_has_no_none_leakage_on_mapped_fields(self):
        """Sanity: when Dhan sends every mapped field, none of them
        collapse to None — the fix must not regress the happy path."""
        resp = {
            "data": {
                "availableBalance": 500000.0,
                "sodLimit": 400000.0,
                "collateralAmount": 10000.0,
                "utilizedAmount": 20000.0,
                "withdrawableBalance": 480000.0,
                "realizedProfit": 1500.0,
                "optionPremium": 2500.0,
            }
        }
        result = _dhan_normalise_margins(resp, segment=None)
        assert result["net"] == 500000.0
        assert result["available"]["opening_balance"] == 400000.0
        assert result["available"]["collateral"] == 10000.0
        assert result["utilised"]["debits"] == 20000.0
        assert result["utilised"]["payout"] == 480000.0
        assert result["utilised"]["m2m_realised"] == 1500.0
        assert result["utilised"]["option_premium"] == 2500.0

    def test_dhan_margins_available_direct_call_none_cash(self):
        result = _dhan_margins_available({}, cash=None)
        assert result["cash"] is None
        assert result["opening_balance"] is None
        assert result["intraday_payin"] is None

    def test_dhan_margins_utilised_direct_call_none_realised(self):
        result = _dhan_margins_utilised({}, realised=None, opt_prem=None)
        assert result["m2m_realised"] is None
        assert result["option_premium"] is None
        assert result["exposure"] is None


# ---------------------------------------------------------------------------
# Groww — helper unit tests
# ---------------------------------------------------------------------------

class TestGrowwGfOrNone:
    def test_no_keys_present_returns_none(self):
        assert _gf_or_none({}, "a", "b") is None

    def test_real_zero_on_first_key_not_skipped(self):
        # Pre-fix `_first` used truthiness, so a real 0 on the first key
        # was skipped in favour of the second. The fixed helper must
        # treat "key present with value 0" as found, not absent.
        assert _gf_or_none({"a": 0, "b": 999}, "a", "b") == 0.0

    def test_falls_through_to_second_key_when_first_absent(self):
        assert _gf_or_none({"b": 42.0}, "a", "b") == 42.0

    def test_invalid_value_returns_none(self):
        assert _gf_or_none({"a": "nope"}, "a") is None


# ---------------------------------------------------------------------------
# Groww — margins available/utilised/normalise
# ---------------------------------------------------------------------------

class TestGrowwMarginsMissingVsZero:
    def test_avail_margin_none_when_net_and_available_balance_absent(self):
        """Repro of the exact prod bug on the Groww side: an account
        whose margin-details response carries neither `net` nor
        `available_balance` must surface avail_margin as None."""
        resp = {"data": {"opening_balance": 100000.0}}
        result = _groww_normalise_margins(resp, segment=None)
        assert result["net"] is None

    def test_avail_margin_real_zero_preserved(self):
        resp = {"data": {"net": 0.0}}
        result = _groww_normalise_margins(resp, segment=None)
        assert result["net"] == 0.0

    def test_avail_margin_falls_back_to_available_balance(self):
        resp = {"data": {"available_balance": 75000.0}}
        result = _groww_normalise_margins(resp, segment=None)
        assert result["net"] == 75000.0

    def test_cash_none_when_opening_balance_absent(self):
        """`avail opening_balance` feeds grammar.py's `_metric_cash`."""
        resp = {"data": {"net": 1000.0}}
        result = _groww_normalise_margins(resp, segment=None)
        assert result["available"]["opening_balance"] is None

    def test_used_margin_debits_none_when_utilised_absent(self):
        """`util debits` feeds grammar.py's `_metric_used_margin`."""
        resp = {"data": {"net": 1000.0}}
        result = _groww_normalise_margins(resp, segment=None)
        assert result["utilised"]["debits"] is None

    def test_used_margin_debits_real_zero_preserved(self):
        resp = {"data": {"net": 1000.0, "utilised": 0.0}}
        result = _groww_normalise_margins(resp, segment=None)
        assert result["utilised"]["debits"] == 0.0

    def test_collateral_none_when_absent(self):
        """`avail collateral` feeds grammar.py's `_metric_collateral`."""
        resp = {"data": {"net": 1000.0}}
        result = _groww_normalise_margins(resp, segment=None)
        assert result["available"]["collateral"] is None

    def test_fields_with_no_groww_source_are_none_not_hardcoded_zero(self):
        resp = {"data": {"net": 1000.0}}
        result = _groww_normalise_margins(resp, segment=None)
        u = result["utilised"]
        assert u["holding_sales"] is None
        assert u["turnover"] is None
        assert u["liquid_collateral"] is None

    def test_fully_populated_response_has_no_none_leakage_on_mapped_fields(self):
        resp = {
            "data": {
                "net": 500000.0,
                "available_balance": 450000.0,
                "opening_balance": 600000.0,
                "collateral": 25000.0,
                "utilised": 150000.0,
                "exposure_margin": 100000.0,
                "realised_pnl": 5000.0,
                "unrealised_pnl": -2000.0,
                "option_premium": 3000.0,
                "payout": 1000.0,
                "span_margin": 20000.0,
                "stock_collateral": 15000.0,
            }
        }
        result = _groww_normalise_margins(resp, segment=None)
        assert result["net"] == 500000.0
        assert result["available"]["opening_balance"] == 600000.0
        assert result["available"]["collateral"] == 25000.0
        assert result["utilised"]["debits"] == 150000.0
        assert result["utilised"]["exposure"] == 100000.0
        assert result["utilised"]["m2m_realised"] == 5000.0
        assert result["utilised"]["m2m_unrealised"] == -2000.0
        assert result["utilised"]["option_premium"] == 3000.0
        assert result["utilised"]["payout"] == 1000.0
        assert result["utilised"]["span"] == 20000.0
        assert result["utilised"]["stock_collateral"] == 15000.0

    def test_groww_margin_available_direct_call_empty_dict(self):
        result = _groww_margin_available({})
        assert result["cash"] is None
        assert result["opening_balance"] is None
        assert result["collateral"] is None

    def test_groww_margin_utilised_direct_call_empty_dict(self):
        result = _groww_margin_utilised({})
        assert result["debits"] is None
        assert result["holding_sales"] is None
        assert result["turnover"] is None
        assert result["liquid_collateral"] is None

    def test_groww_real_prod_response_shape_resolves_correctly(self):
        """Repro grounded in an ACTUAL Groww raw-key log observed in prod
        (account GR87DF, 2026-09): the response really carries `clear_cash`,
        `collateral_available`, `collateral_used`, `net_margin_used`,
        `adhoc_margin` — NOT the pre-fix guessed names (`net`,
        `available_balance`, `opening_balance`, `collateral`, `utilised`).
        Pre-fix, every one of these fields defaulted to 0.0 on every real
        Groww account, not just on a rare missing-field edge case. This
        test locks in the corrected mapping against the confirmed real
        shape."""
        resp = {
            "adhoc_margin": 1000.0,
            "brokerage_and_charges": 50.0,
            "clear_cash": 275000.0,
            "collateral_available": 40000.0,
            "collateral_used": 10000.0,
            "commodity_margin_details": {},
            "equity_margin_details": {},
            "fno_margin_details": {},
            "net_margin_used": 60000.0,
        }
        result = _groww_normalise_margins(resp, segment=None)
        assert result["net"] == 275000.0
        assert result["available"]["cash"] == 275000.0
        assert result["available"]["opening_balance"] == 275000.0
        assert result["available"]["collateral"] == 40000.0
        assert result["available"]["adhoc_margin"] == 1000.0
        assert result["utilised"]["debits"] == 60000.0
        assert result["utilised"]["stock_collateral"] == 10000.0


# ---------------------------------------------------------------------------
# End-to-end: adapter → _fetch_margins_local → the raw row grammar.py reads
# ---------------------------------------------------------------------------

class TestMissingFundsFieldSurvivesFullFetchPath:
    """Prove None survives the WHOLE broker-layer path (adapter normaliser
    → `_fetch_margins_local`'s pandas DataFrame → `json_normalize` prefix
    flattening) into the exact row shape `grammar.py`'s `_metric_cash` /
    `_metric_avail_margin` / `_metric_used_margin` read (`net`,
    `avail opening_balance`, `util debits`). Without this test, a fix that
    only holds at the normaliser layer could still be silently re-coerced
    to 0.0 by pandas/json_normalize before reaching the alert evaluator."""

    def test_dhan_missing_field_is_none_in_fetched_frame(self):
        resp = {"data": {"sodLimit": 100000.0, "utilizedAmount": 20000.0}}
        margins_data = _dhan_normalise_margins(resp, segment=None)
        assert margins_data["net"] is None  # sanity on the normaliser itself

        class _StubBroker:
            def margins(self, segment="equity"):
                return margins_data

        df = broker_apis._fetch_margins_local.__wrapped__(
            connections=None, account="DH_TEST", kite=None, broker=_StubBroker()
        )
        row = df.to_dict("records")[0]
        assert row["net"] is None
        assert row["avail opening_balance"] == 100000.0
        assert row["util debits"] == 20000.0

    def test_groww_missing_field_is_none_in_fetched_frame(self):
        resp = {"adhoc_margin": 1000.0}  # no clear_cash / net / available_balance at all
        margins_data = _groww_normalise_margins(resp, segment=None)
        assert margins_data["net"] is None

        class _StubBroker:
            def margins(self, segment="equity"):
                return margins_data

        df = broker_apis._fetch_margins_local.__wrapped__(
            connections=None, account="GR_TEST", kite=None, broker=_StubBroker()
        )
        row = df.to_dict("records")[0]
        assert row["net"] is None

    def test_dhan_real_zero_is_a_float_zero_not_none_in_fetched_frame(self):
        """A genuine zero must still reach the alert evaluator as 0.0, not
        None — only missing data is suppressed, per the plan's explicit
        "a true 0 must still be allowed to alert" requirement."""
        resp = {"data": {"availableBalance": 0.0, "sodLimit": 500.0}}
        margins_data = _dhan_normalise_margins(resp, segment=None)

        class _StubBroker:
            def margins(self, segment="equity"):
                return margins_data

        df = broker_apis._fetch_margins_local.__wrapped__(
            connections=None, account="DH_TEST2", kite=None, broker=_StubBroker()
        )
        row = df.to_dict("records")[0]
        assert row["net"] == 0.0
        assert row["net"] is not None
