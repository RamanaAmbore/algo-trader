"""Tests for grammar_tokens/scope_positions_expiring_today_mcx_unhedged.py
— `positions.expiring_today.mcx_unhedged` scope.

2026-11 classifier re-base (operator decision): this scope no longer
classifies via the crude CE/PE signed-quantity sum
(`compute_mcx_ce_pe_net_groups`, kept unchanged — still used by
`background.py`'s generic `is_unhedged`/`net_qty` DSL columns). It now
calls `expiry.classify_from_context()`, the SAME real 4-rule
theta-priority netting `ExpiryEngine.scan_positions()` uses for
`expiry-day-preclose-alert`/`expiry-day-itm-auto-close`. Fixtures
below were updated accordingly:

  - every "hedge" fixture now needs a real, rule-valid netting pair
    (same-type-opposite-sign, or opposite-type-same-sign) AND a real
    `spot_prices` entry, since the engine can only net legs whose
    moneyness resolves to ITM/NTM;
  - two NEW tests pin the exact disagreement the operator's re-base
    decision was meant to close: a "synthetic" combo (long CE + short
    PE, same strike — a directional position, not a hedge) that the
    OLD heuristic wrongly excluded, and a "guts" position (long CE +
    long PE, different ITM strikes, equal qty) that the OLD heuristic
    wrongly flagged;
  - a new test pins the UNKNOWN-moneyness inclusion rule (no spot
    price ⇒ surfaced, never silently assumed safe).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_positions_expiring_today_mcx_unhedged import (
    _scope_positions_expiring_today_mcx_unhedged,
    compute_mcx_ce_pe_net_groups,
)


def _ctx(*, position_rows=None, spot_prices=None, now=None) -> Context:
    return Context(
        sum_positions=pd.DataFrame(),
        position_rows=position_rows or [],
        spot_prices=spot_prices or {},
        now=now or datetime.now(timezone.utc),
    )


def test_mcx_unhedged_skips_genuinely_netted_pair():
    """Long CE + short CE at the SAME strike (rule 1: same type,
    opposite sign) is a real, engine-valid netting pair — residual 0,
    never flagged. Both legs priced ITM so they genuinely enter the
    netting pool (an OTM leg never enters the engine's `mcx_in_money`
    candidate list at all)."""
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "CRUDEOILM25MAY5500CE", "exchange": "MCX", "account": "A1",
         "quantity": 1, "last_price": 100},
        {"tradingsymbol": "CRUDEOILM25MAY5500CE", "exchange": "MCX", "account": "A1",
         "quantity": -1, "last_price": 100},
    ], spot_prices={"CRUDEOILM": 6000.0})
    rows = _scope_positions_expiring_today_mcx_unhedged(ctx)
    assert rows == [], "a genuine rule-1 (same-strike, opposite-sign) CE pair should net to zero"


def test_mcx_unhedged_surfaces_lone_leg():
    # Single unhedged GOLD call expiring this expiry, with no genuine
    # partner at all → MUST surface (residual == quantity).
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0,
                        tzinfo=timezone.utc)  # ~03:30 IST tomorrow (close to 23:00 IST tonight)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "GOLDM25MAY75000CE", "exchange": "MCX", "account": "A1",
         "quantity": 1, "last_price": 500.0},
    ], spot_prices={"GOLDM": 76000.0})
    rows = _scope_positions_expiring_today_mcx_unhedged(ctx)
    assert len(rows) == 1
    assert rows[0]["tradingsymbol"] == "GOLDM25MAY75000CE"


def test_mcx_unhedged_surfaces_unhedged_amid_hedged():
    # GOLDM has a lone leg; GOLD has a genuinely-netted rule-1 pair —
    # only GOLDM should surface because GOLD is fully, validly hedged.
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0,
                        tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "GOLDM25MAY75000CE", "exchange": "MCX", "account": "A1",
         "quantity": 1, "last_price": 500.0},
        {"tradingsymbol": "GOLD25MAY75000CE", "exchange": "MCX", "account": "A2",
         "quantity": 1, "last_price": 800.0},
        {"tradingsymbol": "GOLD25MAY75000CE", "exchange": "MCX", "account": "A2",
         "quantity": -1, "last_price": 800.0},
    ], spot_prices={"GOLDM": 76000.0, "GOLD": 76000.0})
    rows = _scope_positions_expiring_today_mcx_unhedged(ctx)
    syms = {r["tradingsymbol"] for r in rows}
    assert syms == {"GOLDM25MAY75000CE"}, (
        f"only the GOLDM leg should surface (hedged GOLD pair must be skipped), got {syms}")


def test_mcx_unhedged_groups_by_underlying_plus_expiry():
    # Same underlying but DIFFERENT expiry months should be grouped
    # separately — a hedge across different expirations isn't a hedge.
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0,
                        tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        # GOLD May 2025 → expiry tomorrow
        {"tradingsymbol": "GOLD25MAY75000CE", "exchange": "MCX", "account": "A1",
         "quantity": 1, "last_price": 500},
        # GOLD Jun 2025 → next month — NOT a hedge against May
        {"tradingsymbol": "GOLD25JUN75000PE", "exchange": "MCX", "account": "A1",
         "quantity": -1, "last_price": 200},
    ], spot_prices={"GOLD": 76000.0})
    rows = _scope_positions_expiring_today_mcx_unhedged(ctx)
    # Only the May leg surfaces (Jun is far from expiry, so the
    # expiring_today filter drops it before the unhedged check).
    assert [r["tradingsymbol"] for r in rows] == ["GOLD25MAY75000CE"]


def test_mcx_unhedged_skips_nfo_rows():
    # Even an unhedged NFO row should NOT appear in the MCX scope.
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0,
                        tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "account": "A1", "quantity": 50},
    ])
    assert _scope_positions_expiring_today_mcx_unhedged(ctx) == []


# ── Classifier re-base discriminator cases (2026-11) ──────────────────

def test_mcx_unhedged_flags_synthetic_combo_old_heuristic_missed():
    """Long CE + short PE at the SAME strike is a synthetic directional
    position (effectively a long future via options), NOT a hedge — no
    valid opt-opt netting rule covers opposite-type + opposite-sign.
    The OLD crude sum-based heuristic summed these to zero and silently
    excluded the whole group; the real engine correctly flags the ITM
    leg. This is the audit's reproduced disagreement."""
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0, tzinfo=timezone.utc)
    rows = [
        {"tradingsymbol": "GOLD25MAY75000CE", "exchange": "MCX", "account": "A1",
         "quantity": 1, "last_price": 1100.0},
        {"tradingsymbol": "GOLD25MAY75000PE", "exchange": "MCX", "account": "A1",
         "quantity": -1, "last_price": 100.0},
    ]
    # Prove the OLD heuristic really did net this to zero (excluded).
    old_groups = compute_mcx_ce_pe_net_groups(rows)
    assert all(g["net_qty"] == 0 for g in old_groups.values())

    ctx = _ctx(now=today_dt, position_rows=rows, spot_prices={"GOLD": 76000.0})
    new_rows = _scope_positions_expiring_today_mcx_unhedged(ctx)
    assert [r["tradingsymbol"] for r in new_rows] == ["GOLD25MAY75000CE"], (
        "the real engine should flag the ITM CE leg — no valid netting "
        "partner exists for a synthetic long-CE/short-PE combo")


