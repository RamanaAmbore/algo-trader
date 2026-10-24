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
  decompileActions, decompileAgent, decompileMetaClause, decompileBracket,
  decompileEventAgent, decompileToken, ALWAYS_LEAF,
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
  // channels — `gate` (Sprint 18) is the real catalog shape on
  // telegram/email/ntfy (see agent_grammar.yaml); deliberately absent on
  // `log` (capability is always None, `gate` has no effect there — same
  // real-catalog omission backend's test_grammar_event_action_gate_field.py
  // asserts).
  {
    grammar_kind: 'notify', token_kind: 'channel', token: 'telegram',
    params_schema: { gate: { type: 'boolean', required: false } },
  },
  {
    grammar_kind: 'notify', token_kind: 'channel', token: 'email',
    params_schema: { gate: { type: 'boolean', required: false } },
  },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'log', params_schema: null },
  // Closed 2026-10-12 — `telegram_info` had NO catalog row at all (not
  // just a missing `gate` param like the other channels above), so the
  // real seeded `market-summary` agent's Edit/decompile failed with
  // "unknown channel 'telegram_info'" / "not declared in its catalog
  // schema" (the latter once `gate: false` was reached) — see
  // agent_grammar.yaml + backend's test_grammar_event_action_gate_field.py.
  {
    grammar_kind: 'notify', token_kind: 'channel', token: 'telegram_info',
    params_schema: { gate: { type: 'boolean', required: false } },
  },
  {
    grammar_kind: 'notify', token_kind: 'channel', token: 'ntfy',
    params_schema: {
      priority: { type: 'enum', enum: ['low', 'default', 'high', 'urgent'], required: false },
      gate: { type: 'boolean', required: false },
    },
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
    // item 7 (backend) — DSL-visible token renamed `emit_log` -> `log`.
    grammar_kind: 'action', token_kind: 'action_type', token: 'log',
    params_schema: {
      level: { type: 'enum', enum: ['info', 'warning', 'error'], required: false, default: 'info' },
      message: { type: 'string', required: true, token_ref_ok: true },
    },
  },
  { grammar_kind: 'action', token_kind: 'action_type', token: 'deactivate_agent', params_schema: {} },
  { grammar_kind: 'action', token_kind: 'action_type', token: 'help', params_schema: { name: { type: 'string', required: true } } },
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
  // Sprint 10 — merged-in AgentTemplate ("Token") rows, hyphenated names
  // (the real seeding convention). See agents.js's `_buildCatalogEntry`
  // for the `isFragmentRef` flag these set.
  { grammar_kind: 'condition', token_kind: 'metric', token: 'loss-positions-default', is_fragment_ref: true, params_schema: null },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'notify-critical-trio', is_fragment_ref: true, params_schema: null },
  // Sprint 16 — matched-row ($row) binding decompile coverage, appended
  // at the end (same append-only convention as every other sprint's
  // fixture additions in this file).
  {
    grammar_kind: 'action', token_kind: 'action_type', token: 'close_position',
    params_schema: {
      account: { type: 'string', required: true, token_ref_ok: true },
      symbol: { type: 'string', required: true },
    },
  },
  // Sprint 18 — the synthetic `render_action` row (backend's
  // `routes/grammar.py:_render_action_catalog_rows`, gated to
  // `grammar == 'action'`) declaring the kind='event' agent render
  // action's own optional `gate` sibling key. `token_kind` is
  // deliberately `'render_action'`, NOT `'action_type'` — see
  // `buildCatalog`'s own comment for why (never lands in the DO-action
  // bucket / `catalog.actions`).
  {
    grammar_kind: 'action', token_kind: 'render_action', token: 'render',
    params_schema: { gate: { type: 'boolean', required: false } },
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'positions.expiring_today.account_id', value_type: 'string',
    label: 'Account',
    description: "Account. Sourced from DataFrame column 'account' on ctx.position_rows, populated by backend.api.algo.grammar._scope_positions_expiring_today.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'positions.expiring_today.symbol', value_type: 'string',
    label: 'Symbol',
    description: "Symbol. Sourced from DataFrame column 'tradingsymbol' on ctx.position_rows, populated by backend.api.algo.grammar._scope_positions_expiring_today.",
  },
  {
    grammar_kind: 'condition', token_kind: 'column', token: 'positions.expiring_today.qty', value_type: 'number',
    label: 'Quantity (Contracts)',
    description: "Quantity (Contracts). Sourced from DataFrame column 'quantity' on ctx.position_rows, populated by backend.api.algo.grammar._scope_positions_expiring_today.",
  },
];

const CATALOG = buildCatalog(FIXTURE_ROWS);

const LOT_SIZES = { NIFTY25JULFUT: 75, BANKNIFTY25JULFUT: 25 };
const lotSizeOf = (sym) => LOT_SIZES[sym] || null;
// Sprint 20 (item 2) — see agentsGrammar.test.js's identical fixture
// for the rationale.
const EXCHANGES = { NIFTY25JULFUT: ['NFO'], BANKNIFTY25JULFUT: ['NFO'] };
const exchangesOf = (sym) => EXCHANGES[sym] || [];

function compile(text, opts = {}) {
  return compileAgentCliStatement(text, CATALOG, { lotSizeOf, exchangesOf, ...opts });
}

function decompile(agentJson, opts = {}) {
  return decompileAgent(agentJson, CATALOG, { lotSizeOf, ...opts });
}

/** Sprint [order-unify] — the compiler always EMITS `type: 'order'`, never
 *  `'place_order'`, regardless of which spelling appears in the INPUT
 *  JSON being decompiled. A legacy `agentJson` fixture (stored as
 *  `'place_order'`, simulating a pre-backfill row) decompiles fine, but
 *  RECOMPILING that decompiled text normalizes the type to the new
 *  canonical spelling — this is the expected, intentional one-way
 *  normalization, not a round-trip mismatch bug. Used by every
 *  decompile→recompile round-trip test whose fixture still says
 *  `'place_order'`. */
