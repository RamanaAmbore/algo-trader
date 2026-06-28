/**
 * pulse_column_order.spec.js
 *
 * Verifies that the pinned/watchlist/movers (left grid, leftColDefs) and
 * the positions/holdings (right grid, rightColDefs) both show `Day %`
 * BEFORE `Close` in the rendered column header sequence.
 *
 * Canonical cluster per CLAUDE.md:
 *   Symbol · 5d · LTP · Avg · Day % · Close · Qty · Day P&L · P&L % · P&L
 *
 * Five quality dimensions (per feedback_test_dimensions.md):
 *   1. SSOT       — column header order in rendered DOM matches canonical
 *   2. Performance — cold-load unique API path count on /pulse ≤30
 *   3. Stale code  — old (wrong) header order (Close before Day %) not in any grid
 *   4. Reusable    — every bucket grid Close header carries col-id="close" (single _prevCol source)
 *   5. UX          — cell-freshness-pulse animation still defined; LTP headers render
 *
 * Session is shared via beforeAll to stay under the 5-req/min login rate-limit.
 * Desktop only — mobile viewports collapse some grids and parallel mobile
 * projects trigger 429s from simultaneous login attempts.
 */

import { test, expect } from '@playwright/test';

// Restrict to desktop so narrow-viewport column virtualisation does not hide
// columns and so the auth rate-limit is not triggered by mobile siblings.
test.use({ viewport: { width: 1440, height: 900 } });

// Single login for the whole describe block.
test.describe.configure({ mode: 'serial' });

// Shared token injected once in beforeAll, reused by every test.
let sharedToken = null;

/**
 * Direct API login — avoids driving the /signin form 5× and saturating the
 * 5-req/min rate limit. Falls back to 'rambo' if 'ambore' is not present.
 */
async function acquireToken(request) {
  const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
  const PASS = process.env.PLAYWRIGHT_PASS   || 'admin1234';
  for (const user of ['ambore', 'rambo']) {
    const r = await request.post(`${BASE}/api/auth/login`, {
      data: { username: user, password: PASS },
    });
    if (r.ok()) {
      const body = await r.json();
      return body.access_token ?? null;
    }
  }
  return null;
}

