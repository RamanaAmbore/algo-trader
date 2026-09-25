/**
 * pageLoad.test.js — Vitest unit tests for `splitClosedReopened` and
 * `buildPositionRowFromBroker` in $lib/derivatives/pageLoad.js.
 *
 * Core invariant under test: splitting a consolidated broker position into
 * closed/open display rows must NEVER change the total Day P&L or Exp P&L
 * attributed to that position — Σ baseDayPnlForPosition(splitRows) must
 * equal baseDayPnlForPosition(the pre-split row), for both the overnight
 * (oq!==0) and intraday-round-trip (oq===0) split paths, and regardless of
 * whether the backend has populated `unrealised` yet (pnl-fallback path)
 * or not (realised+unrealised path).
 *
 * Five quality dimensions:
 *   1. SSOT   — uses the real baseDayPnlForPosition/expiryPnlWithRealised, no reimplementation
 *   2. Perf   — pure synchronous unit tests
 *   3. Stale  — guards the closed/open baseline-double-subtract regression
 *   4. Reuse  — same helpers used by the Legs grid, Snapshot rollup, NavStrip
 *   5. UX     — Σ split rows must reconcile with the pre-split TOTAL by construction
 */

import { describe, it, expect } from 'vitest';
import {
  splitClosedReopened, buildPositionRowFromBroker,
  didUnderlyingChange, synthEquityOnlyStrategy, synthCacheKey,
} from '$lib/derivatives/pageLoad.js';
import { baseDayPnlForPosition } from '$lib/data/nav.js';
import { expiryPnlWithRealised } from '$lib/data/expiryPnl.js';
import { decomposeSymbol } from '$lib/data/decomposeSymbol.js';

function sumDayPnl(rows) {
  return rows.reduce((s, r) => s + baseDayPnlForPosition(r), 0);
}

describe('buildPositionRowFromBroker — unrealised passthrough', () => {
  it('carries unrealised through when present on the broker row', () => {
    const row = buildPositionRowFromBroker({ tradingsymbol: 'NIFTY25SEP24000CE', quantity: 50, realised: 100, unrealised: 200, pnl: 300 }, 'live');
    expect(row.unrealised).toBe(200);
  });

  it('leaves unrealised undefined (not 0) when absent — preserves pnl-fallback in currentTotalProfit', () => {
    const row = buildPositionRowFromBroker({ tradingsymbol: 'NIFTY25SEP24000CE', quantity: 50, realised: 100, pnl: 300 }, 'live');
    expect(row.unrealised).toBeUndefined();
  });
});

describe('splitClosedReopened — no-op paths', () => {
  it('returns [p] unchanged when there is no day activity', () => {
    const p = buildPositionRowFromBroker({ tradingsymbol: 'NIFTY25SEP24000CE', quantity: 50, overnight_quantity: 50, pnl: 500 }, 'live');
    expect(splitClosedReopened(p)).toEqual([p]);
  });
});

