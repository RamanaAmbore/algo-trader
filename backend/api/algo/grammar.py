"""
Agent grammar — condition / notify / action tokens.

The Agent engine (conditions, notify, actions) is defined entirely by TOKENS
stored in `grammar_tokens`. The engine holds no hard-coded list of metrics,
channels, or actions; it loads the catalog into an in-memory dispatch table
(the Registry) and evaluates agents against it.

Adding a new capability = insert a row (and, for metrics/actions, implement
one Python function). NO grammar change, NO engine change.

Three grammar domains
  condition — metrics (number-producing), scopes (row-selecting), operators,
              functions (future: arithmetic / string helpers inside templates).
  notify    — channels (how to deliver), formats (how to render), templates
              (what to say).
  action    — action types that DO things — place/modify/cancel/chase orders,
              monitor fills, toggle agent state, set runtime flags.

Vocabulary
  AGENT   — the rule row. Evaluated every tick during market hours.
  ALERT   — the runtime event an agent produces when its condition fires.
  NOTIFY  — a channel that delivers the alert.
  ACTION  — a side-effect the alert invokes.

System tokens are defined in SYSTEM_TOKENS below and upserted at startup
with is_system=True. Operators can add/deactivate custom tokens via the
admin UI (planned) but cannot delete system tokens.

Resolvers live in this file for now so we can review the full surface area
in one place. Later the dispatch table will support resolvers in any module
via dotted-path import — the `resolver` column already stores a string.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  GRAMMAR CATALOG LOADER (Phase 1 of order/agent grammar unification)
# ═══════════════════════════════════════════════════════════════════════════
#
# SYSTEM_TOKENS / LOG_TAG_TOKENS catalog METADATA (grammar_kind, token_kind,
# token, value_type, units, description, resolver dotted-path string,
# params_schema, template_body, etc.) lives in
# backend/config/grammars/agent_grammar.yaml, matching the convention
# `backend/config/grammars/orders.yaml` already established for the
# frontend CLI grammar. Resolver FUNCTION BODIES are NOT in the YAML — they
# stay exactly where they always were, as real Python functions in this
# module / actions.py / actions_live.py. Loading the YAML at import time and
# reconstructing the identical list-of-dicts shape below is pure data
# relocation — zero behavior change versus the former Python literal.
# ───────────────────────────────────────────────────────────────────────────

_GRAMMAR_YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "grammars" / "agent_grammar.yaml"


def _load_grammar_catalog() -> dict:
    with open(_GRAMMAR_YAML_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_GRAMMAR_CATALOG: dict = _load_grammar_catalog()


# ═══════════════════════════════════════════════════════════════════════════
#  SHARED ORDER-FIELD CATALOG (Phase 2 of order/agent grammar unification)
# ═══════════════════════════════════════════════════════════════════════════
#
# backend/config/grammars/order_fields.yaml defines type/enum/description
# ONCE for the field-level concepts place_order's params_schema shares with
# the frontend CLI's buy/sell grammar (qty, order_type, price,
# trigger_price, product, variety, tag, chase_level). A params_schema field
# spec in agent_grammar.yaml may reference one of these by writing
# `$ref: <key>` instead of hand-typing type/enum/description; any OTHER
# keys on that same spec (required, default, token_ref_ok, or a
# description override) are context-local and merged in on top.
#
# Resolution happens once, here, at import time — the result is what
# SYSTEM_TOKENS / grammar_tokens actually serve downstream. Non-$ref specs
# (the majority — account, symbol, exchange, side, template_* fields, every
# other action's params_schema) pass through completely untouched.
# ───────────────────────────────────────────────────────────────────────────

_ORDER_FIELDS_YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "grammars" / "order_fields.yaml"


def _load_order_fields_catalog() -> dict:
    with open(_ORDER_FIELDS_YAML_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_ORDER_FIELDS_CATALOG: dict = _load_order_fields_catalog()
_ORDER_FIELDS: dict = _ORDER_FIELDS_CATALOG.get("fields", {})

# Canonical key emission order for a resolved params_schema field spec.
# Matches the order every hand-typed field already used pre-Phase-2
# (type, [enum], required, [token_ref_ok], [default], [description]) so
# resolving a $ref never reorders — and therefore never changes the JSON
# serialization of — a field that happens to carry the exact same keys it
# always did.
_PARAM_SPEC_KEY_ORDER = ("type", "enum", "required", "token_ref_ok", "default", "description")


def _resolve_param_spec(spec):
    """Resolve one params_schema field spec. Specs without `$ref` pass
    through unchanged (identity, not a copy) — zero behavior change for
    every field that doesn't opt into the shared catalog."""
    if not isinstance(spec, dict) or "$ref" not in spec:
        return spec
    ref_key = spec["$ref"]
    base = _ORDER_FIELDS.get(ref_key)
    if base is None:
        raise KeyError(
            f"agent_grammar.yaml params_schema references order_fields.yaml "
            f"field {ref_key!r}, which does not exist"
        )
    merged = dict(base)
    for k, v in spec.items():
        if k == "$ref":
            continue
        merged[k] = v  # local override/addition wins (required/default/token_ref_ok/description)
    ordered = {k: merged[k] for k in _PARAM_SPEC_KEY_ORDER if k in merged}
    for k, v in merged.items():  # future-proofing: any key outside the canonical set still survives
        if k not in ordered:
            ordered[k] = v
    return ordered


def _resolve_params_schema(params_schema):
    if not params_schema:
        return params_schema
    return {name: _resolve_param_spec(field_spec) for name, field_spec in params_schema.items()}


def _build_system_tokens(catalog: dict) -> list:
    """Reconstruct SYSTEM_TOKENS from the raw loaded catalog, resolving any
    `$ref` markers inside each action token's params_schema along the way."""
    tokens = []
    for spec in catalog["system_tokens"]:
        if spec.get("params_schema"):
            spec = dict(spec)
            spec["params_schema"] = _resolve_params_schema(spec["params_schema"])
        tokens.append(spec)
    return tokens


# ═══════════════════════════════════════════════════════════════════════════
#  CONDITION GRAMMAR — resolvers
# ═══════════════════════════════════════════════════════════════════════════
#
# Metric resolvers take (ctx, row) — ctx is the evaluation context (live
# snapshot + rate history + now + baseline state); row is the selected row
# from a scope selector. They return a float (or None when not computable,
# which means "skip this leaf").
#
# Scope selectors take (ctx) and return a list of row dicts — one leaf then
# iterates and combines results per the scope's semantics (TOTAL yields one
# row; any_acct yields all non-TOTAL rows and the leaf is OR-combined).
# ───────────────────────────────────────────────────────────────────────────

# ── Missing-vs-zero convention (fix #3) ───────────────────────────────
#
# `float(row.get(col, 0) or 0)` collapses THREE distinct states — column
# genuinely absent, column present but None/NaN, and a real reported 0 —
# into the SAME value (0.0). For raw per-broker funds/margin columns that
# convention is actively dangerous: Dhan/Groww map some fields
# inconsistently (see `dhan.py`/`groww.py` `_dhan_num_or_none` /
# `_gf_or_none`), so an unmapped column silently looked exactly like a
# real zero balance and fired `loss-margin-low` on nothing but missing
# data (61 fires / 60 days, prod audit).
#
# Applied to the raw-broker-column funds metrics below (cash, sod_cash,
# avail_margin, used_margin, collateral) — every one of these reads a
# single column straight off `df_margins`, which is exactly where a
# broker can genuinely omit a field. NOT applied to computed
# positions/holdings aggregate columns (pnl, day_val, day_pct, inv_val,
# cur_val) — those are always populated by the background summary
# builder (groupby + `.fillna(0)`), so a real 0 there is a real 0, never
# an "unmapped field" — see the self-audit note in agent_engine.py's
# `_ae_funds_pnl` docstring for the full boundary rationale.
def _num_or_none(v) -> float | None:
    """Coerce to float; return None (not 0.0) when v is None, NaN, or
    non-numeric. A genuine 0.0 passes through unchanged."""
    if v is None:
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    import math
    return None if math.isnan(fv) else fv


# The following 12 metric resolvers were migrated verbatim into
# backend/api/algo/grammar_tokens/ (Wave 1, batch 1 of the DSL-token
# unification plan) — pnl, pnl_pct, day_val, day_pct, inv_val, cur_val,
# cash, sod_cash, avail_margin, used_margin, collateral, ltp. Catalog
# resolver paths updated in agent_grammar.yaml; see grammar_tokens/
# for the real function bodies.

# Rate-of-change metrics. They use the rolling history the engine maintains
# per (section, scope). Section is inferred from the scope token.
#
# Scope-aware key (underlying-divergence feature): a row with an
# `underlying` field and NO real `account` came from `positions.
# any_underlying` — key off the namespaced `('positions_underlying',
# underlying)` bucket `agent_engine._update_underlying_pnl_history()`
# writes, instead of the per-account/TOTAL `('positions', account)`
# bucket. Every other scope (any_acct/total/worst_acct/worst_symbol) still
# carries a real `account`, so this is purely additive — zero behaviour
# change for every existing caller of these two tokens.
def _pnl_rate_key(row):
    account = row.get('account')
    if account is None:
        underlying = row.get('underlying')
        if underlying:
            return ('positions_underlying', underlying)
    return ('positions', account)

# pnl_rate_abs, pnl_rate_pct, day_rate_abs, day_rate_pct migrated to
# grammar_tokens/ (Wave 1, batch 2). `_pnl_rate_key` stays here — shared
# helper read by both pnl_rate_abs and pnl_rate_pct.


# Phase 24 — Rolling-window statistical metrics. Same pnl_history bucket
# the rate metrics read from; different reducer. Section comes from the
# scope token (positions.* → 'positions', holdings.* → 'holdings'),
# field_idx 1 = pnl ₹, 2 = pnl %.

