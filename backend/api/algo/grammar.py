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


def _metric_pnl(ctx, row):
    """Positions P&L in ₹ (mark-to-market)."""
    return float(row.get('pnl', 0) or 0)

def _metric_pnl_pct(ctx, row):
    """Positions P&L as a % of the account's margin base.

    2026-10 audit fix: previously divided by `ctx.used_margin_for()`
    (an OR-fallback: `util debits` if > 0, else `net`) — a DIFFERENT
    and narrower denominator than `day_pct`'s own corrected basis
    (fix #10: used+available margin SUM, via `account_margin_base()`).
    The OR-fallback denominator swings every time a position opens or
    closes (util debits shrinks/grows) even with zero P&L change,
    reproducing the exact "closed legs drop out of the denominator"
    symptom fix #10 was meant to eliminate — just for pnl_pct instead
    of day_pct. Now shares the SAME denominator as day_pct.

    Returns None when the margin base is unavailable for this account
    (missing-vs-zero convention — a genuinely missing figure must skip
    the leaf, not silently fall back to a different denominator).
    """
    from backend.api.algo.pnl_math import account_margin_base
    base = account_margin_base(ctx.df_margins, row.get('account'))
    if base is None or base <= 0:
        return None
    return (float(row.get('pnl', 0) or 0) / base) * 100.0

def _metric_day_val(ctx, row):
    """Holdings day-change value in ₹."""
    return float(row.get('day_change_val', 0) or 0)

def _metric_day_pct(ctx, row):
    """Day-change percentage.

    Fix #10 — for POSITIONS rows, prefer `day_change_pct_margin` (added by
    `background._apply_positions_margin_pct`: day_change_val / account
    margin base, NOT notional) when present on the row. Notional
    (Σ|prev_close × quantity| over CURRENT rows) drops closed (qty=0)
    legs from the denominator while the numerator still carries their
    P&L, so a mostly-closed book could show -500% on a real -2%-of-margin
    move. HOLDINGS rows never carry that column — they keep the existing
    `day_change_percentage` (opening-value denominator), which is the
    standard, uncontested definition for a day's % move on a holding.
    Returns None (not 0) when the value itself is missing — real 0% is
    still a valid value and must pass through.
    """
    if 'day_change_pct_margin' in row:
        return _num_or_none(row.get('day_change_pct_margin'))
    val = row.get('day_change_percentage')
    return _num_or_none(val) if val is not None else 0.0

def _metric_inv_val(ctx, row):
    return float(row.get('inv_val', 0) or 0)

def _metric_cur_val(ctx, row):
    return float(row.get('cur_val', 0) or 0)

def _metric_cash(ctx, row):
    """Live available cash (fix #4 — was reading start-of-day
    'avail opening_balance', which cannot move intraday, so
    loss-funds-negative's cash<0 leaf could never fire on a real
    intraday cash drop). 'avail cash' is the same column
    `routes/funds.py` maps to `live_cash` (Kite's `available.cash` /
    Dhan+Groww adapters' normalised `available.cash`, both None-safe —
    see `dhan.py:_dhan_margins_available` / `groww.py:_groww_margin_available`).
    Kept the token name `cash` (not renamed) so existing agent rows
    (prod's `loss-funds-negative`) get the corrected live-cash semantics
    without an operator having to re-save the agent — see `sod_cash`
    below for the retained start-of-day reading."""
    return _num_or_none(row.get('avail cash'))

def _metric_sod_cash(ctx, row):
    """Start-of-day cash (the value `cash` used to read before fix #4).
    Kept as its own token for agents that specifically want the SOD
    baseline rather than live intraday cash."""
    return _num_or_none(row.get('avail opening_balance'))

def _metric_avail_margin(ctx, row):
    return _num_or_none(row.get('net'))

def _metric_used_margin(ctx, row):
    return _num_or_none(row.get('util debits'))

def _metric_collateral(ctx, row):
    return _num_or_none(row.get('avail collateral'))

# Rate-of-change metrics. They use the rolling history the engine maintains
# per (section, scope). Section is inferred from the scope token.
def _metric_pnl_rate_abs(ctx, row):
    return ctx.rate_abs(('positions', row.get('account')))

def _metric_pnl_rate_pct(ctx, row):
    return ctx.rate_pct(('positions', row.get('account')))

def _metric_day_rate_abs(ctx, row):
    return ctx.rate_abs(('holdings', row.get('account')))

def _metric_day_rate_pct(ctx, row):
    return ctx.rate_pct(('holdings', row.get('account')))


# Phase 24 — Rolling-window statistical metrics. Same pnl_history bucket
# the rate metrics read from; different reducer. Section comes from the
# scope token (positions.* → 'positions', holdings.* → 'holdings'),
# field_idx 1 = pnl ₹, 2 = pnl %.

def _w_key_pos(row):  return ('positions', row.get('account'))
def _w_key_hold(row): return ('holdings',  row.get('account'))

