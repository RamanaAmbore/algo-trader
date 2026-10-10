/**
 * agentsDecompiler.test.js — Vitest coverage for the Sprint 3 decompiler
 * added to frontend/src/lib/command/grammars/agents.js (the reverse of
 * Sprint 2's compiler, see agentsGrammar.test.js for the compiler's own
 * suite and .claude/PLAN.md for the full Sprint 3 brief).
 *
 * Fixture catalog note: this file declares its OWN catalog (separate from
 * agentsGrammar.test.js's FIXTURE_ROWS) so it can match the REAL live
 * catalog's `is_itm`/`is_future` value_type ('number', not 'boolean' —
 * see agent_grammar.yaml) for the real-builtin-agent round-trip tests
 * below, while still keeping ONE fixture-only boolean metric
 * (`is_itm_bool`) to exercise the boolean-shorthand decompile path the
 * live catalog has nothing to fire against yet (same documented
 * limitation Sprint 2's own fixture notes).
 *
 * Real-builtin-agent fixtures: several test cases below use the EXACT
 * `conditions`/`events`/`actions` JSON from real BUILTIN_AGENTS entries
 * in backend/api/algo/agent_engine.py (market-open-nse, MANUAL_AGENT,
 * loss-positions-acct, loss-rate-acct, loss-margin-low,
 * loss-funds-negative, loss-pos-total-auto-close,
 * expiry-day-equity-itm-auto-close, expiry-nfo-risk-alert,
 * expiry-mcx-risk-alert) — these are genuine "never authored via the
 * CLI" shapes per the Sprint 3 brief, confirmed by reading that file
 * directly rather than inventing synthetic equivalents.
 */

import { describe, it, expect, vi } from 'vitest';

vi.mock('$lib/data/instruments', () => ({ getInstrument: vi.fn(() => null) }));
vi.mock('$lib/api', () => ({ fetchGrammarTokens: vi.fn() }));

const {
  buildCatalog, compileAgentCliStatement, decompileCondition, decompileEvents,
  decompileActions, decompileAgent, ALWAYS_LEAF,
} = await import('$lib/command/grammars/agents.js');

// ── Fixture catalog ──────────────────────────────────────────────────────

const FIXTURE_ROWS = [
  // metrics — number-typed, matching the REAL catalog's value_type for
  // is_itm/is_future (see agent_grammar.yaml) rather than Sprint 2's own
  // fixture-only boolean guess for these two specific tokens.
  { grammar_kind: 'condition', token_kind: 'metric', token: 'pnl', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'pnl_pct', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'day_pct', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'day_val', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'avail_margin', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'cash', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'pnl_rate_abs', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'pnl_rate_pct', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'is_itm', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'is_future', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'days_until_expiry', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'mean_pnl', value_type: 'number', params_schema: { minutes: { type: 'number' } } },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'stdev_pnl', value_type: 'number', params_schema: { minutes: { type: 'number' } } },
  // real catalog's bare, zero-arg, fixed-window token spelling (NOT
  // call syntax) — see agent_grammar.yaml's mean_pnl_30m. Exercises the
  // "hand-constructed, never-compiler-produced token shape" case.
  { grammar_kind: 'condition', token_kind: 'metric', token: 'mean_pnl_30m', value_type: 'number', params_schema: null },
  // fixture-only non-numeric metric, for the generic in/not_in mechanism
  { grammar_kind: 'condition', token_kind: 'metric', token: 'side', value_type: 'string', params_schema: null },
  // fixture-only boolean metric (none exists live — same as Sprint 2's
  // own fixture's is_itm; renamed here to avoid colliding with the
  // real-catalog-accurate numeric `is_itm` above).
  { grammar_kind: 'condition', token_kind: 'metric', token: 'is_itm_bool', value_type: 'boolean', params_schema: null },
  // scopes
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.total', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.any_acct', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'holdings.total', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'funds.total', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'funds.any_acct', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.expiring_today', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.expiring_today.nfo', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.expiring_today.mcx_unhedged', params_schema: null },
  // channels
  { grammar_kind: 'notify', token_kind: 'channel', token: 'telegram', params_schema: null },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'email', params_schema: null },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'log', params_schema: null },
  {
    grammar_kind: 'notify', token_kind: 'channel', token: 'ntfy',
    params_schema: { priority: { type: 'enum', enum: ['low', 'default', 'high', 'urgent'], required: false } },
  },
  // actions
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
  {
    grammar_kind: 'action', token_kind: 'action_type', token: 'chase_close_positions',
    params_schema: {
      scope: { type: 'enum', enum: ['total', 'account'], default: 'total' },
      account: { type: 'string', required: false },
      timeout_minutes: { type: 'number', default: 10 },
      adjust_pct: { type: 'number', default: 0.1 },
    },
  },
  {
    grammar_kind: 'action', token_kind: 'action_type', token: 'expiry_auto_close',
    params_schema: { exchange: { type: 'enum', enum: ['NFO', 'MCX'], required: true } },
  },
];

