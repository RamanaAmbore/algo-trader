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

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


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

def _metric_pnl(ctx, row):
    """Positions P&L in ₹ (mark-to-market)."""
    return float(row.get('pnl', 0) or 0)

def _metric_pnl_pct(ctx, row):
    """Positions P&L as % of used margin. None when no open positions."""
    um = ctx.used_margin_for(row.get('account'))
    if um is None or um <= 0:
        return None
    return (float(row.get('pnl', 0) or 0) / um) * 100.0

def _metric_day_val(ctx, row):
    """Holdings day-change value in ₹."""
    return float(row.get('day_change_val', 0) or 0)

def _metric_day_pct(ctx, row):
    """Holdings day-change percentage."""
    return float(row.get('day_change_percentage', 0) or 0)

def _metric_inv_val(ctx, row):
    return float(row.get('inv_val', 0) or 0)

def _metric_cur_val(ctx, row):
    return float(row.get('cur_val', 0) or 0)

def _metric_cash(ctx, row):
    return float(row.get('avail opening_balance', 0) or 0)

def _metric_avail_margin(ctx, row):
    return float(row.get('net', 0) or 0)

def _metric_used_margin(ctx, row):
    return float(row.get('util debits', 0) or 0)

def _metric_collateral(ctx, row):
    return float(row.get('avail collateral', 0) or 0)

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

def _scope_positions_total(ctx):
    df = ctx.sum_positions
    if df is None or df.empty:
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
    """Single per-account holdings row with the worst day_pct."""
    rows = _scope_holdings_any_acct(ctx)
    return _row_with_min(rows, 'day_pct')

def _scope_holdings_worst_symbol(ctx):
    """Single per-symbol holdings row with the worst day_pct. Note: relies
    on the engine context populating per-symbol detail. When the live
    pipeline only carries per-account aggregates (current default), this
    falls back to the same row as worst_acct — operators get an honest
    drawdown signal either way."""
    df = getattr(ctx, 'holdings_rows', None)
    if df is None or (hasattr(df, 'empty') and df.empty):
        return _scope_holdings_worst_acct(ctx)
    rows = [r.to_dict() for _, r in df.iterrows()] if hasattr(df, 'iterrows') else list(df)
    return _row_with_min(rows, 'day_pct')

def _scope_positions_worst_acct(ctx):
    """Single per-account positions row with the worst pnl_pct."""
    rows = _scope_positions_any_acct(ctx)
    return _row_with_min(rows, 'pnl_pct')

def _scope_positions_worst_symbol(ctx):
    """Single per-symbol positions row with the worst pnl. Falls back to
    worst_acct semantics when per-symbol rows aren't on the context."""
    df = getattr(ctx, 'positions_rows', None)
    if df is None or (hasattr(df, 'empty') and df.empty):
        return _scope_positions_worst_acct(ctx)
    rows = [r.to_dict() for _, r in df.iterrows()] if hasattr(df, 'iterrows') else list(df)
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
# Every entry in this list becomes one row in grammar_tokens with
# is_system=True. Operators editing the DB cannot delete system rows; they
# can only mark them inactive.
#
# Adding a new system capability = append an entry here AND implement the
# resolver function above. The frontend admin UI will display these as
# "built-in" and allow custom extensions in the same table.
# ───────────────────────────────────────────────────────────────────────────

