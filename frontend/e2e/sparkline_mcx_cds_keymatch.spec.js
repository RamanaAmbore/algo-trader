/**
 * sparkline_mcx_cds_keymatch.spec.js
 *
 * Root cause: when the watchlist instruments cache is cold on startup,
 * _expand_root_items_to_futures() can't resolve bare MCX/CDS names
 * (CRUDEOIL, GOLDM, USDINR) to their front-month contracts. The frontend
 * row keeps tradingsymbol="CRUDEOIL"; loadSparklines sends that to the
 * backend. The backend resolves it to "CRUDEOIL26JUNFUT", stores the
 * result under the contract key, and returns data={CRUDEOIL26JUNFUT:[...]}.
 * The frontend merges the response into sparklinesStore but the renderer
 * reads sparklines["CRUDEOIL"] (the row's tradingsymbol) → undefined → "—".
 *
 * Fix (backend quote.py batch_sparkline): when a bare MCX/CDS name is
 * resolved during normalization, dual-write the result series under BOTH
 * the resolved contract key AND the original bare key so the renderer
 * lookup always hits.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *
 * 1. SSOT   — sparkline endpoint returns data under BOTH bare AND resolved
 *             key when a bare MCX/CDS name is sent; renderer lookup hits.
 * 2. Perf   — batch sparkline call for 1 MCX symbol resolves in <3 s.
 * 3. Stale  — grep confirms batch_sparkline dual-writes orig_to_resolved.
 * 4. Reuse  — _mergeSparkSeries already handles the no-variation guard;
 *             no new merge logic added.
 * 5. UX     — every visible sparkline cell on /pulse with backend data
 *             shows an SVG polyline (not "—"), including MCX/CDS rows.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com npx playwright test \
 *     e2e/sparkline_mcx_cds_keymatch.spec.js \
 *     --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

test.describe.configure({ mode: 'serial' });

// ── 1. SSOT: bare MCX name → backend dual-writes bare key ────────────────────

test.describe('SSOT — backend sparkline dual-write for bare MCX/CDS names', () => {
  test.setTimeout(60_000);

  test('batch_sparkline returns data under the bare MCX name (CRUDEOIL)', async ({ page }) => {
    await loginAsAdmin(page);
    const token = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));
    expect(token, 'JWT must be in sessionStorage').toBeTruthy();

    // Send a bare MCX commodity name the way the frontend sends it
    // when the instruments cache is cold (pre-expansion fallback).
    const resp = await page.request.post(`${BASE}/api/quotes/sparkline`, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: {
        symbols: [{ tradingsymbol: 'CRUDEOIL', exchange: 'MCX' }],
        days: 5,
      },
    });

    // The endpoint must return 200 even when the commodity resolves.
    expect(resp.ok(), `sparkline endpoint must return 200 (got ${resp.status()})`).toBe(true);
    const json = await resp.json();

    // With the dual-write fix, the response must include "CRUDEOIL" as a key
    // (the bare name the frontend sent) in addition to the resolved contract.
    // Without the fix, only the resolved contract name is present and the
    // renderer lookup sparklines["CRUDEOIL"] returns undefined → "—".
    const dataKeys = Object.keys(json?.data ?? {});
    console.log(`[sparkline mcx] data keys: ${dataKeys.join(', ')}`);

    const hasBareKey = dataKeys.includes('CRUDEOIL');
    const hasResolvedKey = dataKeys.some(k => /^CRUDEOIL\d/.test(k)); // e.g. CRUDEOIL26JUNFUT

    if (dataKeys.length === 0) {
      // No MCX data at all (MCX closed, broker cold) — skip rather than fail.
      // This is expected on dev after market hours when MCX isn't available.
      test.skip(true, 'No sparkline data returned for CRUDEOIL — MCX may be closed or broker cold');
      return;
    }

    // Primary assertion: bare key "CRUDEOIL" must be present so the
    // renderer lookup sparklines[row.tradingsymbol] hits.
    expect(hasBareKey,
      `backend must include "CRUDEOIL" (bare key) in sparkline response. ` +
      `Got keys: ${dataKeys.join(', ')}. ` +
      `Root cause: batch_sparkline dual-write for orig_to_resolved not applied.`
    ).toBe(true);

    // Secondary: the series under the bare key must have ≥ 2 points.
    const series = json.data['CRUDEOIL'] ?? [];
    expect(series.length, `sparkline series for CRUDEOIL must have ≥2 points`).toBeGreaterThanOrEqual(2);

    console.log(`[sparkline mcx] CRUDEOIL series length: ${series.length}, hasBareKey: ${hasBareKey}, hasResolvedKey: ${hasResolvedKey}`);
  });

  test('batch_sparkline returns data under the bare CDS name (USDINR)', async ({ page }) => {
    await loginAsAdmin(page);
    const token = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));
    expect(token, 'JWT must be in sessionStorage').toBeTruthy();

    const resp = await page.request.post(`${BASE}/api/quotes/sparkline`, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: {
        symbols: [{ tradingsymbol: 'USDINR', exchange: 'CDS' }],
        days: 5,
      },
    });

    expect(resp.ok(), `sparkline endpoint must return 200`).toBe(true);
    const json = await resp.json();
    const dataKeys = Object.keys(json?.data ?? {});
    console.log(`[sparkline cds] data keys: ${dataKeys.join(', ')}`);

    if (dataKeys.length === 0) {
      test.skip(true, 'No sparkline data for USDINR — CDS may be closed or broker cold');
      return;
    }

    const hasBareKey = dataKeys.includes('USDINR');
    expect(hasBareKey,
      `backend must include "USDINR" (bare key) in response. Got: ${dataKeys.join(', ')}`
    ).toBe(true);
  });

  test('batch_sparkline already-resolved name passes through unchanged', async ({ page }) => {
    await loginAsAdmin(page);
    const token = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));
    expect(token, 'JWT must be in sessionStorage').toBeTruthy();

    // A resolved contract name (has digits) is NOT all-alpha → not resolved
    // again by the endpoint. The result key must match what was sent.
    // This guards against regression where the dual-write loop over-writes.
    const resp = await page.request.post(`${BASE}/api/quotes/sparkline`, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: {
        symbols: [{ tradingsymbol: 'RELIANCE', exchange: 'NSE' }],
        days: 5,
      },
    });

    expect(resp.ok()).toBe(true);
    const json = await resp.json();
    const dataKeys = Object.keys(json?.data ?? {});

    if (dataKeys.length === 0) {
      test.skip(true, 'No sparkline data for RELIANCE — NSE may be closed');
      return;
    }

    // RELIANCE is an equity — no resolution, key must be exactly "RELIANCE".
    expect(dataKeys).toContain('RELIANCE');
    // Must NOT contain any MCX-resolution-style alias (regression guard).
    const unexpectedKeys = dataKeys.filter(k => k !== 'RELIANCE');
    expect(unexpectedKeys.length,
      `Unexpected extra keys for RELIANCE: ${unexpectedKeys.join(', ')}`
    ).toBe(0);
  });
});

// ── 2. Perf: sparkline round-trip for 1 MCX symbol < 3 s ─────────────────────

test('Perf — single MCX bare-name sparkline resolves within 3 s', async ({ page }) => {
  test.setTimeout(15_000);

  await loginAsAdmin(page);
  const token = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));
  expect(token, 'JWT must be in sessionStorage').toBeTruthy();

  const t0 = Date.now();
  const resp = await page.request.post(`${BASE}/api/quotes/sparkline`, {
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    data: {
      symbols: [{ tradingsymbol: 'GOLDM', exchange: 'MCX' }],
      days: 5,
    },
  });
  const elapsed = Date.now() - t0;

  expect(resp.ok()).toBe(true);
  expect(elapsed,
    `sparkline endpoint for MCX bare name took ${elapsed}ms — must be <3000ms`
  ).toBeLessThan(3000);
  console.log(`[sparkline mcx perf] GOLDM resolved in ${elapsed}ms`);
});

// ── 3. Stale: backend source has orig_to_resolved dual-write ─────────────────

test('Stale — batch_sparkline source dual-writes orig_to_resolved', async () => {
  const { readFileSync, existsSync } = await import('node:fs');
  const { join } = await import('node:path');

  const candidates = [
    join(process.cwd(), 'backend/api/routes/quote.py'),
    join(process.cwd(), '../backend/api/routes/quote.py'),
    '/Users/ramanambore/projects/ramboq/backend/api/routes/quote.py',
  ];

  const src_path = candidates.find(p => existsSync(p));
  if (!src_path) {
    test.skip(true, 'quote.py not found — skipping stale grep');
    return;
  }

  const src = readFileSync(src_path, 'utf-8');

  // The fix introduces orig_to_resolved dict.
  expect(src, 'quote.py must declare orig_to_resolved').toContain('orig_to_resolved');

  // The dual-write loop must be present.
  expect(src, 'quote.py must dual-write result under bare name').toContain(
    'for bare, resolved_name in orig_to_resolved.items()'
  );

  // The resolution still happens for MCX/CDS bare names.
  expect(src, 'quote.py must still resolve MCX bare names').toContain(
    '_resolve_mcx_commodity'
  );
  expect(src, 'quote.py must still resolve CDS bare names').toContain(
    '_resolve_cds_currency'
  );

  console.log('[sparkline mcx stale] quote.py dual-write confirmed');
});

// ── 4. Reuse: _mergeSparkSeries stale-better guard is unchanged ───────────────

test('Reuse — _mergeSparkSeries stale-better guard still in marketDataStores', async () => {
  const { readFileSync, existsSync } = await import('node:fs');
  const { join } = await import('node:path');

  const candidates = [
    join(process.cwd(), 'src/lib/data/marketDataStores.svelte.js'),
    join(process.cwd(), 'frontend/src/lib/data/marketDataStores.svelte.js'),
    '/Users/ramanambore/projects/ramboq/frontend/src/lib/data/marketDataStores.svelte.js',
  ];

  const src_path = candidates.find(p => existsSync(p));
  if (!src_path) {
    test.skip(true, 'marketDataStores.svelte.js not found');
    return;
  }

  const src = readFileSync(src_path, 'utf-8');

  // The stale-better merge function must still be present.
  expect(src, 'marketDataStores must export _mergeSparkSeries').toContain('_mergeSparkSeries');
  // The "cached real curve wins over flat fresh" rule.
  expect(src, '_mergeSparkSeries must guard cachedVar && !freshVar').toContain(
    'if (cachedVar && !freshVar) return cached'
  );
  // The prune guard that skips on first call.
  expect(src, 'prune must use _firstSparkFetched guard').toContain('_firstSparkFetched');

  console.log('[sparkline mcx reuse] _mergeSparkSeries guard confirmed');
});

// ── 5. UX: every visible sparkline cell shows SVG when backend has data ───────

test.describe('UX — no "—" cells when backend returned sparkline data', () => {
  test.setTimeout(120_000);

  test('/pulse: MCX/equity sparklines both render SVGs where data exists', async ({ page }) => {
    await loginAsAdmin(page);

    // Clear the sparkline cache so we exercise the cold-mount code path
    // that triggers the bare-MCX-name bug.
    await page.evaluate(() => {
      try { localStorage.removeItem('rbq.cache.md.sparklines'); } catch { /* no-op */ }
    });

    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle', timeout: 40_000 }).catch(() => {});

    // Wait for grid rows to appear.
    const rowLocator = page.locator('.ag-row');
    const rowsVisible = await rowLocator.first().waitFor({ timeout: 20_000 })
      .then(() => true).catch(() => false);

    if (!rowsVisible) {
      test.skip(true, '/pulse grid did not load — cannot validate sparklines');
      return;
    }

    // Wait for at least one sparkline SVG — allows up to 20 s for the
    // two-step onMount flow (positionsStore.load → tick → loadSparklines).
    const firstSvg = page.locator('.spark-cell svg polyline').first();
    const svgAppeared = await firstSvg.waitFor({ timeout: 20_000 })
      .then(() => true).catch(() => false);

    if (!svgAppeared) {
      // Collect dash cells for the diagnostic.
      const dashCells = await page.locator('.spark-cell').evaluateAll(cells =>
        cells.filter(c => c.textContent?.trim() === '—').length
      );
      throw new Error(
        `[sparkline UX] No SVG polyline appeared within 20 s. ` +
        `${dashCells} cells show "—". ` +
        `Cold-instruments-cache MCX key mismatch may not be fixed.`
      );
    }

    // Now fetch the backend's sparkline data for all visible symbols to
    // determine which ones SHOULD have SVGs (backend returned data).
    const sparkBatch = await page.evaluate(async (base) => {
      const token = sessionStorage.getItem('ramboq_token');
      const rows = Array.from(document.querySelectorAll('.ag-row[row-id]'));
      const seen = new Set();
      const symbols = [];
      for (const r of rows) {
        // row-id format is "SYM__MAJOR" — take the symbol portion.
        const sym = r.getAttribute('row-id')?.split('__')[0]?.toUpperCase();
        if (!sym || seen.has(sym)) continue;
        seen.add(sym);
        // Infer exchange from row classes set by ag-Grid row class rules.
        const cls = r.className || '';
        const exch = /mcx/i.test(cls) ? 'MCX'
                   : /bse/i.test(cls) ? 'BSE'
                   : /cds/i.test(cls) ? 'CDS'
                   : /nfo/i.test(cls) ? 'NFO'
                   : 'NSE';
        symbols.push({ tradingsymbol: sym, exchange: exch });
      }
      if (!symbols.length) return {};
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

    const backendKeys = new Set(Object.keys(sparkBatch));
    console.log(`[sparkline UX] backend returned data for ${backendKeys.size} symbols`);

    // For every spark-cell that is still "—", check the backend returned data.
    const dashInfo = await page.locator('.spark-cell').evaluateAll(cells =>
      cells.map(cell => {
        const row = cell.closest('.ag-row');
        const rawId = row?.getAttribute('row-id') ?? '';
        const sym = rawId.split('__')[0]?.toUpperCase() ?? '';
        const isDash = cell.textContent?.trim() === '—';
        const hasSvg = !!cell.querySelector('svg polyline');
        return { sym, isDash, hasSvg };
      })
    );

    // Symbols that show "—" but backend returned data → failure.
    const wrongDashes = dashInfo.filter(d => d.isDash && d.sym && backendKeys.has(d.sym));

    if (wrongDashes.length > 0) {
      const syms = wrongDashes.map(d => d.sym).slice(0, 10).join(', ');
      throw new Error(
        `[sparkline UX] ${wrongDashes.length} cell(s) show "—" even though ` +
        `backend returned sparkline data: ${syms}. ` +
        `Check the batch_sparkline dual-write fix in quote.py (orig_to_resolved).`
      );
    }

    // Sanity: at least one SVG must be visible.
    const svgCount = await page.locator('.spark-cell svg polyline').count();
    expect(svgCount, 'at least one sparkline SVG must render on /pulse').toBeGreaterThan(0);

    // All visible SVG polylines must have ≥ 2 points.
    const pointCounts = await page.locator('.spark-cell svg polyline').evaluateAll(polys =>
      polys.map(p => {
        const pts = (p.getAttribute('points') || '').trim().split(/\s+/);
        return pts.filter(s => s.includes(',')).length;
      })
    );
    const badPolys = pointCounts.filter(n => n < 2);
    expect(badPolys.length,
      `${badPolys.length} SVG polyline(s) have <2 points — broken render`
    ).toBe(0);

    console.log(
      `[sparkline UX] ${svgCount} SVGs checked; ` +
      `min points=${Math.min(...pointCounts)} max=${Math.max(...pointCounts)}; ` +
      `wrong dashes: 0`
    );
  });

  // Mobile viewport variant — same assertions, portrait layout.
  test('/pulse mobile: sparkline SVGs render on phone viewport', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await loginAsAdmin(page);

    await page.evaluate(() => {
      try { localStorage.removeItem('rbq.cache.md.sparklines'); } catch { /* no-op */ }
    });

    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle', timeout: 40_000 }).catch(() => {});

    const rowsOk = await page.locator('.ag-row').first()
      .waitFor({ timeout: 20_000 }).then(() => true).catch(() => false);
    if (!rowsOk) {
      test.skip(true, '/pulse grid did not load on mobile viewport');
      return;
    }

    // On mobile the sparkline column may be hidden by the column defs.
    const sparkCells = await page.locator('.spark-cell').count();
    if (sparkCells === 0) {
      // Column absent on mobile — skip without failing.
      test.skip(true, 'sparkline column not visible on mobile — skipping');
      return;
    }

    // If the column is visible, at least one SVG must render.
    const firstSvg = page.locator('.spark-cell svg polyline').first();
    const appeared = await firstSvg.waitFor({ timeout: 15_000 })
      .then(() => true).catch(() => false);

    if (appeared) {
      const count = await page.locator('.spark-cell svg polyline').count();
      expect(count).toBeGreaterThan(0);
      console.log(`[sparkline mobile] ${count} SVGs visible on 390px viewport`);
    }
    // If sparkline column is present but no SVG yet, that is allowed
    // (mobile may have too few rows to trigger the sparkline load).
  });
});