def _w_key_pos(row):  return ('positions', row.get('account'))
def _w_key_hold(row): return ('holdings',  row.get('account'))

# Phase 26 (Sprint 2) — parameterized function-call tokens, e.g.
# `mean_pnl(30)`. Each factory is the resolver bound to the BASE token
# name (registered in agent_grammar.yaml with a `params_schema`);
# GrammarRegistry calls it once per distinct literal call-string
# (cached forever after) and gets back the (ctx, row) -> value
# callable a metric resolver normally is. The pre-existing fixed-window
# tokens are kept forever, unchanged, as one-line bindings to the same
# factories — no deprecation, no migration, zero behavior change for
# existing agents.
#
# All 7 factories (mean_pnl, mean_day, max_drawdown_pnl,
# max_drawdown_day, max_drawdown_pnl_pct, stdev_pnl, range_pnl) AND
# their fixed-window bindings migrated to grammar_tokens/ (Wave 1,
# batch 2) — one file per factory, e.g. grammar_tokens/metric_mean_pnl.py
# holds `_metric_factory_mean_pnl` + `_metric_mean_pnl_30m` +
# `_metric_mean_pnl_1h`. The bindings moved WITH their factory (not kept
# here importing the factory back) to keep the grammar.py <->
# grammar_tokens/ import direction strictly one-way.


# ── Expiry-aware metrics + scopes (Item 1 / Phase 25) ────────────────
#
# Lets an agent reason about which positions are expiring today and
# (when spot is known) whether they're in/near the money. The resolvers
# parse the tradingsymbol on every call — light enough to do per-tick
# without caching since parsing is regex + dict lookups, not I/O.
#
# Spot prices are looked up via `ctx.spot_prices` — a dict[underlying:
# str, ltp: float] populated by _build_context once per tick. When the
# spot for an option's underlying isn't in the dict (broker outage,
# unrecognised symbol, dry-run with no spot fetch), the ITM/NTM
# resolvers return None and the leaf is skipped — same graceful path
# the rate metrics already use when pnl_history is empty.

def _parsed_or_none(symbol):
    """Cached-on-row wrapper around parse_tradingsymbol so repeated
    resolver calls on the same row only re-parse once per evaluation."""
    try:
        from backend.api.algo.derivatives import parse_tradingsymbol
        return parse_tradingsymbol(symbol)
    except Exception:
        return None


def underlying_of_symbol(symbol: str) -> Optional[str]:
    """Return the underlying root (e.g. 'NIFTY', 'CRUDEOIL') for an F&O
    tradingsymbol, or None for a cash-equity / unparseable symbol.

    Sprint G, Task 2 — generalizes the per-row underlying derivation the
    MCX collar detector (`_scope_positions_expiring_today_mcx_unhedged`
    below) already does inline (`parsed.get('root')`) into a single,
    reusable function, so `background.py`'s position-row builder and any
    future caller share the EXACT same symbol-decomposition logic rather
    than re-implementing it. `_parsed_or_none` already wraps
    `derivatives.parse_tradingsymbol` with the same try/except-to-None
    contract every other resolver in this module uses for an
    unparseable symbol — reused verbatim, not duplicated.
    """
    parsed = _parsed_or_none(symbol or '')
    if not parsed:
        return None
    return parsed.get('root') or None


# `_metric_days_until_expiry` migrated to grammar_tokens/ (Wave 1, batch 3).


def _option_moneyness(ctx, row):
    """Return (kind, intrinsic_pct) for an option row — `kind` is 'CE'
    or 'PE', `intrinsic_pct` is (spot − strike)/spot for CE or
    (strike − spot)/spot for PE. Positive ⇒ ITM, negative ⇒ OTM.
    Returns (None, None) when the row isn't an option or spot is
    unavailable."""
    sym = row.get('tradingsymbol') or ''
    parsed = _parsed_or_none(sym)
    if not parsed or parsed.get('kind') != 'opt':
        return (None, None)
    spots = getattr(ctx, 'spot_prices', None) or {}
    spot = spots.get(parsed.get('root') or '')
    if spot is None or spot <= 0:
        return (None, None)
    strike = parsed.get('strike')
    if strike is None:
        return (None, None)
    if parsed['opt_type'] == 'CE':
        return ('CE', (spot - strike) / spot)
    if parsed['opt_type'] == 'PE':
        return ('PE', (strike - spot) / spot)
    return (None, None)


# `_metric_is_itm`, `_metric_is_ntm`, `_metric_is_future` migrated to
# grammar_tokens/ (Wave 1, batch 3). `_option_moneyness` stays here —
# shared helper read by both is_itm and is_ntm.


def _scope_positions_expiring_today(ctx):
    """Per-symbol position rows where the symbol parses to an F&O
    contract expiring TODAY (or already past expiry — days_to_expiry
    floors at 0). Cash-equity rows skip (no parseable expiry).

    Reads from ctx.position_rows — the raw per-symbol list the engine
    fetched. sum_positions is the per-account aggregate and carries
    no per-symbol expiry, so it's the wrong shape for this filter.

    Why ≤ 1.5 day floor rather than === 0: an option that "expires
    today" at the engine's 09:00 tick has ~6.5 hours of life left;
    days_to_expiry yields ~0.27. The 1.5 ceiling also catches "T-1
    intraday warning" if a future operator preference wants that —
    keeping a single scope rather than a wider scope-token set.
    """
    rows_src = getattr(ctx, 'position_rows', None) or []
    out = []
    for r in rows_src:
        sym = r.get('tradingsymbol') or ''
        parsed = _parsed_or_none(sym)
        if not parsed or not parsed.get('expiry'):
            continue
        try:
            from backend.api.algo.derivatives import days_to_expiry
            close_time = (23, 30) if (r.get('exchange') or '').upper() == 'MCX' else (15, 30)
            d = float(days_to_expiry(parsed['expiry'], ref=ctx.now, close_time=close_time))
        except Exception:
            continue
        if d <= 1.5:
            out.append(r)
    return out


def _scope_positions_expiring_today_nfo(ctx):
    """Subset of positions.expiring_today restricted to NFO (equity
    F&O). Used by the equity-only auto-close agent which fires at
    T-30min before the 15:30 IST equity close. Kite's `exchange`
    field is the source of truth — NSE for cash equity, NFO for
    equity F&O contracts. Returns NFO rows only.
    """
    rows = _scope_positions_expiring_today(ctx)
    return [r for r in rows if (r.get('exchange') or '').upper() == 'NFO']


def _scope_positions_expiring_today_mcx_unhedged(ctx):
    """Subset of positions.expiring_today restricted to MCX
    contracts whose CE/PE net qty across the underlying does NOT
    balance — i.e. unhedged legs that will face cash settlement.

    Mirrors the legacy ExpiryEngine grouping: group MCX expiring
    rows by `(underlying, expiry)`; if the sum of CE quantities +
    sum of PE quantities is 0, the pair is perfectly hedged and the
    broker nets them against each other (no close needed). Anything
    else is unhedged and returned.

    Why "underlying + expiry": a long-CE-short-PE collar on the
    same strike + expiry is the typical hedge structure; the net
    qty test catches both single-strike hedges and asymmetric
    multi-strike combos (e.g. long 2 CE, short 2 PE → net 0).
    """
    rows = _scope_positions_expiring_today(ctx)
    mcx = [r for r in rows if (r.get('exchange') or '').upper() == 'MCX']
    if not mcx:
        return []
    groups: dict = {}
    for r in mcx:
        parsed = _parsed_or_none(r.get('tradingsymbol') or '')
        if not parsed:
            continue
        key = f"{parsed.get('root', '')}_{parsed.get('expiry', '')}"
        groups.setdefault(key, []).append((r, parsed))
    out = []
    for entries in groups.values():
        ce_qty = sum(int(r.get('quantity', 0) or 0)
                     for r, p in entries if p.get('opt_type') == 'CE')
        pe_qty = sum(int(r.get('quantity', 0) or 0)
                     for r, p in entries if p.get('opt_type') == 'PE')
        if ce_qty + pe_qty == 0:
            # Perfectly hedged group — broker nets settlement; skip.
            continue
        for r, _p in entries:
            out.append(r)
    return out


# Time metrics — useful for agents that should only fire in specific windows.
def _metric_minutes_since_open(ctx, row):
    return ctx.minutes_since_open()

def _metric_minutes_until_close(ctx, row):
    return ctx.minutes_until_close()


# ── Market-cycle event tokens ─────────────────────────────────────────────
#
# First real consumers of `agent_engine._ae_segment_flags()`'s computed
# dict — see that function and `Context.segment_flags` (agent_evaluator.py)
# for how the values get here. `ctx.segment_flags` is `{}` on any caller
# that doesn't populate it (log-feed-driven event agents via
# `event_agents.py:Context(log_records=[rec])`, or a hand-built test
# Context) — every resolver below degrades to `None` ("missing", not a
# false 0/closed reading) rather than raising.

def _metric_is_mcx_open(ctx, row):
    """1.0/0.0 from `ctx.segment_flags['mcx_open']`. None when unset."""
    v = (ctx.segment_flags or {}).get('mcx_open')
    if v is None:
        return None
    return 1.0 if v else 0.0

def _metric_is_nse_open(ctx, row):
    """1.0/0.0 from `ctx.segment_flags['nse_open']`. None when unset."""
    v = (ctx.segment_flags or {}).get('nse_open')
    if v is None:
        return None
    return 1.0 if v else 0.0

def _metric_minutes_since_mcx_open(ctx, row):
    """Minutes since MCX's own session opened today. None while MCX is
    closed — `_ae_segment_flags` coerces its `minutes_since_mcx_open`
    field to 0 while closed (a diagnostic convention for that function's
    other callers), which would otherwise read as "just opened" all
    night; gate on the open flag here instead of trusting the raw 0."""
    flags = ctx.segment_flags or {}
    if not flags.get('mcx_open'):
        return None
    return flags.get('minutes_since_mcx_open')