# Phase 26 (Sprint 2) — parameterized function-call tokens, e.g.
# `mean_pnl(30)`. Each factory below is the resolver bound to the BASE
# token name (registered in agent_grammar.yaml with a `params_schema`);
# GrammarRegistry calls it once per distinct literal call-string (cached
# forever after) and gets back the (ctx, row) -> value callable a metric
# resolver normally is. The pre-existing fixed-window tokens below are
# kept forever, unchanged, as one-line bindings to the same factories —
# no deprecation, no migration, zero behavior change for existing agents.

def _metric_factory_mean_pnl(minutes):
    return lambda ctx, row: ctx.window_mean(_w_key_pos(row), minutes)

def _metric_factory_mean_day(minutes):
    return lambda ctx, row: ctx.window_mean(_w_key_hold(row), minutes)

def _metric_factory_max_drawdown_pnl(minutes):
    return lambda ctx, row: ctx.window_drawdown(_w_key_pos(row), minutes)

def _metric_factory_max_drawdown_day(minutes):
    return lambda ctx, row: ctx.window_drawdown(_w_key_hold(row), minutes)

def _metric_factory_max_drawdown_pnl_pct(minutes):
    return lambda ctx, row: ctx.window_drawdown(_w_key_pos(row), minutes, field_idx=2)

def _metric_factory_stdev_pnl(minutes):
    return lambda ctx, row: ctx.window_stdev(_w_key_pos(row), minutes)

def _metric_factory_range_pnl(minutes):
    return lambda ctx, row: ctx.window_range(_w_key_pos(row), minutes)

_metric_mean_pnl_30m  = _metric_factory_mean_pnl(30)
_metric_mean_pnl_1h   = _metric_factory_mean_pnl(60)
_metric_mean_day_30m  = _metric_factory_mean_day(30)
_metric_mean_day_1h   = _metric_factory_mean_day(60)

_metric_max_drawdown_pnl_30m = _metric_factory_max_drawdown_pnl(30)
_metric_max_drawdown_pnl_1h  = _metric_factory_max_drawdown_pnl(60)
_metric_max_drawdown_pnl_4h  = _metric_factory_max_drawdown_pnl(240)
_metric_max_drawdown_day_1h  = _metric_factory_max_drawdown_day(60)

_metric_max_drawdown_pnl_pct_30m = _metric_factory_max_drawdown_pnl_pct(30)
_metric_max_drawdown_pnl_pct_1h  = _metric_factory_max_drawdown_pnl_pct(60)

_metric_stdev_pnl_30m = _metric_factory_stdev_pnl(30)
_metric_stdev_pnl_1h  = _metric_factory_stdev_pnl(60)

_metric_range_pnl_30m = _metric_factory_range_pnl(30)
_metric_range_pnl_1h  = _metric_factory_range_pnl(60)


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


def _metric_days_until_expiry(ctx, row):
    """Days until this position's option/future expires. None for
    cash equity holdings (non-parseable tradingsymbol)."""
    sym = row.get('tradingsymbol') or ''
    parsed = _parsed_or_none(sym)
    if not parsed or not parsed.get('expiry'):
        return None
    try:
        from backend.api.algo.derivatives import days_to_expiry
        # MCX commodity options trade until 23:30; everything else 15:30.
        close_time = (23, 30) if (row.get('exchange') or '').upper() == 'MCX' else (15, 30)
        return float(days_to_expiry(parsed['expiry'], ref=ctx.now, close_time=close_time))
    except Exception:
        return None


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


def _metric_is_itm(ctx, row):
    """1.0 when the option is in-the-money at current spot, 0.0
    otherwise. None when row isn't an option or spot is unavailable."""
    kind, intrinsic = _option_moneyness(ctx, row)
    if kind is None:
        return None
    return 1.0 if intrinsic > 0 else 0.0


def _metric_is_ntm(ctx, row):
    """1.0 when the option is within ±1.5% of spot (near-the-money),
    0.0 otherwise. The 1.5% threshold matches the legacy ExpiryEngine
    default. None when row isn't an option or spot is unavailable."""
    kind, intrinsic = _option_moneyness(ctx, row)
    if kind is None:
        return None
    return 1.0 if abs(intrinsic) <= 0.015 else 0.0


def _metric_is_future(ctx, row):
    """1.0 when the position is a futures contract (kind == 'fut'),
    0.0 when it is an option, None for equity or unrecognised symbols."""
    sym = row.get('tradingsymbol') or ''
    parsed = _parsed_or_none(sym)
    if not parsed:
        return None
    kind = parsed.get('kind')
    if kind == 'fut':
        return 1.0
    if kind == 'opt':
        return 0.0
    return None


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

def _metric_ltp(ctx, row):
    """Last-traded price for a watchlist row. Reused for any row that
    carries a `last_price` column — works on positions rows too."""
    return float(row.get('last_price', 0) or 0)


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
    an expression string."""
    return _ACTION_PARAMS_SCHEMAS.get(action_type, {})