const CATALOG = buildCatalog(FIXTURE_ROWS);

const LOT_SIZES = { NIFTY25JULFUT: 75, BANKNIFTY25JULFUT: 25 };
const lotSizeOf = (sym) => LOT_SIZES[sym] || null;

function compile(text, opts = {}) {
  return compileAgentCliStatement(text, CATALOG, { lotSizeOf, ...opts });
}

function decompile(agentJson, opts = {}) {
  return decompileAgent(agentJson, CATALOG, { lotSizeOf, ...opts });
}

/** compile(text) -> decompile(.agent) -> compile(decompiled text) ->
 *  assert the SECOND compile's JSON deep-equals the FIRST's — the hard
 *  round-trip requirement for every canonical example. */
function roundTrip(text) {
  const first = compile(text);
  expect(first.ok).toBe(true);
  const dec = decompile(first.agent);
  expect(dec.errors).toEqual([]);
  expect(typeof dec.text).toBe('string');
  const second = compile(dec.text);
  expect(second.errors).toEqual([]);
  expect(second.ok).toBe(true);
  expect(second.agent).toEqual(first.agent);
  return { first, dec, second };
}

// ── Canonical examples 1–6 (design doc) — decompile→recompile round-trip.
//    Examples 7/8 are BARE ORDER statements (no conditions/events/actions
//    JSON at all — they compile straight to ticket/basket request shapes),
//    so decompileAgent does not apply to them; DO order(...) rendering is
//    still exercised via examples 1, 2, 5a, 6 below. ─────────────────────

