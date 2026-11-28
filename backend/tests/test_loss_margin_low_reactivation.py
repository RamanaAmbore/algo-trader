"""End-to-end tests for the 2026-11-28 `loss-margin-low` reactivation.

Background: `loss-margin-low` (`backend/api/algo/agent_engine.py`) shipped
`status="inactive"` from 2026-09 because of two confirmed false-positive
mechanisms, both already fixed and unit-tested elsewhere before this round:

  Fix #2 — `all[]` used to flatten cross-account matches independently, so
  one account's `avail_margin<25000` match could combine with a DIFFERENT
  account's `avail_margin>0` match and fire with no single account
  satisfying both leaves. Fixed in `agent_evaluator.py:_eval_all` (per
  account/underlying join). Unit-tested in
  `test_agent_evaluator.py::TestAllJoinsSameAccount`.

  Fix #3 — the Dhan/Groww funds adapters defaulted a genuinely-unmapped
  field to a coerced `0.0`, indistinguishable from a real zero balance.
  Fixed via `_dhan_num_or_none`/`_gf_or_none` (None, not 0.0, on a missing
  field) + `_metric_avail_margin`'s `_num_or_none`. Unit-tested in
  `test_funds_missing_vs_zero.py` and
  `grammar_tokens/test_metric_avail_margin.py`.

This file is the missing END-TO-END piece: it drives the REAL seeded
condition read off `BUILTIN_AGENTS` (never a hand-typed copy, so it can't
silently drift from what ships) through the real adapter normalisers,
`_fetch_margins_local.__wrapped__`, and `agent_evaluator.evaluate()` —
covering exactly the account/broker combinations the operator's reactivation
ask named: a Dhan account hitting the missing-field quirk, a Groww account
hitting it (using the REAL confirmed prod field names — `clear_cash` etc.,
not the pre-fix guessed names), a genuine low-margin Kite account (must
still fire), and a healthy account on any broker (must not fire) — plus the
exact historical cross-account mixed scenario (one broker's unmapped field
+ a different, healthy account) that produced the original 61 false fires.

Five quality dimensions:
  SSOT        — condition tree read from the real `BUILTIN_AGENTS` seed,
                never duplicated by hand.
  Correctness — missing-vs-zero AND cross-account join both exercised
                together, through the real adapter -> DataFrame -> evaluate
                pipeline, not mocked at the resolver boundary.
  Reuse       — `_wire_registry`-style fixture mirrors the established
                pattern in test_agent_evaluator.py; `_StubBroker` mirrors
                test_funds_missing_vs_zero.py's full-fetch-path tests.
  Data path   — every row goes through the SAME `_fetch_margins_local`
                flattening (`utilised`/`available` -> `util `/`avail `
                prefixed columns) real production traffic uses.
  UX/alerting — asserts WHICH account fired, not just "did something fire"
                — a false fire on the wrong account is as bad as a missed
                real one.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.api.algo.agent_engine import BUILTIN_AGENTS
from backend.brokers import broker_apis
from backend.brokers.adapters.dhan import _normalise_margins as _dhan_normalise_margins
from backend.brokers.adapters.groww import _normalise_margins as _groww_normalise_margins


def _loss_margin_low_condition() -> dict:
    agent = next((a for a in BUILTIN_AGENTS if a["slug"] == "loss-margin-low"), None)
    assert agent is not None, "loss-margin-low not found in BUILTIN_AGENTS"
    assert agent.get("status") == "active", (
        "loss-margin-low must be active for this end-to-end test to mean "
        f"anything — got status={agent.get('status')!r}"
    )
    return agent["conditions"]


class _StubBroker:
    """Mirrors test_funds_missing_vs_zero.py's full-fetch-path stub —
    `.margins(segment=...)` returns a pre-normalised Kite-shape dict, same
    as every real adapter's `broker.margins()` call."""

    def __init__(self, margins_data: dict):
        self._margins_data = margins_data

    def margins(self, segment: str = "equity") -> dict:
        return self._margins_data


def _fetch_one_account_margins(account: str, margins_data: dict) -> pd.DataFrame:
    """Run ONE account's already-normalised margins dict through the real
    `_fetch_margins_local` flattening — the exact function every broker's
    margins poll uses, unwrapped to call directly without the
    `@for_all_accounts` multi-account fan-out."""
    return broker_apis._fetch_margins_local.__wrapped__(
        connections=None, account=account, kite=None, broker=_StubBroker(margins_data)
    )


def _kite_margins(net: float) -> dict:
    """A Kite-shape margins() response — flat, 'net' present directly,
    matching the real Zerodha API shape (confirmed live on prod: ZG0790/
    ZJ6294 both read 'net' as a bare top-level float)."""
    return {
        "enabled": True,
        "net": net,
        "available": {"cash": net, "opening_balance": net, "collateral": 0.0},
        "utilised": {"debits": 0.0, "m2m_realised": 0.0, "option_premium": 0.0},
    }


