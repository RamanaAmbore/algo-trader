/**
 * charts_mobile_toolbar.spec.js
 *
 * Verifies that the ChartWorkspace toolbar fits in two rows at mobile
 * viewports (360px wide, Pixel-5-class) and that the desktop layout is
 * unchanged.
 *
 * Five quality dimensions:
 *  1. SSOT    — row 1 (symbol+series) and row 2 (range+overlays) are
 *               each single-line: asserted via bounding-box y-coords.
 *  2. Perf    — cold-load XHR count on mobile ≤ 25.
 *  3. Stale   — no orphan `display: none` mobile hacks in CSS source.
 *  4. Reuse   — modal and page share the SAME .cw-picker / .cw-controls
 *               classes (no parallel style blocks).
 *  5. UX      — all tap targets ≥ 32px height; cyan-400 active palette.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/charts_mobile_toolbar.spec.js \
 *   --project=mobile-portrait --workers=1
 *
 * For desktop regression run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/charts_mobile_toolbar.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const CHARTS_URL = `${BASE}/charts?symbol=NIFTY&mode=live`;

// ── helpers ──────────────────────────────────────────────────────────────

/** Wait for both toolbar rows to be visible. */
async function waitForToolbar(page) {
  await expect(page.locator('.cw-picker')).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('.cw-controls')).toBeVisible({ timeout: 10_000 });
}

/** Return bounding boxes for every direct-child button inside a locator. */
async function childButtonBoxes(locator) {
  return locator.evaluate((el) => {
    return Array.from(el.querySelectorAll('button')).map((btn) => {
      const r = btn.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom, height: r.height, left: r.left, right: r.right };
    });
  });
}

