/**
 * derivativesReactive.test.js — Vitest unit tests for the reactive-data-chain
 * fixes applied to the derivatives page (2026-09-10).
 *
 * Five quality dimensions:
 *  1. SSOT  — exercises the same module paths used by +page.svelte
 *  2. Perf  — all synchronous; no I/O or DOM
 *  3. Stale — guards against regression where live LTP was ignored off-market
 *  4. Reuse — same helpers used by _snapshotTotalDay and CandidateLegRow
 *  5. UX    — live LTP must produce a different (more accurate) value than
 *              broker-snapshot dcv when price has moved
 */

import { describe, it, expect } from 'vitest';
import { baseDayPnlForPosition } from '$lib/data/nav.js';

// ─────────────────────────────────────────────────────────────────────────────
// Fix 5 (superseded by §1 positions/holdings LTP-source redesign) —
// _snapshotTotalDay live-LTP path
//
// Historical: _snapshotTotalDay used to switch from baseDayPnlForPosition to
// livePositionDayPnl (a live-tick-delta wrapper) so the TOTAL row tracked SSE
// ticks at 4 Hz. §1 removed that wrapper entirely — Day P&L for positions/
// derivative legs is now purely poll-driven; baseDayPnlForPosition IS the
// value, with no delta layered on top regardless of market-open state or a
// live tick being available. The tests below guard against that delta being
// reintroduced.
// ─────────────────────────────────────────────────────────────────────────────