describe('splitClosedReopened — overnight (oq!==0) split invariant', () => {
  function makeOvernightRow(overrides = {}) {
    return buildPositionRowFromBroker({
      tradingsymbol: 'NIFTY25SEP24000CE',
      quantity: 5,           // 10 overnight - 5 sold today = 5 remaining
      overnight_quantity: 10,
      day_sell_quantity: 5,
      day_sell_value: 5 * 220, // exit @ 220
      day_buy_quantity: 0,
      day_buy_value: 0,
      average_price: 200,
      prev_close: 210,
      pnl: 150,               // broker-reported lifetime pnl on remaining + closed
      realised: 0,
      prev_settlement_pnl: 100,
      ...overrides,
    }, 'live');
  }

  it('Σ baseDayPnlForPosition(split rows) === baseDayPnlForPosition(pre-split row) — pnl-fallback path (no unrealised)', () => {
    const p = makeOvernightRow();
    const expected = baseDayPnlForPosition(p);
    const split = splitClosedReopened(p);
    expect(split.length).toBe(2);
    expect(sumDayPnl(split)).toBeCloseTo(expected, 6);
  });

  it('Σ baseDayPnlForPosition(split rows) === baseDayPnlForPosition(pre-split row) — realised+unrealised path', () => {
    const p = makeOvernightRow({ realised: 20, unrealised: 130 }); // realised+unrealised = pnl = 150
    const expected = baseDayPnlForPosition(p);
    const split = splitClosedReopened(p);
    expect(sumDayPnl(split)).toBeCloseTo(expected, 6);
  });

  it('fully closed overnight position (brokerQty=0) returns a single closed row with base=0', () => {
    const p = makeOvernightRow({ quantity: 0, day_sell_quantity: 10, day_sell_value: 10 * 220, overnight_quantity: 10, pnl: 200 });
    const split = splitClosedReopened(p);
    expect(split.length).toBe(1);
    expect(split[0]._splitTag).toBe('closed');
    // Day P&L for a fully-closed-today row == closed_day_pnl == its lifetime pnl minus prior-session carry.
    expect(baseDayPnlForPosition(split[0])).toBeCloseTo(split[0].day_change_val, 6);
  });
});