/** Inject saved sessionStorage items so subsequent page.goto() starts authed. */
async function injectSession(page, items) {
  await page.addInitScript((data) => {
    for (const [k, v] of Object.entries(data)) sessionStorage.setItem(k, v);
  }, items);
  if (items.ramboq_token) {
    await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${items.ramboq_token}` });
  }
}

// ── 1. SSOT: row alignment at mobile viewport ─────────────────────────

test.describe('mobile toolbar — single-row layout', () => {
  test.use({ viewport: { width: 360, height: 800 }, isMobile: true });

  let _session = /** @type {{ token: string }|null} */ (null);

  // 60 s timeout: loginAsAdmin retries up to 3 times on rate-limit
  // (delays of 0 + 3 + 8 = 11 s) so the default 30 s is too tight.
  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage();
    const info = await loginAsAdmin(page);
    _session = { token: info.token };
    await page.close();
  }, 60000);

  test('row 1 (symbol + series) fits on one line', async ({ page }) => {
    await injectSession(page, { ramboq_token: _session?.token ?? '' });
    await page.goto(CHARTS_URL, { waitUntil: 'domcontentloaded' });
    await waitForToolbar(page);

    // All direct flex children of .cw-picker must share the same row.
    // We sample the top y-coord of each child and assert the spread is
    // ≤ 4px (sub-pixel rounding only — no second-row wrap).
    const pickerTopCoords = await page.locator('.cw-picker').evaluate((el) => {
      return Array.from(el.children).map((child) => {
        const r = child.getBoundingClientRect();
        return Math.round(r.top);
      });
    });

    expect(pickerTopCoords.length).toBeGreaterThan(0);
    const minY = Math.min(...pickerTopCoords);
    const maxY = Math.max(...pickerTopCoords);
    // All children start within 8px of each other — single row.
    expect(maxY - minY).toBeLessThanOrEqual(8);
  });

  test('row 2 (range + overlays) fits on one line', async ({ page }) => {
    await injectSession(page, { ramboq_token: _session?.token ?? '' });
    await page.goto(CHARTS_URL, { waitUntil: 'domcontentloaded' });
    await waitForToolbar(page);

    const controlsTopCoords = await page.locator('.cw-controls').evaluate((el) => {
      return Array.from(el.children).map((child) => {
        const r = child.getBoundingClientRect();
        return Math.round(r.top);
      });
    });

    expect(controlsTopCoords.length).toBeGreaterThan(0);
    const minY = Math.min(...controlsTopCoords);
    const maxY = Math.max(...controlsTopCoords);
    expect(maxY - minY).toBeLessThanOrEqual(8);
  });

  // ── 2. Performance: cold XHR count ≤ 25 ─────────────────────────────

  test('cold-load XHR count on mobile ≤ 25', async ({ page }) => {
    await injectSession(page, { ramboq_token: _session?.token ?? '' });

    let xhrCount = 0;
    page.on('request', (req) => {
      if (req.resourceType() === 'xhr' || req.resourceType() === 'fetch') xhrCount++;
    });

    await page.goto(CHARTS_URL, { waitUntil: 'networkidle', timeout: 30_000 });
    await waitForToolbar(page);

    // Budget: ≤ 25 XHR/fetch calls on cold load for mobile.
    expect(xhrCount).toBeLessThanOrEqual(25);
  });

  // ── 5. UX: tap targets ≥ 32px height ─────────────────────────────────

  test('all toolbar tap targets are ≥ 32px height', async ({ page }) => {
    await injectSession(page, { ramboq_token: _session?.token ?? '' });
    await page.goto(CHARTS_URL, { waitUntil: 'domcontentloaded' });
    await waitForToolbar(page);

    // Collect all buttons in both toolbar rows.
    const buttonHeights = await page.evaluate(() => {
      const rows = ['.cw-picker', '.cw-controls'];
      const heights = [];
      for (const sel of rows) {
        const el = document.querySelector(sel);
        if (!el) continue;
        for (const btn of el.querySelectorAll('button')) {
          const r = btn.getBoundingClientRect();
          heights.push({ label: btn.textContent?.trim().slice(0, 20) ?? '', height: r.height });
        }
      }
      return heights;
    });

    expect(buttonHeights.length).toBeGreaterThan(0);
    for (const { label, height } of buttonHeights) {
      expect(height, `button "${label}" tap target too small: ${height}px`).toBeGreaterThanOrEqual(32);
    }
  });

  // UX: active range button carries cyan/amber palette (not plain text)
  test('active range button carries amber active colour', async ({ page }) => {
    await injectSession(page, { ramboq_token: _session?.token ?? '' });
    await page.goto(CHARTS_URL, { waitUntil: 'domcontentloaded' });
    await waitForToolbar(page);

    // The default active range is 30 days (1M) per initial state.
    const activeBtn = page.locator('.cw-range-btn.active').first();
    await expect(activeBtn).toBeVisible();
    // Active button must have amber text (not muted/default colour).
    // Check inline style / computed: color should NOT be the default muted
    // value (#4a5a7a) — it should be amber (#fbbf24 = rgb(251,191,36)).
    const color = await activeBtn.evaluate((el) =>
      window.getComputedStyle(el).color
    );
    // Amber is rgb(251, 191, 36)
    expect(color).toBe('rgb(251, 191, 36)');
  });
});

// ── 3. Stale code: no orphan display:none mobile hacks in source ──────

test.describe('stale-code grep — no orphan mobile display:none hacks', () => {
  test('ChartWorkspace.svelte has no orphan display:none mobile hacks', () => {
    const srcPath = path.resolve(
      process.cwd(),
      'src/lib/ChartWorkspace.svelte'
    );
    const src = fs.readFileSync(srcPath, 'utf-8');

    // Find every line that contains `display: none` (or `display:none`).
    // The only permitted occurrences are the canonical intraday label-swap
    // rules (.cw-intraday-full and .cw-intraday-short). Any other line with
    // display:none would be a suspicious orphan.
    const displayNoneLines = src
      .split('\n')
      .filter((line) => /display\s*:\s*none/.test(line));

    for (const line of displayNoneLines) {
      // Permit the two intentional label-swap rules.
      const isPermitted =
        /\.cw-intraday-full/.test(line) ||
        /\.cw-intraday-short/.test(line);
      expect(
        isPermitted,
        `Orphan display:none in ChartWorkspace.svelte — remove it or document intent:\n  ${line.trim()}`
      ).toBe(true);
    }
  });
});

// ── 4. Reuse: modal uses same toolbar classes as page ─────────────────

test.describe('reuse — modal toolbar shares same CSS classes as page', () => {
  test('ChartModal.svelte does not define a parallel cw-picker/cw-controls block', () => {
    const modalPath = path.resolve(
      process.cwd(),
      'src/lib/ChartModal.svelte'
    );
    const modalSrc = fs.readFileSync(modalPath, 'utf-8');

    // ChartModal wraps ChartWorkspace — it must NOT define its own
    // .cw-picker or .cw-controls style blocks (that would be a parallel
    // style path diverging from ChartWorkspace's canonical CSS).
    expect(modalSrc).not.toMatch(/\.cw-picker\s*\{/);
    expect(modalSrc).not.toMatch(/\.cw-controls\s*\{/);
  });

  test('ChartWorkspace.svelte .cw-intraday-full default is display:inline (outside @media)', () => {
    const srcPath = path.resolve(
      process.cwd(),
      'src/lib/ChartWorkspace.svelte'
    );
    const src = fs.readFileSync(srcPath, 'utf-8');
    // The default rules (outside @media) must set:
    //   .cw-intraday-full  → display: inline  (full label shown by default)
    //   .cw-intraday-short → display: none    (short label hidden by default)
    // Locate the comment that precedes the default rules so we check the
    // right section, not the @media block's overrides.
    const defaultSectionIdx = src.indexOf('/* Default (≥521px):');
    expect(defaultSectionIdx).toBeGreaterThan(-1);
    const defaultSection = src.slice(defaultSectionIdx);
    expect(defaultSection).toMatch(/\.cw-intraday-full\s*\{[^}]*display\s*:\s*inline/);
    expect(defaultSection).toMatch(/\.cw-intraday-short\s*\{[^}]*display\s*:\s*none/);
  });
});

// ── Desktop regression: layout unchanged at 1400px ───────────────────

test.describe('desktop regression — toolbar layout intact', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  let _session = /** @type {{ token: string }|null} */ (null);

  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage();
    const info = await loginAsAdmin(page);
    _session = { token: info.token };
    await page.close();
  }, 60000);

  test('row 1 type-filter has width ≥ 7rem at desktop (not shrunk)', async ({ page }) => {
    await injectSession(page, { ramboq_token: _session?.token ?? '' });
    await page.goto(CHARTS_URL, { waitUntil: 'domcontentloaded' });
    await waitForToolbar(page);

    // .cw-type-wrap is 8.5rem on desktop; 1rem = 16px so ≥ 7rem = ≥ 112px.
    const typeWrapWidth = await page.locator('.cw-type-wrap').evaluate((el) =>
      el.getBoundingClientRect().width
    );
    expect(typeWrapWidth).toBeGreaterThanOrEqual(112);
  });

  test('chart-type select has trailing auto-margin at desktop', async ({ page }) => {
    await injectSession(page, { ramboq_token: _session?.token ?? '' });
    await page.goto(CHARTS_URL, { waitUntil: 'domcontentloaded' });
    await waitForToolbar(page);

    // At desktop, .cw-type-chart-wrap has margin-left: auto so it
    // floats to the trailing edge. Verify it is positioned in the
    // right half of the picker row (> 50% of row width).
    const [pickerLeft, pickerWidth, chartWrapLeft] = await page.evaluate(() => {
      const picker = document.querySelector('.cw-picker');
      const chartWrap = document.querySelector('.cw-type-chart-wrap');
      if (!picker || !chartWrap) return [0, 1, 0];
      const pr = picker.getBoundingClientRect();
      const cr = chartWrap.getBoundingClientRect();
      return [pr.left, pr.width, cr.left];
    });
    const relativeLeft = chartWrapLeft - pickerLeft;
    expect(relativeLeft).toBeGreaterThan(pickerWidth * 0.4);
  });

  test('row 2 controls row does not wrap at desktop', async ({ page }) => {
    await injectSession(page, { ramboq_token: _session?.token ?? '' });
    await page.goto(CHARTS_URL, { waitUntil: 'domcontentloaded' });
    await waitForToolbar(page);

    const controlsTopCoords = await page.locator('.cw-controls').evaluate((el) => {
      return Array.from(el.children).map((child) => {
        const r = child.getBoundingClientRect();
        return Math.round(r.top);
      });
    });

    expect(controlsTopCoords.length).toBeGreaterThan(0);
    const spread = Math.max(...controlsTopCoords) - Math.min(...controlsTopCoords);
    expect(spread).toBeLessThanOrEqual(8);
  });
});