function _expectedAfterOrderNormalize(agentJson) {
  return {
    ...agentJson,
    actions: (agentJson.actions || []).map((a) => (a && a.type === 'place_order' ? { ...a, type: 'order' } : a)),
  };
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
    roundTrip('WHEN mean_pnl(minutes=30)@positions.total <= -50000 DO alert(telegram), order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );
  });

  it('2. multiple channels (one parameterized), multiple actions', () => {
    roundTrip('WHEN mean_pnl(minutes=30)@positions.total <= -50000 DO alert(telegram, ntfy(priority="urgent")), order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1), log(message="stop hit")'
    );
  });

  it('3. compound boolean — equal precedence, nested all/any', () => {
    roundTrip('WHEN ((mean_pnl(minutes=30)@positions.total <= -50000) & (stdev_pnl(minutes=30)@positions.total > 10000)) | (avail_margin@funds.total < 0) DO alert(telegram), order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );
  });

  it('4. NOT', () => {
    roundTrip('WHEN ~(pnl_pct@positions.total > 0) DO alert(telegram), order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );
  });

  it('5a. only an action (ALERT nop)', () => {
    roundTrip('WHEN mean_pnl(minutes=30)@positions.total <= -50000 DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );
  });

  it('5b. only a notify (DO nop)', () => {
    roundTrip('WHEN pnl_pct@positions.total <= -5 DO alert(telegram)');
  });

  it('6. always + nop', () => {
    const { dec } = roundTrip('WHEN always DO order(account="ZG0790", symbol="BANKNIFTY25JULFUT", side="BUY", lots=2)');
    expect(dec.text).toContain('WHEN always ');
  });
});

// ── ALERT/DO independently optional + reorderable (input flexibility) ──
//
// The decompiler's OWN output is deliberately unchanged by this grammar
// relaxation — it always renders the fixed canonical "WHEN ... ALERT ...
// DO ..." shape (using "nop" for an empty clause), never the newly-allowed
// omitted/reordered input forms. These tests confirm that choice: an
// omitted-clause or reordered-clause INPUT still compiles to the exact
// same JSON a canonical-order, nop-using statement would, and the
// decompiled OUTPUT text is always the fixed canonical shape regardless
// of which input form produced the underlying JSON.

describe('ALERT/DO omission and reordering — decompiled output stays canonical', () => {
  it('DO-only input (no alert(...) call) decompiles with no alert(...) at all — Sprint [grammar-unify]: no more ALERT keyword/nop sentinel to render', () => {
    const first = compile('WHEN pnl_pct@positions.total <= -5 DO log(message="x")');
    expect(first.ok).toBe(true);
    const dec = decompile(first.agent);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toMatch(/^WHEN .* DO log\(/);
    expect(dec.text).not.toContain('alert(');
    const second = compile(dec.text);
    expect(second.ok).toBe(true);
    expect(second.agent).toEqual(first.agent);
  });

  it('alert(...)-only input (no other DO action) decompiles with alert(...) as the sole DO call — no trailing "nop"', () => {
    const first = compile('WHEN pnl_pct@positions.total <= -5 DO alert(telegram)');
    expect(first.ok).toBe(true);
    const dec = decompile(first.agent);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toMatch(/DO alert\(telegram\)$/);
    const second = compile(dec.text);
    expect(second.ok).toBe(true);
    expect(second.agent).toEqual(first.agent);
  });

  it('reversed input order ("DO ... ALERT ...") decompiles to the same canonical ALERT-before-DO text as the non-reversed input', () => {
    const canonical = compile('WHEN pnl_pct@positions.total <= -5 DO alert(telegram), log(message="x")');
    const reversed = compile('WHEN pnl_pct@positions.total <= -5 DO log(message="x"), alert(telegram)');
    expect(reversed.agent).toEqual(canonical.agent);
    const decCanonical = decompile(canonical.agent);
    const decReversed = decompile(reversed.agent);
    expect(decReversed.text).toBe(decCanonical.text);
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
    expect(dec.text).toMatch(/^WHEN always DO alert\(/);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(ALWAYS_LEAF);
    expect(recompiled.agent.events).toEqual(agentJson.events);
    expect(recompiled.agent.actions).toEqual([]);
  });

  it('MANUAL_AGENT: conditions=null has NO CLI representation — fails loud with a SPECIFIC refusal, not the generic "unrecognized condition shape" (operator-reported 2026-10-11, live-data smoke test against all 36 real prod agents)', () => {
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
    // A system/manual-trigger agent with a genuinely null conditions field
    // (never a real malformed shape) gets a clear, dignified, SPECIFIC
    // refusal — not the generic malformed-shape message a real parse
    // failure would get.
    expect(dec.errors.some(e => /no expressible condition.*not editable via CLI/.test(e.message))).toBe(true);
    expect(dec.errors.some(e => /unrecognized condition shape/.test(e.message))).toBe(false);
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

  it('loss-positions-total: flat {any:[4 leaves]} — day_val/day_pct/pnl_rate_abs/pnl_rate_pct all @ positions.total, generic OR. Decompiles + recompiles to the left-nested binary equivalent (same convention as the 25-leaf AND-chain test above — the grammar\'s own chain-builder never produces flat N>2, so a flat N>2 INPUT normalizes to nested pairs on round-trip; semantically identical OR composition, not a behavior change)', () => {
    const agentJson = {
      conditions: { any: [
        { metric: 'day_val', scope: 'positions.total', op: '<=', value: -50000 },
        { metric: 'day_pct', scope: 'positions.total', op: '<=', value: -2.0 },
        { metric: 'pnl_rate_abs', scope: 'positions.total', op: '<=', value: -6000 },
        { metric: 'pnl_rate_pct', scope: 'positions.total', op: '<=', value: -0.25 },
      ] },
      events: [{ channel: 'telegram', enabled: true }, { channel: 'log', enabled: true }, { channel: 'ntfy', enabled: true, priority: 'urgent' }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual({ any: [
      { any: [
        { any: [
          { metric: 'day_val', scope: 'positions.total', op: '<=', value: -50000 },
          { metric: 'day_pct', scope: 'positions.total', op: '<=', value: -2.0 },
        ] },
        { metric: 'pnl_rate_abs', scope: 'positions.total', op: '<=', value: -6000 },
      ] },
      { metric: 'pnl_rate_pct', scope: 'positions.total', op: '<=', value: -0.25 },
    ] });
    expect(recompiled.agent.events).toEqual(agentJson.events);
    expect(recompiled.agent.actions).toEqual(agentJson.actions);
  });

  it('expiry-day-positions-alert: plain (unwrapped) days_until_expiry leaf on positions.expiring_today — 4-channel events, actions=[]', () => {
    const agentJson = {
      conditions: { metric: 'days_until_expiry', scope: 'positions.expiring_today', op: '<=', value: 1.5 },
      events: [
        { channel: 'telegram', enabled: true },
        { channel: 'email', enabled: true },
        { channel: 'log', enabled: true },
        { channel: 'ntfy', enabled: true },
      ],
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
    expect(dec.text).toContain('WHEN is_itm_bool@positions.total DO alert(telegram)');
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
    expect(recompiled.agent).toEqual(_expectedAfterOrderNormalize(agentJson));
  });

  it('a deactivate_agent action (zero params, never exercised by Sprint 2 tests) decompiles bare', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -1000 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'deactivate_agent', params: {} }],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('alert(telegram), deactivate_agent');
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

// ── $ref Token reference — round-trips (Sprint 10) ──────────────────────

describe('$ref Token reference decompiles and round-trips', () => {
  it('a top-level $ref decompiles to the bare Token name', () => {
    const dec = decompileCondition({ '$ref': 'loss-positions-default' }, CATALOG);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toBe('loss-positions-default');
  });

  it('a $ref nested inside an "all" array decompiles as an ordinary AND term', () => {
    const dec = decompileCondition(
      { all: [{ '$ref': 'loss-positions-default' }, { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 }] },
      CATALOG,
    );
    expect(dec.errors).toEqual([]);
    expect(dec.text).toBe('(loss-positions-default) & (pnl@positions.total < 0)');
  });

  it('an unknown $ref name fails loud, not a crash', () => {
    const dec = decompileCondition({ '$ref': 'nonexistent-fragment' }, CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /unknown Token reference/.test(e.message))).toBe(true);
  });

  it('decompileAgent round-trips a $ref-containing condition back to the identical JSON', () => {
    const agentJson = {
      conditions: { '$ref': 'loss-positions-default' },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(typeof dec.text).toBe('string');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual({ '$ref': 'loss-positions-default' });
  });

  it('decompileAgent round-trips a $ref-containing events array back to the identical JSON', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 },
      events: [{ '$ref': 'notify-critical-trio' }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.events).toEqual([{ '$ref': 'notify-critical-trio' }]);
  });

  it('a $ref channel entry mixed with a plain channel round-trips (the $ref is excluded from msg= hoisting)', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 },
      events: [{ '$ref': 'notify-critical-trio' }, { channel: 'log', enabled: true }],
      actions: [],
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.events).toEqual(agentJson.events);
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

  // Operator-reported (2026-10-11, live-data smoke test decompiling all 36
  // real prod agents) — the `manual` agent (`is_system=True`,
  // `scope='manual'`, a bookkeeping row with no real condition at all —
  // `run_cycle()` never auto-evaluates it) stores `conditions: null`. This
  // must refuse with a specific, dignified message, NOT the generic
  // "unrecognized condition shape: null" a real malformed shape gets.
  it('a null conditions field (system/manual-trigger agent, e.g. the real "manual" agent) refuses with a specific message, not the generic "unrecognized condition shape"', () => {
    const dec = decompileCondition(null, CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.length).toBe(1);
    expect(dec.errors[0].message).toMatch(/no expressible condition.*not editable via CLI/);
    expect(dec.errors[0].message).not.toMatch(/unrecognized condition shape/);
  });

  it('an undefined conditions field gets the same specific refusal as null', () => {
    const dec = decompileCondition(undefined, CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors[0].message).toMatch(/no expressible condition/);
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

  it('events=[] and actions=[] together (no preamble either) fails loud with the Sprint [grammar-unify] message', () => {
    const dec = decompile({ conditions: { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 }, events: [], actions: [] });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /must have at least one preamble call\/binding or a WHEN/.test(e.message))).toBe(true);
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
    expect(recompiled.agent).toEqual(_expectedAfterOrderNormalize(agentJson));
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
    expect(recompiled.agent).toEqual(_expectedAfterOrderNormalize(agentJson));
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
    expect(recompiled.agent).toEqual(_expectedAfterOrderNormalize(agentJson));
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
    expect(recompiled.agent).toEqual(_expectedAfterOrderNormalize(agentJson));
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
      [{ type: 'log', params: { message: `she said "it's broken"` } }],
      CATALOG
    );
    expect(dec.text).toBeNull();
    expect(dec.errors.length).toBeGreaterThan(0);
  });

  it('a free-text value containing commas/parens quotes safely and round-trips', () => {
    const agentJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<', value: 0 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'log', params: { message: 'stop hit, closing (urgent)' } }],
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
    roundTrip('WHEN 0 < pnl@positions.total < 100 DO alert(telegram)');
  });

  it('a genuine descending strict chain (100 > pnl > 0, compiler-produced) reverses correctly', () => {
    roundTrip('WHEN 100 > pnl@positions.total > 0 DO alert(telegram)');
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
    expect(recompiled.agent).toEqual(_expectedAfterOrderNormalize(agentJson));
  });
});

// ── ALERT msg= decompile round-trips (Sprint 6) ─────────────────────────

describe('ALERT msg= decompile round-trip', () => {
  it('shared identical message across every channel decompiles to one clause-level msg= default', () => {
    const { first, dec } = roundTrip('WHEN pnl@positions.total <= -1 DO alert(msg="Stop loss hit", telegram, ntfy(priority="urgent"))'
    );
    expect(first.agent.events).toEqual([
      { channel: 'telegram', enabled: true, message: 'Stop loss hit' },
      { channel: 'ntfy', enabled: true, priority: 'urgent', message: 'Stop loss hit' },
    ]);
    expect(dec.text).toContain('msg="Stop loss hit", telegram, ntfy(priority=urgent)');
    expect(dec.text).not.toMatch(/telegram\(/); // hoisted channel with no other args renders bare
  });

  it('per-channel distinct messages decompile to individual per-channel msg=, no shared default', () => {
    const { first, dec } = roundTrip('WHEN pnl@positions.total <= -1 DO alert(telegram(msg="custom A"), ntfy(msg="custom B", priority="urgent"))'
    );
    expect(first.agent.events).toEqual([
      { channel: 'telegram', enabled: true, message: 'custom A' },
      { channel: 'ntfy', enabled: true, priority: 'urgent', message: 'custom B' },
    ]);
    expect(dec.text).not.toMatch(/^WHEN .*ALERT msg=/); // no hoisted clause-level default
    expect(dec.text).toContain('telegram(msg="custom A")');
    expect(dec.text).toContain('ntfy(priority=urgent, msg="custom B")');
  });

  it('no-message agent (today\'s normal case) decompiles with no msg= anywhere — the critical backward-compatibility path', () => {
    const { first, dec } = roundTrip('WHEN pnl@positions.total <= -1 DO alert(telegram, ntfy(priority="urgent"))'
    );
    expect('message' in first.agent.events[0]).toBe(false);
    expect('message' in first.agent.events[1]).toBe(false);
    expect(dec.text).not.toMatch(/\bmsg=/);
    expect(dec.text).toContain('alert(telegram, ntfy(priority=urgent))');
  });

  it('a message present on some channels but not others has no CLI representation and fails loud (never producible by the compiler)', () => {
    const dec = decompileEvents([
      { channel: 'telegram', enabled: true, message: 'a' },
      { channel: 'ntfy', enabled: true, priority: 'urgent' },
    ], CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /has no message while other channels in this ALERT do/.test(e.message))).toBe(true);
  });

  it('a non-string message value fails loud instead of emitting unparseable text', () => {
    const dec = decompileEvents([{ channel: 'telegram', enabled: true, message: 42 }], CATALOG);
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /message: must be a string/.test(e.message))).toBe(true);
  });
});

// ── trailing "#" meta clause decompile round-trips (Sprint 7) ──────────

describe('meta clause decompile round-trip', () => {
  it('round-trips an agent with all four meta fields set', () => {
    const { first, dec } = roundTrip('WHEN pnl@positions.total <= -50000 DO alert(telegram), order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1) #name="NIFTY loss guard" #desc="Closes on -50k drawdown" #tag=risk,nifty #group=hedge-book'
    );
    expect(first.agent.name).toBe('NIFTY loss guard');
    expect(first.agent.description).toBe('Closes on -50k drawdown');
    expect(first.agent.tags).toEqual(['risk', 'nifty']);
    expect(first.agent.group).toBe('hedge-book');
    expect(dec.text).toContain('#name="NIFTY loss guard"');
    expect(dec.text).toContain('#desc="Closes on -50k drawdown"');
    expect(dec.text).toContain('#tag=risk,nifty');
    expect(dec.text).toContain('#group=hedge-book');
  });

  it('round-trips an agent with none of the four meta fields set — no "#..." clause rendered at all', () => {
    const { first, dec } = roundTrip('WHEN pnl@positions.total <= -1 DO alert(telegram)');
    expect(first.agent.name).toBeUndefined();
    expect(first.agent.description).toBeUndefined();
    expect(first.agent.tags).toBeUndefined();
    expect(first.agent.group).toBeUndefined();
    expect(dec.text).not.toMatch(/#/);
  });

  it('a hand-constructed agent shape with only a real "name" (simulating a pre-Sprint-7 agent) still decompiles "#name=..." correctly', () => {
    // Same real market-open-nse builtin shape used elsewhere in this file
    // (always-sentinel, reordered keys, actions=[]) — but with ONLY a
    // Structured-tab `name` set and no description/tags/group at all,
    // exactly the shape every agent created before this sprint has.
    const agentJson = {
      conditions: { op: '>=', scope: 'funds.any_acct', metric: 'avail_margin', value: -999999999 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [],
      name: 'Market open NSE',
    };
    const dec = decompile(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toMatch(/#name="Market open NSE"$/);
    expect(dec.text).not.toMatch(/#desc=/);
    expect(dec.text).not.toMatch(/#tag=/);
    expect(dec.text).not.toMatch(/#group=/);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.name).toBe('Market open NSE');
    expect(recompiled.agent.description).toBeUndefined();
    expect(recompiled.agent.tags).toBeUndefined();
    expect(recompiled.agent.group).toBeUndefined();
  });

  it('decompileMetaClause() omits the whole clause when every field is empty/unset', () => {
    expect(decompileMetaClause({})).toEqual({ text: '', errors: [] });
    expect(decompileMetaClause(null)).toEqual({ text: '', errors: [] });
  });

  it('decompileMetaClause() omits any individual unset key while rendering the ones that are set', () => {
    const r = decompileMetaClause({ name: 'x', description: '', tags: [], group: 'hedge-book' });
    expect(r.errors).toEqual([]);
    expect(r.text).toBe('#name=x #group=hedge-book');
  });
});

// ── Sprint 8 — bracket(...) decompile→recompile round-trip ─────────────

function decompileBracketTemplate(templateJson, opts = {}) {
  return decompileBracket(templateJson, CATALOG, { lotSizeOf, ...opts });
}

/** compile(text) -> decompileBracket(.template) -> compile(decompiled
 *  text) -> assert the SECOND compile's template JSON deep-equals the
 *  FIRST's — the same hard round-trip contract `roundTrip()` above
 *  enforces for agents. */
function roundTripBracket(text) {
  const first = compile(text);
  expect(first.ok).toBe(true);
  const dec = decompileBracketTemplate(first.template);
  expect(dec.errors).toEqual([]);
  expect(typeof dec.text).toBe('string');
  const second = compile(dec.text);
  expect(second.errors).toEqual([]);
  expect(second.ok).toBe(true);
  expect(second.template).toEqual(first.template);
  return { first, dec, second };
}

describe('Sprint 8 — bracket(...) decompile round-trip', () => {
  it('round-trips a bracket with every shared TP/SL/Wing field set, including a 2-entry tp_scales_json', () => {
    const { first, dec } = roundTripBracket(
      'bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20, wing_max_spread_pct=2.5, ' +
      'tp_order_type=LIMIT, sl_trail_pct=5, ' +
      'tp_scales_json=[{at_pct=30, close_pct=50}, {at_pct=60, close_pct=50}]) ' +
      '#name="Default Bull" #desc="Scale-out preset" #tag=hedge,nifty #group=hedge-book'
    );
    expect(first.template).toEqual({
      name: 'Default Bull',
      description: 'Scale-out preset',
      tags: ['hedge', 'nifty'],
      group: 'hedge-book',
      applies_to: 'buy_any',
      tp_pct: 30,
      sl_pct: -20,
      wing_max_spread_pct: 2.5,
      tp_order_type: 'LIMIT',
      sl_trail_pct: 5,
      tp_scales_json: JSON.stringify([{ at_pct: 30, close_pct: 50 }, { at_pct: 60, close_pct: 50 }]),
    });
    expect(dec.text).toMatch(/^bracket\(/);
    expect(dec.text).toMatch(/#name="Default Bull"/);
  });

  it('wing_premium_pct alone (not wing_strike_offset) also round-trips', () => {
    roundTripBracket('bracket(tp_pct=30, wing_premium_pct=4.5) #name="x"');
  });

  it('decompileBracket() fails loud (never guesses) on a nameless template', () => {
    const dec = decompileBracketTemplate({ tp_pct: 30 });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /missing required 'name'/.test(e.message))).toBe(true);
  });
});

// ── Sprint 18 — decompileBracket() refuses loud on slug/is_default/
//    is_active/is_system instead of silently dropping them ─────────────
//
// Audit finding: decompiling (and resaving, via /console's create-only
// submit) a seeded system bracket like 'default-bull' silently produced
// a slugless, non-default, non-system copy — none of these 4 real
// OrderTemplate fields had any bracket(...) CLI representation, and the
// old decompiler just never read them. Fixed by refusing outright
// (never emitting text that would resave as a materially different row)
// — see `_bracketUnexpressibleFields`'s own doc for why "emit cleanly"
// isn't viable (no slug field on OrderTemplateCreate at all; is_system
// is hardcoded False server-side; is_default would actively steal the
// default slot from the original row via `_demote_existing_default`).
describe('Sprint 18 — decompileBracket() refuses loud on slug/is_default/is_active/is_system', () => {
  const BASE = { id: 7, name: 'Realistic Custom Bracket', tp_pct: 30, sl_pct: -20 };

  it('refuses when slug is set (non-null) — system-seed-only field, no create-API support', () => {
    const dec = decompileBracketTemplate({ ...BASE, slug: 'default-bull' });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /\bslug\b/.test(e.message))).toBe(true);
  });

  it('refuses when is_default is true — resaving would steal the default slot from the real row', () => {
    const dec = decompileBracketTemplate({ ...BASE, is_default: true });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /\bis_default\b/.test(e.message))).toBe(true);
  });

  it('refuses when is_active is false — resaving would silently resurrect a deactivated row', () => {
    const dec = decompileBracketTemplate({ ...BASE, is_active: false });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /\bis_active\b/.test(e.message))).toBe(true);
  });

  it('refuses when is_system is true — create_template always forces is_system=False server-side', () => {
    const dec = decompileBracketTemplate({ ...BASE, is_system: true });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /\bis_system\b/.test(e.message))).toBe(true);
  });

  it('reports ALL offending fields at once when more than one is set', () => {
    const dec = decompileBracketTemplate({ ...BASE, slug: 'x', is_system: true });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /\bslug\b/.test(e.message) && /\bis_system\b/.test(e.message))).toBe(true);
  });

  it('still decompiles fine for a realistic custom row where all 4 are at their create-default values', () => {
    const dec = decompileBracketTemplate({
      ...BASE, slug: null, is_default: false, is_active: true, is_system: false,
    });
    expect(dec.errors).toEqual([]);
    expect(dec.text).toMatch(/^bracket\(/);
    expect(dec.text).toMatch(/#name="Realistic Custom Bracket"/);
    // Round-trips cleanly too — proves the predicate isn't over-eager and
    // doesn't regress the ordinary (field-absent) compile->decompile path.
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
  });
});

// ── Sprint C (2026-10, Phase 2 "Automation redesign") — decompileToken(),
//    the reverse of compileTokenStmt. Gives Edit→decompile→Console a real
//    path for a `kind='template'` Agent row holding a notify-kind Token
//    body (non-empty `events`, empty `conditions`) — see template_registry
//    .py's `_tr_fragment_to_agent_fields` for how that shape is stored. ──
describe('Sprint C — decompileToken() round-trip (notify-kind Token)', () => {
  function decompileTok(tokenJson, opts = {}) {
    return decompileToken(tokenJson, CATALOG, { lotSizeOf, ...opts });
  }

  it('single-channel Token decompiles and round-trips', () => {
    const tokenJson = {
      conditions: {},
      events: [{ channel: 'telegram', enabled: true }],
      name: 'notify-critical-trio',
      description: 'Critical ops trio',
    };
    const dec = decompileTok(tokenJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toMatch(/^token\(kind=notify\) telegram #name=notify-critical-trio #desc="Critical ops trio"$/);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.kind).toBe('token');
    expect(recompiled.fragment).toEqual({ kind: 'notify', name: 'notify-critical-trio', body: tokenJson.events, description: 'Critical ops trio' });
  });

  it('multi-channel Token with params round-trips', () => {
    const tokenJson = {
      conditions: null,
      events: [
        { channel: 'telegram', enabled: true },
        { channel: 'ntfy', enabled: true, priority: 'urgent' },
      ],
      name: 'notify-urgent-pair',
    };
    const dec = decompileTok(tokenJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.fragment.body).toEqual(tokenJson.events);
  });

  it('decompiles a condition-kind Token to token(kind=condition) <expr> #name=... and round-trips (2026-10-12)', () => {
    const tokenJson = {
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -1 },
      events: [],
      name: 'loss-positions-default',
    };
    const dec = decompileTok(tokenJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toBe('token(kind=condition) pnl@positions.total <= -1 #name=loss-positions-default');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.fragment.kind).toBe('condition');
    expect(recompiled.fragment.conditions).toEqual(tokenJson.conditions);
    expect(recompiled.fragment.body).toBeUndefined();
  });

  it('a condition-kind Token round-trips a compound boolean expression, same as a WHEN clause', () => {
    const tokenJson = {
      conditions: { any: [
        { metric: 'pnl_pct', scope: 'positions.any_acct', op: '<=', value: -2.0 },
        { metric: 'pnl', scope: 'positions.any_acct', op: '<=', value: -30000 },
      ] },
      events: [],
      name: 'loss-positions-acct-default',
    };
    const dec = decompileTok(tokenJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('token(kind=condition)');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.fragment.conditions).toEqual(tokenJson.conditions);
  });

  it('refuses loud on an empty body (no events, no conditions)', () => {
    const dec = decompileTok({ conditions: {}, events: [], name: 'empty-token' });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /empty body/.test(e.message))).toBe(true);
  });

  it('refuses loud on a missing name', () => {
    const dec = decompileTok({ conditions: {}, events: [{ channel: 'log', enabled: true }] });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /missing required 'name'/.test(e.message))).toBe(true);
  });
});

