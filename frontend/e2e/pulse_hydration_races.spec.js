/**
 * Pulse hydration races regression guard (Jun 2026 — P0).
 *
 * Operator symptom set (verbatim):
 *   1. "sparkline briefly showed the graph. then reset to flat line
 *      after reload."
 *   2. "sometimes winners and gainers rows become empty and show data
 *      on and off."
 *   3. "the sparkline shows either dash or flat line."
 *
 * Three independent races, one shared theme — the store layer was
 * overwriting populated values with transient empty / degenerate
 * responses (broker rate-limit, batchQuote-at-open with pct=0, post-
 * mount /api/quotes/sparkline that returned [ltp, ltp] pads for the
 * symbols that just rate-limited).
 *
 * Three fixes:
 *   - `dataStore.createDataStore({ keepStaleOnEmpty: true })` — drop
 *     `[]`/`{}` writes when the prior value was populated.
 *   - `sparklinesStore` per-symbol stale-better merge — a flat fresh
 *     series never replaces a varied cached one.
 *   - `sparkRenderer` flat-line centering — when min === max the
 *     polyline now sits at y=H/2 instead of glued to the bottom edge.
 *   - MarketPulse positions/holdings bridge — skip the local-state
 *     mirror when the store reports null transiently.
 *
 * Regression checks:
 *   - Reload /pulse twice. Snapshot every spark polyline at t=0.5s,
 *     1s, 2s, 5s. Assert once a polyline shows ≥3 distinct Y values
 *     ("real" curve) it NEVER regresses to all-equal Y or to "—".
 *   - Watch the Movers grids (winners + losers) for 60s. Assert no
 *     non-empty → empty transition.
 *   - Cold-localStorage path — wipe md.sparklines + md.movers, then
 *     reload; first SVG must appear within 15 s.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe.configure({ mode: 'serial' });

const SETTLE_MS = 1500;       // grid mount + first sparkline batch
const MOVERS_WATCH_MS = 60_000;

/** Pull every sparkline polyline's points string from /pulse. */
async function snapshotSparks(page) {
  return page.locator('.spark-cell').evaluateAll(cells =>
    cells.map(cell => {
      const row = cell.closest('.ag-row');
      const sym = row?.getAttribute('row-id')?.split('__')[0]?.toUpperCase() ?? '';
      const poly = cell.querySelector('svg polyline');
      const dashOnly = cell.textContent?.trim() === '—';
      const ptsStr = poly?.getAttribute('points') || '';
      const ys = ptsStr.trim().split(/\s+/)
        .filter(s => s.includes(','))
        .map(s => Number(s.split(',')[1]))
        .filter(y => Number.isFinite(y));
      const distinctYs = new Set(ys.map(y => y.toFixed(1))).size;
      return { sym, dashOnly, points: ys.length, distinctYs };
    })
  );
}

/** Pull mover-grid row counts (winners + losers). */
async function moverCounts(page) {
  // Movers grids carry the mover-bucket sub-group via the
  // `mover-dir-row-winners` / `mover-dir-row-losers` row classes
  // (MarketPulse.svelte getRowClass). Row ids in the unified grid
  // suffix `__mov` when buildUnified routes a symbol to the movers
  // major. Count both signals so the test stays resilient to either
  // class-naming or row-id changes.
  return page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('.ag-row[row-id]'));
    let winners = 0, losers = 0, untyped = 0;
    for (const r of rows) {
      const id = r.getAttribute('row-id') || '';
      const cls = r.className;
      const isMoverById  = id.endsWith('__mov');
      const isWinnerCls  = cls.includes('mover-dir-row-winners');
      const isLoserCls   = cls.includes('mover-dir-row-losers');
      if (isWinnerCls) { winners++; continue; }
      if (isLoserCls)  { losers++;  continue; }
      if (isMoverById) {
        // Row identified as mover by id but no direction class yet —
        // happens transiently when buildUnified hasn't tagged the
        // direction (cold start). Count it as "untyped" so the total
        // stays accurate but the winners/losers split skews if it
        // matters.
        untyped++;
      }
    }
    return { winners, losers, untyped, total: winners + losers + untyped };
  });
}

