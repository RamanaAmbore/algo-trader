/**
 * pulse_mobile_fullwidth.spec.js
 *
 * Verifies that Pulse cards fill the full viewport width on mobile.
 *
 * Root cause: `.mp-flat-wrap` had `padding: 0 0.3rem 0.3rem` on
 * mobile, creating ~9.6 px horizontal insets on each side. Fix:
 * zeroed side padding so `.mp-layout` + `.mp-bucket-wrap` expand to
 * 100 % viewport width. Text stays clear of the screen edge via the
 * bucket-label's own 0.35rem horizontal padding.
 *
 * Five quality dimensions:
 * 1. SSOT — checks the root cause was at .mp-flat-wrap padding, not
 *    a min-width or ag-Grid constraint
 * 2. Perf — single login via beforeAll + seedAuth; no redundant
 *    form submissions that trigger the 5/min rate-limiter
 * 3. Stale-code — verifies NO horizontal scroll on body (would reveal
 *    a min-width constraint causing overflow)
 * 4. Reuse — shares loginAsAdmin / seedAuth pattern from main_thread_perf
 * 5. UX — width within 4 px tolerance (sub-pixel rounding budget);
 *    desktop two-column layout explicitly verified as preserved
 *
 * Auth strategy: login ONCE in beforeAll via ensureJwtWithBrowser, then
 * restore via seedAuth per test — avoids hammering /api/auth/login.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE     = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const MOBILE_W = 393;
const MOBILE_H = 851;
/** Allow up to 4 px tolerance for sub-pixel rounding */
const PX_TOL   = 4;
const TIMEOUT  = 25_000;

// ── Single-login helpers (mirrored from main_thread_perf.spec.js) ─────────────

/** @type {string} */
let _sharedJwt = '';

async function ensureJwtWithBrowser(browser) {
  if (_sharedJwt) return;
  const ctx  = await browser.newContext({ baseURL: BASE });
  const page = await ctx.newPage();
  try {
    await loginAsAdmin(page);
    const token = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));
    if (!token) throw new Error('Login succeeded but no token in sessionStorage');
    _sharedJwt = token;
  } finally {
    // page.close() before ctx.close() so Playwright flushes trace buffers in
    // the correct order (avoids trace-artifact ENOENT on retain-on-failure).
    await page.close().catch(() => {});
    await ctx.close().catch(() => {});
  }
}

async function seedAuth(page, jwt) {
  await page.addInitScript((token) => {
    sessionStorage.setItem('ramboq_token', token);
    const parts = token.split('.');
    if (parts.length === 3) {
      try {
        const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
        sessionStorage.setItem('ramboq_user', JSON.stringify({
          username: payload.sub,
          role: payload.role,
          display_name: payload.display_name,
        }));
      } catch (_) { /* ignore */ }
    }
  }, jwt);
}

// ── Tests ─────────────────────────────────────────────────────────────────────

test.describe('Pulse cards — mobile full-width', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(60_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    await ensureJwtWithBrowser(browser);
  });

  test('mobile 393×851 — .mp-layout fills viewport width, no horizontal scroll', async ({ page }) => {
    await page.setViewportSize({ width: MOBILE_W, height: MOBILE_H });
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.locator('.mp-bucket-wrap').first().waitFor({ state: 'attached', timeout: TIMEOUT });

    const metrics = await page.evaluate(() => {
      const layout = document.querySelector('.mp-layout');
      const wrap   = document.querySelector('.mp-flat-wrap');
      const bucket = document.querySelector('.mp-bucket-wrap');
      const vw     = window.innerWidth;
      return {
        vw,
        scrollW:  document.documentElement.scrollWidth,
        layoutW:  layout?.getBoundingClientRect().width  ?? -1,
        layoutX:  layout?.getBoundingClientRect().left   ?? -1,
        wrapW:    wrap?.getBoundingClientRect().width     ?? -1,
        bucketW:  bucket?.getBoundingClientRect().width   ?? -1,
      };
    });

    // 1. SSOT — .mp-layout fills the viewport (no side insets from .mp-flat-wrap)
    expect(metrics.layoutW, `mp-layout width ${metrics.layoutW} vs vw ${metrics.vw}`)
      .toBeGreaterThanOrEqual(metrics.vw - PX_TOL);

    // 2. SSOT — .mp-flat-wrap fills the viewport
    expect(metrics.wrapW, `mp-flat-wrap width ${metrics.wrapW} vs vw ${metrics.vw}`)
      .toBeGreaterThanOrEqual(metrics.vw - PX_TOL);

    // 3. SSOT — first .mp-bucket-wrap fills the viewport
    expect(metrics.bucketW, `mp-bucket-wrap width ${metrics.bucketW} vs vw ${metrics.vw}`)
      .toBeGreaterThanOrEqual(metrics.vw - PX_TOL);

    // 4. UX — no horizontal scroll (no min-width forcing overflow)
    expect(metrics.scrollW, `scrollWidth ${metrics.scrollW} > vw ${metrics.vw}`)
      .toBeLessThanOrEqual(metrics.vw + PX_TOL);
  });

  test('mobile 393×851 — .mp-layout starts at x=0 (no left inset)', async ({ page }) => {
    await page.setViewportSize({ width: MOBILE_W, height: MOBILE_H });
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.locator('.mp-bucket-wrap').first().waitFor({ state: 'attached', timeout: TIMEOUT });

    const layoutX = await page.evaluate(() =>
      document.querySelector('.mp-layout')?.getBoundingClientRect().left ?? -1
    );

    // Left edge must be at x=0 (≤ 1 px sub-pixel budget)
    expect(layoutX, `mp-layout left ${layoutX}px from viewport left`).toBeLessThanOrEqual(1);
  });

  test('mobile 393×851 — each .mp-bucket-wrap fills viewport (all cards)', async ({ page }) => {
    await page.setViewportSize({ width: MOBILE_W, height: MOBILE_H });
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.locator('.mp-bucket-wrap').first().waitFor({ state: 'attached', timeout: TIMEOUT });

    const results = await page.evaluate((tol) => {
      const vw      = window.innerWidth;
      const buckets = document.querySelectorAll('.mp-bucket-wrap');
      return Array.from(buckets).map((b, i) => {
        const r = b.getBoundingClientRect();
        return { index: i, width: r.width, fills: r.width >= vw - tol };
      });
    }, PX_TOL);

    for (const { index, width, fills } of results) {
      expect(fills, `mp-bucket-wrap[${index}] width ${width}px < ${MOBILE_W - PX_TOL}px`)
        .toBe(true);
    }
  });

  test('desktop 1400×900 — two-column layout preserved (mp-col elements side-by-side)', async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.locator('.mp-col').first().waitFor({ state: 'attached', timeout: TIMEOUT });

    const cols = await page.evaluate(() => {
      const nodes = document.querySelectorAll('.mp-col');
      return Array.from(nodes).map(n => {
        const r = n.getBoundingClientRect();
        return { left: r.left, width: r.width };
      });
    });

    // Must have exactly two columns on desktop
    expect(cols.length, 'expected 2 .mp-col elements on desktop').toBe(2);

    const [left, right] = cols;

    // Columns must be side-by-side — right col starts after left col ends
    expect(right.left, 'right col must start after left col')
      .toBeGreaterThan(left.left + left.width - PX_TOL);

    // Both columns must have non-trivial width (> 200px each)
    expect(left.width,  'left col must have width on desktop').toBeGreaterThan(200);
    expect(right.width, 'right col must have width on desktop').toBeGreaterThan(200);
  });

});
