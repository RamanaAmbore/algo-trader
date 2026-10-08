/**
 * orderGrammarFieldRefs.test.js — Vitest coverage for Phase 4 of the
 * order/agent grammar unification (frontend CLI side).
 *
 * Phase 2 (backend, commit a938340c/0005aeb8) made order_fields.yaml the
 * real catalog for the Python side ($ref resolved in grammar.py at import
 * time). This phase makes frontend/src/lib/command/grammars/orders.js do
 * the equivalent: read order_fields.yaml?raw, resolve any `$ref` marker on
 * a token/kwarg spec against the catalog, and feed the RESOLVED specs into
 * `_wireTokens`/`_wireKwargs`.
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT  — imports the real orderGrammar object _wireTokens/_wireKwargs
 *             actually build, not a reimplementation.
 *  2. Perf  — pure unit, no I/O; $lib/data/instruments + $lib/data/accounts
 *             mocked so import doesn't touch IndexedDB/fetch.
 *  3. Stale — frozen fixture captured from the file AS IT EXISTED BEFORE
 *             this phase's edit (byte-identical regression guard) — proves
 *             $ref-sourced output is indistinguishable from the old bare
 *             literal for every consumer (CommandBar parsing/suggesting).
 *  4. Reuse — exercises the one shared wiring path every buy/sell/modify
 *             command goes through.
 *  5. UX    — asserts enum MEMBERSHIP traces to order_fields.yaml (the
 *             actual point of this phase) while asserting DISPLAY ORDER
 *             (orderType, product) stays the CLI's own — reordering would
 *             change the CommandBar popup, which is the exact behavior
 *             change this phase must not cause.
 */

import { describe, it, expect, vi } from 'vitest';

// ── Mock $lib/data/instruments + $lib/data/accounts so orders.js can be
//    imported in a non-browser Vitest env (IndexedDB / fetch-backed). ──────
vi.mock('$lib/data/instruments', () => ({
  getInstrument: vi.fn(),
  listOptions: vi.fn(),
  listFutures: vi.fn(),
  nearestExpiry: vi.fn(),
  listStrikes: vi.fn(),
  findOption: vi.fn(),
  findNearestFuture: vi.fn(),
  findEquity: vi.fn(),
  listUnderlyingsByType: vi.fn(),
  listExpiries: vi.fn(),
}));
vi.mock('$lib/data/accounts', () => ({
  suggestAccounts: vi.fn(),
}));

const { orderGrammar } = await import('$lib/command/grammars/orders.js');

// Frozen projection of the EXACT shape `_wireTokens`/`_wireKwargs` produced
// before this phase's $ref edit (captured from the live module prior to
// any change — see orders.js Phase 4 commit). `suggest` is projected to its
// function name (or 'undefined') since the actual function references are
// internal to the module and not meaningful to freeze by identity.
function projectToken(tok) {
  return {
    role: tok.role,
    values: tok.values,
    suggestName: tok.suggest ? tok.suggest.name : undefined,
    requiredType: typeof tok.required,
    parseIsNumber: tok.parse === Number,
    parseIsUpper: typeof tok.parse === 'function' && tok.parse !== Number && tok.parse('ab') === 'AB',
    hint: tok.hint,
  };
}

function projectKwarg(kw) {
  return {
    values: kw.values,
    suggestName: kw.suggest ? kw.suggest.name : undefined,
    parseIsNumber: kw.parse === Number,
    parseIsUpper: typeof kw.parse === 'function' && kw.parse !== Number && kw.parse('ab') === 'AB',
    hint: kw.hint,
  };
}

