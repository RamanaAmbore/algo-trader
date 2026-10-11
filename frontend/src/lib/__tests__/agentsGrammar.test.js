/**
 * agentsGrammar.test.js — Vitest coverage for frontend/src/lib/command/
 * grammars/agents.js (Sprint 2, agent CLI grammar).
 *
 * Uses a FIXTURE catalog (via buildCatalog()) rather than the live backend
 * catalog for every test — per the file's own documented "Known gaps"
 * section, several canonical design-doc examples (`ntfy`, a boolean-typed
 * metric, an enum-ish `side` metric) don't exist in the real catalog yet.
 * The fixture below declares them so the GENERIC mechanism is exercised
 * exactly as specified.
 *
 * Interpretation notes (documented here since the design doc's own
 * examples are ambiguous on these two points — see agents.js's "Known
 * gaps" header comment for the full reasoning):
 *   - `account` is a free-text `string` field (not enum) in the real
 *     `place_order` schema — per the grammar's own "enum values are bare,
 *     free-text values are quoted" rule, a bare `account` (as the design
 *     doc's placeholder-style examples literally show it) is REJECTED by
 *     this compiler; tests below use a realistic quoted `account="ZG0790"`.
 *   - `side` is enum-typed; both bare (`side=SELL`) and quoted
 *     (`side="SELL"`) spellings are accepted (case-insensitive, normalized
 *     to the catalog's own casing) — advisor-confirmed resolution of the
 *     apparent "enum values are bare" vs. the design doc's own
 *     `side="SELL"` example contradiction.
 */

import { describe, it, expect, vi } from 'vitest';

vi.mock('$lib/data/instruments', () => ({ getInstrument: vi.fn(() => null) }));
vi.mock('$lib/api', () => ({ fetchGrammarTokens: vi.fn() }));

const {
  buildCatalog, fetchAgentCatalog, parseStatement, compileStatement,
  compileAgentCliStatement, validateAgentCliStatement, lexAgentStatement,
  parseCall, parseValue, parseLeaf, ALWAYS_LEAF, KWARG_ALIASES,
  suggestAgentCliAt, decompileCondition, describeErrorPosition,
} = await import('$lib/command/grammars/agents.js');
const { fetchGrammarTokens } = await import('$lib/api');

// ── Fixture catalog ─────────────────────────────────────────────────────

const FIXTURE_ROWS = [
  // metrics
  { grammar_kind: 'condition', token_kind: 'metric', token: 'pnl', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'pnl_pct', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'day_pct', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'avail_margin', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'mean_pnl', value_type: 'number', params_schema: { minutes: { type: 'number' } } },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'stdev_pnl', value_type: 'number', params_schema: { minutes: { type: 'number' } } },
  // fixture-only boolean metric (none exists in the live catalog today — see "Known gaps")
  { grammar_kind: 'condition', token_kind: 'metric', token: 'is_itm', value_type: 'boolean', params_schema: null },
  // fixture-only non-numeric metric, for the generic `in`/`not_in` mechanism
  { grammar_kind: 'condition', token_kind: 'metric', token: 'side', value_type: 'string', params_schema: null },
  // scopes
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.total', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.any_acct', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'holdings.total', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'funds.total', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'funds.any_acct', params_schema: null },
  // channels — `gate` (Sprint 18) is the real catalog shape on
  // telegram/email/ntfy (see agent_grammar.yaml).
  { grammar_kind: 'notify', token_kind: 'channel', token: 'telegram', params_schema: { gate: { type: 'boolean', required: false } } },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'email', params_schema: { gate: { type: 'boolean', required: false } } },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'log', params_schema: null },
  // fixture-only `ntfy` (used in the design doc's own canonical example 2,
  // but absent from the live catalog today — see "Known gaps")
  { grammar_kind: 'notify', token_kind: 'channel', token: 'ntfy', params_schema: { priority: { type: 'enum', enum: ['low', 'default', 'urgent'], required: false }, gate: { type: 'boolean', required: false } } },
  // Sprint 18 — the synthetic `render_action` row (backend's
  // `routes/grammar.py:_render_action_catalog_rows`), declaring the
  // kind='event' agent render action's own optional `gate` sibling key.
  { grammar_kind: 'action', token_kind: 'render_action', token: 'render', params_schema: { gate: { type: 'boolean', required: false } } },
  // actions — place_order mirrors the REAL agent_grammar.yaml schema,
  // preserving its real declared key order (account first).
  {
    grammar_kind: 'action', token_kind: 'action_type', token: 'place_order',
    params_schema: {
      account: { type: 'string', required: true, token_ref_ok: true },
      symbol: { type: 'string', required: true },
      exchange: { type: 'enum', enum: ['NSE', 'BSE', 'NFO', 'CDS', 'MCX'], required: false, default: 'NFO' },
      side: { type: 'enum', enum: ['BUY', 'SELL'], required: true },
      qty: { type: 'number', required: true, token_ref_ok: true },
      order_type: { type: 'enum', enum: ['MARKET', 'LIMIT', 'SL', 'SL-M'], required: false, default: 'MARKET' },
      // Sprint 17 — mirrors the REAL agent_grammar.yaml's confirmed
      // `required_if` specs (price: "Required for LIMIT / SL", trigger_price:
      // "Required for SL / SL-M" — see backend/tests/test_grammar_catalog_place_order_constraints.py).
      price: { type: 'number', required: false, token_ref_ok: true, required_if: { field: 'order_type', in: ['LIMIT', 'SL'] } },
      trigger_price: { type: 'number', required: false, token_ref_ok: true, required_if: { field: 'order_type', in: ['SL', 'SL-M'] } },
      product: { type: 'enum', enum: ['MIS', 'CNC', 'NRML'], required: false, default: 'MIS' },
      variety: { type: 'enum', enum: ['regular', 'amo', 'co', 'iceberg', 'auction'], required: false, default: 'regular' },
      tag: { type: 'string', required: false },
      chase_level: { type: 'enum', enum: ['LOW', 'MED', 'HIGH'], required: false },
      template_id: { type: 'number', required: false },
      tp_pct_override: { type: 'number', required: false },
      sl_pct_override: { type: 'number', required: false },
      wing_premium_pct_override: { type: 'number', required: false },
      wing_strike_offset_override: { type: 'number', required: false },
    },
    // Sprint 17 — token-level (sibling of params_schema), mirrors the real
    // backend's declared place_order mutex group.
    mutually_exclusive_groups: [['wing_premium_pct_override', 'wing_strike_offset_override']],
  },
  {
    grammar_kind: 'action', token_kind: 'action_type', token: 'emit_log',
    params_schema: {
      level: { type: 'enum', enum: ['info', 'warning', 'error'], required: false, default: 'info' },
      message: { type: 'string', required: true, token_ref_ok: true },
    },
  },
  { grammar_kind: 'action', token_kind: 'action_type', token: 'deactivate_agent', params_schema: {} },
  // Sprint 10 — merged-in AgentTemplate ("Token") rows, hyphenated names
  // (the real seeding convention — e.g. `notify-critical-trio`,
  // `loss-positions-acct-default`). `is_fragment_ref: true` is the ONLY
  // thing that distinguishes these from a real metric/channel row —
  // everything else about their catalog shape is identical.
  { grammar_kind: 'condition', token_kind: 'metric', token: 'loss-positions-default', is_fragment_ref: true, params_schema: null },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'notify-critical-trio', is_fragment_ref: true, params_schema: null },
  // Sprint 15 — appended at the END (not spliced in among the earlier
  // rows) so the existing `fetchAgentCatalog()` test's `FIXTURE_ROWS[13]`/
  // `FIXTURE_ROWS[17]` index references stay valid.
  //
  // A real multi-segment SCOPE (positions.expiring_today.mcx_unhedged is
  // the live catalog's own canonical example) — needed for the
  // longest-scope-chain-match-first regression guard: this chain must
  // keep resolving as a SCOPE, never get misparsed as a shorter scope +
  // a trailing column.
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.expiring_today', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.expiring_today.mcx_unhedged', params_schema: null },
  // Sprint 15 — `token_kind: 'column'` rows (the per-scope exposed-column
  // registry projection). `token` is the compound `<scope>.<exposed_name>`
  // string per the backend's chosen shape; `label` is the short,
  // operator-facing display text — mirrors the REAL registry entries in
  // `backend/api/algo/grammar.py` (`_POSITIONS_SUMMARY_COLUMNS`/
  // `_POSITION_ROW_COLUMNS`) exactly, so a compiled/decompiled fixture
  // test reads the same as it would against the live catalog.
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'positions.total.pnl', value_type: 'number',
    label: 'P&L (₹)',
    description: "P&L (₹). Sourced from DataFrame column 'pnl' on ctx.sum_positions, populated by backend.api.algo.grammar._scope_positions_total.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'positions.total.account_id', value_type: 'string',
    label: 'Account',
    description: "Account. Sourced from DataFrame column 'account' on ctx.sum_positions, populated by backend.api.algo.grammar._scope_positions_total.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'positions.expiring_today.qty', value_type: 'number',
    label: 'Quantity (Contracts)',
    description: "Quantity (Contracts). Sourced from DataFrame column 'quantity' on ctx.position_rows, populated by backend.api.algo.grammar._scope_positions_expiring_today.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'positions.expiring_today.symbol', value_type: 'string',
    label: 'Symbol',
    description: "Symbol. Sourced from DataFrame column 'tradingsymbol' on ctx.position_rows, populated by backend.api.algo.grammar._scope_positions_expiring_today.",
  },
  // Same column, under the DEEPER mcx_unhedged scope — needed to confirm
  // a 4-segment chain (3-segment scope + 1 column) resolves with the
  // FULL 3-segment scope, not a shorter 2-segment guess.
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'positions.expiring_today.mcx_unhedged.pnl', value_type: 'number',
    label: 'P&L (₹)',
    description: "P&L (₹). Sourced from DataFrame column 'pnl' on ctx.position_rows, populated by backend.api.algo.grammar._scope_positions_expiring_today_mcx_unhedged.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'funds.total.option_premium', value_type: 'number',
    label: 'Option Premium',
    description: "Option Premium. Sourced from DataFrame column 'util option_premium' on ctx.df_margins, populated by backend.api.algo.grammar._scope_funds_total.",
  },
  // Sprint 16 — appended at the END (same append-only convention as the
  // Sprint 15 rows above) for matched-row ($row) binding coverage. Reuses
  // the EXISTING `pnl` metric (already registered above) for every new
  // WHEN leaf below rather than introducing a new metric token — a new
  // `qty` metric would collide with the pre-existing
  // `positions.expiring_today.qty` COLUMN row (Sprint 15) and change
  // that scope's decompile-ambiguity behavior for an unrelated test.
  //
  // `close_position` mirrors the REAL agent_grammar.yaml schema (account/
  // symbol are both required free-text strings — see backend/config/
  // grammars/agent_grammar.yaml's own close_position entry).
  {
    grammar_kind: 'action', token_kind: 'action_type', token: 'close_position',
    params_schema: {
      account: { type: 'string', required: true, token_ref_ok: true },
      symbol: { type: 'string', required: true },
    },
  },
  // `account_id` exposed on BOTH the per-row `positions.expiring_today`
  // scope AND the account-level-summary `positions.any_acct` scope —
  // matches the real backend's `_POSITION_ROW_COLUMNS`/
  // `_POSITIONS_SUMMARY_COLUMNS` (both carry `account_id`) — needed so
  // the "any_acct lacks symbol, NOT account_id" guard-2 test exercises a
  // real asymmetry, not a scope with zero columns registered at all.
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'positions.expiring_today.account_id', value_type: 'string',
    label: 'Account',
    description: "Account. Sourced from DataFrame column 'account' on ctx.position_rows, populated by backend.api.algo.grammar._scope_positions_expiring_today.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'positions.any_acct.account_id', value_type: 'string',
    label: 'Account',
    description: "Account. Sourced from DataFrame column 'account' on ctx.sum_positions, populated by backend.api.algo.grammar._scope_positions_any_acct.",
  },
  // Watchlist scope — guard 3 (watchlist rejection) fixture. `symbol` IS
  // a real exposed column on this scope (mirrors the live
  // `_WATCHLIST_COLUMNS` registry) — the point of the guard is that this
  // must still be rejected for a $row reference despite being exposed.
  { grammar_kind: 'condition', token_kind: 'scope', token: 'watchlist.all', params_schema: null },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'watchlist.all.symbol', value_type: 'string',
    label: 'Symbol',
    description: "Symbol. Sourced from DataFrame column 'tradingsymbol' on ctx.watchlist_rows, populated by backend.api.algo.grammar._scope_watchlist_all.",
  },
  // `funds.any_acct` also carries a real `account_id` (mirrors the live
  // `_FUNDS_COLUMNS` registry, which always includes `account_id`) — the
  // ALWAYS_LEAF sentinel's own scope is `funds.any_acct`, so this is
  // needed for the "bare DO with no WHEN" test to exercise the REAL
  // asymmetry (account_id exposed, symbol never is) instead of a scope
  // with zero columns registered at all.
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'funds.any_acct.account_id', value_type: 'string',
    label: 'Account',
    description: "Account. Sourced from DataFrame column 'account' on ctx.df_margins, populated by backend.api.algo.grammar._scope_funds_any_acct.",
  },
  // Sprint 19 — appended at the END (same append-only convention as every
  // earlier batch above) for qualifier-filter (`account:`/`symbol:`)
  // coverage. `positions.worst_acct` is a REAL existing scope token
  // (backend/api/algo/grammar.py's EXPOSED_COLUMNS) that `account:worst`
  // on the bare `positions` family aliases onto directly — a pure alias,
  // no `scope_filter` involved, so no new COLUMN row is needed for it.
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.worst_acct', params_schema: null },
  // Sprint 21 — appended at the END (same append-only convention as every
  // earlier batch above). `orders.any_acct` (account-rooted, one row per
  // currently open AlgoOrder) and `quote.tracked` (instrument-rooted,
  // pre-tracked ad-hoc market data) — real shapes confirmed against the
  // real backend diff (commit e8b1ad38, `backend/api/algo/grammar.py`'s
  // `_ORDERS_COLUMNS`/`_QUOTE_COLUMNS` + `agent_grammar.yaml`'s new scope
  // tokens), not just PLAN.md's summary.
  { grammar_kind: 'condition', token_kind: 'scope', token: 'orders.any_acct', params_schema: null },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'orders.any_acct.account_id', value_type: 'string',
    label: 'Account',
    description: "Account. Sourced from DataFrame column 'account' on ctx.orders_rows, populated by backend.api.algo.grammar._scope_orders_any_acct.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'orders.any_acct.symbol', value_type: 'string',
    label: 'Symbol',
    description: "Symbol. Sourced from DataFrame column 'tradingsymbol' on ctx.orders_rows, populated by backend.api.algo.grammar._scope_orders_any_acct.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'orders.any_acct.exchange', value_type: 'string',
    label: 'Exchange',
    description: "Exchange. Sourced from DataFrame column 'exchange' on ctx.orders_rows, populated by backend.api.algo.grammar._scope_orders_any_acct.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'orders.any_acct.side', value_type: 'string',
    label: 'Side (BUY/SELL)',
    description: "Side (BUY/SELL). Sourced from DataFrame column 'side' on ctx.orders_rows, populated by backend.api.algo.grammar._scope_orders_any_acct.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'orders.any_acct.qty', value_type: 'number',
    label: 'Quantity (Contracts)',
    description: "Quantity (Contracts). Sourced from DataFrame column 'quantity' on ctx.orders_rows, populated by backend.api.algo.grammar._scope_orders_any_acct.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'orders.any_acct.price', value_type: 'number',
    label: 'Order Price (₹)',
    description: "Order Price (₹). Sourced from DataFrame column 'price' on ctx.orders_rows, populated by backend.api.algo.grammar._scope_orders_any_acct.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'orders.any_acct.status', value_type: 'string',
    label: 'Order Status',
    description: "Order Status. Sourced from DataFrame column 'status' on ctx.orders_rows, populated by backend.api.algo.grammar._scope_orders_any_acct.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'orders.any_acct.order_id', value_type: 'string',
    label: 'Broker Order ID',
    description: "Broker Order ID. Sourced from DataFrame column 'order_id' on ctx.orders_rows, populated by backend.api.algo.grammar._scope_orders_any_acct.",
  },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'quote.tracked', params_schema: null },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'quote.tracked.symbol', value_type: 'string',
    label: 'Symbol',
    description: "Symbol. Sourced from DataFrame column 'tradingsymbol' on ctx.quote_rows, populated by backend.api.algo.grammar._scope_quote_tracked.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'quote.tracked.exchange', value_type: 'string',
    label: 'Exchange',
    description: "Exchange. Sourced from DataFrame column 'exchange' on ctx.quote_rows, populated by backend.api.algo.grammar._scope_quote_tracked.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'quote.tracked.ltp', value_type: 'number',
    label: 'LTP',
    description: "LTP. Sourced from DataFrame column 'last_price' on ctx.quote_rows, populated by backend.api.algo.grammar._scope_quote_tracked.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'quote.tracked.bid', value_type: 'number',
    label: 'Bid',
    description: "Bid. Sourced from DataFrame column 'bid' on ctx.quote_rows, populated by backend.api.algo.grammar._scope_quote_tracked.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'quote.tracked.ask', value_type: 'number',
    label: 'Ask',
    description: "Ask. Sourced from DataFrame column 'ask' on ctx.quote_rows, populated by backend.api.algo.grammar._scope_quote_tracked.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'quote.tracked.volume', value_type: 'number',
    label: 'Volume',
    description: "Volume. Sourced from DataFrame column 'volume' on ctx.quote_rows, populated by backend.api.algo.grammar._scope_quote_tracked.",
  },
  // `ltp` is ALSO a real registered metric token (`agent_grammar.yaml`'s
  // existing `token: ltp`, `grammar.py`'s `_metric_ltp`) — reused by
  // `quote.tracked.ltp` exactly like `watchlist.*` already reuses it, not
  // merely an exposed-column fallback. Needed for the `ltp@quote.tracked...`
  // ("@" form) spelling used below.
  { grammar_kind: 'condition', token_kind: 'metric', token: 'ltp', value_type: 'number', params_schema: null },
  // `cancel_order` mirrors the REAL agent_grammar.yaml schema. NOTE: the
  // real param key is `broker_order_id`, NOT `order_id` — PLAN.md's own
  // literal example text `cancel_order(order_id=order_id)` does not
  // compile against the real catalog for this reason (see this sprint's
  // handback report for the full explanation).
  {
    grammar_kind: 'action', token_kind: 'action_type', token: 'cancel_order',
    params_schema: {
      account: { type: 'string', required: true },
      broker_order_id: { type: 'string', required: true },
      variety: { type: 'enum', enum: ['regular', 'amo'], required: false },
    },
  },
];