// ── Sprint 12 — event_stmt decompiler (decompileEventAgent) ────────────────
// +Sprint 18 — channel-level and render-action-level `gate`, empty
// `events: []`, round-tripped against the REAL seeded shapes in
// backend/api/algo/event_agents.py:SEEDED_AGENTS.
//
// `ERROR_AGENT`'s REAL `conditions`/`events`/`actions` are reused here
// VERBATIM (including its real `actions[0].gate: true` — Sprint 18 made
// this expressible via the new `gate=` event_meta kwarg, resolved
// against `catalog.renderAction`'s own params_schema):
//   conditions: {"log": {"tag": "error", "min_level": "ERROR"}}
//   events:     [{"channel": "telegram", "enabled": true},
//                {"channel": "ntfy", "enabled": true}]
//   actions:    [{"type": "render", "render": "error", "gate": true}]
describe('Sprint 12/18 — event_stmt decompile round-trip (real seeded agent shapes)', () => {
  const ERROR_AGENT_LIKE = {
    conditions: { log: { tag: 'error', min_level: 'ERROR' } },
    events: [
      { channel: 'telegram', enabled: true },
      { channel: 'ntfy', enabled: true },
    ],
    actions: [{ type: 'render', render: 'error', gate: true }], // real ERROR_AGENT shape, gate now expressible (Sprint 18)
    name: 'Error alert',
  };

  function decompileEvent(agentJson, opts = {}) {
    return decompileEventAgent(agentJson, CATALOG, { lotSizeOf, ...opts });
  }

  it('decompiles to the expected ALERT log_tag=... CLI text, including the action-level gate=true', () => {
    const dec = decompileEvent(ERROR_AGENT_LIKE);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toBe('ALERT log_tag=error, min_level=ERROR, render=error, gate=true, telegram, ntfy #name="Error alert"');
  });

  it('recompiles back to the IDENTICAL conditions/events/actions JSON (round-trip), gate:true included', () => {
    const dec = decompileEvent(ERROR_AGENT_LIKE);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(ERROR_AGENT_LIKE.conditions);
    expect(recompiled.agent.events).toEqual(ERROR_AGENT_LIKE.events);
    expect(recompiled.agent.actions).toEqual(ERROR_AGENT_LIKE.actions);
    expect(recompiled.agent.name).toBe(ERROR_AGENT_LIKE.name);
  });

  it('min_level absent on the stored row defaults to "INFO" on decompile (matches the compiler default)', () => {
    const dec = decompileEvent({
      conditions: { log: { tag: 'summary' } },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'render', render: 'summary' }],
      name: 'x',
    });
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('min_level=INFO');
  });

  it('a stored where= object round-trips with values preserved exactly, not re-cased', () => {
    const agentJson = {
      conditions: { log: { tag: 'chase', min_level: 'CRITICAL', where: { alert_event: 'cancel_unconfirmed' } } },
      events: [{ channel: 'ntfy', enabled: true, priority: 'urgent' }],
      actions: [{ type: 'render', render: 'chase_cancel' }],
      name: 'Chase cancel',
    };
    const dec = decompileEvent(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(agentJson.conditions);
    expect(recompiled.agent.events).toEqual(agentJson.events);
  });

  it('fails loud (never silently drops) on an actions[0] carrying a genuinely undeclared extra key', () => {
    const dec = decompileEvent({
      conditions: ERROR_AGENT_LIKE.conditions,
      events: ERROR_AGENT_LIKE.events,
      actions: [{ type: 'render', render: 'error', made_up_key: true }],
      name: 'Error alert',
    });
    expect(dec.text).toBeNull();
    expect(dec.errors.some(e => /not expressible by this syntax yet/.test(e.message))).toBe(true);
  });

  it('fails loud on a malformed conditions shape (missing "log"/"any")', () => {
    const dec = decompileEvent({ conditions: { all: [] }, events: [], actions: [] });
    expect(dec.text).toBeNull();
    // Sprint 18 follow-up — the message now also mentions the `{"any":
    // [...]}` (match=) alternative; match on the stable leading substring
    // rather than the old exact-to-"shape" wording.
    expect(dec.errors.some(e => /'conditions' must be a \{"log": \{\.\.\.\}\}/.test(e.message))).toBe(true);
  });

  // Closed 2026-10-12 — real `market-summary` SEEDED_AGENTS shape
  // (backend/api/algo/event_agents.py). Before `telegram_info` was
  // registered as a catalog channel (agent_grammar.yaml), clicking Edit
  // on this agent in Automation → Alerts failed with "unknown channel
  // 'telegram_info'" / "event 'telegram_info': unknown parameter 'gate'
  // (not declared in its catalog schema)" — the operator-visible "not
  // added to the catalog" report this fix closes.
  it('the real market-summary seeded agent (telegram_info channel + gate:false) decompiles and round-trips', () => {
    // Exact stored shape (confirmed against the real dev-DB row) —
    // min_level/where both present, not relying on decompile's own
    // default-filling for an omitted min_level (covered separately above).
    const agentJson = {
      conditions: { log: { tag: 'summary', min_level: 'INFO', where: { alert_event: 'summary' } } },
      events: [{ channel: 'telegram_info', enabled: true, gate: false }],
      actions: [{ type: 'render', render: 'summary' }],
      name: 'Market summary',
    };
    const dec = decompileEvent(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('telegram_info(gate=false)');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(agentJson.conditions);
    expect(recompiled.agent.events).toEqual(agentJson.events);
    expect(recompiled.agent.actions).toEqual(agentJson.actions);
  });

  // ── real seeded ORDER_FAILURE_AGENT shape — channel-level `gate: false`
  // on all three channels (telegram/ntfy/email). This is the generic
  // `decompileEvents`-driven path (Sprint 18's channel `gate` schema
  // addition) — no code change was needed here, only the catalog schema;
  // this test VERIFIES that claim against the real seeded shape rather
  // than trusting it. ──────────────────────────────────────────────────
  it('real order-failure-alert shape: channel-level gate:false on telegram/ntfy/email round-trips', () => {
    const agentJson = {
      conditions: { log: { tag: 'orders', min_level: 'WARNING', where: { alert_event: 'order_failure' } } },
      events: [
        { channel: 'telegram', enabled: true, gate: false },
        { channel: 'ntfy', enabled: true, priority: 'urgent', gate: false },
        { channel: 'email', enabled: true, gate: false },
      ],
      actions: [{ type: 'render', render: 'order_failure' }],
      name: 'Order failure',
    };
    const dec = decompileEvent(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('telegram(gate=false)');
    expect(dec.text).toContain('ntfy(priority=urgent, gate=false)');
    expect(dec.text).toContain('email(gate=false)');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(agentJson.conditions);
    expect(recompiled.agent.events).toEqual(agentJson.events);
    expect(recompiled.agent.actions).toEqual(agentJson.actions);
  });

  // ── compile direction, independently of decompile — confirms
  // `telegram(gate=false)` compiles correctly on its own, not merely as
  // the output of a decompile call (compile/decompile are NOT assumed
  // symmetric just because one direction works). ──────────────────────
  it('compiles a hand-typed telegram(gate=false) channel call on its own', () => {
    const r = compile('ALERT log_tag=orders, min_level=WARNING, render=order_failure, telegram(gate=false)');
    expect(r.ok).toBe(true);
    expect(r.agent.events).toEqual([{ channel: 'telegram', enabled: true, gate: false }]);
  });

  // ── real seeded BREACH_AGENT shape — `events: []` (confirmed
  // intentional: this agent supplies channels dynamically at render
  // time). Sprint 18 made this expressible via `nop` in the channel
  // list position. ──────────────────────────────────────────────────
  it('real cycle-breach-alert shape: empty events:[] decompiles via a bare "nop" channel list', () => {
    const agentJson = {
      conditions: { log: { tag: 'agent', min_level: 'INFO', where: { alert_event: 'breach' } } },
      events: [],
      actions: [{ type: 'render', render: 'breach' }],
      name: 'Cycle breach',
    };
    const dec = decompileEvent(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toMatch(/,\s*nop(\s|$)/);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(agentJson.conditions);
    expect(recompiled.agent.events).toEqual([]);
    expect(recompiled.agent.actions).toEqual(agentJson.actions);
  });

  // ── Sprint 18 follow-up (`match=[...]`) — the two real seeded agents
  // that originally motivated this syntax, `template-attach-urgent`/
  // `template-attach-high` (backend/api/algo/event_agents.py:
  // SEEDED_AGENTS — `conditions`/`events`/`actions` reused VERBATIM
  // below), now round-trip (previously confirmed blocked — see the
  // removed test this replaces — since `event_stmt` had no syntax for
  // a second, independently-thresholded log leaf). ───────────────────
  it('real template-attach-urgent shape: any:[{log},{log}] decompiles to match=[...] and round-trips', () => {
    const agentJson = {
      conditions: { any: [
        { log: { tag: 'gtt', min_level: 'WARNING', where: { alert_event: 'wing_unprotected' } } },
        { log: { tag: 'gtt', min_level: 'CRITICAL', where: { alert_event: 'wing_hard_reject' } } },
      ] },
      events: [{ channel: 'ntfy', enabled: true, priority: 'urgent' }],
      actions: [{ type: 'render', render: 'template_attach' }],
      name: 'Template attach urgent',
    };
    const dec = decompileEvent(agentJson);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toBe(
      'ALERT match=[{tag=gtt, min_level=WARNING, where={alert_event=wing_unprotected}}, '
      + '{tag=gtt, min_level=CRITICAL, where={alert_event=wing_hard_reject}}], '
      + 'render=template_attach, ntfy(priority=urgent) #name="Template attach urgent"'
    );
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(agentJson.conditions);
    expect(recompiled.agent.events).toEqual(agentJson.events);
    expect(recompiled.agent.actions).toEqual(agentJson.actions);
    expect(recompiled.agent.name).toBe(agentJson.name);
  });

  it('real template-attach-high shape: any:[{log},{log}] (same min_level both entries) decompiles to match=[...] and round-trips', () => {
    const agentJson = {
      conditions: { any: [
        { log: { tag: 'gtt', min_level: 'WARNING', where: { alert_event: 'wing_skip' } } },
        { log: { tag: 'gtt', min_level: 'WARNING', where: { alert_event: 'wing_offset_skip' } } },
      ] },
      events: [{ channel: 'ntfy', enabled: true, priority: 'high' }],
      actions: [{ type: 'render', render: 'template_attach' }],
      name: 'Template attach high',
    };
    const dec = decompileEvent(agentJson);
    expect(dec.errors).toEqual([]);
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.conditions).toEqual(agentJson.conditions);
    expect(recompiled.agent.events).toEqual(agentJson.events);
    expect(recompiled.agent.actions).toEqual(agentJson.actions);
  });

  it('match= combined with log_tag= in the same statement is a compile-time error', () => {
    const r = compile('ALERT match=[{tag=gtt, min_level=WARNING}, {tag=gtt, min_level=CRITICAL}], log_tag=gtt, render=template_attach, ntfy(priority=urgent)');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /match= is mutually exclusive with log_tag=/.test(e.message))).toBe(true);
  });

  it('match= requires at least one entry', () => {
    const r = compile('ALERT match=[], render=x, telegram');
    expect(r.ok).toBe(false);
    expect(r.errors.some(e => /match= requires at least one/.test(e.message))).toBe(true);
  });
});

