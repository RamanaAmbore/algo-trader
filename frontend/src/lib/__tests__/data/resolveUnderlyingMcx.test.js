/**
 * resolveUnderlyingMcx.test.js — Vitest unit tests for the MCX cold-start
 * re-derive fix (_underlyingQuoteKeys gating on instrumentsReady).
 *
 * Five quality dimensions:
 *  1. SSOT  — resolveUnderlying() is the single routing boundary for MCX
 *             commodities; both cases exercise its quoteKey output.
 *  2. Perf  — pure data, no I/O; completes in < 1 ms.
 *  3. Stale — guards that the synthetic fallback path (cold instruments)
 *             and the resolved path (warm instruments) both return the
 *             expected quoteKey — catches regressions in the fallback branch.
 *  4. Reuse — same function used by _underlyingQuoteKeys, batchQuote, and
 *             loadUnderlyingSpots — fixing the quoteKey here fixes all.
 *  5. UX    — correct quoteKey prevents batchQuote from subscribing to the
 *             synthetic "MCX:CRUDEOIL" key and silently missing live LTPs.
 */

import { describe, it, expect } from 'vitest';
import { resolveUnderlying } from '../../data/resolveUnderlying.js';

describe('resolveUnderlying — MCX CRUDEOIL quoteKey', () => {
  it('returns synthetic quoteKey when findNearestFuture returns null (instruments not loaded)', () => {
    const result = resolveUnderlying('CRUDEOIL', () => null);
    expect(result).not.toBeNull();
    expect(result.quoteKey).toBe('MCX:CRUDEOIL');
  });

  it('returns contract quoteKey when findNearestFuture returns a resolved future (instruments loaded)', () => {
    const findNearestFuture = (root) =>
      root === 'CRUDEOIL' ? { s: 'CRUDEOIL26OCTFUT', e: 'MCX' } : null;
    const result = resolveUnderlying('CRUDEOIL', findNearestFuture);
    expect(result).not.toBeNull();
    expect(result.quoteKey).toBe('MCX:CRUDEOIL26OCTFUT');
    expect(result.tradingsymbol).toBe('CRUDEOIL26OCTFUT');
    expect(result.exchange).toBe('MCX');
    expect(result.kind).toBe('fut');
  });
});