const CATALOG = buildCatalog(FIXTURE_ROWS);

// Fixture lot sizes — NIFTY/BANKNIFTY-ish, so lots→qty tests are readable.
const LOT_SIZES = { NIFTY25JULFUT: 75, BANKNIFTY25JULFUT: 25 };
const lotSizeOf = (sym) => LOT_SIZES[sym] || null;
// Sprint 20 (item 2) — fixture exchange listing, mirroring `LOT_SIZES`'s
// pattern: both fixture symbols are single-listed NFO futures, so the
// new exchange-derivation logic in `_compileOrderCallToLeg` resolves
// unambiguously without every test needing its own `exchange=`/
// `exchangesOf` override.
// RELIANCE is deliberately dual-listed (NSE+BSE), for the ambiguous-
// exchange compile-error test below.
const EXCHANGES = { NIFTY25JULFUT: ['NFO'], BANKNIFTY25JULFUT: ['NFO'], RELIANCE: ['NSE', 'BSE'] };
const exchangesOf = (sym) => EXCHANGES[sym] || [];

function compile(text, opts = {}) {
  return compileAgentCliStatement(text, CATALOG, { lotSizeOf, exchangesOf, ...opts });
}

// ── buildCatalog() / fetchAgentCatalog() ────────────────────────────────

describe('buildCatalog()', () => {
  it('buckets rows by grammar_kind/token_kind and preserves declared param order', () => {
    expect(CATALOG.metrics.has('mean_pnl')).toBe(true);
    expect(CATALOG.actions.get('place_order').paramKeys[0]).toBe('account');
  });

  // Sprint 17 — the row-level wire field (`mutually_exclusive_groups`,
  // snake_case, exactly as `GrammarTokenOut` serializes it) must survive
  // into the catalog entry's own `mutuallyExclusiveGroups` — this is the
  // one link none of the compile-level tests below exercise directly
  // (they all read it off `CATALOG` already built at module load).
  it('surfaces the row-level mutually_exclusive_groups wire field on the catalog entry', () => {
    expect(CATALOG.actions.get('place_order').mutuallyExclusiveGroups).toEqual([
      ['wing_premium_pct_override', 'wing_strike_offset_override'],
    ]);
  });

  it('a row with no mutually_exclusive_groups key gets null, not []', () => {
    expect(CATALOG.actions.get('emit_log').mutuallyExclusiveGroups).toBeNull();
  });

  it('skips inactive rows', () => {
    const cat = buildCatalog([{ grammar_kind: 'notify', token_kind: 'channel', token: 'x', is_active: false }]);
    expect(cat.channels.has('x')).toBe(false);
  });

  // Sprint 18 — the synthetic `render_action` row (`token_kind:
  // 'render_action'`, NEVER 'action_type') surfaces as a single
  // `catalog.renderAction` entry, and critically does NOT land in
  // `catalog.actions` — the DO-dispatchable bucket. A cycle-agent's DO
  // list must never be tricked into accepting a bare `render` action it
  // has no handler for.
  it('a render_action row populates catalog.renderAction, never catalog.actions', () => {
    expect(CATALOG.renderAction).not.toBeNull();
    expect(CATALOG.renderAction.token).toBe('render');
    expect(CATALOG.renderAction.paramsSchema.gate.type).toBe('boolean');
    expect(CATALOG.actions.has('render')).toBe(false);
  });

  it('catalog.renderAction is null when no render_action row is present', () => {
    const cat = buildCatalog([{ grammar_kind: 'notify', token_kind: 'channel', token: 'telegram', params_schema: null }]);
    expect(cat.renderAction).toBeNull();
  });
});

describe('fetchAgentCatalog()', () => {
  it('fetches all three grammar kinds and merges into one catalog', async () => {
    vi.mocked(fetchGrammarTokens).mockImplementation(async (kind) => {
      if (kind === 'condition') return [FIXTURE_ROWS[0]];
      if (kind === 'notify') return [FIXTURE_ROWS[13]];
      return [FIXTURE_ROWS[18]];
    });
    const cat = await fetchAgentCatalog();
    expect(cat.metrics.has('pnl')).toBe(true);
    expect(cat.channels.has('telegram')).toBe(true);
    expect(cat.actions.has('place_order')).toBe(true);
  });
});

// ── The 8 canonical design-doc examples — exact compiled JSON ──────────

describe('canonical examples — exact compiled JSON', () => {
  it('1. single channel, single action', () => {
    const r = compile(
      'WHEN mean_pnl(minutes=30)@positions.total <= -50000 ' +
      'ALERT telegram ' +
      'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );
    expect(r.errors).toEqual([]);
    expect(r.ok).toBe(true);
    expect(r.agent).toEqual({
      conditions: { metric: 'mean_pnl(30)', scope: 'positions.total', op: '<=', value: -50000 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'place_order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'SELL', qty: 75 } }],
    });
  });

  it('2. multiple channels (one parameterized), multiple actions', () => {
    const r = compile(
      'WHEN mean_pnl(minutes=30)@positions.total <= -50000 ' +
      'ALERT telegram, ntfy(priority="urgent") ' +
      'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1), emit_log(message="stop hit")'
    );
    expect(r.errors).toEqual([]);
    expect(r.agent.events).toEqual([
      { channel: 'telegram', enabled: true },
      { channel: 'ntfy', enabled: true, priority: 'urgent' },
    ]);
    expect(r.agent.actions).toEqual([
      { type: 'place_order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'SELL', qty: 75 } },
      { type: 'emit_log', params: { message: 'stop hit' } },
    ]);
  });

  it('3. compound boolean — equal precedence, parens control grouping', () => {
    const r = compile(
      'WHEN ((mean_pnl(minutes=30)@positions.total <= -50000) & (stdev_pnl(minutes=30)@positions.total > 10000)) | (avail_margin@funds.total < 0) ' +
      'ALERT telegram ' +
      'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );
    expect(r.errors).toEqual([]);
    expect(r.agent.conditions).toEqual({
      any: [
        { all: [
          { metric: 'mean_pnl(30)', scope: 'positions.total', op: '<=', value: -50000 },
          { metric: 'stdev_pnl(30)', scope: 'positions.total', op: '>', value: 10000 },
        ] },
        { metric: 'avail_margin', scope: 'funds.total', op: '<', value: 0 },
      ],
    });
  });

  it('4. NOT', () => {
    const r = compile(
      'WHEN ~(pnl_pct@positions.total > 0) ' +
      'ALERT telegram ' +
      'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );
    expect(r.errors).toEqual([]);
    expect(r.agent.conditions).toEqual({ not: { metric: 'pnl_pct', scope: 'positions.total', op: '>', value: 0 } });
  });

  it('5a. only an action (ALERT nop)', () => {
    const r = compile(
      'WHEN mean_pnl(minutes=30)@positions.total <= -50000 ALERT nop ' +
      'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );
    expect(r.errors).toEqual([]);
    expect(r.agent.events).toEqual([]);
    expect(r.agent.actions).toEqual([
      { type: 'place_order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'SELL', qty: 75 } },
    ]);
  });

  it('5b. only a notify (DO nop)', () => {
    const r = compile('WHEN pnl_pct@positions.total <= -5 ALERT telegram DO nop');
    expect(r.errors).toEqual([]);
    expect(r.agent.events).toEqual([{ channel: 'telegram', enabled: true }]);
    expect(r.agent.actions).toEqual([]);
  });

  it('6. always + nop', () => {
    const r = compile('WHEN always ALERT nop DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="BUY", lots=2)');
    expect(r.errors).toEqual([]);
    expect(r.agent.conditions).toEqual(ALWAYS_LEAF);
    expect(r.agent.actions).toEqual([
      { type: 'place_order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'BUY', qty: 150 } },
    ]);
  });

  it('7. bare order — single ticket shape', () => {
    // Sprint 20 (item 2) — `price=` is now required at compile time for
    // the actually-emitted LIMIT order_type default (previously silently
    // compiled with `price: null`, failing only later at the broker).
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100)', { mode: 'paper' });
    expect(r.errors).toEqual([]);
    expect(r.kind).toBe('order');
    expect(r.basket).toBeNull();
    expect(r.ticket).toEqual({
      mode: 'paper', account: 'ZG0790', side: 'SELL', tradingsymbol: 'NIFTY25JULFUT', quantity: 1,
      exchange: 'NFO', product: 'NRML', order_type: 'LIMIT', variety: 'regular',
      price: 100, trigger_price: null, chase: false, chase_aggressiveness: 'low',
      template_id: null, tp_pct_override: null, sl_pct_override: null,
      wing_premium_pct_override: null, wing_strike_offset_override: null,
      lot_size_hint: 75,
    });
  });

  it('8. bare multiple orders — basket shape, grouped by account', () => {
    const r = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100), ' +
      'order(account="ZJ6294", symbol="BANKNIFTY25JULFUT", side="BUY", lots=1, price=200)'
    );
    expect(r.errors).toEqual([]);
    expect(r.ticket).toBeNull();
    expect(r.basket).toEqual({
      groups: [
        { account: 'ZG0790', legs: [{
          transaction_type: 'SELL', tradingsymbol: 'NIFTY25JULFUT', quantity: 1,
          exchange: 'NFO', product: 'NRML', order_type: 'LIMIT', variety: 'regular',
          price: 100, trigger_price: null, chase: false, chase_aggressiveness: 'low',
          template_id: null, tp_pct_override: null, sl_pct_override: null,
          wing_premium_pct_override: null, wing_strike_offset_override: null, lot_size_hint: 75,
        }] },
        { account: 'ZJ6294', legs: [{
          transaction_type: 'BUY', tradingsymbol: 'BANKNIFTY25JULFUT', quantity: 1,
          exchange: 'NFO', product: 'NRML', order_type: 'LIMIT', variety: 'regular',
          price: 200, trigger_price: null, chase: false, chase_aggressiveness: 'low',
          template_id: null, tp_pct_override: null, sl_pct_override: null,
          wing_premium_pct_override: null, wing_strike_offset_override: null, lot_size_hint: 25,
        }] },
      ],
    });
  });
});

// ── Adversarial cases (.claude/PLAN.md Step 4) ──────────────────────────

describe('adversarial — unknown tokens', () => {
  it('unknown metric', () => {
    const r = compile('WHEN bogus_metric@positions.total <= -1 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /unknown metric/.test(e.message))).toBe(true);
  });
  it('unknown action', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram DO bogus_action(account="ZG0790")');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /unknown action/.test(e.message))).toBe(true);
  });
  it('unknown channel', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT bogus_channel DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /unknown channel/.test(e.message))).toBe(true);
  });
});

describe('adversarial — kind mismatch', () => {
  it('a metric written inside DO is rejected with a kind-mismatch message, not a bare "unknown action"', () => {
    // agents.js's _describeKind() deliberately gives a clearer message when
    // the token IS real, just in the wrong clause ("'pnl' is a metric —
    // only actions are allowed in DO") rather than the generic "unknown
    // action 'pnl'" a truly-nonexistent token would get (see the "unknown
    // action" test above, which IS a nonexistent token and DOES match that
    // generic pattern).
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram DO pnl(account="ZG0790")');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /'pnl' is a metric.*only actions are allowed in DO/.test(e.message))).toBe(true);
  });
  it('an action token written inside WHEN is rejected with a kind-mismatch message, not a bare "unknown metric"', () => {
    const r = compile('WHEN emit_log@positions.total <= -1 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /'emit_log' is an action.*only metrics are allowed in a WHEN condition/.test(e.message))).toBe(true);
  });
});

describe('adversarial — typos and missing params', () => {
  it('keyword argument typo (symbl= instead of symbol=)', () => {
    const r = compile('order(account="ZG0790", symbl="NIFTY25JULFUT", side="SELL", lots=1)', { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /unknown parameter 'symbl'/.test(e.message))).toBe(true);
  });
  it('missing required param', () => {
    const r = compile('order(account="ZG0790", side="SELL", lots=1)', { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /symbol/.test(e.message))).toBe(true);
  });
});

describe('adversarial — ALERT nop DO nop together', () => {
  it('is rejected (decided: caught by the semantic validator, not the parser — see agents.js compileAgentStmt)', () => {
    const parsed = parseStatement('WHEN pnl@positions.total <= -1 ALERT nop DO nop');
    expect(parsed.errors).toEqual([]); // parses fine — structurally it's syntactically valid
    const r = compile('WHEN pnl@positions.total <= -1 ALERT nop DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /at least one of ALERT or DO/.test(e.message))).toBe(true);
  });
});

describe('ALERT/DO — independently optional, either order (operator request: "alert and do sequence can inter change, one of them optional")', () => {
  it('DO before ALERT (reversed order) compiles identically to the canonical ALERT-before-DO order', () => {
    const canonical = compile('WHEN pnl@positions.total <= -1 ALERT telegram DO emit_log(message="x")');
    const reversed = compile('WHEN pnl@positions.total <= -1 DO emit_log(message="x") ALERT telegram');
    expect(reversed.ok).toBe(true);
    expect(reversed.agent).toEqual(canonical.agent);
  });

  it('DO alone (ALERT token omitted entirely) compiles identically to "ALERT nop DO ..."', () => {
    const withNop = compile('WHEN pnl@positions.total <= -1 ALERT nop DO emit_log(message="x")');
    const omitted = compile('WHEN pnl@positions.total <= -1 DO emit_log(message="x")');
    expect(omitted.ok).toBe(true);
    expect(omitted.agent).toEqual(withNop.agent);
  });

  it('ALERT alone (DO token omitted entirely) compiles identically to "ALERT ... DO nop"', () => {
    const withNop = compile('WHEN pnl@positions.total <= -1 ALERT telegram DO nop');
    const omitted = compile('WHEN pnl@positions.total <= -1 ALERT telegram');
    expect(omitted.ok).toBe(true);
    expect(omitted.agent).toEqual(withNop.agent);
  });

  it('neither clause present ("WHEN cond" alone) is rejected the same way as "ALERT nop DO nop"', () => {
    const r = compile('WHEN pnl@positions.total <= -1');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /at least one of ALERT or DO/.test(e.message))).toBe(true);
  });

  it('a repeated clause keyword ("ALERT x ALERT y") leaves the second occurrence as unconsumed trailing input — a parse error', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram ALERT email DO nop');
    expect(r.ok).toBe(false);
  });
});

describe('WHEN — now optional too (operator request: "alert to be placed, or action performed with no when condition")', () => {
  it('bare "DO ..." with no WHEN at all compiles identically to "WHEN always DO ..."', () => {
    const withAlways = compile('WHEN always DO emit_log(message="x")');
    const omitted = compile('DO emit_log(message="x")');
    expect(omitted.ok).toBe(true);
    expect(omitted.agent).toEqual(withAlways.agent);
  });

  it('bare "ALERT ..." with no WHEN at all compiles identically to "WHEN always ALERT ..."', () => {
    const withAlways = compile('WHEN always ALERT telegram');
    const omitted = compile('ALERT telegram');
    expect(omitted.ok).toBe(true);
    expect(omitted.agent).toEqual(withAlways.agent);
  });

  it('WHEN omitted + ALERT/DO reversed order still compiles identically', () => {
    const canonical = compile('ALERT telegram DO emit_log(message="x")');
    const reversed = compile('DO emit_log(message="x") ALERT telegram');
    expect(reversed.ok).toBe(true);
    expect(reversed.agent).toEqual(canonical.agent);
  });

  it('ALERT/DO both omitted alongside WHEN ("DO nop" with no WHEN) still rejected — at least one real clause required', () => {
    const r = compile('DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /at least one of ALERT or DO/.test(e.message))).toBe(true);
  });

  it('a bare "order(...)" statement is still routed as order_stmt, never mistaken for a WHEN-omitted agent_stmt', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.kind).toBe('order');
    expect(r.ticket).not.toBeNull();
  });
});

