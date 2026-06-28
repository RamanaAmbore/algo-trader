/**
 * charts_equity_no_option_msg.spec.js
 *
 * Verifies that the Greeks strip (and any option-only messages) are
 * NOT rendered for equity / index symbols on /charts.
 *
 * Five quality dimensions:
 *  1. SSOT       — no "not an option" text or Greeks strip visible for
 *                  RELIANCE, TCS, HDFCBANK, NIFTY 50 (all non-options).
 *  2. Performance — cold-load XHR budget ≤ 25 requests.
 *  3. Stale code  — ChartWorkspace.svelte must NOT contain "Equity — no Greeks"
 *                   as a rendered placeholder string.
 *  4. Reusable    — symbol-kind detection in ChartWorkspace uses `_isOption`
 *                   derived from `/(?:CE|PE)$/i` (canonical pattern).
 *  5. UX          — chart renders cleanly: `.cw-range-group` is visible and
 *                   `.cw-greeks-strip` is absent for equity symbols.
 *
 * Target: dev.ramboq.com
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/charts_equity_no_option_msg.spec.js \
 *   --project=chromium-desktop --workers=1
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

/**
 * Inject saved sessionStorage items so tests don't burn the rate-limit
 * with repeated form submits.
 * @param {import('@playwright/test').Page} page
 * @param {Record<string, string>} items
 */
async function injectSession(page, items) {
  await page.addInitScript((data) => {
    for (const [k, v] of Object.entries(data)) {
      sessionStorage.setItem(k, v);
    }
  }, items);
  if (items.ramboq_token) {
    await page.context().setExtraHTTPHeaders({
      Authorization: `Bearer ${items.ramboq_token}`,
    });
  }
}

test.describe('/charts — equity symbols show no option-only sections', () => {
  /** @type {Record<string, string>} */
  let _session = {};

  test.beforeAll(async ({ browser }) => {
    // 60s: login retries (0s + 3s + 8s backoff) need room for rate-limit recovery.
    test.setTimeout(60_000);
    const ctx = await browser.newContext({ baseURL: BASE });
    const setupPage = await ctx.newPage();
    await loginAsAdmin(setupPage);
    // Read sessionStorage keys directly — loginAsAdmin return value gives
    // {user_id, token} but injectSession needs the actual sessionStorage map.
    _session = await setupPage.evaluate(() => {
      /** @type {Record<string, string>} */
      const out = {};
      for (const k of ['ramboq_token', 'ramboq_user']) {
        const v = sessionStorage.getItem(k);
        if (v) out[k] = v;
      }
      return out;
    });
    await setupPage.close();
    await ctx.close();
  });

  // ── 1. SSOT: no option-only noise for equity / index symbols ──────────────
  for (const symbol of ['RELIANCE', 'TCS', 'HDFCBANK', 'NIFTY 50']) {
    test(`SSOT — "${symbol}" has no option-only text or Greeks strip`, async ({ page }) => {
      test.setTimeout(45_000);
      await injectSession(page, _session);
      const url = `${BASE}/charts?symbol=${encodeURIComponent(symbol)}&mode=live`;
      await page.goto(url, { waitUntil: 'domcontentloaded' });
      // Wait for chart workspace to mount (range pill strip confirms it).
      await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 25_000 });

      // No text matching "not an option" pattern anywhere on the page.
      await expect(page.locator('text=/not an option/i')).toHaveCount(0);

      // No "Equity — no Greeks" placeholder (removed in this fix).
      await expect(page.locator('text=/equity.*no greeks/i')).toHaveCount(0);

      // Greeks strip div must be absent for non-option symbols.
      await expect(page.locator('.cw-greeks-strip')).toHaveCount(0);
    });
  }

  // ── 2. Performance: cold-load XHR budget ≤ 25 ────────────────────────────
  test('Performance — cold-load XHR count ≤ 25 for RELIANCE', async ({ browser }) => {
    test.setTimeout(45_000);
    const ctx = await browser.newContext({ baseURL: BASE });
    const page = await ctx.newPage();
    await injectSession(page, _session);

    const xhrRequests = /** @type {string[]} */ ([]);
    page.on('request', (req) => {
      if (req.resourceType() === 'xhr' || req.resourceType() === 'fetch') {
        xhrRequests.push(req.url());
      }
    });

    await page.goto(`${BASE}/charts?symbol=RELIANCE&mode=live`, {
      waitUntil: 'domcontentloaded',
    });
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 25_000 });
    // Brief pause to let deferred fetches settle.
    await page.waitForTimeout(2_000);

    expect(xhrRequests.length).toBeLessThanOrEqual(25);
    await ctx.close();
  });

  // ── 3. Stale code: source-file grep ──────────────────────────────────────
  test('Stale code — ChartWorkspace.svelte has no "Equity — no Greeks" placeholder', () => {
    const cwPath = path.resolve(
      path.dirname(new URL(import.meta.url).pathname),
      '../src/lib/ChartWorkspace.svelte',
    );
    const src = fs.readFileSync(cwPath, 'utf8');

    // The old placeholder string must not appear as a rendered message.
    expect(src).not.toContain('Equity — no Greeks');

    // The canonical detection variable must be present (SSOT check).
    expect(src).toContain('_isOption');

    // Detection pattern: digit-prefixed CE|PE suffix to avoid false-positives
    // on equities like RELIANCE that end in the letters C+E.
    expect(src).toContain('/\\d(?:CE|PE)$/i');
  });

  // ── 4. Reusable: option path still works for a known option symbol ────────
  test('Reusable — Greeks strip IS rendered for an option symbol (NIFTY CE)', async ({
    page,
  }) => {
    test.setTimeout(45_000);
    await injectSession(page, _session);

    // A tradingsymbol ending in CE triggers _isOption = true.
    // The Greeks API may return 404 in test env but the strip container
    // must mount (it shows "Loading Greeks…" or an error, but the div
    // must exist to confirm the {#if _isOption} gate fires correctly).
    const optionSymbol = 'NIFTY2560010000CE';
    await page.goto(`${BASE}/charts?symbol=${encodeURIComponent(optionSymbol)}&mode=live`, {
      waitUntil: 'domcontentloaded',
    });
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 25_000 });

    // Greeks strip container must be present for an option symbol.
    await expect(page.locator('.cw-greeks-strip')).toBeVisible({ timeout: 10_000 });
  });

  // ── 5. UX: equity chart renders cleanly, no orphan whitespace ────────────
  test('UX — RELIANCE chart renders cleanly with range group, no Greeks strip', async ({
    page,
  }) => {
    test.setTimeout(45_000);
    await injectSession(page, _session);
    await page.goto(`${BASE}/charts?symbol=RELIANCE&mode=live`, {
      waitUntil: 'domcontentloaded',
    });

    // Range group (the 1D/1W/1M/3M/6M/1Y pill row) must be visible.
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 25_000 });

    // Greeks strip must be absent — no invisible spacer div either.
    const greeksCount = await page.locator('.cw-greeks-strip').count();
    expect(greeksCount).toBe(0);

    // The chart root must still be visible (no layout collapse).
    await expect(page.locator('.cw-root')).toBeVisible();
  });
});