def _metric_minutes_since_nse_open(ctx, row):
    """Minutes since NSE's own session opened today. None while NSE is
    closed — see `_metric_minutes_since_mcx_open`'s docstring for why."""
    flags = ctx.segment_flags or {}
    if not flags.get('nse_open'):
        return None
    return flags.get('minutes_since_nse_open')

def _metric_minutes_until_mcx_close(ctx, row):
    """Minutes until MCX's own session closes today. None while MCX is
    closed (nothing to count down to)."""
    flags = ctx.segment_flags or {}
    if not flags.get('mcx_open'):
        return None
    return flags.get('minutes_until_mcx_close')

def _metric_minutes_until_nse_close(ctx, row):
    """Minutes until NSE's own session closes today. None while NSE is
    closed (nothing to count down to)."""
    flags = ctx.segment_flags or {}
    if not flags.get('nse_open'):
        return None
    return flags.get('minutes_until_nse_close')

def _metric_minutes_until_mcx_open(ctx, row):
    """Minutes until MCX's own session opens today — 0 while already
    open; None after today's close or on a day with no session at all
    (see `_ae_segment_flags`'s own computation)."""
    return (ctx.segment_flags or {}).get('minutes_until_mcx_open')

def _metric_minutes_until_nse_open(ctx, row):
    """Minutes until NSE's own session opens today — 0 while already
    open; None after today's close or on a day with no session at all
    (see `_ae_segment_flags`'s own computation)."""
    return (ctx.segment_flags or {}).get('minutes_until_nse_open')

def _metric_is_past_holiday_refresh(ctx, row):
    """1.0 once today's `holiday_refresh_time` (backend_config.yaml,
    default 05:30 IST) clock boundary has passed; 0.0 before it. Face-
    value clock check only — does not confirm the refresh job actually
    ran or succeeded. None when ctx.now is unavailable."""
    now = ctx.now
    if now is None:
        return None
    from backend.shared.helpers.utils import config as _cfg
    target_str = str(_cfg.get("holiday_refresh_time", "05:30") or "05:30")
    try:
        hh, mm = (int(x) for x in target_str.split(":", 1))
    except Exception:
        hh, mm = 5, 30
    target_t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return 1.0 if now >= target_t else 0.0

def _metric_is_past_window_open(ctx, row):
    """1.0 from today's 08:00 IST broker-connectivity window boundary
    through the rest of the same trading session (including the 23:31-
    23:59 IST tail, before the closed overnight window actually starts
    at 23:31); 0.0 only during 00:00-07:59 IST, before today's own 08:00
    boundary has passed. This is a "has today's session begun" flag, not
    a live "is the window currently open" check — it does NOT flip back
    to 0.0 at 23:31 like `_segment_is_open()` would. Delegates to the
    same trading-session-date convention `expiry_freeze.session_date_of()`
    already uses (00:00-07:59 IST attributed to the PRIOR session) rather
    than re-deriving the boundary — a bare `now.time() >= 08:00` check
    would be wrong at e.g. 23:50 IST (still today's session, correctly
    "past window open", but also "past 08:00" only by coincidence of
    being later in the same calendar day — the session-date compare is
    the one that generalises correctly across midnight). None when
    ctx.now is unavailable."""
    now = ctx.now
    if now is None:
        return None
    from backend.api.algo.expiry_freeze import session_date_of
    return 1.0 if session_date_of(now) == now.date() else 0.0


# ── Scope selectors — "which rows does this leaf evaluate over?" ─────────

def _scope_holdings_total(ctx):
    df = ctx.sum_holdings
    if df is None or df.empty:
        return []
    mask = df['account'].astype(str) == 'TOTAL'
    return [r.to_dict() for _, r in df[mask].iterrows()]

def _scope_holdings_any_acct(ctx):
    df = ctx.sum_holdings
    if df is None or df.empty:
        return []
    mask = df['account'].astype(str) != 'TOTAL'
    return [r.to_dict() for _, r in df[mask].iterrows()]

_PARTIAL_OUTAGE_LOG_INTERVAL_MIN = 15
_last_partial_outage_log: "dict[str, object]" = {"ts": None, "accounts": None}


def _log_partial_outage_suppression(accounts: list) -> None:
    """Rate-limited warning when `_scope_positions_total` suppresses
    evaluation due to a partial outage. Without this, a single account
    stuck failing indefinitely (e.g. an expired Dhan/Groww token) silently
    disables EVERY positions.total loss/ROC alert for as long as it lasts
    — with nothing logged anywhere to surface that degradation. Logs once
    per `_PARTIAL_OUTAGE_LOG_INTERVAL_MIN` (not every tick — this scope
    resolver runs on every agent-engine cycle, every few seconds under
    the simulator and every 5 min live) UNLESS the failed-account set
    itself changes, in which case it logs immediately regardless of the
    interval so a NEW failure is never masked by a still-cooling-down
    rate limit from an older one."""
    import time as _time
    now_ts = _time.monotonic()
    last_ts = _last_partial_outage_log["ts"]
    last_accounts = _last_partial_outage_log["accounts"]
    interval_s = _PARTIAL_OUTAGE_LOG_INTERVAL_MIN * 60
    accounts_changed = last_accounts != accounts
    if not accounts_changed and last_ts is not None and (now_ts - last_ts) < interval_s:
        return
    _last_partial_outage_log["ts"] = now_ts
    _last_partial_outage_log["accounts"] = accounts
    logger.warning(
        f"[PARTIAL-OUTAGE] positions.total scope suppressed — "
        f"accts={accounts} — every loss/ROC agent scoped to positions.total "
        f"is silently not evaluating this tick"
    )


def _scope_positions_total(ctx):
    df = ctx.sum_positions
    if df is None or df.empty:
        return []
    # Partial-outage guard (2026-09 alerts audit item 5): when
    # `background._fetch_positions_direct` detected that some (not all)
    # accounts failed to fetch this tick, `df`'s TOTAL row silently omits
    # the failed account(s)' contribution — pd.concat just drops what
    # never arrived. Returning [] here means the evaluator records NO
    # observation for any positions.total leaf this tick (fix #5's
    # "absent from observations = untouched, never treated as
    # recovered" contract), rather than firing/clearing latches off a
    # P&L total that's silently missing money.
    accts = (getattr(df, 'attrs', {}) or {}).get('partial_outage')
    if accts:
        _log_partial_outage_suppression(accts)
        return []
    mask = df['account'].astype(str) == 'TOTAL'
    return [r.to_dict() for _, r in df[mask].iterrows()]

def _scope_positions_any_acct(ctx):
    df = ctx.sum_positions
    if df is None or df.empty:
        return []
    mask = df['account'].astype(str) != 'TOTAL'
    return [r.to_dict() for _, r in df[mask].iterrows()]

def _scope_funds_total(ctx):
    df = ctx.df_margins
    if df is None or df.empty:
        return []
    mask = df['account'].astype(str) == 'TOTAL'
    return [r.to_dict() for _, r in df[mask].iterrows()]

def _scope_funds_any_acct(ctx):
    df = ctx.df_margins
    if df is None or df.empty:
        return []
    mask = df['account'].astype(str) != 'TOTAL'
    return [r.to_dict() for _, r in df[mask].iterrows()]


# ── Underlying-scoped theoretical-vs-market loss divergence ──────────────
#
# "Gate a rate-of-change loss alert on how far real market P&L has diverged
# from the Greeks/IV-implied move" (per-underlying, not per-account — a
# book-wide ₹ threshold means something different for a single-lot CRUDEOIL
# collar than for a 50-lot NIFTY straddle).
#
# Scope (`positions.any_underlying`) and the three metrics below all read
# `ctx.position_rows` directly, filtered by the SAME `row['underlying']`
# grouping key — deliberately NOT `pnl_math.breakdown_positions_by_
# underlying()` (that helper aggregates across every account unconditionally,
# truncates to top_n=5, and has no 'accounts' key — wrong shape for a
# condition-tree scope that must stay in lockstep with these metrics'
# own per-leg walk). Using one grouping key for both guarantees scope
# membership and metric computation never silently disagree (the same
# defect class Fix A/B below close for account-vs-underlying latching and
# cross-row AND-joins).
#
# Baseline-reprice design (why this is NOT `multileg_greeks`/`expected_
# value`, and why IV is calibrated ONCE, not every tick):
#
#   `expected_value()` integrates a payoff curve against a risk-neutral
#   lognormal density to get E[payoff AT EXPIRY] — a forward-looking
#   expectation, not "what should today's P&L be given the realized spot
#   move so far". Calibrating IV from the SAME tick's own LTP and then
#   repricing at that IV reproduces the LTP by construction (circular —
#   divergence would always be ≈0 and the gate could never open).
#
#   Instead: `_update_underlying_theo_baseline()` (agent_engine.py, called
#   once per tick alongside `_update_pnl_history`) calibrates σ for each
#   held leg ONCE — the first tick that leg is seen this session — and
#   freezes it in `alert_state['underlying_theo_baseline']`. Every
#   subsequent tick, `_underlying_theoretical_loss()` below re-prices each
#   leg at that FROZEN σ against the CURRENT spot and CURRENT time-to-
#   expiry (so theta decay is live, only vol is frozen) and diffs against
#   the leg's own LTP at baseline time. Summed per leg, this is "what the
#   book's P&L would be if nothing but spot/time had moved since the
#   baseline" — the real quantity "theoretical loss" means here.