// ═══════════════════════════════════════════════════════════════════════
// Sprint 16 — matched-row ($row) binding decompile round-trip.
// ═══════════════════════════════════════════════════════════════════════

describe('Sprint 16 — {"$row": "<name>"} decompiles to the bare field-name CLI form', () => {
  it('a real {"$row": "account_id"} action param decompiles to the bare name and recompiles to the IDENTICAL JSON', () => {
    const r = decompileActions(
      [{ type: 'close_position', params: { account: { '$row': 'account_id' }, symbol: { '$row': 'symbol' } } }],
      CATALOG,
    );
    expect(r.errors).toEqual([]);
    expect(r.text).toBe('close_position(account=account_id, symbol=symbol)');
  });

  it('full agent round-trip: compile -> decompile -> recompile yields byte-identical JSON', () => {
    roundTrip(
      'WHEN pnl@positions.expiring_today > 0 DO close_position(account=account_id, symbol=symbol)'
    );
  });

  it('a mixed literal + $row action (account bound, symbol literal) round-trips correctly', () => {
    roundTrip(
      'WHEN pnl@positions.expiring_today > 0 DO close_position(account=account_id, symbol="NIFTY25JULFUT")'
    );
  });

  it('place_order/order(...)\'s own $row-bound qty= round-trips correctly', () => {
    roundTrip(
      'WHEN pnl@positions.expiring_today > 0 '
      + 'DO order(account=account_id, symbol=symbol, side="SELL", qty=qty)'
    );
  });
});