describe('adversarial — mixed-direction between chain', () => {
  it('0 <= x >= 5 is a parse/compile error, not silently always-false', () => {
    const r = compile('WHEN 0 <= pnl@positions.total >= 5 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /mixed-direction/.test(e.message))).toBe(true);
  });
});

describe('adversarial — always combined with & / |', () => {
  it('always & something is a parse error', () => {
    const r = compile('WHEN always & (pnl@positions.total <= -1) ALERT telegram DO nop');
    expect(r.ok).toBe(false);
  });
  it('always alone still works', () => {
    const r = compile('WHEN always ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual(ALWAYS_LEAF);
  });
});

describe('adversarial — trailing/leading comma', () => {
  it('trailing comma in order(...) args', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1,)', { mode: 'paper' });
    expect(r.ok).toBe(false);
  });
  it('leading comma in ALERT clause', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT , telegram DO nop');
    expect(r.ok).toBe(false);
  });
});

describe('adversarial — nop() / any zero-arg token with parens', () => {
  it('nop() is a parse error', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT nop() DO nop');
    expect(r.ok).toBe(false);
  });
  it('telegram() (a zero-arg channel) with empty parens is a parse error', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram() DO nop');
    expect(r.ok).toBe(false);
  });
});

describe('adversarial — case-insensitivity', () => {
  it('SELL / sell / Sell are all equivalent and normalize to catalog casing', () => {
    for (const side of ['SELL', 'sell', 'Sell']) {
      const r = compile(`order(account="ZG0790", symbol="NIFTY25JULFUT", side=${side}, lots=1, price=100)`, { mode: 'paper' });
      expect(r.ok).toBe(true);
      expect(r.ticket.side).toBe('SELL');
    }
  });
  it('WHEN/when and mean_pnl/MEAN_PNL are case-insensitive', () => {
    const r = compile('when MEAN_PNL(minutes=30)@positions.total <= -50000 alert telegram do nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions.metric).toBe('mean_pnl(30)');
  });
  it('a quoted string\'s case is NOT touched', () => {
    const r = compile('order(account="zg0790", symbol="NIFTY25JULFUT", side=SELL, lots=1, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.account).toBe('zg0790');
  });
});

describe('adversarial — mean_pnl(minutes=30) positional-only normalization', () => {
  it('compiles to the EXACT string "mean_pnl(30)", never the keyword form', () => {
    const r = compile('WHEN mean_pnl(minutes=30)@positions.total <= -50000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions.metric).toBe('mean_pnl(30)');
  });
});

describe('adversarial — depth/length guard', () => {
  it('a pathologically deep paren nesting is rejected, not a stack overflow', () => {
    const deep = '('.repeat(5000) + 'pnl@positions.total > 0' + ')'.repeat(5000);
    const r = compile(`WHEN ${deep} ALERT telegram DO nop`);
    expect(r.ok).toBe(false);
  });
  it('a statement far beyond the length cap is rejected', () => {
    const long = 'WHEN pnl@positions.total <= -1 ALERT telegram DO emit_log(message="' + 'x'.repeat(5000) + '")';
    const r = compile(long);
    expect(r.ok).toBe(false);
  });
});

describe('adversarial — first-arg-as-keyword for order', () => {
  it('all-keyword form (account=...) works exactly like positional-first', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.account).toBe('ZG0790');
  });
});

describe('adversarial — lots vs qty', () => {
  it('both lots and qty is an error', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", qty=75, lots=1)', { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /cannot specify both qty and lots/.test(e.message))).toBe(true);
  });
  // Sprint 20 (item 1, URGENT) — `qty=` now means CONTRACTS everywhere,
  // including the bare ticket/basket path — a literal F&O symbol's
  // contracts are converted to LOTS (the real `/api/orders/ticket`
  // endpoint's own documented convention) at compile time, never passed
  // through raw (that was the "qty=75 silently became 75 LOTS instead
  // of 1" incident class this item exists to close).
  it('qty alone (contracts) converts to lots via lot_size for the ticket path', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", qty=75, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.quantity).toBe(1); // 75 contracts / lot_size 75
    expect(r.ticket.lot_size_hint).toBe(75);
  });
  it('qty= that is not an exact multiple of the lot size is a structured error, never silently truncated', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", qty=80, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /qty= is contracts.*multiple of 75/.test(e.message))).toBe(true);
  });
  it('qty= for an equity (non-F&O) exchange passes through unchanged (raw shares, no lot conversion)', () => {
    const r = compile(
      'order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=10, price=100)',
      { mode: 'paper' },
    );
    expect(r.ok).toBe(true);
    expect(r.ticket.quantity).toBe(10);
    expect(r.ticket.lot_size_hint).toBeUndefined();
    // Sprint 20 (item 2) — equity product default is CNC, not NRML.
    expect(r.ticket.product).toBe('CNC');
  });
  it('unresolvable lot size is a structured error, never silently defaults to 1', () => {
    const r = compile('order(account="ZG0790", symbol="UNKNOWN_SYM", exchange="NFO", side="SELL", lots=1, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /could not resolve lot size/.test(e.message))).toBe(true);
  });
  it('unresolvable exchange (unlisted symbol, no exchange= given) is a structured error, never silently defaults to NFO', () => {
    const r = compile('order(account="ZG0790", symbol="UNKNOWN_SYM", side="SELL", lots=1, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /could not resolve exchange/.test(e.message))).toBe(true);
  });
  it('a dual-listed symbol (RELIANCE on NSE+BSE) with no exchange= given is a structured error, never a silent guess', () => {
    const r = compile('order(account="ZG0790", symbol="RELIANCE", side="SELL", qty=10, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /lists on multiple exchanges \(NSE, BSE\)/.test(e.message))).toBe(true);
  });
  it('DO order(...) lots converts to CONTRACTS (lots × lot_size), NOT the ticket "lots-only" convention', () => {
    const r = compile('WHEN always ALERT nop DO order(account="ZG0790", symbol="BANKNIFTY25JULFUT", side="BUY", lots=3)');
    expect(r.ok).toBe(true);
    expect(r.agent.actions[0].params.qty).toBe(75); // 3 * 25
  });
});

// ── Sprint 8 — shared TP/SL/Wing kwarg vocabulary, tp_scales_json object
//    literals, bracket= on order(...), and the standalone bracket_stmt ──

describe('Sprint 8 — tp_scales_json object-literal parsing', () => {
  it('a single {...} entry parses and compiles to a JSON-stringified array', () => {
    const r = compile('bracket(tp_scales_json=[{at_pct=30, close_pct=50}]) #name="x"');
    expect(r.ok).toBe(true);
    expect(r.template.tp_scales_json).toBe(JSON.stringify([{ at_pct: 30, close_pct: 50 }]));
  });

  it('multiple comma-separated {...} entries all parse correctly', () => {
    const r = compile(
      'bracket(tp_scales_json=[{at_pct=30, close_pct=50}, {at_pct=60, close_pct=50}]) #name="x"'
    );
    expect(r.ok).toBe(true);
    expect(r.template.tp_scales_json).toBe(
      JSON.stringify([{ at_pct: 30, close_pct: 50 }, { at_pct: 60, close_pct: 50 }])
    );
  });

  it('"=" is the object literal\'s key/value separator — ":" is a parse error, not a silent alternate spelling', () => {
    const r = compile('bracket(tp_scales_json=[{at_pct:30}]) #name="x"');
    expect(r.ok).toBe(false);
  });
});

describe('Sprint 8 — order(...) inline TP/SL/Wing overrides', () => {
  it('tp_pct/sl_pct compile to the *_override ticket field names', () => {
    const r = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100, tp_pct=30, sl_pct=-20)',
      { mode: 'paper' },
    );
    expect(r.ok).toBe(true);
    expect(r.ticket.tp_pct_override).toBe(30);
    expect(r.ticket.sl_pct_override).toBe(-20);
  });

  it('wing_premium_pct + wing_strike_offset together is a compile error (mirrors the lots/qty exclusivity style)', () => {
    const r = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, wing_premium_pct=5, wing_strike_offset=100)',
      { mode: 'paper' },
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /cannot specify both wing_premium_pct and wing_strike_offset/.test(e.message))).toBe(true);
  });

  it('bracket="name" compiles to template_slug on the ticket', () => {
    const r = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100, bracket="default-bull")',
      { mode: 'paper' },
    );
    expect(r.ok).toBe(true);
    expect(r.ticket.template_slug).toBe('default-bull');
  });
});

// ── Sprint 17 — object-literal per-key typing, conditional (`required_if`)
//    param requirements, and generalized (`mutuallyExclusiveGroups`)
//    mutual-exclusion ─────────────────────────────────────────────────────

describe('Sprint 17 — tp_scales_json per-key object-literal typing', () => {
  it('a string value where a number is required ("thirty") is a compile error naming the key and expected type', () => {
    const r = compile('bracket(tp_scales_json=[{at_pct="thirty", close_pct=50}]) #name="x"');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /at_pct/.test(e.message) && /number/.test(e.message))).toBe(true);
  });

  it('a valid numeric entry still compiles fine (no regression)', () => {
    const r = compile('bracket(tp_scales_json=[{at_pct=30, close_pct=50}]) #name="x"');
    expect(r.ok).toBe(true);
    expect(r.template.tp_scales_json).toBe(JSON.stringify([{ at_pct: 30, close_pct: 50 }]));
  });
});

describe('Sprint 17 — where= stays untyped/passthrough (no fixed schema)', () => {
  it('where={some_key="value"} still compiles untyped — no per-key schema regression', () => {
    const r = compile('ALERT log_tag=error, render=error, where={some_key="value"}, telegram #name="x"');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions.log.where).toEqual({ some_key: 'value' });
  });
});

describe('Sprint 17 — conditional (required_if) price/trigger_price requirements', () => {
  it('order_type=LIMIT with no price= is a compile error', () => {
    const r = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, order_type=LIMIT)',
      { mode: 'paper' },
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /\bprice is required when order_type=LIMIT/.test(e.message))).toBe(true);
  });

  it('order_type=MARKET with no price= still compiles fine (no regression)', () => {
    const r = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, order_type=MARKET)',
      { mode: 'paper' },
    );
    expect(r.ok).toBe(true);
    expect(r.ticket.price).toBeNull();
  });

  it('order_type=SL with trigger_price= but no price= is ALSO a compile error (SL requires BOTH)', () => {
    const r = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, order_type=SL, trigger_price=100)',
      { mode: 'paper' },
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /\bprice is required when order_type=SL/.test(e.message))).toBe(true);
  });

  it('order_type=SL with BOTH price= and trigger_price= compiles fine', () => {
    const r = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, order_type=SL, price=100, trigger_price=105)',
      { mode: 'paper' },
    );
    expect(r.ok).toBe(true);
    expect(r.ticket.price).toBe(100);
    expect(r.ticket.trigger_price).toBe(105);
  });

  it('DO place_order(...) with order_type=SL and no trigger_price= is also a compile error (generic path)', () => {
    const r = compile(
      'WHEN always ALERT nop DO place_order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", qty=75, order_type=SL, price=100)'
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /trigger_price is required when order_type=SL/.test(e.message))).toBe(true);
  });
});

describe('Sprint 17 — generalized mutual-exclusion (declared mutuallyExclusiveGroups)', () => {
  it('wing_premium_pct + wing_strike_offset together is STILL a compile error (regression guard on the generalized mechanism)', () => {
    const r = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, wing_premium_pct=5, wing_strike_offset=100)',
      { mode: 'paper' },
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /cannot specify both wing_premium_pct and wing_strike_offset/.test(e.message))).toBe(true);
  });

  it('the direct _override field names (no short-kwarg sugar) are also caught', () => {
    const r = compile(
      'WHEN always ALERT nop DO place_order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", qty=75, ' +
      'wing_premium_pct_override=5, wing_strike_offset_override=100)'
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /cannot specify both wing_premium_pct_override and wing_strike_offset_override/.test(e.message))).toBe(true);
  });

  it('a mix — one via short-kwarg sugar, one via the direct _override name — is also caught on order(...)', () => {
    const r = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, wing_premium_pct=5, wing_strike_offset_override=100)',
      { mode: 'paper' },
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /cannot specify both/.test(e.message))).toBe(true);
  });

  it('the wing-pair check still applies to standalone bracket(...) (catalog threaded through)', () => {
    const r = compile('bracket(wing_premium_pct=5, wing_strike_offset=100) #name="x"');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /cannot specify both wing_premium_pct and wing_strike_offset/.test(e.message))).toBe(true);
  });

  it('only one of the pair still compiles fine (no regression)', () => {
    const r = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100, wing_premium_pct=5)',
      { mode: 'paper' },
    );
    expect(r.ok).toBe(true);
    expect(r.ticket.wing_premium_pct_override).toBe(5);
  });
});

describe('Sprint 8 — standalone bracket(...) statement', () => {
  it('compiles to an OrderTemplateCreate-shaped object', () => {
    const r = compile(
      'bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20, tp_order_type=LIMIT) #name="Default Bull"'
    );
    expect(r.ok).toBe(true);
    expect(r.template).toEqual({
      name: 'Default Bull', applies_to: 'buy_any', tp_pct: 30, sl_pct: -20, tp_order_type: 'LIMIT',
    });
  });

  it('a missing #name= is a compile-time error — a saved bracket must have a name', () => {
    const r = compile('bracket(applies_to=buy_any, tp_pct=30)');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /missing required meta tag '#name'/.test(e.message))).toBe(true);
  });

  it('bracket(...) used inside a DO clause is rejected the same way any other non-catalog action is ("unknown action")', () => {
    const r = compile('WHEN always ALERT nop DO bracket(applies_to=buy_any)');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /unknown action 'bracket'/.test(e.message))).toBe(true);
  });

  // Sprint 14 — `applies_to=sell_any` was missing from the CLI's enum
  // list even though the real backend `_APPLIES_TO_CHOICES`
  // (backend/api/routes/templates.py:39) has always allowed it.
  it('accepts applies_to=sell_any, matching the real backend enum', () => {
    const r = compile('bracket(applies_to=sell_any, tp_pct=20) #name="Default Short"');
    expect(r.ok).toBe(true);
    expect(r.template.applies_to).toBe('sell_any');
  });
});

// ── Sprint 10 — Token ($ref) references ─────────────────────────────────

describe('Sprint 10 — bare Token ($ref) reference', () => {
  it('a bare Token reference in WHEN compiles to {"$ref": name}', () => {
    const r = compile('WHEN loss-positions-default ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ '$ref': 'loss-positions-default' });
  });

  it('a bare Token reference in ALERT compiles to {"$ref": name}', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT notify-critical-trio DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.events).toEqual([{ '$ref': 'notify-critical-trio' }]);
  });

  it('a Token reference combines with an ordinary leaf via "&"', () => {
    const r = compile('WHEN loss-positions-default & pnl@positions.total < 0 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      all: [
        { '$ref': 'loss-positions-default' },
        { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 },
      ],
    });
  });

  it('a real metric missing its "@" still gets the original compile error (just at compile time, not parse time)', () => {
    const r = compile('WHEN pnl ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /expected "@" after metric 'pnl'/.test(e.message))).toBe(true);
  });

  it('a Token reference carrying arguments is a compile error — it takes none', () => {
    const r = compile('WHEN loss-positions-default(x=1) ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /Token reference and takes no arguments/.test(e.message))).toBe(true);
  });

  it('a Token reference with its own arguments in ALERT is likewise a compile error', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT notify-critical-trio(msg="hi") DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /Token reference and takes no arguments/.test(e.message))).toBe(true);
  });
});

