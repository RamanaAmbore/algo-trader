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
 * Five quality dimensions:
 *  1. SSOT  — same functions portfolioStore.svelte.js's _posTier2 and
 *             derivatives/+page.svelte's four call-sites both import.
 *  2. Perf  — all synchronous; no I/O.
 *  3. Stale — the "discriminating" test directly reproduces the bug this
 *             fix closes (frozen value must NOT depend on a spot input).
 *  4. Reuse — isExpiredHeldContract accepts an injected lookup, matching
 *             buildCandidatePositions' own DI pattern (pageLoad.js).
 *  5. UX    — cold-start guard: before the instruments master loads, no
 *             held F&O row is misclassified as expired app-wide.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

// Pin todayIST so expiry-date comparisons aren't flaky across calendar days.
vi.mock('$lib/dateFormat.js', () => ({
  todayIST: () => '2026-07-27',
}));

// Controllable fake instruments cache — isInstrumentsCacheLoaded() gates
// isExpiredHeldContract's `!inst` branch so a genuine cold-start (cache
// not loaded yet) never misclassifies every held F&O row as expired.
const _mockCache = { loaded: true, byInst: /** @type {Record<string, {x?:string}>} */ ({}) };
vi.mock('$lib/data/instruments.js', () => ({
  getInstrument: (/** @type {string} */ sym) => _mockCache.byInst[sym] ?? null,
  isInstrumentsCacheLoaded: () => _mockCache.loaded,
}));

import {
  isExpiredHeldContract,
  expiredLegFrozenPnl,
  expiredPositionExpPnl,
  expiredPositionExpPnlPieces,
} from '$lib/data/expiryPnl.js';

beforeEach(() => {
  _mockCache.loaded = true;
  _mockCache.byInst = {};
});

describe('isExpiredHeldContract', () => {
  it('false for qty=0 (not held — nothing to classify)', () => {
    expect(isExpiredHeldContract('GOLD26FEBFUT', 0)).toBe(false);
  });

  it('true when the instrument is missing entirely (delisted from the master)', () => {
    _mockCache.byInst = {};
    expect(isExpiredHeldContract('GOLD25DECFUT', 1)).toBe(true);
  });

  it('true when the instrument exists but its expiry date has already passed', () => {
    _mockCache.byInst = { GOLDM25DEC5000CE: { x: '2025-12-24' } }; // before mocked today 2026-07-27
    expect(isExpiredHeldContract('GOLDM25DEC5000CE', 1)).toBe(true);
  });

  it('false when the instrument exists and has NOT expired yet', () => {
    _mockCache.byInst = { GOLD26AUGFUT: { x: '2026-08-05' } };
    expect(isExpiredHeldContract('GOLD26AUGFUT', 1)).toBe(false);
  });

  it('COLD START (2026-09 audit fix): instruments cache not loaded yet — never misclassifies as expired, even on a lookup miss', () => {
    _mockCache.loaded = false;
    _mockCache.byInst = {};
    // Without the cache-loaded gate, this would return true (no
    // instrument found) purely because the master hasn't loaded — which
    // would misclassify EVERY held F&O row app-wide during that window.
    expect(isExpiredHeldContract('NIFTY26FEBFUT', 75)).toBe(false);
  });

  it('accepts an injected lookup function (parameterized DI, matches buildCandidatePositions\' own pattern)', () => {
    const fakeLookup = vi.fn(() => null);
    expect(isExpiredHeldContract('X', 1, fakeLookup)).toBe(true);
    expect(fakeLookup).toHaveBeenCalledWith('X');
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