def _scope_positions_any_underlying(ctx):
    """One row per distinct F&O underlying currently held, grouped from
    `ctx.position_rows`. Cash-equity / unparseable rows (no `underlying`)
    are skipped — same skip rule `_scope_positions_expiring_today` already
    uses for the same reason. Leaf is OR-combined per underlying.

    `accounts` / `pnl` / `day_change_val` are cheap roll-ups useful for
    alert-message enrichment — the metric resolvers below don't read them
    off this summary row; they re-walk `ctx.position_rows` themselves for
    full per-leg detail (qty, strike, expiry, ...) this summary row
    doesn't carry.
    """
    rows = getattr(ctx, 'position_rows', None) or []
    groups: dict = {}
    for r in rows:
        u = r.get('underlying')
        if not u:
            continue
        groups.setdefault(u, []).append(r)
    out = []
    for u, group in groups.items():
        out.append({
            'underlying': u,
            'accounts': sorted({r.get('account') for r in group if r.get('account')}),
            'pnl': sum(float(r.get('pnl', 0) or 0) for r in group),
            'day_change_val': sum(float(r.get('day_change_val', 0) or 0) for r in group),
        })
    return out


# `_metric_underlying_market_loss` migrated to grammar_tokens/ (Wave 1,
# batch 3).


def _theo_reprice_fut_leg(b: dict, S_now: float) -> "float | None":
    """Linear futures-leg theoretical P&L: qty × (S_now − S_base). None
    on a malformed baseline entry."""
    try:
        qty = int(b.get('qty', 0) or 0)
        S_base = float(b['S_base'])
    except (TypeError, ValueError, KeyError):
        return None
    return qty * (S_now - S_base)


def _theo_reprice_opt_leg(b: dict, S_now: float, now) -> "float | None":
    """Option-leg theoretical P&L at the leg's FROZEN σ (`sigma_base`)
    against the CURRENT spot and CURRENT time-to-expiry. None on any
    malformed baseline entry or numeric reprice failure — extracted from
    `_underlying_theoretical_loss` to keep it under the radon complexity
    gate."""
    from backend.api.algo.derivatives import black_scholes, black_76, days_to_expiry, DEFAULT_RISK_FREE
    try:
        qty = int(b.get('qty', 0) or 0)
        K = float(b['K'])
        sigma_base = float(b['sigma_base'])
        ltp_base = float(b['ltp_base'])
    except (TypeError, ValueError, KeyError):
        return None
    opt_type = b.get('opt_type')
    if opt_type not in ('CE', 'PE'):
        return None
    is_mcx = bool(b.get('is_mcx'))
    close_time = (23, 30) if is_mcx else (15, 30)
    try:
        T_now = days_to_expiry(b.get('expiry'), ref=now, close_time=close_time) / 365.0
        if is_mcx:
            price_now = black_76(S_now, K, T_now, DEFAULT_RISK_FREE, sigma_base, opt_type)
        else:
            price_now = black_scholes(S_now, K, T_now, DEFAULT_RISK_FREE, sigma_base, opt_type)
    except Exception:
        return None
    return qty * (price_now - ltp_base)


def _theo_reprice_leg(b: dict, S_now: float, now) -> "float | None":
    """Dispatch ONE baselined leg to its fut/opt reprice helper. None on
    any failure — the caller (`_underlying_theoretical_loss`) treats a
    single-leg None as "whole underlying unavailable this tick", never a
    silently-dropped leg."""
    if b.get('kind') == 'fut':
        return _theo_reprice_fut_leg(b, S_now)
    return _theo_reprice_opt_leg(b, S_now, now)


def _underlying_theoretical_loss(ctx, underlying):
    """Baseline-reprice theoretical P&L for every currently-held leg of
    `underlying`, summed. σ is whatever `_update_underlying_theo_baseline()`
    (agent_engine.py) calibrated once this session for each leg — frozen;
    only spot (`ctx.spot_prices`) and time-to-expiry move tick to tick.

    Returns None (never a partial/understated sum) when:
      - `underlying` is falsy, or no position rows exist for it, or
      - no live spot price is available for it, or
      - ANY one leg has no baseline entry yet (a brand-new leg gets ONE
        captured the NEXT tick, not retro-filled this tick — see
        `_update_underlying_theo_baseline`'s own docstring), or
      - any one leg's reprice fails numerically (`_theo_reprice_leg`).
    A partial failure on one leg would UNDERSTATE the true theoretical
    loss if silently dropped, so the whole underlying is reported
    unavailable for the tick instead — same "skip this tick entirely"
    contract `_scope_positions_total`'s partial-outage guard already uses.
    """
    if not underlying:
        return None
    rows = [r for r in (getattr(ctx, 'position_rows', None) or [])
            if r.get('underlying') == underlying]
    if not rows:
        return None
    spot_prices = getattr(ctx, 'spot_prices', None) or {}
    try:
        S_now = float(spot_prices.get(underlying))
    except (TypeError, ValueError):
        return None
    if S_now <= 0:
        return None
    baseline = ((getattr(ctx, 'alert_state', None) or {}).get('underlying_theo_baseline') or {})
    total = 0.0
    for row in rows:
        b = baseline.get(row.get('tradingsymbol') or '')
        if b is None:
            return None
        leg_pnl = _theo_reprice_leg(b, S_now, ctx.now)
        if leg_pnl is None:
            return None
        total += leg_pnl
    return total


# `_metric_underlying_theoretical_loss` and `_metric_underlying_loss_
# divergence` migrated to grammar_tokens/ (Wave 1, batch 3).
# `_underlying_theoretical_loss` (and its `_theo_reprice_*` helpers
# above) stay here — shared, used by both migrated metrics.


# ── "Worst case" scope selectors — collapse N per-account agents to 1 ─────
#
# Returns the SINGLE row with the largest drawdown in the chosen dimension.
# Pairs naturally with the existing day_pct / pnl_pct / day_rate_abs metrics
# — the leaf evaluator OR-combines across returned rows, so a single-row
# list means "fire if THIS one row breaches".
#
# Operator workflow this replaces:
#   Before — five per-account agents at threshold -3% (one per account),
#            all of which fire simultaneously on a market dump.
#   After  — ONE agent with scope=holdings.worst_acct, threshold -3%.
#            Fires once with the worst-affected account's row attached.

def _row_with_min(rows: list, key: str) -> list:
    """Helper: pick the row whose `key` is the most-negative (or smallest)
    value. Returns a single-row list, or empty if no row has a numeric
    value at `key`. None/NaN values are skipped."""
    import math
    candidates = []
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(fv):
            continue
        candidates.append((fv, r))
    if not candidates:
        return []
    candidates.sort(key=lambda t: t[0])
    return [candidates[0][1]]


def _scope_holdings_worst_acct(ctx):
    """Single per-account holdings row with the worst day_change_percentage.

    Fix #11 — was keying on 'day_pct', a column name the holdings summary
    frame never carries (it carries 'day_change_percentage' — see
    background._bg_holdings_add_pct), so this scope always returned []."""
    rows = _scope_holdings_any_acct(ctx)
    return _row_with_min(rows, 'day_change_percentage')

def _scope_holdings_worst_symbol(ctx):
    """Single per-symbol holdings row with the worst day_change_percentage.
    Note: relies on the engine context populating per-symbol detail. When
    the live pipeline only carries per-account aggregates (current
    default), this falls back to the same row as worst_acct — operators
    get an honest drawdown signal either way."""
    df = getattr(ctx, 'holdings_rows', None)
    if df is None or (hasattr(df, 'empty') and df.empty):
        return _scope_holdings_worst_acct(ctx)
    rows = [r.to_dict() for _, r in df.iterrows()] if hasattr(df, 'iterrows') else list(df)
    return _row_with_min(rows, 'day_change_percentage')

def _scope_positions_worst_acct(ctx):
    """Single per-account positions row with the worst day_change_percentage.

    Fix #11 — was keying on 'pnl_pct', a column the positions summary
    frame never carries at all (only 'day_change_val'/'day_change_percentage'
    — see background._fetch_positions_direct / _rebuild_positions_summary),
    so this scope always returned []."""
    rows = _scope_positions_any_acct(ctx)
    return _row_with_min(rows, 'day_change_percentage')

def _scope_positions_worst_symbol(ctx):
    """Single per-symbol positions row with the worst pnl.

    Fix #11 — was reading `ctx.positions_rows`, an attribute that does not
    exist on Context (the real field is `position_rows`, singular), so
    this scope always fell through to the worst_acct fallback."""
    rows = getattr(ctx, 'position_rows', None) or []
    if not rows:
        return _scope_positions_worst_acct(ctx)
    return _row_with_min(rows, 'pnl')


# ── Watchlist scopes ─────────────────────────────────────────────────────
# Each scope returns rows from ctx.watchlist_rows. The `account` slot on
# each row carries the watchlist NAME so we filter by list name.

def _scope_watchlist_all(ctx):
    """Every row across every watchlist the user owns."""
    return list(getattr(ctx, 'watchlist_rows', []) or [])

def _scope_watchlist_default(ctx):
    """Rows in the user's 'Default' watchlist."""
    rows = getattr(ctx, 'watchlist_rows', []) or []
    return [r for r in rows if str(r.get('account', '')) == 'Default']

def _scope_watchlist_markets(ctx):
    """Rows in the auto-seeded 'Markets' watchlist (indices + commodities)."""
    rows = getattr(ctx, 'watchlist_rows', []) or []
    return [r for r in rows if str(r.get('account', '')) == 'Markets']


# ── Watchlist-specific metrics ───────────────────────────────────────────
# `_metric_ltp` migrated to grammar_tokens/metric_ltp.py (Wave 1, batch 1).


# ── Orders scope (Sprint 21) ─────────────────────────────────────────────
# Reads ctx.orders_rows — currently OPEN/active `AlgoOrder` rows (mode=
# 'live', status not in ALGO_ORDER_TERMINAL_STATUSES), refreshed once per
# `_task_performance` cycle by `background._perf_fetch_orders_rows`. NOT a
# live broker call — same cadence/cost profile as positions/holdings/funds.
# Account-rooted (same `account:` qualifier mechanism as positions/
# holdings/funds), never `exchange:`/`symbol:` — those are instrument-
# rooted qualifiers, reserved for `quote.*` below.
#
# No `orders.total` aggregate — unlike P&L, there is no single meaningful
# scalar to sum across open orders of mixed BUY/SELL direction and
# instrument, so this scope family only ever has a per-row variant.

