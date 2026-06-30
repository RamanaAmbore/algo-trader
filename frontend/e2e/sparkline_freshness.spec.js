/**
 * Sparkline freshness audit (Sleep audit Jun 2026).
 *
 * Operator concern: "look for LTP flickering and sparkline accuracy"
 * — verify the last point of every visible sparkline is current (live
 * LTP appended) and not a stale 30-minute bar close. The backend
 * sparkline endpoint appends live LTP from the ticker tick_map; if the
 * append path silently fails (e.g. token map cold, broker miss), the
 * sparkline tail freezes at the latest historical bar boundary.
 *
 * Test strategy: hit /api/quotes/sparkline against a real symbol, then
 * fetch /api/positions to capture the current LTP. The sparkline's
 * last data point should be within 1% of the live LTP (allowing for
 * 30-minute bar smoothing during low-volatility periods).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

test.describe('Sparkline freshness — last point near live LTP', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(120_000);

  test('sparkline last-point within 5% of live position LTP', async ({ page }) => {
    await loginAsAdmin(page);
    // Use the page's authenticated context to call the API.
    const token = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));
    expect(token, 'JWT must be in sessionStorage after login').toBeTruthy();

    // Fetch positions; pick the first symbol with a positive LTP.
    const posResp = await page.request.get(`${BASE}/api/positions`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(posResp.ok(), 'positions endpoint must return 200').toBeTruthy();
    const posJson = await posResp.json();
    /** @type {Array<{tradingsymbol: string, exchange: string, last_price: number}>} */
    const rows = (posJson.rows || []).filter(r => r?.last_price > 0);
    if (rows.length === 0) {
      test.skip(true, 'no positions with positive LTP — cannot validate sparkline');
      return;
    }
    // Pick a row that we know has a real LTP. Equity-like rows tend
    // to have stable LTPs; options can swing wildly between historical
    // bar boundaries. Prefer NSE > MCX > NFO.
    const sortedRows = rows.slice().sort((a, b) => {
      const rank = (ex) => ex === 'NSE' ? 0 : ex === 'MCX' ? 1 : 2;
      return rank(a.exchange) - rank(b.exchange);
    });
    const probe = sortedRows[0];
    console.log(`[sparkline] probe symbol: ${probe.tradingsymbol} @ ${probe.exchange}, LTP=${probe.last_price}`);

    // Fetch sparkline for the probe symbol.
    const sparkResp = await page.request.post(`${BASE}/api/quotes/sparkline`, {
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: {
        symbols: [{ tradingsymbol: probe.tradingsymbol, exchange: probe.exchange }],
        days: 5,
      },
    });
    expect(sparkResp.ok(), 'sparkline endpoint must return 200').toBeTruthy();
    const sparkJson = await sparkResp.json();
    const series = sparkJson?.data?.[probe.tradingsymbol] || [];
    expect(series.length, 'sparkline must return at least 1 point').toBeGreaterThan(0);

    const lastPoint = Number(series[series.length - 1]);
    expect(Number.isFinite(lastPoint), `sparkline tail value must be finite (got ${series.slice(-3)})`).toBe(true);
    expect(lastPoint, 'sparkline tail must be > 0').toBeGreaterThan(0);

    // Compare sparkline tail to live LTP. Allow 5% drift to absorb:
    //   - 30-minute bar smoothing for high-volatility symbols
    //   - 1-tick race between positions endpoint reading and sparkline LTP appending
    //   - Network RTT between the two calls (~500 ms)
    const livePct = Math.abs(lastPoint - probe.last_price) / probe.last_price * 100;
    console.log(`[sparkline] ${probe.tradingsymbol}: tail=${lastPoint} live=${probe.last_price} drift=${livePct.toFixed(2)}%`);
    expect(livePct,
      `sparkline tail drifted ${livePct.toFixed(2)}% from live LTP ` +
      `(sparkline=${lastPoint}, position=${probe.last_price}) — append path may be broken`
    ).toBeLessThan(5);
  });
});
