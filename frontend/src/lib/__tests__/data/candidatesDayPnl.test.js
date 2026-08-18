import { describe, it, expect } from 'vitest';

/**
 * Pure guard logic extracted from `candidatesDayPnl` in
 * frontend/src/routes/(algo)/admin/derivatives/+page.svelte.
 *
 * `dayPnlUsedLivePath` mirrors the `dayPnlUsedLive` boolean that gates
 * whether `_dayPnlForLeg` already consumed the SSE ltp for overnight legs.
 * When it returns true, the `delta = (liveLtp - pollLtp) * qty` term is
 * skipped to prevent the live-tick move from being counted twice.
 *
 * Condition mirrors `_dayPnlForLeg` line 2035:
 *   oq !== 0 && legLiveLtp != null && Number(legLiveLtp) > 0 && close > 0 && qty !== 0
 */
function dayPnlUsedLivePath({ oq, legLiveLtp, close, qty }) {
  return oq !== 0 && legLiveLtp != null && Number(legLiveLtp) > 0 && close > 0 && qty !== 0;
}

describe('candidatesDayPnl delta gating', () => {
  it('returns true (no delta) when overnight leg has valid SSE ltp', () => {
    expect(dayPnlUsedLivePath({ oq: 50, legLiveLtp: 120.5, close: 115, qty: 50 })).toBe(true);
  });

  it('returns false (add delta) when oq=0 (intraday leg)', () => {
    expect(dayPnlUsedLivePath({ oq: 0, legLiveLtp: 120.5, close: 115, qty: 50 })).toBe(false);
  });

  it('returns false (add delta) when legLiveLtp is null (no SSE snapshot)', () => {
    expect(dayPnlUsedLivePath({ oq: 50, legLiveLtp: null, close: 115, qty: 50 })).toBe(false);
  });

  it('returns false (add delta) when legLiveLtp is undefined (no SSE snapshot)', () => {
    expect(dayPnlUsedLivePath({ oq: 50, legLiveLtp: undefined, close: 115, qty: 50 })).toBe(false);
  });

  it('returns false (add delta) when legLiveLtp is 0 (stale tick)', () => {
    expect(dayPnlUsedLivePath({ oq: 50, legLiveLtp: 0, close: 115, qty: 50 })).toBe(false);
  });

  it('returns false (add delta) when close=0 (stale close guard)', () => {
    expect(dayPnlUsedLivePath({ oq: 50, legLiveLtp: 120.5, close: 0, qty: 50 })).toBe(false);
  });

  it('returns false (add delta) when qty=0 (fully closed position)', () => {
    expect(dayPnlUsedLivePath({ oq: 50, legLiveLtp: 120.5, close: 115, qty: 0 })).toBe(false);
  });

  it('returns true for short overnight position (oq < 0)', () => {
    expect(dayPnlUsedLivePath({ oq: -50, legLiveLtp: 120.5, close: 115, qty: -50 })).toBe(true);
  });

  it('no double-count: delta contribution is 0 when dayPnlUsedLive is true', () => {
    const pollLtp = 118;
    const liveLtp = 122;
    /** @type {number} */
    const qty = 50;
    const dayPnlUsedLive = dayPnlUsedLivePath({ oq: 50, legLiveLtp: liveLtp, close: 115, qty });
    const delta = (!dayPnlUsedLive && pollLtp > 0 && liveLtp > 0 && qty !== 0)
      ? (liveLtp - pollLtp) * qty
      : 0;
    expect(dayPnlUsedLive).toBe(true);
    expect(delta).toBe(0);
  });

  it('delta is applied for intraday leg (oq=0) with valid SSE ltp', () => {
    const pollLtp = 118;
    const liveLtp = 122;
    /** @type {number} */
    const qty = 50;
    const dayPnlUsedLive = dayPnlUsedLivePath({ oq: 0, legLiveLtp: liveLtp, close: 115, qty });
    const delta = (!dayPnlUsedLive && pollLtp > 0 && liveLtp > 0 && qty !== 0)
      ? (liveLtp - pollLtp) * qty
      : 0;
    expect(dayPnlUsedLive).toBe(false);
    expect(delta).toBe((liveLtp - pollLtp) * qty);
  });
});
