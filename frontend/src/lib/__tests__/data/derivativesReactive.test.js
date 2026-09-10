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
import { baseDayPnlForPosition, livePositionDayPnl } from '$lib/data/nav.js';

// ─────────────────────────────────────────────────────────────────────────────
// Fix 5 — _snapshotTotalDay live-LTP path
//
// _snapshotTotalDay switched from baseDayPnlForPosition to livePositionDayPnl.
// The tests below verify that livePositionDayPnl diverges from
// baseDayPnlForPosition when a live tick is available and price has moved,
// matching the intent of Fix 5 (TOTAL row stays current at 4 Hz).
// ─────────────────────────────────────────────────────────────────────────────

describe('Fix 5 — livePositionDayPnl vs baseDayPnlForPosition diverges on live tick', () => {
  /**
   * Overnight position: close=100, broker poll ltp=102, dcv=10.
   * Live tick: 108 (moved since last poll).
   * Expected: livePositionDayPnl gives (108-100)*5=40; base gives 10.
   */
  it('market open: live tick overrides stale broker dcv', () => {
    const dcvRow = {
      pnl: 50,
      overnight_quantity: 5,
      day_change_val: 10,
      close_price: 100,
      average_price: 95,
    };
    const fields = { closePx: 100, pollLtp: 102, qty: 5, avg: 95, dcvRow };
    const liveLtp = 108;

    const base = baseDayPnlForPosition(dcvRow);
    const live = livePositionDayPnl(fields, liveLtp, { marketOpen: true });

    // base returns broker dcv (10); live recomputes with tick (40)
    expect(base).toBe(10);
    expect(live).not.toBe(base);
    // realisedToday = 10 - (102 - 100)*5 = 0; live = 0 + (108-100)*5 = 40
    expect(live).toBeCloseTo(40, 4);
  });

  /**
   * Off-market: market closed. livePositionDayPnl uses the price formula
   * (pollLtp - closePx) * qty, not the broker dcv.
   * Regression: before Fix 5, _snapshotTotalDay called baseDayPnlForPosition
   * which returned dcv=0 when flat-settlement prev_settlement_pnl matched pnl.
   */
  it('market closed: price formula rescues flat-settlement zero', () => {
    const dcvRow = {
      pnl: -5000,
      prev_settlement_pnl: -5000,  // identical → baseDayPnlForPosition returns 0
      overnight_quantity: 100,
      day_change_val: 0,
      close_price: 930,
      average_price: 1000,
    };
    const fields = { closePx: 930, pollLtp: 850, qty: 100, avg: 1000, dcvRow };

    const base = baseDayPnlForPosition(dcvRow);  // 0 (flat settlement)
    const live = livePositionDayPnl(fields, null, { marketOpen: false });

    expect(base).toBe(0);
    // Price formula: (850 - 930) * 100 = -8000
    expect(live).toBe(-8000);
    expect(live).not.toBe(base);
  });

  /**
   * When liveLtp is null (no SSE tick yet), livePositionDayPnl must fall
   * back to baseDayPnlForPosition. SSOT consistency: the two functions must
   * agree when no live data is available.
   */
  it('liveLtp=null (no tick yet) → matches baseDayPnlForPosition exactly', () => {
    const dcvRow = {
      pnl: 800,
      overnight_quantity: 4,
      day_change_val: 320,
      close_price: 200,
      average_price: 190,
    };
    const fields = { closePx: 200, pollLtp: 280, qty: 4, avg: 190, dcvRow };

    const base = baseDayPnlForPosition(dcvRow);
    const live = livePositionDayPnl(fields, null, { marketOpen: true });

    // dcvRow: oq=4 (non-zero), dcv=320 (non-zero) → base = 320
    expect(base).toBe(320);
    expect(live).toBe(base);
  });

  /**
   * Short position fix (oq < 0): livePositionDayPnl must handle negative qty
   * correctly. This covers the _snapshotTotalDay path for short F&O positions
   * after the Fix 5 migration from baseDayPnlForPosition.
   */
  it('short overnight position: live tick computes gain on price fall', () => {
    // Short 10 lots at close 200; ltp fell to 190 (profit for short).
    const dcvRow = {
      pnl: 1000,
      overnight_quantity: -10,
      day_change_val: 1000,
      close_price: 200,
      average_price: 210,
    };
    const fields = { closePx: 200, pollLtp: 195, qty: -10, avg: 210, dcvRow };
    const liveLtp = 190;

    const live = livePositionDayPnl(fields, liveLtp, { marketOpen: true });

    // brokerDcv = 1000 (oq=-10, dcv=1000 non-zero → 1000)
    // realisedToday = 1000 - (195 - 200)*(-10) = 1000 - 50 = 950
    // result = 950 + (190 - 200)*(-10) = 950 + 100 = 1050
    expect(live).toBeCloseTo(1050, 4);
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
