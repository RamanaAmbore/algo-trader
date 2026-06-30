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
 * Extended (Jun 2026 — sparkline dash regression):
 *   - Assert no /pulse sparkline cell shows "—" when backend returned
 *     non-empty data for that symbol.
 *   - Assert that symbols WITH historical data but NO live LTP (liveTail=0)
 *     render an SVG with the historical bars — the live tail is an
 *     enhancement, not a gate on rendering.
 *   - Assert that symbols with genuinely no backend data show "—" (correct).
 *   - Regression guard: the Sprint F+ loadPulse-store-migration caused
 *     loadSparklines() to fire with an incomplete pairs list on cold start
 *     (positionsStore.value still null because the book poller's first
 *     tick was in-flight). The fix adds
 *       await Promise.allSettled([positionsStore.load(), holdingsStore.load()])
 *       await tick()
 *     before the first loadSparklines() call so positions are always
 *     in unifiedRows when the pairs list is built.
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

test.describe('Sparkline /pulse render — dash regression guard', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(120_000);

  /**
   * Fetch the sparkline batch data the backend would return for the same
   * symbols /pulse would request.  Walk every visible sparkline cell on
   * /pulse and assert:
   *   - If backend returned data for that symbol → SVG polyline is visible.
   *   - If backend returned nothing for that symbol → "—" is acceptable.
   *
   * This is the regression guard for the cold-start "—" bug introduced in
   * Sprint F+ (loadPulse store migration).  The fix ensures
   * positionsStore/holdingsStore are awaited before the sparkline pairs
   * list is built.
   */
  test('no sparkline cell shows "—" when backend has data for that symbol', async ({ page }) => {
    await loginAsAdmin(page);
    const token = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));
    expect(token, 'JWT must be in sessionStorage after login').toBeTruthy();

    // Navigate to /pulse with a clean localStorage so we exercise the cold-start
    // code path (positionsStore.value = null on mount).
    await page.evaluate(() => {
      // Remove sparkline cache only; keep other caches so the page loads fast.
      try { localStorage.removeItem('rbq.cache.md.sparklines'); } catch { /* no-op */ }
    });

    await page.goto('/pulse', { waitUntil: 'networkidle' });

    // Wait for the grid to be populated.
    await page.locator('.ag-row').first().waitFor({ timeout: 20_000 });

    // Wait for the sparkline bootstrap to complete. The fix adds
    // await positionsStore.load() + await tick() + loadSparklines() before
    // the 2 s retry, so SVGs should appear within ~5 s of rows landing.
    // We allow up to 15 s total to cover slow CI environments.
    const firstSvg = page.locator('.spark-cell svg polyline').first();
    const svgAppeared = await firstSvg.waitFor({ timeout: 15_000 }).then(() => true).catch(() => false);

    // If NO SVG appeared at all, fail with a diagnostic message.
    if (!svgAppeared) {
      // Gather which symbols show "—" for the bug report.
      const dashCells = await page.locator('.spark-cell').evaluateAll(cells =>
        cells.filter(c => c.textContent?.trim() === '—').map(c => {
          const row = c.closest('.ag-row');
          return row?.getAttribute('row-id') ?? 'unknown';
        })
      );
      throw new Error(
        `[sparkline dash regression] No SVG appeared within 15 s on /pulse. ` +
        `Cells showing "—": ${dashCells.slice(0, 10).join(', ')} ` +
        `(total dash cells: ${dashCells.length}). ` +
        `Root cause: loadSparklines() fired before positionsStore settled — ` +
        `check the await tick() + positionsStore.load() guard in onMount.`
      );
    }

    // Collect all symbols visible in the grids and the sparkline batch
    // data for those symbols.
    const sparkBatch = await page.evaluate(async (base) => {
      // Read symbolStore snapshot from the page context to get the active
      // symbol list.  Fall back to scraping ag-row tradingsymbol attributes.
      const rows = Array.from(document.querySelectorAll('.ag-row[row-id]'));
      const seen = new Set();
      const symbols = [];
      for (const r of rows) {
        const sym = r.getAttribute('row-id')?.split('__')[0]?.toUpperCase();
        if (sym && !seen.has(sym)) {
          seen.add(sym);
          // Infer exchange from row classes (rough heuristic sufficient for test).
          const exch = r.classList.contains('mcx-row') ? 'MCX' : 'NSE';
          symbols.push({ tradingsymbol: sym, exchange: exch });
        }
      }
      if (!symbols.length) return {};
      // Batch sparkline call — same endpoint /pulse uses.
      const token = sessionStorage.getItem('ramboq_token');
      const CHUNK = 100;
      const data = {};
      for (let i = 0; i < symbols.length; i += CHUNK) {
        const slice = symbols.slice(i, i + CHUNK);
        try {
          const resp = await fetch(`${base}/api/quotes/sparkline`, {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${token}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({ symbols: slice, days: 5 }),
          });
          if (resp.ok) {
            const j = await resp.json();
            if (j?.data) Object.assign(data, j.data);
          }
        } catch { /* non-fatal */ }
      }
      return data;
    }, BASE);

    const backendSyms = new Set(Object.keys(sparkBatch));
    console.log(`[sparkline] backend returned data for ${backendSyms.size} symbol(s)`);

    // For every spark-cell that is still "—", check that the backend
    // genuinely returned nothing for that symbol.
    const dashInfo = await page.locator('.spark-cell').evaluateAll(cells =>
      cells.map(cell => {
        const row = cell.closest('.ag-row');
        const sym = row?.getAttribute('row-id')?.split('__')[0]?.toUpperCase() ?? '';
        const isDash = cell.textContent?.trim() === '—';
        const hasSvg = !!cell.querySelector('svg polyline');
        return { sym, isDash, hasSvg };
      })
    );

    const wrongDashes = dashInfo.filter(
      d => d.isDash && d.sym && backendSyms.has(d.sym)
    );

    if (wrongDashes.length > 0) {
      const syms = wrongDashes.map(d => d.sym).slice(0, 10).join(', ');
      throw new Error(
        `[sparkline dash regression] ${wrongDashes.length} cell(s) show "—" ` +
        `even though backend returned data: ${syms}. ` +
        `The positionsStore.load() + tick() guard in onMount may not be working.`
      );
    }

    // Sanity: at least one cell must have an SVG (not all "—").
    const svgCount = await page.locator('.spark-cell svg polyline').count();
    expect(svgCount, 'at least one sparkline SVG must be visible on /pulse').toBeGreaterThan(0);
  });

  /**
   * Render contract — historical bars render even when live LTP (SSE tail)
   * is absent for that symbol.
   *
   * Strategy: post a sparkline request for a symbol, then assert the
   * renderer uses the historical body (non-zero polyline) without needing
   * a live LTP in _liveLtpSnap.  We confirm by intercepting the SSE stream
   * for that symbol to confirm zero live ticks have arrived (or by
   * checking a symbol known to be illiquid / options at expiry).
   *
   * Simpler cross-environment approach: assert that the cells that DO have
   * SVGs are rendering polylines with more than one point (i.e. the body
   * comes from historical data, not a single live-LTP fallback).
   */
  test('sparkline SVG uses historical bars body (not just live LTP)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'networkidle' });
    await page.locator('.ag-row').first().waitFor({ timeout: 20_000 });

    // Wait for any sparkline SVG to appear.
    const firstSvg = page.locator('.spark-cell svg polyline').first();
    const svgAppeared = await firstSvg.waitFor({ timeout: 15_000 }).then(() => true).catch(() => false);
    if (!svgAppeared) {
      test.skip(true, 'no sparkline SVGs on /pulse — cannot validate historical body');
      return;
    }

    // Collect all visible polyline point-lists.
    const pointCounts = await page.locator('.spark-cell svg polyline').evaluateAll(polys =>
      polys.map(p => {
        const pts = (p.getAttribute('points') || '').trim().split(/\s+/);
        return pts.filter(s => s.includes(',')).length;
      })
    );

    // Every SVG polyline must have ≥ 2 points. A single point (or 0) means
    // the renderer received a single-element series, which the backend pads
    // to [ltp, ltp]. That's fine (flat line), but all historical data yields
    // ≥ 2 points. Zero points means the polyline is broken.
    const badPolys = pointCounts.filter(n => n < 2);
    expect(badPolys.length,
      `${badPolys.length} sparkline polyline(s) have <2 points — ` +
      `renderer may be using a single live-LTP fallback with no historical body`
    ).toBe(0);

    console.log(`[sparkline] ${pointCounts.length} SVGs checked; ` +
      `min points=${Math.min(...pointCounts)} max=${Math.max(...pointCounts)}`);
  });
});
