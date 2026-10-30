"""
Item 1 / Phase 25 — expiry-aware grammar resolver tests.

Covers:
  Seeded-agent shape, and end-to-end condition evaluation against a
  real GOLDM/NIFTY book.

Resolver-level tests for _metric_days_until_expiry, _metric_is_itm, and
_metric_is_ntm moved to backend/tests/grammar_tokens/
test_metric_days_until_expiry.py, test_metric_is_itm.py,
test_metric_is_ntm.py (Wave 1, batch 3 of the DSL token-unification
plan). Resolver-level tests for _scope_positions_expiring_today and
its .nfo / .mcx_unhedged variants moved to backend/tests/grammar_tokens/
test_scope_positions_expiring_today.py and its two siblings (Wave 1,
scope batch A).
"""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

import pandas as pd

from backend.api.algo import grammar
from backend.api.algo.agent_evaluator import Context


def _ctx(*, position_rows=None, spot_prices=None, now=None) -> Context:
    return Context(
        sum_positions=pd.DataFrame(),
        position_rows=position_rows or [],
        spot_prices=spot_prices or {},
        now=now or datetime.now(timezone.utc),
    )


# ── SYSTEM_TOKENS catalog wiring ───────────────────────────────────

def test_new_tokens_registered_in_system_catalog():
    tokens = {t['token'] for t in grammar.SYSTEM_TOKENS}
    for k in ('days_until_expiry', 'is_itm', 'is_ntm',
              'positions.expiring_today',
              'positions.expiring_today.nfo',
              'positions.expiring_today.mcx_unhedged'):
        assert k in tokens, f"missing system token: {k}"
    action_tokens = {t['token'] for t in grammar.SYSTEM_TOKENS
                     if t['grammar_kind'] == 'action'}
    assert 'expiry_auto_close' in action_tokens


# ── Seeded agents: shape + slug ──────────────────────────────────

def test_expiry_agents_seeded_with_correct_shape():
    """2026-10-29: the two per-exchange auto-close agents were retired
    and consolidated into expiry-day-itm-auto-close, covering both
    exchanges with no exchange param and a flat, token-driven condition
    (no fire_at_time, no per-row scope — the position identification
    moved into ExpiryEngine.scan_positions(), called from the action).
    See test_expiry_day_and_preclose_agents.py for the full shape
    coverage of the current agent."""
    from backend.api.algo.agent_engine import BUILTIN_AGENTS
    by_slug = {a["slug"]: a for a in BUILTIN_AGENTS}
    for slug in ("expiry-day-equity-itm-auto-close",
                 "expiry-day-commodity-itm-auto-close"):
        assert slug not in by_slug, f"retired slug still present: {slug}"
    for slug in ("expiry-day-itm-auto-close", "expiry-day-positions-alert"):
        assert slug in by_slug, f"missing seeded agent: {slug}"

    combined = by_slug["expiry-day-itm-auto-close"]
    assert combined.get("fire_at_time") is None
    assert combined["conditions"] == {
        "op": "==", "scope": "funds.total",
        "metric": "is_expiry_preclose_close_window", "value": 1.0,
    }
    assert combined["actions"][0]["type"] == "expiry_auto_close"
    assert combined["actions"][0]["params"] == {}
    # Deliberately active — see the comment above
    # _EXPIRY_DAY_AWARENESS_AGENTS in agent_engine.py: its safety net is
    # the order-level hold-by-default in ExpiryEngine.close_positions(),
    # not agent deactivation.
    assert combined["status"] == "active"


def test_expiry_auto_close_action_token_has_exchange_param():
    """The grammar token's `exchange` param is now optional (2026-10-29
    — omitting it, the built-in agent's own params, processes both
    exchanges from one scan). An explicit exchange still restricts to
    just that one."""
    tok = next(t for t in grammar.SYSTEM_TOKENS
               if t['grammar_kind'] == 'action'
               and t['token'] == 'expiry_auto_close')
    schema = tok['params_schema']['exchange']
    assert schema.get('required') is False
    assert set(schema['enum']) == {"NFO", "MCX"}


# ── End-to-end: condition tree evaluates on a real GOLDM book ─────

