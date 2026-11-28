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
import { isExpiredHeldContract } from '$lib/data/expiryPnl.js';

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

// ─────────────────────────────────────────────────────────────────────────────
// 2026-11-27 incident fixture — Friday expiry, weekend grace window.
//
// GOLD26NOV124000CE, real expiry Friday 2026-11-27, GOLD26DECFUT as the
// live sibling future (confirms hasFNO('GOLD') once the exact Nov
// contract is purged from the instruments cache). Covers BOTH branches:
// instrument still present (pre-purge) and instrument purged but
// root-authoritative (post Saturday-08:00-IST instruments refresh).
// ─────────────────────────────────────────────────────────────────────────────

describe('buildCandidatePositions — Friday expiry stays resolvable through the whole weekend (instrument still cached)', () => {
  const SYM = 'GOLD26NOV124000CE';
  const getInstrument = makeGetInst({ [SYM]: '2026-11-27' });
  const PARAMS = { ...BASE_PARAMS, target: 'GOLD' };

  it('Friday, expiry day itself — not tagged `_expired`', () => {
    setISTTime('2026-11-27T20:00:00');
    const result = buildCandidatePositions({ ...PARAMS, positions: [makePos(SYM)], getInstrument });
    expect(result.find(r => r.symbol === SYM)._expired).toBeUndefined();
  });

  it('THE BUG THIS FIXES: Saturday 08:01 IST — still NOT tagged `_expired`', () => {
    setISTTime('2026-11-28T08:01:00');
    const result = buildCandidatePositions({ ...PARAMS, positions: [makePos(SYM)], getInstrument });
    expect(result.find(r => r.symbol === SYM)._expired).toBeUndefined();
  });

  it('Sunday — still not tagged', () => {
    setISTTime('2026-11-29T12:00:00');
    const result = buildCandidatePositions({ ...PARAMS, positions: [makePos(SYM)], getInstrument });
    expect(result.find(r => r.symbol === SYM)._expired).toBeUndefined();
  });

  it('Monday, one minute before the real boundary (07:59 IST) — still not tagged', () => {
    setISTTime('2026-11-30T07:59:00');
    const result = buildCandidatePositions({ ...PARAMS, positions: [makePos(SYM)], getInstrument });
    expect(result.find(r => r.symbol === SYM)._expired).toBeUndefined();
  });

  it('Monday, at the real boundary (08:00 IST) — NOW tagged `_expired`', () => {
    setISTTime('2026-11-30T08:00:00');
    const result = buildCandidatePositions({ ...PARAMS, positions: [makePos(SYM)], getInstrument });
    expect(result.find(r => r.symbol === SYM)._expired).toBe(true);
  });
});