def test_mcx_unhedged_nets_guts_position_old_heuristic_flagged():
    """Long CE (ITM, strike below spot) + long PE (ITM, different
    strike above spot), equal quantity — a 'guts' position. Rule 3
    (same sign, opposite type) is a valid netting pair, so the real
    engine nets this to zero residual. The OLD heuristic summed
    +1 + +1 = 2 (never zero for same-sign legs) and wrongly flagged
    both legs."""
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0, tzinfo=timezone.utc)
    rows = [
        {"tradingsymbol": "GOLD25MAY70000CE", "exchange": "MCX", "account": "A1",
         "quantity": 1, "last_price": 6500.0},
        {"tradingsymbol": "GOLD25MAY82000PE", "exchange": "MCX", "account": "A1",
         "quantity": 1, "last_price": 6200.0},
    ]
    old_groups = compute_mcx_ce_pe_net_groups(rows)
    assert all(g["net_qty"] != 0 for g in old_groups.values()), (
        "the old heuristic should have wrongly flagged this as unhedged")

    ctx = _ctx(now=today_dt, position_rows=rows, spot_prices={"GOLD": 76000.0})
    new_rows = _scope_positions_expiring_today_mcx_unhedged(ctx)
    assert new_rows == [], (
        "the real engine's rule 3 (long CE + long PE, same sign) should "
        "net this guts position to zero residual")


