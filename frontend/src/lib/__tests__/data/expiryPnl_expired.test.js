/**
 * expiryPnl_expired.test.js — Vitest tests for the expired-but-held leg
 * valuation fix (2026-09 GOLD/GOLDM chart-vs-Snapshot divergence).
 *
 * Background: a root whose ENTIRE F&O book is expired-but-held (all legs
 * `_expired`-tagged) previously showed Exp P&L = 0 on the derivatives
 * Payoff chart while Snapshot showed a real (but ALSO wrong — valued
 * against a rolled-forward root spot via findNearestFuture) non-zero
 * number. This file exercises the shared fix: isExpiredHeldContract
 * (detection), expiredLegFrozenPnl / expiredPositionExpPnl /
 * expiredPositionExpPnlPieces (frozen valuation, no live-spot dependency).
 *
 * 2026-09 audit follow-up (Defect 2): the original isExpiredHeldContract
 * treated ANY cache miss as "expired" (gated only by a since-removed
 * isInstrumentsCacheLoaded()) — this misclassified every BFO-listed
 * instrument (BFO is never fetched at all — see
 * backend/api/routes/instruments.py's _EXCHANGES tuple) as permanently
 * expired-and-frozen EVERYWHERE (NavStrip/Pulse/Snapshot/Legs TOTAL, not
 * just derivatives) — a wider blast radius than the bug being fixed.
 * Redesigned to a 3-branch decision (found in cache / missing-but-root-
 * has-other-live-F&O / missing-and-cache-not-authoritative-for-this-root)
 * — this file's tests now cover all three branches directly.
 *
 * Five quality dimensions:
 *  1. SSOT  — same functions portfolioStore.svelte.js's _posTier2 and
 *             derivatives/+page.svelte's four call-sites both import.
 *  2. Perf  — all synchronous; no I/O.
 *  3. Stale — the "discriminating" test directly reproduces the bug this
 *             fix closes (frozen value must NOT depend on a spot input).
 *  4. Reuse — isExpiredHeldContract accepts injected lookups, matching
 *             buildCandidatePositions' own DI pattern (pageLoad.js).
 *  5. UX    — BFO / cold-start / failed-exchange-download guard: a cache
 *             miss that ISN'T root-authoritative never misclassifies a
 *             live position as expired app-wide.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

// Pin todayIST so expiry-date comparisons aren't flaky across calendar days.
vi.mock('$lib/dateFormat.js', () => ({
  todayIST: () => '2026-09-27',
}));

// Controllable fake instruments cache. getInstrument mirrors the real
// module's exact-symbol lookup; hasFNO mirrors "does this ROOT have ANY
// live F&O in cache" (true when the relevant exchange was actually
// fetched and has OTHER contracts for this root, independent of whether
// THIS exact symbol is present).
const _mockCache = {
  byInst: /** @type {Record<string, {x?:string}>} */ ({}),
  fnoRoots: /** @type {Set<string>} */ (new Set()),
};
vi.mock('$lib/data/instruments.js', () => ({
  getInstrument: (/** @type {string} */ sym) => _mockCache.byInst[sym] ?? null,
  hasFNO: (/** @type {string} */ root) => _mockCache.fnoRoots.has(root),
}));

import {
  isExpiredHeldContract,
  expiredLegFrozenPnl,
  expiredPositionExpPnl,
  expiredPositionExpPnlPieces,
} from '$lib/data/expiryPnl.js';

beforeEach(() => {
  _mockCache.byInst = {};
  _mockCache.fnoRoots = new Set();
});

