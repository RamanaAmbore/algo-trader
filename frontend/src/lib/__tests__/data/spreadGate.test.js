import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  evaluateLegSpread,
  resolveWingTradingsymbol,
  createSpreadGate,
  SPREAD_GATE_MAX_ERRORS,
  isCloseIntentLeg,
  buildSpreadGatePlan,
  resolveHedgeStrike,
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

  it('retry() from a bounded "error" state shows "checking" immediately, not stuck on "error" (2026-11-26 regression — the blink fix above must not make error/timeout sticky)', async () => {
    const checkLegs = vi.fn(async () => { throw new Error('boom'); });
    const gate = createSpreadGate({ checkLegs, pollMs: 10, maxErrors: 1, maxWaitMs: 100_000 });
    gate.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(gate.getState().phase).toBe('error');

    // A pending (never-resolving within this assertion window) checkLegs
    // lets us observe the transient 'checking' phase retry() must show
    // immediately — before this fix, the sticky-phase condition wrongly
    // included 'error'/'timeout', which left the banner stuck reading
    // "Spread check failed" for the entire retry attempt instead.
    /** @type {(v: any) => void} */
    let resolveRetry = () => {};
    checkLegs.mockImplementationOnce(() => new Promise((res) => { resolveRetry = res; }));
    gate.retry();
    expect(gate.getState().phase).toBe('checking');
    expect(gate.getState().rechecking).toBe(false);

    resolveRetry({ ok: true, legs: [] });
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

  it('a background re-poll while already "wide" never regresses phase to "checking" (2026-11-26 blink fix)', async () => {
    // Regression for operator report: "Place anyway keeps blinking" —
    // root cause was `_tick` unconditionally emitting `phase:'checking'`
    // at the start of EVERY poll, including a background re-poll while
    // a known problem ('wide') was already showing. A UI branching on
    // `phase` via `{#if}/{:else if}` unmounted and remounted its
    // "Place anyway" button every single poll tick as a result.
    let call = 0;
    /** @type {string[]} */
    const phaseHistory = [];
    const checkLegs = vi.fn(async () => {
      call += 1;
      // Always wide — a spread that never recovers on its own, so the
      // gate keeps re-polling indefinitely (bounded only by maxWaitMs).
      return { ok: false, legs: [{ label: 'X', tradingsymbol: 'X', ok: false, spread_pct: 20, bid: 1, ask: 2, maxSpreadPct: 10 }] };
    });
    const gate = createSpreadGate({
      checkLegs, pollMs: 50, maxWaitMs: 100_000,
      onUpdate: (s) => phaseHistory.push(s.phase),
    });
    gate.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(gate.getState().phase).toBe('wide');
    expect(gate.getState().rechecking).toBe(false);

    // Three more background re-poll ticks — phase must stay 'wide' the
    // ENTIRE time, never visiting 'checking' again once a problem phase
    // has been reached. `rechecking` flips true for the in-flight
    // instant of each of these ticks (a caller MAY use it for a quiet
    // indicator) but `phase` itself is untouched.
    for (let i = 0; i < 3; i++) {
      await vi.advanceTimersByTimeAsync(50);
      expect(gate.getState().phase).toBe('wide');
    }
    expect(call).toBe(4); // 1 initial + 3 re-polls
    expect(gate.getState().rechecking).toBe(false); // settled back to false once each result lands

    // The FIRST tick transitions 'checking' → 'wide' (legitimate, a
    // genuinely first check). Every tick after that must never emit
    // 'checking' again.
    expect(phaseHistory[0]).toBe('checking');
    expect(phaseHistory.slice(1).every((p) => p === 'wide')).toBe(true);
  });

  it('recheck() while already "wide" (e.g. a Spread% edit) also stays sticky — no blink on an edit-triggered recheck either', async () => {
    const checkLegs = vi.fn(async () => ({ ok: false, legs: [{ label: 'X', tradingsymbol: 'X', ok: false, spread_pct: 20, bid: 1, ask: 2, maxSpreadPct: 10 }] }));
    /** @type {string[]} */
    const phaseHistory = [];
    const gate = createSpreadGate({
      checkLegs, pollMs: 5_000, maxWaitMs: 100_000,
      onUpdate: (s) => phaseHistory.push(s.phase),
    });
    gate.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(gate.getState().phase).toBe('wide');

    gate.recheck(); // operator edited Spread% mid-wait, still wide
    await vi.advanceTimersByTimeAsync(0);
    expect(gate.getState().phase).toBe('wide');
    expect(phaseHistory.every((p) => p === 'checking' || p === 'wide')).toBe(true);
    expect(phaseHistory.filter((p) => p === 'checking').length).toBe(1); // only the genuine first check
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

// ─────────────────────────────────────────────────────────────────────────
// isCloseIntentLeg / buildSpreadGatePlan — Chain tab's pre-submit spread
// gate close/reduce exemption (a close must never be blocked by a
// liquidity sanity check meant to protect new entries).
// ─────────────────────────────────────────────────────────────────────────
describe('isCloseIntentLeg', () => {
  const positionRows = [
    { account: 'ACC1', tradingsymbol: 'NIFTY26JUN24000CE', quantity: 10 }, // held LONG 10
    { account: 'ACC1', tradingsymbol: 'NIFTY26JUN24500PE', quantity: -5 }, // held SHORT 5
  ];

  it('a SELL leg that exactly flattens a held LONG position is close-intent', () => {
    const leg = { sym: 'NIFTY26JUN24000CE', side: 'SELL', lots: 2, lotSize: 5 }; // 10 contracts
    expect(isCloseIntentLeg(leg, 'ACC1', positionRows)).toBe(true);
  });

  it('a SELL leg that REDUCES (not flattens) a held LONG position is still close-intent', () => {
    const leg = { sym: 'NIFTY26JUN24000CE', side: 'SELL', lots: 1, lotSize: 5 }; // 5 contracts <= 10 held
    expect(isCloseIntentLeg(leg, 'ACC1', positionRows)).toBe(true);
  });

  it('a BUY leg that exactly flattens a held SHORT position is close-intent', () => {
    const leg = { sym: 'NIFTY26JUN24500PE', side: 'BUY', lots: 1, lotSize: 5 }; // 5 contracts
    expect(isCloseIntentLeg(leg, 'ACC1', positionRows)).toBe(true);
  });

  it('a SELL leg adding to an existing SHORT (same direction) is NOT close-intent', () => {
    const leg = { sym: 'NIFTY26JUN24500PE', side: 'SELL', lots: 1, lotSize: 5 };
    expect(isCloseIntentLeg(leg, 'ACC1', positionRows)).toBe(false);
  });

  it('a BUY leg adding to an existing LONG (same direction) is NOT close-intent', () => {
    const leg = { sym: 'NIFTY26JUN24000CE', side: 'BUY', lots: 1, lotSize: 5 };
    expect(isCloseIntentLeg(leg, 'ACC1', positionRows)).toBe(false);
  });

  it('a SELL leg that OVERSHOOTS a held LONG (a flip past flat) is NOT close-intent — the whole leg stays gated', () => {
    const leg = { sym: 'NIFTY26JUN24000CE', side: 'SELL', lots: 3, lotSize: 5 }; // 15 contracts > 10 held
    expect(isCloseIntentLeg(leg, 'ACC1', positionRows)).toBe(false);
  });

  it('no held position for that symbol+account → NOT close-intent (fails toward the existing, safer behavior)', () => {
    const leg = { sym: 'BANKNIFTY26JUN45000PE', side: 'SELL', lots: 1, lotSize: 5 };
    expect(isCloseIntentLeg(leg, 'ACC1', positionRows)).toBe(false);
  });

  it('positionRows not yet loaded (empty/undefined) → NOT close-intent', () => {
    const leg = { sym: 'NIFTY26JUN24000CE', side: 'SELL', lots: 1, lotSize: 5 };
    expect(isCloseIntentLeg(leg, 'ACC1', [])).toBe(false);
    expect(isCloseIntentLeg(leg, 'ACC1', undefined)).toBe(false);
  });

  it('a different account holding the same symbol does not make a leg close-intent (account-scoped)', () => {
    const leg = { sym: 'NIFTY26JUN24000CE', side: 'SELL', lots: 1, lotSize: 5 };
    expect(isCloseIntentLeg(leg, 'ACC2', positionRows)).toBe(false);
  });

  it('sums quantity across multiple matching rows for the same symbol+account (e.g. NRML + MIS)', () => {
    const multiProductRows = [
      { account: 'ACC1', tradingsymbol: 'NIFTY26JUN24000CE', quantity: 6 },
      { account: 'ACC1', tradingsymbol: 'NIFTY26JUN24000CE', quantity: 4 },
    ];
    const leg = { sym: 'NIFTY26JUN24000CE', side: 'SELL', lots: 2, lotSize: 5 }; // 10 contracts == 6+4 held
    expect(isCloseIntentLeg(leg, 'ACC1', multiProductRows)).toBe(true);
  });

  it('MCX lotSize (e.g. 100) close-intent reduce — the operator\'s own reported expiry-close scenario', () => {
    const mcxRows = [{ account: 'ACC1', tradingsymbol: 'CRUDEOIL26NOVFUT', quantity: -200 }]; // short 2 lots
    const leg = { sym: 'CRUDEOIL26NOVFUT', side: 'BUY', lots: 2, lotSize: 100 }; // 200 contracts <= 200 held
    expect(isCloseIntentLeg(leg, 'ACC1', mcxRows)).toBe(true);
  });

  it('falls back to a `symbol`/`qty` shaped row (not only `tradingsymbol`/`quantity`)', () => {
    const rows = [{ account: 'ACC1', symbol: 'NIFTY26JUN24000CE', qty: 10 }];
    const leg = { sym: 'NIFTY26JUN24000CE', side: 'SELL', lots: 1, lotSize: 5 };
    expect(isCloseIntentLeg(leg, 'ACC1', rows)).toBe(true);
  });

  it('a zero-contracts leg (missing lots/lotSize) is never close-intent', () => {
    const leg = { sym: 'NIFTY26JUN24000CE', side: 'SELL' };
    expect(isCloseIntentLeg(leg, 'ACC1', positionRows)).toBe(false);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// resolveHedgeStrike — Part A, Chain-tab auto-hedge strike resolution.
// Mirrors (not merges with) resolveWingTradingsymbol above — a
// forward-looking, premium-percent match among currently-quoted
// strikes, not a backward manual-points offset.
// ─────────────────────────────────────────────────────────────────────────
describe('resolveHedgeStrike', () => {
  it('CE direction — searches strikes strictly above the parent, nearest-to-target wins', () => {
    const chainQuotesMap = {
      '90':  { ce: { bid: 200, ask: 205 } }, // below parent — must be ignored
      '110': { ce: { bid: 60, ask: 62 } },   // ratio 60%
      '120': { ce: { bid: 50, ask: 52 } },   // ratio 50% — exact target
      '130': { ce: { bid: 20, ask: 22 } },   // ratio 20% — below tolerance [25,75]
    };
    const r = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY',
      parentPremium: 100, targetPct: 50, chainQuotesMap,
    });
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.strike).toBe(120);
      expect(r.price).toBe(50);
      expect(r.ratio).toBeCloseTo(50, 6);
    }
  });

  it('PE direction — searches strikes strictly below the parent, nearest-to-target wins', () => {
    const chainQuotesMap = {
      '110': { pe: { bid: 999, ask: 999 } }, // above parent — must be ignored
      '90':  { pe: { bid: 10, ask: 12 } },   // ratio 12%
      '80':  { pe: { bid: 23, ask: 25 } },   // ratio 25% — exact target
      '70':  { pe: { bid: 40, ask: 45 } },   // ratio 45% — above tolerance [12.5,37.5]
    };
    const r = resolveHedgeStrike({
      parentOptType: 'PE', parentStrike: 100, parentSide: 'SELL',
      parentPremium: 100, targetPct: 25, chainQuotesMap,
    });
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.strike).toBe(80);
      expect(r.price).toBe(25);
      expect(r.ratio).toBeCloseTo(25, 6);
    }
  });

  it('BUY parent → hedge is a SELL, reads bid first (falls back to ask when bid is missing)', () => {
    const withBid = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100,
      targetPct: 50, chainQuotesMap: { '110': { ce: { bid: 50, ask: 55 } } },
    });
    expect(withBid.ok).toBe(true);
    if (withBid.ok) expect(withBid.price).toBe(50); // bid preferred over ask

    const noBid = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100,
      targetPct: 50, chainQuotesMap: { '110': { ce: { bid: null, ask: 50 } } },
    });
    expect(noBid.ok).toBe(true);
    if (noBid.ok) expect(noBid.price).toBe(50); // falls back to ask
  });

  it('SELL parent → hedge is a BUY, reads ask first (falls back to bid when ask is missing)', () => {
    const withAsk = resolveHedgeStrike({
      parentOptType: 'PE', parentStrike: 100, parentSide: 'SELL', parentPremium: 100,
      targetPct: 25, chainQuotesMap: { '90': { pe: { bid: 20, ask: 25 } } },
    });
    expect(withAsk.ok).toBe(true);
    if (withAsk.ok) expect(withAsk.price).toBe(25); // ask preferred over bid

    const noAsk = resolveHedgeStrike({
      parentOptType: 'PE', parentStrike: 100, parentSide: 'SELL', parentPremium: 100,
      targetPct: 25, chainQuotesMap: { '90': { pe: { bid: 25, ask: null } } },
    });
    expect(noAsk.ok).toBe(true);
    if (noAsk.ok) expect(noAsk.price).toBe(25); // falls back to bid
  });

  it('rejects a candidate whose resolved price is 0/null/undefined — never treats it as a 0%-ratio match', () => {
    const r = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 50,
      chainQuotesMap: {
        '110': { ce: { bid: 0, ask: null } },    // no real quote — rejected
        '120': { ce: { bid: 50, ask: 52 } },     // valid, exact target
      },
    });
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.strike).toBe(120);
  });

  it('tolerance band is inclusive at exactly ±50% of target (25% and 75% both accepted for target=50)', () => {
    const lowBoundary = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 50,
      chainQuotesMap: { '110': { ce: { bid: 25, ask: 26 } } }, // ratio exactly 25%
    });
    expect(lowBoundary.ok).toBe(true);

    const highBoundary = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 50,
      chainQuotesMap: { '110': { ce: { bid: 75, ask: 76 } } }, // ratio exactly 75%
    });
    expect(highBoundary.ok).toBe(true);

    const justBelowLow = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 50,
      chainQuotesMap: { '110': { ce: { bid: 24.9, ask: 25 } } }, // ratio 24.9% — outside
    });
    expect(justBelowLow.ok).toBe(false);

    const justAboveHigh = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 50,
      chainQuotesMap: { '110': { ce: { bid: 75.1, ask: 76 } } }, // ratio 75.1% — outside
    });
    expect(justAboveHigh.ok).toBe(false);
  });

  it('on an exact distance tie, the strike nearer to the parent (visited first in search order) wins', () => {
    // target=50: strike 110 → ratio 60 (dist 10); strike 130 → ratio 40 (dist 10).
    // Ascending search order for CE visits 110 before 130.
    const r = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 50,
      chainQuotesMap: {
        '110': { ce: { bid: 60, ask: 61 } },
        '130': { ce: { bid: 40, ask: 41 } },
      },
    });
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.strike).toBe(110);
  });

  it('structured failure — invalid inputs (non-positive parentPremium)', () => {
    const r = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 0, targetPct: 50,
      chainQuotesMap: { '110': { ce: { bid: 50, ask: 52 } } },
    });
    expect(r.ok).toBe(false);
    if (r.ok === false) expect(r.reason).toMatch(/invalid/i);
  });

  it('structured failure — invalid inputs (non-positive targetPct)', () => {
    const r = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 0,
      chainQuotesMap: { '110': { ce: { bid: 50, ask: 52 } } },
    });
    expect(r.ok).toBe(false);
    if (r.ok === false) expect(r.reason).toMatch(/invalid/i);
  });

  it('structured failure — invalid inputs (unrecognised option type)', () => {
    const r = resolveHedgeStrike({
      // @ts-expect-error — deliberately wrong optType to exercise the guard
      parentOptType: 'FUT', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 50,
      chainQuotesMap: { '110': { ce: { bid: 50, ask: 52 } } },
    });
    expect(r.ok).toBe(false);
    if (r.ok === false) expect(r.reason).toMatch(/option type/i);
  });

  it('structured failure — no chain quotes loaded yet (null map)', () => {
    const r = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 50,
      chainQuotesMap: null,
    });
    expect(r.ok).toBe(false);
    if (r.ok === false) expect(r.reason).toMatch(/no chain quotes/i);
  });

  it('structured failure — no chain quotes loaded yet (empty map)', () => {
    const r = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 50,
      chainQuotesMap: {},
    });
    expect(r.ok).toBe(false);
    if (r.ok === false) expect(r.reason).toMatch(/no chain quotes/i);
  });

  it('structured failure — no strikes further OTM than the parent at all', () => {
    const r = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 50,
      chainQuotesMap: { '90': { ce: { bid: 10, ask: 12 } }, '100': { ce: { bid: 50, ask: 52 } } },
    });
    expect(r.ok).toBe(false);
    if (r.ok === false) expect(r.reason).toMatch(/further OTM/i);
  });

  it('structured failure — further-OTM strikes exist but none has a real quote', () => {
    const r = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 50,
      chainQuotesMap: { '110': { ce: { bid: 0, ask: null } }, '120': { ce: { bid: null, ask: null } } },
    });
    expect(r.ok).toBe(false);
    if (r.ok === false) expect(r.reason).toMatch(/depth-backed quote/i);
  });

  it('structured failure — priced candidates exist but none within tolerance of target', () => {
    const r = resolveHedgeStrike({
      parentOptType: 'CE', parentStrike: 100, parentSide: 'BUY', parentPremium: 100, targetPct: 50,
      chainQuotesMap: { '110': { ce: { bid: 5, ask: 6 } } }, // ratio 5% — nowhere near [25,75]
    });
    expect(r.ok).toBe(false);
    if (r.ok === false) expect(r.reason).toMatch(/no strike priced near/i);
  });
});