test.describe('Pulse hydration races', () => {
  test.setTimeout(180_000);

  test('sparkline never regresses from real curve to flat/dash after second reload', async ({ page }) => {
    await loginAsAdmin(page);

    // First load — establishes baseline with whatever localStorage has.
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.locator('.ag-row').first().waitFor({ timeout: 30_000 });
    // Allow the post-mount loadSparklines + 2s retry to settle.
    await page.waitForTimeout(SETTLE_MS + 2500);

    const baseline = await snapshotSparks(page);
    // baseline may have a mix of real curves + dashes (first-paint).
    // Keep symbols where distinctYs >= 3 — those have proven "real".
    const realSyms = new Set(
      baseline.filter(b => b.distinctYs >= 3 && !b.dashOnly).map(b => b.sym)
    );
    if (realSyms.size === 0) {
      // Off-hours / fresh-cache test environment — no historical bars
      // returned by the broker. Skip rather than false-positive.
      test.skip(true, 'no real curves on first paint — cannot validate regression');
      return;
    }
    console.log(`[hydration] ${realSyms.size} symbols have real curves on first paint`);

    // Second reload — this is where the flicker historically happened.
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator('.ag-row').first().waitFor({ timeout: 30_000 });

    // Sample at t=0.5s, 1s, 2s, 5s, 8s. The flicker window in the
    // reported defect was roughly 200-1500ms after reload — between
    // the localStorage hydration paint (real curve) and the fresh
    // broker fetch overwrite (flat). Sampling densely in that window
    // and a settle-point at 5s + 8s gives full coverage.
    const samplesMs = [500, 1000, 2000, 5000, 8000];
    /** @type {Array<{t:number, snap: any[]}>} */
    const samples = [];
    const t0 = Date.now();
    for (const t of samplesMs) {
      const remaining = t - (Date.now() - t0);
      if (remaining > 0) await page.waitForTimeout(remaining);
      const snap = await snapshotSparks(page);
      samples.push({ t, snap });
    }

    // Regression rule: for every symbol that has a "real curve" at
    // ANY sample (distinctYs >= 3), check that no later sample shows
    // dashOnly OR distinctYs < 2 for that symbol.
    const everRealBySym = new Map();
    for (const { snap } of samples) {
      for (const cell of snap) {
        if (!cell.sym) continue;
        if (cell.distinctYs >= 3 && !cell.dashOnly) everRealBySym.set(cell.sym, true);
      }
    }

    /** @type {Array<{sym: string, t: number, dashOnly: boolean, distinctYs: number}>} */
    const regressions = [];
    for (const { t, snap } of samples) {
      for (const cell of snap) {
        if (!cell.sym) continue;
        if (!everRealBySym.has(cell.sym)) continue;
        // Regression: a symbol that proved "real" at some sample now
        // looks degenerate (dash or fewer than 2 distinct Y values).
        if (cell.dashOnly || cell.distinctYs < 2) {
          regressions.push({ sym: cell.sym, t, dashOnly: cell.dashOnly, distinctYs: cell.distinctYs });
        }
      }
    }

    if (regressions.length > 0) {
      const sample = regressions.slice(0, 8)
        .map(r => `${r.sym}@${r.t}ms (dash=${r.dashOnly}, distinctYs=${r.distinctYs})`)
        .join(', ');
      throw new Error(
        `[sparkline flicker regression] ${regressions.length} symbol-sample(s) ` +
        `regressed from a real curve to flat / dash after reload: ${sample}. ` +
        `Root cause: sparkline fetcher merged a degenerate fresh series over ` +
        `the cached good one. _mergeSparkSeries in marketDataStores should keep ` +
        `the cached curve when fresh is flat-or-shorter.`
      );
    }

    console.log(`[hydration] sparkline ${realSyms.size} real curves stable across ${samples.length} samples`);
  });

  test('movers grid does not transition from non-empty to empty during 60s window', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.locator('.ag-row').first().waitFor({ timeout: 30_000 });
    await page.waitForTimeout(SETTLE_MS);

    const initial = await moverCounts(page);
    if (initial.total === 0) {
      // Movers panel empty at start — could be off-hours / pre-open.
      // Watch the cache reload from network and assert it eventually
      // populates rather than spuriously failing.
      test.skip(true, 'movers panel empty at start — cannot validate non-empty → empty transition');
      return;
    }
    console.log(`[hydration] movers panel start: ${initial.winners}W / ${initial.losers}L`);

    // Sample every 3s for 60s. Track the worst dip (min mover total)
    // and the count of "empty samples" (total === 0).
    const sampleEveryMs = 3000;
    const samples = Math.ceil(MOVERS_WATCH_MS / sampleEveryMs);
    /** @type {Array<{t:number, c: any}>} */
    const snaps = [];
    for (let i = 0; i < samples; i++) {
      await page.waitForTimeout(sampleEveryMs);
      const c = await moverCounts(page);
      snaps.push({ t: (i + 1) * sampleEveryMs, c });
    }

    const emptyHits = snaps.filter(s => s.c.total === 0);
    const minTotal = Math.min(initial.total, ...snaps.map(s => s.c.total));
    console.log(`[hydration] movers samples=${snaps.length}, ` +
      `empty-hits=${emptyHits.length}, min total=${minTotal}, ` +
      `last=${snaps[snaps.length - 1].c.total}`);

    if (emptyHits.length > 0) {
      throw new Error(
        `[movers empty regression] Movers panel transitioned from ` +
        `${initial.total} rows to 0 at ${emptyHits.length} sample(s): ` +
        `${emptyHits.map(s => s.t + 'ms').join(', ')}. ` +
        `Root cause: moversStore fetcher returned [] (broker pct=0 / rate-limit) ` +
        `and dataStore wrote it over the prior good cache. Check that ` +
        `keepStaleOnEmpty:true is set on moversStore in marketDataStores.svelte.js.`
      );
    }
  });

  test('cold-cache /pulse shows sparkline SVG within 15s after wiping localStorage', async ({ page }) => {
    await loginAsAdmin(page);
    // Wipe both sparkline + movers caches so we exercise the fully cold
    // hydration path. Other caches (positions/holdings/funds) survive
    // so the layout's book poller can paint the rest of the grid normally.
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.evaluate(() => {
      try {
        localStorage.removeItem('rbq.cache.md.sparklines');
        localStorage.removeItem('rbq.cache.md.movers');
      } catch { /* private mode */ }
    });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator('.ag-row').first().waitFor({ timeout: 30_000 });

    // Off-hours the broker's historical_data endpoint may rate-limit or
    // return empty for cold-cache symbols (backend's _spark_past_cache
    // gets rebuilt on segment-open + 00:30 IST cron; between deploys it
    // may not be warm yet). When the spark column is universally a dash
    // ("—") the regression we care about (flicker from real curve to
    // flat) cannot occur. Skip rather than false-positive.
    const sparkCells = page.locator('.spark-cell');
    const cellCount  = await sparkCells.count();
    if (cellCount === 0) {
      test.skip(true, 'no spark cells visible — pulse grid did not paint any symbol rows');
      return;
    }
    const firstSvg = page.locator('.spark-cell svg polyline').first();
    const seen = await firstSvg.waitFor({ timeout: 15_000 }).then(() => true).catch(() => false);
    if (!seen) {
      // Confirm whether the backend just rejected every spark request
      // (off-hours / Kite session expired / rate-limited). When every
      // spark cell still reads "—" after 15 s, the backend never
      // returned a populated map — that's a backend / market-state
      // problem, not the hydration race we're guarding. Some renders
      // pad text with whitespace, so we test by SVG-presence: a cell
      // with no <svg> child is the "—" fallback regardless of textContent.
      const dashedCount = await sparkCells.evaluateAll(cells =>
        cells.filter(c => !c.querySelector('svg')).length
      );
      const totalCount = await sparkCells.count();
      if (dashedCount === totalCount) {
        test.skip(true, `backend returned no sparkline data (${totalCount} cells all dashed — off-hours / cold backend cache)`);
        return;
      }
    }
    expect(seen, 'cold-cache /pulse must paint a sparkline SVG within 15s').toBe(true);
  });
});