def test_mcx_unhedged_surfaces_unknown_moneyness_lone_leg():
    """No spot price at all for the root ⇒ moneyness is UNKNOWN. The
    scope must still surface the leg (never a silent all-clear for
    unconfirmed data) — matches the alert renderer's own explicit
    UNKNOWN warning (`_v2_expiry_root_unknown_lines`)."""
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "GOLDM25MAY75000CE", "exchange": "MCX", "account": "A1",
         "quantity": 1, "last_price": 500.0},
    ], spot_prices={})
    rows = _scope_positions_expiring_today_mcx_unhedged(ctx)
    assert [r["tradingsymbol"] for r in rows] == ["GOLDM25MAY75000CE"]


def test_mcx_unhedged_excludes_flat_position():
    """A squared-off (qty==0) leg is never surfaced — matches
    `ExpiryEngine._fetch_option_positions()`'s own `qty == 0` skip."""
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "GOLDM25MAY75000CE", "exchange": "MCX", "account": "A1",
         "quantity": 0, "last_price": 500.0},
    ], spot_prices={"GOLDM": 76000.0})
    assert _scope_positions_expiring_today_mcx_unhedged(ctx) == []


# ── Fail-LOUD on classifier exception (not a false all-clear) ──────────
#
# Pre-fix behaviour: a `classify_from_context()` exception degraded to
# `return []` — "nothing confirmed unhedged". That is exactly the false
# all-clear this codebase's design philosophy forbids on every OTHER
# resolver (see CLAUDE.md: "never a false all-clear") — and this is a
# RISK-ALERT resolver, on expiry day, the single highest-stakes moment
# the mechanism exists for. Fixed: on exception, log CRITICAL (so the
# ERROR_AGENT catch-all surfaces the resolver failure itself as an
# operator-visible event — CRITICAL records are auto-tagged "error" by
# `tags_for()`, no extra tags/alert_event needed) and return every
# currently-expiring MCX position — the conservative "I don't know, so
# alert on everything expiring" answer — never an empty list.

def test_mcx_unhedged_classifier_exception_fails_loud_not_quiet(monkeypatch, caplog):
    """A `classify_from_context` exception must surface EVERY expiring
    MCX row (not `[]`) and log CRITICAL — proving this resolver can
    never again produce a false all-clear on a classifier failure."""
    import backend.api.algo.grammar_tokens.scope_positions_expiring_today_mcx_unhedged as mod

    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0, tzinfo=timezone.utc)
    rows = [
        {"tradingsymbol": "GOLDM25MAY75000CE", "exchange": "MCX", "account": "A1",
         "quantity": 1, "last_price": 500.0, "pnl": -1234.5},
        {"tradingsymbol": "GOLD25MAY75000CE", "exchange": "MCX", "account": "A2",
         "quantity": 1, "last_price": 800.0, "pnl": 500.0},
    ]
    ctx = _ctx(now=today_dt, position_rows=rows, spot_prices={"GOLDM": 76000.0, "GOLD": 76000.0})

    def _boom(*a, **kw):
        raise RuntimeError("simulated classify_from_context failure")

    # Patch the real module `expiry.classify_from_context` resolves to —
    # the resolver imports it lazily INSIDE the function, so patching the
    # source module (not a name on `mod`) is what actually takes effect.
    import backend.api.algo.expiry as expiry_mod
    monkeypatch.setattr(expiry_mod, "classify_from_context", _boom)

    mod.logger.addHandler(caplog.handler)
    old_level = mod.logger.level
    mod.logger.setLevel("CRITICAL")
    try:
        result = _scope_positions_expiring_today_mcx_unhedged(ctx)
    finally:
        mod.logger.removeHandler(caplog.handler)
        mod.logger.setLevel(old_level)

    # Fail LOUD, never a quiet empty list: every expiring MCX row surfaces.
    assert [r["tradingsymbol"] for r in result] == [
        "GOLDM25MAY75000CE", "GOLD25MAY75000CE",
    ], "a classifier exception must surface every expiring MCX row, never []"

    critical_records = [r for r in caplog.records if r.levelno >= 50]
    assert len(critical_records) == 1, "classifier exception must log exactly one CRITICAL record"
    assert "classify_from_context" in critical_records[0].getMessage()