describe('buildSpreadGatePlan', () => {
  const positionRows = [
    { account: 'ACC1', tradingsymbol: 'NIFTY26JUN24000CE', quantity: 10 }, // held LONG 10
  ];

  it('a close-intent leg is excluded from the check plan', () => {
    const basket = [{ sym: 'NIFTY26JUN24000CE', optType: 'CE', side: 'SELL', lots: 2, lotSize: 5, exchange: 'NFO' }];
    const items = buildSpreadGatePlan(basket, 'ACC1', positionRows, 0.5);
    expect(items).toEqual([]);
  });

  it('an open/entry leg for the same symbol is still included', () => {
    const basket = [{ sym: 'NIFTY26JUN24000CE', optType: 'CE', side: 'BUY', lots: 1, lotSize: 5, exchange: 'NFO' }];
    const items = buildSpreadGatePlan(basket, 'ACC1', positionRows, 0.5);
    expect(items).toHaveLength(1);
    expect(items[0]).toEqual({ tradingsymbol: 'NIFTY26JUN24000CE', exchange: 'NFO', maxSpreadPct: 0.5, label: 'NIFTY26JUN24000CE' });
  });

  it('a basket mixing one open and one close leg still gates — on the open leg only', () => {
    const basket = [
      { sym: 'NIFTY26JUN24000CE', optType: 'CE', side: 'SELL', lots: 2, lotSize: 5, exchange: 'NFO' }, // close (10 held)
      { sym: 'BANKNIFTY26JUN45000PE', optType: 'PE', side: 'BUY', lots: 1, lotSize: 25, exchange: 'NFO' }, // open (no held position)
    ];
    const items = buildSpreadGatePlan(basket, 'ACC1', positionRows, 0.5);
    expect(items).toHaveLength(1);
    expect(items[0].tradingsymbol).toBe('BANKNIFTY26JUN45000PE');
  });

  it('futures legs stay out of scope regardless of close/open intent (unchanged pre-existing behavior)', () => {
    const basket = [{ sym: 'CRUDEOIL26NOVFUT', side: 'BUY', lots: 1, lotSize: 100, exchange: 'MCX' }]; // no optType
    const items = buildSpreadGatePlan(basket, 'ACC1', positionRows, 0.5);
    expect(items).toEqual([]);
  });

  it('falls back to the plan-level account when a leg carries no account field of its own', () => {
    const basket = [{ sym: 'NIFTY26JUN24000CE', optType: 'CE', side: 'SELL', lots: 2, lotSize: 5, exchange: 'NFO' }];
    const items = buildSpreadGatePlan(basket, 'ACC1', positionRows, 0.5);
    expect(items).toEqual([]); // still exempted via the fallback account
  });

  it('prefers a leg\'s own account over the plan-level fallback', () => {
    const basket = [{ sym: 'NIFTY26JUN24000CE', optType: 'CE', side: 'SELL', lots: 2, lotSize: 5, exchange: 'NFO', account: 'ACC2' }];
    // ACC2 holds nothing, so despite the plan-level account (ACC1) holding
    // this symbol, the leg's OWN account must be the one actually checked.
    const items = buildSpreadGatePlan(basket, 'ACC1', positionRows, 0.5);
    expect(items).toHaveLength(1);
  });
});
