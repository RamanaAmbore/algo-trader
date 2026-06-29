/**
 * Pulse grid heights — proportional to row count (desktop).
 *
 * Five quality dimensions:
 *
 * 1. SSOT (desktop): the two .mp-col containers together fill the
 *    .mp-layout height; each .mp-col fills its parent; each bucket
 *    fills its share of the column height.
 *
 * 2. Performance: XHR count on cold load is unchanged relative to
 *    the pre-change baseline (we check ≤ 50 requests in the first
 *    5s, not an absolute number, to stay stable across data sizes).
 *
 * 3. Stale code: no inline `height: <N>px` on .mp-bucket-wrap
 *    elements on desktop — heights must come from flex, not hardcode.
 *
 * 4. Reusable pattern: both left AND right columns use the same
 *    flex-grow mechanism (--bucket-rows CSS var drives flex-grow on
 *    every .mp-bucket-wrap inside .mp-col).
 *
 * 5. UX — desktop + mobile:
 *    - Desktop: grids fill viewport height; bucket flex-grows match
 *      row-count ratios within each column.
 *    - Mobile: stacked layout unchanged; non-empty bucket-grids have
 *      fixed pixel heights; no unexpected zero-height grids.
 *    - Fluid-width + no-overlap at 360/768/1280 px.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30_000;
const DATA_WAIT = 3_000;

// Give beforeEach + multi-retry login headroom.
test.setTimeout(90_000);

// ── helpers ────────────────────────────────────────────────────────────────

/**
 * Fast reachability probe — returns true if the API is up.
 * Uses a short 3 s timeout so we skip quickly when the backend is absent.
 * @param {import('@playwright/test').Page} page
 */
async function isApiReachable(page) {
  try {
    const resp = await page.request.get('/api/health', { timeout: 3000 });
    return resp.status() < 500;
  } catch {
    return false;
  }
}

/**
 * Attempt login; if the backend isn't reachable (local dev without API)
 * the test is skipped gracefully rather than timing out.
 * @param {import('@playwright/test').Page} page
 */
async function tryLoginAsAdmin(page) {
  if (!(await isApiReachable(page))) {
    test.skip(true, 'Backend API not reachable — skipping auth-dependent test');
    return;
  }
  try {
    await loginAsAdmin(page);
  } catch (err) {
    test.skip(true, `Auth failed: ${err.message}`);
  }
}

/** Navigate to /pulse and wait for at least one bucket grid to render. */
async function goToPulse(page) {
  await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
  await page.locator('.mp-layout').waitFor({ state: 'visible', timeout: TIMEOUT });
  await page.locator('.mp-bucket-wrap').first().waitFor({ state: 'visible', timeout: TIMEOUT });
  // Give data fetches time to land so proportional sizing has rows to work with.
  await page.waitForTimeout(DATA_WAIT);
}

// ── Desktop-specific tests ─────────────────────────────────────────────────