describe('Sprint 10 — hyphenated NAME lexing (call/token-reference position only)', () => {
  it('a hyphenated Token name lexes as ONE NAME token', () => {
    const { tokens, errors } = lexAgentStatement('loss-positions-default');
    expect(errors).toEqual([]);
    expect(tokens.map(t => t.type)).toEqual(['NAME', 'EOF']);
    expect(tokens[0].value).toBe('loss-positions-default');
  });

  it('a trailing hyphen (before whitespace/EOF) is NOT swallowed into the name', () => {
    const { errors } = lexAgentStatement('loss-positions-default-');
    // The dangling "-" (no digit, no word-char after it) has no legal
    // production of its own — fails loud rather than silently being
    // absorbed into the preceding name.
    expect(errors.length).toBeGreaterThan(0);
  });

  it('a negative number in a kwarg value position inside a call still lexes correctly, unaffected by the hyphen widening', () => {
    const { tokens, errors } = lexAgentStatement('mean_pnl(minutes=-50000)');
    expect(errors).toEqual([]);
    // Sprint 20 (items 4/5) — every ArgEntry/ValueNode now also carries
    // position metadata (`keyStart`/`start`/`end`) — asserted via
    // `objectContaining` here so this test stays scoped to the
    // key/value SHAPE it was written to check, not exact offsets.
    expect(tokens[0].args).toEqual([expect.objectContaining({
      key: 'minutes', value: expect.objectContaining({ type: 'number', value: -50000 }),
    })]);
  });

  it('a kwarg KEY still cannot resolve as a known parameter even though it lexes with a hyphen (no regression — real keys use underscore)', () => {
    const r = compile('WHEN mean_pnl(tp-pct=30)@positions.total < -1 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /unknown parameter 'tp-pct'/.test(e.message))).toBe(true);
  });
});

describe('Sprint 10 — standalone token(...) statement', () => {
  it('compiles to a POST /api/admin/fragments-shaped object', () => {
    const r = compile('token(kind=notify) telegram, email #name="x"');
    expect(r.ok).toBe(true);
    expect(r.fragment).toEqual({
      kind: 'notify',
      name: 'x',
      body: [
        { channel: 'telegram', enabled: true },
        { channel: 'email', enabled: true },
      ],
    });
  });

  it('kind=condition is a clear compile error — not supported from the CLI yet', () => {
    const r = compile('token(kind=condition) telegram #name="x"');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /condition-kind Token creation isn't supported/.test(e.message))).toBe(true);
  });

  it('a missing #name= is a compile-time error', () => {
    const r = compile('token(kind=notify) telegram, email');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /missing required meta tag '#name'/.test(e.message))).toBe(true);
  });

  // Sprint 14 — these compile-error strings used to say "AgentTemplate has
  // no tags/group field", a stale internal model name that reached
  // operators verbatim in the CLI editor's error list. Reworded to "Token".
  it('#tag= and #group= are rejected with operator-facing "Token" wording, not "AgentTemplate"', () => {
    const rTag = compile('token(kind=notify) telegram #name="x" #tag=foo');
    expect(rTag.ok).toBe(false);
    expect(rTag.errors.some(e => /Token has no tags field/.test(e.message))).toBe(true);
    expect(rTag.errors.some(e => /AgentTemplate/.test(e.message))).toBe(false);

    const rGroup = compile('token(kind=notify) telegram #name="x" #group=foo');
    expect(rGroup.ok).toBe(false);
    expect(rGroup.errors.some(e => /Token has no group field/.test(e.message))).toBe(true);
    expect(rGroup.errors.some(e => /AgentTemplate/.test(e.message))).toBe(false);
  });

  it('token(...) used inside a DO clause is rejected the same way any other non-catalog action is ("unknown action")', () => {
    const r = compile('WHEN always ALERT nop DO token(kind=notify)');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /unknown action 'token'/.test(e.message))).toBe(true);
  });
});

// ── Boolean-metric shorthand ─────────────────────────────────────────────

describe('boolean-metric shorthand', () => {
  it('a bare boolean metric with no comparator means "is truthy"', () => {
    const r = compile('WHEN is_itm@positions.total ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'is_itm', scope: 'positions.total', op: '==', value: true });
  });
  it('a bare NUMERIC metric with no comparator is a clear parse error, not a silent success', () => {
    const r = compile('WHEN pnl@positions.total ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /expected a comparator after a numeric metric/.test(e.message))).toBe(true);
  });
});

// ── in / not_in ──────────────────────────────────────────────────────────

describe('in / not_in membership', () => {
  it('side in [BUY, SELL] compiles with op "in"', () => {
    const r = compile('WHEN side@positions.total in [BUY, SELL] ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'side', scope: 'positions.total', op: 'in', value: ['BUY', 'SELL'] });
  });
  it('not in compiles to op "not_in" (backend vocabulary, not "not in")', () => {
    const r = compile('WHEN side@positions.total not in [BUY, SELL] ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions.op).toBe('not_in');
  });
});

// ── Between (chained comparison) — inclusive vs strict ──────────────────

describe('chained ("between") comparison', () => {
  it('inclusive same-direction chain compiles to a single "between" leaf', () => {
    const r = compile('WHEN -100 <= pnl@positions.total <= 100 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'pnl', scope: 'positions.total', op: 'between', value: [-100, 100] });
  });
  it('inclusive descending-direction chain normalizes value order to [low, high]', () => {
    const r = compile('WHEN 100 >= pnl@positions.total >= -100 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'pnl', scope: 'positions.total', op: 'between', value: [-100, 100] });
  });
  it('a STRICT chain does NOT compile to "between" (backend\'s between op is always inclusive) — compiles to an "all" of two leaves', () => {
    const r = compile('WHEN 0 < pnl@positions.total < 100 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      all: [
        { metric: 'pnl', scope: 'positions.total', op: '>', value: 0 },
        { metric: 'pnl', scope: 'positions.total', op: '<', value: 100 },
      ],
    });
  });
});

// ── Sprint 15 — generic `scope.attribute` dot-access, per-scope column
//    registry projected into the token catalog ─────────────────────────

describe('Sprint 15 — scope.attribute dot-access (col_ref leaf)', () => {
  it('positions.total.pnl <= -50000 compiles to the IDENTICAL JSON as pnl@positions.total <= -50000', () => {
    const dotted = compile('WHEN positions.total.pnl <= -50000 ALERT telegram DO nop');
    const atForm = compile('WHEN pnl@positions.total <= -50000 ALERT telegram DO nop');
    expect(dotted.ok).toBe(true);
    expect(dotted.errors).toEqual([]);
    expect(dotted.agent.conditions).toEqual({ metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 });
    expect(dotted.agent.conditions).toEqual(atForm.agent.conditions);
  });

  it('a column with no hand-registered metric token compiles via dot-access (e.g. qty — no "qty@scope" form exists)', () => {
    const r = compile('WHEN positions.expiring_today.qty > 10 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'qty', scope: 'positions.expiring_today', op: '>', value: 10 });
  });

  it('a string-typed column works with in/not_in, same as a string-typed metric', () => {
    const r = compile('WHEN positions.expiring_today.symbol in ["NIFTY25JULFUT"] ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'symbol', scope: 'positions.expiring_today', op: 'in', value: ['NIFTY25JULFUT'] });
  });

  it('a column under a space-containing real DataFrame column name (funds option_premium) compiles — the exposed name stays clean', () => {
    const r = compile('WHEN funds.total.option_premium > 0 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'option_premium', scope: 'funds.total', op: '>', value: 0 });
  });

  it('a numeric column compared against a non-numeric value errors with the SAME message a numeric metric already produces (reused, not duplicated, check)', () => {
    const colForm = compile('WHEN positions.total.pnl == "5" ALERT telegram DO nop');
    const metricForm = compile('WHEN pnl@positions.total == "5" ALERT telegram DO nop');
    expect(colForm.ok).toBe(false);
    expect(colForm.errors[0].message).toBe('comparator requires both sides numeric');
    expect(colForm.errors[0].message).toBe(metricForm.errors[0].message);
  });

  it('between-chain syntax also accepts a bare dotted column (symmetry with compileLeaf)', () => {
    const r = compile('WHEN -1000 <= funds.total.option_premium <= 5000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'option_premium', scope: 'funds.total', op: 'between', value: [-1000, 5000] });
  });

  // ── Regression guard: the EXISTING multi-segment scope_ref (always
  //    reached via "@") is completely unaffected by the new bare dotted
  //    leaf form. ──────────────────────────────────────────────────────
  describe('regression guard — positions.expiring_today.mcx_unhedged (real multi-segment SCOPE)', () => {
    it('unaffected: pnl@positions.expiring_today.mcx_unhedged still compiles exactly as before', () => {
      const r = compile('WHEN pnl@positions.expiring_today.mcx_unhedged <= -1000 ALERT telegram DO nop');
      expect(r.ok).toBe(true);
      expect(r.agent.conditions).toEqual({ metric: 'pnl', scope: 'positions.expiring_today.mcx_unhedged', op: '<=', value: -1000 });
    });

    it('a 4-segment dotted chain (3-segment scope + column) resolves scope = the FULL 3-segment scope, not a shorter guess', () => {
      const r = compile('WHEN positions.expiring_today.mcx_unhedged.pnl <= -1000 ALERT telegram DO nop');
      expect(r.ok).toBe(true);
      expect(r.agent.conditions).toEqual({ metric: 'pnl', scope: 'positions.expiring_today.mcx_unhedged', op: '<=', value: -1000 });
    });

    it('the bare 3-segment chain alone (no trailing column) resolves as a SCOPE and errors clearly — never misparsed as a shorter scope + column', () => {
      const r = compile('WHEN positions.expiring_today.mcx_unhedged <= -1000 ALERT telegram DO nop');
      expect(r.ok).toBe(false);
      expect(r.errors[0].message).toMatch(/'positions\.expiring_today\.mcx_unhedged' is a scope/);
      expect(r.errors[0].message).not.toMatch(/unknown column/);
    });
  });

  describe('decompile round-trip (a column with no backing metric token)', () => {
    it('{metric:"qty", scope:"positions.expiring_today", ...} decompiles to the dotted column form and recompiles to the same JSON', () => {
      const conditions = { metric: 'qty', scope: 'positions.expiring_today', op: '>', value: 10 };
      const { text, errors } = decompileCondition(conditions, CATALOG);
      expect(errors).toEqual([]);
      expect(text).toBe('positions.expiring_today.qty > 10');
      const recompiled = compile(`WHEN ${text} ALERT telegram DO nop`);
      expect(recompiled.ok).toBe(true);
      expect(recompiled.agent.conditions).toEqual(conditions);
    });
  });
});

describe('Sprint 15 — autocomplete: column suggestions show the SHORT label, not the long compound token', () => {
  it('typing a complete known scope + "." suggests its exposed columns, inserting the FULL compound token but LABELING it with the short display text', () => {
    const text = 'WHEN positions.total.';
    const r = suggestAgentCliAt(text, text.length, CATALOG);
    expect(r.kind).toBe('column');
    const idx = r.suggestions.indexOf('positions.total.pnl');
    expect(idx).toBeGreaterThanOrEqual(0);
    // The long compound token is never what gets shown as the primary
    // label — the short catalog `label` ("P&L (₹)") is.
    expect(r.labels[idx]).toBe('P&L (₹)');
    expect(r.labels[idx]).not.toBe('positions.total.pnl');
    // The description still carries full provenance (unaffected).
    expect(r.descriptions[idx]).toContain('P&L (₹)');
  });

  it('replaceRange spans the WHOLE typed chain (from the fresh-name start), same convention as @-anchored scope_ref completion', () => {
    const text = 'WHEN positions.total.';
    const r = suggestAgentCliAt(text, text.length, CATALOG);
    const chainStart = text.indexOf('positions.total.');
    expect(r.replaceRange).toEqual([chainStart, text.length]);
  });

  it('mid-typing a partial column name fuzzy-filters within that one scope only', () => {
    const text = 'WHEN positions.total.pn';
    const r = suggestAgentCliAt(text, text.length, CATALOG);
    expect(r.kind).toBe('column');
    expect(r.suggestions).toEqual(['positions.total.pnl']);
    expect(r.suggestions).not.toContain('positions.total.account_id');
  });

  it('a non-column, non-metric suggestion (e.g. the ordinary metric-name branch) carries labels[i] === null, never a stray label', () => {
    const r = suggestAgentCliAt('WHEN pn', 'WHEN pn'.length, CATALOG);
    expect(r.kind).toBe('token');
    const idx = r.suggestions.indexOf('pnl');
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(r.labels[idx]).toBeNull();
  });

  it('an incomplete scope prefix (not yet a real, complete scope) suggests nothing — exact scope matching, never a fuzzy cross-scope guess', () => {
    const text = 'WHEN positions.tot';
    const r = suggestAgentCliAt(text, text.length, CATALOG);
    // "positions.tot" is mid-typing a SCOPE segment itself (not yet
    // "positions.total" followed by "."), so this still falls through to
    // the existing scope-chain-extension suggestion path, not a column
    // list — confirms no column leaks in before the scope is complete.
    expect(r.suggestions).not.toContain('positions.total.pnl');
  });
});

describe('Sprint 15 — AgentCliEditor hover description: required_if / mutually_exclusive_groups rendered as plain English', () => {
  it("a required_if-carrying param's hover description includes the generated requirement text (price — required when order_type is LIMIT/SL)", () => {
    const text = 'WHEN always ALERT nop DO place_order(';
    const r = suggestAgentCliAt(text, text.length, CATALOG);
    expect(r.kind).toBe('param');
    const idx = r.suggestions.indexOf('price');
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(r.descriptions[idx]).toContain('price — required when order_type is LIMIT/SL');
  });

  it("trigger_price's required_if (SL/SL-M) is rendered distinctly from price's own (LIMIT/SL)", () => {
    const text = 'WHEN always ALERT nop DO place_order(';
    const r = suggestAgentCliAt(text, text.length, CATALOG);
    const idx = r.suggestions.indexOf('trigger_price');
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(r.descriptions[idx]).toContain('trigger_price — required when order_type is SL/SL-M');
  });

  it('a mutually_exclusive_groups member\'s description names the OTHER member', () => {
    const text = 'WHEN always ALERT nop DO place_order(';
    const r = suggestAgentCliAt(text, text.length, CATALOG);
    const idx = r.suggestions.indexOf('wing_premium_pct_override');
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(r.descriptions[idx]).toContain('wing_premium_pct_override is mutually exclusive with wing_strike_offset_override');
  });

  it('a param with neither required_if nor a mutex membership still degrades to its own plain description, unchanged', () => {
    const text = 'WHEN always ALERT nop DO place_order(';
    const r = suggestAgentCliAt(text, text.length, CATALOG);
    const idx = r.suggestions.indexOf('symbol');
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(r.descriptions[idx]).toBe('');
  });
});

// ── Unparenthesized leaf & leaf — EBNF allows it even though the operator
//    table's examples always show parens (advisor-flagged ambiguity,
//    resolved in favor of the literal EBNF, which is the task brief's
//    explicit "implement this EXACTLY" grammar). ──────────────────────────

describe('unparenthesized leaf combination', () => {
  it('bare "a & b" (no parens around either leaf) parses and compiles', () => {
    const r = compile('WHEN pnl@positions.total <= -1 & day_pct@positions.total <= -2 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      all: [
        { metric: 'pnl', scope: 'positions.total', op: '<=', value: -1 },
        { metric: 'day_pct', scope: 'positions.total', op: '<=', value: -2 },
      ],
    });
  });
});

// ── Lexer edge cases (advisor-flagged) ──────────────────────────────────

describe('lexer — spacing around "="', () => {
  it('spaced "symbol = \\"X\\"" is equivalent to fused "symbol=\\"X\\""', () => {
    const r = compile('order(account="ZG0790", symbol = "NIFTY25JULFUT", side="SELL", lots=1, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.tradingsymbol).toBe('NIFTY25JULFUT');
  });
  it('"key =value" (space before =, none after) also works', () => {
    const r = compile('order(account="ZG0790", symbol ="NIFTY25JULFUT", side="SELL", lots=1, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.tradingsymbol).toBe('NIFTY25JULFUT');
  });
  it('"key= value" (space after =, none before) also works', () => {
    const r = compile('order(account="ZG0790", symbol= "NIFTY25JULFUT", side="SELL", lots=1, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.tradingsymbol).toBe('NIFTY25JULFUT');
  });
});

describe('lexer — mean_pnl (30) with a space is NOT a call', () => {
  it('a space between a name and its own "(" means the "(" opens a grouping, not args — clean error here since a bare metric_ref needs "@" next, not "("', () => {
    const r = compile('WHEN mean_pnl (30)@positions.total <= -1 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
  });
});

describe('lexer — tag="ALERT_ME" is never mistaken for the real ALERT keyword', () => {
  it('keyword-boundary scanning is token-level, not substring-level', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram DO emit_log(message="ALERT_ME")');
    expect(r.ok).toBe(true);
    expect(r.agent.actions[0].params.message).toBe('ALERT_ME');
  });
});

// ── Free-text (string) field requires quoting — bare NAME rejected ──────

describe('free-text field requires quoting', () => {
  it('a bare (unquoted) value in a string-typed, non-enum field is rejected', () => {
    const r = compile('order(account=ZG0790, symbol="NIFTY25JULFUT", side="SELL", lots=1)', { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /must be quoted/.test(e.message))).toBe(true);
  });
});

// ── validateAgentCliStatement() — thin wrapper, same path as compile ────

describe('validateAgentCliStatement()', () => {
  it('returns ok:true with no errors for a valid statement', () => {
    const v = validateAgentCliStatement('WHEN pnl@positions.total <= -1 ALERT telegram DO nop', CATALOG, { lotSizeOf });
    expect(v).toEqual({ ok: true, errors: [] });
  });
  it('returns structured errors for an invalid statement, never throws', () => {
    const v = validateAgentCliStatement('WHEN bogus@positions.total <= -1 ALERT telegram DO nop', CATALOG, { lotSizeOf });
    expect(v.ok).toBe(false);
    expect(v.errors.length).toBeGreaterThan(0);
  });
});

// ── KWARG_ALIASES sanity ─────────────────────────────────────────────────

describe('KWARG_ALIASES', () => {
  it('reuses the already-established "chase" alias for chase_level (orders.yaml convention)', () => {
    expect(KWARG_ALIASES.chase_level).toBe('chase');
  });
  it('an alias resolves to the real schema key in compiled output', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100, chase=HIGH)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.chase).toBe(true);
    expect(r.ticket.chase_aggressiveness).toBe('high');
  });

  it('trigger_price has the "trigpx" short alias (matching the price→px convention)', () => {
    expect(KWARG_ALIASES.trigger_price).toBe('trigpx');
  });

  it('trigger_price= and its trigpx= alias compile to identical params for an SL order', () => {
    const real = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, order_type=SL, price=23000, trigger_price=23050)',
      { mode: 'paper' },
    );
    const aliased = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, order_type=SL, price=23000, trigpx=23050)',
      { mode: 'paper' },
    );
    expect(real.ok).toBe(true);
    expect(aliased.ok).toBe(true);
    expect(real.ticket.trigger_price).toBe(23050);
    expect(aliased.ticket).toEqual(real.ticket);
  });
});

// ── parseStatement()/order_stmt restriction ──────────────────────────────

describe('order_stmt restricted to order/place_order calls only', () => {
  it('a bare non-order action is a clear parse error', () => {
    const r = compile('emit_log(message="hi")');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /only order\(\.\.\.\) can be used standalone/.test(e.message))).toBe(true);
  });
});

