"""
Sprint 19 backend — parameterized symbol-filtered scope resolution.

A per-position scope (`positions.expiring_today` and its `.nfo` /
`.mcx_unhedged` variants) can return several rows for the SAME account —
one per held symbol. Splitting "this symbol" and "this metric breaches"
into two separately-ANDed leaves is UNSAFE: `_eval_all`'s join key
(agent_evaluator.py) is `account` only, not `(account, symbol)`, so two
leaves could spuriously "agree" on account while referring to two
DIFFERENT symbol-rows within it.

Fix: an optional `scope_filter` key on the leaf JSON
(`{"metric", "scope", "op", "value", "scope_filter": {"symbol": "..."}}`)
applied by `_eval_leaf` to the rows the scope already selected, BEFORE the
metric evaluates each row — a single-leaf filter has no cross-leaf join
ambiguity. See `grammar.scope_filter_row_predicate`'s docstring for the
full design rationale (leaf-key vs parameterized-scope-token tradeoff).

Covers (per PLAN.md "Test coverage"):
  1. THE BUG THIS CLOSES — synthetic multi-symbol-same-account rows: only
     the symbol-filtered-and-breaching row fires, never a different
     symbol in the same account.
  2. Composition with `.nfo` and `.mcx_unhedged`, not just the bare
     `expiring_today` scope.
  3. Save-path: `create_agent` accepts a condition using the new shape
     without a 422.
  4. Regression: every EXISTING scope token (unfiltered) still resolves
     exactly as before — purely additive.
  5. Supporting unit coverage for `scope_filter_row_predicate` itself
     (wildcard, unknown qualifier, multi-qualifier AND).
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from datetime import datetime, timezone

import pandas as pd
import pytest

from backend.api.algo import grammar
from backend.api.algo.agent_evaluator import Context, evaluate, validate


# ─── Fixtures ────────────────────────────────────────────────────────────

# NIFTY 25-APR monthly options — expiry = last Thursday of April 2025
# (24th). `now` below is ~14:30 IST on expiry day, matching the existing
# expiry-resolver test convention (test_expiry_resolvers.py).
_NOW_EXPIRY_DAY = datetime(2025, 4, 24, 9, 0, tzinfo=timezone.utc)

_SYM_A = "NIFTY25APR22000CE"   # the breaching leg
_SYM_B = "NIFTY25APR23000PE"   # the non-breaching leg, SAME account


def _two_symbol_same_account_rows():
    """One account, two different F&O symbols, both expiring today.
    Row A breaches a `pnl <= -5000` threshold; row B does not."""
    return [
        {"account": "ACCT_X", "tradingsymbol": _SYM_A, "exchange": "NFO",
         "quantity": 50, "pnl": -6000.0},
        {"account": "ACCT_X", "tradingsymbol": _SYM_B, "exchange": "NFO",
         "quantity": -50, "pnl": -100.0},
    ]


@pytest.fixture(autouse=True)
def _wire_registry(monkeypatch):
    """REGISTRY is a process-wide singleton other test files' reload()
    calls mutate — wire exactly the real resolvers this file needs via
    monkeypatch (auto-restored after each test), same pattern as
    test_agent_evaluator.py's TestExposedColumnFallback / TestAllJoins."""
    from backend.api.algo.grammar_registry import REGISTRY
    monkeypatch.setattr(REGISTRY, 'metrics', {
        'pnl': grammar._metric_pnl, 'avail_margin': grammar._metric_avail_margin,
    })
    monkeypatch.setattr(REGISTRY, 'scopes', {
        'positions.expiring_today': grammar._scope_positions_expiring_today,
        'positions.expiring_today.nfo': grammar._scope_positions_expiring_today_nfo,
        'positions.expiring_today.mcx_unhedged': grammar._scope_positions_expiring_today_mcx_unhedged,
        'funds.any_acct': grammar._scope_funds_any_acct,
    })
    monkeypatch.setattr(REGISTRY, 'operators', {
        '<=': grammar.OPERATORS['<='], '>': grammar.OPERATORS['>'],
    })
    monkeypatch.setattr(REGISTRY, 'metric_factories', {})
    monkeypatch.setattr(REGISTRY, '_metric_call_cache', {})
    monkeypatch.setattr(REGISTRY, '_scope_call_cache', {})


# ═══════════════════════════════════════════════════════════════════════
#  1. THE BUG THIS CLOSES
# ═══════════════════════════════════════════════════════════════════════