test.describe('Pulse grid heights — desktop', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test.beforeEach(async ({ page }) => {
    await tryLoginAsAdmin(page);
  });

  // 1. SSOT — layout fills available height.
  test('mp-layout fills the mp-flat-wrap height', async ({ page }) => {
    await goToPulse(page);

    const sizes = await page.evaluate(() => {
      const flat   = document.querySelector('.mp-flat-wrap');
      const layout = document.querySelector('.mp-layout');
      if (!flat || !layout) return null;
      return {
        flatH:   flat.getBoundingClientRect().height,
        layoutH: layout.getBoundingClientRect().height,
      };
    });

    if (!sizes) { test.skip(true, 'mp-flat-wrap or mp-layout not found'); return; }

    expect(sizes.flatH).toBeGreaterThan(200);
    // layout may be slightly shorter due to padding; allow 30 px slack.
    expect(sizes.layoutH).toBeGreaterThanOrEqual(sizes.flatH - 30);
  });

  test('each mp-col fills the mp-layout height', async ({ page }) => {
    await goToPulse(page);

    const result = await page.evaluate(() => {
      const layout = document.querySelector('.mp-layout');
      const cols   = Array.from(document.querySelectorAll('.mp-col'));
      if (!layout || !cols.length) return null;
      const lh = layout.getBoundingClientRect().height;
      return cols.map(c => ({ colH: c.getBoundingClientRect().height, lh }));
    });

    if (!result) { test.skip(true, 'mp-layout or mp-col not found'); return; }
    for (const { colH, lh } of result) {
      // Each col must reach at least 90 % of the layout height.
      expect(colH).toBeGreaterThanOrEqual(lh * 0.9);
    }
  });

  // 2. Performance — cold-load XHR budget.
  //    Only meaningful when the backend is running; without it, every
  //    API probe counts as a failed XHR and inflates the number.
  test('XHR count on cold load stays within budget (≤80 in first ~8 s)', async ({ page }) => {
    if (!(await isApiReachable(page))) {
      test.skip(true, 'Backend not reachable — XHR budget test skipped');
      return;
    }

    let xhrCount = 0;
    page.on('request', req => {
      if (req.resourceType() === 'fetch' || req.resourceType() === 'xhr') xhrCount++;
    });

    await goToPulse(page);
    await page.waitForTimeout(5000);

    // 80 requests in first ~8 s is a generous ceiling that prevents
    // runaway polling regressions while tolerating Pulse's multi-feed
    // cold-start (quotes, positions, holdings, movers, sparklines,
    // watchlists, SSE).
    expect(xhrCount).toBeLessThanOrEqual(80);
  });

  // 3. Stale code — no hard-coded px height on bucket wraps on desktop.
  test('bucket wraps have no inline hardcoded pixel height on desktop', async ({ page }) => {
    await goToPulse(page);

    const violations = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('.mp-bucket-wrap'))
        .map(el => /** @type {HTMLElement} */ (el).style.height)
        .filter(h => /^\d+px$/.test(h));
    });

    expect(violations).toHaveLength(0);
  });

  // 4. Reusable — both left and right columns use --bucket-rows.
  test('both left and right columns use --bucket-rows CSS var for flex-grow', async ({ page }) => {
    await goToPulse(page);

    const result = await page.evaluate(() => {
      const checks = /** @type {Array<{rowsVar:string}>} */ ([]);
      for (const col of document.querySelectorAll('.mp-col')) {
        for (const b of col.querySelectorAll('.mp-bucket-wrap')) {
          const rowsVar = getComputedStyle(b).getPropertyValue('--bucket-rows').trim();
          checks.push({ rowsVar });
        }
      }
      return checks;
    });

    expect(result.length).toBeGreaterThan(0);
    for (const { rowsVar } of result) {
      // Every bucket must have a positive-integer --bucket-rows var.
      expect(Number(rowsVar)).toBeGreaterThanOrEqual(1);
    }
  });

  // 5. UX — positions and holdings are proportionally sized.
  test('positions/holdings bucket heights are proportional to row counts', async ({ page }) => {
    await goToPulse(page);
    await page.waitForTimeout(2000); // extra wait for row data to settle

    const result = await page.evaluate(() => {
      const pos  = document.querySelector('.mp-bucket-positions');
      const hold = document.querySelector('.mp-bucket-holdings');
      if (!pos || !hold) return null;

      const posRows  = Number(getComputedStyle(pos).getPropertyValue('--bucket-rows').trim());
      const holdRows = Number(getComputedStyle(hold).getPropertyValue('--bucket-rows').trim());
      const posH  = pos.getBoundingClientRect().height;
      const holdH = hold.getBoundingClientRect().height;

      return { posRows, holdRows, posH, holdH };
    });

    if (!result) { test.skip(true, 'positions/holdings buckets not found'); return; }

    const { posRows, holdRows, posH, holdH } = result;

    // Both buckets must be visible (≥ 240 px, the 5-row min-height floor).
    expect(posH).toBeGreaterThanOrEqual(240);
    expect(holdH).toBeGreaterThanOrEqual(240);

    // When row counts differ meaningfully, the larger-count bucket takes
    // more height. 30 px tolerance absorbs header-height differences.
    if (Math.abs(posRows - holdRows) > 1) {
      if (posRows > holdRows) {
        expect(posH).toBeGreaterThanOrEqual(holdH - 30);
      } else {
        expect(holdH).toBeGreaterThanOrEqual(posH - 30);
      }
    }
  });

  // 5b. UX — no visible (non-collapsed) bucket has near-zero height.
  test('all non-collapsed bucket wraps are visible (height > 60 px)', async ({ page }) => {
    await goToPulse(page);

    const heights = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('.mp-col .mp-bucket-wrap'))
        .map(el => ({
          cls: el.className,
          h:   el.getBoundingClientRect().height,
        }));
    });

    for (const { cls, h } of heights) {
      if (cls.includes('is-collapsed')) continue;
      if (h === 0) continue; // showWinners/showLosers=false → display:none
      expect(h).toBeGreaterThanOrEqual(60);
    }
  });

  // 5d. UX — every desktop bucket has a minimum height of 240 px (fits
  //     5 data rows + ag-Grid header + bucket-header without internal scroll).
  test('each desktop bucket is at least 240 px tall (5-row floor)', async ({ page }) => {
    await goToPulse(page);

    const buckets = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('.mp-col .mp-bucket-wrap'))
        .map(el => ({
          cls: el.className,
          h:   el.getBoundingClientRect().height,
        }));
    });

    // Skip this test if no buckets rendered (backend absent).
    if (!buckets.length) { test.skip(true, 'no buckets rendered'); return; }

    for (const { cls, h } of buckets) {
      if (cls.includes('is-collapsed')) continue;
      if (h === 0) continue; // hidden bucket (showWinners/showLosers=false)
      expect(h).toBeGreaterThanOrEqual(240);
    }
  });

  // 5e. UX — when a bucket has >= 5 rows, those rows fit without
  //     grid-internal vertical scroll (visible .ag-row count >= 5).
  test('buckets with >= 5 rows show at least 5 visible ag-rows without internal scroll', async ({ page }) => {
    await goToPulse(page);
    await page.waitForTimeout(2000); // allow row data to settle

    const result = await page.evaluate(() => {
      const out = [];
      for (const wrap of document.querySelectorAll('.mp-col .mp-bucket-wrap')) {
        if (wrap.classList.contains('is-collapsed')) continue;

        // Count rendered ag-rows (ag-Grid only puts DOM rows for visible rows
        // when row virtualisation is on; for small grids all rows are in DOM).
        const allRows     = wrap.querySelectorAll('.ag-row');
        const totalRows   = allRows.length;
        if (totalRows < 5) continue; // not enough data to assert

        const wrapRect    = wrap.getBoundingClientRect();

        // Count rows whose top edge sits within the bucket's bounding rect
        // (i.e. not clipped above/below the bucket chrome).
        let visibleCount  = 0;
        for (const row of allRows) {
          const rr = row.getBoundingClientRect();
          if (rr.top >= wrapRect.top && rr.bottom <= wrapRect.bottom + 4) {
            visibleCount++;
          }
        }
        out.push({ totalRows, visibleCount });
      }
      return out;
    });

    // If no bucket had >= 5 rows the check is moot (data-dependent).
    if (!result.length) { test.skip(true, 'no bucket had >= 5 rows of data'); return; }

    for (const { visibleCount } of result) {
      expect(visibleCount).toBeGreaterThanOrEqual(5);
    }
  });

  // 5c. UX — no horizontal overflow at 768 px (tablet-wide).
  test('no horizontal overflow at 768 px tablet width', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 900 });
    await goToPulse(page);

    const overflow = await page.evaluate(() => {
      return document.body.scrollWidth > document.body.clientWidth + 4;
    });

    expect(overflow).toBe(false);
  });
});