// ── ALERT msg= clause-level/per-channel message default (Sprint 6) ─────

describe('ALERT msg= default', () => {
  it('clause-level default applies to every bare channel that does not override it', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT msg="Stop loss hit", telegram, ntfy(priority="urgent") DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.events).toEqual([
      { channel: 'telegram', enabled: true, message: 'Stop loss hit' },
      { channel: 'ntfy', enabled: true, priority: 'urgent', message: 'Stop loss hit' },
    ]);
  });

  it('a per-channel own msg= overrides the clause default; a channel with no own msg= still inherits it', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT msg="default text", telegram, ntfy(msg="override", priority="urgent") DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.events).toEqual([
      { channel: 'telegram', enabled: true, message: 'default text' },
      { channel: 'ntfy', enabled: true, priority: 'urgent', message: 'override' },
    ]);
  });

  it('per-channel own msg= on every channel needs no clause-level default at all', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram(msg="custom A"), ntfy(msg="custom B", priority="urgent") DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.events).toEqual([
      { channel: 'telegram', enabled: true, message: 'custom A' },
      { channel: 'ntfy', enabled: true, priority: 'urgent', message: 'custom B' },
    ]);
  });

  it('a channel with neither its own message nor a clause-level default is a compile error with the exact documented wording', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram(msg="only this one has a message"), ntfy(priority="urgent") DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => e.message === "channel 'ntfy' has no message — set msg= on ALERT or on this channel directly")).toBe(true);
  });

  it('when msg= is used nowhere in the clause, every channel compiles with NO message key at all (unchanged from pre-Sprint-6 output)', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram, ntfy(priority="urgent") DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.events).toEqual([
      { channel: 'telegram', enabled: true },
      { channel: 'ntfy', enabled: true, priority: 'urgent' },
    ]);
    expect('message' in r.agent.events[0]).toBe(false);
    expect('message' in r.agent.events[1]).toBe(false);
  });

  it('telegram() with empty parens, relying only on the clause default, is still a parse error (existing "zero-arg must be bare" rule is untouched)', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT msg="x", telegram(), ntfy(priority="urgent") DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /zero-argument token is always written bare/.test(e.message))).toBe(true);
  });

  it('a duplicate msg/message kwarg on the same channel call is a compile error', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram(msg="a", message="b") DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /duplicate parameter 'msg'\/'message'/.test(e.message))).toBe(true);
  });

  // Sprint 20 (item 4) — `msg=`/`message=` now accept a bare single
  // word, same as `log_tag=`/`render=`/meta values already do (no
  // $row-binding concept applies to notification text) — closes the
  // "msg=hello doesn't [accept bare]" inconsistency the audit flagged.
  // `account=`/`symbol=` (genuinely $row-ambiguous action params) are
  // deliberately UNCHANGED — see the "free-text field requires quoting"
  // describe block above, still green.
  it('a bare (unquoted) per-channel message value is now accepted, same bare-or-quoted rule as log_tag= and meta values', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram(msg=hello) DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.events[0].message).toBe('hello');
  });

  it('msg= and MSG= are case-insensitive and behave identically', () => {
    const lower = compile('WHEN pnl@positions.total <= -1 ALERT msg="hi", telegram DO nop');
    const upper = compile('WHEN pnl@positions.total <= -1 ALERT MSG="hi", telegram DO nop');
    expect(lower.ok).toBe(true);
    expect(upper.ok).toBe(true);
    expect(upper.agent).toEqual(lower.agent);
  });

  it('DO emit_log(message=...) is unaffected by the ALERT-only msg= stripping — DO\'s own real "message" param still works', () => {
    const r = compile('WHEN pnl@positions.total <= -1 DO emit_log(message="x") ALERT msg="y", telegram');
    expect(r.ok).toBe(true);
    expect(r.agent.actions).toEqual([{ type: 'emit_log', params: { message: 'x' } }]);
    expect(r.agent.events).toEqual([{ channel: 'telegram', enabled: true, message: 'y' }]);
  });

  it('msg= composes with the already-shipped ALERT/DO reordering: DO-before-ALERT with a clause default compiles identically to the canonical order', () => {
    const canonical = compile('WHEN pnl@positions.total <= -1 ALERT msg="y", telegram DO emit_log(message="x")');
    const reversed = compile('WHEN pnl@positions.total <= -1 DO emit_log(message="x") ALERT msg="y", telegram');
    expect(reversed.ok).toBe(true);
    expect(reversed.agent).toEqual(canonical.agent);
  });

  it('msg= composes with a bare "ALERT ..." with no WHEN at all', () => {
    const withWhen = compile('WHEN always ALERT msg="y", telegram');
    const omitted = compile('ALERT msg="y", telegram');
    expect(omitted.ok).toBe(true);
    expect(omitted.agent).toEqual(withWhen.agent);
  });

  it('"ALERT msg=..." with no channel list at all is a parse error, not a silent success', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT msg="y" DO nop');
    expect(r.ok).toBe(false);
  });

  it('"ALERT nop" is unaffected — msg= has no meaning alongside an explicit nop', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT nop DO emit_log(message="x")');
    expect(r.ok).toBe(true);
    expect(r.agent.events).toEqual([]);
  });
});

// ── lexAgentStatement() / parseCall() / parseValue() / parseLeaf() — a
//    few direct unit tests on the lower-level exports, for Sprint 3/4's
//    benefit (decompiler / live preview will call these directly). ──────

describe('low-level exports', () => {
  it('lexAgentStatement tokenizes a call with its args pre-resolved', () => {
    // Every argument is keyword-only, including the first — see the
    // "RESOLVED" note in this file's own header comment for why.
    const { tokens, errors } = lexAgentStatement('mean_pnl(minutes=30)');
    expect(errors).toEqual([]);
    expect(tokens[0].type).toBe('NAME');
    expect(tokens[0].hasCall).toBe(true);
    // Sprint 20 (items 4/5) — see the sibling hyphen-widening test's own
    // comment above for why this now uses `objectContaining`.
    expect(tokens[0].args).toEqual([expect.objectContaining({
      key: 'minutes', value: expect.objectContaining({ type: 'number', value: 30 }),
    })]);
  });

  it('a bare positional argument is a parse error, even as the first argument', () => {
    const { errors } = lexAgentStatement('mean_pnl(30)');
    expect(errors.length).toBeGreaterThan(0);
    expect(errors[0].message).toMatch(/keyword-only, including the first/);
  });

  it('parseCall consumes one NAME token and returns a CallNode', () => {
    const { tokens } = lexAgentStatement('telegram');
    const state = { tokens, pos: 0 };
    const call = parseCall(state);
    expect(call).toMatchObject({ type: 'call', name: 'telegram', args: [] });
  });

  it('parseValue reads a quoted string exactly, case preserved', () => {
    const { tokens } = lexAgentStatement('"ZG0790"');
    const state = { tokens, pos: 0 };
    expect(parseValue(state)).toEqual({ type: 'string', value: 'ZG0790' });
  });

  it('parseLeaf parses a plain comparator leaf', () => {
    const { tokens } = lexAgentStatement('pnl@positions.total <= -50000');
    const state = { tokens, pos: 0 };
    const leaf = parseLeaf(state);
    expect(leaf.type).toBe('leaf');
    expect(leaf.op).toBe('<=');
    expect(leaf.value).toEqual({ type: 'number', value: -50000 });
  });
});

// ── trailing "#" meta clause (Sprint 7) — #name=/#desc=/#tag=/#group= ──

describe('meta clause — #name=/#desc=/#tag=/#group=', () => {
  it('#name= alone compiles to agent.name', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram #name="NIFTY loss guard"');
    expect(r.ok).toBe(true);
    expect(r.agent.name).toBe('NIFTY loss guard');
    expect(r.agent.description).toBeUndefined();
    expect(r.agent.tags).toBeUndefined();
    expect(r.agent.group).toBeUndefined();
  });

  it('#desc= alone compiles to agent.description', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram #desc="Closes on -50k drawdown"');
    expect(r.ok).toBe(true);
    expect(r.agent.description).toBe('Closes on -50k drawdown');
  });

  it('#tag= with multiple comma-separated values produces a real JS array', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram #tag=risk,nifty');
    expect(r.ok).toBe(true);
    expect(Array.isArray(r.agent.tags)).toBe(true);
    expect(r.agent.tags).toEqual(['risk', 'nifty']);
  });

  it('#tag= with a single value still produces a one-element array', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram #tag=risk');
    expect(r.ok).toBe(true);
    expect(r.agent.tags).toEqual(['risk']);
  });

  it('a hyphenated BARE_WORD value (#group=hedge-book) lexes and compiles correctly — confirmed gap, NOT handled by the NAME scanner', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram #group=hedge-book');
    expect(r.ok).toBe(true);
    expect(r.agent.group).toBe('hedge-book');
  });

  it('a quoted value containing a space still works', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram #name="NIFTY loss guard"');
    expect(r.ok).toBe(true);
    expect(r.agent.name).toBe('NIFTY loss guard');
  });

  it('all four keys compile correctly in combination, in the documented example order', () => {
    const r = compile(
      'WHEN pnl@positions.total <= -50000 ALERT telegram DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1) '
      + '#name="NIFTY loss guard" #desc="Closes on -50k drawdown" #tag=risk,nifty #group=hedge-book'
    );
    expect(r.ok).toBe(true);
    expect(r.agent.name).toBe('NIFTY loss guard');
    expect(r.agent.description).toBe('Closes on -50k drawdown');
    expect(r.agent.tags).toEqual(['risk', 'nifty']);
    expect(r.agent.group).toBe('hedge-book');
    // conditions/events/actions compile unaffected by the trailing meta clause.
    expect(r.agent.actions).toEqual([{
      type: 'place_order',
      params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'SELL', qty: 75 },
    }]);
  });

  it('no meta clause at all leaves name/description/tags/group entirely absent from the compiled agent', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram');
    expect(r.ok).toBe(true);
    expect('name' in r.agent).toBe(false);
    expect('description' in r.agent).toBe(false);
    expect('tags' in r.agent).toBe(false);
    expect('group' in r.agent).toBe(false);
  });

  it('a bare order(...) statement with a trailing #... produces the exact compile-error text', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", qty=75) #name="x"', { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => e.message === 'meta tags only apply to agents/brackets, not a bare order')).toBe(true);
  });

  it('an unknown meta key is a compile-time error, not a silent drop', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram #bogus=x');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /unknown meta tag '#bogus'/.test(e.message))).toBe(true);
  });

  it('#name= with 2+ comma-separated values is a compile-time error (only #tag= accepts multiple)', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram #name=foo,bar');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /#name takes exactly one value/.test(e.message))).toBe(true);
  });

  it('a duplicate meta key is a compile-time error', () => {
    const r = compile('WHEN pnl@positions.total <= -1 ALERT telegram #name=foo #name=bar');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /duplicate meta tag '#name'/.test(e.message))).toBe(true);
  });

  it('composes with the already-shipped ALERT/DO reordering (DO before ALERT)', () => {
    const canonical = compile('WHEN pnl@positions.total <= -1 ALERT telegram DO emit_log(message="x") #name="x"');
    const reversed = compile('WHEN pnl@positions.total <= -1 DO emit_log(message="x") ALERT telegram #name="x"');
    expect(reversed.ok).toBe(true);
    expect(reversed.agent).toEqual(canonical.agent);
  });

  it('composes with the WHEN-omission form (no WHEN at all)', () => {
    const withWhen = compile('WHEN always ALERT telegram #name="x"');
    const omitted = compile('ALERT telegram #name="x"');
    expect(omitted.ok).toBe(true);
    expect(omitted.agent).toEqual(withWhen.agent);
  });

  it('composes with a bare "DO ..." with no ALERT at all', () => {
    const r = compile('DO emit_log(message="x") #name="x" #group=hedge-book');
    expect(r.ok).toBe(true);
    expect(r.agent.name).toBe('x');
    expect(r.agent.group).toBe('hedge-book');
  });
});

// ── Sprint 12 — event_stmt: ALERT-only statement for notification
//    (kind='event') agents ────────────────────────────────────────────────

describe('Sprint 12 — event_stmt: full compile of the example statement', () => {
  it('compiles the example statement to the exact JSON shape', () => {
    const r = compile('ALERT log_tag=error, min_level=ERROR, render=error, telegram, email, log #name="Error alert"');
    expect(r.ok).toBe(true);
    expect(r.kind).toBe('event');
    expect(r.agent).toEqual({
      kind: 'event',
      conditions: { log: { tag: 'error', min_level: 'ERROR' } },
      events: [
        { channel: 'telegram', enabled: true },
        { channel: 'email', enabled: true },
        { channel: 'log', enabled: true },
      ],
      actions: [{ type: 'render', render: 'error' }],
      name: 'Error alert',
    });
  });

  it('log_tag= missing is a compile error', () => {
    const r = compile('ALERT render=error, telegram #name="x"');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /missing required 'log_tag='/.test(e.message))).toBe(true);
  });

  it('render= missing is a compile error', () => {
    const r = compile('ALERT log_tag=error, telegram #name="x"');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /missing required 'render='/.test(e.message))).toBe(true);
  });

  it('min_level= omitted defaults to "INFO"', () => {
    const r = compile('ALERT log_tag=error, render=error, telegram #name="x"');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions.log.min_level).toBe('INFO');
  });

  it('min_level=error (lowercase input) compiles to "ERROR" (the one field that IS case-normalized)', () => {
    const r = compile('ALERT log_tag=error, min_level=error, render=error, telegram #name="x"');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions.log.min_level).toBe('ERROR');
  });

  it('log_tag= and render= are preserved EXACTLY as typed — never case-normalized', () => {
    const r = compile('ALERT log_tag=Chase, render=ChaseCancel, telegram #name="x"');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions.log.tag).toBe('Chase');
    expect(r.agent.actions).toEqual([{ type: 'render', render: 'ChaseCancel' }]);
  });

  it('where={alert_event=cancel_unconfirmed} compiles with the value preserved as the literal string, NOT re-cased', () => {
    const r = compile('ALERT log_tag=chase, min_level=CRITICAL, render=chase_cancel, where={alert_event=cancel_unconfirmed}, ntfy #name="x"');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions.log.where).toEqual({ alert_event: 'cancel_unconfirmed' });
  });

  it('where= is omitted entirely from conditions.log when not supplied (never an empty {})', () => {
    const r = compile('ALERT log_tag=error, render=error, telegram #name="x"');
    expect(r.ok).toBe(true);
    expect('where' in r.agent.conditions.log).toBe(false);
  });

  it('a channel with its own param still compiles via the exact same ALERT call_list path (e.g. ntfy(priority=urgent))', () => {
    const r = compile('ALERT log_tag=error, render=error, ntfy(priority=urgent) #name="x"');
    expect(r.ok).toBe(true);
    expect(r.agent.events).toEqual([{ channel: 'ntfy', enabled: true, priority: 'urgent' }]);
  });

  it('msg= combined with log_tag= is a compile error naming both keys', () => {
    const r = compile('ALERT log_tag=error, render=error, msg="hi", telegram #name="x"');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /msg=.*doesn't apply to event agents.*log_tag=/.test(e.message))).toBe(true);
  });

  it('a per-channel own msg= is rejected the same way, naming the channel', () => {
    const r = compile('ALERT log_tag=error, render=error, telegram(msg="hi") #name="x"');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /doesn't apply to event agents \(channel 'telegram'\)/.test(e.message))).toBe(true);
  });

  it('log_tag= combined with WHEN is a compile error, not a confusing "unknown channel" fallthrough', () => {
    const r = compile('WHEN always ALERT log_tag=error, render=error DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /only valid on a bare ALERT-only statement, not combined with WHEN\/DO/.test(e.message))).toBe(true);
    expect(r.errors.some(e => /unknown channel/.test(e.message))).toBe(false);
  });

  it('log_tag= combined with a trailing DO is a compile error', () => {
    const r = compile('ALERT log_tag=error, render=error, telegram DO nop #name="x"');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /only valid on a bare ALERT-only statement, not combined with WHEN\/DO/.test(e.message))).toBe(true);
  });

  it('a duplicate event-meta key is a PARSE-time error (not deferred to compile time)', () => {
    const r = compile('ALERT log_tag=error, log_tag=chase, render=error, telegram #name="x"');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /duplicate event-meta key 'log_tag='/.test(e.message))).toBe(true);
  });

  it('dispatch correctly distinguishes "ALERT log_tag=..." (event_stmt) from an ordinary WHEN-omitted "ALERT msg=... telegram" (agent_stmt)', () => {
    const eventForm = compile('ALERT log_tag=error, render=error, telegram #name="x"');
    expect(eventForm.ok).toBe(true);
    expect(eventForm.kind).toBe('event');

    const ordinaryForm = compile('ALERT msg="hi", telegram DO nop #name="x"');
    expect(ordinaryForm.ok).toBe(true);
    expect(ordinaryForm.kind).toBe('agent');
    expect(ordinaryForm.agent.events).toEqual([{ channel: 'telegram', enabled: true, message: 'hi' }]);
  });

  it('a real channel bearing no event-meta kwarg still dispatches as the ordinary bare-ALERT agent_stmt (no regression)', () => {
    const r = compile('ALERT telegram DO nop #name="x"');
    expect(r.ok).toBe(true);
    expect(r.kind).toBe('agent');
  });
});