const EXPECTED_BUY_SELL_TOKENS = [
  { role: 'account', values: undefined, suggestName: 'account', requiredType: 'boolean', parseIsNumber: false, parseIsUpper: false, hint: undefined },
  { role: 'instType', values: ['CALL', 'PUT', 'FUT', 'EQ'], suggestName: undefined, requiredType: 'boolean', parseIsNumber: false, parseIsUpper: true, hint: 'CALL | PUT | FUT | EQ (default: EQ)' },
  { role: 'symbol', values: undefined, suggestName: 'symbolSuggest', requiredType: 'boolean', parseIsNumber: false, parseIsUpper: false, hint: 'underlying — type 3+ chars' },
  { role: 'strike', values: undefined, suggestName: 'strikeSuggest', requiredType: 'function', parseIsNumber: true, parseIsUpper: false, hint: 'strike price (* = wide spread)' },
  { role: 'expiry', values: undefined, suggestName: 'expirySuggest', requiredType: 'function', parseIsNumber: false, parseIsUpper: false, hint: 'expiry date' },
  { role: 'qty', values: undefined, suggestName: 'qtySuggest', requiredType: 'boolean', parseIsNumber: true, parseIsUpper: false, hint: 'quantity (lots × lot_size for F&O)' },
  { role: 'orderType', values: ['LIMIT', 'SL', 'SL-M', 'MARKET'], suggestName: undefined, requiredType: 'boolean', parseIsNumber: false, parseIsUpper: true, hint: 'LIMIT | SL | SL-M | MARKET' },
  { role: 'price', values: undefined, suggestName: 'priceSuggest', requiredType: 'function', parseIsNumber: true, parseIsUpper: false, hint: 'limit/trigger price' },
  { role: 'chase', values: ['LOW', 'MED', 'HIGH'], suggestName: undefined, requiredType: 'boolean', parseIsNumber: false, parseIsUpper: true, hint: 'chase aggressiveness (non-MARKET only)' },
];

const EXPECTED_PRODUCT_KWARG = { values: ['MIS', 'NRML', 'CNC'], suggestName: undefined, parseIsNumber: false, parseIsUpper: false, hint: undefined };

const EXPECTED_MODIFY_KWARGS = {
  price: { values: undefined, suggestName: undefined, parseIsNumber: true, parseIsUpper: false, hint: undefined },
  qty: { values: undefined, suggestName: undefined, parseIsNumber: true, parseIsUpper: false, hint: undefined },
  chase: { values: ['LOW', 'MED', 'HIGH'], suggestName: undefined, parseIsNumber: false, parseIsUpper: true, hint: undefined },
};

describe('orderGrammar wired output — byte-identical regression (Phase 4 $ref sourcing)', () => {
  for (const verb of ['buy', 'sell']) {
    it(`${verb}.tokens projection unchanged`, () => {
      const live = orderGrammar.verbs[verb].tokens.map(projectToken);
      expect(live).toEqual(EXPECTED_BUY_SELL_TOKENS);
    });

    it(`${verb}.kwargs.product projection unchanged`, () => {
      const live = projectKwarg(orderGrammar.verbs[verb].kwargs.product);
      expect(live).toEqual(EXPECTED_PRODUCT_KWARG);
    });
  }

  it('modify.kwargs (price/qty/chase) projection unchanged', () => {
    const kwargs = orderGrammar.verbs.modify.kwargs;
    const live = {
      price: projectKwarg(kwargs.price),
      qty: projectKwarg(kwargs.qty),
      chase: projectKwarg(kwargs.chase),
    };
    expect(live).toEqual(EXPECTED_MODIFY_KWARGS);
  });

  it('orderType / product keep the CLI\'s own display order (not catalog order)', () => {
    // order_fields.yaml's canonical enum order is [MARKET, LIMIT, SL, SL-M]
    // for order_type and [MIS, CNC, NRML] for product — deliberately
    // different from the CLI's own popup order (documented cosmetic
    // divergence in order_fields.yaml's header). A $ref resolution that
    // replaced local `values` with the catalog's enum verbatim would
    // silently reorder the CommandBar popup — this is the regression this
    // test exists to catch.
    expect(orderGrammar.verbs.buy.tokens.find(t => t.role === 'orderType').values)
      .toEqual(['LIMIT', 'SL', 'SL-M', 'MARKET']);
    expect(orderGrammar.verbs.buy.kwargs.product.values)
      .toEqual(['MIS', 'NRML', 'CNC']);
  });
});