def _scope_orders_any_acct(ctx):
    """Every currently open/active order, across every account."""
    return list(getattr(ctx, 'orders_rows', []) or [])


# ── Quote scope (Sprint 21) ───────────────────────────────────────────────
# Reads ctx.quote_rows — pre-tracked ad-hoc market-data rows (symbol +
# exchange + ltp/bid/ask/volume) for every symbol referenced by at least
# one active agent's `quote.*` leaf, refreshed once per `_task_performance`
# cycle by `background._perf_build_quote_rows` (reuses `routes/quote.py`'s
# `_fetch_ltp` directly — never a live per-condition broker round-trip).
# Instrument-rooted (qualified by `symbol:` — REQUIRED, see
# `agent_evaluator.validate()`'s quote.* guard — with `exchange:` as an
# OPTIONAL secondary qualifier for a symbol ambiguous across exchanges);
# never `account:` — market data belongs to no account. Mirrors
# `watchlist.*`'s existing per-row-only pattern exactly — no aggregate.

def _scope_quote_tracked(ctx):
    """Every pre-tracked ad-hoc market-data row."""
    return list(getattr(ctx, 'quote_rows', []) or [])


# ── Symbols scope (Sprint G) ──────────────────────────────────────────────
# Reads ctx.symbols_rows — one dict per row, UNIONING four previously
# separate concepts (watchlist / pinned / gainers / losers) behind a single
# `kind` qualifier, per the operator's "symbols.* groups" design. Populated
# once per `_task_performance` cycle by `background._perf_build_symbols_rows`
# (gated on whether any active agent actually references `symbols.*`, same
# cost profile as `watchlist.*`/`quote.*` — never a live per-condition
# broker round-trip). Instrument-rooted like `watchlist.*`/`quote.*` — no
# account dimension. REQUIRES a non-wildcard `scope_filter.kind` (enforced
# in `agent_evaluator.validate()`, mirroring the existing `quote.*`
# required-symbol guard) since the four kinds carry materially different
# row populations and mixing them unfiltered under one OR-combined leaf
# would be meaningless.
def _scope_symbols_all(ctx):
    """Every row across every `symbols.*` kind (watchlist/pinned/gainers/
    losers) — always narrowed by a `scope_filter.kind` qualifier in
    practice; see module-level note above."""
    return list(getattr(ctx, 'symbols_rows', []) or [])


# ═══════════════════════════════════════════════════════════════════════════
#  EXPOSED-COLUMN REGISTRY (Sprint 15 — generic `scope.attribute` dot-access)
# ═══════════════════════════════════════════════════════════════════════════
#
# Every scope selector above returns a list of plain dicts — a pandas row
# (`.to_dict()`) or a hand-built dict (watchlist rows) — carrying every REAL
# column that row happens to have, not just the handful that already have a
# hand-registered metric token (`pnl`, `day_pct`, `cash`, ...). Sprint 15
# makes those real columns addressable from `WHEN` via `scope.attribute`
# dot-access (e.g. `positions.total.pnl <= -50000`, equivalent to today's
# `pnl@positions.total <= -50000`, but works for ANY column already on the
# row — `positions.expiring_today.symbol`, `funds.total.collateral`,
# etc. — with no new backend resolver needed per column).
#
# This dict is a PURE ADDITIVE ALIAS LAYER — it never renames, touches, or
# reads any real DB/DataFrame column differently than every existing caller
# already does. Each entry is `{type, from_column, label}`:
#   type         — 'number' | 'string' (matches compileLeaf's valueType
#                  vocabulary; no boolean-typed column exists in the real
#                  data today).
#   from_column  — the REAL, possibly messy DataFrame/dict column name
#                  (e.g. 'avail cash', 'day_change_pct_margin'). Never
#                  changed anywhere else in the codebase.
#   label        — operator-facing display text. Matches an existing grid
#                  headerName for the SAME real concept where one exists
#                  (see per-entry comments), else a sensible title-case.
#
# Naming rule: `exposed_name` (the dict KEY) is always a clean identifier
# (`^[A-Za-z_][A-Za-z0-9_]*$` — enforced by
# backend/tests/test_grammar_exposed_columns.py) and reuses an EXISTING
# metric token's name whenever that metric is a PURE passthrough of the
# same column with no extra math (`pnl`, `day_val`, `inv_val`, `cur_val`,
# `cash`, `sod_cash`, `avail_margin`, `used_margin`, `collateral`, `ltp` —
# all confirmed pure `row.get(<from_column>)` reads above). Where an
# existing metric of a similar-sounding name does EXTRA MATH beyond a raw
# column read (`day_pct` branches between two different columns depending
# on which is present; `pnl_pct` divides by the account's margin base, NOT
# by `inv_val`/notional), the raw column gets its OWN distinct exposed name
# instead (`day_change_pct`, `day_pct_margin`, `pnl_percentage`) — reusing
# the metric's name for the raw column here would make
# `scope.day_pct`/`scope.pnl_pct` silently mean something different from
# `day_pct@scope`/`pnl_pct@scope`, exactly the kind of misleading collision
# this registry exists to prevent.
#
# `account` is deliberately NOT used as an exposed name anywhere — it is a
# real enum CONSTANT VALUE today (`cancel_all_orders`/`chase_close_positions`
# action params' `scope` enum is `[total, account]`,
# agent_grammar.yaml:842-843/869-870) — colliding would make a leaf that
# happens to parse `account` ambiguous with that enum literal. The real
# `account` column is exposed as `account_id` everywhere instead.
#
# `watchlist_rows`' `account` field is a second, DIFFERENT trap: it carries
# the WATCHLIST NAME, not a trading account (see `Context.watchlist_rows`'
# own field comment, agent_evaluator.py:170-174) — exposed as
# `watchlist_name`, never `account_id`, so a dot-read can never silently
# return a watchlist name where an account id is expected.

def _col(from_column: str, label: str, value_type: str = 'number') -> dict:
    return {'type': value_type, 'from_column': from_column, 'label': label}


_COL_ACCOUNT_ID = _col('account', 'Account', 'string')

# Account-level positions summary (ctx.sum_positions). Shared verbatim by
# positions.total / positions.any_acct / positions.worst_acct — all three
# select rows from the SAME summary frame (`_scope_positions_total`,
# `_scope_positions_any_acct`, `_scope_positions_worst_acct` above all read
# `ctx.sum_positions`), so they carry identical columns.
_POSITIONS_SUMMARY_COLUMNS = {
    'account_id':     _COL_ACCOUNT_ID,
    'pnl':            _col('pnl', 'P&L (₹)'),
    'day_val':        _col('day_change_val', 'Day Change (₹)'),
    # Notional-based % (Σ|prev_close × quantity| denominator) — see
    # `_apply_positions_margin_pct`'s docstring: kept for reference, no
    # other confirmed consumer today. Distinct from `day_pct_margin` below.
    'day_change_pct': _col('day_change_percentage', 'Day Change % (Notional)'),
    # Margin-based % — what the `day_pct` metric token actually reads for
    # POSITIONS rows (fix #10: day_change_val / account margin base). Added
    # to the summary frame by `background._apply_positions_margin_pct`
    # after `_rebuild_positions_summary` runs, so it is always present by
    # the time the agent engine evaluates this scope.
    'day_pct_margin': _col('day_change_pct_margin', 'Day Change % (Margin-based)'),
}

# Account-level holdings summary (ctx.sum_holdings). Shared by
# holdings.total / holdings.any_acct / holdings.worst_acct AND
# holdings.worst_symbol — the latter's own resolver
# (`_scope_holdings_worst_symbol`) falls back to `_scope_holdings_worst_acct`
# unconditionally today (`ctx.holdings_rows` is read via `getattr` but is
# never actually populated anywhere in the codebase), so in practice it
# ALWAYS returns this same summary-row shape, never a per-symbol row.
_HOLDINGS_SUMMARY_COLUMNS = {
    'account_id':     _COL_ACCOUNT_ID,
    'inv_val':        _col('inv_val', 'Invested Value (₹)'),
    'cur_val':        _col('cur_val', 'Current Value (₹)'),
    'pnl':            _col('pnl', 'P&L (₹)'),
    'day_val':        _col('day_change_val', 'Day Change (₹)'),
    # Inv_val-based % (`pnl / inv_val * 100` — see `_bg_holdings_add_pct`).
    # NOT the same formula as the `pnl_pct` metric token (which divides by
    # the account's margin base, identical formula to positions) — kept as
    # its own name to avoid a silent "same name, different math" collision.
    'pnl_percentage': _col('pnl_percentage', 'P&L % (of Invested Value)'),
    # HOLDINGS rows never carry `day_change_pct_margin` (that column is
    # only ever added to the POSITIONS summary), so for holdings rows the
    # `day_pct` metric token IS a pure passthrough of this exact column —
    # still exposed under its own name here for consistency with the
    # positions-side naming (`day_change_pct`), not `day_pct`.
    'day_change_pct': _col('day_change_percentage', 'Day Change %'),
}

# Funds / margins (ctx.df_margins). Shared by funds.total / funds.any_acct.
# Raw column names are Kite's own broker-response convention (space
# included) — see `backend/brokers/broker_apis.py`'s `avail `/`util `
# prefixing of the flattened `available`/`utilised` sub-dicts.
_FUNDS_COLUMNS = {
    'account_id':     _COL_ACCOUNT_ID,
    # 'Live Cash' — matches the Funds grid's own headerName for this exact
    # column (`frontend/src/lib/data/pulseColumns.js` field `live_cash`),
    # not the grid's separate derived `cash_total` aggregate.
    'cash':           _col('avail cash', 'Live Cash'),
    'sod_cash':       _col('avail opening_balance', 'Start-of-Day Cash'),
    'avail_margin':   _col('net', 'Avail Margin'),
    'used_margin':    _col('util debits', 'Used Margin'),
    'collateral':     _col('avail collateral', 'Collateral'),
    # No existing metric token reads this column (fix #4 only wired
    # `cash`/`sod_cash`/`avail_margin`/`used_margin`/`collateral`) — new
    # exposed name, matching `routes/funds.py`'s own `_COL_MAP` entry
    # ('util option_premium' -> 'option_premium').
    'option_premium': _col('util option_premium', 'Option Premium'),
}

