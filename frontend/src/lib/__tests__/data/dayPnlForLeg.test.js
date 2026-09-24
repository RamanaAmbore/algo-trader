/**
 * Tests for the derivatives page's per-candidate Day P&L path (`_candDayPnl`
 * in derivatives/+page.svelte), which delegates directly to the real
 * `livePositionDayPnl` SSOT in nav.js — same function Pulse uses.
 *
 * Historical note: this file previously replicated a local `_dayPnlForLeg`
 * helper that no longer exists in +page.svelte (superseded by `_candDayPnl`
 * calling `livePositionDayPnl` directly during the Day P&L / Exp P&L
 * baseline-diff redesign). Rewritten to exercise the real shared function
 * instead of a stale local reimplementation.
 *
 * Quality dimensions:
 *   1. SSOT: calls the actual `livePositionDayPnl` from nav.js — no local reimplementation
 *   2. Performance: pure synchronous function, no async / side-effects
 *   3. Stale-code: guards against re-introducing a diverging local copy
 *   4. Reuse: same function used by MarketPulse, portfolioStore, and the derivatives page
 *   5. UX: correct per-leg Day P&L feeds the Legs TOTAL row and NavStrip P pill
 */

import { describe, it, expect } from 'vitest';
import { livePositionDayPnl, baseDayPnlForPosition } from '$lib/data/nav.js';

// _candDayPnl's exact field mapping (see derivatives/+page.svelte):
//   pollLtp: c.ltp, qty: c.qty, dcvRow: c
function candDayPnl(c, legLiveLtp, marketOpen) {
  return livePositionDayPnl(
    { pollLtp: c.ltp ?? 0, qty: c.qty ?? 0, dcvRow: c },
    legLiveLtp,
    { marketOpen },
  );
}

describe('_candDayPnl (derivatives page) — new intraday position (no prev_settlement_pnl)', () => {
  it('base = pnl when prev_settlement_pnl absent; live delta layers on top', () => {
    const c = { prev_close: 1000, ltp: 1000, qty: 10, avg_cost: 95, pnl: 500, prev_settlement_pnl: null };
    // base = 500; delta = (1050-1000)*10 = 500 → result = 1000
    const result = candDayPnl(c, 1050, true);
    expect(result).toBe(1000);
  });

  it('market closed → live delta not applied, returns base (pnl)', () => {
    const c = { prev_close: 1000, ltp: 1000, qty: 10, avg_cost: 95, pnl: 200, prev_settlement_pnl: null };
    const result = candDayPnl(c, 1050, false);
    expect(result).toBe(200);
  });
});

describe('_candDayPnl — overnight position (prev_settlement_pnl present)', () => {
  it('base = pnl - prev_settlement_pnl; live delta layers on top for a long position', () => {
    const c = { prev_close: 1000, ltp: 1020, qty: 10, avg_cost: 900, pnl: 700, prev_settlement_pnl: 500 };
    // base = 200; delta = (1050-1020)*10 = 300 → result = 500
    const result = candDayPnl(c, 1050, true);
    expect(result).toBe(500);
  });

  it('short overnight position (oq<0 economically, qty<0): live delta sign follows qty', () => {
    const c = { prev_close: 1000, ltp: 980, qty: -10, avg_cost: 1100, pnl: -700, prev_settlement_pnl: -500 };
    // base = -200; delta = (950-980)*(-10) = 300 → result = 100
    const result = candDayPnl(c, 950, true);
    expect(result).toBe(100);
  });
});

describe('_candDayPnl — no live tick / pollLtp=0 guards', () => {
  it('legLiveLtp null → falls back to base', () => {
    const c = { prev_close: 1000, ltp: 1000, qty: 10, avg_cost: 900, pnl: 700, prev_settlement_pnl: 500 };
    const result = candDayPnl(c, null, true);
    expect(result).toBe(baseDayPnlForPosition(c));
    expect(result).toBe(200);
  });

  it('legLiveLtp = 0 (non-positive) → falls back to base', () => {
    const c = { prev_close: 1000, ltp: 1000, qty: 10, avg_cost: 900, pnl: 400, prev_settlement_pnl: 220 };
    const result = candDayPnl(c, 0, true);
    expect(result).toBe(180);
  });

  it('ltp (pollLtp) = 0 → delta not applied, returns base', () => {
    const c = { prev_close: 1000, ltp: 0, qty: 10, avg_cost: 900, pnl: 400, prev_settlement_pnl: 220 };
    const result = candDayPnl(c, 1050, true);
    expect(result).toBe(180);
  });
});

describe('_candDayPnl — edge cases', () => {
  it('qty=0 → delta not applied, returns base', () => {
    const c = { prev_close: 1000, ltp: 1000, qty: 0, avg_cost: 900, pnl: 0, prev_settlement_pnl: 0 };
    const result = candDayPnl(c, 1050, true);
    expect(result).toBe(0);
  });

  it('no prev_settlement_pnl and no pnl → base defaults to 0', () => {
    const c = { prev_close: 1000, ltp: 1000, qty: 10 };
    const result = candDayPnl(c, 1050, false);
    expect(result).toBe(0);
  });
});