// ── Sprint 18 — event_stmt: render-action `gate=`, channel `gate=`,
//    and a bare `nop` channel list (empty events). ─────────────────────

describe('Sprint 18 — event_stmt: render-action gate= and empty-events nop', () => {
  it('gate=true on the render action compiles onto actions[0] as a sibling of type/render', () => {
    const r = compile('ALERT log_tag=error, render=error, gate=true, telegram #name="x"');
    expect(r.ok).toBe(true);
    expect(r.agent.actions).toEqual([{ type: 'render', render: 'error', gate: true }]);
  });

  it('gate= omitted entirely compiles actions[0] with NO gate key at all (never defaulted to false)', () => {
    const r = compile('ALERT log_tag=error, render=error, telegram #name="x"');
    expect(r.ok).toBe(true);
    expect(r.agent.actions).toEqual([{ type: 'render', render: 'error' }]);
    expect('gate' in r.agent.actions[0]).toBe(false);
  });

  it('gate= accepts any order relative to the other event-meta kwargs', () => {
    const r = compile('ALERT gate=false, render=x, log_tag=y, telegram #name="x"');
    expect(r.ok).toBe(true);
    expect(r.agent.actions).toEqual([{ type: 'render', render: 'x', gate: false }]);
  });

  it('gate= must be a boolean — a string value is a compile error', () => {
    const r = compile('ALERT log_tag=error, render=error, gate="yes", telegram #name="x"');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /gate.*expected true or false/.test(e.message))).toBe(true);
  });

  it('a duplicate gate= key is a PARSE-time error, same as the other four event-meta keys', () => {
    const r = compile('ALERT log_tag=error, render=error, gate=true, gate=false, telegram #name="x"');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /duplicate event-meta key 'gate='/.test(e.message))).toBe(true);
  });

  it('a channel\'s own gate=false kwarg compiles via the ordinary per-channel params path (unrelated to the action-level gate=)', () => {
    const r = compile('ALERT log_tag=orders, render=order_failure, telegram(gate=false), email(gate=false) #name="x"');
    expect(r.ok).toBe(true);
    expect(r.agent.events).toEqual([
      { channel: 'telegram', enabled: true, gate: false },
      { channel: 'email', enabled: true, gate: false },
    ]);
    expect(r.agent.actions).toEqual([{ type: 'render', render: 'order_failure' }]);
  });

  it('a bare "nop" channel list compiles to an empty events array (real cycle-breach-alert shape)', () => {
    const r = compile('ALERT log_tag=agent, render=breach, nop #name="Cycle breach"');
    expect(r.ok).toBe(true);
    expect(r.agent.events).toEqual([]);
    expect(r.agent.actions).toEqual([{ type: 'render', render: 'breach' }]);
  });

  it('"nop" combined with a trailing DO is still a compile error (same guard as a real channel list)', () => {
    const r = compile('ALERT log_tag=error, render=error, nop DO nop #name="x"');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /only valid on a bare ALERT-only statement, not combined with WHEN\/DO/.test(e.message))).toBe(true);
  });
});

// ═══════════════════════════════════════════════════════════════════════
// Sprint 16 — matched-row ($row) binding for DO-clause action params.
// Backend marker shape (confirmed, backend/api/algo/actions.py):
// {"$row": "<exposed_name>"} — a single-key dict, never a bare string.
// ═══════════════════════════════════════════════════════════════════════

describe('Sprint 16 — bare-NAME $row binding, both spellings', () => {
  it('bare NAME (account_id) compiles to {"$row": "account_id"}', () => {
    const r = compile(
      'WHEN pnl@positions.expiring_today > 0 DO close_position(account=account_id, symbol=symbol)'
    );
    expect(r.ok).toBe(true);
    expect(r.agent.actions).toEqual([{
      type: 'close_position',
      params: { account: { '$row': 'account_id' }, symbol: { '$row': 'symbol' } },
    }]);
  });

  it('the "result." alias (result.account_id) compiles to the IDENTICAL marker as the bare spelling', () => {
    const bare = compile(
      'WHEN pnl@positions.expiring_today > 0 DO close_position(account=account_id, symbol=symbol)'
    );
    const alias = compile(
      'WHEN pnl@positions.expiring_today > 0 DO close_position(account=result.account_id, symbol=result.symbol)'
    );
    expect(bare.ok).toBe(true);
    expect(alias.ok).toBe(true);
    expect(alias.agent).toEqual(bare.agent);
  });

  it('a bare NAME NOT matching any known exposed column still gets the pre-existing "must be quoted" error (no regression)', () => {
    const r = compile(
      'WHEN pnl@positions.expiring_today > 0 DO close_position(account="ZG0790", symbol=totally_unknown_bare_word)'
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /free-text value must be quoted/.test(e.message))).toBe(true);
  });
});

describe('Sprint 16 — valid per-row example (PLAN canonical)', () => {
  it('WHEN pnl@positions.expiring_today > 0 DO close_position(account=account_id, symbol=symbol) compiles successfully', () => {
    const r = compile(
      'WHEN pnl@positions.expiring_today > 0 DO close_position(account=account_id, symbol=symbol)'
    );
    expect(r.ok).toBe(true);
    expect(r.errors).toEqual([]);
    expect(r.agent.conditions).toEqual({ metric: 'pnl', scope: 'positions.expiring_today', op: '>', value: 0 });
  });

  // NOTE on a literal reading of the task brief's own example text,
  // `WHEN qty@positions.expiring_today > 0 DO close_position(...)`: this
  // does NOT compile against the real catalog — `qty` is NEVER a
  // registered METRIC token (`grep "token: qty" agent_grammar.yaml` finds
  // nothing); it exists ONLY as an exposed-column name on
  // `positions.expiring_today` (Sprint 15). The `metric@scope` ("@")
  // syntax strictly resolves `metric` against `catalog.metrics` only —
  // the correct, real-catalog-compilable spelling for "qty on this
  // scope" is Sprint 15's dot-access `col_ref` form,
  // `positions.expiring_today.qty > 0`, exercised below. Every OTHER
  // test in this file substitutes the pre-existing `pnl` metric for the
  // brief's `qty@scope` leaves for exactly this reason.
  it('the REAL compilable spelling of the brief\'s canonical example — dot-access col_ref, not qty@scope', () => {
    const r = compile(
      'WHEN positions.expiring_today.qty > 0 DO close_position(account=account_id, symbol=symbol)'
    );
    expect(r.ok).toBe(true);
    expect(r.errors).toEqual([]);
    expect(r.agent.conditions).toEqual({ metric: 'qty', scope: 'positions.expiring_today', op: '>', value: 0 });
    expect(r.agent.actions).toEqual([{
      type: 'close_position',
      params: { account: { '$row': 'account_id' }, symbol: { '$row': 'symbol' } },
    }]);
  });
});

describe('Sprint 16 — compile-time guard 1: aggregate (*.total) scope rejects $row', () => {
  it('the SAME DO action under positions.total (aggregate) is a compile error', () => {
    const r = compile(
      'WHEN pnl@positions.total > 0 DO close_position(account=account_id, symbol=symbol)'
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e =>
      /'account_id' cannot be bound from a matched row/.test(e.message)
      && /aggregate 'positions\.total'/.test(e.message)
    )).toBe(true);
  });

  it('account_id IS a real exposed column on positions.total, yet is STILL rejected — the aggregate guard is independent of column exposure', () => {
    // Sanity precondition: account_id really is registered on positions.total
    // in this fixture (guard-1 must fire even though guard-2's "is it
    // exposed on this scope" check alone would have passed).
    expect(CATALOG.columns.has('positions.total.account_id')).toBe(true);
    const r = compile('WHEN pnl@positions.total > 0 DO close_position(account=account_id, symbol="NIFTY25JULFUT")');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /aggregate 'positions\.total'/.test(e.message))).toBe(true);
  });
});

describe('Sprint 16 — compile-time guard 2: per-leaf-scope consistency (any_acct has no per-symbol identity)', () => {
  it('symbol=symbol under positions.any_acct is a compile error naming the field and scope', () => {
    const r = compile(
      'WHEN pnl@positions.any_acct > 0 DO close_position(account=account_id, symbol=symbol)'
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e =>
      /'symbol' is not an exposed column for scope 'positions\.any_acct'/.test(e.message)
    )).toBe(true);
  });

  it('account_id alone (no symbol) under positions.any_acct compiles fine — only the unexposed field fails', () => {
    const r = compile('WHEN pnl@positions.any_acct > 0 DO deactivate_agent');
    expect(r.ok).toBe(true);
  });

  it('mixed-scope leaves: a name exposed on ONE leaf scope but not the OTHER is rejected, naming the lacking scope', () => {
    // positions.expiring_today exposes account_id; positions.any_acct does
    // NOT expose symbol — combining both leaves in one condition tree
    // means $row:symbol must fail even though its OWN leaf (expiring_today)
    // does expose it, because the tree also contains an any_acct leaf.
    const r = compile(
      'WHEN (pnl@positions.expiring_today > 0) | (pnl@positions.any_acct > 0) '
      + 'DO close_position(account=account_id, symbol=symbol)'
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e =>
      /'symbol' is not an exposed column for scope 'positions\.any_acct'/.test(e.message)
    )).toBe(true);
  });
});

describe('Sprint 16 — compile-time guard 3: watchlist.* scope rejects $row even for an exposed column', () => {
  it('symbol=symbol under watchlist.all is a compile error, even though symbol IS exposed there', () => {
    expect(CATALOG.columns.has('watchlist.all.symbol')).toBe(true);
    const r = compile('WHEN pnl@watchlist.all > 0 DO close_position(account="ZG0790", symbol=symbol)');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e =>
      /'symbol' cannot be bound from a matched row/.test(e.message)
      && /watchlist row \('watchlist\.all'\)/.test(e.message)
    )).toBe(true);
  });
});

describe('Sprint 16 — $row fails closed against a Token ($ref) reference (own scope unknowable at compile time)', () => {
  it('a WHEN clause that is a bare Token ($ref) reference is a compile error naming the Token-reference reason', () => {
    // `loss-positions-default` is a Token reference (is_fragment_ref), so
    // the compiled condition is `{"$ref": "loss-positions-default"}` — its
    // real scope is resolved server-side, against whatever condition the
    // referenced Token itself declares, so it is genuinely unknowable
    // here at compile time.
    const r = compile('WHEN loss-positions-default DO close_position(account=account_id, symbol=symbol)');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e =>
      /includes a Token \(\$ref\) reference whose own per-row scope isn't visible/.test(e.message)
    )).toBe(true);
  });

  it('a Token ($ref) reference MIXED into a larger tree alongside a real per-row leaf still fails closed (not just the pure-$ref case)', () => {
    // Without the hasTokenRef flag, this would wrongly "succeed" by only
    // validating against the ONE leaf scope it can see
    // (positions.expiring_today, which DOES expose both fields) while
    // silently ignoring the sibling Token leaf's own unknown scope.
    const r = compile(
      'WHEN loss-positions-default & (pnl@positions.expiring_today > 0) '
      + 'DO close_position(account=account_id, symbol=symbol)'
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e =>
      /includes a Token \(\$ref\) reference whose own per-row scope isn't visible/.test(e.message)
    )).toBe(true);
  });

  it('bare "DO ..." with no WHEN at all defaults to "WHEN always" (funds.any_acct scope, per ALWAYS_LEAF) — account_id IS exposed there, but symbol never is', () => {
    expect(CATALOG.columns.has('funds.any_acct.account_id')).toBe(true);
    const r = compile('DO close_position(account=account_id, symbol=symbol)');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /'symbol' is not an exposed column for scope 'funds\.any_acct'/.test(e.message))).toBe(true);
    // account_id alone (no symbol) compiles fine against this real scope.
    const ok = compile('DO close_position(account=account_id, symbol="NIFTY25JULFUT")');
    expect(ok.ok).toBe(true);
  });
});

describe('Sprint 16 — $row fails closed against a negated (~) leaf (backend never binds a row for a `not` match)', () => {
  it('a $row reference under a negated leaf is a compile error naming the negation reason', () => {
    // Per the backend's own _ae_fanout_matches docstring, a `not`
    // composite emits a synthetic match with account=None, which
    // _ae_dispatch_actions treats as aggregate (_matched_row=None) —
    // this must be a compile-time error, not a silent runtime skip.
    const r = compile(
      'WHEN ~(pnl@positions.expiring_today > 0) DO close_position(account=account_id, symbol=symbol)'
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e =>
      /contains a negated \(~\) leaf \(no real per-row identity at runtime/.test(e.message)
    )).toBe(true);
  });

  it('a negated leaf MIXED into a larger tree alongside a real per-row leaf still fails closed', () => {
    const r = compile(
      'WHEN ~(pnl@positions.total > 0) & (pnl@positions.expiring_today > 0) '
      + 'DO close_position(account=account_id, symbol=symbol)'
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e =>
      /contains a negated \(~\) leaf \(no real per-row identity at runtime/.test(e.message)
    )).toBe(true);
  });
});

describe('Sprint 16 — $row binding for place_order/order(...)\'s own qty= param, and the lots= literal-symbol guard', () => {
  it('qty=qty ($row-bound, same contracts unit as place_order.qty) compiles to {"$row": "qty"}', () => {
    const r = compile(
      'WHEN pnl@positions.expiring_today > 0 '
      + 'DO order(account=account_id, symbol=symbol, side="SELL", qty=qty)'
    );
    expect(r.ok).toBe(true);
    expect(r.agent.actions).toEqual([{
      type: 'place_order',
      params: {
        account: { '$row': 'account_id' }, symbol: { '$row': 'symbol' },
        side: 'SELL', qty: { '$row': 'qty' },
      },
    }]);
  });

  it('lots= with a $row-bound symbol is a clear compile error, not a crash/garbled "[object Object]" message', () => {
    const r = compile(
      'WHEN pnl@positions.expiring_today > 0 '
      + 'DO order(account=account_id, symbol=symbol, side="SELL", lots=1)'
    );
    expect(r.ok).toBe(false);
    expect(r.errors.some(e =>
      /lots= requires a literal \(quoted\) symbol to resolve lot size at compile time/.test(e.message)
      && /use qty= instead/.test(e.message)
    )).toBe(true);
    expect(r.errors.some(e => /\[object Object\]/.test(e.message))).toBe(false);
  });

  it('lots= with a LITERAL (quoted) symbol is unaffected — still resolves the lot size normally', () => {
    const r = compile(
      'WHEN pnl@positions.expiring_today > 0 '
      + 'DO order(account=account_id, symbol="NIFTY25JULFUT", side="SELL", lots=2)'
    );
    expect(r.ok).toBe(true);
    expect(r.agent.actions[0].params.qty).toBe(150); // 2 lots x 75 (fixture lot size)
  });
});

// ═══════════════════════════════════════════════════════════════════════
// Sprint 19 — `account:value`/`symbol:value` qualifier syntax on scope
// chains, short scope-family aliases (pos/hld/fnd/wl), and (bundled per
// the Sprint 18 follow-up note) `match=[...]` event-meta syntax.
// ═══════════════════════════════════════════════════════════════════════

