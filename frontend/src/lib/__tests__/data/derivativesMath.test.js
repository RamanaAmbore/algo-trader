/**
 * derivativesMath.test.js — Vitest tests for annotateOptionCandidates and
 * related helpers in derivativesMath.js.
 *
 * Five quality dimensions:
 *  1. SSOT  — exercises the same module path used by +page.svelte
 *  2. Perf  — all synchronous; no I/O
 *  3. Stale — guards against regressions in the qty=0 / expFilter guard
 *  4. Reuse — same helpers used by the derivatives expiry-close analysis
 *  5. UX    — zero-qty (closed) positions must not appear in expiry bands
 */

import { describe, it, expect } from 'vitest';
import { annotateOptionCandidates } from '$lib/data/derivativesMath.js';

// ─────────────────────────────────────────────────────────────────────────────
// Minimal fixture helpers
// ─────────────────────────────────────────────────────────────────────────────

/** Build a minimal instrument record for a CE/PE option. */
function makeInst(optType, strike, underlying, expiry = '2026-08-28') {
  return { t: optType, k: strike, u: underlying, x: expiry };
}

/** Build a minimal candidate row. */
function makeCand(sym, qty, extra = {}) {
  return { symbol: sym, qty, account: 'ZG0790', source: 'live', ...extra };
}

const SPOT_800 = 800;
const MCX_EMPTY = new Set();

// ─────────────────────────────────────────────────────────────────────────────
// annotateOptionCandidates — qty=0 guard
// ─────────────────────────────────────────────────────────────────────────────

describe('annotateOptionCandidates — qty=0 guard', () => {
  it('skips qty=0 rows when expFilter is empty', () => {
    const getInstrument = (sym) =>
      sym === 'NIFTY26AUG800CE' ? makeInst('CE', 800, 'NIFTY') : null;
    const candidates = [makeCand('NIFTY26AUG800CE', 0)];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: [],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument,
    });
    expect(result).toHaveLength(0);
  });

  it('skips qty=0 rows even when expFilter is non-empty (regression: Bug 2)', () => {
    // Before the fix, `qty === 0 && !expFilter.length` would pass qty=0 rows
    // through when expFilter was set (e.g. ['2026-08-28']).
    const getInstrument = (sym) =>
      sym === 'NIFTY26AUG800CE' ? makeInst('CE', 800, 'NIFTY') : null;
    const candidates = [makeCand('NIFTY26AUG800CE', 0)];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: ['2026-08-28'],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument,
    });
    expect(result).toHaveLength(0);
  });

  it('includes non-zero qty rows when expFilter is non-empty', () => {
    const getInstrument = (sym) =>
      sym === 'NIFTY26AUG800CE' ? makeInst('CE', 800, 'NIFTY') : null;
    const candidates = [makeCand('NIFTY26AUG800CE', 50)];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: ['2026-08-28'],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument,
    });
    expect(result).toHaveLength(1);
    expect(result[0]._qty).toBe(50);
  });

  it('includes non-zero qty rows when expFilter is empty', () => {
    const getInstrument = (sym) =>
      sym === 'NIFTY26AUG800PE' ? makeInst('PE', 800, 'NIFTY') : null;
    const candidates = [makeCand('NIFTY26AUG800PE', -50)];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: [],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument,
    });
    expect(result).toHaveLength(1);
    expect(result[0]._qty).toBe(-50);
  });

  it('skips draft-source rows regardless of qty', () => {
    const getInstrument = (sym) =>
      sym === 'NIFTY26AUG800CE' ? makeInst('CE', 800, 'NIFTY') : null;
    const candidates = [makeCand('NIFTY26AUG800CE', 50, { source: 'draft' })];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: ['2026-08-28'],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument,
    });
    expect(result).toHaveLength(0);
  });

  it('skips rows where getInstrument returns null', () => {
    const candidates = [makeCand('UNKNOWN24AUG800CE', 50)];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: ['2026-08-28'],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument: () => null,
    });
    expect(result).toHaveLength(0);
  });

  it('correctly mixes zero and non-zero qty rows — only non-zero passes', () => {
    const getInstrument = (sym) => {
      if (sym === 'NIFTY26AUG800CE') return makeInst('CE', 800, 'NIFTY');
      if (sym === 'NIFTY26AUG750PE') return makeInst('PE', 750, 'NIFTY');
      return null;
    };
    const candidates = [
      makeCand('NIFTY26AUG800CE', 0),   // closed — should be skipped
      makeCand('NIFTY26AUG750PE', -50), // open short — should pass
    ];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: ['2026-08-28'],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument,
    });
    expect(result).toHaveLength(1);
    expect(result[0].symbol).toBe('NIFTY26AUG750PE');
  });
});