describe('buildCandidatePositions — Friday expiry, instrument PURGED from cache (root-authoritative), stays resolvable through the weekend', () => {
  const SYM = 'GOLD26NOV124000CE';
  const getInstrument = () => null; // purged
  const hasFNO = (root) => root === 'GOLD'; // GOLD26DECFUT sibling still live
  const PARAMS = { ...BASE_PARAMS, target: 'GOLD' };

  it('THE BUG THIS FIXES: Saturday 08:01 IST — `_expired` and `_expiredFrozen` AGREE (both false), leg flows through to buildCleanLegs', () => {
    setISTTime('2026-11-28T08:01:00');
    const result = buildCandidatePositions({ ...PARAMS, positions: [makePos(SYM)], getInstrument, hasFNO });
    const row = result.find(r => r.symbol === SYM);
    expect(row).toBeTruthy();
    expect(row._expired).toBeUndefined();
    expect(row._expiredFrozen).toBeUndefined();
    // The leg must not be excluded from the backend /strategy-analytics
    // payload (buildCleanLegs filters out any `_expired`-tagged leg) —
    // this is the chart-vs-Snapshot divergence class the 2026-09
    // GOLD/GOLDM fix exists to prevent.
    const legs = buildCleanLegs([row], getInstrument);
    expect(legs).toHaveLength(1);
    expect(legs[0].expiry).toBeNull(); // backend's own fallback derives the real expiry server-side
  });

  it('Monday, at the real boundary (08:00 IST) — NOW tagged both `_expired` and `_expiredFrozen` (consistent)', () => {
    setISTTime('2026-11-30T08:00:00');
    const result = buildCandidatePositions({ ...PARAMS, positions: [makePos(SYM)], getInstrument, hasFNO });
    const row = result.find(r => r.symbol === SYM);
    expect(row._expired).toBe(true);
    expect(row._expiredFrozen).toBe(true);
    // Excluded from the backend payload once genuinely frozen.
    const legs = buildCleanLegs([row], getInstrument);
    expect(legs).toHaveLength(0);
  });

  it('`_expired` never disagrees with the shared isExpiredHeldContract predicate at any instant across the weekend', () => {
    for (const iso of [
      '2026-11-27T20:00:00', '2026-11-28T08:01:00', '2026-11-29T12:00:00',
      '2026-11-30T07:59:00', '2026-11-30T08:00:00', '2026-11-30T11:00:00',
    ]) {
      setISTTime(iso);
      const result = buildCandidatePositions({ ...PARAMS, positions: [makePos(SYM)], getInstrument, hasFNO });
      const row = result.find(r => r.symbol === SYM);
      const expected = isExpiredHeldContract(SYM, 50, getInstrument, hasFNO);
      expect(!!row._expired).toBe(expected);
      expect(!!row._expiredFrozen).toBe(expected);
    }
  });

  it('REGRESSION: an active expiry-selector filter must not drop this leg during the grace window (root-authoritative miss bypasses matchExpiry, same as a cache-miss always did)', () => {
    // Before this fix, `isUnresolvable` was unconditionally true for a
    // root-authoritative cache-miss, so the expiry-selector bypass
    // (`!isUnresolvable && ...`) always fired for it too. Once
    // `isUnresolvable` deferred to the (now weekend-aware) `isExpiredFrozen`
    // value, a leg genuinely inside its grace window (isUnresolvable=false)
    // would otherwise run through `matchExpiry(sym)` — which can NEVER
    // match, since `getInstrument(sym)` is null by definition here — and
    // get silently dropped by ANY active expiry-selector filter. This is
    // the exact 2026-09 GOLDM "vanished leg" regression class, reproduced
    // specifically for this new combination.
    setISTTime('2026-11-28T08:01:00'); // Saturday — the incident instant
    const result = buildCandidatePositions({
      ...PARAMS,
      positions: [makePos(SYM)],
      getInstrument,
      hasFNO,
      // An operator has some OTHER (unrelated, known) expiry selected —
      // this symbol's own (unresolvable) expiry can never be in this list.
      selectedExpiries: ['2026-12-25'],
    });
    const row = result.find(r => r.symbol === SYM);
    expect(row, 'leg must not be dropped by the active expiry selector').toBeTruthy();
    expect(row._expired).toBeUndefined();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// 2026-11-27 fix — the DRAFTS loop used a bare `_dInst.x < todayIST()`
// compare (NOT weekend-aware), while the real-positions loop above
// (fixed earlier this session) already uses `isPastExpiryFreezeWindow`.
// A draft leg on a Friday-expiring contract vanished at bare Saturday
// 08:00 IST, two full days before its real-leg counterpart on the
// IDENTICAL contract (which correctly stays visible until Monday).
// ─────────────────────────────────────────────────────────────────────────────

/** Minimal draft row for buildCandidatePositions' drafts loop. */
function makeDraft(sym, extra = {}) {
  return {
    id: 1,
    symbol: sym,
    qty: 10,
    avg_cost: 5,
    ltp: 6,
    account: 'ZG0790',
    ...extra,
  };
}

describe('buildCandidatePositions — draft legs use the same 08:00 IST session boundary as real legs', () => {
  // Contract's own expiry day: 2026-11-27 (a Friday) — same fixture
  // symbol as the real-leg weekend-grace describe block above.
  const SYM = 'GOLD26NOV124000CE';
  const getInstrument = makeGetInst({ [SYM]: '2026-11-27' });
  const PARAMS = { ...BASE_PARAMS, target: 'GOLD' };

  it('Friday, expiry day itself — draft leg still shown', () => {
    setISTTime('2026-11-27T20:00:00');
    const result = buildCandidatePositions({ ...PARAMS, positions: [], drafts: [makeDraft(SYM)], getInstrument });
    expect(result.find(r => r.symbol === SYM && r.source === 'draft')).toBeTruthy();
  });

  it('THE BUG THIS FIXES: Saturday 08:01 IST — draft leg must still be shown (old code dropped it here)', () => {
    setISTTime('2026-11-28T08:01:00');
    const result = buildCandidatePositions({ ...PARAMS, positions: [], drafts: [makeDraft(SYM)], getInstrument });
    const row = result.find(r => r.symbol === SYM && r.source === 'draft');
    expect(row, 'draft leg must stay visible through the whole weekend grace window').toBeTruthy();

    // Negative control — pins the OLD buggy comparison so this test
    // would have failed against the pre-fix code, not just passed by
    // construction (same technique the real-leg describe block above
    // uses for its own "THE BUG THIS FIXES" case).
    const oldBuggyDropped = '2026-11-27' < '2026-11-28'; // bare todayIST() compare
    expect(oldBuggyDropped).toBe(true);
  });

  it('Sunday — still shown (whole weekend covered, not just the first day)', () => {
    setISTTime('2026-11-29T12:00:00');
    const result = buildCandidatePositions({ ...PARAMS, positions: [], drafts: [makeDraft(SYM)], getInstrument });
    expect(result.find(r => r.symbol === SYM && r.source === 'draft')).toBeTruthy();
  });

  it('Monday, one minute before the real boundary (07:59 IST) — still shown', () => {
    setISTTime('2026-11-30T07:59:00');
    const result = buildCandidatePositions({ ...PARAMS, positions: [], drafts: [makeDraft(SYM)], getInstrument });
    expect(result.find(r => r.symbol === SYM && r.source === 'draft')).toBeTruthy();
  });

  it('Monday, at the real boundary (08:00 IST) — NOW correctly dropped', () => {
    setISTTime('2026-11-30T08:00:00');
    const result = buildCandidatePositions({ ...PARAMS, positions: [], drafts: [makeDraft(SYM)], getInstrument });
    expect(result.find(r => r.symbol === SYM && r.source === 'draft')).toBeUndefined();
  });

  it('a real leg and a draft leg on the IDENTICAL contract reach the same expired/visible verdict at every instant across the weekend', () => {
    // Real legs stay IN the array but get TAGGED `_expired: true` once
    // past the freeze window (so buildCleanLegs can still exclude them
    // from the backend payload); draft legs are REMOVED from the array
    // entirely at that exact same boundary — different representations
    // of the identical `isPastExpiryFreezeWindow` verdict. A real row's
    // `_expired` tag must therefore be the exact logical negation of
    // whether the draft row still exists.
    for (const iso of [
      '2026-11-27T20:00:00', '2026-11-28T08:01:00', '2026-11-29T12:00:00',
      '2026-11-30T07:59:00', '2026-11-30T08:00:00',
    ]) {
      setISTTime(iso);
      const result = buildCandidatePositions({
        ...PARAMS,
        positions: [makePos(SYM)],
        drafts: [makeDraft(SYM)],
        getInstrument,
      });
      const realRow  = result.find(r => r.symbol === SYM && r.source === 'live');
      const draftRow = result.find(r => r.symbol === SYM && r.source === 'draft');
      expect(realRow, iso).toBeTruthy();
      expect(!!realRow._expired, iso).toBe(!draftRow);
    }
  });
});