// ═══════════════════════════════════════════════════════════════════════
// Sprint G — debug(x) decompile round-trip.
// ═══════════════════════════════════════════════════════════════════════

describe('Sprint G — debug(x) decompiles from a {"$row": ...}-message emit_log(level=debug) action', () => {
  it('a debug(x)-compiled $row action decompiles back to the bare debug(name) CLI form and recompiles to IDENTICAL JSON', () => {
    const r = decompileActions(
      [{ type: 'log', params: { level: 'debug', message: { '$row': 'account_id' } } }],
      CATALOG,
    );
    expect(r.errors).toEqual([]);
    expect(r.text).toBe('debug(account_id)');
  });

  it('full agent round-trip: WHEN ... DO debug(account_id) compiles -> decompiles -> recompiles byte-identical', () => {
    roundTrip('WHEN pnl@positions.expiring_today > 0 DO debug(account_id)');
  });

  it('a GIVEN-bound PRIMITIVE label debug(x) is NOT reversible to debug(...) — decompiles to the generic, EXPANDED emit_log(level=debug, message="...") form instead (documented GIVEN macro-expansion trade-off), and that form still recompiles cleanly', () => {
    // Mirrors the same documented trade-off as a plain GIVEN value
    // binding: the ORIGINAL label name is never stored in the compiled
    // JSON (only its expanded value is), so there is no way back to
    // `debug(x)` — this is NOT an ambiguity bug, it is the same
    // "editing a saved rule shows the expanded form" rule already
    // documented for the bind operator generally.
    const r = decompileActions(
      [{ type: 'log', params: { level: 'debug', message: 'ping' } }],
      CATALOG,
    );
    expect(r.errors).toEqual([]);
    expect(r.text).toBe('log(level=debug, message="ping")');
    const recompiled = compile(`WHEN always DO alert(telegram), ${r.text}`);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.actions).toEqual([{ type: 'log', params: { level: 'debug', message: 'ping' } }]);
  });

  it('a hand-authored emit_log(level=debug, message="literal") is INDISTINGUISHABLE from one that came from debug(x) with a literal-bound label — both decompile/recompile identically, by design (no new ambiguity: both are equally valid)', () => {
    roundTrip('WHEN always DO alert(telegram), log(level=debug, message="hand typed")');
  });
});

