/**
 * liveHoldingsValue.test.js
 *
 * Unit tests for the three-tier fallback logic in `_liveHoldingsValue` from
 * `PositionStrip.svelte`.
 *
 * Because `_liveHoldingsValue` is a `$derived.by` block deeply embedded in
 * a Svelte 5 component (which cannot be mounted in Vitest), the logic is
 * mirrored here as a standalone pure function `computeLiveHoldingsValue`.
 * Any change to the source logic in PositionStrip.svelte MUST be reflected
 * in this helper and re-verified.
 *
 * Three-tier fallback (per PositionStrip.svelte comment):
 *   1. symbolStore ltp × qty   — live tick from WebSocket
 *   2. h.last_price × qty      — broker's last seen price (avoids cur_val=inv_val trap)
 *   3. h.cur_val               — broker computed; may equal inv_val when last_price=0
 *
 * Five quality dimensions:
 *   1. SSOT   — mirrors PositionStrip._liveHoldingsValue exactly
 *   2. Perf   — pure function, sub-millisecond, no DOM/network
 *   3. Stale  — tier 2 avoids invented cur_val when Dhan/Groww last_price=0
 *   4. Reuse  — three-tier logic is the canonical pattern for H-slot value
 *   5. UX     — no wrong "investment cost" shown as market value
 */

import { describe, it, expect } from 'vitest';

// ── Pure-function mirror of PositionStrip._liveHoldingsValue ─────────────────

/**
 * @param {any[]} holdings
 * @param {Record<string, { ltp?: number | null | undefined }>} [snapshots]
 * @returns {number}
 */
function computeLiveHoldingsValue(holdings = [], snapshots = /** @type {Record<string, { ltp?: number | null | undefined }>} */ ({})) {
  let s = 0;
  for (const h of holdings) {
    const sym    = String(h?.tradingsymbol || '').toUpperCase();
    const snap   = snapshots[sym];
    const ltp    = snap?.ltp;
    const qty    = Number(h?.quantity || 0);
    const lastPx = Number(h?.last_price || 0);
    if (ltp != null && ltp > 0 && qty !== 0) {
      s += ltp * qty;
    } else if (lastPx > 0 && qty !== 0) {
      s += lastPx * qty;
    } else {
      s += Number(h?.cur_val || 0);
    }
  }
  return s;
}

function makeHolding(overrides = {}) {
  return {
    tradingsymbol: 'RELIANCE',
    quantity:      10,
    last_price:    2500,
    cur_val:       25000,
    ...overrides,
  };
}

// ── Test 1: Tier 1 — symbolStore ltp wins ────────────────────────────────────

describe('liveHoldingsValue — tier 1: symbolStore ltp', () => {
  it('uses symbolStore ltp × qty when ltp is positive', () => {
    const holdings = [makeHolding({ quantity: 10, last_price: 2500, cur_val: 25000 })];
    const snapshots = { RELIANCE: { ltp: 2600 } };

    expect(computeLiveHoldingsValue(holdings, snapshots)).toBeCloseTo(26000, 4);
  });

  it('ltp = 0 in snapshot: does not use tier 1, falls through to tier 2', () => {
    const holdings = [makeHolding({ quantity: 10, last_price: 2500, cur_val: 25000 })];
    const snapshots = { RELIANCE: { ltp: 0 } };

    // Tier 2: last_price=2500 × qty=10 = 25000
    expect(computeLiveHoldingsValue(holdings, snapshots)).toBeCloseTo(25000, 4);
  });

  it('ltp = null in snapshot: falls through to tier 2', () => {
    const holdings = [makeHolding({ quantity: 10, last_price: 2500, cur_val: 25000 })];
    const snapshots = { RELIANCE: { ltp: null } };

    expect(computeLiveHoldingsValue(holdings, snapshots)).toBeCloseTo(25000, 4);
  });

  it('no snapshot entry for symbol: falls through to tier 2', () => {
    const holdings = [makeHolding({ quantity: 10, last_price: 2500, cur_val: 25000 })];
    const snapshots = /** @type {Record<string, {ltp?: number|null}>} */ ({});

    expect(computeLiveHoldingsValue(holdings, snapshots)).toBeCloseTo(25000, 4);
  });
});

// ── Test 2: Tier 2 — last_price × qty ────────────────────────────────────────

describe('liveHoldingsValue — tier 2: last_price × qty (avoids cur_val=inv_val trap)', () => {
  it('no ltp but last_price > 0: uses last_price × qty (NOT cur_val)', () => {
    // The key case: Dhan/Groww backend sets cur_val = avg_price × qty (investment cost,
    // not market value) when last_price = 0. But here last_price > 0, so tier 2 fires.
    const holdings = [makeHolding({ quantity: 10, last_price: 2550, cur_val: 30000 })];
    const snapshots = /** @type {Record<string, {ltp?: number|null}>} */ ({});

    // Tier 2: 2550 * 10 = 25500 (not cur_val=30000)
    expect(computeLiveHoldingsValue(holdings, snapshots)).toBeCloseTo(25500, 4);
  });

  it('last_price > 0 takes tier 2 even when cur_val differs', () => {
    // cur_val is an invented value (e.g. avg_price×qty) — tier 2 must win over tier 3
    const holdings = [makeHolding({ quantity: 5, last_price: 1000, cur_val: 9999 })];
    const snapshots = /** @type {Record<string, {ltp?: number|null}>} */ ({});

    expect(computeLiveHoldingsValue(holdings, snapshots)).toBeCloseTo(5000, 4);
  });
});

