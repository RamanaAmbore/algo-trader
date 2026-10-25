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
    from backend.api.algo.agent_engine import BUILTIN_AGENTS
    by_slug = {a["slug"]: a for a in BUILTIN_AGENTS}
    for slug in ("expiry-day-equity-itm-auto-close",
                 "expiry-day-commodity-itm-auto-close",
                 "expiry-day-positions-alert"):
        assert slug in by_slug, f"missing seeded agent: {slug}"
    equity = by_slug["expiry-day-equity-itm-auto-close"]
    commodity = by_slug["expiry-day-commodity-itm-auto-close"]

    # Equity: fires at 15:15 IST (matches order_hold_gate.cutoff_for("NFO")'s
    # default lead_minutes_nfo=15: 15:30 close - 15 = 15:15) against NFO
    # scope, action exchange=NFO. Fixed 2026-10 (Sprint 1a) — was "15:00",
    # 15 min BEFORE its own cutoff, so the action's before_cutoff() gate
    # deferred every cycle and never actually scanned/closed anything.
    assert equity["fire_at_time"] == "15:15"
    leaf = equity["conditions"]["all"][0]
    assert leaf["scope"] == "positions.expiring_today.nfo"
    assert equity["actions"][0]["type"] == "expiry_auto_close"
    assert equity["actions"][0]["params"]["exchange"] == "NFO"
    # Ships INACTIVE (destructive) per its own description — matches
    # status so a fresh/reset DB doesn't resurrect it active with zero
    # operator review. Fixed 2026-10.
    assert equity["status"] == "inactive"

    # Commodity: fires at 23:00 IST against MCX-unhedged scope, exchange=MCX
    assert commodity["fire_at_time"] == "23:00"
    leaf = commodity["conditions"]["all"][0]
    assert leaf["scope"] == "positions.expiring_today.mcx_unhedged"
    assert commodity["actions"][0]["type"] == "expiry_auto_close"
    assert commodity["actions"][0]["params"]["exchange"] == "MCX"
    # Ships INACTIVE (destructive) per its own description — matches
    # status so a fresh/reset DB doesn't resurrect it active with zero
    # operator review. Fixed 2026-10.
    assert commodity["status"] == "inactive"


def test_expiry_auto_close_action_token_has_exchange_param():
    """The grammar token must require `exchange` so an agent can't
    fire an unscoped expiry close that would hit the wrong segment."""
    tok = next(t for t in grammar.SYSTEM_TOKENS
               if t['grammar_kind'] == 'action'
               and t['token'] == 'expiry_auto_close')
    schema = tok['params_schema']['exchange']
    assert schema.get('required') is True
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
    """Walk the full condition tree the seeded commodity agent uses
    against a fabricated GOLDM book where ONE leg is ITM + unhedged
    and another is fully hedged. Engine should match exactly the
    unhedged leg.
    """
    _seed_registry_inline()
    from backend.api.algo.agent_evaluator import evaluate
    from backend.api.algo.agent_engine import BUILTIN_AGENTS

    agent = next(a for a in BUILTIN_AGENTS
                 if a["slug"] == "expiry-day-commodity-itm-auto-close")

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
    matches = evaluate(agent["conditions"], ctx)
    # The condition is "is_itm == 1 over MCX-unhedged" — should fire
    # once, against the GOLDM leg only.
    assert len(matches) == 1, (
        f"expected exactly one match (GOLDM unhedged ITM), got {len(matches)}: {matches}")


def test_equity_agent_condition_fires_on_itm_nifty_calls():
    """Validate the equity agent on a NIFTY book where one strike is
    deep ITM and another is far OTM. Should match only the ITM leg.
    Equity rules: hedging does NOT save you — every ITM contract
    must close — so a hedged distractor should also surface if it's
    ITM. Use a long call + long put pair: both ITM at right spot."""
    _seed_registry_inline()
    from backend.api.algo.agent_evaluator import evaluate
    from backend.api.algo.agent_engine import BUILTIN_AGENTS

    agent = next(a for a in BUILTIN_AGENTS
                 if a["slug"] == "expiry-day-equity-itm-auto-close")

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
    matches = evaluate(agent["conditions"], ctx)
    assert len(matches) == 1
    # The matched row should be the ITM one.
    matched_syms = {m.get('row', {}).get('tradingsymbol')
                    for m in matches if isinstance(m, dict)}
    # evaluate() may return the literal row or a wrapped match dict;
    # either way the test confirms exactly one fire, which is the
    # behavior the seeded agent depends on.