describe('Sprint 19 — account:/symbol: qualifier syntax on scope chains', () => {
  it('positions.account:ZG0790 (literal) compiles to scope:"positions.any_acct" (unchanged) + scope_filter', () => {
    const r = compile('WHEN pnl@positions.account:ZG0790 <= -50000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      metric: 'pnl', scope: 'positions.any_acct', op: '<=', value: -50000,
      scope_filter: { account_id: 'ZG0790' },
    });
  });

  it('positions.account:* compiles to bare scope:"positions.any_acct" — NO scope_filter key at all', () => {
    const r = compile('WHEN pnl@positions.account:* <= -50000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'pnl', scope: 'positions.any_acct', op: '<=', value: -50000 });
    expect(r.agent.conditions.scope_filter).toBeUndefined();
  });

  it('positions.account:worst compiles to scope:"positions.worst_acct" — zero new mechanism, pure alias, no scope_filter', () => {
    const r = compile('WHEN pnl@positions.account:worst <= -50000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'pnl', scope: 'positions.worst_acct', op: '<=', value: -50000 });
    expect(r.agent.conditions.scope_filter).toBeUndefined();
  });

  it('positions.expiring_today.symbol:NIFTY25JULFUT compiles correctly (scope unchanged, scope_filter on symbol)', () => {
    const r = compile('WHEN pnl@positions.expiring_today.symbol:NIFTY25JULFUT <= -5000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      metric: 'pnl', scope: 'positions.expiring_today', op: '<=', value: -5000,
      scope_filter: { symbol: 'NIFTY25JULFUT' },
    });
  });

  it('the SAME .symbol:X qualifier under positions.total is a compile error — no symbol dimension on that scope', () => {
    const r = compile('WHEN pnl@positions.total.symbol:NIFTY25JULFUT <= -5000 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /not a valid qualifier for scope 'positions\.total'/.test(e.message))).toBe(true);
  });

  it('positions.expiring_today.symbol:* is redundant with omitting the filter — no scope_filter key', () => {
    const r = compile('WHEN pnl@positions.expiring_today.symbol:* <= -5000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'pnl', scope: 'positions.expiring_today', op: '<=', value: -5000 });
  });

  it('an unknown qualifier key (e.g. "foo:") is a clear compile error', () => {
    const r = compile('WHEN pnl@positions.foo:bar <= -5000 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /unknown scope qualifier 'foo:'/.test(e.message))).toBe(true);
  });

  it('a qualifier with no preceding scope ("@account:X") is a compile error', () => {
    const r = compile('WHEN pnl@account:ZG0790 <= -5000 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /expected a scope before a qualifier segment/.test(e.message))).toBe(true);
  });

  it('":worst" on a family with no existing worst_acct token (funds) is a real, intentionally-deferred gap — compile error', () => {
    const r = compile('WHEN avail_margin@funds.account:worst <= 0 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /has no existing scope token to alias onto/.test(e.message))).toBe(true);
  });

  it('":worst" combined with a second qualifier is rejected — it already selects one specific row', () => {
    const r = compile('WHEN pnl@positions.account:worst.symbol:NIFTY25JULFUT <= -5000 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /must be the only qualifier on this scope/.test(e.message))).toBe(true);
  });

  it('a scope-narrowing segment after a qualifier segment is a trailing-only violation', () => {
    const r = compile('WHEN pnl@positions.account:ZG0790.expiring_today <= -5000 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /must be trailing/.test(e.message))).toBe(true);
  });

  it('an args-violation on the scope-family segment is never silently swallowed by the implicit any_acct fallback', () => {
    const r = compile('WHEN pnl@positions(foo=5).account:ZG0790 <= -50000 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /takes no arguments/.test(e.message))).toBe(true);
  });

  it('chained-comparison ("between") form also threads scope_filter through', () => {
    const r = compile('WHEN -5000 <= pnl@positions.account:ZG0790 <= 5000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      metric: 'pnl', scope: 'positions.any_acct', op: 'between', value: [-5000, 5000],
      scope_filter: { account_id: 'ZG0790' },
    });
  });

  it('a STRICT between-chain (the all:[...] fallback, not native "between") also threads scope_filter onto BOTH leaves', () => {
    const r = compile('WHEN -5000 < pnl@positions.account:ZG0790 < 5000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      all: [
        { metric: 'pnl', scope: 'positions.any_acct', op: '>', value: -5000, scope_filter: { account_id: 'ZG0790' } },
        { metric: 'pnl', scope: 'positions.any_acct', op: '<', value: 5000, scope_filter: { account_id: 'ZG0790' } },
      ],
    });
  });

  // ── advisor-flagged correctness guards ──────────────────────────────
  it('":worst" is REJECTED when there is a narrowing scope segment before it — no "worst within a narrower scope" token exists (would otherwise silently discard the narrowing)', () => {
    const r = compile('WHEN pnl@positions.expiring_today.symbol:worst <= -5000 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /only valid directly off the bare 'positions' scope family/.test(e.message))).toBe(true);
  });

  it('"*" is validated against the SAME exposed-column registry as a literal value — an unsupported qualifier key with "*" still errors', () => {
    const r = compile('WHEN pnl@positions.foo:* <= -5000 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /unknown scope qualifier 'foo:'/.test(e.message))).toBe(true);
  });

  it('"*" on a qualifier with no exposed column on that scope still errors (funds has no symbol dimension)', () => {
    const r = compile('WHEN avail_margin@funds.symbol:* <= 0 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /not a valid qualifier for scope 'funds\.any_acct'/.test(e.message))).toBe(true);
  });
});

describe('Sprint 19 — scope_filter decompile round-trip (compile → decompile → recompile)', () => {
  it('positions.account:ZG0790 (literal qualifier) round-trips byte-identical', () => {
    const r = compile('WHEN pnl@positions.account:ZG0790 <= -50000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    const dec = decompileCondition(r.agent.conditions, CATALOG);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toBe('pnl@positions.any_acct.account:ZG0790 <= -50000');
    const recompiled = compile(`WHEN ${dec.text} ALERT telegram DO nop`);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(r.agent.conditions);
  });

  it('positions.expiring_today.symbol:NIFTY25JULFUT round-trips byte-identical', () => {
    const r = compile('WHEN pnl@positions.expiring_today.symbol:NIFTY25JULFUT <= -5000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    const dec = decompileCondition(r.agent.conditions, CATALOG);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toBe('pnl@positions.expiring_today.symbol:NIFTY25JULFUT <= -5000');
    const recompiled = compile(`WHEN ${dec.text} ALERT telegram DO nop`);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(r.agent.conditions);
  });

  it('a STRICT between-chain (all:[...]) with a matching scope_filter on both leaves decompiles back to the pretty chained form', () => {
    const r = compile('WHEN -5000 < pnl@positions.account:ZG0790 < 5000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    const dec = decompileCondition(r.agent.conditions, CATALOG);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toBe('-5000 < pnl@positions.any_acct.account:ZG0790 < 5000');
    const recompiled = compile(`WHEN ${dec.text} ALERT telegram DO nop`);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(r.agent.conditions);
  });

  it('a qualifier combined with a $row binding (the motivating multi-row-per-account use case) compiles and round-trips', () => {
    const r = compile(
      'WHEN pnl@positions.expiring_today.symbol:NIFTY25JULFUT > 0 '
      + 'DO close_position(account=account_id, symbol=symbol)'
    );
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      metric: 'pnl', scope: 'positions.expiring_today', op: '>', value: 0,
      scope_filter: { symbol: 'NIFTY25JULFUT' },
    });
    expect(r.agent.actions).toEqual([{
      type: 'close_position',
      params: { account: { '$row': 'account_id' }, symbol: { '$row': 'symbol' } },
    }]);
  });
});

describe('Sprint 19 — short scope-family aliases (pos/hld/fnd/wl)', () => {
  it('pos.any_acct compiles identically to positions.any_acct (same scope token in the compiled JSON)', () => {
    const viaAlias = compile('WHEN pnl@pos.any_acct <= -50000 ALERT telegram DO nop');
    const viaLong = compile('WHEN pnl@positions.any_acct <= -50000 ALERT telegram DO nop');
    expect(viaAlias.ok).toBe(true);
    expect(viaAlias.agent.conditions).toEqual(viaLong.agent.conditions);
    expect(viaAlias.agent.conditions.scope).toBe('positions.any_acct');
  });

  it('pos.any_acct DECOMPILES to the canonical long form "positions.any_acct", never the alias (mandatory PLAN.md case)', () => {
    const r = compile('WHEN pnl@pos.any_acct <= -50000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    const dec = decompileCondition(r.agent.conditions, CATALOG);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toBe('pnl@positions.any_acct <= -50000');
    expect(dec.text).not.toContain('pos.');
  });

  it('fnd.any_acct compiles to the canonical funds.any_acct token', () => {
    const r = compile('WHEN avail_margin@fnd.any_acct <= 0 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions.scope).toBe('funds.any_acct');
  });

  it('the short alias also combines with a qualifier (pos.account:ZG0790)', () => {
    const r = compile('WHEN pnl@pos.account:ZG0790 <= -50000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      metric: 'pnl', scope: 'positions.any_acct', op: '<=', value: -50000,
      scope_filter: { account_id: 'ZG0790' },
    });
  });

  it('the alias also works for the bare col_ref dot-access form (Sprint 15)', () => {
    const r = compile('WHEN pos.total.pnl <= -50000 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 });
  });

  it('an unknown alias-looking prefix still fails as an unknown scope (no silent guess)', () => {
    const r = compile('WHEN pnl@xyz.any_acct <= -50000 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /unknown scope/.test(e.message))).toBe(true);
  });
});

describe('Sprint 18 follow-up — match=[...] event-meta syntax', () => {
  it('compiles to the exact {"any": [{"log": {...}}, {"log": {...}}]} shape', () => {
    const r = compile(
      'ALERT match=[{tag=orders, min_level=WARNING, where={alert_event="cancel_unconfirmed"}}, '
      + '{tag=gtt, min_level=WARNING}], render=template_attach, telegram, email'
    );
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      any: [
        { log: { tag: 'orders', min_level: 'WARNING', where: { alert_event: 'cancel_unconfirmed' } } },
        { log: { tag: 'gtt', min_level: 'WARNING' } },
      ],
    });
    expect(r.agent.events).toEqual([
      { channel: 'telegram', enabled: true },
      { channel: 'email', enabled: true },
    ]);
    expect(r.agent.actions).toEqual([{ type: 'render', render: 'template_attach' }]);
  });

  it('a match= entry missing min_level defaults to INFO, same default the simple form uses', () => {
    const r = compile('ALERT match=[{tag=a}, {tag=b}], render=x, telegram');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      any: [{ log: { tag: 'a', min_level: 'INFO' } }, { log: { tag: 'b', min_level: 'INFO' } }],
    });
  });

  it('match= used together with log_tag= in the same statement is a compile error', () => {
    const r = compile('ALERT match=[{tag=a}, {tag=b}], log_tag=x, render=y, telegram');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /match= is mutually exclusive with log_tag=/.test(e.message))).toBe(true);
  });

  it('match= used together with where= in the same statement is a compile error, naming where=', () => {
    const r = compile('ALERT match=[{tag=a}, {tag=b}], where={x=1}, render=y, telegram');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /match= is mutually exclusive with where=/.test(e.message))).toBe(true);
  });

  it('an empty match=[] is a compile error', () => {
    const r = compile('ALERT match=[], render=x, telegram');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /match= requires at least one/.test(e.message))).toBe(true);
  });

  it('a match= entry missing tag= is a compile error naming the entry index', () => {
    const r = compile('ALERT match=[{min_level=WARNING}, {tag=b}], render=x, telegram');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /match\[0\]: missing required 'tag='/.test(e.message))).toBe(true);
  });

  it('an unknown key inside a match= entry is a compile error', () => {
    const r = compile('ALERT match=[{tag=a, bogus=1}, {tag=b}], render=x, telegram');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /unknown key 'bogus'/.test(e.message))).toBe(true);
  });
});

// ── Sprint 20, item 2 — exchange/product defaults + real required_if ───

describe('Sprint 20 (item 2) — exchange derived from the instruments cache, product default follows it', () => {
  it('exchange= omitted resolves from a single-listed symbol, product defaults to NRML for F&O', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.exchange).toBe('NFO');
    expect(r.ticket.product).toBe('NRML');
  });
  it('an explicit exchange= always wins over the resolver, even for an ambiguous dual-listed symbol', () => {
    const r = compile('order(account="ZG0790", symbol="RELIANCE", exchange="BSE", side="SELL", qty=10, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.exchange).toBe('BSE');
    expect(r.ticket.product).toBe('CNC');
  });
});

describe('Sprint 20 (item 2) — required_if checks the ACTUALLY-emitted order_type, not the catalog default', () => {
  it('order_type omitted (defaults to the real LIMIT the ticket ships with) with no price= is now a compile error', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)', { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /price is required when order_type=LIMIT/.test(e.message))).toBe(true);
  });
  it('order_type omitted WITH price= compiles fine (the fix only closes the price-less gap)', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.order_type).toBe('LIMIT');
    expect(r.ticket.price).toBe(100);
  });
  it('order_type="MARKET" explicitly still compiles fine with no price= (no regression)', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, order_type="MARKET")', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.price).toBeNull();
  });
});

// ── Sprint 20, item 3 — &/| precedence, and/or/not word aliases, hints ──

describe('Sprint 20 (item 3) — & binds tighter than | (standard precedence)', () => {
  it('a | b & c means a | (b & c), not (a | b) & c', () => {
    // pnl > 0 is TRUE, day_pct > 0 is FALSE, avail_margin > 0 is FALSE
    // (a | b) & c -> (T|F) & F -> F.  a | (b & c) -> T | (F&F) -> T.
    const r = compile(
      'WHEN pnl@positions.total > 0 | day_pct@positions.total > 0 & avail_margin@funds.total > 0 '
      + 'ALERT telegram DO nop'
    );
    expect(r.ok).toBe(true);
    // Wire vocabulary is `any`/`all` (not `or`/`and` — that's only the
    // internal AST node type) per `compileCondNode`'s existing mapping.
    expect(r.agent.conditions).toEqual({
      any: [
        { metric: 'pnl', scope: 'positions.total', op: '>', value: 0 },
        {
          all: [
            { metric: 'day_pct', scope: 'positions.total', op: '>', value: 0 },
            { metric: 'avail_margin', scope: 'funds.total', op: '>', value: 0 },
          ],
        },
      ],
    });
  });

  it('explicit parens still override precedence exactly as before', () => {
    const r = compile(
      'WHEN (pnl@positions.total > 0 | day_pct@positions.total > 0) & avail_margin@funds.total > 0 '
      + 'ALERT telegram DO nop'
    );
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      all: [
        {
          any: [
            { metric: 'pnl', scope: 'positions.total', op: '>', value: 0 },
            { metric: 'day_pct', scope: 'positions.total', op: '>', value: 0 },
          ],
        },
        { metric: 'avail_margin', scope: 'funds.total', op: '>', value: 0 },
      ],
    });
  });

  it('a decompiled existing agent (fully-parenthesized mixed &/| tree) round-trips byte-identically under the new precedence', () => {
    // Regression guard (per the task's own instruction to check this
    // before changing precedence) — confirms the pre-existing fully-
    // parenthesized fixture (identical to the one already covered in
    // the "canonical 2-op boolean tree" describe block) is UNAFFECTED,
    // since every nested composite the decompiler emits is always
    // wrapped in its own disambiguating parens regardless of precedence.
    const text = 'WHEN ((mean_pnl(minutes=30)@positions.total <= -50000) & (stdev_pnl(minutes=30)@positions.total > 10000)) '
      + '| (avail_margin@funds.total < 0) ALERT telegram DO nop';
    const r1 = compile(text);
    expect(r1.ok).toBe(true);
    const d = decompileCondition(r1.agent.conditions, CATALOG);
    expect(d.errors).toEqual([]);
    const r2 = compile(`WHEN ${d.text} ALERT telegram DO nop`);
    expect(r2.ok).toBe(true);
    expect(r2.agent.conditions).toEqual(r1.agent.conditions);
  });
});

describe('Sprint 20 (item 3) — and/or/not word aliases for &/|/~', () => {
  it('"and"/"or" compile identically to "&"/"|"', () => {
    const viaWords = compile('WHEN pnl@positions.total > 0 and avail_margin@funds.total > 0 ALERT telegram DO nop');
    const viaSymbols = compile('WHEN pnl@positions.total > 0 & avail_margin@funds.total > 0 ALERT telegram DO nop');
    expect(viaWords.ok).toBe(true);
    expect(viaWords.agent.conditions).toEqual(viaSymbols.agent.conditions);
  });
  it('"or" also respects the new precedence against "&"', () => {
    const viaWords = compile(
      'WHEN pnl@positions.total > 0 or day_pct@positions.total > 0 and avail_margin@funds.total > 0 '
      + 'ALERT telegram DO nop'
    );
    const viaSymbols = compile(
      'WHEN pnl@positions.total > 0 | day_pct@positions.total > 0 & avail_margin@funds.total > 0 '
      + 'ALERT telegram DO nop'
    );
    expect(viaWords.ok).toBe(true);
    expect(viaWords.agent.conditions).toEqual(viaSymbols.agent.conditions);
  });
  it('"not" negates a term exactly like "~"', () => {
    const viaWord = compile('WHEN not pnl@positions.total > 0 ALERT telegram DO nop');
    const viaSymbol = compile('WHEN ~(pnl@positions.total > 0) ALERT telegram DO nop');
    expect(viaWord.ok).toBe(true);
    expect(viaWord.agent.conditions).toEqual(viaSymbol.agent.conditions);
  });
  it('"not in"/"not_in" comparator is completely unaffected by the new "not" term-negation alias (different grammar position, no collision)', () => {
    const r = compile('WHEN side@positions.total not in [BUY] ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions.op).toBe('not_in');
  });
  it('"always and x" is rejected the same way "always & x" already was', () => {
    const r = compile('WHEN always and (pnl@positions.total <= -1) ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /"always" is standalone-only/.test(e.message))).toBe(true);
  });
  it('"and"/"or" are reserved — "and(...)"/"or(...)" cannot be used as a token/channel name', () => {
    const r = compile('WHEN pnl@positions.total > 0 ALERT telegram DO emit_log(message="x"), and()');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /reserved word/.test(e.message))).toBe(true);
  });
});