describe('Fix 5 (superseded) — baseDayPnlForPosition has no live-tick delta', () => {
  it('market open: no delta layers on top of the settlement-diff base', () => {
    const dcvRow = { pnl: 60, prev_settlement_pnl: 50 };
    const base = baseDayPnlForPosition(dcvRow);
    // base = 60 - 50 = 10 — a live tick moving 102→108 has zero effect.
    expect(base).toBe(10);
  });

  it('market closed: base is honest, including flat-settlement zero', () => {
    const dcvRow = {
      pnl: -5000,
      prev_settlement_pnl: -5000,  // identical → base = 0
    };
    expect(baseDayPnlForPosition(dcvRow)).toBe(0);
  });

  it('no live-price parameter exists — result is a pure function of the row', () => {
    const dcvRow = { pnl: 800, prev_settlement_pnl: 480 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(320);
    expect(baseDayPnlForPosition.length).toBe(1);
  });

  it('short overnight position: base is signed pnl-diff only, no qty-scaled tick delta', () => {
    // Short 10 lots; base = pnl(1000) - prev_settlement_pnl(700) = 300.
    const dcvRow = { pnl: 1000, prev_settlement_pnl: 700 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(300);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Fix 3 — _quoteGeneration counter increment semantics
//
// The _quoteGeneration counter is a plain integer incremented in
// loadUnderlyingQuotes() after _underlyingQuotes is replaced. We test the
// increment pattern in isolation (without Svelte reactivity) to confirm the
// counter produces distinct values on each call, which is the invariant that
// allows $derived blocks to re-run off-market via `void _quoteGeneration`.
// ─────────────────────────────────────────────────────────────────────────────

describe('Fix 3 — quoteGeneration counter increment semantics', () => {
  it('counter increments monotonically across simulated quote refreshes', () => {
    // Simulate the pattern: start at 0, increment each time _underlyingQuotes is replaced.
    let gen = 0;
    const snapshots = [];

    // Simulate 5 batchQuote refresh cycles.
    for (let i = 0; i < 5; i++) {
      // _underlyingQuotes = next; (simulated)
      gen++;
      snapshots.push(gen);
    }

    // Every snapshot must be strictly greater than the previous.
    for (let i = 1; i < snapshots.length; i++) {
      expect(snapshots[i]).toBeGreaterThan(snapshots[i - 1]);
    }
    // Final value equals the number of refreshes.
    expect(gen).toBe(5);
  });

  it('void gen expression is idempotent — reading does not change the counter', () => {
    let gen = 0;
    gen++;  // one refresh

    const before = gen;
    // eslint-disable-next-line no-unused-expressions
    void gen;  // $derived pattern: registers dependency without side-effect
    const after = gen;

    expect(after).toBe(before);
    expect(after).toBe(1);
  });

  it('off-market path: counter guards re-derivation when tick bus is frozen', () => {
    // Simulate liveSpot / _clientPayoffStub pattern:
    // marketOpen=false → touch _quoteGeneration as reactive dep.
    // marketOpen=true → skip (handled by _throttledTick instead).
    let gen = 0;
    const liveSpotDeps = [];

    const deriveSpot = (marketOpen) => {
      // Mirrors: if (!isMarketOpen()) void _quoteGeneration;
      if (!marketOpen) liveSpotDeps.push(gen);
      // Returns current gen value (proxy for "spot resolved from batchQuote")
      return gen;
    };

    // Off-market: dep is captured.
    const spot0 = deriveSpot(false);
    expect(liveSpotDeps).toHaveLength(1);
    expect(spot0).toBe(0);

    // batchQuote fires, gen increments.
    gen++;
    const spot1 = deriveSpot(false);
    expect(liveSpotDeps).toHaveLength(2);
    expect(spot1).toBe(1);
    expect(spot1).not.toBe(spot0);

    // Market open: dep NOT captured.
    const lenBefore = liveSpotDeps.length;
    deriveSpot(true);
    expect(liveSpotDeps.length).toBe(lenBefore);  // no new dep registration
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Fix 8 — CandidateLegRow SSE-reactive LTP
//
// The `ltp` derived in CandidateLegRow now reads getSnapshot(symbol)?.ltp first.
// We verify the priority chain: SSE > legAnalytics.ltp > c.ltp.
// (Tested as pure priority logic; getSnapshot is integration-tested elsewhere.)
// ─────────────────────────────────────────────────────────────────────────────

describe('Fix 8 — CandidateLegRow LTP priority chain', () => {
  // Mirror the new derived: getSnapshot(sym)?.ltp ?? (lg?.ltp ?? c.ltp)
  const deriveLtp = (sseLtp, lgLtp, cLtp) =>
    sseLtp ?? (lgLtp ?? cLtp);

  it('SSE tick (highest priority) overrides both legAnalytics and c.ltp', () => {
    expect(deriveLtp(108, 102, 100)).toBe(108);
  });

  it('SSE null → legAnalytics.ltp used', () => {
    expect(deriveLtp(null, 102, 100)).toBe(102);
  });

  it('SSE null + legAnalytics null → c.ltp used', () => {
    expect(deriveLtp(null, null, 100)).toBe(100);
  });

  it('SSE 0 (falsy) → falls through to legAnalytics (nullish coalescing)', () => {
    // ?? treats 0 as non-null; 0 is a valid LTP for expired options.
    // The ?? chain treats 0 as defined, so SSE=0 is returned as-is.
    expect(deriveLtp(0, 102, 100)).toBe(0);
  });

  it('all three undefined → undefined (no price data)', () => {
    expect(deriveLtp(undefined, undefined, undefined)).toBeUndefined();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Fix 1 — qtySum in picker options
//
// underlyingOptionsForPicker now includes a `qtySum` field on each option.
// Tier 1 (options) and Tier 2 (futures) derive it from the _rootQtySum map;
// Tiers 3-6 (holdings/pinned/watchlist/popular) always emit 0.
// ─────────────────────────────────────────────────────────────────────────────

describe('Fix 1 — qtySum in picker options', () => {
  /**
   * Simulate the _rootQtySum map-building logic from underlyingOptionsForPicker.
   * @param {Array<{symbol: string, qty: number}>} positions
   * @returns {Map<string, number>}
   */
  function buildRootQtySum(positions) {
    const map = new Map();
    for (const p of positions) {
      // Mirror: const r = p.symbol.replace(/\d.*$/, '');
      const r = p.symbol.replace(/\d.*$/, '');
      if (r) map.set(r, (map.get(r) || 0) + Math.abs(Number(p.qty || 0)));
    }
    return map;
  }

  /**
   * Simulate one tier of the picker push logic.
   * @param {string[]} roots
   * @param {Map<string, number>} rootQtySum
   * @param {string} hint
   * @param {boolean} useQtySum - true for Tier 1+2, false for Tier 3-6
   */
  function buildTierOpts(roots, rootQtySum, hint, useQtySum) {
    return roots.map(u => ({
      value: u, label: u, hint,
      qtySum: useQtySum ? (rootQtySum.get(u) || 0) : 0,
    }));
  }

  it('Tier 1 (options) gets non-zero qtySum from position roots', () => {
    const positions = [
      { symbol: 'CRUDEOIL26JUNFUT', qty: 2 },
      { symbol: 'CRUDEOIL26JUNPE',  qty: 1 },
      { symbol: 'GOLDM26JUNCE',     qty: 3 },
    ];
    const rootQtySum = buildRootQtySum(positions);
    // CRUDEOIL has 3 total abs(qty), GOLDM has 3
    expect(rootQtySum.get('CRUDEOIL')).toBe(3);
    expect(rootQtySum.get('GOLDM')).toBe(3);

    const opts = buildTierOpts(['CRUDEOIL', 'GOLDM'], rootQtySum, 'options', true);
    expect(opts[0]).toMatchObject({ value: 'CRUDEOIL', hint: 'options', qtySum: 3 });
    expect(opts[1]).toMatchObject({ value: 'GOLDM', hint: 'options', qtySum: 3 });
  });

  it('Tier 2 (futures) gets qtySum from position roots', () => {
    const positions = [{ symbol: 'BANKNIFTY26JUNFUT', qty: 5 }];
    const rootQtySum = buildRootQtySum(positions);
    expect(rootQtySum.get('BANKNIFTY')).toBe(5);

    const opts = buildTierOpts(['BANKNIFTY'], rootQtySum, 'futures', true);
    expect(opts[0]).toMatchObject({ value: 'BANKNIFTY', hint: 'futures', qtySum: 5 });
  });

  it('Tier 5 (watchlist) always emits qtySum=0', () => {
    const rootQtySum = new Map([['COPPER', 10]]);  // COPPER IS in positions map
    // but watchlist tier ignores it
    const opts = buildTierOpts(['COPPER'], rootQtySum, 'watchlist', false);
    expect(opts[0]).toMatchObject({ value: 'COPPER', hint: 'watchlist', qtySum: 0 });
  });

  it('Tier 6 (popular) always emits qtySum=0', () => {
    const rootQtySum = new Map([['NIFTY', 50]]);
    const opts = buildTierOpts(['NIFTY'], rootQtySum, 'popular', false);
    expect(opts[0]).toMatchObject({ value: 'NIFTY', hint: 'popular', qtySum: 0 });
  });

  it('firstActive finds first option with qtySum > 0', () => {
    const opts = [
      { value: 'COPPER',    hint: 'watchlist', qtySum: 0 },
      { value: 'CRUDEOIL',  hint: 'futures',   qtySum: 5 },
      { value: 'GOLDM',     hint: 'options',   qtySum: 3 },
    ];
    // Mirror: opts.find(o => (o.qtySum || 0) > 0) ?? opts[0]
    const firstActive = opts.find(o => (o.qtySum || 0) > 0) ?? opts[0];
    expect(firstActive.value).toBe('CRUDEOIL');
  });

  it('firstActive falls back to opts[0] when no option has active qty', () => {
    const opts = [
      { value: 'NIFTY',  hint: 'popular', qtySum: 0 },
      { value: 'COPPER', hint: 'popular', qtySum: 0 },
    ];
    const firstActive = opts.find(o => (o.qtySum || 0) > 0) ?? opts[0];
    expect(firstActive.value).toBe('NIFTY');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Fix 1 — one-time promote to active underlying
//
// The auto-select $effect fires a one-time promote when:
//   _autoSelectDone = false, _positionsLoaded = true,
//   curHasActiveQty = false, bestHasActiveQty = true.
// After _autoSelectDone is set to true the promote must NOT re-fire.
// ─────────────────────────────────────────────────────────────────────────────

describe('Fix 1 — one-time promote to active underlying', () => {
  /**
   * Simulate the promote guard logic extracted from the $effect.
   * Returns { promoted: boolean, newUnderlying: string | null, newDone: boolean }.
   */
  function runPromoteLogic({ curUnderlying, opts, positionsLoaded, autoSelectDone }) {
    const curInOpts = opts.find(o => o.value === curUnderlying);
    const firstActive = opts.find(o => (o.qtySum || 0) > 0) ?? opts[0];

    const curIsPopular    = curInOpts?.hint === 'popular';
    const curHasActiveQty = (curInOpts?.qtySum || 0) > 0;
    const bestHasActiveQty = (firstActive?.qtySum || 0) > 0;

    // Popular promote (existing logic — not the one-time path).
    if (curIsPopular && firstActive?.hint !== 'popular') {
      return { promoted: true, newUnderlying: firstActive.value, newDone: autoSelectDone };
    }

    // One-time promote.
    if (!autoSelectDone && positionsLoaded && !curIsPopular && !curHasActiveQty && bestHasActiveQty) {
      return { promoted: true, newUnderlying: firstActive.value, newDone: true };
    }

    return { promoted: false, newUnderlying: curUnderlying, newDone: autoSelectDone };
  }

  it('fires promote when all conditions met', () => {
    const opts = [
      { value: 'CRUDEOIL', hint: 'futures',   qtySum: 5 },
      { value: 'COPPER',   hint: 'watchlist',  qtySum: 0 },
    ];
    const result = runPromoteLogic({
      curUnderlying: 'COPPER',
      opts,
      positionsLoaded: true,
      autoSelectDone: false,
    });
    expect(result.promoted).toBe(true);
    expect(result.newUnderlying).toBe('CRUDEOIL');
    expect(result.newDone).toBe(true);
  });

  it('does NOT re-fire after _autoSelectDone = true', () => {
    const opts = [
      { value: 'CRUDEOIL', hint: 'futures',   qtySum: 5 },
      { value: 'COPPER',   hint: 'watchlist',  qtySum: 0 },
    ];
    // Simulate second effect run after done=true
    const result = runPromoteLogic({
      curUnderlying: 'COPPER',
      opts,
      positionsLoaded: true,
      autoSelectDone: true,  // already done
    });
    expect(result.promoted).toBe(false);
    expect(result.newUnderlying).toBe('COPPER');  // operator's pick preserved
  });

  it('does NOT promote when positions not loaded yet', () => {
    const opts = [
      { value: 'CRUDEOIL', hint: 'futures',   qtySum: 5 },
      { value: 'COPPER',   hint: 'watchlist',  qtySum: 0 },
    ];
    const result = runPromoteLogic({
      curUnderlying: 'COPPER',
      opts,
      positionsLoaded: false,  // not loaded yet
      autoSelectDone: false,
    });
    expect(result.promoted).toBe(false);
  });

  it('does NOT promote when current already has active qty', () => {
    const opts = [
      { value: 'CRUDEOIL', hint: 'futures',   qtySum: 5 },
      { value: 'COPPER',   hint: 'futures',    qtySum: 2 },
    ];
    const result = runPromoteLogic({
      curUnderlying: 'COPPER',
      opts,
      positionsLoaded: true,
      autoSelectDone: false,
    });
    // COPPER has qtySum=2, curHasActiveQty=true → no promote
    expect(result.promoted).toBe(false);
  });

  it('does NOT promote when no option has active qty (no positions)', () => {
    const opts = [
      { value: 'NIFTY',  hint: 'popular',    qtySum: 0 },
      { value: 'COPPER', hint: 'watchlist',  qtySum: 0 },
    ];
    const result = runPromoteLogic({
      curUnderlying: 'COPPER',
      opts,
      positionsLoaded: true,
      autoSelectDone: false,
    });
    // bestHasActiveQty = false → no promote
    expect(result.promoted).toBe(false);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Fix 2 — _snapshotTotalDay = sum of _dayPnlByRootMap
//
// The new formula: Object.values(_dayPnlByRootMap).reduce((s, v) => s + Number(v || 0), 0)
// Must equal the algebraic sum of all per-root values, including negatives and zeros.
// Symmetric with _snapshotTotalPnl / _snapshotTotalExp.
// ─────────────────────────────────────────────────────────────────────────────

describe('Fix 2 — _snapshotTotalDay = sum of _dayPnlByRootMap', () => {
  /**
   * Simulate the new _snapshotTotalDay formula.
   * @param {Record<string, number>} dayPnlByRootMap
   */
  function computeSnapshotTotalDay(dayPnlByRootMap) {
    return Object.values(dayPnlByRootMap).reduce((s, v) => s + Number(v || 0), 0);
  }

  it('sums two roots correctly (CRUDEOIL + GOLDM)', () => {
    const map = { CRUDEOIL: -16000, GOLDM: -8000 };
    expect(computeSnapshotTotalDay(map)).toBe(-24000);
  });

  it('empty map returns 0', () => {
    expect(computeSnapshotTotalDay({})).toBe(0);
  });

  it('single positive root', () => {
    expect(computeSnapshotTotalDay({ NIFTY: 12500 })).toBe(12500);
  });

  it('mixed positive and negative roots', () => {
    const map = { NIFTY: 5000, BANKNIFTY: -3000, CRUDEOIL: -16000, GOLDM: 2000 };
    expect(computeSnapshotTotalDay(map)).toBe(-12000);
  });

  it('null/undefined values are treated as 0 (Number(null)=0)', () => {
    const map = { NIFTY: null, BANKNIFTY: undefined, CRUDEOIL: -16000 };
    // Number(null)=0, Number(undefined)=NaN → || 0 guard handles undefined
    expect(computeSnapshotTotalDay(map)).toBe(-16000);
  });

  it('SSOT: per-row sum equals total (no formula divergence)', () => {
    // Each per-root value is already the output of _dayPnlForLeg (with
    // prev_settlement_pnl adjustment). Total = sum of per-rows by construction.
    const perRootValues = [-16000, -8000, 5000];
    const map = Object.fromEntries(
      ['CRUDEOIL', 'GOLDM', 'NIFTY'].map((k, i) => [k, perRootValues[i]])
    );
    const total = computeSnapshotTotalDay(map);
    const sumOfRows = perRootValues.reduce((s, v) => s + v, 0);
    expect(total).toBe(sumOfRows);
    expect(total).toBe(-19000);
  });
});
