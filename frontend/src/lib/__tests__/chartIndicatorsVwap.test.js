// chartIndicatorsVwap.test.js
//
// 2026-10-31 — operator: "audit all the chart durations and indicators" +
// "if the backend/frontend computes VWAP naively across the whole multi-day
// window instead of per-session, that's a real bug worth finding."
//
// Covers the session-reset fix in frontend/src/lib/chart/indicators.js:vwap().
// Before the fix, VWAP accumulated from bar[0] of whatever window was
// fetched — so the SAME calendar date's VWAP value differed depending on
// which chart range (1M vs 1Y) supplied the bars. This is the live,
// reproducible bug the operator asked to confirm or rule out.

import { describe, it, expect } from 'vitest';
import { vwap } from '$lib/chart/indicators.js';

/** One daily bar, well inside IST trading hours so the session-date math
 * is unambiguous (00:00 UTC = 05:30 IST, after the 08:00 IST − 8h shift
 * lands the session date correctly). */
function dayBar(dateIso, { high, low, close, volume }) {
  return { ts: `${dateIso}T03:45:00+00:00`, high, low, close, volume };
}

describe('vwap() — session reset', () => {
  it('resets at every new IST trading-session date for daily bars', () => {
    const bars = [
      dayBar('2026-10-28', { high: 110, low: 90, close: 100, volume: 1000 }),
      dayBar('2026-10-29', { high: 210, low: 190, close: 200, volume: 500 }),
    ];
    const out = vwap(bars);
    // Each daily bar is its own session — VWAP must equal that bar's own
    // typical price, not a cumulative blend with the prior day.
    expect(out[0].value).toBeCloseTo((110 + 90 + 100) / 3, 6);
    expect(out[1].value).toBeCloseTo((210 + 190 + 200) / 3, 6);
  });

  it('accumulates within a single intraday session (no reset mid-session)', () => {
    const bars = [
      { ts: '2026-10-30T03:45:00+00:00', high: 110, low: 90, close: 100, volume: 1000 },
      { ts: '2026-10-30T03:50:00+00:00', high: 210, low: 190, close: 200, volume: 500 },
    ];
    const out = vwap(bars);
    const tp1 = (110 + 90 + 100) / 3;
    const tp2 = (210 + 190 + 200) / 3;
    const expectedCumulative = (tp1 * 1000 + tp2 * 500) / 1500;
    expect(out[0].value).toBeCloseTo(tp1, 6);
    expect(out[1].value).toBeCloseTo(expectedCumulative, 6);
    // Confirm it is NOT a per-bar reset (i.e. genuinely cumulative within
    // the session) — bar 2's value must differ from its own bare TP.
    expect(out[1].value).not.toBeCloseTo(tp2, 6);
  });

  it('the same calendar date must produce the SAME VWAP regardless of how much history precedes it (1M vs 1Y window parity)', () => {
    // Simulates the exact reported bug: a "1M" fetch starting right at the
    // target date vs. a "1Y" fetch with many prior days. Pre-fix, the two
    // windows disagreed because the accumulator never reset.
    const target = dayBar('2026-10-30', { high: 310, low: 290, close: 300, volume: 777 });

    const oneMonthWindow = [
      dayBar('2026-10-29', { high: 110, low: 90, close: 100, volume: 1000 }),
      target,
    ];
    const oneYearWindow = [
      dayBar('2025-11-01', { high: 10, low: 5, close: 8, volume: 50000 }),
      dayBar('2025-12-15', { high: 20, low: 10, close: 15, volume: 40000 }),
      dayBar('2026-10-29', { high: 110, low: 90, close: 100, volume: 1000 }),
      target,
    ];

    const outMonth = vwap(oneMonthWindow);
    const outYear  = vwap(oneYearWindow);

    const monthValue = outMonth[outMonth.length - 1].value;
    const yearValue  = outYear[outYear.length - 1].value;

    expect(monthValue).toBeCloseTo((310 + 290 + 300) / 3, 6);
    expect(yearValue).toBeCloseTo(monthValue, 6);
  });

  it('returns null for a zero-volume bar (unchanged contract)', () => {
    const bars = [dayBar('2026-10-30', { high: 110, low: 90, close: 100, volume: 0 })];
    const out = vwap(bars);
    expect(out[0].value).toBeNull();
  });
});