describe('isExpiredHeldContract', () => {
  it('false for qty=0 (not held — nothing to classify)', () => {
    expect(isExpiredHeldContract('GOLD26FEBFUT', 0)).toBe(false);
  });

  it('AUTHORITATIVE branch: instrument exists — uses its own expiry date', () => {
    _mockCache.byInst = { GOLDM25DEC5000CE: { x: '2025-12-24' } }; // before mocked today 2026-09-27
    expect(isExpiredHeldContract('GOLDM25DEC5000CE', 1)).toBe(true);
  });

  it('AUTHORITATIVE branch: instrument exists and has NOT expired yet', () => {
    _mockCache.byInst = { GOLD26DECFUT: { x: '2026-12-04' } };
    expect(isExpiredHeldContract('GOLD26DECFUT', 1)).toBe(false);
  });

  it('GOLD/GOLDM regression case — missing exact contract, but root has other live F&O (root-authoritative) → true', () => {
    // The exact reported bug: GOLDM26SEPFUT already expired (5th of the
    // month, per Kite's real MCX calendar) and delisted from the dump,
    // but sibling GOLDM contracts (26OCT, 26NOV, ...) remain in cache —
    // hasFNO('GOLDM') is true, so the missing exact symbol is trusted as
    // expired instead of falling through to the conservative (and, for
    // this case, too-lenient) last-day-of-month symbol guess.
    _mockCache.byInst = {}; // GOLDM26SEPFUT itself not in cache (delisted)
    _mockCache.fnoRoots = new Set(['GOLDM']); // sibling contracts ARE in cache
    expect(isExpiredHeldContract('GOLDM26SEPFUT', 1)).toBe(true);
  });

  it('BFO NOT-AUTHORITATIVE case — missing exact contract, root has NO live F&O at all (BFO never fetched) → false for a current-month symbol', () => {
    // SENSEX options trade on BFO, which backend/api/routes/instruments.py
    // never fetches — hasFNO('SENSEX') is false (no BFO/any-exchange
    // entries at all for this root), so the cache is NOT authoritative;
    // falls back to the conservative symbol guess. "26SEP" is the mocked
    // CURRENT month (today = 2026-09-27) — last-day-of-month (Sep 30) is
    // NOT before today, so this must NOT be misclassified as expired.
    _mockCache.byInst = {};
    _mockCache.fnoRoots = new Set(); // nothing for SENSEX at all
    expect(isExpiredHeldContract('SENSEX26SEP82000CE', 1)).toBe(false);
  });

  it('NOT-AUTHORITATIVE + strictly-past month guess → true (cold cache / failed exchange download, but symbol month has clearly passed)', () => {
    _mockCache.byInst = {};
    _mockCache.fnoRoots = new Set(); // cache has nothing for this root
    // "26FEB" (Feb 2026) is strictly before the mocked today (2026-09-27)
    // — the whole month has elapsed, so even the conservative last-day-
    // of-month guess correctly resolves to expired.
    expect(isExpiredHeldContract('NIFTY26FEBFUT', 75)).toBe(true);
  });

  it('accepts injected lookup functions (parameterized DI, matches buildCandidatePositions\' own pattern)', () => {
    const fakeGetInstrument = vi.fn(() => null);
    const fakeHasFNO = vi.fn(() => true);
    expect(isExpiredHeldContract('GOLDM26SEPFUT', 1, fakeGetInstrument, fakeHasFNO)).toBe(true);
    expect(fakeGetInstrument).toHaveBeenCalledWith('GOLDM26SEPFUT');
    expect(fakeHasFNO).toHaveBeenCalledWith('GOLDM');
  });
});

describe('expiredLegFrozenPnl — frozen valuation basis', () => {
  it('uses currentTotalProfit (realised+unrealised) in preference to raw pnl', () => {
    const leg = { realised: 100, unrealised: 400, pnl: 999 };
    expect(expiredLegFrozenPnl(leg)).toBe(500);
  });

  it('falls back to pnl only when both realised and unrealised are exactly zero/absent', () => {
    const leg = { pnl: 1234 };
    expect(expiredLegFrozenPnl(leg)).toBe(1234);
  });
});

describe('expiredPositionExpPnl / expiredPositionExpPnlPieces — no live-spot dependency', () => {
  it('DISCRIMINATING: the frozen value has no spot parameter to drift through — this is the actual GOLD/GOLDM bug the fix closes', () => {
    // The whole point of this fix: expiredPositionExpPnl/expiredLegFrozenPnl
    // take NO spot argument at all, unlike positionExpPnl(p, kind, anchor) —
    // so there is no channel through which a rolled-forward root spot
    // (findNearestFuture resolving to the NEXT month's live, still-moving
    // future) can leak in and make an expired-but-held leg's value drift.
    const row = {
      symbol: 'GOLDM25DEC5000CE', account: 'ZG0790', quantity: 1,
      overnight_quantity: 1, day_buy_quantity: 0, day_sell_quantity: 0,
      average_price: 4800, last_price: 5200, pnl: 400, realised: 0, unrealised: 400,
    };
    const first = expiredPositionExpPnl(row);
    // Same row, called again — nothing in this function's signature could
    // even accept a "the root spot just changed" signal.
    const second = expiredPositionExpPnl(row);
    expect(second).toBe(first);
    expect(first).toBe(400);
  });

  it('a fully-flat expired row (qty=0) still resolves via the realised/pnl fallback, not null', () => {
    const row = {
      symbol: 'GOLD25DECFUT', account: 'ZG0790', quantity: 0,
      overnight_quantity: 0, day_buy_quantity: 0, day_sell_quantity: 0,
      average_price: 70000, last_price: 71500, pnl: 1500, realised: 1500, unrealised: 0,
    };
    expect(expiredPositionExpPnl(row)).toBe(1500);
  });

  it('expiredPositionExpPnlPieces mirrors positionExpPnlPieces\' shape — one entry per splitClosedReopened piece', () => {
    const row = {
      symbol: 'GOLD25DECFUT', account: 'ZG0790', quantity: 1,
      overnight_quantity: 1, day_buy_quantity: 0, day_sell_quantity: 0,
      average_price: 70000, last_price: 71500, pnl: 1500, realised: 0, unrealised: 1500,
    };
    const pieces = expiredPositionExpPnlPieces(row);
    expect(pieces).toEqual([1500]);
  });
});
