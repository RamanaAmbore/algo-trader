/**
 * liveHoldingsToday.test.js
 *
 * Unit tests for the _liveHoldingsToday computation in PositionStrip.svelte.
 *
 * Because _liveHoldingsToday is a private $derived.by inside a Svelte component,
 * we extract the identical pure logic into a helper and test that directly —
 * matching the pattern used in positionsDayPnlStore.test.js.
 *
 * Five quality dimensions:
 *   1. SSOT   — logic mirrors PositionStrip._liveHoldingsToday exactly
 *   2. Perf   — pure unit, no DOM / network / Svelte runtime, sub-millisecond
 *   3. Stale  — post-settlement and market-closed paths return correct fallback
 *   4. Reuse  — getSnapshot abstracted as a parameter so tests stay isolated
 *   5. UX     — verifies that no phantom zeroes appear in the NavStrip H∆ slot
 */

import { describe, it, expect } from 'vitest';

// ── Pure reimplementation of the PositionStrip._liveHoldingsToday logic ──────
//
// Signature matches the component's derived exactly.  `getSnapshot` is passed
// in as a parameter so tests can inject any map without a Svelte store.
//
// KEEP THIS IN SYNC with PositionStrip.svelte lines 426-445 whenever that
// block changes.
function computeLiveHoldingsToday(holdings, getSnapshot) {
  let s = 0;
  for (const h of holdings) {
    const sym     = String(h?.tradingsymbol || '').toUpperCase();
    const snapLtp = getSnapshot(sym)?.ltp;
    // Use h.last_price as fallback when symbolStore has no tick for this holding.
    const holdLtp = (snapLtp != null && snapLtp > 0) ? snapLtp : Number(h?.last_price ?? 0);
    const close   = Number(h?.close_price || 0);
    const qty     = Number(h?.opening_quantity || h?.quantity || 0);
    const dcv     = Number(h?.day_change_val ?? 0);
    if (holdLtp > 0 && close > 0 && qty !== 0 && Math.abs(holdLtp - close) > 0.005) {
      s += (holdLtp - close) * qty;
    } else {
      s += dcv;
    }
  }
  return s;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Build a minimal holding row. */
function makeHolding(overrides = {}) {
  return {
    tradingsymbol:    'RELIANCE',
    last_price:       2500,
    close_price:      2480,
    opening_quantity: 10,
    quantity:         10,
    day_change_val:   200,
    ...overrides,
  };
}

/** getSnapshot that returns null for every symbol (nothing subscribed). */
const noSnapshot = (_sym) => null;

/** getSnapshot that returns a fixed ltp for the given symbol. */
function snapWith(sym, ltp) {
  return (s) => (s === sym ? { ltp } : null);
}

// ── Case 1: getSnapshot returns null → h.last_price used ─────────────────────

describe('_liveHoldingsToday — fallback to h.last_price when snapshot absent', () => {
  it('uses last_price when getSnapshot returns null', () => {
    // last_price=2500, close=2480, qty=10 → (2500−2480)×10 = 200
    const h = makeHolding({ last_price: 2500, close_price: 2480, opening_quantity: 10 });
    expect(computeLiveHoldingsToday([h], noSnapshot)).toBe(200);
  });

  it('uses last_price when getSnapshot returns object with ltp=null', () => {
    const h = makeHolding({ last_price: 2500, close_price: 2480, opening_quantity: 10 });
    const getSnapshot = (_sym) => ({ ltp: null });
    expect(computeLiveHoldingsToday([h], getSnapshot)).toBe(200);
  });

  it('uses last_price when getSnapshot returns object with ltp=0', () => {
    const h = makeHolding({ last_price: 2500, close_price: 2480, opening_quantity: 10 });
    const getSnapshot = (_sym) => ({ ltp: 0 });
    expect(computeLiveHoldingsToday([h], getSnapshot)).toBe(200);
  });

  it('falls to dcv when last_price is also 0', () => {
    // No snap, no last_price → holdLtp=0, guard fails → use dcv=300
    const h = makeHolding({ last_price: 0, close_price: 2480, opening_quantity: 10, day_change_val: 300 });
    expect(computeLiveHoldingsToday([h], noSnapshot)).toBe(300);
  });

  it('falls to dcv when last_price is missing (undefined)', () => {
    const h = makeHolding({ close_price: 2480, opening_quantity: 10, day_change_val: 150 });
    delete h.last_price;
    expect(computeLiveHoldingsToday([h], noSnapshot)).toBe(150);
  });
});

// ── Case 2: ltp ≈ close (post-settlement) → day_change_val used ──────────────

describe('_liveHoldingsToday — post-settlement fingerprint (ltp ≈ close)', () => {
  it('falls to dcv when snapLtp equals close exactly (Kite post-settlement)', () => {
    // |2480 - 2480| = 0 ≤ 0.005 → use dcv
    const h = makeHolding({
      close_price: 2480, opening_quantity: 10, day_change_val: 250,
    });
    const getSnapshot = snapWith('RELIANCE', 2480);
    expect(computeLiveHoldingsToday([h], getSnapshot)).toBe(250);
  });

  it('falls to dcv when |ltp - close| = 0.004 (below threshold, not strictly greater than 0.005)', () => {
    // |2480.004 - 2480| = 0.004 < 0.005 → use dcv (within post-settlement band)
    const h = makeHolding({
      close_price: 2480, opening_quantity: 10, day_change_val: 200,
    });
    const getSnapshot = snapWith('RELIANCE', 2480.004);
    expect(computeLiveHoldingsToday([h], getSnapshot)).toBe(200);
  });

  it('uses formula when |ltp - close| is just above 0.005', () => {
    // |2480.006 - 2480| ≈ 0.006 > 0.005 → formula: 0.006 × 10 ≈ 0.06
    const h = makeHolding({
      close_price: 2480, opening_quantity: 10, day_change_val: 999,
    });
    const getSnapshot = snapWith('RELIANCE', 2480.006);
    const result = computeLiveHoldingsToday([h], getSnapshot);
    expect(result).toBeCloseTo(0.06, 5);
  });

  it('falls to dcv when last_price ≈ close (no snap, fallback path)', () => {
    // No snap; holdLtp = last_price = close → |2480 - 2480| = 0 → dcv
    const h = makeHolding({
      last_price: 2480, close_price: 2480, opening_quantity: 10, day_change_val: 300,
    });
    expect(computeLiveHoldingsToday([h], noSnapshot)).toBe(300);
  });
});

// ── Case 3: normal live path (valid ltp, close, qty, |ltp-close| > 0.005) ────

describe('_liveHoldingsToday — normal live formula path', () => {
  it('single holding: formula (ltp-close)×qty', () => {
    // snap ltp=2500, close=2480, qty=10 → 200
    const h = makeHolding({ close_price: 2480, opening_quantity: 10 });
    const getSnapshot = snapWith('RELIANCE', 2500);
    expect(computeLiveHoldingsToday([h], getSnapshot)).toBe(200);
  });

  it('snap ltp takes priority over h.last_price', () => {
    // snap ltp=2600, last_price=2500, close=2480, qty=10
    // formula uses snap: (2600−2480)×10 = 1200, not (2500−2480)×10=200
    const h = makeHolding({ last_price: 2500, close_price: 2480, opening_quantity: 10 });
    const getSnapshot = snapWith('RELIANCE', 2600);
    expect(computeLiveHoldingsToday([h], getSnapshot)).toBe(1200);
  });

  it('negative price move: (ltp-close)<0', () => {
    // ltp=2460, close=2480, qty=10 → −200
    const h = makeHolding({ close_price: 2480, opening_quantity: 10 });
    const getSnapshot = snapWith('RELIANCE', 2460);
    expect(computeLiveHoldingsToday([h], getSnapshot)).toBe(-200);
  });

  it('two holdings accumulate correctly', () => {
    const h1 = makeHolding({ tradingsymbol: 'RELIANCE', close_price: 2480, opening_quantity: 10 });
    const h2 = { ...makeHolding({ tradingsymbol: 'TCS', close_price: 3800, opening_quantity: 5 }) };
    const getSnapshot = (sym) => {
      if (sym === 'RELIANCE') return { ltp: 2500 };
      if (sym === 'TCS')      return { ltp: 3850 };
      return null;
    };
    // RELIANCE: (2500-2480)×10=200, TCS: (3850-3800)×5=250 → 450
    expect(computeLiveHoldingsToday([h1, h2], getSnapshot)).toBe(450);
  });

  it('uses opening_quantity before quantity', () => {
    const h = makeHolding({ close_price: 2480, opening_quantity: 5, quantity: 10 });
    const getSnapshot = snapWith('RELIANCE', 2500);
    // (2500-2480)×5 = 100, NOT 200
    expect(computeLiveHoldingsToday([h], getSnapshot)).toBe(100);
  });
});

// ── Case 4: stale close guard (close = 0) ────────────────────────────────────

describe('_liveHoldingsToday — stale close guard', () => {
  it('falls to dcv when close_price = 0 (broker returned zero)', () => {
    // Even if ltp is valid and well above zero, close=0 fails the guard
    const h = makeHolding({ close_price: 0, opening_quantity: 10, day_change_val: 400 });
    const getSnapshot = snapWith('RELIANCE', 2500);
    expect(computeLiveHoldingsToday([h], getSnapshot)).toBe(400);
  });

  it('falls to dcv when close_price is missing', () => {
    const h = makeHolding({ opening_quantity: 10, day_change_val: 350 });
    delete h.close_price;
    const getSnapshot = snapWith('RELIANCE', 2500);
    expect(computeLiveHoldingsToday([h], getSnapshot)).toBe(350);
  });

  it('empty holdings array → sum = 0', () => {
    expect(computeLiveHoldingsToday([], noSnapshot)).toBe(0);
  });

  it('qty = 0 → falls to dcv', () => {
    const h = makeHolding({
      close_price: 2480, opening_quantity: 0, quantity: 0, day_change_val: 111,
    });
    const getSnapshot = snapWith('RELIANCE', 2500);
    expect(computeLiveHoldingsToday([h], getSnapshot)).toBe(111);
  });
});