# Per-symbol position rows (ctx.position_rows — `df_positions.to_dict
# ("records")`, the SAME enriched frame `background._enrich_positions`
# produces; cross-broker normalised, so these columns exist for
# Kite/Dhan/Groww alike). Shared by positions.expiring_today /
# positions.expiring_today.nfo / positions.expiring_today.mcx_unhedged
# (all three filter the same `_scope_positions_expiring_today` base list)
# AND positions.worst_symbol, whose own resolver
# (`_scope_positions_worst_symbol`) reads `ctx.position_rows` directly and
# only falls back to the summary-row shape when that list is empty (rare —
# legacy/sim call sites that never populate it).
_POSITION_ROW_COLUMNS = {
    'account_id':     _COL_ACCOUNT_ID,
    # Exposed as `symbol`, NOT `tradingsymbol` — `order(symbol="...", ...)`
    # already established `symbol` as the operator-facing word for this
    # exact concept everywhere else in the grammar (order/bracket
    # statements). `from_column` stays the real, unchanged column name.
    'symbol':         _col('tradingsymbol', 'Symbol', 'string'),
    'exchange':       _col('exchange', 'Exchange', 'string'),
    'product':        _col('product', 'Product', 'string'),
    # Sprint G, Task 2 — derived underlying root (e.g. 'NIFTY',
    # 'CRUDEOIL') via `underlying_of_symbol` at row-build time
    # (`background.py`'s position_rows construction), same
    # `derivatives.parse_tradingsymbol`-based logic the MCX collar
    # detector already uses. None for cash-equity/unparseable rows —
    # a `scope_filter: {"underlying": ...}` naturally excludes those
    # rows (str(None) never matches a real underlying name). Wires the
    # `underlying:` qualifier onto every positions.* scope that shares
    # this per-symbol row schema (expiring_today / .nfo /
    # .mcx_unhedged / worst_symbol) via the SAME generic
    # `scope_filter_row_predicate` mechanism `symbol:`/`exchange:`
    # already use — no new filter machinery.
    'underlying':     _col('underlying', 'Underlying', 'string'),
    # Always in CONTRACTS, never lots — `_annotate_lot_size` normalises
    # `quantity` to contracts for every exchange before this row shape is
    # ever built (see CLAUDE.md "F&O order qty convention").
    'qty':            _col('quantity', 'Quantity (Contracts)'),
    'avg_price':      _col('average_price', 'Avg Price'),
    # Pure passthrough of the SAME column the real `ltp` metric token
    # already reads (`_metric_ltp` above) — reused verbatim.
    'ltp':            _col('last_price', 'LTP'),
    # Renamed from Kite's `close_price` at fetch time
    # (`broker_apis._fetch_positions_local`) — prior SETTLEMENT close, not
    # today's live close. See CLAUDE.md "close_price / ltp invariant".
    'prev_close':     _col('prev_close', 'Prev Close'),
    'pnl':            _col('pnl', 'P&L (₹)'),
    # Legacy backstop value (`apply_day_change_backstop`), NOT the
    # baseline-diff Day P&L SSOT `_rebuild_positions_summary` derives for
    # the account-level summary frame — see CLAUDE.md "Frontend Day P&L
    # SSOT". Exposed as-is since it is the real column on this row shape;
    # do not assume it agrees with `positions.total.pnl`'s day components.
    'day_val':        _col('day_change_val', 'Day Change (₹)'),
    'day_change_pct': _col('day_change_percentage', 'Day Change %'),
    'realised':       _col('realised', 'Realised P&L (₹)'),
    'unrealised':     _col('unrealised', 'Unrealised P&L (₹)'),
    'lots':           _col('lots', 'Lots'),
    'lot_size':       _col('lot_size', 'Lot Size'),
}

# Watchlist rows (ctx.watchlist_rows — see `_sd_build_watchlist_row`,
# backend/api/algo/sim/driver.py). `quantity`/`average_price`/`pnl` are
# hardcoded 0/0.0 on every watchlist row (no real position behind a
# watchlist entry) — deliberately NOT exposed here; a dot-read on them
# would always be the same constant, never a real signal.
_WATCHLIST_COLUMNS = {
    # RENAMED (semantic-collision guard) — the raw `account` column on a
    # watchlist row holds the WATCHLIST NAME ('Default', 'Markets', ...),
    # never a trading account. See module-level note above.
    'watchlist_name': _col('account', 'Watchlist Name', 'string'),
    # Exposed as `symbol`, NOT `tradingsymbol` — same operator-facing
    # naming convention as `order(symbol=...)` and the position-row family
    # above. `from_column` stays the real, unchanged column name.
    'symbol':         _col('tradingsymbol', 'Symbol', 'string'),
    'exchange':       _col('exchange', 'Exchange', 'string'),
    'ltp':            _col('last_price', 'LTP'),
    'prev_close':     _col('close_price', 'Prev Close'),
    'bid':            _col('bid', 'Bid'),
    'ask':            _col('ask', 'Ask'),
    'day_change':     _col('day_change', 'Day Change (₹/share)'),
    # SECOND semantic-collision guard found on this scope family (NOT just
    # the watchlist-name one above): `_sd_build_watchlist_row` sets this
    # column to `change_pct / 100.0` — i.e. a FRACTION (-0.02 for -2%),
    # NOT a percent. positions/holdings' `day_change_pct` (and the raw
    # `day_change_percentage` column everywhere else) is a PERCENT
    # (-2.0 for -2%). Reusing the name `day_change_pct` here would make
    # `watchlist.all.day_change_pct <= -2` silently never fire (a real
    # -2% day never reaches -2.0 once expressed as a fraction) — exactly
    # the kind of "same name, different unit" collision this registry
    # exists to prevent. Exposed under its own distinct name instead.
    'day_change_frac': _col('day_change_percentage', 'Day Change % (fraction, e.g. -0.02 = -2%)'),
}

# Orders rows (ctx.orders_rows — see `background._perf_fetch_orders_rows`).
# One dict per currently open/active AlgoOrder, account-rooted like
# positions/holdings/funds. `symbol`/`qty`/`account_id` reuse established
# vocabulary; `side`/`price`/`status`/`order_id` are new names with no
# prior precedent on any other scope family.
_ORDERS_COLUMNS = {
    'account_id': _COL_ACCOUNT_ID,
    # Exposed as `symbol`, NOT `tradingsymbol` — same convention every
    # other row-shaped scope family above already uses.
    'symbol':     _col('tradingsymbol', 'Symbol', 'string'),
    'exchange':   _col('exchange', 'Exchange', 'string'),
    # BUY/SELL — AlgoOrder.transaction_type.
    'side':       _col('side', 'Side (BUY/SELL)', 'string'),
    'qty':        _col('quantity', 'Quantity (Contracts)'),
    # current_limit (if the chase loop has re-quoted) else initial_price —
    # computed once at row-build time (background._perf_fetch_orders_rows),
    # same "live re-quoted price wins" convention the ChaseCard UI already
    # uses for this exact column pair (see AlgoOrder.current_limit's own
    # field comment, models.py).
    'price':      _col('price', 'Order Price (₹)'),
    'status':     _col('status', 'Order Status', 'string'),
    # Maps 1:1 to the `broker_order_id` param every `cancel_order`/
    # `modify_order` action reads first (actions_live.py; the legacy
    # `order_id` key is still read as a fallback) — i.e. the BROKER's
    # order id (AlgoOrder.broker_order_id), never the internal
    # AlgoOrder.id primary key. A `{"broker_order_id": {"$row":
    # "order_id"}}` action param on an `orders.any_acct` match therefore
    # resolves to exactly the value those actions expect.
    'order_id':   _col('order_id', 'Broker Order ID', 'string'),
}

# Quote rows (ctx.quote_rows — see `background._perf_build_quote_rows`).
# One dict per pre-tracked ad-hoc symbol, instrument-rooted (no account
# dimension at all — market data belongs to no account). `symbol`/
# `exchange`/`ltp`/`bid`/`ask` reuse the EXACT same names + from_columns
# `_WATCHLIST_COLUMNS` already established for the identical concepts
# (`ltp` is additionally a real registered metric token — see
# `_metric_ltp` above, proven a pure passthrough of `last_price` by
# `test_reused_exposed_names_are_pure_passthroughs_of_their_metric`).
# `volume` is new, no prior precedent.
_QUOTE_COLUMNS = {
    'symbol':   _col('tradingsymbol', 'Symbol', 'string'),
    'exchange': _col('exchange', 'Exchange', 'string'),
    'ltp':      _col('last_price', 'LTP'),
    'bid':      _col('bid', 'Bid'),
    'ask':      _col('ask', 'Ask'),
    'volume':   _col('volume', 'Volume'),
}

