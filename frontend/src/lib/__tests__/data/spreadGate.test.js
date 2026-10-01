import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  evaluateLegSpread,
  resolveWingTradingsymbol,
  createSpreadGate,
  SPREAD_GATE_MAX_ERRORS,
} from '../../data/spreadGate.js';

// ─────────────────────────────────────────────────────────────────────────
// evaluateLegSpread — pure bid/ask → spread%/ok calculation
// ─────────────────────────────────────────────────────────────────────────
describe('evaluateLegSpread', () => {
  it('ltp-preferred basis (matches backend evaluate_spread) — tight spread within threshold → ok true', () => {
    const r = evaluateLegSpread({ bid: 100, ask: 102, ltp: 101, maxSpreadPct: 10 });
    expect(r.ok).toBe(true);
    expect(r.basis).toBe('ltp');
    expect(r.spread_pct).toBeCloseTo((2 / 101) * 100, 4);
  });

  it('ltp absent/non-positive → falls back to bid/ask midpoint basis', () => {
    const r = evaluateLegSpread({ bid: 95, ask: 105, ltp: 0, maxSpreadPct: 10 });
    expect(r.basis).toBe('mid');
    expect(r.spread_pct).toBeCloseTo(10, 6); // mid=100, diff=10 -> 10%
  });

  it('ltp present materially diverges spread% from the midpoint basis', () => {
    // bid/ask midpoint would be 101 → ~1.98%; a much lower ltp (thin
    // book quoting away from last trade) changes the denominator and
    // therefore the resulting spread_pct — confirms ltp truly wins.
    const mid = evaluateLegSpread({ bid: 100, ask: 102, ltp: 0, maxSpreadPct: 10 });
    const ltpBased = evaluateLegSpread({ bid: 100, ask: 102, ltp: 50, maxSpreadPct: 10 });
    expect(ltpBased.basis).toBe('ltp');
    expect(ltpBased.spread_pct).not.toBeCloseTo(mid.spread_pct, 2);
    expect(ltpBased.spread_pct).toBeCloseTo((2 / 50) * 100, 4);
  });

  it('wide spread beyond threshold → ok false', () => {
    const r = evaluateLegSpread({ bid: 100, ask: 130, ltp: 115, maxSpreadPct: 10 });
    expect(r.ok).toBe(false);
    expect(r.spread_pct).toBeGreaterThan(10);
  });

  it('spread exactly at threshold → ok true (<=, not <)', () => {
    const bid = 95, ask = 105; // mid=100, diff=10 -> 10% (no ltp supplied)
    const r = evaluateLegSpread({ bid, ask, maxSpreadPct: 10 });
    expect(r.spread_pct).toBeCloseTo(10, 6);
    expect(r.ok).toBe(true);
  });

  it('missing/zero ask → ok null, spread_pct null (unknown, not blocking)', () => {
    const r = evaluateLegSpread({ bid: 100, ask: 0, maxSpreadPct: 10 });
    expect(r.ok).toBeNull();
    expect(r.spread_pct).toBeNull();
    expect(r.basis).toBeNull();
  });

  it('crossed quote (ask < bid) → ok null, treated as invalid', () => {
    const r = evaluateLegSpread({ bid: 100, ask: 90, maxSpreadPct: 10 });
    expect(r.ok).toBeNull();
  });

  it('null/undefined bid+ask → ok null, no throw', () => {
    const r = evaluateLegSpread({ bid: null, ask: undefined, maxSpreadPct: 10 });
    expect(r.ok).toBeNull();
    expect(r.bid).toBeNull();
    expect(r.ask).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────────
// resolveWingTradingsymbol — mirrors backend _wing_symbol (CE +offset,
// PE -offset), offset=0 is a valid ATM wing.
// ─────────────────────────────────────────────────────────────────────────
describe('resolveWingTradingsymbol', () => {
  it('CE parent → wing strike is parent + offset', () => {
    expect(resolveWingTradingsymbol('NIFTY25APR22000CE', 100)).toBe('NIFTY25APR22100CE');
  });

  it('PE parent → wing strike is parent - offset', () => {
    expect(resolveWingTradingsymbol('NIFTY25APR22000PE', 100)).toBe('NIFTY25APR21900PE');
  });

  it('offset 0 → valid ATM wing (not null — offset is not-None, not truthy)', () => {
    expect(resolveWingTradingsymbol('NIFTY25APR22000CE', 0)).toBe('NIFTY25APR22000CE');
  });

  it('offset null → null (no manual offset configured)', () => {
    expect(resolveWingTradingsymbol('NIFTY25APR22000CE', null)).toBeNull();
  });

  it('unparseable parent symbol (futures, no CE/PE) → null', () => {
    expect(resolveWingTradingsymbol('NIFTY25APRFUT', 100)).toBeNull();
  });

  it('resulting strike <= 0 → null', () => {
    expect(resolveWingTradingsymbol('NIFTY25APR50PE', 100)).toBeNull();
  });

  it('non-finite offset → null, no throw', () => {
    expect(resolveWingTradingsymbol('NIFTY25APR22000CE', NaN)).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────────
// createSpreadGate — bounded poll/retry/timeout state machine
// ─────────────────────────────────────────────────────────────────────────
describe('createSpreadGate', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('passes immediately when the first check is already ok', async () => {
    const checkLegs = vi.fn(async () => ({ ok: true, legs: [] }));
    const gate = createSpreadGate({ checkLegs, pollMs: 1000, maxWaitMs: 100_000 });
    gate.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(gate.getState().phase).toBe('passed');
    expect(checkLegs).toHaveBeenCalledTimes(1);
  });

  it('loops on a wide spread and resolves once it recovers on its own', async () => {
    let call = 0;
    const checkLegs = vi.fn(async () => {
      call += 1;
      if (call < 3) {
        return { ok: false, legs: [{ label: 'X', tradingsymbol: 'X', ok: false, spread_pct: 20, bid: 1, ask: 2, maxSpreadPct: 10 }] };
      }
      return { ok: true, legs: [] };
    });
    const gate = createSpreadGate({ checkLegs, pollMs: 50, maxWaitMs: 100_000 });
    gate.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(gate.getState().phase).toBe('wide');
    await vi.advanceTimersByTimeAsync(50);
    expect(gate.getState().phase).toBe('wide');
    await vi.advanceTimersByTimeAsync(50);
    expect(gate.getState().phase).toBe('passed');
    expect(checkLegs).toHaveBeenCalledTimes(3);
  });

  it('requests never overlap — a setTimeout chain, not setInterval', async () => {
    let inFlight = 0;
    let maxConcurrent = 0;
    const checkLegs = vi.fn(async () => {
      inFlight += 1;
      maxConcurrent = Math.max(maxConcurrent, inFlight);
      await new Promise((r) => setTimeout(r, 5));
      inFlight -= 1;
      return { ok: false, legs: [] };
    });
    const gate = createSpreadGate({ checkLegs, pollMs: 10, maxWaitMs: 200 });
    gate.start();
    await vi.advanceTimersByTimeAsync(200);
    expect(maxConcurrent).toBe(1);
  });

  it('bounds consecutive failures to a terminal error state (never hangs)', async () => {
    const checkLegs = vi.fn(async () => { throw new Error('network down'); });
    const gate = createSpreadGate({ checkLegs, pollMs: 10, maxErrors: 3, maxWaitMs: 100_000 });
    gate.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(gate.getState().phase).toBe('checking'); // 1st failure — still retrying
    await vi.advanceTimersByTimeAsync(10);
    expect(gate.getState().phase).toBe('checking'); // 2nd failure — still retrying
    await vi.advanceTimersByTimeAsync(10);
    expect(gate.getState().phase).toBe('error'); // 3rd consecutive failure — bounded stop
    expect(checkLegs).toHaveBeenCalledTimes(SPREAD_GATE_MAX_ERRORS);

    // No further polling after the bounded error — confirms the loop
    // actually stopped rather than silently continuing in background.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(checkLegs).toHaveBeenCalledTimes(SPREAD_GATE_MAX_ERRORS);
  });

  it('bounds total wait to a terminal timeout state and never auto-submits', async () => {
    const checkLegs = vi.fn(async () => ({ ok: false, legs: [] }));
    const gate = createSpreadGate({ checkLegs, pollMs: 10, maxWaitMs: 25, maxErrors: 999 });
    gate.start();
    for (let i = 0; i < 6 && gate.getState().phase !== 'timeout'; i++) {
      await vi.advanceTimersByTimeAsync(10);
    }
    expect(gate.getState().phase).toBe('timeout');
    const callsAtTimeout = checkLegs.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(checkLegs).toHaveBeenCalledTimes(callsAtTimeout); // stopped — no auto-resolve
  });

  it('retry() resumes polling from a bounded error/timeout state', async () => {
    let shouldFail = true;
    const checkLegs = vi.fn(async () => {
      if (shouldFail) throw new Error('boom');
      return { ok: true, legs: [] };
    });
    const gate = createSpreadGate({ checkLegs, pollMs: 10, maxErrors: 2, maxWaitMs: 100_000 });
    gate.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(10);
    expect(gate.getState().phase).toBe('error');

    shouldFail = false;
    gate.retry();
    await vi.advanceTimersByTimeAsync(0);
    expect(gate.getState().phase).toBe('passed');
  });

  it('recheck() fires an immediate re-check and discards the superseded in-flight response', async () => {
    /** @type {(v: any) => void} */
    let resolveStale = () => {};
    const checkLegs = vi.fn()
      .mockImplementationOnce(() => new Promise((res) => { resolveStale = res; }))
      .mockImplementationOnce(async () => ({ ok: true, legs: [] }));
    const gate = createSpreadGate({ checkLegs, pollMs: 5_000, maxWaitMs: 100_000 });
    gate.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(gate.getState().phase).toBe('checking'); // first call still pending

    gate.recheck(); // operator edited TP%/SL%/Spread% mid-wait
    await vi.advanceTimersByTimeAsync(0);
    expect(gate.getState().phase).toBe('passed');
    expect(checkLegs).toHaveBeenCalledTimes(2);

    // The stale first call finally resolves with a WIDE result — must
    // be ignored; a stale response can never un-pass an already-passed gate.
    resolveStale({ ok: false, legs: [{ label: 'stale', tradingsymbol: 'stale', ok: false, spread_pct: 50, bid: 1, ask: 3, maxSpreadPct: 10 }] });
    await vi.advanceTimersByTimeAsync(0);
    expect(gate.getState().phase).toBe('passed');
  });

  it('confirmOverride() moves straight to overridden and stops polling', async () => {
    const checkLegs = vi.fn(async () => ({ ok: false, legs: [] }));
    const gate = createSpreadGate({ checkLegs, pollMs: 10, maxWaitMs: 100_000 });
    gate.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(gate.getState().phase).toBe('wide');

    gate.confirmOverride();
    expect(gate.getState().phase).toBe('overridden');
    const calls = checkLegs.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(checkLegs).toHaveBeenCalledTimes(calls); // no further polling
  });

  it('cancel() is idempotent and leaves no dangling timers', async () => {
    const checkLegs = vi.fn(async () => ({ ok: false, legs: [] }));
    const gate = createSpreadGate({ checkLegs, pollMs: 10, maxWaitMs: 100_000 });
    gate.start();
    await vi.advanceTimersByTimeAsync(0);
    gate.cancel();
    expect(gate.getState().phase).toBe('cancelled');
    expect(() => gate.cancel()).not.toThrow();
    const calls = checkLegs.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(checkLegs).toHaveBeenCalledTimes(calls);
  });
});