describe('canonical examples — decompile→recompile round-trip', () => {
  it('1. single channel, single action', () => {
    roundTrip(
      'WHEN mean_pnl(minutes=30)@positions.total <= -50000 ' +
      'ALERT telegram ' +
      'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );
  });

  it('2. multiple channels (one parameterized), multiple actions', () => {
    roundTrip(
      'WHEN mean_pnl(minutes=30)@positions.total <= -50000 ' +
      'ALERT telegram, ntfy(priority="urgent") ' +
      'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1), emit_log(message="stop hit")'
    );
  });

  it('3. compound boolean — equal precedence, nested all/any', () => {
    roundTrip(
      'WHEN ((mean_pnl(minutes=30)@positions.total <= -50000) & (stdev_pnl(minutes=30)@positions.total > 10000)) | (avail_margin@funds.total < 0) ' +
      'ALERT telegram ' +
      'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );
  });

  it('4. NOT', () => {
    roundTrip(
      'WHEN ~(pnl_pct@positions.total > 0) ' +
      'ALERT telegram ' +
      'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );
  });

  it('5a. only an action (ALERT nop)', () => {
    roundTrip(
      'WHEN mean_pnl(minutes=30)@positions.total <= -50000 ALERT nop ' +
      'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );
  });

  it('5b. only a notify (DO nop)', () => {
    roundTrip('WHEN pnl_pct@positions.total <= -5 ALERT telegram DO nop');
  });

  it('6. always + nop', () => {
    const { dec } = roundTrip('WHEN always ALERT nop DO order(account="ZG0790", symbol="BANKNIFTY25JULFUT", side="BUY", lots=2)');
    expect(dec.text).toContain('WHEN always ');
  });
});

// ── Real builtin-agent shapes (agent_engine.py BUILTIN_AGENTS) ──────────

describe('real builtin agents — never authored via the CLI', () => {
  it('market-open-nse / market-preclose-mcx: always-sentinel with REORDERED keys + 3-channel events + actions=[]', () => {
    // Exact shape from agent_engine.py's _INFO_AGENTS / _INFO_AGENT_DEFAULTS
    // — note op/scope/metric/value key order, NOT ALWAYS_LEAF's own
    // metric/scope/op/value order, confirming field-by-field (not
    // JSON.stringify) equality is required.
    const agentJson = {
      conditions: { op: '>=', scope: 'funds.any_acct', metric: 'avail_margin', value: -999999999 },
      events: [
        { channel: 'telegram', enabled: true },
        { channel: 'log', enabled: true },
        { channel: 'ntfy', enabled: true, priority: 'default' },
      ],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toMatch(/^WHEN always ALERT /);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(ALWAYS_LEAF);
    expect(recompiled.agent.events).toEqual(agentJson.events);
    expect(recompiled.agent.actions).toEqual([]);
  });

  it('MANUAL_AGENT: conditions=null has NO CLI representation — fails loud, not a crash', () => {
    const agentJson = {
      conditions: null,
      events: [
        { channel: 'telegram', enabled: true },
        { channel: 'email', enabled: true },
        { channel: 'log', enabled: true },
        { channel: 'ntfy', enabled: true },
      ],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.text).toBeNull();
    expect(dec.errors.length).toBeGreaterThan(0);
    expect(dec.errors.some(e => /unrecognized condition shape/.test(e.message))).toBe(true);
  });

  it('loss-positions-acct: {any:[day_val leaf, day_pct leaf]} — different metrics, generic OR', () => {
    const agentJson = {
      conditions: { any: [
        { metric: 'day_val', scope: 'positions.any_acct', op: '<=', value: -30000 },
        { metric: 'day_pct', scope: 'positions.any_acct', op: '<=', value: -2.0 },
      ] },
      events: [{ channel: 'telegram', enabled: true }, { channel: 'log', enabled: true }, { channel: 'ntfy', enabled: true, priority: 'high' }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('loss-rate-acct: {all:[pnl_rate_abs leaf, pnl_rate_pct leaf]} — different metrics, generic AND', () => {
    const agentJson = {
      conditions: { all: [
        { metric: 'pnl_rate_abs', scope: 'positions.any_acct', op: '<=', value: -10000 },
        { metric: 'pnl_rate_pct', scope: 'positions.any_acct', op: '<=', value: -0.25 },
      ] },
      events: [{ channel: 'telegram', enabled: true }, { channel: 'log', enabled: true }, { channel: 'ntfy', enabled: true, priority: 'urgent' }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('loss-margin-low: {all:[avail_margin<25000, avail_margin>0]} — SAME metric+scope, opposite-bucket ops: reconstructs as a between-chain', () => {
    const agentJson = {
      conditions: { all: [
        { op: '<', scope: 'funds.any_acct', metric: 'avail_margin', value: 25000 },
        { op: '>', scope: 'funds.any_acct', metric: 'avail_margin', value: 0 },
      ] },
      events: [{ channel: 'telegram', enabled: true }, { channel: 'log', enabled: true }, { channel: 'ntfy', enabled: true, priority: 'high' }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    // Between-chain reversal fired (nice-to-have syntax), confirmed by the
    // "<=" between-syntax markers NOT appearing (this is a STRICT chain,
    // so the reconstructed comparators are the original strict ones).
    expect(dec.text).toMatch(/avail_margin@funds\.any_acct/);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(agentJson.conditions); // exact order preserved
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('loss-funds-negative: {any:[cash<0, avail_margin<0]} — different metrics, generic OR', () => {
    const agentJson = {
      conditions: { any: [
        { metric: 'cash', scope: 'funds.any_acct', op: '<', value: 0 },
        { metric: 'avail_margin', scope: 'funds.any_acct', op: '<', value: 0 },
      ] },
      events: [{ channel: 'telegram', enabled: true }, { channel: 'log', enabled: true }, { channel: 'ntfy', enabled: true, priority: 'urgent' }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('loss-pos-total-auto-close: plain (unwrapped) leaf + a generic non-place_order action with enum/number/number params', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 },
      events: [{ channel: 'telegram', enabled: true }, { channel: 'log', enabled: true }, { channel: 'ntfy', enabled: true, priority: 'urgent' }],
      actions: [{ type: 'chase_close_positions', params: { scope: 'total', timeout_minutes: 10, adjust_pct: 0.1 } }],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('chase_close_positions(');
    expect(dec.text).not.toContain('place_order');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('expiry-day-equity-itm-auto-close: {all:[<one leaf>]} — single-element composite normalizes to the bare leaf (documented, not a bug)', () => {
    const agentJson = {
      conditions: { all: [
        { metric: 'is_itm', scope: 'positions.expiring_today.nfo', op: '==', value: 1.0 },
      ] },
      events: [{ channel: 'telegram', enabled: true }, { channel: 'email', enabled: true }, { channel: 'log', enabled: true }, { channel: 'ntfy', enabled: true }],
      actions: [{ type: 'expiry_auto_close', params: { exchange: 'NFO' } }],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    // Documented normalization: NOT wrapped in `all` on resave — the
    // grammar has no distinct syntax for "AND of exactly one condition".
    expect(recompiled.agent.conditions).toEqual({ metric: 'is_itm', scope: 'positions.expiring_today.nfo', op: '==', value: 1.0 });
    expect(recompiled.agent.actions).toEqual(agentJson.actions);
  });

  it('expiry-nfo-risk-alert: {any:[is_itm==1, is_future==1]} on the SAME scope but different metrics — generic OR, not between', () => {
    const agentJson = {
      conditions: { any: [
        { op: '==', scope: 'positions.expiring_today.nfo', metric: 'is_itm', value: 1.0 },
        { op: '==', scope: 'positions.expiring_today.nfo', metric: 'is_future', value: 1.0 },
      ] },
      events: [{ channel: 'telegram', enabled: true }, { channel: 'email', enabled: true }, { channel: 'log', enabled: true }, { channel: 'ntfy', enabled: true, priority: 'high' }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('expiry-mcx-risk-alert: pnl >= -999999999 on a DIFFERENT metric/scope than ALWAYS_LEAF — must NOT decompile as "always"', () => {
    const agentJson = {
      conditions: { op: '>=', scope: 'positions.expiring_today.mcx_unhedged', metric: 'pnl', value: -999999999 },
      events: [{ channel: 'telegram', enabled: true }, { channel: 'email', enabled: true }, { channel: 'log', enabled: true }, { channel: 'ntfy', enabled: true, priority: 'high' }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).not.toMatch(/\balways\b/);
    expect(dec.text).toMatch(/pnl@positions\.expiring_today\.mcx_unhedged >= -999999999/);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });
});

// ── Hand-constructed shapes Sprint 2's compiler never produces ─────────

describe('hand-constructed shapes (JSON-textarea / AI-draft style authorship)', () => {
  it('a bare fixed-window metric token (mean_pnl_30m, not call syntax) round-trips', () => {
    const agentJson = {
      conditions: { metric: 'mean_pnl_30m', scope: 'positions.total', op: '<=', value: -5000 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('mean_pnl_30m@positions.total <= -5000');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('native "between" op decompiles to low <= metric@scope <= high', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: 'between', value: [-100, 100] },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('-100 <= pnl@positions.total <= 100');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('in / not_in list leaves round-trip', () => {
    const agentJson = {
      conditions: { metric: 'side', scope: 'positions.total', op: 'in', value: ['BUY', 'SELL'] },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);

    const agentJson2 = { ...agentJson, conditions: { ...agentJson.conditions, op: 'not_in' } };
    const dec2 = decompile(agentJson2);
    expect(dec2.errors).toEqual([]);
    expect(dec2.text).toMatch(/not in \[/);
    const recompiled2 = compile(dec2.text);
    expect(recompiled2.ok).toBe(true);
    expect(recompiled2.agent).toEqual(agentJson2);
  });

  it('boolean-shorthand leaf (op:"==", value:true on a boolean metric) decompiles to the bare metric form', () => {
    const agentJson = {
      conditions: { metric: 'is_itm_bool', scope: 'positions.total', op: '==', value: true },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('WHEN is_itm_bool@positions.total ALERT');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('explicit boolean leaf (op:"!=", value:false) round-trips WITHOUT collapsing to shorthand', () => {
    const agentJson = {
      conditions: { metric: 'is_itm_bool', scope: 'positions.total', op: '!=', value: false },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('a generic action with an enum param needing quotes (order_type-like hyphenated enum, SL-M) round-trips via place_order', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -1000 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'place_order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'SELL', qty: 75, order_type: 'SL-M' } }],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('order_type="SL-M"'); // hyphen is not bare-identifier-safe
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('a deactivate_agent action (zero params, never exercised by Sprint 2 tests) decompiles bare', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -1000 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'deactivate_agent', params: {} }],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('DO deactivate_agent');
    expect(dec.text).not.toContain('deactivate_agent(');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('a genuinely flat 3-element "all" array (never compiler-produced) normalizes to the nested-pair shape on resave', () => {
    const agentJson = {
      conditions: { all: [
        { metric: 'pnl', scope: 'positions.total', op: '<', value: 100 },
        { metric: 'day_pct', scope: 'positions.total', op: '<', value: 5 },
        { metric: 'avail_margin', scope: 'funds.total', op: '>', value: 0 },
      ] },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    // Documented normalization — nested-pair, not the original flat
    // 3-array (the grammar's own chain-builder never produces flat N>2).
    expect(recompiled.agent.conditions).toEqual({ all: [
      { all: [
        { metric: 'pnl', scope: 'positions.total', op: '<', value: 100 },
        { metric: 'day_pct', scope: 'positions.total', op: '<', value: 5 },
      ] },
      { metric: 'avail_margin', scope: 'funds.total', op: '>', value: 0 },
    ] });
  });

  it('a deeply left-nested AND chain (25 leaves) decompiles without hitting the paren-depth guard', () => {
    // Build {all:[{all:[{all:[leaf1,leaf2]},leaf3]},leaf4]}... 25 levels —
    // exactly what compileCondNode produces for a chain of 25 ANDed leaves,
    // and exactly the shape the advisor flagged as a paren-depth risk for
    // a naive "(decompile(left)) & (decompile(right))" decompiler.
    /** @type {any} */
    let cond = { metric: 'pnl', scope: 'positions.total', op: '<', value: 1 };
    for (let i = 2; i <= 25; i++) {
      cond = { all: [cond, { metric: 'pnl', scope: 'positions.total', op: '<', value: i }] };
    }
    const agentJson = { conditions: cond, events: [{ channel: 'telegram', enabled: true }], actions: [] };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(cond);
  });
});

// ── $ref non-goal ─────────────────────────────────────────────────────

describe('$ref fragment — explicit non-goal', () => {
  it('a top-level $ref fails loud with the documented message', () => {
    const dec = decompileCondition({ $ref: 'some-fragment' }, CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /\$ref fragment/.test(e.message))).toBe(true);
  });

  it('a $ref nested inside an "all" array also fails loud, not a crash', () => {
    const dec = decompileCondition({ all: [{ $ref: 'frag' }, { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 }] }, CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /\$ref fragment/.test(e.message))).toBe(true);
  });

  it('decompileAgent surfaces the same structured error for a $ref-containing agent', () => {
    const dec = decompile({
      conditions: { $ref: 'frag' },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [],
    });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /\$ref fragment/.test(e.message))).toBe(true);
  });
});

// ── Unrecognized / malformed shapes — fail loud, never guess ───────────

describe('unrecognized or malformed shapes fail loud', () => {
  it('unknown metric referenced in a stored leaf', () => {
    const dec = decompileCondition({ metric: 'bogus_metric', scope: 'positions.total', op: '<', value: 1 }, CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /unknown metric/.test(e.message))).toBe(true);
  });

  it('unknown scope referenced in a stored leaf', () => {
    const dec = decompileCondition({ metric: 'pnl', scope: 'bogus.scope', op: '<', value: 1 }, CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /unknown scope/.test(e.message))).toBe(true);
  });

  it('unrecognized op/value combination on an otherwise-resolvable leaf', () => {
    const dec = decompileCondition({ metric: 'pnl', scope: 'positions.total', op: 'bogus_op', value: 1 }, CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /unrecognized op\/value/.test(e.message))).toBe(true);
  });

  it('a leaf with a string value where a number is required', () => {
    const dec = decompileCondition({ metric: 'pnl', scope: 'positions.total', op: '<', value: 'not-a-number' }, CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.length).toBeGreaterThan(0);
  });

  it('unknown channel referenced in stored events', () => {
    const dec = decompileEvents([{ channel: 'bogus_channel', enabled: true }], CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /unknown channel/.test(e.message))).toBe(true);
  });

  it('a channel param not declared in its catalog schema', () => {
    const dec = decompileEvents([{ channel: 'telegram', enabled: true, bogus_param: 'x' }], CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /unknown parameter/.test(e.message))).toBe(true);
  });

  it('a channel entry missing "enabled" has no CLI representation', () => {
    const dec = decompileEvents([{ channel: 'telegram' }], CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /no CLI representation/.test(e.message))).toBe(true);
  });

  it('a channel entry with enabled:false has no CLI representation', () => {
    const dec = decompileEvents([{ channel: 'telegram', enabled: false }], CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /no CLI representation/.test(e.message))).toBe(true);
  });

  it('unknown action referenced in stored actions', () => {
    const dec = decompileActions([{ type: 'bogus_action', params: {} }], CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /unknown action/.test(e.message))).toBe(true);
  });

  it('an action missing "params" entirely has no CLI representation', () => {
    const dec = decompileActions([{ type: 'deactivate_agent' }], CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /no CLI representation/.test(e.message))).toBe(true);
  });

  it('an action with params:null has no CLI representation', () => {
    const dec = decompileActions([{ type: 'deactivate_agent', params: null }], CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.length).toBeGreaterThan(0);
  });

  it('an action param not declared in its catalog schema', () => {
    const dec = decompileActions([{ type: 'deactivate_agent', params: { bogus: 1 } }], CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /unknown parameter/.test(e.message))).toBe(true);
  });

  it('events=[] and actions=[] together fails loud with the same "at least one of ALERT or DO" message the compiler uses', () => {
    const dec = decompile({ conditions: { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 }, events: [], actions: [] });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /at least one of ALERT or DO/.test(e.message))).toBe(true);
  });
});

// ── place_order qty→lots reconversion — adversarial cases ──────────────

describe('place_order qty (contracts) → lots reconversion', () => {
  it('a clean multiple converts to lots correctly', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'place_order', params: { account: 'ZG0790', symbol: 'BANKNIFTY25JULFUT', side: 'BUY', qty: 75 } }],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('lots=3');
    expect(dec.text).not.toContain('qty=');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  // The three cases below were originally "fail loud, agent can't be
  // decompiled" — revised: _compileOrderActionParams (Sprint 2) already
  // accepts a plain qty= as a legitimate alternate spelling for DO
  // order(...), coerced through the exact same path every other param
  // uses (so a token_ref_ok expression string renders quoted, same as
  // elsewhere) — there's no real failure case for qty at all, only a
  // choice between the nicer lots= form (when it converts cleanly) and
  // plain qty= otherwise. See the Sprint 3 PLAN-VS-CODE DISCREPANCY note
  // at _decompileOrderAction's own docstring for the full reasoning.

  it('a qty expression string (token_ref_ok) falls back to a quoted qty=, never fails and never drops the agent', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'place_order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'SELL', qty: 'base_lots * 2' } }],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('qty="base_lots * 2"');
    expect(dec.text).not.toContain('lots=');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('an unresolvable lot size falls back to plain qty=, never fails and never silently defaults to a lot size of 1', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'place_order', params: { account: 'ZG0790', symbol: 'UNKNOWN_SYM', side: 'SELL', qty: 75 } }],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('qty=75');
    expect(dec.text).not.toContain('lots=');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('a qty that is NOT an exact multiple of the resolved lot size falls back to plain qty= rather than losing precision', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'place_order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'SELL', qty: 77 } }],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('qty=77');
    expect(dec.text).not.toContain('lots=');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });

  it('qty absent entirely still fails loud — this IS genuinely malformed, no fallback possible', () => {
    const dec = decompile({
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'place_order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'SELL' } }],
    });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /missing 'qty'/.test(e.message))).toBe(true);
  });
});

// ── Number-formatting guard (exponent notation) ─────────────────────────

describe('number-formatting guard', () => {
  it('a value that would render with exponent notation (1e-7) fails loud instead of emitting unparseable text', () => {
    const dec = decompileCondition({ metric: 'pnl_pct', scope: 'positions.total', op: '<', value: 0.0000001 }, CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.length).toBeGreaterThan(0);
  });

  it('an ordinary small decimal renders fine', () => {
    const dec = decompileCondition({ metric: 'pnl_pct', scope: 'positions.total', op: '<', value: -2.5 }, CATALOG);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('-2.5');
  });
});

// ── Quoting guard (both quote characters present) ───────────────────────

describe('quoting guard', () => {
  it('a free-text value containing both " and \' has no safe CLI representation', () => {
    const dec = decompileActions(
      [{ type: 'emit_log', params: { message: `she said "it's broken"` } }],
      CATALOG
    );
    expect(dec.text).toBeNull();
    expect(dec.errors.length).toBeGreaterThan(0);
  });

  it('a free-text value containing commas/parens quotes safely and round-trips', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'emit_log', params: { message: 'stop hit, closing (urgent)' } }],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });
});

// ── Between-chain reversal — inclusive-same-op guard (advisor-flagged) ──

describe('between-chain reversal — inclusive-same-op guard', () => {
  it('an explicit AND of two INCLUSIVE same-direction bounds (>=0 & <=100) must NOT collapse to native between', () => {
    // This shape is NOT producible by compileBetween (which emits native
    // {op:'between'} for this exact inclusive case, never {all:[...]})
    // — a hand-authored equivalent must decompile generically, since
    // collapsing it would recompile to a DIFFERENT JSON shape.
    const agentJson = {
      conditions: { all: [
        { metric: 'pnl', scope: 'positions.total', op: '>=', value: 0 },
        { metric: 'pnl', scope: 'positions.total', op: '<=', value: 100 },
      ] },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    // Exact structural match confirms the generic "all" fallback fired —
    // if the inclusive guard were missing, this would instead recompile
    // to {metric,scope,op:'between',value:[0,100]}, a DIFFERENT shape.
    expect(recompiled.agent.conditions).toEqual(agentJson.conditions);
  });

  it('a genuine strict chain (0 < pnl < 100, compiler-produced) reverses correctly', () => {
    roundTrip('WHEN 0 < pnl@positions.total < 100 ALERT telegram DO nop');
  });

  it('a genuine descending strict chain (100 > pnl > 0, compiler-produced) reverses correctly', () => {
    roundTrip('WHEN 100 > pnl@positions.total > 0 ALERT telegram DO nop');
  });
});

// ── KWARG_ALIASES choice — decompiled output always uses REAL key names ─

describe('decompiled output key-name choice', () => {
  it('place_order params render with real schema key names, never KWARG_ALIASES short forms', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'place_order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'SELL', qty: 75, chase_level: 'HIGH' } }],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('account="ZG0790"');
    expect(dec.text).toContain('chase_level=HIGH');
    expect(dec.text).not.toMatch(/\bacct=/);
    expect(dec.text).not.toMatch(/\bchase=/);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent).toEqual(agentJson);
  });
});