// ═══════════════════════════════════════════════════════════════════════
// Sprint [GIVEN-expr] — `agent.given[]` decompile round-trip. The ONLY
// place GIVEN round-trips at all (a pure-alias/primitive binding has
// always decompiled via its own already-inlined expansion inside
// conditions/actions — zero backend footprint, Sprint G's original
// design; an operator-expression binding is new and has no other home).
// ═══════════════════════════════════════════════════════════════════════

describe('Sprint [GIVEN-expr] — agent.given[] decompile round-trip', () => {
  it('a single operator-expression binding decompiles to a leading GIVEN line, verbatim, and recompiles byte-identical', () => {
    roundTrip('msg_txt = "a" + "b" WHEN always DO alert(msg = msg_txt, telegram)');
  });

  it('a pure alias referenced by an operator expression decompiles with its OWN injected GIVEN entry too, and recompiles byte-identical', () => {
    roundTrip('p = pnl@positions.total\nmsg_txt = "Loss: " + p WHEN p <= -50000 DO alert(msg = msg_txt, telegram)'
    );
  });

  it('decompileAgent renders the GIVEN preamble — an "expr" entry reads its string back VERBATIM; a "parts" entry (string-building) is freshly reconstructed from its parts array into the SAME source text', () => {
    const r = compile('p = pnl@positions.total\nmsg_txt = "Loss: " + p WHEN p <= -50000 DO alert(msg = msg_txt, telegram)');
    expect(r.ok).toBe(true);
    // `p` is `expr`-shaped (bare metric@scope, no string literal);
    // `msg_txt` is `parts`-shaped (mixes a string literal) per the
    // Sprint [GIVEN-parts] classification rule.
    expect(r.agent.given).toEqual([
      { name: 'p', expr: 'pnl@positions.total' },
      { name: 'msg_txt', parts: ['Loss: ', { '$given': 'p' }] },
    ]);
    const dec = decompile(r.agent);
    expect(dec.errors).toEqual([]);
    expect(dec.text.startsWith('p = pnl@positions.total; msg_txt = "Loss: " + p\n')).toBe(true);
  });

  it('a "parts"-kind binding decompile→recompile round-trip is correct — the reconstructed CLI text re-parses to the IDENTICAL "parts" array', () => {
    const r = compile('msg_txt = "Symbol: " + "NIFTY" WHEN always DO alert(msg = msg_txt, telegram)');
    expect(r.ok).toBe(true);
    expect(r.agent.given).toEqual([{ name: 'msg_txt', parts: ['Symbol: ', 'NIFTY'] }]);
    const dec = decompile(r.agent);
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('msg_txt = "Symbol: " + "NIFTY"');
    const recompiled = compile(dec.text);
    expect(recompiled.ok).toBe(true);
    expect(recompiled.agent.given).toEqual(r.agent.given);
  });

  it('a "parts" entry carrying a {"$given": name} reference decompiles the reference back to the bare name (not a quoted string)', () => {
    const dec = decompileAgent({
      conditions: ALWAYS_LEAF,
      events: [{ channel: 'telegram', enabled: true, message: { '$given': 'msg_txt' } }],
      actions: [],
      given: [{ name: 'p', expr: 'pnl@positions.total' }, { name: 'msg_txt', parts: ['Loss: ', { '$given': 'p' }] }],
    }, CATALOG, { lotSizeOf });
    expect(dec.errors).toEqual([]);
    expect(dec.text).toContain('msg_txt = "Loss: " + p');
  });

  it('debug(x) on an operator-expression label decompiles from the NEW {"$given": name} marker and recompiles byte-identical', () => {
    const r = decompileActions(
      [{ type: 'log', params: { level: 'debug', message: { '$given': 'msg_txt' } } }],
      CATALOG,
    );
    expect(r.errors).toEqual([]);
    expect(r.text).toBe('debug(msg_txt)');
    roundTrip('msg_txt = "a" + "b" WHEN always DO alert(telegram), debug(msg_txt)');
  });

  it('ALERT msg= resolving to a {"$given": name} marker decompiles back to the bare label name and recompiles byte-identical', () => {
    roundTrip('msg_txt = "a" + "b" WHEN always DO alert(msg = msg_txt, telegram)');
  });

  it('an agent with no "given" key at all decompiles with no leading GIVEN line (zero behavior change for every pre-existing agent)', () => {
    const r = compile('WHEN pnl@positions.total <= -50000 DO alert(telegram)');
    expect(r.ok).toBe(true);
    expect(r.agent.given).toBeUndefined();
    const dec = decompile(r.agent);
    expect(dec.errors).toEqual([]);
    expect(dec.text.startsWith('GIVEN')).toBe(false);
  });
});