test.describe('/pulse — column order: Day % before Close', () => {

  test.beforeAll(async ({ request }) => {
    sharedToken = await acquireToken(request);
    if (!sharedToken) throw new Error('pulse_column_order: could not acquire auth token in beforeAll');
  });

  /** Navigate to /pulse with the shared token injected into sessionStorage. */
  async function gotoWithAuth(page) {
    const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
    await page.context().addInitScript((tok) => {
      sessionStorage.setItem('ramboq_token', tok);
    }, sharedToken);
    await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${sharedToken}` });
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
  }

  // ── 1. SSOT: Day % before Close in all bucket grids ─────────────────────────
  test('1. SSOT: all bucket grids show Day % before Close', async ({ page }) => {
    test.setTimeout(90_000);
    const vp = page.viewportSize();
    if (vp && vp.width < 1000) { test.skip(); return; }

    await gotoWithAuth(page);
    await page.waitForSelector('.ag-theme-algo .ag-header-cell', { state: 'visible', timeout: 30_000 });
    await page.waitForTimeout(4000);

    const result = await page.evaluate(() => {
      function readGrid(grid) {
        const section   = grid.closest('.mp-bucket-wrap');
        const labelEl   = section?.querySelector('.mp-bucket-label');
        const label     = labelEl
          ? labelEl.textContent.trim()
          : (Array.from(section?.classList ?? []).find(c => c.startsWith('mp-bucket-') && c !== 'mp-bucket-wrap') ?? 'unknown').replace('mp-bucket-', '');

        const texts = Array.from(grid.querySelectorAll('.ag-header-cell'))
          .map(h => (h.querySelector('.ag-header-cell-text')?.textContent ?? '').trim())
          .filter(Boolean);

        return { label, texts, dayPctIdx: texts.findIndex(t => t === 'Day %'), closeIdx: texts.indexOf('Close') };
      }
      return Array.from(document.querySelectorAll('.ag-theme-algo.bucket-grid')).map(readGrid);
    });

    console.log('--- Column header sequences ---');
    for (const g of result) {
      console.log(`  ${g.label}: [${g.texts.join(', ')}]`);
      console.log(`    Day % @ ${g.dayPctIdx}, Close @ ${g.closeIdx}`);
    }

    expect(result.length, 'at least one grid must have rendered headers').toBeGreaterThan(0);

    let checkedCount = 0;
    for (const g of result) {
      if (g.dayPctIdx === -1 || g.closeIdx === -1) continue;
      checkedCount++;
      expect(g.dayPctIdx,
        `${g.label}: Day % (${g.dayPctIdx}) must come BEFORE Close (${g.closeIdx})`
      ).toBeLessThan(g.closeIdx);
    }
    if (checkedCount === 0) {
      console.warn('WARN: no grid had both Day % and Close — grid may be empty or positions/holdings unavailable.');
    }
  });

  // ── 2. Performance: cold-load unique API paths ≤30 ──────────────────────────
  test('2. Perf: cold-load unique /api path count ≤30', async ({ page }) => {
    test.setTimeout(90_000);
    const vp = page.viewportSize();
    if (vp && vp.width < 1000) { test.skip(); return; }

    const seenPaths = new Set();
    page.on('request', req => {
      if (!['fetch', 'xhr'].includes(req.resourceType())) return;
      try {
        const p = new URL(req.url()).pathname;
        if (p.startsWith('/api/')) seenPaths.add(p);
      } catch (_) { /* ignore */ }
    });

    await gotoWithAuth(page);
    await page.waitForTimeout(3000);

    const uniqueCount = seenPaths.size;
    console.log(`Unique /api paths: ${uniqueCount}`);
    console.log('Paths:', [...seenPaths].join(', '));
    expect(uniqueCount, `Unique /api path count ${uniqueCount} should be ≤30`).toBeLessThanOrEqual(30);
  });

  // ── 3. Stale: old wrong order (Close before Day %) absent from all grids ─────
  test('3. Stale: no bucket grid renders Close before Day %', async ({ page }) => {
    test.setTimeout(90_000);
    const vp = page.viewportSize();
    if (vp && vp.width < 1000) { test.skip(); return; }

    await gotoWithAuth(page);
    await page.waitForSelector('.ag-theme-algo .ag-header-cell', { state: 'visible', timeout: 30_000 });
    await page.waitForTimeout(4000);

    const wrongOrderBuckets = await page.evaluate(() => {
      function readGrid(grid) {
        const section   = grid.closest('.mp-bucket-wrap');
        const labelEl   = section?.querySelector('.mp-bucket-label');
        const label     = labelEl
          ? labelEl.textContent.trim()
          : (Array.from(section?.classList ?? []).find(c => c.startsWith('mp-bucket-') && c !== 'mp-bucket-wrap') ?? 'unknown').replace('mp-bucket-', '');
        const texts = Array.from(grid.querySelectorAll('.ag-header-cell'))
          .map(h => (h.querySelector('.ag-header-cell-text')?.textContent ?? '').trim())
          .filter(Boolean);
        return { label, texts, dayPctIdx: texts.findIndex(t => t === 'Day %'), closeIdx: texts.indexOf('Close') };
      }
      return Array.from(document.querySelectorAll('.ag-theme-algo.bucket-grid'))
        .map(readGrid)
        .filter(g => g.dayPctIdx !== -1 && g.closeIdx !== -1 && g.closeIdx < g.dayPctIdx);
    });

    if (wrongOrderBuckets.length > 0) {
      console.log('Grids with wrong order (Close before Day %):', JSON.stringify(wrongOrderBuckets, null, 2));
    }
    expect(wrongOrderBuckets.length,
      `${wrongOrderBuckets.length} grid(s) still show Close before Day %: ${wrongOrderBuckets.map(g => g.label).join(', ')}`
    ).toBe(0);
  });

  // ── 4. Reusable: shared _prevCol — every Close header has col-id="close" ─────
  test('4. Reusable: every bucket grid Close header has col-id "close"', async ({ page }) => {
    test.setTimeout(90_000);
    const vp = page.viewportSize();
    if (vp && vp.width < 1000) { test.skip(); return; }

    await gotoWithAuth(page);
    await page.waitForSelector('.ag-theme-algo .ag-header-cell', { state: 'visible', timeout: 30_000 });
    await page.waitForTimeout(4000);

    const closeColIds = await page.evaluate(() => {
      const results = [];
      for (const grid of document.querySelectorAll('.ag-theme-algo.bucket-grid')) {
        const section = grid.closest('.mp-bucket-wrap');
        const labelEl = section?.querySelector('.mp-bucket-label');
        const label   = labelEl
          ? labelEl.textContent.trim()
          : (Array.from(section?.classList ?? []).find(c => c.startsWith('mp-bucket-') && c !== 'mp-bucket-wrap') ?? 'unknown').replace('mp-bucket-', '');
        for (const h of grid.querySelectorAll('.ag-header-cell')) {
          if ((h.querySelector('.ag-header-cell-text')?.textContent ?? '').trim() === 'Close') {
            results.push({ label, colId: h.getAttribute('col-id') });
            break;
          }
        }
      }
      return results;
    });

    console.log('Close col-id per grid:', JSON.stringify(closeColIds));
    expect(closeColIds.length, 'at least two grids must have a Close header').toBeGreaterThanOrEqual(2);
    for (const { label, colId } of closeColIds) {
      expect(colId, `${label}: Close header must have col-id="close"`).toBe('close');
    }
  });

  // ── 5. UX: freshness-shimmer class intact; LTP headers present ───────────────
  test('5. UX: LTP headers render; cell-freshness-pulse animation class defined', async ({ page }) => {
    test.setTimeout(90_000);
    const vp = page.viewportSize();
    if (vp && vp.width < 1000) { test.skip(); return; }

    await gotoWithAuth(page);
    await page.waitForSelector('.ag-theme-algo .ag-header-cell', { state: 'visible', timeout: 30_000 });
    await page.waitForTimeout(3000);

    // UX 1: LTP header appears in at least one grid.
    const ltpHeaderCount = await page.evaluate(() => {
      let count = 0;
      for (const grid of document.querySelectorAll('.ag-theme-algo.bucket-grid')) {
        if (Array.from(grid.querySelectorAll('.ag-header-cell'))
            .some(h => (h.querySelector('.ag-header-cell-text')?.textContent ?? '').trim() === 'LTP')) count++;
      }
      return count;
    });
    console.log('Grids with LTP header:', ltpHeaderCount);
    expect(ltpHeaderCount, 'LTP header must appear in at least one grid').toBeGreaterThan(0);

    // UX 2: cell-freshness-pulse CSS animation is still defined (freshness shimmer intact).
    const shimmerDefined = await page.evaluate(() => {
      const el = document.createElement('div');
      el.className = 'cell-freshness-pulse';
      el.style.cssText = 'position:absolute;top:-9999px;width:100px;height:20px;';
      document.body.appendChild(el);
      const anim = window.getComputedStyle(el, '::after').animationName;
      document.body.removeChild(el);
      return anim !== 'none' && anim !== '';
    });
    console.log('cell-freshness-pulse animation defined:', shimmerDefined);
    expect(shimmerDefined, 'cell-freshness-pulse ::after animation must be defined').toBe(true);

    // UX 3: in left-side grids, LTP appears BEFORE Day % (canonical visual order).
    const ltpBeforeDayPct = await page.evaluate(() => {
      for (const cls of ['mp-bucket-pinwatch', 'mp-bucket-winners', 'mp-bucket-losers']) {
        const section = document.querySelector(`.${cls}`);
        if (!section) continue;
        const grid = section.querySelector('.ag-theme-algo.bucket-grid:not(.mp-grid-hidden)');
        if (!grid) continue;
        const texts = Array.from(grid.querySelectorAll('.ag-header-cell'))
          .map(h => (h.querySelector('.ag-header-cell-text')?.textContent ?? '').trim())
          .filter(Boolean);
        const ltpIdx = texts.indexOf('LTP'), dayPctIdx = texts.findIndex(t => t === 'Day %');
        if (ltpIdx !== -1 && dayPctIdx !== -1) return { bucket: cls, ltpIdx, dayPctIdx };
      }
      return null;
    });
    if (ltpBeforeDayPct) {
      console.log(`Left grid (${ltpBeforeDayPct.bucket}): LTP@${ltpBeforeDayPct.ltpIdx} Day %@${ltpBeforeDayPct.dayPctIdx}`);
      expect(ltpBeforeDayPct.ltpIdx, 'LTP must precede Day %').toBeLessThan(ltpBeforeDayPct.dayPctIdx);
    }
  });

});
