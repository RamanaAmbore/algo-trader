/**
 * pageLoad_sessionBoundary.test.js — Vitest tests for the 3D fix
 * (2026-10-23): pageLoad.js's expired/unresolvable checks used the bare
 * midnight boundary (todayIST()) instead of the 08:00 IST trading-session
 * boundary (tradingSessionDateIST()) that expiryPnl.js's
 * isExpiredHeldContract already uses.
 *
 * Bug: between 00:00-08:00 IST the day after a contract's own expiry day,
 * buildCandidatePositions' `isUnresolvable` check and buildCleanLegs' own
 * safety-net filter both flagged a still-mid-freeze-window leg as expired
 * hours too early — reproducing the exact GOLDM "flat 0 curve" regression
 * this codebase already fixed once, 8 hours early, while the Legs TOTAL
 * row and NavStrip (which use the correct boundary) kept showing the
 * right mark-to-spot value.
 *
 * Unlike pageLoad_expired.test.js (which mocks $lib/dateFormat.js to pin a
 * fixed date for its own, unrelated branch-coverage tests — and so cannot
 * see this exact bug, since it pins todayIST and tradingSessionDateIST to
 * the SAME value), this file deliberately does NOT mock dateFormat.js —
 * it exercises the REAL tradingSessionDateIST() against a fake system
 * clock, the same technique expiryPnl_sessionBoundary.test.js uses for
 * the sibling isExpiredHeldContract fix.
 *
 * Five quality dimensions:
 *  1. SSOT   — the real buildCandidatePositions/buildCleanLegs call sites,
 *              not a reimplementation.
 *  2. Perf   — pure unit, fake timers, no I/O.
 *  3. Stale  — reproduces the exact midnight-vs-08:00 mismatch window.
 *  4. Reuse  — same boundary convention as expiryPnl.js, NavStrip, the
 *              Legs TOTAL row.
 *  5. UX     — Exp P&L must stay a theoretical mark-to-spot value through
 *              the whole overnight closed window, never collapsing early.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { buildCandidatePositions, buildCleanLegs } from '$lib/derivatives/pageLoad.js';

/** Set the fake system clock to a specific IST wall-clock instant. */
function setISTTime(/** @type {string} */ isoDateNoOffset) {
  vi.setSystemTime(new Date(`${isoDateNoOffset}+05:30`));
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

/** Minimal position row for buildCandidatePositions. */
function makePos(sym, extra = {}) {
  return {
    symbol: sym,
    account: 'ZG0790',
    qty: 50,
    source: 'live',
    pnl: 1000,
    ...extra,
  };
}

const BASE_PARAMS = {
  holdings: [],
  drafts: [],
  target: 'GOLDM',
  selectedExpiries: [],
  selectedAccounts: [],
  simActive: false,
  proxiesForTarget: () => [],
};

function makeGetInst(map) {
  return (sym) => (map[sym] ? { x: map[sym] } : null);
}

describe('buildCandidatePositions — isUnresolvable uses the 08:00 IST session boundary, not bare midnight', () => {
  // Contract's own expiry day: 2026-10-15 (a Thursday).
  const SYM = 'GOLDM26OCT150000CE';
  const getInstrument = makeGetInst({ [SYM]: '2026-10-15' });

  it('THE BUG THIS FIXES: just after midnight the day after expiry (00:30 IST) — still resolvable, not tagged `_expired`', () => {
    setISTTime('2026-10-16T00:30:00');
    const positions = [makePos(SYM)];
    const result = buildCandidatePositions({ ...BASE_PARAMS, positions, getInstrument });
    const row = result.find(r => r.symbol === SYM);
    expect(row).toBeTruthy();
    expect(row._expired).toBeUndefined();
  });

  it('05:00 IST the next morning, still well before market open — still resolvable', () => {
    setISTTime('2026-10-16T05:00:00');
    const positions = [makePos(SYM)];
    const result = buildCandidatePositions({ ...BASE_PARAMS, positions, getInstrument });
    const row = result.find(r => r.symbol === SYM);
    expect(row._expired).toBeUndefined();
  });

  it('one minute before the next session boundary (07:59 IST) — still resolvable', () => {
    setISTTime('2026-10-16T07:59:00');
    const positions = [makePos(SYM)];
    const result = buildCandidatePositions({ ...BASE_PARAMS, positions, getInstrument });
    const row = result.find(r => r.symbol === SYM);
    expect(row._expired).toBeUndefined();
  });

  it('at the next trading session boundary (08:00 IST) — now tagged `_expired`', () => {
    setISTTime('2026-10-16T08:00:00');
    const positions = [makePos(SYM)];
    const result = buildCandidatePositions({ ...BASE_PARAMS, positions, getInstrument });
    const row = result.find(r => r.symbol === SYM);
    expect(row._expired).toBe(true);
  });
});

describe('buildCleanLegs — safety-net filter uses the 08:00 IST session boundary, not bare midnight', () => {
  const SYM = 'GOLDM26OCT150000CE';
  const getInst = makeGetInst({ [SYM]: '2026-10-15' });

  it('THE BUG THIS FIXES: just after midnight the day after expiry (00:30 IST) — leg still included in the /strategy-analytics payload', () => {
    setISTTime('2026-10-16T00:30:00');
    const legs = [{ kind: 'opt', symbol: SYM, qty: 50, avg_cost: 5, source: 'live' }];
    const result = buildCleanLegs(legs, getInst);
    expect(result).toHaveLength(1);
  });

  it('one minute before the session boundary (07:59 IST) — still included', () => {
    setISTTime('2026-10-16T07:59:00');
    const legs = [{ kind: 'opt', symbol: SYM, qty: 50, avg_cost: 5, source: 'live' }];
    const result = buildCleanLegs(legs, getInst);
    expect(result).toHaveLength(1);
  });

  it('at the next session boundary (08:00 IST) — now excluded', () => {
    setISTTime('2026-10-16T08:00:00');
    const legs = [{ kind: 'opt', symbol: SYM, qty: 50, avg_cost: 5, source: 'live' }];
    const result = buildCleanLegs(legs, getInst);
    expect(result).toHaveLength(0);
  });
});