class TestClosesJoinGranularityBug:
    def test_symbol_filter_targeting_non_breaching_row_does_not_fire(self):
        """scope_filter targets SYM_B (pnl=-100, not breaching). SYM_A in
        the SAME account DOES breach (-6000 <= -5000) but must NEVER leak
        through just because it shares the account — this is the exact
        defect two-ANDed-leaves would have produced."""
        ctx = Context(position_rows=_two_symbol_same_account_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": -5000,
            "scope_filter": {"symbol": _SYM_B},
        }
        result = evaluate(leaf, ctx)
        assert result == [], (
            f"symbol filter must isolate row B only — expected no fire "
            f"(row B's own pnl=-100 does not breach), got {result}"
        )

    def test_symbol_filter_targeting_breaching_row_fires(self):
        """Same two rows, scope_filter now targets SYM_A — the row that
        genuinely breaches. Must fire, and the match must carry SYM_A's
        own row, not SYM_B's."""
        ctx = Context(position_rows=_two_symbol_same_account_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": -5000,
            "scope_filter": {"symbol": _SYM_A},
        }
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["row"]["tradingsymbol"] == _SYM_A
        assert result[0]["account"] == "ACCT_X"

    def test_without_scope_filter_both_rows_are_candidates(self):
        """Sanity check isolating the mechanism's effect: with NO
        scope_filter, the unfiltered leaf correctly fires once (for row
        A only, since row B doesn't breach) — proving the filter, not
        some other change, is what narrowed row B out above."""
        ctx = Context(position_rows=_two_symbol_same_account_rows(), now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "pnl", "scope": "positions.expiring_today", "op": "<=", "value": -5000}
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["row"]["tradingsymbol"] == _SYM_A

    def test_symbol_filter_does_not_leak_across_accounts(self):
        """Two accounts both hold SYM_A; only ACCT_X's row breaches.
        Filtering to SYM_A must still only fire for the breaching
        account's row, never fabricate a cross-account match."""
        rows = _two_symbol_same_account_rows() + [
            {"account": "ACCT_Y", "tradingsymbol": _SYM_A, "exchange": "NFO",
             "quantity": 10, "pnl": -10.0},
        ]
        ctx = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": -5000,
            "scope_filter": {"symbol": _SYM_A},
        }
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["account"] == "ACCT_X"


# ═══════════════════════════════════════════════════════════════════════
#  2. Composition with .nfo and .mcx_unhedged variants
# ═══════════════════════════════════════════════════════════════════════

class TestComposesWithEveryVariant:
    def test_composes_with_nfo_variant(self):
        """`.nfo` already restricts to NFO rows; scope_filter narrows
        further to one symbol on top of that, without a hand-written
        combination."""
        rows = _two_symbol_same_account_rows() + [
            # An MCX row also expiring today — .nfo must exclude this
            # regardless of scope_filter.
            {"account": "ACCT_X", "tradingsymbol": "GOLDM25APR75000CE",
             "exchange": "MCX", "quantity": 1, "pnl": -9999.0},
        ]
        ctx = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today.nfo",
            "op": "<=", "value": -5000,
            "scope_filter": {"symbol": _SYM_A},
        }
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["row"]["tradingsymbol"] == _SYM_A

    def test_nfo_variant_symbol_filter_excludes_mcx_row_even_if_named(self):
        """Filtering to the MCX symbol while scoped to `.nfo` must still
        return nothing — the base scope's own exchange filter runs first,
        same precedence `.nfo` already has over `expiring_today`."""
        rows = [
            {"account": "ACCT_X", "tradingsymbol": "GOLDM25APR75000CE",
             "exchange": "MCX", "quantity": 1, "pnl": -9999.0},
        ]
        ctx = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today.nfo",
            "op": "<=", "value": -5000,
            "scope_filter": {"symbol": "GOLDM25APR75000CE"},
        }
        assert evaluate(leaf, ctx) == []

    def test_composes_with_mcx_unhedged_variant(self):
        """A lone unhedged MCX leg survives `.mcx_unhedged`'s own
        hedge-balance filter; scope_filter narrows it to exactly that
        symbol on top, with no duplicated filtering logic."""
        rows = [
            # Unhedged lone leg — GOLDM, net != 0.
            {"account": "ACCT_X", "tradingsymbol": "GOLDM25APR75000CE",
             "exchange": "MCX", "quantity": 1, "pnl": -8000.0},
            # Perfectly hedged pair on a DIFFERENT root — GOLD, net == 0 —
            # `.mcx_unhedged` already drops this before scope_filter ever
            # sees it.
            {"account": "ACCT_X", "tradingsymbol": "GOLD25APR75000CE",
             "exchange": "MCX", "quantity": 1, "pnl": -9000.0},
            {"account": "ACCT_X", "tradingsymbol": "GOLD25APR75000PE",
             "exchange": "MCX", "quantity": -1, "pnl": -9000.0},
        ]
        ctx = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today.mcx_unhedged",
            "op": "<=", "value": -5000,
            "scope_filter": {"symbol": "GOLDM25APR75000CE"},
        }
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["row"]["tradingsymbol"] == "GOLDM25APR75000CE"

    def test_mcx_unhedged_symbol_filter_on_already_hedged_leg_still_empty(self):
        """Filtering to a leg that `.mcx_unhedged` already excludes
        (perfectly hedged pair) must still return nothing — the
        hedge-balance filter is not bypassed by scope_filter."""
        rows = [
            {"account": "ACCT_X", "tradingsymbol": "GOLD25APR75000CE",
             "exchange": "MCX", "quantity": 1, "pnl": -9000.0},
            {"account": "ACCT_X", "tradingsymbol": "GOLD25APR75000PE",
             "exchange": "MCX", "quantity": -1, "pnl": -9000.0},
        ]
        ctx = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        leaf = {
            "metric": "pnl", "scope": "positions.expiring_today.mcx_unhedged",
            "op": "<=", "value": -5000,
            "scope_filter": {"symbol": "GOLD25APR75000CE"},
        }
        assert evaluate(leaf, ctx) == []


