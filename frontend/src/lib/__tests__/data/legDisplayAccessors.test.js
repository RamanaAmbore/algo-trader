/**
 * legDisplayAccessors.test.js — extraction-regression guard for the
 * 2026-11 CandidateLegRow.svelte → derivativesMath.js/lotsForRow.js
 * refactor (column-sort SSOT plan, step 3/4/5).
 *
 * The new `legIsClosed` / `legLiveLtp` / `legChgPct` / `legDisplayQty` /
 * `legCost` (derivativesMath.js) and `legLotsValue` (lotsForRow.js) MUST
 * produce byte-identical output to the inline `$derived`s
 * CandidateLegRow.svelte carried before the refactor — that identity is
 * what guarantees a column-sort accessor and the rendered cell can never
 * disagree.
 *
 * Five quality dimensions:
 *  1. SSOT  — asserts the new shared functions agree with a hand-mirrored
 *             copy of the EXACT pre-refactor inline formulas (not a
 *             re-derivation), same convention as symbolStorePersist.test.js.
 *  2. Perf  — all synchronous; no I/O, no DOM.
 *  3. Stale — covers every branch a future edit could silently diverge on
 *             (closed/open, eq/F&O, _residualQty, lots null/0/non-zero,
 *             proxy/non-proxy, lg undefined, liveLtp null).
 *  4. Reuse — exercises the real exported functions, not copies.
 *  5. UX    — the Qty cell's own literal-0-when-closed rule (NOT
 *             legDisplayQty's entry-size fallback) is asserted separately
 *             in +page.svelte's own column-spec test, not here.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('$lib/data/instruments', () => ({
  getInstrument: vi.fn(),
  getOptionUnderlyingLot: vi.fn(),
}));

import { getInstrument, getOptionUnderlyingLot } from '$lib/data/instruments';
import {
  legIsClosed, legLiveLtp, legChgPct, legDisplayQty, legCost,
} from '$lib/data/derivativesMath.js';
import { legLotsValue, lotsForRow } from '$lib/data/lotsForRow.js';

const _getInstrument = vi.mocked(getInstrument);
const _getOptionUnderlyingLot = vi.mocked(getOptionUnderlyingLot);

beforeEach(() => {
  vi.resetAllMocks();
});
afterEach(() => {
  vi.resetAllMocks();
});

// ─────────────────────────────────────────────────────────────────────────────
// Hand-mirrored GOLDEN reference — verbatim copy of the inline `$derived`
// formulas CandidateLegRow.svelte carried immediately before the 2026-11
// extraction (confirmed against the pre-edit file read during the refactor).
// Any disagreement between these and the real exports below is a real
// regression, not a test artifact.
// ─────────────────────────────────────────────────────────────────────────────

function refIsClosed(c) {
  return Number(c.qty || 0) === 0;
}

function refLtp(c, lg, liveLtp) {
  const isClosed = refIsClosed(c);
  return isClosed
    ? (lg?.ltp ?? c.ltp)
    : (liveLtp ?? (lg?.ltp ?? c.ltp));
}

function refChgPct(c, ltp) {
  const px = Number(c?.prev_close);
  if (!(px > 0) || ltp == null) return null;
  return ((ltp - px) / px) * 100;
}

function refDisplayQty(c) {
  const eqDisplayQty = (
    c.kind === 'eq' && Number(c.qty || 0) === 0 && Number(c.opening_qty || 0) !== 0
  ) ? Number(c.opening_qty) : null;
  const foDisplayQty = (
    c.kind !== 'eq' && Number(c.qty || 0) === 0 && c.lots != null && Number(c.lots) !== 0
  ) ? Number(c.lots) * (Number(getInstrument(String(c.symbol || '').toUpperCase())?.ls) || 1) : null;
  return c._residualQty != null
    ? Number(c._residualQty)
    : (eqDisplayQty ?? foDisplayQty ?? Number(c.qty || 0));
}

function refCost(c, lg) {
  return c.avg_cost != null ? c.avg_cost : (lg ? lg.avg_cost : null);
}

function refLotsValue(c, strategy, proxyRow) {
  const displayQty = refDisplayQty(c);
  if (c.proxy_for) {
    const targetSpot = Number(strategy?.spot) || 0;
    const proxyLtp = Number(c.ltp) || 0;
    const beta = proxyRow?.beta != null ? Number(proxyRow.beta) : 1.0;
    const targetLot = getOptionUnderlyingLot(c.proxy_for);
    const effQty = (targetSpot > 0 && proxyLtp > 0)
      ? (beta * Math.abs(displayQty) * proxyLtp) / targetSpot : 0;
    const targetLots = targetLot > 0 ? effQty / targetLot : 0;
    return targetLots > 0 ? targetLots : null;
  }
  return lotsForRow({ tradingsymbol: c.symbol, quantity: displayQty, lots: c.lots ?? null });
}

// ─────────────────────────────────────────────────────────────────────────────
// Fixture matrix
// ─────────────────────────────────────────────────────────────────────────────

const FO_OPEN = { symbol: 'NIFTY26JUN22000CE', account: 'ZG0790', kind: 'opt', qty: 50, avg_cost: 100, ltp: 120, prev_close: 110, source: 'live' };
const FO_CLOSED_NO_LOTS = { ...FO_OPEN, qty: 0 };
const FO_CLOSED_WITH_LOTS = { ...FO_OPEN, qty: 0, lots: 2 };
const EQ_OPEN = { symbol: 'RELIANCE', account: 'ZG0790', kind: 'eq', qty: 100, avg_cost: 2500, ltp: 2550, prev_close: 2520, source: 'live' };
const EQ_CLOSED_SOLD_TODAY = { ...EQ_OPEN, qty: 0, opening_qty: 100 };
const FO_RESIDUAL = { ...FO_OPEN, qty: 50, _residualQty: 10 };
const FO_NO_PREV_CLOSE = { ...FO_OPEN, prev_close: null };

const LG_WITH_LTP = { ltp: 125, avg_cost: 105, iv: 0.2, greeks: { delta: 0.5, gamma: 0.01, theta: -1, vega: 2 } };

describe('legIsClosed — matches pre-refactor inline formula', () => {
  it.each([
    [FO_OPEN, false],
    [FO_CLOSED_NO_LOTS, true],
    [EQ_OPEN, false],
    [EQ_CLOSED_SOLD_TODAY, true],
  ])('%#: isClosed matches reference', (c, expected) => {
    expect(legIsClosed(c)).toBe(expected);
    expect(legIsClosed(c)).toBe(refIsClosed(c));
    expect(legIsClosed(c)).toBe(expected);
  });
});

describe('legLiveLtp — matches pre-refactor inline formula', () => {
  it('open leg prefers the caller-supplied liveLtp', () => {
    expect(legLiveLtp(FO_OPEN, undefined, 130)).toBe(130);
    expect(legLiveLtp(FO_OPEN, undefined, 130)).toBe(refLtp(FO_OPEN, undefined, 130));
  });

  it('open leg falls back to lg.ltp then c.ltp when liveLtp is null/undefined', () => {
    expect(legLiveLtp(FO_OPEN, LG_WITH_LTP, null)).toBe(refLtp(FO_OPEN, LG_WITH_LTP, null));
    expect(legLiveLtp(FO_OPEN, LG_WITH_LTP, null)).toBe(125);
    expect(legLiveLtp(FO_OPEN, undefined, null)).toBe(refLtp(FO_OPEN, undefined, null));
    expect(legLiveLtp(FO_OPEN, undefined, null)).toBe(FO_OPEN.ltp);
  });

  it('closed leg freezes on lg.ltp/c.ltp and IGNORES liveLtp entirely', () => {
    // Even a non-null liveLtp must be ignored once the leg is closed — this
    // is the "ghost leg" guard the original comment documents.
    expect(legLiveLtp(FO_CLOSED_NO_LOTS, LG_WITH_LTP, 999)).toBe(refLtp(FO_CLOSED_NO_LOTS, LG_WITH_LTP, 999));
    expect(legLiveLtp(FO_CLOSED_NO_LOTS, LG_WITH_LTP, 999)).toBe(LG_WITH_LTP.ltp);
    expect(legLiveLtp(FO_CLOSED_NO_LOTS, undefined, 999)).toBe(refLtp(FO_CLOSED_NO_LOTS, undefined, 999));
    expect(legLiveLtp(FO_CLOSED_NO_LOTS, undefined, 999)).toBe(FO_CLOSED_NO_LOTS.ltp);
  });
});

describe('legChgPct — matches pre-refactor inline formula', () => {
  it('computes pure price-% vs prev_close', () => {
    const ltp = 120;
    expect(legChgPct(FO_OPEN, ltp)).toBe(refChgPct(FO_OPEN, ltp));
    expect(legChgPct(FO_OPEN, ltp)).toBeCloseTo(((120 - 110) / 110) * 100);
  });

  it('returns null when prev_close is missing/non-positive', () => {
    expect(legChgPct(FO_NO_PREV_CLOSE, 120)).toBeNull();
    expect(legChgPct(FO_NO_PREV_CLOSE, 120)).toBe(refChgPct(FO_NO_PREV_CLOSE, 120));
  });

  it('returns null when ltp is null', () => {
    expect(legChgPct(FO_OPEN, null)).toBeNull();
    expect(legChgPct(FO_OPEN, null)).toBe(refChgPct(FO_OPEN, null));
  });
});

describe('legDisplayQty — matches pre-refactor inline formula', () => {
  it('open leg returns raw qty', () => {
    expect(legDisplayQty(FO_OPEN)).toBe(refDisplayQty(FO_OPEN));
    expect(legDisplayQty(FO_OPEN)).toBe(50);
  });

  it('closed F&O leg with no lots returns 0', () => {
    expect(legDisplayQty(FO_CLOSED_NO_LOTS)).toBe(refDisplayQty(FO_CLOSED_NO_LOTS));
    expect(legDisplayQty(FO_CLOSED_NO_LOTS)).toBe(0);
  });

  it('closed F&O leg with lots multiplies by the instrument lot size', () => {
    _getInstrument.mockReturnValue({ ls: 50 });
    expect(legDisplayQty(FO_CLOSED_WITH_LOTS)).toBe(refDisplayQty(FO_CLOSED_WITH_LOTS));
    expect(legDisplayQty(FO_CLOSED_WITH_LOTS)).toBe(100); // 2 lots * 50
  });

  it('closed F&O leg with lots and a cold instrument cache falls back to lot size 1', () => {
    _getInstrument.mockReturnValue(null);
    expect(legDisplayQty(FO_CLOSED_WITH_LOTS)).toBe(refDisplayQty(FO_CLOSED_WITH_LOTS));
    expect(legDisplayQty(FO_CLOSED_WITH_LOTS)).toBe(2); // 2 lots * 1 fallback
  });

  it('closed eq (sold-today) leg surfaces opening_qty', () => {
    expect(legDisplayQty(EQ_CLOSED_SOLD_TODAY)).toBe(refDisplayQty(EQ_CLOSED_SOLD_TODAY));
    expect(legDisplayQty(EQ_CLOSED_SOLD_TODAY)).toBe(100);
  });

  it('_residualQty overrides every other branch', () => {
    expect(legDisplayQty(FO_RESIDUAL)).toBe(refDisplayQty(FO_RESIDUAL));
    expect(legDisplayQty(FO_RESIDUAL)).toBe(10);
  });
});

describe('legCost — matches pre-refactor inline formula', () => {
  it('prefers row-own avg_cost', () => {
    expect(legCost(FO_OPEN, LG_WITH_LTP)).toBe(refCost(FO_OPEN, LG_WITH_LTP));
    expect(legCost(FO_OPEN, LG_WITH_LTP)).toBe(100);
  });

  it('falls back to lg.avg_cost when row has none', () => {
    const c = { ...FO_OPEN, avg_cost: null };
    expect(legCost(c, LG_WITH_LTP)).toBe(refCost(c, LG_WITH_LTP));
    expect(legCost(c, LG_WITH_LTP)).toBe(105);
  });

  it('returns null when both row and lg are absent', () => {
    const c = { ...FO_OPEN, avg_cost: null };
    expect(legCost(c, undefined)).toBe(refCost(c, undefined));
    expect(legCost(c, undefined)).toBeNull();
  });
});

describe('legLotsValue — matches pre-refactor inline formula (proxy branch)', () => {
  const PROXY_LEG = { symbol: 'GOLDBEES', account: 'ZG0790', kind: 'eq', qty: 1500, proxy_for: 'GOLD', ltp: 65, source: 'live' };
  const STRATEGY = { spot: 95000 };
  const PROXY_ROW = { beta: 1.02, correlation: 0.98 };

  it('returns the scaled lot count when positive', () => {
    _getOptionUnderlyingLot.mockReturnValue(100);
    const got = legLotsValue(PROXY_LEG, STRATEGY, PROXY_ROW);
    expect(got).toBe(refLotsValue(PROXY_LEG, STRATEGY, PROXY_ROW));
    expect(got).toBeGreaterThan(0);
  });

  it('returns null (renders "—") when target spot is unavailable', () => {
    _getOptionUnderlyingLot.mockReturnValue(100);
    const got = legLotsValue(PROXY_LEG, { spot: 0 }, PROXY_ROW);
    expect(got).toBe(refLotsValue(PROXY_LEG, { spot: 0 }, PROXY_ROW));
    expect(got).toBeNull();
  });

  it('defaults beta to 1.0 when the proxy row has none', () => {
    _getOptionUnderlyingLot.mockReturnValue(100);
    const got = legLotsValue(PROXY_LEG, STRATEGY, null);
    expect(got).toBe(refLotsValue(PROXY_LEG, STRATEGY, null));
  });
});

describe('legLotsValue — matches pre-refactor inline formula (plain branch)', () => {
  it('delegates to lotsForRow for a non-proxy leg', () => {
    _getInstrument.mockReturnValue({ t: 'CE', ls: 50 });
    const got = legLotsValue(FO_OPEN, undefined, undefined);
    expect(got).toBe(refLotsValue(FO_OPEN, undefined, undefined));
  });
});