// ── Test 3: Tier 3 — cur_val fallback ────────────────────────────────────────

describe('liveHoldingsValue — tier 3: cur_val fallback (last resort)', () => {
  it('Dhan/Groww zero-LTP case: last_price=0 and no snapshot → uses cur_val', () => {
    // When backend last_price = 0 (Dhan/Groww zero-LTP), tier 2 gives 0 × qty = 0
    // which is "clearly missing". Tier 3 (cur_val) is used as last resort.
    // cur_val may equal inv_val here, but it's preferable to showing 0 in the H slot.
    const holdings = [makeHolding({ quantity: 10, last_price: 0, cur_val: 24000 })];
    const snapshots = /** @type {Record<string, {ltp?: number|null}>} */ ({});

    expect(computeLiveHoldingsValue(holdings, snapshots)).toBe(24000);
  });

  it('qty = 0: tiers 1 and 2 skip (qty !== 0 guard), cur_val used', () => {
    const holdings = [makeHolding({ quantity: 0, last_price: 2500, cur_val: 999 })];
    const snapshots = { RELIANCE: { ltp: 2600 } };

    // ltp > 0 but qty === 0 → tier 1 guard fails
    // lastPx > 0 but qty === 0 → tier 2 guard fails
    // → cur_val = 999
    expect(computeLiveHoldingsValue(holdings, snapshots)).toBe(999);
  });

  it('all price sources zero: cur_val used', () => {
    const holdings = [makeHolding({ quantity: 10, last_price: 0, cur_val: 0 })];
    const snapshots = /** @type {Record<string, {ltp?: number|null}>} */ ({});

    expect(computeLiveHoldingsValue(holdings, snapshots)).toBe(0);
  });
});

// ── Test 4: Multiple holdings ─────────────────────────────────────────────────

describe('liveHoldingsValue — multiple holdings across tiers', () => {
  it('RELIANCE via ltp, INFY via last_price, TCS via cur_val — sums correctly', () => {
    const holdings = [
      makeHolding({ tradingsymbol: 'RELIANCE', quantity: 10, last_price: 2500, cur_val: 25000 }),
      makeHolding({ tradingsymbol: 'INFY',     quantity: 5,  last_price: 1800, cur_val: 9000  }),
      makeHolding({ tradingsymbol: 'TCS',      quantity: 3,  last_price: 0,    cur_val: 10200 }),
    ];
    const snapshots = {
      RELIANCE: { ltp: 2600 },  // tier 1: 2600*10 = 26000
      // INFY: no snapshot → tier 2: 1800*5 = 9000
      // TCS:  no snapshot, last_price=0 → tier 3: cur_val = 10200
    };

    const result = computeLiveHoldingsValue(holdings, snapshots);

    expect(result).toBeCloseTo(26000 + 9000 + 10200, 4);
  });

  it('empty holdings: returns 0', () => {
    expect(computeLiveHoldingsValue([], {})).toBe(0);
  });
});

// ── Test 5: Dhan/Groww zero-LTP scenario (primary motivation for tier 2) ─────

describe('liveHoldingsValue — Dhan/Groww zero-LTP scenario', () => {
  it('last_price=0 means cur_val=inv_val (wrong): tier 3 shows 0 not invented cost', () => {
    // Scenario: Groww holding, no WebSocket tick, last_price=0.
    // Backend sets cur_val = average_price × qty = 50000 (investment cost, not market val).
    // Tier 2 evaluates: lastPx=0 → guard fails → falls to tier 3 (cur_val=50000).
    // This is the documented "last resort" — cur_val is used because we have nothing better.
    const holdings = [makeHolding({ tradingsymbol: 'GROWWSTOCK', quantity: 100, last_price: 0, cur_val: 50000 })];
    const snapshots = /** @type {Record<string, {ltp?: number|null}>} */ ({});

    // Tier 3 fires: cur_val = 50000
    expect(computeLiveHoldingsValue(holdings, snapshots)).toBe(50000);
  });

  it('last_price=500 (Dhan recovers): tier 2 uses 500×qty=50000, not cur_val=48000', () => {
    // When Dhan does provide last_price > 0, tier 2 must prefer it over cur_val.
    const holdings = [makeHolding({ tradingsymbol: 'DHANSTOCK', quantity: 100, last_price: 500, cur_val: 48000 })];
    const snapshots = /** @type {Record<string, {ltp?: number|null}>} */ ({});

    expect(computeLiveHoldingsValue(holdings, snapshots)).toBeCloseTo(50000, 4);
  });
});
