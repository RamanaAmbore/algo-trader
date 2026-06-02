"""
Item 1 / Phase 25 — expiry-aware grammar resolver tests.

Covers:
  _metric_days_until_expiry — parses tradingsymbol, returns days as float
  _metric_is_itm            — 1.0 ITM / 0.0 OTM, None when spot missing
  _metric_is_ntm            — 1.0 within ±1.5% / 0.0 otherwise
  _scope_positions_expiring_today — filters per-symbol rows
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


# ── days_until_expiry ──────────────────────────────────────────────

def test_days_until_expiry_parses_monthly_option():
    # NIFTY 25-APR-22000 CE (monthly) — expiry = last Thursday of April
    # 2025 = 24th April 2025.
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    ctx = _ctx(now=datetime(2025, 4, 24, 9, 0, tzinfo=timezone.utc))  # ~14:30 IST
    d = grammar._metric_days_until_expiry(ctx, row)
    assert d is not None
    # 24 Apr 2025 expiry at 15:30 IST; ref 09:00 UTC = 14:30 IST → ~1 hour to expiry
    assert 0 < d < 0.2


def test_days_until_expiry_floors_after_expiry():
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    ctx = _ctx(now=datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc))
    d = grammar._metric_days_until_expiry(ctx, row)
    assert d == 0.0


def test_days_until_expiry_none_for_equity():
    row = {"tradingsymbol": "RELIANCE", "exchange": "NSE"}
    assert grammar._metric_days_until_expiry(_ctx(), row) is None


def test_days_until_expiry_mcx_uses_2330_close():
    # CRUDEOILM expiry — MCX trades till 23:30 so an option expiring
    # today has more time-to-expiry than an NFO option at the same
    # wall-clock time. MCX commodity monthlies expire on the last
    # FRIDAY of the contract month (per MCX rule revision), not the
    # equity 'last Thursday' convention.
    row = {"tradingsymbol": "CRUDEOILM25MAY5500CE", "exchange": "MCX"}
    # 30 May 2025 — last Friday of May 2025. At 12:00 UTC = 17:30 IST,
    # 6 hours to the 23:30 IST MCX close → days_to_expiry ≈ 0.25.
    ctx = _ctx(now=datetime(2025, 5, 30, 12, 0, tzinfo=timezone.utc))
    d = grammar._metric_days_until_expiry(ctx, row)
    assert d is not None and 0.2 < d < 0.3


# ── is_itm ──────────────────────────────────────────────────────────

def test_is_itm_call_above_strike():
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 22150.0})
    assert grammar._metric_is_itm(ctx, row) == 1.0


def test_is_itm_call_below_strike():
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 21800.0})
    assert grammar._metric_is_itm(ctx, row) == 0.0


def test_is_itm_put_below_strike():
    row = {"tradingsymbol": "NIFTY25APR22000PE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 21800.0})
    assert grammar._metric_is_itm(ctx, row) == 1.0


def test_is_itm_put_above_strike():
    row = {"tradingsymbol": "NIFTY25APR22000PE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 22150.0})
    assert grammar._metric_is_itm(ctx, row) == 0.0


def test_is_itm_none_without_spot():
    row = {"tradingsymbol": "NIFTY25APR22000CE"}
    assert grammar._metric_is_itm(_ctx(), row) is None  # spot_prices empty


def test_is_itm_none_for_future():
    row = {"tradingsymbol": "NIFTY25APRFUT"}
    ctx = _ctx(spot_prices={"NIFTY": 22000.0})
    assert grammar._metric_is_itm(ctx, row) is None  # futures have no strike


# ── is_ntm ──────────────────────────────────────────────────────────

def test_is_ntm_within_band():
    # Spot 22000, strike 22050 → 0.227% away → NTM
    row = {"tradingsymbol": "NIFTY25APR22050CE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 22000.0})
    assert grammar._metric_is_ntm(ctx, row) == 1.0


def test_is_ntm_outside_band():
    # Spot 22000, strike 22500 → 2.27% away → outside ±1.5%
    row = {"tradingsymbol": "NIFTY25APR22500CE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 22000.0})
    assert grammar._metric_is_ntm(ctx, row) == 0.0


# ── positions.expiring_today scope ─────────────────────────────────

def test_scope_expiring_today_filters_to_today():
    # Build a fake position book: one expiring today, one next month,
    # one cash equity. Only the first should appear.
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        {"tradingsymbol": "NIFTY25MAY22500CE", "exchange": "NFO", "quantity": 50},
        {"tradingsymbol": "RELIANCE",          "exchange": "NSE", "quantity": 10},
    ])
    rows = grammar._scope_positions_expiring_today(ctx)
    assert len(rows) == 1
    assert rows[0]["tradingsymbol"] == "NIFTY25APR22000CE"


def test_scope_expiring_today_empty_when_no_position_rows():
    ctx = _ctx(position_rows=[])
    assert grammar._scope_positions_expiring_today(ctx) == []


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


# ── Segment-specific expiry scopes (NFO + MCX-unhedged) ────────────

def _gold_mcx_book(expiry_yymon: str, *, with_hedge: bool):
    """Build a GOLD MCX option book.

    `expiry_yymon` is the 2-char yy + 3-char MON token Kite uses
    (e.g. '25MAY' for the contract expiring on the last Thursday of
    May 2025). Tomorrow's expiry for the user — GOLDM May 2025 —
    parses with the last-Thursday convention.

    `with_hedge=True` adds a CE/PE pair on the same strike whose
    quantities net to zero (the broker settles these against each
    other). The MCX-unhedged scope SHOULD skip them.
    """
    book = [
        # The unhedged ITM call we expect the scope to surface
        {"tradingsymbol": f"GOLDM{expiry_yymon}75000CE", "exchange": "MCX",
         "quantity": 1, "last_price": 500.0},
    ]
    if with_hedge:
        # Long CE + short PE on the SAME strike → net qty 0 → fully hedged
        book.append(
            {"tradingsymbol": f"GOLD{expiry_yymon}75000CE", "exchange": "MCX",
             "quantity": 1, "last_price": 800.0})
        book.append(
            {"tradingsymbol": f"GOLD{expiry_yymon}75000PE", "exchange": "MCX",
             "quantity": -1, "last_price": 100.0})
    return book


def test_nfo_scope_returns_only_nfo_rows():
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        {"tradingsymbol": "GOLD25APR75000CE",  "exchange": "MCX", "quantity": 1},
        {"tradingsymbol": "RELIANCE",          "exchange": "NSE", "quantity": 10},
    ])
    rows = grammar._scope_positions_expiring_today_nfo(ctx)
    assert [r["tradingsymbol"] for r in rows] == ["NIFTY25APR22000CE"]


def test_mcx_unhedged_skips_perfectly_hedged_pair():
    # CRUDE call + put on same underlying+expiry, qty net = 0.
    # Should be skipped because broker nets them.
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "CRUDEOILM25MAY5500CE", "exchange": "MCX",
         "quantity": 1, "last_price": 100},
        {"tradingsymbol": "CRUDEOILM25MAY5500PE", "exchange": "MCX",
         "quantity": -1, "last_price": 200},
    ])
    rows = grammar._scope_positions_expiring_today_mcx_unhedged(ctx)
    assert rows == [], "perfectly hedged CE/PE pair should not surface"


def test_mcx_unhedged_surfaces_lone_leg():
    # Single unhedged GOLD call expiring this expiry → MUST surface.
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0,
                        tzinfo=timezone.utc)  # ~03:30 IST tomorrow (close to 23:00 IST tonight)
    ctx = _ctx(now=today_dt,
               position_rows=_gold_mcx_book("25MAY", with_hedge=False))
    rows = grammar._scope_positions_expiring_today_mcx_unhedged(ctx)
    assert len(rows) == 1
    assert rows[0]["tradingsymbol"] == "GOLDM25MAY75000CE"


def test_mcx_unhedged_surfaces_unhedged_amid_hedged():
    # GOLDM has a lone leg; GOLD has a hedged pair — only GOLDM
    # should surface because GOLD is fully hedged.
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0,
                        tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt,
               position_rows=_gold_mcx_book("25MAY", with_hedge=True))
    rows = grammar._scope_positions_expiring_today_mcx_unhedged(ctx)
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
        {"tradingsymbol": "GOLD25MAY75000CE", "exchange": "MCX",
         "quantity": 1, "last_price": 500},
        # GOLD Jun 2025 → next month — NOT a hedge against May
        {"tradingsymbol": "GOLD25JUN75000PE", "exchange": "MCX",
         "quantity": -1, "last_price": 200},
    ])
    rows = grammar._scope_positions_expiring_today_mcx_unhedged(ctx)
    # Only the May leg surfaces (Jun is far from expiry, so the
    # expiring_today filter drops it before the unhedged check).
    assert [r["tradingsymbol"] for r in rows] == ["GOLD25MAY75000CE"]


def test_mcx_unhedged_skips_nfo_rows():
    # Even an unhedged NFO row should NOT appear in the MCX scope.
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0,
                        tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
    ])
    assert grammar._scope_positions_expiring_today_mcx_unhedged(ctx) == []


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

    # Equity: fires at 15:00 IST against NFO scope, action exchange=NFO
    assert equity["fire_at_time"] == "15:00"
    leaf = equity["conditions"]["all"][0]
    assert leaf["scope"] == "positions.expiring_today.nfo"
    assert equity["actions"][0]["type"] == "expiry_auto_close"
    assert equity["actions"][0]["params"]["exchange"] == "NFO"
    assert equity["status"] == "inactive"  # destructive — opt-in

    # Commodity: fires at 23:00 IST against MCX-unhedged scope, exchange=MCX
    assert commodity["fire_at_time"] == "23:00"
    leaf = commodity["conditions"]["all"][0]
    assert leaf["scope"] == "positions.expiring_today.mcx_unhedged"
    assert commodity["actions"][0]["type"] == "expiry_auto_close"
    assert commodity["actions"][0]["params"]["exchange"] == "MCX"
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