@pytest.fixture
def _wired_registry(monkeypatch):
    """Wires the real grammar.py/grammar_tokens resolvers used by
    `loss-margin-low`'s condition tree into REGISTRY — mirrors
    test_agent_evaluator.py::TestAllJoinsSameAccount's own fixture, since
    REGISTRY is normally populated from the DB at app startup."""
    from backend.api.algo.grammar import OPERATORS
    from backend.api.algo.grammar_registry import REGISTRY
    from backend.api.algo.grammar_tokens.metric_avail_margin import _metric_avail_margin
    from backend.api.algo.grammar_tokens.scope_funds_any_acct import _scope_funds_any_acct

    monkeypatch.setitem(REGISTRY.metrics, "avail_margin", _metric_avail_margin)
    monkeypatch.setitem(REGISTRY.scopes, "funds.any_acct", _scope_funds_any_acct)
    for op_tok in ("<", ">"):
        monkeypatch.setitem(REGISTRY.operators, op_tok, OPERATORS[op_tok])
    return REGISTRY


def _evaluate_accounts(_wired_registry, frames: list[pd.DataFrame]) -> list[dict]:
    from backend.api.algo.agent_evaluator import Context, evaluate

    non_empty = [f for f in frames if f is not None and not f.empty]
    df = pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame()
    ctx = Context(df_margins=df)
    return evaluate(_loss_margin_low_condition(), ctx)


class TestDhanMissingFieldNeverFalseFires:
    """A Dhan account whose fund_limits response carries neither
    `availabelBalance` nor `availableBalance` — the exact confirmed prod
    repro — must resolve avail_margin to None and never match either leaf."""

    def test_dhan_account_alone_does_not_fire(self, _wired_registry):
        margins_data = _dhan_normalise_margins(
            {"data": {"sodLimit": 100000.0, "utilizedAmount": 20000.0}}, segment=None
        )
        assert margins_data["net"] is None, "sanity: normaliser must still return None here"
        df = _fetch_one_account_margins("DH_MISSING", margins_data)
        assert df.to_dict("records")[0]["net"] is None, (
            "sanity: None must survive _fetch_margins_local's flattening"
        )
        result = _evaluate_accounts(_wired_registry, [df])
        assert result == [], (
            f"Dhan account with an unmapped avail_margin field must not "
            f"fire — got {result}"
        )

    def test_dhan_missing_field_plus_different_healthy_account_does_not_fire(
        self, _wired_registry
    ):
        """The EXACT historical false-fire shape: one account's missing
        field used to default to 0.0 (satisfying `<25000`), a DIFFERENT
        healthy account satisfied `>0`, and `all[]` used to flatten both
        into one fire. Both fixes together must now suppress this."""
        dhan_margins = _dhan_normalise_margins(
            {"data": {"sodLimit": 100000.0}}, segment=None
        )
        dhan_df = _fetch_one_account_margins("DH_MISSING", dhan_margins)
        kite_df = _fetch_one_account_margins("ZZ_HEALTHY", _kite_margins(373828.52))
        result = _evaluate_accounts(_wired_registry, [dhan_df, kite_df])
        assert result == [], (
            f"A Dhan account's missing field must not combine with a "
            f"different, healthy Kite account's match — got {result}"
        )


class TestGrowwMissingFieldNeverFalseFires:
    """A Groww account using the REAL confirmed prod response shape
    (clear_cash / collateral_available / collateral_used / net_margin_used
    / adhoc_margin — see test_funds_missing_vs_zero.py's
    test_groww_real_prod_response_shape_resolves_correctly) but missing
    even that shape's own fields must resolve to None, not 0."""

    def test_groww_account_alone_does_not_fire(self, _wired_registry):
        # No clear_cash, no net, no available_balance at all.
        margins_data = _groww_normalise_margins({"adhoc_margin": 1000.0}, segment=None)
        assert margins_data["net"] is None, "sanity: normaliser must still return None here"
        df = _fetch_one_account_margins("GR_MISSING", margins_data)
        assert df.to_dict("records")[0]["net"] is None
        result = _evaluate_accounts(_wired_registry, [df])
        assert result == [], (
            f"Groww account with an unmapped avail_margin field must not "
            f"fire — got {result}"
        )

    def test_groww_real_prod_shape_without_clear_cash_does_not_fire(self, _wired_registry):
        """Real prod Groww payload shape, but with clear_cash stripped —
        the exact field that previously defaulted to 0.0 on every real
        Groww account before the mapping fix."""
        resp = {
            "adhoc_margin": 1000.0,
            "collateral_available": 40000.0,
            "collateral_used": 10000.0,
            "net_margin_used": 60000.0,
            # clear_cash deliberately absent
        }
        margins_data = _groww_normalise_margins(resp, segment=None)
        assert margins_data["net"] is None
        df = _fetch_one_account_margins("GR87DF", margins_data)
        result = _evaluate_accounts(_wired_registry, [df])
        assert result == [], f"expected no fire, got {result}"