# Symbols rows (ctx.symbols_rows — see `background._perf_build_symbols_rows`).
# Sprint G — unifies watchlist/pinned/gainers/losers under one scope family.
# `kind` is the qualifier column `scope_filter: {"kind": "gainers"}` reads
# (REQUIRED — see `agent_evaluator.validate()`'s `symbols.*` guard), via the
# SAME generic `scope_filter_row_predicate` mechanism `symbol:`/`exchange:`
# already use elsewhere — no new filter machinery for this sprint.
# `day_change_pct` is normalised to a true PERCENT (e.g. -2.0 = -2%) across
# EVERY kind at row-build time in `background.py`, even though the
# watchlist/pinned source rows carry a FRACTION internally (see
# `_WATCHLIST_COLUMNS`'s own `day_change_frac` collision-guard note above) —
# deliberately avoiding that exact "same name, different unit" trap for
# this new, unified family.
_SYMBOLS_COLUMNS = {
    # 'watchlist' | 'pinned' | 'gainers' | 'losers' — see
    # `background._perf_build_symbols_rows` for how each kind is built.
    'kind':           _col('kind', 'Kind', 'string'),
    'symbol':         _col('tradingsymbol', 'Symbol', 'string'),
    'exchange':       _col('exchange', 'Exchange', 'string'),
    'ltp':            _col('last_price', 'LTP'),
    'day_change':     _col('day_change', 'Day Change (₹)'),
    'day_change_pct': _col('day_change_percentage', 'Day Change %'),
}

# Underlying-grouped positions summary (ctx.position_rows, grouped — see
# `_scope_positions_any_underlying`). `accounts` is a list, not a scalar —
# deliberately not exposed here (EXPOSED_COLUMNS only supports
# number/string/boolean leaf types; see
# `test_every_entry_has_a_valid_type_and_from_column`).
_POSITIONS_ANY_UNDERLYING_COLUMNS = {
    'underlying': _col('underlying', 'Underlying', 'string'),
    'pnl':        _col('pnl', 'P&L (₹)'),
    'day_val':    _col('day_change_val', 'Day Change (₹)'),
}

# scope token -> its exposed-column schema. MUST cover every real
# `token_kind: scope` row in agent_grammar.yaml — enforced by
# backend/tests/test_grammar_exposed_columns.py (walks the YAML itself,
# never a hardcoded copy of this key list).
EXPOSED_COLUMNS: dict[str, dict] = {
    'positions.total':                       _POSITIONS_SUMMARY_COLUMNS,
    'positions.any_acct':                     _POSITIONS_SUMMARY_COLUMNS,
    'positions.worst_acct':                   _POSITIONS_SUMMARY_COLUMNS,
    'positions.worst_symbol':                 _POSITION_ROW_COLUMNS,
    'positions.expiring_today':               _POSITION_ROW_COLUMNS,
    'positions.expiring_today.nfo':           _POSITION_ROW_COLUMNS,
    'positions.expiring_today.mcx_unhedged':  _POSITION_ROW_COLUMNS,
    'holdings.total':                         _HOLDINGS_SUMMARY_COLUMNS,
    'holdings.any_acct':                      _HOLDINGS_SUMMARY_COLUMNS,
    'holdings.worst_acct':                    _HOLDINGS_SUMMARY_COLUMNS,
    'holdings.worst_symbol':                  _HOLDINGS_SUMMARY_COLUMNS,
    'funds.total':                            _FUNDS_COLUMNS,
    'funds.any_acct':                         _FUNDS_COLUMNS,
    'watchlist.all':                          _WATCHLIST_COLUMNS,
    'watchlist.default':                      _WATCHLIST_COLUMNS,
    'watchlist.markets':                      _WATCHLIST_COLUMNS,
    'orders.any_acct':                        _ORDERS_COLUMNS,
    'quote.tracked':                          _QUOTE_COLUMNS,
    'symbols.all':                            _SYMBOLS_COLUMNS,
    'positions.any_underlying':               _POSITIONS_ANY_UNDERLYING_COLUMNS,
}

# scope token -> (ctx attribute the resolver reads, resolver function name
# in this module). Catalog-projection provenance ONLY (routes/grammar.py's
# `_merge_exposed_column_rows` uses this to write each column row's
# `description`) — kept separate from EXPOSED_COLUMNS itself since it is
# one fact per SCOPE, not per column.
EXPOSED_COLUMN_SCOPE_PROVENANCE: dict[str, tuple[str, str]] = {
    'positions.total':                       ('ctx.sum_positions', '_scope_positions_total'),
    'positions.any_acct':                     ('ctx.sum_positions', '_scope_positions_any_acct'),
    'positions.worst_acct':                   ('ctx.sum_positions', '_scope_positions_worst_acct'),
    'positions.worst_symbol':                 ('ctx.position_rows', '_scope_positions_worst_symbol'),
    'positions.expiring_today':               ('ctx.position_rows', '_scope_positions_expiring_today'),
    'positions.expiring_today.nfo':           ('ctx.position_rows', '_scope_positions_expiring_today_nfo'),
    'positions.expiring_today.mcx_unhedged':  ('ctx.position_rows', '_scope_positions_expiring_today_mcx_unhedged'),
    'holdings.total':                         ('ctx.sum_holdings', '_scope_holdings_total'),
    'holdings.any_acct':                      ('ctx.sum_holdings', '_scope_holdings_any_acct'),
    'holdings.worst_acct':                    ('ctx.sum_holdings', '_scope_holdings_worst_acct'),
    'holdings.worst_symbol':                  ('ctx.sum_holdings', '_scope_holdings_worst_symbol'),
    'funds.total':                            ('ctx.df_margins', '_scope_funds_total'),
    'funds.any_acct':                         ('ctx.df_margins', '_scope_funds_any_acct'),
    'watchlist.all':                          ('ctx.watchlist_rows', '_scope_watchlist_all'),
    'watchlist.default':                      ('ctx.watchlist_rows', '_scope_watchlist_default'),
    'watchlist.markets':                      ('ctx.watchlist_rows', '_scope_watchlist_markets'),
    'orders.any_acct':                        ('ctx.orders_rows', '_scope_orders_any_acct'),
    'quote.tracked':                          ('ctx.quote_rows', '_scope_quote_tracked'),
    'symbols.all':                            ('ctx.symbols_rows', '_scope_symbols_all'),
    'positions.any_underlying':               ('ctx.position_rows', '_scope_positions_any_underlying'),
}


def exposed_columns_for_scope(scope_token: str) -> dict:
    """Return the `{exposed_name: {type, from_column, label}}` schema for
    `scope_token`, or `{}` if the scope has no registered schema (unknown
    scope / a future scope added without updating EXPOSED_COLUMNS)."""
    return EXPOSED_COLUMNS.get(scope_token, {})


# ── Scope filter (Sprint 19) — generic row-predicate for leaf.scope_filter ──
#
# Closes a real join-granularity bug: a per-position scope like
# `positions.expiring_today` can return SEVERAL rows for the same account
# (one per held symbol). Splitting "this symbol" and "this metric breaches"
# into two separately-ANDed leaves is UNSAFE — `_eval_all`'s join key
# (agent_evaluator.py) is `account` only, not `(account, symbol)`, so the
# two leaves could spuriously "agree" on account while actually referring
# to two different symbol-rows within it. A single-leaf row filter has no
# such ambiguity (there's only one leaf, so nothing to cross-join).
#
# Chosen shape: an OPTIONAL `scope_filter` key alongside the existing leaf
# `{"metric", "scope", "op", "value"}` shape —
#   {"metric": "pnl", "scope": "positions.expiring_today",
#    "op": "<=", "value": -5000,
#    "scope_filter": {"symbol": "NIFTY25JULFUT"}}
# applied by `_eval_leaf` to the rows `scope_fn(ctx)` already returned,
# BEFORE the metric evaluates each row — so it composes for free with
# `expiring_today`, `.nfo`, and `.mcx_unhedged` alike (whichever base
# scope ran, the SAME filter step runs on its output; no per-variant
# duplication).
#
# Rejected alternative: encoding the symbol as a call-argument on the
# scope token itself (mirroring `mean_pnl(30)`'s parameterized-token
# precedent, e.g. `positions.expiring_today(symbol=...)`). Two concrete
# incompatibilities with the EXISTING shared mechanism
# (`GrammarRegistry._parse_call_token`) ruled this out after reading it:
# (1) that parser requires the call's `func` to be a bare `ast.Name` —
# `positions.expiring_today(...)` parses as an `ast.Attribute` chain, not
# a `Name`, so it wouldn't even reach factory lookup without generalizing
# the dotted-name walk; (2) it accepts ONLY positive numeric literal args
# (explicitly documented as intentional — "every current params_schema is
# a window in minutes") — a symbol is a string, so arg validation would
# also need loosening. Both changes would widen a mechanism SHARED by
# metric/scope/channel/format factories just to serve this one scope
# family, for no benefit over the leaf-key approach, which touches zero
# shared/generic code and needs no change to the scope token strings
# `REGISTRY.scope()`/`validate()` already resolve correctly today.
def scope_filter_row_predicate(scope_token, scope_filter):
    """Build a `(row) -> bool` predicate for an optional leaf `scope_filter`
    dict, e.g. `{"symbol": "NIFTY25JULFUT"}`.

    Each qualifier key MUST be a real exposed column on `scope_token`'s own
    EXPOSED_COLUMNS schema (reuses the SAME whitelist `_resolve_metric_fn`'s
    fallback already uses) — `funds.any_acct` has no `symbol` column, so
    `{"symbol": ...}` there is rejected (returns None), matching the
    "attempting funds.symbol:... is a compile error" rule. Returns None
    when ANY qualifier key isn't on the scope's schema; the caller decides
    whether that's a validate()-time error or a runtime skip-with-warning.

    `value == "*"` is an explicit "any value" wildcard for that qualifier —
    functionally identical to omitting it, kept only for operator-facing
    symmetry with `account:*` elsewhere in this grammar.

    Multiple qualifiers AND-combine (e.g. a future
    `{"account": "ZG0790", "symbol": "NIFTY25JULFUT"}`) — there is no
    ambiguity here the way there is across separate leaves, since this is
    one predicate applied to one scope's own rows.

    Always returns a callable when every qualifier resolves (including an
    always-true predicate for an empty/all-wildcard filter dict)."""
    schema = exposed_columns_for_scope(scope_token)
    resolved = []
    for qual, want in (scope_filter or {}).items():
        if want == '*':
            continue
        col = schema.get(qual)
        if not col:
            return None
        resolved.append((col['from_column'], want))

    def predicate(row):
        return all(str(row.get(from_col)) == str(want) for from_col, want in resolved)

    return predicate