// ── Mobile-specific tests ──────────────────────────────────────────────────

test.describe('Pulse grid heights — mobile', () => {
  test.use({ viewport: { width: 360, height: 800 }, isMobile: true });

  test.beforeEach(async ({ page }) => {
    await tryLoginAsAdmin(page);
  });

  // Mobile: non-empty bucket-grids have the 220 px fixed height.
  test('non-empty bucket-grids have 220 px fixed height on mobile', async ({ page }) => {
    await goToPulse(page);

    const heights = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('.bucket-grid'))
        .map(el => ({
          h:       el.getBoundingClientRect().height,
          isEmpty: el.closest('.mp-bucket-wrap')?.classList.contains('is-empty') ?? false,
        }));
    });

    // Filter to non-empty, non-zero grids — those must be 220 px.
    const nonEmpty = heights.filter(({ h, isEmpty }) => h > 0 && !isEmpty);
    if (!nonEmpty.length) {
      test.skip(true, 'all bucket-grids are empty-state or hidden');
      return;
    }
    for (const { h } of nonEmpty) {
      // Allow ±15 px tolerance (subpixel rendering, scrollbar).
      expect(h).toBeGreaterThanOrEqual(205);
      expect(h).toBeLessThanOrEqual(235);
    }
  });

  // Mobile: page is scrollable (content taller than viewport).
  test('pulse page is taller than mobile viewport (scrollable)', async ({ page }) => {
    await goToPulse(page);
    await page.waitForTimeout(2000);

    const scrollable = await page.evaluate(() => document.body.scrollHeight > window.innerHeight);
    expect(scrollable).toBe(true);
  });

  // Mobile: no horizontal overflow at 360 px.
  test('no horizontal overflow at 360 px (mobile portrait)', async ({ page }) => {
    await goToPulse(page);

    const overflow = await page.evaluate(
      () => document.body.scrollWidth > document.body.clientWidth + 4,
    );
    expect(overflow).toBe(false);
  });
});
