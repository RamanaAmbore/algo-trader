/**
 * Tests for frontend/src/lib/debug/debugLog.js
 *
 * The module uses a module-level _ring array and exposes globalThis.__RAMBOQ_DUMP.
 * Because Vitest runs each test file in a fresh module scope, _ring starts empty
 * for this file. We toggle globalThis.__RAMBOQ_DEBUG before each test and restore
 * it afterward.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

// Capture the original value so we can restore it.
const _origDebug = globalThis.__RAMBOQ_DEBUG;

// We import AFTER setting up globalThis so the module initialisation block runs.
// Note: the module sets globalThis.__RAMBOQ_DUMP on import, which is what we test.
let debugLog;

beforeEach(async () => {
  // Reset debug flag to off before each test.
  globalThis.__RAMBOQ_DEBUG = false;
  // Dynamic import ensures the module registers __RAMBOQ_DUMP on globalThis.
  // Vitest caches modules between tests in the same file, so _ring is shared.
  // We control it via the debug flag (off = nothing pushed).
  ({ debugLog } = await import('$lib/debug/debugLog.js'));
});

afterEach(() => {
  // Restore original debug flag.
  globalThis.__RAMBOQ_DEBUG = _origDebug;
});

// Helper: parse dump JSON.
function dumpAll() {
  return JSON.parse(globalThis.__RAMBOQ_DUMP());
}
function dumpNs(ns) {
  return JSON.parse(globalThis.__RAMBOQ_DUMP(ns));
}

describe('debugLog — off (default)', () => {
  it('does not push entries when __RAMBOQ_DEBUG is false', () => {
    const before = dumpAll().length;
    globalThis.__RAMBOQ_DEBUG = false;
    debugLog('sse', 'connect', { url: '/api/quotes/stream' });
    const after = dumpAll().length;
    expect(after).toBe(before);
  });

  it('does not push entries when __RAMBOQ_DEBUG is undefined', () => {
    const before = dumpAll().length;
    globalThis.__RAMBOQ_DEBUG = undefined;
    debugLog('payoff:spot', 'resolved', { tier: '1a-anchor', value: 24000 });
    const after = dumpAll().length;
    expect(after).toBe(before);
  });
});

describe('debugLog — all namespaces (true)', () => {
  it('adds an entry to the ring buffer when __RAMBOQ_DEBUG = true', () => {
    globalThis.__RAMBOQ_DEBUG = true;
    const before = dumpAll().length;
    debugLog('sse', 'snapshot', { count: 5 });
    const after = dumpAll();
    expect(after.length).toBe(before + 1);
    const last = after[after.length - 1];
    expect(last.ns).toBe('sse');
    expect(last.event).toBe('snapshot');
    expect(last.data).toEqual({ count: 5 });
    expect(typeof last.ts).toBe('number');
  });

  it('adds entries for any namespace when debug is true', () => {
    globalThis.__RAMBOQ_DEBUG = true;
    const before = dumpAll().length;
    debugLog('payoff:spot', 'resolved', { tier: '4-bq', value: 24000 });
    debugLog('navstrip:expiry', 'computed', { total: 5000 });
    const after = dumpAll();
    expect(after.length).toBe(before + 2);
  });
});

describe('debugLog — namespace prefix filter', () => {
  it("captures 'payoff:spot' when filter is 'payoff'", () => {
    globalThis.__RAMBOQ_DEBUG = 'payoff';
    const before = dumpAll().length;
    debugLog('payoff:spot', 'resolved', { tier: '1a-anchor', value: 24100 });
    const after = dumpAll();
    expect(after.length).toBe(before + 1);
    expect(after[after.length - 1].ns).toBe('payoff:spot');
  });

  it("does NOT capture 'sse' when filter is 'payoff'", () => {
    globalThis.__RAMBOQ_DEBUG = 'payoff';
    const before = dumpAll().length;
    debugLog('sse', 'connect', { url: '/api/quotes/stream' });
    const after = dumpAll();
    expect(after.length).toBe(before);
  });

  it("captures 'payoff:bq' when filter is 'payoff'", () => {
    globalThis.__RAMBOQ_DEBUG = 'payoff';
    const before = dumpAll().length;
    debugLog('payoff:bq', 'request', { keys: ['NSE:NIFTY 50'] });
    const after = dumpAll();
    expect(after.length).toBe(before + 1);
  });

  it("does NOT capture 'navstrip:expiry' when filter is 'payoff'", () => {
    globalThis.__RAMBOQ_DEBUG = 'payoff';
    const before = dumpAll().length;
    debugLog('navstrip:expiry', 'computed', { total: 1000 });
    const after = dumpAll();
    expect(after.length).toBe(before);
  });
});

describe('debugLog — ring buffer cap at 500', () => {
  it('keeps at most 500 entries when 501 are pushed', () => {
    globalThis.__RAMBOQ_DEBUG = true;
    // Push enough to overflow the cap (ring may already have some entries
    // from prior tests, so push until total would exceed 500 + buffer size).
    for (let i = 0; i < 600; i++) {
      debugLog('test:cap', 'entry', { i });
    }
    const all = dumpAll();
    expect(all.length).toBeLessThanOrEqual(500);
  });
});

describe('__RAMBOQ_DUMP namespace filter', () => {
  it('filters entries by exact namespace', () => {
    globalThis.__RAMBOQ_DEBUG = true;
    debugLog('sse', 'connect', {});
    debugLog('payoff:spot', 'resolved', { tier: '4-bq', value: 24000 });
    debugLog('payoff:bq', 'request', { keys: [] });

    const sseEntries = dumpNs('sse');
    expect(sseEntries.every(e => e.ns === 'sse' || e.ns.startsWith('sse:'))).toBe(true);

    const payoffEntries = dumpNs('payoff');
    expect(payoffEntries.every(e => e.ns === 'payoff' || e.ns.startsWith('payoff:'))).toBe(true);
    // Both 'payoff:spot' and 'payoff:bq' should be included.
    const nses = payoffEntries.map(e => e.ns);
    expect(nses).toContain('payoff:spot');
    expect(nses).toContain('payoff:bq');
  });

  it('returns all entries when no namespace is passed to __RAMBOQ_DUMP', () => {
    globalThis.__RAMBOQ_DEBUG = true;
    debugLog('sse', 'snapshot', { count: 3 });
    debugLog('navstrip:spot', 'request', { keys: [] });
    const all = dumpAll();
    // At least these two new entries are present.
    expect(all.length).toBeGreaterThanOrEqual(2);
  });

  it('excludes non-matching namespace entries from filtered dump', () => {
    globalThis.__RAMBOQ_DEBUG = true;
    debugLog('navstrip:expiry', 'computed', { total: 99 });
    const payoffDump = dumpNs('payoff');
    const hasNavstrip = payoffDump.some(e => e.ns.startsWith('navstrip'));
    expect(hasNavstrip).toBe(false);
  });
});
