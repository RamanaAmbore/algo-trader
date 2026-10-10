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
  // channels
  { grammar_kind: 'notify', token_kind: 'channel', token: 'telegram', params_schema: null },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'email', params_schema: null },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'log', params_schema: null },
  // fixture-only `ntfy` (used in the design doc's own canonical example 2,
  // but absent from the live catalog today — see "Known gaps")
  { grammar_kind: 'notify', token_kind: 'channel', token: 'ntfy', params_schema: { priority: { type: 'enum', enum: ['low', 'default', 'urgent'], required: false } } },
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
      price: { type: 'number', required: false, token_ref_ok: true },
      trigger_price: { type: 'number', required: false, token_ref_ok: true },
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
  },
  {
    grammar_kind: 'action', token_kind: 'action_type', token: 'emit_log',
    params_schema: {
      level: { type: 'enum', enum: ['info', 'warning', 'error'], required: false, default: 'info' },
      message: { type: 'string', required: true, token_ref_ok: true },
    },
  },
  { grammar_kind: 'action', token_kind: 'action_type', token: 'deactivate_agent', params_schema: {} },
];

const CATALOG = buildCatalog(FIXTURE_ROWS);

// Fixture lot sizes — NIFTY/BANKNIFTY-ish, so lots→qty tests are readable.
const LOT_SIZES = { NIFTY25JULFUT: 75, BANKNIFTY25JULFUT: 25 };
const lotSizeOf = (sym) => LOT_SIZES[sym] || null;

function compile(text, opts = {}) {
  return compileAgentCliStatement(text, CATALOG, { lotSizeOf, ...opts });
}

// ── buildCatalog() / fetchAgentCatalog() ────────────────────────────────

describe('buildCatalog()', () => {
  it('buckets rows by grammar_kind/token_kind and preserves declared param order', () => {
    expect(CATALOG.metrics.has('mean_pnl')).toBe(true);
    expect(CATALOG.actions.get('place_order').paramKeys[0]).toBe('account');
  });

  it('skips inactive rows', () => {
    const cat = buildCatalog([{ grammar_kind: 'notify', token_kind: 'channel', token: 'x', is_active: false }]);
    expect(cat.channels.has('x')).toBe(false);
  });
});

describe('fetchAgentCatalog()', () => {
  it('fetches all three grammar kinds and merges into one catalog', async () => {
    vi.mocked(fetchGrammarTokens).mockImplementation(async (kind) => {
      if (kind === 'condition') return [FIXTURE_ROWS[0]];
      if (kind === 'notify') return [FIXTURE_ROWS[13]];
      return [FIXTURE_ROWS[17]];
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
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)', { mode: 'paper' });
    expect(r.errors).toEqual([]);
    expect(r.kind).toBe('order');
    expect(r.basket).toBeNull();
    expect(r.ticket).toEqual({
      mode: 'paper', account: 'ZG0790', side: 'SELL', tradingsymbol: 'NIFTY25JULFUT', quantity: 1,
      exchange: 'NFO', product: 'NRML', order_type: 'LIMIT', variety: 'regular',
      price: null, trigger_price: null, chase: false, chase_aggressiveness: 'low',
      template_id: null, tp_pct_override: null, sl_pct_override: null,
      wing_premium_pct_override: null, wing_strike_offset_override: null,
      lot_size_hint: 75,
    });
  });

  it('8. bare multiple orders — basket shape, grouped by account', () => {
    const r = compile(
      'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1), ' +
      'order(account="ZJ6294", symbol="BANKNIFTY25JULFUT", side="BUY", lots=1)'
    );
    expect(r.errors).toEqual([]);
    expect(r.ticket).toBeNull();
    expect(r.basket).toEqual({
      groups: [
        { account: 'ZG0790', legs: [{
          transaction_type: 'SELL', tradingsymbol: 'NIFTY25JULFUT', quantity: 1,
          exchange: 'NFO', product: 'NRML', order_type: 'LIMIT', variety: 'regular',
          price: null, trigger_price: null, chase: false, chase_aggressiveness: 'low',
          template_id: null, tp_pct_override: null, sl_pct_override: null,
          wing_premium_pct_override: null, wing_strike_offset_override: null, lot_size_hint: 75,
        }] },
        { account: 'ZJ6294', legs: [{
          transaction_type: 'BUY', tradingsymbol: 'BANKNIFTY25JULFUT', quantity: 1,
          exchange: 'NFO', product: 'NRML', order_type: 'LIMIT', variety: 'regular',
          price: null, trigger_price: null, chase: false, chase_aggressiveness: 'low',
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
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)', { mode: 'paper' });
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
      const r = compile(`order(account="ZG0790", symbol="NIFTY25JULFUT", side=${side}, lots=1)`, { mode: 'paper' });
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
    const r = compile('order(account="zg0790", symbol="NIFTY25JULFUT", side=SELL, lots=1)', { mode: 'paper' });
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
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)', { mode: 'paper' });
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
  it('qty alone (contracts, no lot_size_hint) works for the ticket path', () => {
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", qty=75)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.quantity).toBe(75);
    expect(r.ticket.lot_size_hint).toBeUndefined();
  });
  it('unresolvable lot size is a structured error, never silently defaults to 1', () => {
    const r = compile('order(account="ZG0790", symbol="UNKNOWN_SYM", side="SELL", lots=1)', { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /could not resolve lot size/.test(e.message))).toBe(true);
  });
  it('DO order(...) lots converts to CONTRACTS (lots × lot_size), NOT the ticket "lots-only" convention', () => {
    const r = compile('WHEN always ALERT nop DO order(account="ZG0790", symbol="BANKNIFTY25JULFUT", side="BUY", lots=3)');
    expect(r.ok).toBe(true);
    expect(r.agent.actions[0].params.qty).toBe(75); // 3 * 25
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
    const r = compile('order(account="ZG0790", symbol = "NIFTY25JULFUT", side="SELL", lots=1)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.tradingsymbol).toBe('NIFTY25JULFUT');
  });
  it('"key =value" (space before =, none after) also works', () => {
    const r = compile('order(account="ZG0790", symbol ="NIFTY25JULFUT", side="SELL", lots=1)', { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(r.ticket.tradingsymbol).toBe('NIFTY25JULFUT');
  });
  it('"key= value" (space after =, none before) also works', () => {
    const r = compile('order(account="ZG0790", symbol= "NIFTY25JULFUT", side="SELL", lots=1)', { mode: 'paper' });
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
    const r = compile('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, chase=HIGH)', { mode: 'paper' });
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
    expect(tokens[0].args).toEqual([{ key: 'minutes', value: { type: 'number', value: 30 } }]);
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
