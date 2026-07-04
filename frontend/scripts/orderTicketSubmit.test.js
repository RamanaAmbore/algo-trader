/**
 * orderTicketSubmit.test.js — Unit tests for pure OrderTicket submit helpers.
 *
 * These tests use only the Node.js test runner (no framework dependency).
 * Run with:  node --test frontend/scripts/orderTicketSubmit.test.js
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT  — all submission logic lives in orderTicketSubmit.js, not duplicated
 *  2. Perf  — no async I/O; all compute is synchronous
 *  3. Stale — grepped: no ternaries of this shape exist in OrderTicket.svelte submit()
 *  4. Reuse — same module used by OrderTicket.svelte (import paths match)
 *  5. UX    — edge cases: empty overrides, zero qty, MARKET/LIMIT paths, close vs open
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  numericOverride,
  classifyIntent,
  buildModifyPayload,
  buildOnSubmitPayload,
  buildPlacePayload,
  formatPlacementOk,
} from '../src/lib/order/orderTicketSubmit.js';

// ── numericOverride ───────────────────────────────────────────────────────────

describe('numericOverride', () => {
  test('empty string → null', () => {
    assert.strictEqual(numericOverride(''), null);
  });

  test('numeric string → Number', () => {
    assert.strictEqual(numericOverride('2.5'), 2.5);
  });

  test('numeric value 0 → 0 (not null)', () => {
    assert.strictEqual(numericOverride(0), 0);
  });

  test('numeric value 1.5 → 1.5', () => {
    assert.strictEqual(numericOverride(1.5), 1.5);
  });

  test('string "0" → 0 (not null)', () => {
    assert.strictEqual(numericOverride('0'), 0);
  });
});

// ── classifyIntent ────────────────────────────────────────────────────────────

describe('classifyIntent', () => {
  test('long + SELL → close', () => {
    assert.strictEqual(classifyIntent(10, 'SELL'), 'close');
  });

  test('short + BUY → close', () => {
    assert.strictEqual(classifyIntent(-5, 'BUY'), 'close');
  });

  test('long + BUY → open (adding to long)', () => {
    assert.strictEqual(classifyIntent(10, 'BUY'), 'open');
  });

  test('short + SELL → open (adding to short)', () => {
    assert.strictEqual(classifyIntent(-5, 'SELL'), 'open');
  });

  test('no position (0) + BUY → open', () => {
    assert.strictEqual(classifyIntent(0, 'BUY'), 'open');
  });

  test('no position (0) + SELL → open', () => {
    assert.strictEqual(classifyIntent(0, 'SELL'), 'open');
  });

  test('accepts string currentQty via Number() coercion', () => {
    // currentQty may arrive as a string from broker row
    assert.strictEqual(classifyIntent('10', 'SELL'), 'close');
  });
});

// ── buildModifyPayload ────────────────────────────────────────────────────────

const roundToTick = (v) => Number(v);  // identity for tests

describe('buildModifyPayload', () => {
  const base = {
    account: 'ZG0790',
    qty: 50,
    showLimit: true,
    showTrigger: false,
    roundToTick,
    price: '590.80',
    trigger: '',
    type: 'LIMIT',
    variety: 'regular',
    validity: 'DAY',
  };

  test('includes account, type, variety, validity', () => {
    const p = buildModifyPayload(base);
    assert.strictEqual(p.account, 'ZG0790');
    assert.strictEqual(p.order_type, 'LIMIT');
    assert.strictEqual(p.variety, 'regular');
    assert.strictEqual(p.validity, 'DAY');
  });

  test('price set when showLimit=true', () => {
    const p = buildModifyPayload(base);
    assert.strictEqual(p.price, 590.80);
  });

  test('price null when showLimit=false', () => {
    const p = buildModifyPayload({ ...base, showLimit: false });
    assert.strictEqual(p.price, null);
  });

  test('trigger_price null when showTrigger=false', () => {
    const p = buildModifyPayload(base);
    assert.strictEqual(p.trigger_price, null);
  });

  test('trigger_price set when showTrigger=true', () => {
    const p = buildModifyPayload({ ...base, showTrigger: true, trigger: '580' });
    assert.strictEqual(p.trigger_price, 580);
  });

  test('qty: 0 input → undefined (falsy coercion)', () => {
    const p = buildModifyPayload({ ...base, qty: 0 });
    assert.strictEqual(p.quantity, undefined);
  });

  test('qty: 50 → 50', () => {
    const p = buildModifyPayload(base);
    assert.strictEqual(p.quantity, 50);
  });
});

// ── buildOnSubmitPayload ──────────────────────────────────────────────────────

describe('buildOnSubmitPayload', () => {
  const base = {
    mode: 'paper',
    action: 'open',
    symbol: 'NIFTY26JUN22000PE',
    exchange: 'NFO',
    side: 'BUY',
    qty: 50,
    product: 'NRML',
    type: 'LIMIT',
    variety: 'regular',
    validity: 'DAY',
    showLimit: true,
    showTrigger: false,
    roundToTick,
    price: '200',
    trigger: '',
    account: 'ZG0790',
    chase: true,
    chaseAgg: 'med',
  };

  test('assembles core fields', () => {
    const p = buildOnSubmitPayload(base);
    assert.strictEqual(p.mode, 'paper');
    assert.strictEqual(p.side, 'BUY');
    assert.strictEqual(p.quantity, 50);
    assert.strictEqual(p.order_type, 'LIMIT');
    assert.strictEqual(p.account, 'ZG0790');
  });

  test('chase=true on LIMIT', () => {
    const p = buildOnSubmitPayload(base);
    assert.strictEqual(p.chase, true);
    assert.strictEqual(p.chase_aggressiveness, 'med');
  });

  test('chase=false on MARKET (showLimit=false)', () => {
    const p = buildOnSubmitPayload({ ...base, showLimit: false });
    assert.strictEqual(p.chase, false);
    assert.strictEqual(p.chase_aggressiveness, 'low');
  });

  test('chase_aggressiveness=low when chase=false even on LIMIT', () => {
    const p = buildOnSubmitPayload({ ...base, chase: false });
    assert.strictEqual(p.chase_aggressiveness, 'low');
  });

  test('price null on MARKET', () => {
    const p = buildOnSubmitPayload({ ...base, showLimit: false });
    assert.strictEqual(p.price, null);
  });
});

// ── buildPlacePayload ─────────────────────────────────────────────────────────

describe('buildPlacePayload', () => {
  const base = {
    mode: 'paper',
    side: 'SELL',
    resolvedSymbol: 'NIFTY26JUN22000PE',
    symbol: 'NIFTY',
    exchange: 'NFO',
    resolvedExchange: 'NFO',
    qty: 50,
    lotSize: 50,
    currentQty: 50,   // long position → SELL = close
    product: 'NRML',
    type: 'LIMIT',
    variety: 'regular',
    validity: 'DAY',
    showLimit: true,
    showTrigger: false,
    roundToTick,
    price: '200',
    trigger: '',
    account: 'ZG0790',
    chase: true,
    chaseAgg: 'low',
    templateId: 3,
    tpOverride: '2.5',
    slOverride: '',
    wingPremPctOverride: '',
    wingStrikeOffsetOverride: '50',
    strategyId: 7,
  };

  test('uses resolvedSymbol as tradingsymbol', () => {
    const p = buildPlacePayload(base);
    assert.strictEqual(p.tradingsymbol, 'NIFTY26JUN22000PE');
  });

  test('falls back to symbol when resolvedSymbol is null', () => {
    const p = buildPlacePayload({ ...base, resolvedSymbol: null });
    assert.strictEqual(p.tradingsymbol, 'NIFTY');
  });

  test('intent=close for long+SELL', () => {
    const p = buildPlacePayload(base);
    assert.strictEqual(p.intent, 'close');
  });

  test('intent=open for zero qty', () => {
    const p = buildPlacePayload({ ...base, currentQty: 0 });
    assert.strictEqual(p.intent, 'open');
  });

  test('lot_size_hint from lotSize > 0', () => {
    const p = buildPlacePayload(base);
    assert.strictEqual(p.lot_size_hint, 50);
  });

  test('lot_size_hint null when lotSize=0', () => {
    const p = buildPlacePayload({ ...base, lotSize: 0 });
    assert.strictEqual(p.lot_size_hint, null);
  });

  test('template_id wired through', () => {
    const p = buildPlacePayload(base);
    assert.strictEqual(p.template_id, 3);
  });

  test('tp_pct_override coerced to Number', () => {
    const p = buildPlacePayload(base);
    assert.strictEqual(p.tp_pct_override, 2.5);
  });

  test('sl_pct_override null when empty string', () => {
    const p = buildPlacePayload(base);
    assert.strictEqual(p.sl_pct_override, null);
  });

  test('wing_strike_offset_override coerced to Number', () => {
    const p = buildPlacePayload(base);
    assert.strictEqual(p.wing_strike_offset_override, 50);
  });

  test('wing_premium_pct_override null when empty string', () => {
    const p = buildPlacePayload(base);
    assert.strictEqual(p.wing_premium_pct_override, null);
  });

  test('strategy_id wired through', () => {
    const p = buildPlacePayload(base);
    assert.strictEqual(p.strategy_id, 7);
  });

  test('exchange falls back to NFO when all exchange fields empty', () => {
    const p = buildPlacePayload({ ...base, exchange: '', resolvedExchange: '' });
    assert.strictEqual(p.exchange, 'NFO');
  });
});

// ── formatPlacementOk ─────────────────────────────────────────────────────────

describe('formatPlacementOk', () => {
  test('LIMIT order includes @₹ price', () => {
    const s = formatPlacementOk({
      mode: 'paper',
      side: 'BUY',
      qty: 50,
      symbolLabel: 'RELIANCE',
      showLimit: true,
      price: '2500',
      roundedPrice: 2500,
      orderId: 'ABC123',
    });
    assert.ok(s.includes('@₹2500'), `expected @₹2500 in "${s}"`);
    assert.ok(s.includes('#ABC123'), `expected #ABC123 in "${s}"`);
    assert.ok(s.startsWith('PAPER BUY'), `expected PAPER BUY prefix in "${s}"`);
  });

  test('MARKET order shows @MKT', () => {
    const s = formatPlacementOk({
      mode: 'live',
      side: 'SELL',
      qty: 25,
      symbolLabel: 'NIFTY26JUN22000PE',
      showLimit: false,
      price: '',
      roundedPrice: 0,
      orderId: '999',
    });
    assert.ok(s.includes('@MKT'), `expected @MKT in "${s}"`);
    assert.ok(s.startsWith('LIVE SELL'), `expected LIVE SELL prefix in "${s}"`);
  });

  test('showLimit=true but price empty → @MKT', () => {
    // showLimit=true but _price is '' (form not filled yet) → no price
    const s = formatPlacementOk({
      mode: 'paper',
      side: 'BUY',
      qty: 50,
      symbolLabel: 'NIFTY',
      showLimit: true,
      price: '',
      roundedPrice: 0,
      orderId: '1',
    });
    assert.ok(s.includes('@MKT'), `expected @MKT when price empty, got "${s}"`);
  });

  test('unknown orderId renders as #?', () => {
    const s = formatPlacementOk({
      mode: 'paper',
      side: 'BUY',
      qty: 1,
      symbolLabel: 'RELIANCE',
      showLimit: false,
      price: '',
      roundedPrice: 0,
      orderId: '?',
    });
    assert.ok(s.includes('#?'), `expected #? in "${s}"`);
  });
});