describe('Sprint 20 (item 3) — targeted error hints for "=" and "!"', () => {
  it('"=" in comparator position hints at "=="', () => {
    const r = compile('WHEN pnl@positions.total = -1 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /use "==" for equality/.test(e.message))).toBe(true);
  });
  it('"!x" hints at "~"/"not" for negation', () => {
    const r = compile('WHEN !pnl@positions.total > 0 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /use '~' or 'not' to negate/.test(e.message))).toBe(true);
  });
});

// ── Sprint 20, item 4 — typed-alias-aware error messages ────────────────

describe('Sprint 20 (item 4) — quoting-error names the ALIAS the trader typed, not the canonical field', () => {
  it('order(acct=ZG0790, ...) names "acct" in the quoting error, not "account"', () => {
    const r = compile('order(acct=ZG0790, symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100)', { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /order\.acct: free-text value must be quoted/.test(e.message))).toBe(true);
  });
});

// ── Sprint 20, item 5 — describeErrorPosition() + position threading ───

describe('Sprint 20 (item 5) — describeErrorPosition()', () => {
  it('returns 1-based line/column and a bounded snippet with a caret offset', () => {
    const text = 'order(acct=ZG0790, symbol="NIFTY25JULFUT")';
    const d = describeErrorPosition(text, 6);
    expect(d).not.toBeNull();
    expect(d.line).toBe(1);
    expect(d.column).toBe(7);
    expect(d.snippet).toContain('acct');
    expect(d.snippet[d.caretOffset]).toBe('a');
  });
  it('computes line/column correctly across an embedded newline', () => {
    const text = 'WHEN pnl@positions.total > 0\nALERT telegram DO nop';
    const d = describeErrorPosition(text, text.indexOf('ALERT')); // 'A' of ALERT, right after the \n
    expect(d.line).toBe(2);
    expect(d.column).toBe(1);
  });
  it('returns null for an out-of-range or non-numeric position (never throws, never a garbled result)', () => {
    expect(describeErrorPosition('abc', -1)).toBeNull();
    expect(describeErrorPosition('abc', 999)).toBeNull();
    expect(describeErrorPosition('abc', undefined)).toBeNull();
    expect(describeErrorPosition('abc', NaN)).toBeNull();
  });
  it('sets truncatedStart/truncatedEnd when the window is clipped by a short windowSize', () => {
    const text = 'x'.repeat(100);
    const d = describeErrorPosition(text, 50, 5);
    expect(d.truncatedStart).toBe(true);
    expect(d.truncatedEnd).toBe(true);
    expect(d.snippet.length).toBe(10);
  });
  it('a real compile error\'s position, fed through describeErrorPosition, lands on the offending "acct=ZG0790" argument', () => {
    const text = 'order(acct=ZG0790, symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100)';
    const r = compile(text, { mode: 'paper' });
    expect(r.ok).toBe(false);
    const err = r.errors.find(e => /must be quoted/.test(e.message));
    expect(typeof err.position).toBe('number');
    // position lands at (or before) the fused "acct=ZG0790" token — the
    // tokenizer fuses an unquoted "key=value" into one atom with no
    // finer-grained sub-offset for just the value half (see engine.js's
    // `tokenizeCallStyle`), so the snippet contains the whole argument.
    expect(text.slice(err.position)).toContain('acct=ZG0790');
    const d = describeErrorPosition(text, err.position);
    expect(d).not.toBeNull();
    expect(d.snippet).toContain('acct=ZG0790');
  });
});

// ═══════════════════════════════════════════════════════════════════════
// Sprint 21 — orders.any_acct / quote.tracked scope families.
//
// Backend half committed in e8b1ad38 (backend/api/algo/grammar.py +
// agent_evaluator.py + agent_grammar.yaml) — real shapes confirmed against
// that diff + backend/tests/test_orders_quote_scopes.py directly, not just
// .claude/PLAN.md's summary (two discrepancies found this way, documented
// inline below).
// ═══════════════════════════════════════════════════════════════════════

describe('Sprint 21 — orders.any_acct: the generic Sprint-15 dot-access mechanism picks it up for free', () => {
  it('orders.any_acct.status == "OPEN" compiles correctly (PLAN item 1 + Test coverage)', () => {
    const r = compile('WHEN orders.any_acct.status == "OPEN" ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'status', scope: 'orders.any_acct', op: '==', value: 'OPEN' });
  });

  it('orders.any_acct.status != "OPEN" also compiles (!= side of the new string-equality branch)', () => {
    const r = compile('WHEN orders.any_acct.status != "OPEN" ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({ metric: 'status', scope: 'orders.any_acct', op: '!=', value: 'OPEN' });
  });

  it('a string-typed column still rejects an ordered comparator (<, >, etc) — only ==/!= are valid', () => {
    const r = compile('WHEN orders.any_acct.status < "OPEN" ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /only supports == \/ !=/.test(e.message))).toBe(true);
  });

  it('every other new exposed name (side/qty/price/order_id/account_id/exchange) resolves via the SAME generic mechanism', () => {
    expect(compile('WHEN orders.any_acct.side == "SELL" ALERT telegram DO nop').ok).toBe(true);
    expect(compile('WHEN orders.any_acct.qty >= 5 ALERT telegram DO nop').ok).toBe(true);
    expect(compile('WHEN orders.any_acct.price > 100 ALERT telegram DO nop').ok).toBe(true);
    expect(compile('WHEN orders.any_acct.order_id == "ABC123" ALERT telegram DO nop').ok).toBe(true);
    expect(compile('WHEN orders.any_acct.account_id == "ZG0790" ALERT telegram DO nop').ok).toBe(true);
    expect(compile('WHEN orders.any_acct.exchange == "NFO" ALERT telegram DO nop').ok).toBe(true);
  });

  it('"@" form (metric@scope) also compiles for a bare-column metric that has no dedicated metric token, via existing dot-access fallback resolution being unnecessary here — confirms orders.any_acct is a real catalog scope token usable as any other', () => {
    // orders.any_acct itself has no bare-call form issue — this just
    // confirms the scope token round-trips through the ordinary
    // qualifier-free path with NO code change, e.g. combined in a tree.
    const r = compile(
      'WHEN (orders.any_acct.qty >= 5) & (orders.any_acct.side == "BUY") ALERT telegram DO nop'
    );
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      all: [
        { metric: 'qty', scope: 'orders.any_acct', op: '>=', value: 5 },
        { metric: 'side', scope: 'orders.any_acct', op: '==', value: 'BUY' },
      ],
    });
  });

  it('orders.account:ZG0790 resolves through the implicit any_acct fallback — no new qualifier mechanism needed (advisor-confirmed "should work as-is")', () => {
    const r = compile('WHEN pnl@orders.account:ZG0790 > 0 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      metric: 'pnl', scope: 'orders.any_acct', op: '>', value: 0,
      scope_filter: { account_id: 'ZG0790' },
    });
  });
});

describe('Sprint 21 — decompile round-trip for orders.any_acct', () => {
  it('orders.any_acct.status == "OPEN" round-trips through decompile → recompile (decompiler prefers the bare spelling, same "bare when possible" rule _renderListItem already applies to in/not_in list items)', () => {
    const r = compile('WHEN orders.any_acct.status == "OPEN" ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    const dec = decompileCondition(r.agent.conditions, CATALOG);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toBe('orders.any_acct.status == OPEN');
    const recompiled = compile(`WHEN ${dec.text} ALERT telegram DO nop`);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(r.agent.conditions);
  });
});

describe('Sprint 21 — $row fan-out binding for order_id/status/side on a matched orders.any_acct row (PLAN item 3)', () => {
  it('cancel_order(account=account_id, broker_order_id=order_id) inside a WHEN ... orders.any_acct compiles to the $row marker shape', () => {
    // NOTE on PLAN.md's own literal example text, `cancel_order(order_id=
    // order_id)`: this does NOT compile against the real catalog —
    // `cancel_order`'s real params_schema key is `broker_order_id`, never
    // `order_id` (confirmed against agent_grammar.yaml directly — see
    // this sprint's handback report). The correct, real-catalog-compilable
    // spelling keeps the bare-NAME VALUE as `order_id` (the real exposed
    // column name on orders.any_acct, resolved via $row) but the KEYWORD
    // on the left must be the schema's own `broker_order_id`.
    const r = compile(
      'WHEN orders.any_acct.status == "OPEN" DO cancel_order(account=account_id, broker_order_id=order_id)'
    );
    expect(r.ok).toBe(true);
    expect(r.agent.actions).toEqual([{
      type: 'cancel_order',
      params: { account: { '$row': 'account_id' }, broker_order_id: { '$row': 'order_id' } },
    }]);
  });

  it('the "result." alias spelling resolves to the identical marker', () => {
    const r = compile(
      'WHEN orders.any_acct.status == "OPEN" DO cancel_order(account=result.account_id, broker_order_id=result.order_id)'
    );
    expect(r.ok).toBe(true);
    expect(r.agent.actions).toEqual([{
      type: 'cancel_order',
      params: { account: { '$row': 'account_id' }, broker_order_id: { '$row': 'order_id' } },
    }]);
  });

  it('side is ALSO exposed and bindable via $row (not just order_id) — e.g. a hypothetical future action taking a side= param', () => {
    // emit_log's `message` param is a free-text string (token_ref_ok),
    // reused here purely as a vehicle to prove `side`/`status` resolve
    // via $row — no new action needed for this generic-mechanism check.
    const r = compile(
      'WHEN orders.any_acct.qty >= 5 DO emit_log(message=side)'
    );
    expect(r.ok).toBe(true);
    expect(r.agent.actions).toEqual([{ type: 'emit_log', params: { message: { '$row': 'side' } } }]);
  });

  it('orders.any_acct is NOT an aggregate and NOT a watchlist scope — neither $row guard misfires on it', () => {
    // Sanity precondition + regression guard: orders.any_acct must never
    // be caught by either Sprint-16 guard that exists for OTHER scopes.
    const r = compile(
      'WHEN orders.any_acct.status == "OPEN" DO cancel_order(account=account_id, broker_order_id=order_id)'
    );
    expect(r.ok).toBe(true);
    expect(r.errors).toEqual([]);
  });
});

describe('Sprint 21 — quote.tracked: generic dot-access mechanism + the real gap it exposed', () => {
  it('quote.tracked.bid/ask/volume (no dedicated metric token) compile via plain dot-access, same mechanism as orders.any_acct — but (unlike orders.any_acct) still require the symbol: qualifier, proven in the next describe block', () => {
    expect(compile('WHEN quote.tracked.symbol:NIFTY25JULFUT.symbol == "NIFTY25JULFUT" ALERT telegram DO nop').ok).toBe(true);
  });

  it('ltp@quote.tracked (the "@" form, since ltp IS a real registered metric) compiles for free with no qualifier', () => {
    const r = compile('WHEN ltp@quote.tracked <= 100 ALERT telegram DO nop');
    // no scope_filter present — this leaf will be rejected by the NEW
    // quote.* compile-time guard (PLAN item 2), proven in the next
    // describe block. Asserted here only as the pre-guard parse/scope
    // resolution sanity check.
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /requires a specific symbol:/.test(e.message))).toBe(true);
  });
});

describe('Sprint 21 — quote.tracked requires a non-wildcard symbol: qualifier (PLAN item 2, compile-time guard)', () => {
  it('quote.tracked.ltp with NO symbol qualifier is a compile error (Test coverage #3)', () => {
    const r = compile('WHEN quote.tracked.ltp <= 100 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e =>
      /quote\.tracked requires a specific symbol: qualifier/.test(e.message)
      && /quote\.tracked\.symbol:NIFTY25JULFUT/.test(e.message)
      && /no 'any symbol' aggregate exists for market data/.test(e.message)
    )).toBe(true);
  });

  it('ltp@quote.tracked with NO symbol qualifier (the "@" form) is the SAME compile error', () => {
    const r = compile('WHEN ltp@quote.tracked <= 100 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /quote\.tracked requires a specific symbol: qualifier/.test(e.message))).toBe(true);
  });

  it('quote.tracked.symbol:*.ltp (wildcard) is the SAME compile error (Test coverage #4)', () => {
    const r = compile('WHEN quote.tracked.symbol:*.ltp <= 100 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /quote\.tracked requires a specific symbol: qualifier/.test(e.message))).toBe(true);
  });

  it('ltp@quote.tracked.symbol:* (the "@" form wildcard) is the SAME compile error', () => {
    const r = compile('WHEN ltp@quote.tracked.symbol:* <= 100 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /quote\.tracked requires a specific symbol: qualifier/.test(e.message))).toBe(true);
  });

  it('quote.tracked.symbol:NIFTY25JULFUT with .ltp <= 100 compiles successfully (Test coverage #5 — the real new mechanism: dot-access col_ref now carries an embedded qualifier)', () => {
    const r = compile('WHEN quote.tracked.symbol:NIFTY25JULFUT.ltp <= 100 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      metric: 'ltp', scope: 'quote.tracked', op: '<=', value: 100,
      scope_filter: { symbol: 'NIFTY25JULFUT' },
    });
  });

  it('ltp@quote.tracked.symbol:NIFTY25JULFUT <= 100 (the "@" form, pre-existing Sprint 19 qualifier mechanism) compiles identically', () => {
    const atForm = compile('WHEN ltp@quote.tracked.symbol:NIFTY25JULFUT <= 100 ALERT telegram DO nop');
    const dotForm = compile('WHEN quote.tracked.symbol:NIFTY25JULFUT.ltp <= 100 ALERT telegram DO nop');
    expect(atForm.ok).toBe(true);
    expect(atForm.agent.conditions).toEqual(dotForm.agent.conditions);
  });

  it('quote.tracked.symbol:NIFTY25JULFUT.bid <= 100 (a column with NO dedicated metric token) compiles via the same new mechanism — proves bid/ask/volume are reachable at all (they have no "@" form)', () => {
    const r = compile('WHEN quote.tracked.symbol:NIFTY25JULFUT.bid <= 100 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      metric: 'bid', scope: 'quote.tracked', op: '<=', value: 100,
      scope_filter: { symbol: 'NIFTY25JULFUT' },
    });
  });

  it('the between-chain (inclusive) form also threads the symbol: qualifier through dot-access', () => {
    const r = compile('WHEN 10 <= quote.tracked.symbol:NIFTY25JULFUT.bid <= 200 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      metric: 'bid', scope: 'quote.tracked', op: 'between', value: [10, 200],
      scope_filter: { symbol: 'NIFTY25JULFUT' },
    });
  });

  it('the between-chain (strict, "all" fallback) form also threads the symbol: qualifier onto BOTH leaves', () => {
    const r = compile('WHEN 10 < quote.tracked.symbol:NIFTY25JULFUT.bid < 200 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    expect(r.agent.conditions).toEqual({
      all: [
        { metric: 'bid', scope: 'quote.tracked', op: '>', value: 10, scope_filter: { symbol: 'NIFTY25JULFUT' } },
        { metric: 'bid', scope: 'quote.tracked', op: '<', value: 200, scope_filter: { symbol: 'NIFTY25JULFUT' } },
      ],
    });
  });

  it('a non-quote scope is completely unaffected by the new guard (regression)', () => {
    const r = compile('WHEN pnl@positions.total > 0 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
  });

  it('quote.tracked.account:X is rejected by the existing column whitelist, not the new guard — no account dimension on quote.* (advisor-confirmed "should work as-is")', () => {
    const r = compile('WHEN pnl@quote.tracked.account:ZG0790 > 0 ALERT telegram DO nop');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /not a valid qualifier for scope 'quote\.tracked'/.test(e.message))).toBe(true);
  });
});

describe('Sprint 21 — quote.tracked.symbol:NIFTY25JULFUT.ltp decompile round-trip (dot-access-with-qualifier)', () => {
  it('compiles and decompiles to the "@" form (dot-access-with-qualifier has no dedicated decompile spelling; ltp has a real metric token so the generic decompiler prefers it) and recompiles identically', () => {
    const r = compile('WHEN quote.tracked.symbol:NIFTY25JULFUT.ltp <= 100 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    const dec = decompileCondition(r.agent.conditions, CATALOG);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toBe('ltp@quote.tracked.symbol:NIFTY25JULFUT <= 100');
    const recompiled = compile(`WHEN ${dec.text} ALERT telegram DO nop`);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(r.agent.conditions);
  });

  it('quote.tracked.symbol:NIFTY25JULFUT.bid decompile round-trip (a column with NO metric token — the bare-column-plus-scope_filter decompile path)', () => {
    const r = compile('WHEN quote.tracked.symbol:NIFTY25JULFUT.bid <= 100 ALERT telegram DO nop');
    expect(r.ok).toBe(true);
    const dec = decompileCondition(r.agent.conditions, CATALOG);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toBe('quote.tracked.symbol:NIFTY25JULFUT.bid <= 100');
    const recompiled = compile(`WHEN ${dec.text} ALERT telegram DO nop`);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(r.agent.conditions);
  });
});