# ═══════════════════════════════════════════════════════════════════════
#  3. Save-path — create_agent accepts the new shape without a 422
# ═══════════════════════════════════════════════════════════════════════

class TestSavePath:
    def test_validate_accepts_scope_filter_leaf(self):
        cond = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": -5000,
            "scope_filter": {"symbol": _SYM_A},
        }
        assert validate(cond) == []

    @pytest.mark.asyncio
    async def test_create_agent_accepts_scope_filter_condition(self, monkeypatch):
        """Mirrors test_agent_kind_tier_topic.py's
        test_create_agent_cycle_kind_accepts_valid_call_syntax_token
        pattern — exercises the REAL route handler (not just validate()
        directly), proving the end-to-end save path returns 200/201, not
        a 422."""
        from unittest.mock import AsyncMock, MagicMock, patch as _patch
        from backend.api.routes.agents import AgentController, AgentCreateRequest

        session = MagicMock()
        session.commit = AsyncMock()
        session.add = MagicMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=result_mock)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=cm)

        controller = AgentController.__new__(AgentController)
        data = AgentCreateRequest(
            slug="sprint19-scope-filter-test", name="Scope filter agent",
            events=[],
            conditions={
                "metric": "pnl", "scope": "positions.expiring_today",
                "op": "<=", "value": -5000,
                "scope_filter": {"symbol": _SYM_A},
            },
        )

        with _patch("backend.api.routes.agents.async_session", session_factory):
            out = await AgentController.create_agent.fn(controller, data)

        assert out == {"detail": f"Agent '{data.slug}' created"}
        session.commit.assert_awaited_once()

    def test_validate_rejects_scope_filter_with_unknown_qualifier(self):
        """`funds.any_acct` has no `symbol` dimension — a scope_filter
        naming it must be a compile error, same whitelist the Sprint 15
        exposed-column schema already enforces."""
        cond = {
            "metric": "avail_margin", "scope": "funds.any_acct",
            "op": ">", "value": 0,
            "scope_filter": {"symbol": "NIFTY25APR22000CE"},
        }
        errors = validate(cond)
        assert len(errors) == 1
        assert "scope_filter" in errors[0]
        assert "symbol" in errors[0]

    def test_validate_rejects_empty_scope_filter_dict(self):
        cond = {
            "metric": "pnl", "scope": "positions.expiring_today",
            "op": "<=", "value": -5000, "scope_filter": {},
        }
        errors = validate(cond)
        assert len(errors) == 1
        assert "scope_filter" in errors[0]


# ═══════════════════════════════════════════════════════════════════════
#  4. Regression — every existing scope token still resolves unchanged
# ═══════════════════════════════════════════════════════════════════════

