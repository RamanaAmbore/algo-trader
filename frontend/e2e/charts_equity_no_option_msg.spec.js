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
 *                   or similar placeholder text as a rendered string.
 *  4. Reusable    — symbol-kind detection in ChartWorkspace uses `_isOption`
 *                   derived from `/(?:CE|PE)$/i` (canonical pattern), not a new
 *                   ad-hoc regex.
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
    const page = await browser.newPage();
    page.setDefaultTimeout(30_000);
    const result = await loginAsAdmin(page, {});
    _session = result ?? {};
    await page.close();
  });

  // Helper: open /charts for a symbol, wait for range group, return page.
  async function openChart(browser, symbol) {
    const page = await browser.newPage();
    page.setDefaultTimeout(30_000);
    await injectSession(page, _session);
    const url = `${BASE}/charts?symbol=${encodeURIComponent(symbol)}&mode=live`;
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    // Wait for the range-group pill strip — confirms chart workspace mounted.
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 20_000 });
    return page;
  }

  // ── 1. SSOT: no option-only noise for equity symbols ──────────────────────
  for (const symbol of ['RELIANCE', 'TCS', 'HDFCBANK', 'NIFTY 50']) {
    test(`SSOT — "${symbol}" has no option-only text or Greeks strip`, async ({ browser }) => {
      const page = await openChart(browser, symbol);

      // No text matching "not an option" pattern anywhere on the page.
      await expect(page.locator('text=/not an option/i')).toHaveCount(0);

      // No "Equity — no Greeks" placeholder (removed in this fix).
      await expect(page.locator('text=/equity.*no greeks/i')).toHaveCount(0);

      // Greeks strip div must be absent for non-option symbols.
      await expect(page.locator('.cw-greeks-strip')).toHaveCount(0);

      await page.close();
    });
  }

  // ── 2. Performance: cold-load XHR budget ≤ 25 ────────────────────────────
  test('Performance — cold-load XHR count ≤ 25 for RELIANCE', async ({ browser }) => {
    const page = await browser.newPage();
    page.setDefaultTimeout(30_000);
    await injectSession(page, _session);

    const xhrRequests = [];
    page.on('request', (req) => {
      if (req.resourceType() === 'xhr' || req.resourceType() === 'fetch') {
        xhrRequests.push(req.url());
      }
    });

    await page.goto(`${BASE}/charts?symbol=RELIANCE&mode=live`, {
      waitUntil: 'domcontentloaded',
    });
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 20_000 });

    // Brief pause to let deferred fetches settle.
    await page.waitForTimeout(2_000);

    expect(xhrRequests.length).toBeLessThanOrEqual(25);
    await page.close();
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

    // Detection pattern: CE|PE suffix (canonical, not new ad-hoc logic).
    expect(src).toContain('/(?:CE|PE)$/i');
  });

  // ── 4. Reusable: option path still works for a known option symbol ────────
  test('Reusable — Greeks strip IS rendered for an option symbol (NIFTY CE)', async ({
    browser,
  }) => {
    // Use a generic pattern; a real option tradingsymbol ends in CE or PE.
    // We navigate and check the strip is present. The exact symbol doesn't
    // matter — the key is that the {#if _isOption} gate is correctly true.
    const page = await browser.newPage();
    page.setDefaultTimeout(30_000);
    await injectSession(page, _session);

    // NIFTYBEES is NOT an option. Use a real F&O style symbol name:
    // NIFTY2560010000CE is a plausible option tradingsymbol.
    // In test, we only verify the gate logic by checking that a symbol
    // ending in CE causes the greeks strip to appear (or at least that
    // the page renders without crashing — Greeks API may return 404 in
    // test env but the strip container should still mount).
    const optionSymbol = 'NIFTY2560010000CE';
    const url = `${BASE}/charts?symbol=${encodeURIComponent(optionSymbol)}&mode=live`;
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 20_000 });

    // The greeks strip container must be present for an option symbol.
    // (It may show "Loading Greeks…" or an error, but the div must exist.)
    await expect(page.locator('.cw-greeks-strip')).toBeVisible({ timeout: 10_000 });

    await page.close();
  });

  // ── 5. UX: equity chart renders cleanly, no orphan whitespace ────────────
  test('UX — RELIANCE chart renders cleanly with range group, no Greeks strip gap', async ({
    browser,
  }) => {
    const page = await openChart(browser, 'RELIANCE');

    // Range group (the 1D/1W/1M/3M/6M/1Y pill row) must be visible.
    await expect(page.locator('.cw-range-group')).toBeVisible();

    // Greeks strip must be absent — no invisible spacer div either.
    const greeksCount = await page.locator('.cw-greeks-strip').count();
    expect(greeksCount).toBe(0);

    // The chart root must still be visible (no layout collapse).
    await expect(page.locator('.cw-root')).toBeVisible();

    await page.close();
  });
});