class TestGenuineZeroIsExcludedByGTZero:
    """A broker genuinely reporting avail_margin=0 (confirmed live on prod
    2026-11-28 for DH6847/DH3747/GR87DF — every fund_limits field,
    including sodLimit, was a real 0.0, not an absent key) must NOT fire —
    excluded by the `>0` leaf, same as it always would be for a real zero
    on any broker."""

    def test_dhan_real_zero_does_not_fire(self, _wired_registry):
        margins_data = _dhan_normalise_margins(
            {"data": {"availableBalance": 0.0, "sodLimit": 0.0}}, segment=None
        )
        assert margins_data["net"] == 0.0
        df = _fetch_one_account_margins("DH6847", margins_data)
        result = _evaluate_accounts(_wired_registry, [df])
        assert result == [], f"a real 0 must not satisfy avail_margin>0 — got {result}"

    def test_groww_real_zero_does_not_fire(self, _wired_registry):
        margins_data = _groww_normalise_margins({"clear_cash": 0.0}, segment=None)
        assert margins_data["net"] == 0.0
        df = _fetch_one_account_margins("GR87DF", margins_data)
        result = _evaluate_accounts(_wired_registry, [df])
        assert result == [], f"a real 0 must not satisfy avail_margin>0 — got {result}"


class TestGenuineLowMarginKiteStillFires:
    """A real, non-missing low-margin Kite account (0 < net < 25000) must
    still fire — the whole point of the alert must not be lost by the
    missing-vs-zero / cross-account fixes."""

    def test_kite_account_in_band_fires_on_that_account_only(self, _wired_registry):
        kite_df = _fetch_one_account_margins("ZG0790", _kite_margins(12000.0))
        result = _evaluate_accounts(_wired_registry, [kite_df])
        assert result, "a genuine 0 < avail_margin < 25000 Kite account must fire"
        assert all(m.get("account") == "ZG0790" for m in result), f"got {result}"

    def test_kite_in_band_plus_healthy_dhan_fires_only_on_kite(self, _wired_registry):
        """Mixed-broker sanity: a genuine Kite breach must still fire even
        alongside an unrelated, healthy Dhan account — and must name only
        the breaching account."""
        kite_df = _fetch_one_account_margins("ZG0790", _kite_margins(12000.0))
        dhan_margins = _dhan_normalise_margins(
            {"data": {"availableBalance": 500000.0}}, segment=None
        )
        dhan_df = _fetch_one_account_margins("DH6847", dhan_margins)
        result = _evaluate_accounts(_wired_registry, [kite_df, dhan_df])
        assert result, "the genuine Kite breach must still fire"
        assert all(m.get("account") == "ZG0790" for m in result), f"got {result}"


class TestHealthyAccountOnAnyBrokerDoesNotFire:
    """A genuinely healthy avail_margin (well above 25000) on any broker
    must not fire, regardless of which broker reported it."""

    @pytest.mark.parametrize(
        "account,build_margins",
        [
            ("ZG0790", lambda: _kite_margins(10125930.45)),
            ("DH6847", lambda: _dhan_normalise_margins(
                {"data": {"availableBalance": 500000.0}}, segment=None
            )),
            ("GR87DF", lambda: _groww_normalise_margins(
                {"clear_cash": 275000.0}, segment=None
            )),
        ],
        ids=["kite", "dhan", "groww"],
    )
    def test_healthy_margin_does_not_fire(self, _wired_registry, account, build_margins):
        df = _fetch_one_account_margins(account, build_margins())
        result = _evaluate_accounts(_wired_registry, [df])
        assert result == [], f"{account}: healthy margin must not fire, got {result}"


class TestAllFiveTestAccountsTogetherMirrorsLiveProd:
    """Reproduces the exact live prod snapshot taken 2026-11-28 (via a
    read-only `fetch_margins(force_refresh=True)` call against
    conn_service) — ZG0790/ZJ6294 healthy, DH6847/DH3747/GR87DF all a
    genuine broker-reported 0 across every fund_limits field. None of
    the five should fire."""

    def test_live_prod_snapshot_shape_produces_no_fire(self, _wired_registry):
        frames = [
            _fetch_one_account_margins("ZG0790", _kite_margins(10125930.45)),
            _fetch_one_account_margins("ZJ6294", _kite_margins(5538487.0)),
            _fetch_one_account_margins(
                "DH6847",
                _dhan_normalise_margins(
                    {"data": {
                        "availabelBalance": 0.0, "sodLimit": 0.0,
                        "collateralAmount": 0.0, "utilizedAmount": 0.0,
                        "withdrawableBalance": 0.0,
                    }},
                    segment=None,
                ),
            ),
            _fetch_one_account_margins(
                "DH3747",
                _dhan_normalise_margins(
                    {"data": {
                        "availabelBalance": 0.0, "sodLimit": 0.0,
                        "collateralAmount": 0.0, "utilizedAmount": 0.0,
                        "withdrawableBalance": 0.0,
                    }},
                    segment=None,
                ),
            ),
            _fetch_one_account_margins(
                "GR87DF",
                _groww_normalise_margins(
                    {"clear_cash": 0.0, "net_margin_used": 0.0,
                     "collateral_used": 0.0, "collateral_available": 0.0,
                     "adhoc_margin": 0.0},
                    segment=None,
                ),
            ),
        ]
        result = _evaluate_accounts(_wired_registry, frames)
        assert result == [], f"live prod snapshot shape must not fire — got {result}"