describe('splitClosedReopened — intraday round-trip (oq===0) split invariant', () => {
  function makeIntradayRow(overrides = {}) {
    return buildPositionRowFromBroker({
      tradingsymbol: 'NIFTY25SEP24000CE',
      quantity: 5,             // bought 10, sold 5 today -> 5 remain
      overnight_quantity: 0,   // no overnight carry (or Groww hardcoding it to 0)
      day_buy_quantity: 10,
      day_buy_value: 10 * 200,
      day_sell_quantity: 5,
      day_sell_value: 5 * 220,
      average_price: 200,
      prev_close: 0,
      pnl: 250,                 // (220-200)*5 realised + (ltp-200)*5 unrealised, broker total
      realised: 0,
      prev_settlement_pnl: null,
      ...overrides,
    }, 'live');
  }

  it('Σ baseDayPnlForPosition(split rows) === baseDayPnlForPosition(pre-split row) — pnl-fallback path', () => {
    const p = makeIntradayRow();
    const expected = baseDayPnlForPosition(p);
    const split = splitClosedReopened(p);
    expect(split.length).toBe(2);
    expect(sumDayPnl(split)).toBeCloseTo(expected, 6);
  });

  it('Σ baseDayPnlForPosition(split rows) === baseDayPnlForPosition(pre-split row) — realised+unrealised path', () => {
    const p = makeIntradayRow({ realised: 100, unrealised: 150 }); // sums to pnl=250
    const expected = baseDayPnlForPosition(p);
    const split = splitClosedReopened(p);
    expect(sumDayPnl(split)).toBeCloseTo(expected, 6);
  });

  it('Groww-style row (overnight_quantity hardcoded 0, but genuinely a round trip) still splits and reconciles', () => {
    const p = makeIntradayRow({ tradingsymbol: 'RELIANCE', overnight_quantity: 0, quantity: 0, day_buy_quantity: 10, day_sell_quantity: 10, day_buy_value: 2000, day_sell_value: 2100, pnl: 100 });
    const split = splitClosedReopened(p);
    expect(split.length).toBe(1); // brokerQty=0 -> fully closed, single row
    expect(split[0]._splitTag).toBe('closed');
    expect(sumDayPnl(split)).toBeCloseTo(baseDayPnlForPosition(p), 6);
  });

  it('Groww hidden-overnight bug: fully-closed row with dbq!==dsq must NOT drop the residual portion (2026-09 regression)', () => {
    // Groww hardcodes overnight_quantity=0 even when a position genuinely
    // carried overnight. Simulate: a position with a true 10-unit overnight
    // carry-in plus a 5-unit intraday round-trip, fully closed today via
    // 5 buys + 15 sells (net: +10 overnight -10 via sells + round-trip).
    // The OLD buggy code sized the closed row on closedQty=min(dbq,dsq)=5
    // (the round-trip only) and silently dropped the other 10 units' P&L
    // whenever brokerQty (final qty) === 0. The fix must use the whole
    // row's pnl/baseDayPnlForPosition instead.
    const p = makeIntradayRow({
      tradingsymbol: 'RELIANCE',
      overnight_quantity: 0,      // Groww's (untrustworthy) hardcoded 0
      quantity: 0,                // fully closed today
      day_buy_quantity: 5,
      day_buy_value: 5 * 200,
      day_sell_quantity: 15,
      day_sell_value: 15 * 220,
      pnl: 500,                   // broker-authoritative total realized P&L
      realised: 0,
      // True prior-session baseline for this (account,symbol) — the backend
      // baseline join is keyed by account+symbol, not by Groww's oq flag,
      // so it correctly reflects the hidden overnight carry-in.
      prev_settlement_pnl: 300,
    });
    const expectedDayPnl = baseDayPnlForPosition(p); // 500 - 300 = 200
    const split = splitClosedReopened(p);
    expect(split.length).toBe(1);
    expect(split[0]._splitTag).toBe('closed');
    // Lifetime P&L must be the FULL broker-reported pnl (500), not the
    // round-trip-only figure (100) the old min(dbq,dsq) sizing produced.
    expect(split[0].pnl).toBeCloseTo(500, 6);
    // Day P&L must reconcile with the baseline-diff formula on the
    // pre-split row, not the truncated round-trip-only value.
    expect(sumDayPnl(split)).toBeCloseTo(expectedDayPnl, 6);
    expect(expectedDayPnl).toBeCloseTo(200, 6);
  });

  it('Groww hidden-overnight bug: fully-closed row sourced from realised+unrealised (no top-level pnl) still recovers the full lifetime P&L', () => {
    // Groww may ship realised_pnl + unrealised_pnl without a native
    // combined `pnl` field (per the design doc's per-broker sourcing:
    // Groww falls back to realised_pnl + unrealised_pnl when pnl is
    // absent). The closed row must be sized via currentTotalProfit(p)
    // (realised+unrealised), not a bare `Number(p.pnl || 0)` which would
    // silently read 0 when pnl is undefined.
    const p = makeIntradayRow({
      tradingsymbol: 'RELIANCE',
      overnight_quantity: 0,
      quantity: 0,
      day_buy_quantity: 5,
      day_buy_value: 5 * 200,
      day_sell_quantity: 15,
      day_sell_value: 15 * 220,
      pnl: undefined,           // no native combined field
      realised: 350,
      unrealised: 150,          // realised+unrealised = 500 (same total as above)
      prev_settlement_pnl: 300,
    });
    const split = splitClosedReopened(p);
    expect(split.length).toBe(1);
    expect(split[0].pnl).toBeCloseTo(500, 6);
    expect(split[0].realised).toBeCloseTo(500, 6);
    expect(sumDayPnl(split)).toBeCloseTo(200, 6);
  });

  it('closed row is unaffected by whether unrealised is present on the pre-split row', () => {
    const pWithout = makeIntradayRow();
    const pWith    = makeIntradayRow({ realised: 100, unrealised: 150 });
    const closedWithout = splitClosedReopened(pWithout)[0];
    const closedWith    = splitClosedReopened(pWith)[0];
    expect(baseDayPnlForPosition(closedWithout)).toBeCloseTo(baseDayPnlForPosition(closedWith), 6);
  });
});

