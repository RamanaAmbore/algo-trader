// Investor portal smoke test — /investor/[token]
//
// B5 (2026-09): this page had zero e2e coverage before. It's a token-as-
// credential public route (no login) — normally an admin mints a token
// via POST /api/admin/users/{id}/investor-tokens, then anyone holding the
// URL can view it. Minting requires the `manage_investor_tokens`
// capability, which is 'designated'-role only — the default test account
// (PLAYWRIGHT_USER=rambo, role=admin) can't mint one, so this spec mocks
// the two API calls the page makes (`/api/investor/:token/slice` and
// `/api/investor/:token/history`) instead of depending on a real minted
// token. That also makes the test deterministic (fixed NAV figures) and
// fast (no DB round-trip).
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — mocks match the real Slice/HistRow typedefs declared at
//                the top of +page.svelte's <script> block.
//   2. Perf    — no login, no DB writes; pure route interception.
//   3. Stale   — n/a (new surface).
//   4. Reuse   — same page.route fixture serves both the smoke assertions
//                here and B8's hairline/vignette computed-style checks.
//   5. UX      — checks expected sections render with no console errors,
//                and that mobile (390px) doesn't overflow.

import { test, expect } from '@playwright/test';

/** @type {import('@playwright/test').Route[]} */
const SLICE_FIXTURE = {
  display_name: 'Test Partner',
  share_pct: 4.25,
  contribution: 1000000,
  firm_nav: 42000000,
  nav_share: 1785000,
  pnl: 285000,
  pnl_pct: 0.19,
  day_delta_share: 12500,
  day_delta_share_pct: 0.007,
  as_of_date: '2026-09-26',
};

function historyFixture(days = 10) {
  const rows = [];
  const base = new Date('2026-09-01T00:00:00Z');
  for (let i = 0; i < days; i++) {
    const d = new Date(base);
    d.setDate(d.getDate() + i);
    rows.push({
      as_of_date: d.toISOString().slice(0, 10),
      firm_nav: 40000000 + i * 200000,
      nav_share: 1700000 + i * 8500,
      pnl: 200000 + i * 8500,
    });
  }
  return { rows };
}

/** @param {import('@playwright/test').Page} page */
async function mockInvestorApi(page) {
  await page.route('**/api/investor/*/slice', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SLICE_FIXTURE) })
  );
  await page.route('**/api/investor/*/history*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(historyFixture()) })
  );
}

test.describe('investor portal — /investor/[token]', () => {
  test('loads, shows expected sections, no console errors', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
    page.on('pageerror', (err) => consoleErrors.push(`UNCAUGHT: ${err.message}`));

    await mockInvestorApi(page);
    await page.goto('/investor/mock-token-e2e', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(800);

    await expect(page.getByText('Investor Statement').first()).toBeVisible();
    await expect(page.getByText('Hello Test Partner', { exact: false })).toBeVisible();
    await expect(page.getByText('Your portfolio value').first()).toBeVisible();
    await expect(page.getByText('Net Profit / Loss').first()).toBeVisible();
    await expect(page.getByText("Today's move").first()).toBeVisible();
    await expect(page.getByText('Your value over time', { exact: false })).toBeVisible();
    await expect(page.locator('.ip-footer-disclaimer').first()).toBeVisible();
    await expect(page.getByText('rambo@ramboq.com')).toBeVisible();

    const meaningfulErrors = consoleErrors.filter((e) => !/favicon|ResizeObserver/i.test(e));
    expect(meaningfulErrors, `console errors: ${meaningfulErrors.join(' | ')}`).toEqual([]);
  });

  test('error state — invalid/expired token renders the error card, not a blank page', async ({ page }) => {
    await page.route('**/api/investor/*/slice', (route) =>
      route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'This link is no longer active.' }) })
    );
    await page.goto('/investor/revoked-token-e2e', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(800);

    await expect(page.locator('.ip-error').first()).toBeVisible();
    await expect(page.getByText('This link is no longer active.', { exact: false })).toBeVisible();
  });

  test('mobile (390px) — no horizontal overflow', async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await ctx.newPage();
    await mockInvestorApi(page);
    await page.goto('/investor/mock-token-e2e', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(800);

    const { scrollWidth, clientWidth } = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(scrollWidth, `scrollWidth ${scrollWidth} must not exceed clientWidth ${clientWidth}`)
      .toBeLessThanOrEqual(clientWidth + 1); // +1px rounding tolerance

    await ctx.close();
  });
});