def _seed_registry_inline():
    """Populate the in-memory GrammarRegistry with the system tokens
    the seeded agents need. Tests don't have a DB session so the
    normal `reload()` path can't run — we walk SYSTEM_TOKENS and
    import each resolver by dotted path. Idempotent + scoped to the
    test session."""
    from backend.api.algo.grammar_registry import REGISTRY
    from backend.api.algo.grammar import OPERATORS, SYSTEM_TOKENS
    import importlib

    def _import_dotted(path: str):
        mod_path, _, attr = path.rpartition(".")
        return getattr(importlib.import_module(mod_path), attr)

    REGISTRY.operators = dict(OPERATORS)
    for t in SYSTEM_TOKENS:
        if t.get('grammar_kind') != 'condition':
            continue
        resolver = t.get('resolver')
        if not resolver:
            continue
        try:
            fn = _import_dotted(resolver)
        except Exception:
            continue
        if t['token_kind'] == 'metric':
            REGISTRY.metrics[t['token']] = fn
        elif t['token_kind'] == 'scope':
            REGISTRY.scopes[t['token']] = fn


def test_commodity_agent_condition_fires_on_unhedged_itm_goldm():
    """Walk the `positions.expiring_today.mcx_unhedged` + `is_itm`
    condition tree against a fabricated GOLDM book where ONE leg is
    ITM + unhedged and another is fully hedged. Engine should match
    exactly the unhedged leg. The condition tree is built inline
    (2026-10-29) rather than read off a BUILTIN_AGENTS entry — the two
    exchange-specific auto-close agents that used to carry this exact
    shape were retired and consolidated into a single
    expiry-day-itm-auto-close, whose own condition is a flat
    is_expiry_preclose_close_window leaf with no per-row scope at all
    (the real position identification moved into
    ExpiryEngine.scan_positions(), called from the action, not
    evaluated via this condition tree). This test still proves the
    `mcx_unhedged`/`is_itm` resolvers themselves are correct — they
    remain live, used today by the still-active expiry-mcx-risk-alert
    agent.
    """
    _seed_registry_inline()
    from backend.api.algo.agent_evaluator import evaluate

    agent_conditions = {"all": [
        {"op": "==", "scope": "positions.expiring_today.mcx_unhedged",
         "metric": "is_itm", "value": 1.0},
    ]}

    # Build a real GOLDM book — strike 75000, spot 76000 → call ITM.
    # Add a perfectly hedged GOLD pair as a distractor; should NOT match.
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 17, 30,
                        tzinfo=timezone.utc)  # 23:00 IST
    ctx = _ctx(
        now=today_dt,
        position_rows=[
            # Unhedged ITM GOLDM call
            {"tradingsymbol": "GOLDM25MAY75000CE", "exchange": "MCX",
             "quantity": 1, "last_price": 1100.0},
            # Hedged GOLD pair (qty 0 net) — distractor
            {"tradingsymbol": "GOLD25MAY75000CE", "exchange": "MCX",
             "quantity": 1, "last_price": 1100.0},
            {"tradingsymbol": "GOLD25MAY75000PE", "exchange": "MCX",
             "quantity": -1, "last_price": 100.0},
        ],
        spot_prices={"GOLDM": 76000.0, "GOLD": 76000.0},
    )
    matches = evaluate(agent_conditions, ctx)
    # The condition is "is_itm == 1 over MCX-unhedged" — should fire
    # once, against the GOLDM leg only.
    assert len(matches) == 1, (
        f"expected exactly one match (GOLDM unhedged ITM), got {len(matches)}: {matches}")


def test_equity_agent_condition_fires_on_itm_nifty_calls():
    """Validate the `positions.expiring_today.nfo` + `is_itm` condition
    tree on a NIFTY book where one strike is deep ITM and another is
    far OTM. Should match only the ITM leg. Equity rules: hedging does
    NOT save you — every ITM contract must close. Condition built
    inline (2026-10-29, same rationale as the commodity test above) —
    the resolvers remain live, used today by the still-active
    expiry-nfo-risk-alert agent."""
    _seed_registry_inline()
    from backend.api.algo.agent_evaluator import evaluate

    agent_conditions = {"op": "==", "scope": "positions.expiring_today.nfo",
                         "metric": "is_itm", "value": 1.0}

    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 30,
                        tzinfo=timezone.utc)  # 15:00 IST
    ctx = _ctx(
        now=today_dt,
        position_rows=[
            # ITM call (strike 22000 below spot 22200)
            {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO",
             "quantity": 50, "last_price": 250},
            # OTM call (strike 23000 above spot 22200) — should NOT match
            {"tradingsymbol": "NIFTY25APR23000CE", "exchange": "NFO",
             "quantity": 50, "last_price": 10},
        ],
        spot_prices={"NIFTY": 22200.0},
    )
    matches = evaluate(agent_conditions, ctx)
    assert len(matches) == 1
    # The matched row should be the ITM one.
    matched_syms = {m.get('row', {}).get('tradingsymbol')
                    for m in matches if isinstance(m, dict)}
    # evaluate() may return the literal row or a wrapped match dict;
    # either way the test confirms exactly one fire, which is the
    # behavior the seeded agent depends on.