# ── Operators — binary comparators (leaf-level) ──────────────────────────

OPERATORS = {
    '<':       lambda a, b: a is not None and a <  b,
    '<=':      lambda a, b: a is not None and a <= b,
    '>':       lambda a, b: a is not None and a >  b,
    '>=':      lambda a, b: a is not None and a >= b,
    '==':      lambda a, b: a == b,
    '!=':      lambda a, b: a != b,
    'in':      lambda a, b: a in (b or []),
    'not_in':  lambda a, b: a not in (b or []),
    'between': lambda a, b: a is not None and (b[0] <= a <= b[1]),
}


# ── Composite operators (tree level) are keywords, not tokens: all|any|not.
#    They live in the condition tree schema itself.


# ═══════════════════════════════════════════════════════════════════════════
#  SYSTEM TOKEN CATALOG — seeded into grammar_tokens on every boot.
# ═══════════════════════════════════════════════════════════════════════════
#
# Catalog entries live in backend/config/grammars/agent_grammar.yaml (see
# the loader at the top of this file). Every entry becomes one row in
# grammar_tokens with is_system=True. Operators editing the DB cannot
# delete system rows; they can only mark them inactive.
#
# Adding a new system capability = append an entry to agent_grammar.yaml
# AND implement the resolver function above. The frontend admin UI will
# display these as "built-in" and allow custom extensions in the same
# table.
# ───────────────────────────────────────────────────────────────────────────

SYSTEM_TOKENS: list[dict] = _build_system_tokens(_GRAMMAR_CATALOG)


# ═══════════════════════════════════════════════════════════════════════════
#  SEEDER — upsert system tokens into grammar_tokens on every app startup.
# ═══════════════════════════════════════════════════════════════════════════

async def seed_grammar_tokens():
    """
    Upsert every system token into grammar_tokens. Run once per app startup.

    Preserves any operator-authored custom tokens (is_system=False) and any
    is_active flip operators have made on system rows. Any system token that
    disappears from the SYSTEM_TOKENS list between releases is left in the
    table as is_active=True until manually cleaned — safer than auto-deleting
    something an agent might still reference.
    """
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import GrammarToken

    async with async_session() as s:
        existing = await s.execute(select(GrammarToken).where(GrammarToken.is_system == True))  # noqa: E712
        by_key = {(t.grammar_kind, t.token_kind, t.token): t for t in existing.scalars().all()}

        inserted = 0
        updated = 0
        for spec in SYSTEM_TOKENS:
            key = (spec['grammar_kind'], spec['token_kind'], spec['token'])
            row = by_key.get(key)
            if row is None:
                s.add(GrammarToken(
                    grammar_kind=spec['grammar_kind'],
                    token_kind=spec['token_kind'],
                    token=spec['token'],
                    value_type=spec.get('value_type'),
                    units=spec.get('units'),
                    description=spec.get('description', ''),
                    resolver=spec.get('resolver'),
                    params_schema=spec.get('params_schema'),
                    enum_values=spec.get('enum_values'),
                    template_body=spec.get('template_body'),
                    source=spec.get('source'),
                    is_system=True,
                    is_active=True,
                ))
                inserted += 1
            else:
                # Keep the operator-facing fields fresh (description, schema, resolver
                # path can all shift between releases) but do NOT overwrite is_active
                # so a disabled system token stays disabled across deploys.
                row.value_type    = spec.get('value_type',    row.value_type)
                row.units         = spec.get('units',         row.units)
                row.description   = spec.get('description',   row.description or '')
                row.resolver      = spec.get('resolver',      row.resolver)
                row.source        = spec.get('source',        row.source)
                row.params_schema = spec.get('params_schema', row.params_schema)
                row.enum_values   = spec.get('enum_values',   row.enum_values)
                row.template_body = spec.get('template_body', row.template_body)
                updated += 1
        await s.commit()
        logger.info(f"Grammar tokens seeded — inserted={inserted} updated={updated}")


_LOG_SOURCE = _GRAMMAR_CATALOG["log_tags"]["source"]
LOG_TAG_TOKENS: list[dict] = [
    {'grammar_kind': 'log', 'token_kind': 'tag', 'token': t['token'], 'value_type': 'string',
     'description': t['description'], 'source': _LOG_SOURCE}
    for t in _GRAMMAR_CATALOG["log_tags"]["tags"]
]
SYSTEM_TOKENS.extend(LOG_TAG_TOKENS)


# ═══════════════════════════════════════════════════════════════════════════
#  ACTION PARAMS-SCHEMA LOOKUP (action-param expression evaluator, Sprint 1)
# ═══════════════════════════════════════════════════════════════════════════
#
# Built from SYSTEM_TOKENS — the code-reviewed, YAML-sourced catalog —
# rather than the DB-backed `GrammarRegistry`. Two reasons: (1) the
# registry is only populated by an async `reload()` against the live
# `grammar_tokens` table, so it is empty in-process until the first
# `seed_grammar_tokens()`/`reload()` runs (and empty in any unit test that
# doesn't stand up a DB session) — `resolve_action_params()` in
# `actions.py` must work with no DB involved at all. (2) Deciding which
# params are allowed to carry an expression string is a TRUST decision —
# the code-reviewed YAML catalog is the right boundary for that, not an
# operator-editable DB row (an operator flipping `token_ref_ok` on a
# custom action via the admin UI should not silently grant expression
# evaluation on a field never vetted for it).
_ACTION_PARAMS_SCHEMAS: dict[str, dict] = {
    tok["token"]: tok.get("params_schema") or {}
    for tok in SYSTEM_TOKENS
    if tok.get("grammar_kind") == "action" and tok.get("token_kind") == "action_type"
}


def get_action_params_schema(action_type: str) -> dict:
    """Return the resolved params_schema dict for a system action token
    (any `$ref` markers already merged — see `_resolve_param_spec`), or
    `{}` if `action_type` is unknown. Used by
    `actions.py:resolve_action_params()` to decide which params may carry
    an expression string.

    NOTE: an empty dict is also the correct return for a KNOWN action type
    that simply takes no params (e.g. `deactivate_agent`) — callers that
    need to distinguish "known, no params" from "unknown action type"
    must use `is_known_action_type()` below, not truthiness of this
    return value."""
    return _ACTION_PARAMS_SCHEMAS.get(action_type, {})


def is_known_action_type(action_type: str) -> bool:
    """True iff `action_type` is a registered system action token —
    independent of whether its params_schema is empty. Used by
    `routes/agents.py:_age_validate_action_entries()` to reject an
    unknown action type without also rejecting a real action (like
    `deactivate_agent`) that legitimately has no params."""
    return action_type in _ACTION_PARAMS_SCHEMAS


# ═══════════════════════════════════════════════════════════════════════════
#  ACTION MUTUAL-EXCLUSION GROUPS (Sprint 17)
# ═══════════════════════════════════════════════════════════════════════════
#
# `mutually_exclusive_groups` is a TOKEN-level key (sibling of
# params_schema, not nested inside it — see place_order's entry in
# agent_grammar.yaml) declaring field-name groups where at most one member
# may be present on a given call. Built from SYSTEM_TOKENS for the same
# trust-boundary reason `_ACTION_PARAMS_SCHEMAS` above is: the DB-backed
# `GrammarToken` model (grammar_tokens table) has no column for this key,
# so a value here can ONLY ever come from the code-reviewed YAML catalog,
# never from an operator-editable custom token row. `routes/grammar.py`'s
# `_to_out()` reads this lookup by token name (for system action tokens
# only) to populate `GrammarTokenOut.mutually_exclusive_groups` on the
# wire — the YAML key itself is never persisted into the grammar_tokens
# table by `seed_grammar_tokens()`.
_ACTION_MUTEX_GROUPS: dict[str, list] = {
    tok["token"]: tok["mutually_exclusive_groups"]
    for tok in SYSTEM_TOKENS
    if tok.get("grammar_kind") == "action" and tok.get("token_kind") == "action_type"
    and tok.get("mutually_exclusive_groups")
}


def get_action_mutex_groups(action_type: str) -> list | None:
    """Return the declared `mutually_exclusive_groups` list (list of
    list[str] field-name groups) for a system action token, or `None` if
    the action type is unknown or declares no mutex groups."""
    return _ACTION_MUTEX_GROUPS.get(action_type)


# ═══════════════════════════════════════════════════════════════════════════
#  EVENT-AGENT RENDER ACTION PARAMS SCHEMA (Sprint 18)
# ═══════════════════════════════════════════════════════════════════════════
#
# Sourced from agent_grammar.yaml's `event_actions.render.params_schema` —
# a SEPARATE top-level YAML key from `SYSTEM_TOKENS`'s `action_type`
# family, deliberately not merged into it (see that section's own
# comment in the YAML for why: it would make `is_known_action_type
# ("render")` true and let a kind='cycle' agent's DO list accept a
# `render` action with no real handler). A kind='event' agent's single
# `{"type": "render", "render": <key>, ...}` action entry is validated
# against `event_agents.RENDERS` (the `render` key's own value), not
# against this schema — this schema only covers RENDER's OWN optional
# sibling keys (currently just `gate`).
_EVENT_ACTIONS_CATALOG: dict = _GRAMMAR_CATALOG.get("event_actions") or {}
_RENDER_ACTION_PARAMS_SCHEMA: dict = (_EVENT_ACTIONS_CATALOG.get("render") or {}).get("params_schema") or {}


def get_render_action_params_schema() -> dict:
    """Return the declared params_schema for a kind='event' agent's
    `{"type": "render", ...}` action entry (currently just `gate`) —
    the keys an action entry may carry besides the required `type`/
    `render` pair. Empty dict if the YAML section is absent."""
    return dict(_RENDER_ACTION_PARAMS_SCHEMA)