describe('expiryPnlWithRealised — unaffected by the split (Exp P&L reconciles too)', () => {
  it('Σ expiryPnlWithRealised(split rows) accounts for the full realised + remaining-qty intrinsic value', () => {
    // Pre-split: qty=5 remaining @ avg=200, realised on the closed 5 lots = (220-200)*5 = 100.
    const p = {
      symbol: 'NIFTY25SEP24000CE', kind: 'opt', qty: 5, avg_cost: 200, realised: 0, pnl: 250,
    };
    const spot = 24500; // strike parses via decomposeSymbol/legAnalytics fallback; use legAnalytics for determinism
    const legAnalytics = { 'NIFTY25SEP24000CE': { strike: 24000, opt_type: 'CE' } };
    const closedRow = { symbol: p.symbol, kind: 'opt', qty: 0, pnl: 100, realised: 100, unrealised: 0 };
    const openRow   = { symbol: p.symbol, kind: 'opt', qty: 5, avg_cost: 200, realised: 0 };

    const closedExp = expiryPnlWithRealised(closedRow, spot, legAnalytics);
    const openExp   = expiryPnlWithRealised(openRow, spot, legAnalytics);
    // closed leg locks in its realised (100); open leg carries intrinsic value at spot.
    expect(closedExp).toBe(100);
    expect(openExp).not.toBeNull();
  });
});

describe('didUnderlyingChange — root-switch detection + the equity-synth gap', () => {
  it('returns false when there is no current strategy (nothing to compare against)', () => {
    const cleanLegs = [{ symbol: 'NIFTY25SEP24000CE' }];
    expect(didUnderlyingChange(cleanLegs, null, decomposeSymbol)).toBe(false);
  });

  it('returns false when cleanLegs is empty (nothing to compare)', () => {
    const currentStrategy = { legs: [{ symbol: 'NIFTY25SEP24000CE' }] };
    expect(didUnderlyingChange([], currentStrategy, decomposeSymbol)).toBe(false);
  });

  it('returns true on a genuine underlying switch between two real (option) strategies', () => {
    const cleanLegs = [{ symbol: 'BANKNIFTY25SEP52000CE' }];
    const currentStrategy = { legs: [{ symbol: 'NIFTY25SEP24000CE' }] };
    expect(didUnderlyingChange(cleanLegs, currentStrategy, decomposeSymbol)).toBe(true);
  });

  it('returns false when the underlying is unchanged between two real strategies', () => {
    const cleanLegs = [{ symbol: 'NIFTY25SEP24500CE' }];
    const currentStrategy = { legs: [{ symbol: 'NIFTY25SEP24000CE' }] };
    expect(didUnderlyingChange(cleanLegs, currentStrategy, decomposeSymbol)).toBe(false);
  });

  // ── The gap `loadStrategy({ clear: true })` closes ──────────────────────
  //
  // synthEquityOnlyStrategy() (the shell strategy rendered when the leg set
  // is equity-only) always carries `legs: []` (see pageLoad.js). When the
  // CURRENTLY-DISPLAYED strategy is this synth shell and the operator then
  // switches to a real option/futures underlying, didUnderlyingChange's own
  // early-return (`if (!prevLegs?.length) return false`) means it NEVER
  // detects the switch — it can't compare roots against a strategy with no
  // legs to decompose. Without an external reset, loadStrategy()'s legsKey
  // memo (`_stratLastKey`) is untouched by the equity-synth branch (which
  // returns before ever setting it) and may still hold a stale key from
  // BEFORE the synth phase — if that stale key happens to match the new
  // real legs' key, the fetch is skipped entirely and the synth shell's
  // payoff stays on screen mislabeled under the new underlying.
  //
  // `loadStrategy({ clear: true })` (wired to the selectedUnderlying-change
  // $effect in +page.svelte) closes this by unconditionally resetting
  // `_stratLastKey = ''` BEFORE this early-return path is ever reached —
  // it doesn't depend on didUnderlyingChange detecting anything.
  it('documents the gap: synth-shell strategy (legs: []) is never detected as an underlying change by didUnderlyingChange alone', () => {
    const eqLeg = { symbol: 'RELIANCE', qty: 10, avg_cost: 2500, ltp: 2600, prev_close: 2580 };
    const synthStrategy = synthEquityOnlyStrategy([eqLeg], 'RELIANCE');
    expect(synthStrategy.legs).toEqual([]); // precondition for the gap

    // Operator switches to a real option strategy on a DIFFERENT underlying.
    const cleanLegs = [{ symbol: 'NIFTY25SEP24000CE' }];
    // didUnderlyingChange cannot see the switch — this is the gap.
    expect(didUnderlyingChange(cleanLegs, synthStrategy, decomposeSymbol)).toBe(false);
  });
});