def test_mcx_unhedged_classifier_exception_triggers_real_agent_condition():
    """End-to-end: evaluate the REAL `expiry-mcx-risk-alert` condition
    tree (same shape as the seeded agent in `agent_engine.py`) against a
    ctx whose classifier raises. The fail-loud fallback rows must carry
    enough real data (`pnl`) for the condition's own metric leaf to
    actually fire — proving the fix reaches the agent, not just the
    scope resolver in isolation.

    `GrammarRegistry.reload()` is DB-backed (reads the `grammar_tokens`
    table), so this unit test can't rely on it. Same pattern as
    `test_scope_where_filter.py`: register the REAL scope resolver and
    the real `>=` operator directly on the `REGISTRY` singleton for the
    duration of this test. The `pnl` metric needs no registration at
    all — it resolves via `grammar.EXPOSED_COLUMNS`'s static fallback
    (`_resolve_metric_fn`), which carries no DB dependency. The `not`
    branch's `funds.total` leaves are deliberately left unregistered —
    `_eval_leaf` returns `[]` (a no-match, not a crash) for an
    unregistered scope/op, and `ctx.df_margins=None` makes `funds.total`
    resolve to `[]` either way, so the `not` branch correctly flips to
    a true match regardless."""
    import backend.api.algo.expiry as expiry_mod
    from backend.api.algo import agent_evaluator
    from backend.api.algo.grammar import OPERATORS
    from backend.api.algo.grammar_registry import REGISTRY

    def _boom(*a, **kw):
        raise RuntimeError("simulated classify_from_context failure")

    orig_classify = expiry_mod.classify_from_context
    expiry_mod.classify_from_context = _boom
    had_scope = 'positions.expiring_today.mcx_unhedged' in REGISTRY.scopes
    orig_scope = REGISTRY.scopes.get('positions.expiring_today.mcx_unhedged')
    had_op = '>=' in REGISTRY.operators
    orig_op = REGISTRY.operators.get('>=')
    REGISTRY.scopes['positions.expiring_today.mcx_unhedged'] = (
        _scope_positions_expiring_today_mcx_unhedged
    )
    REGISTRY.operators['>='] = OPERATORS['>=']
    try:
        today = date(2025, 5, 29)
        today_dt = datetime(today.year, today.month, today.day, 22, 0, tzinfo=timezone.utc)
        ctx = agent_evaluator.Context(
            sum_positions=pd.DataFrame(),
            position_rows=[
                {"tradingsymbol": "GOLDM25MAY75000CE", "exchange": "MCX",
                 "account": "A1", "quantity": 1, "last_price": 500.0, "pnl": -999.0},
            ],
            spot_prices={"GOLDM": 76000.0},
            df_margins=None,
            now=today_dt,
        )
        condition = {"all": [
            {"op": ">=", "scope": "positions.expiring_today.mcx_unhedged",
             "metric": "pnl", "value": -999999999},
            {"not": {"all": [
                {"op": "==", "scope": "funds.total", "metric": "is_mcx_expiry_today", "value": 1.0},
                {"op": "<=", "scope": "funds.total", "metric": "minutes_until_mcx_close", "value": 30},
            ]}},
        ]}
        matches = agent_evaluator.evaluate(condition, ctx)
    finally:
        expiry_mod.classify_from_context = orig_classify
        if had_scope:
            REGISTRY.scopes['positions.expiring_today.mcx_unhedged'] = orig_scope
        else:
            REGISTRY.scopes.pop('positions.expiring_today.mcx_unhedged', None)
        if had_op:
            REGISTRY.operators['>='] = orig_op
        else:
            REGISTRY.operators.pop('>=', None)

    assert matches, (
        "the real expiry-mcx-risk-alert condition must evaluate as "
        "TRIGGERED when the classifier raises — a quiet [] scope result "
        "would silently pass this condition instead"
    )