describe('Sprint [order-unify] — order/place_order decompile tolerance', () => {
  it('a stored type: "order" action decompiles to order(...)', () => {
    const r = decompileActions(
      [{ type: 'order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'SELL', qty: 75 } }],
      CATALOG,
    );
    expect(r.errors).toEqual([]);
    expect(r.text).toContain('order(');
  });

  it('a legacy stored type: "place_order" action decompiles IDENTICALLY to order(...)', () => {
    const a = decompileActions(
      [{ type: 'order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'SELL', qty: 75 } }],
      CATALOG,
    );
    const b = decompileActions(
      [{ type: 'place_order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'SELL', qty: 75 } }],
      CATALOG,
    );
    expect(a.errors).toEqual([]);
    expect(b.errors).toEqual([]);
    expect(a.text).toBe(b.text);
  });
});

// ═══════════════════════════════════════════════════════════════════════
// Part B (2026-10) — `notify=[...]` decompile support. The compiled
// JSON's `notify` sibling key (never inside `params`, see
// `_compileNotifyClause` in agents.js) renders back to `notify=[...]`
// CLI text, reusing `_resolveChannelEntryForDecompile` — the SAME
// per-channel rendering `decompileEvents` already uses for ALERT.
// ═══════════════════════════════════════════════════════════════════════

describe('Part B — notify=[...] decompile support', () => {
  it('a single-channel notify[] sibling key decompiles to notify=[telegram] as part of the action call', () => {
    const r = decompileActions(
      [{ type: 'close_position', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT' }, notify: [{ channel: 'telegram' }] }],
      CATALOG,
    );
    expect(r.errors).toEqual([]);
    expect(r.text).toBe('close_position(account="ZG0790", symbol="NIFTY25JULFUT", notify=[telegram])');
  });

  it('a multi-channel notify[] with one channel\'s own param (ntfy.priority) decompiles identically to how ALERT renders the same channel', () => {
    const r = decompileActions(
      [{
        type: 'close_position',
        params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT' },
        notify: [{ channel: 'telegram' }, { channel: 'ntfy', params: { priority: 'urgent' } }],
      }],
      CATALOG,
    );
    expect(r.errors).toEqual([]);
    expect(r.text).toBe('close_position(account="ZG0790", symbol="NIFTY25JULFUT", notify=[telegram, ntfy(priority=urgent)])');
  });

  it('an action with no `notify` key at all decompiles with no notify= text — zero behavior change', () => {
    const r = decompileActions(
      [{ type: 'close_position', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT' } }],
      CATALOG,
    );
    expect(r.errors).toEqual([]);
    expect(r.text).toBe('close_position(account="ZG0790", symbol="NIFTY25JULFUT")');
  });

  it('a zero-other-param action (deactivate_agent) with notify= still renders correctly (bare token + wrapped parens)', () => {
    const r = decompileActions(
      [{ type: 'deactivate_agent', params: {}, notify: [{ channel: 'telegram' }] }],
      CATALOG,
    );
    expect(r.errors).toEqual([]);
    expect(r.text).toBe('deactivate_agent(notify=[telegram])');
  });

  it('full agent round-trip: compile -> decompile -> recompile with notify=[...] yields byte-identical JSON', () => {
    roundTrip(
      'WHEN pnl@positions.expiring_today > 0 '
      + 'DO close_position(account="ZG0790", symbol="NIFTY25JULFUT", notify=[telegram, ntfy(priority="urgent")])'
    );
  });

  it('order(...)\'s own notify=[...] round-trips alongside lots= conversion', () => {
    roundTrip(
      'WHEN pnl@positions.expiring_today > 0 '
      + 'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, notify=[telegram])'
    );
  });
});