class TestPurelyAdditiveRegression:
    def test_expiring_today_unfiltered_still_matches_every_breaching_row(self):
        """No scope_filter key at all (the overwhelming majority of
        existing agents) — both breaching rows across both accounts
        still fire, exactly as before this change."""
        rows = _two_symbol_same_account_rows() + [
            {"account": "ACCT_Y", "tradingsymbol": _SYM_A, "exchange": "NFO",
             "quantity": 10, "pnl": -7000.0},
        ]
        ctx = Context(position_rows=rows, now=_NOW_EXPIRY_DAY)
        leaf = {"metric": "pnl", "scope": "positions.expiring_today", "op": "<=", "value": -5000}
        result = evaluate(leaf, ctx)
        accounts = {m["account"] for m in result}
        assert accounts == {"ACCT_X", "ACCT_Y"}
        assert len(result) == 2

    def test_funds_any_acct_unfiltered_unchanged(self):
        """A completely unrelated scope family (funds, no symbol
        dimension) is untouched by this change when no scope_filter key
        is present."""
        df = pd.DataFrame([
            {"account": "ACCT_A", "net": 10000.0},
            {"account": "ACCT_B", "net": -500.0},
        ])
        ctx = Context(df_margins=df)
        leaf = {"metric": "avail_margin", "scope": "funds.any_acct", "op": ">", "value": 0}
        result = evaluate(leaf, ctx)
        assert len(result) == 1
        assert result[0]["account"] == "ACCT_A"

    def test_validate_unaffected_for_leaf_without_scope_filter(self):
        cond = {"metric": "pnl", "scope": "positions.expiring_today", "op": "<=", "value": -5000}
        assert validate(cond) == []


# ═══════════════════════════════════════════════════════════════════════
#  5. Unit coverage for scope_filter_row_predicate itself
# ═══════════════════════════════════════════════════════════════════════

class TestScopeFilterRowPredicateUnit:
    def test_wildcard_value_matches_any_row(self):
        predicate = grammar.scope_filter_row_predicate(
            "positions.expiring_today", {"symbol": "*"})
        assert predicate is not None
        assert predicate({"tradingsymbol": "ANYTHING"}) is True
        assert predicate({"tradingsymbol": ""}) is True

    def test_unknown_qualifier_returns_none(self):
        assert grammar.scope_filter_row_predicate(
            "funds.any_acct", {"symbol": "X"}) is None

    def test_known_qualifier_exact_match(self):
        predicate = grammar.scope_filter_row_predicate(
            "positions.expiring_today", {"symbol": "NIFTY25APR22000CE"})
        assert predicate is not None
        assert predicate({"tradingsymbol": "NIFTY25APR22000CE"}) is True
        assert predicate({"tradingsymbol": "NIFTY25APR23000PE"}) is False

    def test_multi_qualifier_and_combines(self):
        predicate = grammar.scope_filter_row_predicate(
            "positions.expiring_today",
            {"symbol": "NIFTY25APR22000CE", "account_id": "ACCT_X"})
        assert predicate is not None
        assert predicate({"tradingsymbol": "NIFTY25APR22000CE", "account": "ACCT_X"}) is True
        assert predicate({"tradingsymbol": "NIFTY25APR22000CE", "account": "ACCT_Y"}) is False

    def test_empty_filter_dict_matches_everything(self):
        predicate = grammar.scope_filter_row_predicate("positions.expiring_today", {})
        assert predicate is not None
        assert predicate({"tradingsymbol": "anything"}) is True

    def test_runtime_leaf_with_unknown_qualifier_logs_and_skips(self, monkeypatch):
        """Mirrors _resolve_metric_fn's 'genuinely unknown token' runtime
        behaviour — a config error at the scope_filter level must skip
        gracefully (never raise), with a warning logged."""
        import backend.api.algo.agent_evaluator as ev_mod
        warnings = []
        monkeypatch.setattr(ev_mod.logger, 'warning', lambda msg: warnings.append(msg))
        df = pd.DataFrame([{"account": "ACCT_A", "net": 10000.0}])
        ctx = Context(df_margins=df)
        leaf = {
            "metric": "avail_margin", "scope": "funds.any_acct", "op": ">", "value": 0,
            "scope_filter": {"symbol": "X"},
        }
        result = evaluate(leaf, ctx)
        assert result == []
        assert any("scope_filter" in w for w in warnings)