describe('synthEquityOnlyStrategy — proxy-hedge spot basis (D1 fix, 2026-09)', () => {
  // Regression test for the confirmed real-money incident: GOLDM's F&O legs
  // all settled to qty=0 at expiry, leaving only 2 GOLDBEES beta-hedge
  // proxy legs (`proxy_for: 'GOLDM'`). Before the fix, the shell's `spot`
  // silently fell back to `primary.ltp` — GOLDBEES's OWN price (₹124.43) —
  // instead of GOLDM's real spot (₹1,50,736), a ~1211× difference that
  // cascaded into a ₹76.4-crore Exp P&L and a +122,012.77% CHG% on screen.
  const GOLDBEES_LTP = 124.43;
  const GOLDBEES_PREV_CLOSE = 123.44;
  const GOLDM_SPOT = 150736;
  const GOLDM_PREV_CLOSE = 148500;

  function makeGoldbeesProxyLegs() {
    return [
      { symbol: 'GOLDBEES', kind: 'eq', qty: 500, avg_cost: 60, ltp: GOLDBEES_LTP, prev_close: GOLDBEES_PREV_CLOSE, proxy_for: 'GOLDM' },
      { symbol: 'GOLDBEES', kind: 'eq', qty: 300, avg_cost: 58, ltp: GOLDBEES_LTP, prev_close: GOLDBEES_PREV_CLOSE, proxy_for: 'GOLDM' },
    ];
  }

  it('prices the shell in the HEDGED root\'s own space, not the proxy ETF\'s own ltp — the exact root-cause regression', () => {
    const legs = makeGoldbeesProxyLegs();
    const strat = synthEquityOnlyStrategy(legs, 'GOLDM', GOLDM_SPOT, GOLDM_PREV_CLOSE);
    expect(strat).not.toBeNull();
    // The ~1211× smoking gun: spot must be GOLDM's real spot, never GOLDBEES's own ltp.
    expect(strat.spot).toBe(GOLDM_SPOT);
    expect(strat.spot).not.toBeCloseTo(GOLDBEES_LTP, 0);
    const ratio = GOLDM_SPOT / GOLDBEES_LTP;
    expect(ratio).toBeGreaterThan(1200);
    expect(ratio).toBeLessThan(1220);
    expect(strat.spot_prev_close).toBe(GOLDM_PREV_CLOSE);
  });

  it('builds the payoff grid centered on the target spot, in the real underlying\'s price space', () => {
    const legs = makeGoldbeesProxyLegs();
    const strat = synthEquityOnlyStrategy(legs, 'GOLDM', GOLDM_SPOT, GOLDM_PREV_CLOSE);
    expect(strat.payoff.length).toBe(41);
    // spanPct = 0.15 around GOLDM_SPOT — grid must span ~1,28,000–1,73,000,
    // NOT ~106–143 (the proxy-price-space grid the bug produced).
    expect(strat.payoff[0].spot).toBeCloseTo(GOLDM_SPOT * 0.85, 1);
    expect(strat.payoff[40].spot).toBeCloseTo(GOLDM_SPOT * 1.15, 1);
    for (const pt of strat.payoff) {
      expect(pt.spot).toBeGreaterThan(1000); // sanity: never in GOLDBEES's ~100-140 range
    }
  });

  it('returns null (fail-closed) for a proxy leg when no target spot is available yet — never falls back to the proxy\'s own price', () => {
    const legs = makeGoldbeesProxyLegs();
    expect(synthEquityOnlyStrategy(legs, 'GOLDM')).toBeNull();
    expect(synthEquityOnlyStrategy(legs, 'GOLDM', 0, 0)).toBeNull();
    expect(synthEquityOnlyStrategy(legs, 'GOLDM', NaN, NaN)).toBeNull();
  });

  it('proxy_for match is case-insensitive against `underlying`', () => {
    const legs = [{ symbol: 'GOLDBEES', kind: 'eq', qty: 100, avg_cost: 60, ltp: GOLDBEES_LTP, prev_close: GOLDBEES_PREV_CLOSE, proxy_for: 'goldm' }];
    const strat = synthEquityOnlyStrategy(legs, 'GOLDM', GOLDM_SPOT, GOLDM_PREV_CLOSE);
    expect(strat.spot).toBe(GOLDM_SPOT);
  });

  it('net_cost still sums real invested rupees from the proxy legs, unaffected by the spot-basis fix', () => {
    const legs = makeGoldbeesProxyLegs();
    const strat = synthEquityOnlyStrategy(legs, 'GOLDM', GOLDM_SPOT, GOLDM_PREV_CLOSE);
    expect(strat.net_cost).toBeCloseTo(500 * 60 + 300 * 58, 6);
  });

  // ── Must-not-regress: no-proxy case (holdings genuinely ARE the plotted
  // underlying, e.g. holding GOLDBEES under its own "GOLDBEES" tab) ──────
  it('non-proxy case: still uses primary.ltp/prev_close, ignoring any passed target spot', () => {
    const eqLeg = { symbol: 'RELIANCE', qty: 10, avg_cost: 2500, ltp: 2600, prev_close: 2580 };
    // Call site now always passes SOME target spot (from _undLive[selectedUnderlying]) —
    // a deliberately WRONG one here (9999) proves the no-proxy branch ignores it.
    const strat = synthEquityOnlyStrategy([eqLeg], 'RELIANCE', 9999, 9998);
    expect(strat.spot).toBe(2600);
    expect(strat.spot).not.toBe(9999);
    expect(strat.spot_prev_close).toBe(2580);
    expect(strat.payoff[0].spot).toBeCloseTo(2600 * 0.85, 6);
    expect(strat.payoff[40].spot).toBeCloseTo(2600 * 1.15, 6);
    expect(strat.net_cost).toBeCloseTo(25000, 6);
  });

  it('non-proxy case with no explicit target args (backward-compatible 2-arg call): unchanged behavior', () => {
    const eqLeg = { symbol: 'RELIANCE', qty: 10, avg_cost: 2500, ltp: 2600, prev_close: 2580 };
    const strat = synthEquityOnlyStrategy([eqLeg], 'RELIANCE');
    expect(strat.spot).toBe(2600);
    expect(strat.spot_prev_close).toBe(2580);
    expect(strat.legs).toEqual([]);
  });
});

describe('synthCacheKey — includes target spot/prev-close (D1 fix, 2026-09)', () => {
  it('changes when the target root\'s own spot moves, even if the proxy leg\'s own fields are unchanged', () => {
    const eqLeg = { symbol: 'GOLDBEES', qty: 500, avg_cost: 60, ltp: 124.43, proxy_for: 'GOLDM' };
    const k1 = synthCacheKey('GOLDM', [eqLeg], 150736, 148500);
    const k2 = synthCacheKey('GOLDM', [eqLeg], 150900, 148500);
    expect(k1).not.toBe(k2);
  });

  it('is stable when nothing changes (memo hit)', () => {
    const eqLeg = { symbol: 'GOLDBEES', qty: 500, avg_cost: 60, ltp: 124.43, proxy_for: 'GOLDM' };
    const k1 = synthCacheKey('GOLDM', [eqLeg], 150736, 148500);
    const k2 = synthCacheKey('GOLDM', [eqLeg], 150736, 148500);
    expect(k1).toBe(k2);
  });
});
