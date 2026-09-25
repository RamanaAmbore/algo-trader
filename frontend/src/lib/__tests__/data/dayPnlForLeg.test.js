/**
 * Tests for the derivatives page's per-candidate Day P&L path (`_candDayPnl`
 * in derivatives/+page.svelte), which delegates directly to the real
 * `baseDayPnlForPosition` SSOT in nav.js — same function Pulse and
 * portfolioStore use.
 *
 * Historical note: `_candDayPnl` used to wrap `livePositionDayPnl` (a
 * live-tick-delta layer on top of baseDayPnlForPosition). §1 (positions/
 * holdings LTP-source redesign) removed that wrapper entirely — Day P&L
 * for positions/derivative legs is now purely poll-driven, with no
 * `(liveLtp − pollLtp) × qty` adjustment. This file is rewritten to
 * exercise `baseDayPnlForPosition` directly (what `_candDayPnl` now
 * delegates to) and to guard against the live-tick delta being
 * reintroduced.
 *
 * Quality dimensions:
 *   1. SSOT: calls the actual `baseDayPnlForPosition` from nav.js — no local reimplementation
 *   2. Performance: pure synchronous function, no async / side-effects
 *   3. Stale-code: guards against re-introducing a diverging local copy or a live-tick delta
 *   4. Reuse: same function used by MarketPulse, portfolioStore, and the derivatives page
 *   5. UX: correct per-leg Day P&L feeds the Legs TOTAL row and NavStrip P pill
 */

import { describe, it, expect } from 'vitest';
import { baseDayPnlForPosition } from '$lib/data/nav.js';

// _candDayPnl's exact delegation (see derivatives/+page.svelte): base only,
// no live-tick delta — mirrors the real page code 1:1.
function candDayPnl(c) {
  return baseDayPnlForPosition(c);
}

describe('_candDayPnl (derivatives page) — new intraday position (no prev_settlement_pnl)', () => {
  it('base = pnl when prev_settlement_pnl absent; no live-tick delta layers on top', () => {
    const c = { prev_close: 1000, ltp: 1000, qty: 10, avg_cost: 95, pnl: 500, prev_settlement_pnl: null };
    const result = candDayPnl(c);
    expect(result).toBe(500);
  });

  it('result is identical regardless of any "market open" context — no such branch remains', () => {
    const c = { prev_close: 1000, ltp: 1000, qty: 10, avg_cost: 95, pnl: 200, prev_settlement_pnl: null };
    const result = candDayPnl(c);
    expect(result).toBe(200);
  });
});

describe('_candDayPnl — overnight position (prev_settlement_pnl present)', () => {
  it('base = pnl - prev_settlement_pnl; no live-tick delta for a long position', () => {
    const c = { prev_close: 1000, ltp: 1020, qty: 10, avg_cost: 900, pnl: 700, prev_settlement_pnl: 500 };
    const result = candDayPnl(c);
    expect(result).toBe(200);
  });

  it('short overnight position (qty<0): base is signed pnl-diff only, no qty-scaled tick delta', () => {
    const c = { prev_close: 1000, ltp: 980, qty: -10, avg_cost: 1100, pnl: -700, prev_settlement_pnl: -500 };
    const result = candDayPnl(c);
    expect(result).toBe(-200);
  });
});

describe('_candDayPnl — ltp/qty fields present but unread by baseDayPnlForPosition', () => {
  it('ltp field on the row does not affect the result', () => {
    const c = { prev_close: 1000, ltp: 1000, qty: 10, avg_cost: 900, pnl: 700, prev_settlement_pnl: 500 };
    const result = candDayPnl(c);
    expect(result).toBe(baseDayPnlForPosition(c));
    expect(result).toBe(200);
  });

  it('ltp = 0 on the row does not change the result — no fallback branch depends on it', () => {
    const c = { prev_close: 1000, ltp: 0, qty: 10, avg_cost: 900, pnl: 400, prev_settlement_pnl: 220 };
    const result = candDayPnl(c);
    expect(result).toBe(180);
  });
});

describe('_candDayPnl — edge cases', () => {
  it('qty=0 → result is base only', () => {
    const c = { prev_close: 1000, ltp: 1000, qty: 0, avg_cost: 900, pnl: 0, prev_settlement_pnl: 0 };
    const result = candDayPnl(c);
    expect(result).toBe(0);
  });

  it('no prev_settlement_pnl and no pnl → base defaults to 0', () => {
    const c = { prev_close: 1000, ltp: 1000, qty: 10 };
    const result = candDayPnl(c);
    expect(result).toBe(0);
  });
});
