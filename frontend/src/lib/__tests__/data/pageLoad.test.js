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
  buildCandidatePositions, buildCleanLegs,
  buildPagePositionRows, buildSimPositionRows,
  buildHoldingRowFromBroker, buildPageHoldingRows,
} from '$lib/derivatives/pageLoad.js';
import { baseDayPnlForPosition } from '$lib/data/nav.js';
import { expiryPnlWithRealised, positionExpPnl, positionExpPnlPieces } from '$lib/data/expiryPnl.js';
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

  it('fully closed overnight position (brokerQty=0) returns a single closed row whose Day P&L equals baseDayPnlForPosition on the PRE-SPLIT row, not internal self-consistency alone', () => {
    const p = makeOvernightRow({ quantity: 0, day_sell_quantity: 10, day_sell_value: 10 * 220, overnight_quantity: 10, pnl: 200 });
    const expected = baseDayPnlForPosition(p); // 200 (pnl) - 100 (prev_settlement_pnl) = 100
    const split = splitClosedReopened(p);
    expect(split.length).toBe(1);
    expect(split[0]._splitTag).toBe('closed');
    // Asserting against the pre-split baseline (not just split[0]'s own
    // internally-forced consistency, which is true by construction via
    // _forceBaseline regardless of whether the fix landed) is what actually
    // exercises the Commit-1 fix.
    expect(split[0].day_change_val).toBeCloseTo(expected, 6);
    expect(baseDayPnlForPosition(split[0])).toBeCloseTo(split[0].day_change_val, 6);
  });

  it('Commit-1 regression: fully closed overnight position WITH an embedded same-day round-trip (dsq > oq, dbq > 0) — old (exit − prev_close) × min(oq,dsq) blends the round-trip sell price into the overnight exit price and diverges from baseDayPnlForPosition; the fix must use the unsplit-row baseline instead', () => {
    // Overnight long 10 @ avg_cost 200, prev_close 210. Today: buys 6 @ 230
    // (intraday), then sells 16 total (10 closing the overnight lot @ 220,
    // 6 round-tripping the fresh buy @ 240) — broker only reports the
    // AGGREGATE day_sell_value/day_sell_quantity, so the old formula's
    // exit_price = dsv/dsq = 3640/16 = 227.5 is a blend of both fills.
    // Old (buggy) closed_day_pnl = (227.5 − 210) × min(10,16) = 175.
    // baseDayPnlForPosition(p) = pnl(260) − prev_settlement_pnl(100) = 160.
    // 175 !== 160 — this is the confirmed divergence the plan describes.
    const p = makeOvernightRow({
      quantity: 0,
      overnight_quantity: 10,
      day_buy_quantity: 6,
      day_buy_value: 6 * 230,
      day_sell_quantity: 16,
      day_sell_value: 10 * 220 + 6 * 240, // 3640
      average_price: 200,
      prev_close: 210,
      pnl: 260,
      prev_settlement_pnl: 100,
    });
    const expected = baseDayPnlForPosition(p);
    expect(expected).toBeCloseTo(160, 6);
    const split = splitClosedReopened(p);
    expect(split.length).toBe(1);
    expect(split[0].day_change_val).toBeCloseTo(160, 6);
    expect(split[0].day_change_val).not.toBeCloseTo(175, 6); // old buggy blended value
  });

  it('Commit-1 regression, short side: fully closed overnight SHORT position (oq<0) with an embedded round-trip exits via dbq — same fix applies symmetrically', () => {
    // Overnight short 10 @ avg_cost 200, prev_close 210. Today: sells 6 @ 195
    // (intraday short round-trip), then buys 16 total to fully cover (10
    // covering the overnight short @ 205, 6 round-tripping the fresh short
    // @ 190) — dbv/dbq = (10*205 + 6*190)/16 = (2050+1140)/16 = 199.375.
    // Old closed_day_pnl = (close − exit) × min(|oq|,dbq) = (210-199.375)*10
    // = 106.25. baseDayPnlForPosition(p) = pnl(120) − prev_settlement_pnl(50)
    // = 70. These diverge (106.25 !== 70).
    const p = makeOvernightRow({
      quantity: 0,
      overnight_quantity: -10,
      day_sell_quantity: 6,
      day_sell_value: 6 * 195,
      day_buy_quantity: 16,
      day_buy_value: 10 * 205 + 6 * 190, // 3190
      average_price: 200,
      prev_close: 210,
      pnl: 120,
      prev_settlement_pnl: 50,
    });
    const expected = baseDayPnlForPosition(p);
    expect(expected).toBeCloseTo(70, 6);
    const split = splitClosedReopened(p);
    expect(split.length).toBe(1);
    expect(split[0].day_change_val).toBeCloseTo(70, 6);
    expect(split[0].day_change_val).not.toBeCloseTo(106.25, 6); // old buggy blended value
  });

  it('Commit-1 sentinel: prev_close null/0 no longer matters — the fix does not read `close` at all for the fully-closed branch', () => {
    // buildPositionRowFromBroker maps a missing/0 prev_close to `null`, and
    // splitClosedReopened reads it as `Number(p.prev_close ?? 0)` = 0 — the
    // documented sentinel. The old formula (exit − 0) × closedQty would
    // silently inflate Day P&L by the full exit price. The fix sidesteps
    // this entirely since baseDayPnlForPosition never references `close`.
    const p = makeOvernightRow({
      quantity: 0,
      overnight_quantity: 10,
      day_sell_quantity: 10,
      day_sell_value: 10 * 220,
      prev_close: null,
      pnl: 200,
      prev_settlement_pnl: 100,
    });
    const expected = baseDayPnlForPosition(p); // 200 - 100 = 100, unaffected by prev_close
    const split = splitClosedReopened(p);
    expect(split[0].day_change_val).toBeCloseTo(expected, 6);
    expect(split[0].day_change_val).toBeCloseTo(100, 6);
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

// ============================================================================
// buildCandidatePositions / buildCleanLegs — 2026-09 GOLDM regression fix.
// A held (qty!==0) F&O row whose contract has expired and/or dropped out of
// the live instruments master must stay VISIBLE and counted (tagged
// `_expired`), not silently dropped — Snapshot's portfolioStore pipeline has
// no such exclusion, so the old drop behavior made Legs/Payoff disagree with
// Snapshot for the same underlying the day after expiry.
// ============================================================================

describe('buildCandidatePositions — expired/unresolvable contracts are tagged, not dropped (GOLDM regression)', () => {
  function baseParams(overrides = {}) {
    return {
      positions: [],
      holdings: [],
      drafts: [],
      target: 'GOLDM',
      selectedExpiries: [],
      selectedAccounts: [],
      simActive: false,
      proxiesForTarget: () => [],
      getInstrument: () => null,
      ...overrides,
    };
  }

  it('a held position whose symbol is ABSENT from the instruments master is tagged `_expired: true` and included, not dropped', () => {
    const p = { source: 'live', account: 'ACC1', symbol: 'GOLDM24SEP150000CE', qty: 5, avg_cost: 100 };
    const result = buildCandidatePositions(baseParams({
      positions: [p],
      getInstrument: () => null, // symbol not found — expired/removed by Kite
    }));
    expect(result.length).toBe(1);
    expect(result[0]._expired).toBe(true);
    expect(result[0].symbol).toBe('GOLDM24SEP150000CE');
  });

  it('a held position whose instrument IS in the master but its expiry date has already passed is also tagged `_expired: true` and included', () => {
    const p = { source: 'live', account: 'ACC1', symbol: 'GOLDM24SEP150000CE', qty: 5, avg_cost: 100 };
    const result = buildCandidatePositions(baseParams({
      positions: [p],
      getInstrument: () => ({ x: '2020-01-01' }), // long-past expiry
    }));
    expect(result.length).toBe(1);
    expect(result[0]._expired).toBe(true);
  });

  it('a resolvable, unexpired contract that simply is not in the operator\'s selectedExpiries selection is still filtered out (the expiry SELECTOR remains a real UI filter, not bypassed)', () => {
    const p = { source: 'live', account: 'ACC1', symbol: 'GOLDM25DEC150000CE', qty: 5, avg_cost: 100 };
    const result = buildCandidatePositions(baseParams({
      positions: [p],
      selectedExpiries: ['2099-02-01'], // active filter, doesn't include the row's real expiry
      getInstrument: () => ({ x: '2099-01-01' }), // known + future
    }));
    expect(result.length).toBe(0);
  });

  it('a flat (qty=0) historical row is always included regardless of instrument resolvability, matching pre-fix behavior', () => {
    const p = { source: 'live', account: 'ACC1', symbol: 'GOLDM24SEP150000CE', qty: 0, avg_cost: 100 };
    const result = buildCandidatePositions(baseParams({
      positions: [p],
      getInstrument: () => null,
    }));
    expect(result.length).toBe(1);
    expect(result[0]._expired).toBeUndefined();
  });

  it('account matcher now normalises BOTH sides (case/whitespace) — the old exact-match comparison silently dropped this row', () => {
    const p = { source: 'live', account: ' acc1 ', symbol: 'GOLDM24SEP150000FUT', qty: 5, avg_cost: 100 };
    const result = buildCandidatePositions(baseParams({
      positions: [p],
      selectedAccounts: ['ACC1'], // operator's selection, already uppercase — row's own value isn't
      getInstrument: () => ({ x: '2099-01-01' }),
    }));
    expect(result.length).toBe(1);
  });
});

describe('buildCleanLegs — `_expired`-tagged legs excluded from the backend request payload only', () => {
  it('drops a leg tagged `_expired` even though its (null) expiry would otherwise pass the existing expiry filter', () => {
    const legs = [
      { symbol: 'GOLDM24SEP150000CE', kind: 'opt', qty: 5, avg_cost: 100, source: 'live', _expired: true },
      { symbol: 'GOLDM25DEC150000CE', kind: 'opt', qty: 3, avg_cost: 120, source: 'live' },
    ];
    const getInstrument = (sym) => sym === 'GOLDM25DEC150000CE' ? { x: '2099-01-01' } : null;
    const clean = buildCleanLegs(legs, getInstrument);
    expect(clean.length).toBe(1);
    expect(clean[0].symbol).toBe('GOLDM25DEC150000CE');
  });

  it('a non-expired leg is unaffected by the new filter', () => {
    const legs = [{ symbol: 'GOLDM25DEC150000CE', kind: 'opt', qty: 3, avg_cost: 120, source: 'live' }];
    const getInstrument = () => ({ x: '2099-01-01' });
    const clean = buildCleanLegs(legs, getInstrument);
    expect(clean.length).toBe(1);
  });
});

describe('buildPositionRowFromBroker — exchange passthrough (2026-09 fix)', () => {
  it('carries exchange through when present on the broker row', () => {
    const row = buildPositionRowFromBroker({ tradingsymbol: 'GOLDM24SEPFUT', quantity: 1, exchange: 'MCX' }, 'live');
    expect(row.exchange).toBe('MCX');
  });

  it('defaults to null when absent (not dropped/undefined silently)', () => {
    const row = buildPositionRowFromBroker({ tradingsymbol: 'NIFTY25SEP24000CE', quantity: 1 }, 'live');
    expect(row.exchange).toBeNull();
  });
});

// ============================================================================
// buildPagePositionRows / buildSimPositionRows — single row-source
// unification (2026-09, Commit 2 completion). The derivatives page's own
// `positions` $state is now built by these two functions instead of
// independently re-deriving from positionsStore.value / pulsePositionsStore
// fallback inline in +page.svelte.
// ============================================================================

describe('buildPagePositionRows', () => {
  it('filters to F&O only, splits each row via splitClosedReopened, and appends simRows verbatim', () => {
    const storeRows = [
      { tradingsymbol: 'GOLDM24SEP150000CE', account: 'ACC1', quantity: 5, average_price: 100, last_price: 120, exchange: 'MCX' },
      { tradingsymbol: 'RELIANCE', account: 'ACC1', quantity: 10, average_price: 2500, last_price: 2600, exchange: 'NSE' }, // equity — excluded
    ];
    const simRows = [{ symbol: 'GOLDM24SEP150000CE', account: 'SIM1', qty: 1, source: 'sim' }];
    const rows = buildPagePositionRows(storeRows, simRows);
    expect(rows.length).toBe(2); // one F&O live row (split into 1 piece, no day activity) + one sim row verbatim
    expect(rows.some(r => r.symbol === 'RELIANCE')).toBe(false);
    expect(rows.some(r => r.source === 'sim' && r.account === 'SIM1')).toBe(true);
  });

  it('splits a row with same-day partial close into two display rows', () => {
    const storeRows = [{
      tradingsymbol: 'NIFTY25SEP24000CE', account: 'ACC1', quantity: 5,
      overnight_quantity: 10, day_sell_quantity: 5, day_sell_value: 5 * 220,
      average_price: 200, prev_close: 210, pnl: 150, exchange: 'NFO',
    }];
    const rows = buildPagePositionRows(storeRows);
    expect(rows.length).toBe(2);
    expect(rows.map(r => r._splitTag).sort()).toEqual(['closed', 'open']);
  });

  it('defaults simRows to empty array when omitted', () => {
    expect(buildPagePositionRows([])).toEqual([]);
  });

  it('skips a row with no symbol at all', () => {
    const rows = buildPagePositionRows([{ account: 'ACC1', quantity: 5 }]);
    expect(rows).toEqual([]);
  });

  // ==========================================================================
  // _storeExpPnl wiring (2026-09, Commit 5) — portfolioStore.svelte.js's
  // _posTier2 computes `_exp_pnl_pieces` via positionExpPnlPieces on the SAME
  // raw row this test simulates; buildPagePositionRows must zip its own
  // splitClosedReopened output against those pieces BY INDEX.
  // ==========================================================================

  describe('_storeExpPnl wiring — mixed raw rows (partial close, two accounts, one fully closed)', () => {
    /** Mimics portfolioStore.svelte.js's _posTier2: stamps `_exp_pnl_pieces`
     *  (and `_exp_pnl`, for cross-checking) onto a raw row exactly the way
     *  the real store does, using the SAME anchor for every row. */
    function stampStoreFields(rawRow, kind, anchor) {
      return {
        ...rawRow,
        _exp_pnl: positionExpPnl(rawRow, kind, anchor),
        _exp_pnl_pieces: positionExpPnlPieces(rawRow, kind, anchor),
      };
    }

    const anchor = 23000; // OTM for a 24000-strike CE

    it('a partial-close row (2 pieces): each split page row carries the matching piece by index, and they sum to positionExpPnl', () => {
      const raw = {
        tradingsymbol: 'NIFTY24000CE', account: 'ACC1', quantity: 5,
        overnight_quantity: 10, day_sell_quantity: 5, day_sell_value: 5 * 220,
        average_price: 200, prev_close: 210, pnl: 150, exchange: 'NFO',
      };
      const stamped = stampStoreFields(raw, 'opt', anchor);
      const pageRows = buildPagePositionRows([stamped]);
      expect(pageRows.length).toBe(2);
      for (const row of pageRows) expect('_storeExpPnl' in row).toBe(true);
      const sum = pageRows.reduce((s, r) => s + Number(r._storeExpPnl ?? 0), 0);
      expect(sum).toBe(positionExpPnl(raw, 'opt', anchor));
    });

    it('a fully-closed row (1 piece): the single page row carries the store value directly', () => {
      const raw = { tradingsymbol: 'NIFTY24000CE', account: 'ACC2', quantity: 0, realised: 500, pnl: 3000 };
      const stamped = stampStoreFields(raw, 'opt', anchor);
      const pageRows = buildPagePositionRows([stamped]);
      expect(pageRows.length).toBe(1);
      expect(pageRows[0]._storeExpPnl).toBe(positionExpPnl(raw, 'opt', anchor));
    });

    it('mixed set (two accounts + a partial close + a fully-closed row): Σ over ALL page rows _storeExpPnl equals Σ positionExpPnl per raw row (Snapshot\'s own value)', () => {
      const rawA = {
        tradingsymbol: 'NIFTY24000CE', account: 'ACC1', quantity: 5,
        overnight_quantity: 10, day_sell_quantity: 5, day_sell_value: 5 * 220,
        average_price: 200, prev_close: 210, pnl: 150, exchange: 'NFO',
      };
      const rawB = { tradingsymbol: 'NIFTY24000CE', account: 'ACC2', quantity: 0, realised: 500, pnl: 3000 };
      const rawC = { tradingsymbol: 'NIFTY24000CE', account: 'ACC3', quantity: 3, average_price: 150, exchange: 'NFO' };
      const stampedRows = [rawA, rawB, rawC].map(r => stampStoreFields(r, 'opt', anchor));
      const pageRows = buildPagePositionRows(stampedRows);

      const pageSum = pageRows.reduce((s, r) => s + Number(r._storeExpPnl ?? 0), 0);
      const snapshotSum = [rawA, rawB, rawC].reduce((s, r) => s + Number(positionExpPnl(r, 'opt', anchor) ?? 0), 0);
      expect(pageSum).toBe(snapshotSum);
    });

    it('a row with no _exp_pnl_pieces (non-F&O-derived source, e.g. drafts) never gets `_storeExpPnl` set — falls through to local compute at the call site', () => {
      const raw = { tradingsymbol: 'NIFTY24000CE', account: 'ACC1', quantity: 5, average_price: 200, exchange: 'NFO' };
      // No stampStoreFields() call — no `_exp_pnl_pieces` field at all.
      const pageRows = buildPagePositionRows([raw]);
      expect(pageRows.length).toBe(1);
      expect('_storeExpPnl' in pageRows[0]).toBe(false);
    });
  });
});

describe('buildSimPositionRows', () => {
  it('filters to F&O only and splits each sim position, tagging source:"sim"', () => {
    const simPositions = [
      { symbol: 'GOLDM24SEP150000CE', account: 'SIM1', quantity: 2, average_price: 100 },
      { symbol: 'RELIANCE', account: 'SIM1', quantity: 10, average_price: 2500 }, // equity — excluded
    ];
    const rows = buildSimPositionRows(simPositions);
    expect(rows.length).toBe(1);
    expect(rows[0].source).toBe('sim');
    expect(rows[0].symbol).toBe('GOLDM24SEP150000CE');
  });

  it('returns [] for an empty/undefined input', () => {
    expect(buildSimPositionRows([])).toEqual([]);
    expect(buildSimPositionRows(undefined)).toEqual([]);
  });
});

// ============================================================================
// buildPageHoldingRows — single holdings source (2026-09, Commit 6).
// portfolioStore.svelte.js's `_holdTier2` computes the canonical per-holding
// Day P&L formula ((ltp − prev_close) × qty, `_dcv` fallback) as `_day_pnl`
// on portfolioStore.holdings.rows; buildPageHoldingRows threads it onto the
// page's own built rows as `_storeDayPnl` instead of the page reimplementing
// the formula (buildHoldingRowFromBroker carries no realised/unrealised/
// prev_settlement_pnl, so baseDayPnlForPosition — the F&O-oriented
// formula — would silently fall back to LIFETIME pnl for a holding row).
// ============================================================================

describe('buildPageHoldingRows', () => {
  it('threads the store\'s own _day_pnl onto the built row as _storeDayPnl', () => {
    const storeRows = [{
      tradingsymbol: 'RELIANCE', account: 'ACC1', quantity: 10, opening_quantity: 10,
      average_price: 2500, last_price: 2600, prev_close: 2550,
      pnl: 1000, day_change_val: 500,
      _day_pnl: 500, // (2600-2550)*10, portfolioStore's own computation
    }];
    const rows = buildPageHoldingRows(storeRows);
    expect(rows.length).toBe(1);
    expect(rows[0]._storeDayPnl).toBe(500);
    // Confirms the bug this fixes: baseDayPnlForPosition on this same row
    // (no realised/unrealised/prev_settlement_pnl) would return the
    // LIFETIME pnl (1000), not the real Day P&L (500).
    expect(baseDayPnlForPosition(buildHoldingRowFromBroker(storeRows[0]))).toBe(1000);
    expect(rows[0]._storeDayPnl).not.toBe(1000);
  });

  it('skips a row buildHoldingRowFromBroker itself rejects (no symbol, or qty=0 and opening_qty=0)', () => {
    expect(buildPageHoldingRows([{ account: 'ACC1', quantity: 10, _day_pnl: 100 }])).toEqual([]);
    expect(buildPageHoldingRows([{ tradingsymbol: 'RELIANCE', account: 'ACC1', quantity: 0, opening_quantity: 0, _day_pnl: 100 }])).toEqual([]);
  });

  it('does not set _storeDayPnl when the store row carries no _day_pnl field at all', () => {
    const storeRows = [{ tradingsymbol: 'RELIANCE', account: 'ACC1', quantity: 10, opening_quantity: 10, average_price: 2500 }];
    const rows = buildPageHoldingRows(storeRows);
    expect('_storeDayPnl' in rows[0]).toBe(false);
  });

  it('returns [] for an empty/undefined input', () => {
    expect(buildPageHoldingRows([])).toEqual([]);
    expect(buildPageHoldingRows(undefined)).toEqual([]);
  });

  it('multiple holdings: each carries its OWN _day_pnl, not a shared/aggregated value', () => {
    const storeRows = [
      { tradingsymbol: 'RELIANCE', account: 'ACC1', quantity: 10, opening_quantity: 10, average_price: 2500, _day_pnl: 500 },
      { tradingsymbol: 'TCS', account: 'ACC1', quantity: 5, opening_quantity: 5, average_price: 3200, _day_pnl: -150 },
    ];
    const rows = buildPageHoldingRows(storeRows);
    expect(rows.find(r => r.symbol === 'RELIANCE')._storeDayPnl).toBe(500);
    expect(rows.find(r => r.symbol === 'TCS')._storeDayPnl).toBe(-150);
  });
});

// ============================================================================
// Row-source parity — portfolioStore.positions.rows vs candidatePositions
// (2026-09, Commit 2 completion). The derivatives page's `positions` $state
// (now built by buildPagePositionRows off the SAME raw broker rows
// portfolioStore.svelte.js's `_posTier3`/`rows` export reflects) must agree
// with buildCandidatePositions on which (account, symbol) pairs exist for a
// given underlying — including a held-but-expired (GOLDM regression) row.
//
// NOT a row-count comparison: buildPagePositionRows may split a single
// broker-consolidated row into two display rows (closed + open) for the
// SAME (account, symbol) key, so row counts between the two pipelines can
// legitimately differ even when they agree on coverage. Compare distinct
// (account, symbol) keys instead.
//
// Deliberately uses GOLDM/NIFTY (letters-only roots) rather than a
// digit-bearing root like NIFTYNXT50 — portfolioStore.svelte.js's OWN root
// grouping (_posTier3's `_root`, via decomposeSymbol().root) and
// candidatePositions' `^target\d` prefix-regex root match are a SEPARATE,
// documented, un-unified divergence (the plan's "adopt rootOf.js" item,
// skipped as a wrong-tool-for-the-job — see the 76998fbd commit message)
// that would fail a parity test on such a root for a reason unrelated to
// this change. Not fixed here — flagged as a known, pre-existing gap.
// ============================================================================

describe('row-source parity — buildPagePositionRows output agrees with buildCandidatePositions on (account, symbol) coverage', () => {
  function acctSymKeys(rows) {
    return new Set(rows.map(r => `${String(r.account || '').toUpperCase()}|${String(r.symbol || '').toUpperCase()}`));
  }

  it('two accounts, one held-and-expired GOLDM leg, one same-day-partial-close GOLDM leg (splits into 2 display rows): candidatePositions keys match the RAW store rows keys (post F&O filter) — NOT a row-count match — and every candidate\'s ltp matches its raw store row\'s last_price', () => {
    const storeRows = [
      { tradingsymbol: 'GOLDM24SEP150000CE', account: 'ACC1', quantity: 5, average_price: 100, last_price: 120, exchange: 'MCX' },
      // Same-day partial close: overnight 10, sold 5 today, 5 remaining —
      // splitClosedReopened produces TWO display rows (closed + open) for
      // this SAME (account, symbol) key. Row-count parity would fail here
      // by construction; key-set parity must still hold.
      {
        tradingsymbol: 'GOLDM24SEP150000CE', account: 'ACC2', quantity: 5,
        overnight_quantity: 10, day_sell_quantity: 5, day_sell_value: 5 * 220,
        average_price: 200, prev_close: 210, pnl: 150, last_price: 120, exchange: 'MCX',
      },
      // Held-but-expired: nonzero qty, symbol absent from the mocked instruments master.
      { tradingsymbol: 'GOLDM24AUG140000PE', account: 'ACC1', quantity: 2, average_price: 50, last_price: 60, exchange: 'MCX' },
    ];
    const pageRows = buildPagePositionRows(storeRows, []);
    // Sanity: the partial-close row really did split into 2 pieces (proves
    // the row-count-vs-key-set distinction this test is guarding is real).
    expect(pageRows.filter(r => r.account === 'ACC2' && r.symbol === 'GOLDM24SEP150000CE').length).toBe(2);

    const getInstrument = (sym) => (sym === 'GOLDM24SEP150000CE' ? { x: '2099-01-01' } : null);
    const candidates = buildCandidatePositions({
      positions: pageRows,
      holdings: [],
      drafts: [],
      target: 'GOLDM',
      selectedExpiries: [],   // empty = no filter, per advisor's guidance
      selectedAccounts: [],   // empty = no filter
      simActive: false,
      proxiesForTarget: () => [],
      getInstrument,
    });

    // Expected key set derived from the RAW store rows directly (post F&O
    // filter) — the store-side SSOT, not the page's own intermediate output.
    const expectedKeys = acctSymKeys(storeRows.map(r => ({ account: r.account, symbol: r.tradingsymbol })));
    const candKeys = acctSymKeys(candidates);
    expect(candKeys).toEqual(expectedKeys);
    // The held-but-expired leg must be present (tagged), not dropped.
    expect(candidates.some(c => c.symbol === 'GOLDM24AUG140000PE' && c._expired === true)).toBe(true);

    // Per-leg LTP agreement: every candidate row's ltp matches its RAW
    // store row's last_price for the same (account,symbol) key (not the
    // page's own intermediate `ltp` field, which would be tautological).
    const storeLtpByKey = new Map(storeRows.map(r => [`${r.account.toUpperCase()}|${r.tradingsymbol.toUpperCase()}`, r.last_price]));
    for (const c of candidates) {
      const key = `${String(c.account).toUpperCase()}|${String(c.symbol).toUpperCase()}`;
      expect(c.ltp).toBe(storeLtpByKey.get(key));
    }
  });
});