SYSTEM_TOKENS: list[dict] = [
    # ══════════════════════════════════════════════════════════════════════
    #  CONDITION — METRICS (number-producing)
    # ══════════════════════════════════════════════════════════════════════
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'pnl',
     'value_type': 'number', 'units': '₹',
     'description': 'Positions mark-to-market P&L in ₹ for the selected scope.',
     'resolver': 'backend.api.algo.grammar._metric_pnl'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'pnl_pct',
     'value_type': 'number', 'units': '%',
     'description': 'Positions P&L as percent of used margin. Undefined when no open positions.',
     'resolver': 'backend.api.algo.grammar._metric_pnl_pct'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'day_val',
     'value_type': 'number', 'units': '₹',
     'description': 'Holdings day-change value in ₹ for the selected scope.',
     'resolver': 'backend.api.algo.grammar._metric_day_val'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'day_pct',
     'value_type': 'number', 'units': '%',
     'description': 'Holdings day-change percentage for the selected scope.',
     'resolver': 'backend.api.algo.grammar._metric_day_pct'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'inv_val',
     'value_type': 'number', 'units': '₹',
     'description': 'Holdings invested value (cost basis).',
     'resolver': 'backend.api.algo.grammar._metric_inv_val'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'cur_val',
     'value_type': 'number', 'units': '₹',
     'description': 'Holdings current market value.',
     'resolver': 'backend.api.algo.grammar._metric_cur_val'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'cash',
     'value_type': 'number', 'units': '₹',
     'description': 'Available cash on the funds row.',
     'resolver': 'backend.api.algo.grammar._metric_cash'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'avail_margin',
     'value_type': 'number', 'units': '₹',
     'description': 'Net available margin.',
     'resolver': 'backend.api.algo.grammar._metric_avail_margin'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'used_margin',
     'value_type': 'number', 'units': '₹',
     'description': 'Utilised margin (positions denominator for pnl_pct).',
     'resolver': 'backend.api.algo.grammar._metric_used_margin'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'collateral',
     'value_type': 'number', 'units': '₹',
     'description': 'Collateral component of margin.',
     'resolver': 'backend.api.algo.grammar._metric_collateral'},
    # Rate-of-change metrics (computed over the rolling history held by the engine)
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'pnl_rate_abs',
     'value_type': 'number', 'units': '₹/min',
     'description': 'Positions P&L rate of change in ₹ per minute over the last window.',
     'resolver': 'backend.api.algo.grammar._metric_pnl_rate_abs'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'pnl_rate_pct',
     'value_type': 'number', 'units': '%/min',
     'description': 'Positions P&L rate of change in percent per minute over the last window.',
     'resolver': 'backend.api.algo.grammar._metric_pnl_rate_pct'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'day_rate_abs',
     'value_type': 'number', 'units': '₹/min',
     'description': 'Holdings day-change rate of change in ₹ per minute over the last window.',
     'resolver': 'backend.api.algo.grammar._metric_day_rate_abs'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'day_rate_pct',
     'value_type': 'number', 'units': '%/min',
     'description': 'Holdings day-change rate of change in percent per minute over the last window.',
     'resolver': 'backend.api.algo.grammar._metric_day_rate_pct'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'minutes_since_open',
     'value_type': 'number', 'units': 'min',
     'description': 'Minutes since the first market segment opened today.',
     'resolver': 'backend.api.algo.grammar._metric_minutes_since_open'},
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'minutes_until_close',
     'value_type': 'number', 'units': 'min',
     'description': 'Minutes until the nearest market segment close.',
     'resolver': 'backend.api.algo.grammar._metric_minutes_until_close'},

    # ══════════════════════════════════════════════════════════════════════
    #  CONDITION — SCOPES (row selectors)
    # ══════════════════════════════════════════════════════════════════════
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'holdings.total',
     'value_type': 'object',
     'description': 'The single TOTAL row of the holdings summary.',
     'resolver': 'backend.api.algo.grammar._scope_holdings_total'},
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'holdings.any_acct',
     'value_type': 'array',
     'description': 'Every non-TOTAL account row of the holdings summary (leaf is OR-combined).',
     'resolver': 'backend.api.algo.grammar._scope_holdings_any_acct'},
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'positions.total',
     'value_type': 'object',
     'description': 'The single TOTAL row of the positions summary.',
     'resolver': 'backend.api.algo.grammar._scope_positions_total'},
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'positions.any_acct',
     'value_type': 'array',
     'description': 'Every non-TOTAL account row of the positions summary (leaf is OR-combined).',
     'resolver': 'backend.api.algo.grammar._scope_positions_any_acct'},
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'funds.total',
     'value_type': 'object',
     'description': 'The single TOTAL row of the funds/margins dataframe.',
     'resolver': 'backend.api.algo.grammar._scope_funds_total'},
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'funds.any_acct',
     'value_type': 'array',
     'description': 'Every non-TOTAL account row of the funds dataframe (leaf is OR-combined).',
     'resolver': 'backend.api.algo.grammar._scope_funds_any_acct'},
    # Worst-case scopes — collapse N per-account agents into 1 by selecting
    # the single biggest-loser row each tick. Pair with the standard day_pct
    # / pnl_pct / pnl metrics. Operator-facing benefit: one agent, one
    # threshold, one notification per fire.
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'holdings.worst_acct',
     'value_type': 'object',
     'description': 'The single account row with the most-negative day_pct in holdings.',
     'resolver': 'backend.api.algo.grammar._scope_holdings_worst_acct'},
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'holdings.worst_symbol',
     'value_type': 'object',
     'description': 'The single per-symbol holding row with the most-negative day_pct.',
     'resolver': 'backend.api.algo.grammar._scope_holdings_worst_symbol'},
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'positions.worst_acct',
     'value_type': 'object',
     'description': 'The single account row with the most-negative pnl_pct in positions.',
     'resolver': 'backend.api.algo.grammar._scope_positions_worst_acct'},
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'positions.worst_symbol',
     'value_type': 'object',
     'description': 'The single per-symbol position row with the most-negative pnl.',
     'resolver': 'backend.api.algo.grammar._scope_positions_worst_symbol'},

    # ── Watchlist metric ─────────────────────────────────────────────
    {'grammar_kind': 'condition', 'token_kind': 'metric', 'token': 'ltp',
     'value_type': 'number', 'units': '₹',
     'description': 'Last-traded price of a watchlist row (or any row with a last_price column).',
     'resolver': 'backend.api.algo.grammar._metric_ltp'},

    # ── Watchlist scopes ─────────────────────────────────────────────
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'watchlist.all',
     'value_type': 'array',
     'description': 'Every row across every watchlist the operator owns (leaf is OR-combined per row).',
     'resolver': 'backend.api.algo.grammar._scope_watchlist_all'},
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'watchlist.default',
     'value_type': 'array',
     'description': "Rows in the operator's Default watchlist.",
     'resolver': 'backend.api.algo.grammar._scope_watchlist_default'},
    {'grammar_kind': 'condition', 'token_kind': 'scope', 'token': 'watchlist.markets',
     'value_type': 'array',
     'description': "Rows in the auto-seeded Markets watchlist (indices + MCX commodities).",
     'resolver': 'backend.api.algo.grammar._scope_watchlist_markets'},

    # ══════════════════════════════════════════════════════════════════════
    #  CONDITION — OPERATORS (leaf comparators)
    # ══════════════════════════════════════════════════════════════════════
    {'grammar_kind': 'condition', 'token_kind': 'operator', 'token': '<',
     'value_type': 'boolean', 'description': 'Strictly less than.'},
    {'grammar_kind': 'condition', 'token_kind': 'operator', 'token': '<=',
     'value_type': 'boolean', 'description': 'Less than or equal to.'},
    {'grammar_kind': 'condition', 'token_kind': 'operator', 'token': '>',
     'value_type': 'boolean', 'description': 'Strictly greater than.'},
    {'grammar_kind': 'condition', 'token_kind': 'operator', 'token': '>=',
     'value_type': 'boolean', 'description': 'Greater than or equal to.'},
    {'grammar_kind': 'condition', 'token_kind': 'operator', 'token': '==',
     'value_type': 'boolean', 'description': 'Equal to.'},
    {'grammar_kind': 'condition', 'token_kind': 'operator', 'token': '!=',
     'value_type': 'boolean', 'description': 'Not equal to.'},
    {'grammar_kind': 'condition', 'token_kind': 'operator', 'token': 'in',
     'value_type': 'boolean',
     'description': 'Membership test. The RHS must be an array literal.'},
    {'grammar_kind': 'condition', 'token_kind': 'operator', 'token': 'not_in',
     'value_type': 'boolean',
     'description': 'Non-membership test. The RHS must be an array literal.'},
    {'grammar_kind': 'condition', 'token_kind': 'operator', 'token': 'between',
     'value_type': 'boolean',
     'description': 'Range test, inclusive. RHS is a [min, max] literal.'},

    # ══════════════════════════════════════════════════════════════════════
    #  NOTIFY — CHANNELS (how the alert is delivered)
    # ══════════════════════════════════════════════════════════════════════
    {'grammar_kind': 'notify', 'token_kind': 'channel', 'token': 'telegram',
     'value_type': 'enum',
     'description': 'Telegram group defined by secrets.telegram_chat_id.'},
    {'grammar_kind': 'notify', 'token_kind': 'channel', 'token': 'email',
     'value_type': 'enum',
     'description': 'Email to every address in secrets.alert_emails.'},
    {'grammar_kind': 'notify', 'token_kind': 'channel', 'token': 'websocket',
     'value_type': 'enum',
     'description': 'Live push to connected /algo dashboard clients.'},
    {'grammar_kind': 'notify', 'token_kind': 'channel', 'token': 'log',
     'value_type': 'enum',
     'description': 'Write to the app log only — useful for silent testing of new agents.'},

    # ══════════════════════════════════════════════════════════════════════
    #  NOTIFY — FORMATS (how the alert body is rendered)
    # ══════════════════════════════════════════════════════════════════════
    {'grammar_kind': 'notify', 'token_kind': 'format', 'token': 'text_narrow',
     'value_type': 'enum',
     'description': 'Two-line-per-row fixed-width monospace — sized for phone-width Telegram.'},
    {'grammar_kind': 'notify', 'token_kind': 'format', 'token': 'html_table',
     'value_type': 'enum',
     'description': 'Structured HTML table with per-kind row colour — for email.'},
    {'grammar_kind': 'notify', 'token_kind': 'format', 'token': 'plain_text',
     'value_type': 'enum',
     'description': 'Minimal single-line-per-row text.'},
    {'grammar_kind': 'notify', 'token_kind': 'format', 'token': 'json',
     'value_type': 'enum',
     'description': 'Machine-readable JSON — for webhook channels.'},

    # ══════════════════════════════════════════════════════════════════════
    #  NOTIFY — TEMPLATES (default message bodies, overridable per agent)
    # ══════════════════════════════════════════════════════════════════════
    {'grammar_kind': 'notify', 'token_kind': 'template', 'token': 'alert_loss_default',
     'value_type': 'string',
     'description': 'Default body for P&L-loss alerts — a list of triggered rows.',
     'template_body':
        "Alert — ${timestamp}\n\n"
        "${row_lines}"},
    {'grammar_kind': 'notify', 'token_kind': 'template', 'token': 'deploy_ok_default',
     'value_type': 'string',
     'description': 'Default deploy-ok ping body.',
     'template_body':
        "Deploy OK${branch_tag}\n${timestamp}\n${service_status}"},

    # ══════════════════════════════════════════════════════════════════════
    #  ACTION — ACTION TYPES (what the alert makes happen)
    # ══════════════════════════════════════════════════════════════════════
    {'grammar_kind': 'action', 'token_kind': 'action_type', 'token': 'place_order',
     'value_type': 'void',
     'description': 'Place a new broker order with the supplied parameters.',
     'resolver': 'backend.api.algo.actions.place_order',
     'params_schema': {
         'account':       {'type': 'string',  'required': True,  'token_ref_ok': True,
                           'description': 'Masked account id to route the order to (e.g. ZG####).'},
         'symbol':        {'type': 'string',  'required': True,
                           'description': 'Tradingsymbol, e.g. NIFTY26APR22500CE or RELIANCE.'},
         'exchange':      {'type': 'enum',    'enum': ['NSE','BSE','NFO','CDS','MCX'],
                           'required': False, 'default': 'NFO'},
         'side':          {'type': 'enum',    'enum': ['BUY','SELL'], 'required': True,
                           'description': 'BUY opens long / covers short; SELL opens short / closes long.'},
         'qty':           {'type': 'number',  'required': True,  'token_ref_ok': True,
                           'description': 'Number of lots × lot size. Must be positive.'},
         'order_type':    {'type': 'enum',    'enum': ['MARKET','LIMIT','SL','SL-M'],
                           'required': False, 'default': 'MARKET'},
         'price':         {'type': 'number',  'required': False, 'token_ref_ok': True,
                           'description': 'Required for LIMIT / SL.'},
         'trigger_price': {'type': 'number',  'required': False, 'token_ref_ok': True,
                           'description': 'Required for SL / SL-M.'},
         'product':       {'type': 'enum',    'enum': ['MIS','CNC','NRML'],
                           'required': False, 'default': 'MIS'},
         'variety':       {'type': 'enum',    'enum': ['regular','amo','co','iceberg','auction'],
                           'required': False, 'default': 'regular'},
         'tag':           {'type': 'string',  'required': False,
                           'description': 'Free-form tag propagated into the broker order id and AlgoOrder row.'},
     }},

    {'grammar_kind': 'action', 'token_kind': 'action_type', 'token': 'modify_order',
     'value_type': 'void',
     'description': 'Modify an existing open broker order by broker_order_id.',
     'resolver': 'backend.api.algo.actions.modify_order',
     'params_schema': {
         'account':          {'type': 'string', 'required': True},
         'broker_order_id':  {'type': 'string', 'required': True},
         'new_price':        {'type': 'number', 'required': False, 'token_ref_ok': True},
         'new_qty':          {'type': 'number', 'required': False, 'token_ref_ok': True},
         'new_trigger':      {'type': 'number', 'required': False, 'token_ref_ok': True},
     }},

    {'grammar_kind': 'action', 'token_kind': 'action_type', 'token': 'cancel_order',
     'value_type': 'void',
     'description': 'Cancel a specific open broker order by broker_order_id.',
     'resolver': 'backend.api.algo.actions.cancel_order',
     'params_schema': {
         'account':          {'type': 'string', 'required': True},
         'broker_order_id':  {'type': 'string', 'required': True},
         'variety':          {'type': 'enum',   'enum': ['regular','amo','co','iceberg','auction'],
                              'required': False, 'default': 'regular'},
     }},

    {'grammar_kind': 'action', 'token_kind': 'action_type', 'token': 'cancel_all_orders',
     'value_type': 'void',
     'description': 'Cancel every pending/open order matching the scope filter.',
     'resolver': 'backend.api.algo.actions.cancel_all_orders',
     'params_schema': {
         'scope':            {'type': 'enum', 'enum': ['total','account'], 'default': 'total'},
         'account':          {'type': 'string', 'required': False,
                              'description': 'Required when scope=account.'},
         'side':             {'type': 'enum', 'enum': ['BUY','SELL'], 'required': False},
         'symbol':           {'type': 'string', 'required': False,
                              'description': 'If set, cancel only orders on this tradingsymbol.'},
     }},

    {'grammar_kind': 'action', 'token_kind': 'action_type', 'token': 'chase_close_positions',
     'value_type': 'void',
     'description': 'Close every open position in scope using the adaptive limit-order chase engine.',
     'resolver': 'backend.api.algo.actions.chase_close_positions',
     'params_schema': {
         'scope':            {'type': 'enum', 'enum': ['total','account'], 'default': 'total'},
         'account':          {'type': 'string', 'required': False},
         'timeout_minutes':  {'type': 'number', 'default': 10,
                              'description': 'Bail out if not filled within this many minutes.'},
         'adjust_pct':       {'type': 'number', 'default': 0.1,
                              'description': 'Percent of spread to adjust on each chase step.'},
     }},

    # Simpler one-shot close of a specific position — LIMIT order at the
    # instrument's current LTP. Side is derived from position direction
    # (long → SELL to flatten, short → BUY to cover); operators can still
    # override via `side`. In the simulator, the order is paper-traded:
    # an AlgoOrder row is written with mode='sim' and initial_price = LTP
    # at the moment the agent fired, so operators can see exactly what
    # price the engine would have used.
    {'grammar_kind': 'action', 'token_kind': 'action_type', 'token': 'close_position',
     'value_type': 'void',
     'description': "Close a single open position with a LIMIT order at current LTP. "
                    "In sim mode, records a paper AlgoOrder with the sim's current LTP "
                    "so the trade price is visible in order logs.",
     'resolver': 'backend.api.algo.actions.close_position',
     'params_schema': {
         'account':          {'type': 'string',  'required': True,  'token_ref_ok': True,
                              'description': 'Masked account id (e.g. ZG####).'},
         'symbol':           {'type': 'string',  'required': True,
                              'description': 'Tradingsymbol to close. Must match an open position.'},
         'exchange':         {'type': 'enum',    'enum': ['NSE','BSE','NFO','CDS','MCX'],
                              'required': False, 'default': 'NFO'},
         'quantity':         {'type': 'number',  'required': False,  'token_ref_ok': True,
                              'description': 'Partial close. Omit to flatten the full position.'},
         'side':             {'type': 'enum',    'enum': ['BUY','SELL'], 'required': False,
                              'description': 'Override auto-derived side. Default: long → SELL, short → BUY.'},
         'product':          {'type': 'enum',    'enum': ['MIS','CNC','NRML'],
                              'required': False, 'default': 'NRML'},
     }},

    {'grammar_kind': 'action', 'token_kind': 'action_type', 'token': 'monitor_order',
     'value_type': 'void',
     'description': 'Poll an order until it fills or times out, then trigger on_fill / on_timeout actions.',
     'resolver': 'backend.api.algo.actions.monitor_order',
     'params_schema': {
         'account':          {'type': 'string', 'required': True},
         'broker_order_id':  {'type': 'string', 'required': True},
         'timeout_minutes':  {'type': 'number', 'default': 5},
         'on_fill':          {'type': 'array',  'required': False,
                              'description': 'List of action_spec objects to run after fill.'},
         'on_timeout':       {'type': 'array',  'required': False,
                              'description': 'List of action_spec objects to run on timeout.'},
     }},

    {'grammar_kind': 'action', 'token_kind': 'action_type', 'token': 'deactivate_agent',
     'value_type': 'void',
     'description': 'Pause the agent that fired — useful for one-shot safety rules.',
     'resolver': 'backend.api.algo.actions.deactivate_agent',
     'params_schema': {}},

    {'grammar_kind': 'action', 'token_kind': 'action_type', 'token': 'set_flag',
     'value_type': 'void',
     'description': 'Set a named runtime flag that other agents can read via the condition grammar.',
     'resolver': 'backend.api.algo.actions.set_flag',
     'params_schema': {
         'name':  {'type': 'string',  'required': True},
         'value': {'type': 'boolean', 'required': True},
     }},

    {'grammar_kind': 'action', 'token_kind': 'action_type', 'token': 'emit_log',
     'value_type': 'void',
     'description': 'Write a message to the app log (quiet action for testing agent wiring).',
     'resolver': 'backend.api.algo.actions.emit_log',
     'params_schema': {
         'level':   {'type': 'enum',   'enum': ['info','warning','error'], 'default': 'info'},
         'message': {'type': 'string', 'required': True, 'token_ref_ok': True},
     }},
]


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
                row.params_schema = spec.get('params_schema', row.params_schema)
                row.enum_values   = spec.get('enum_values',   row.enum_values)
                row.template_body = spec.get('template_body', row.template_body)
                updated += 1
        await s.commit()
        logger.info(f"Grammar tokens seeded — inserted={inserted} updated={updated}")
